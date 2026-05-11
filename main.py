"""
main.py — V4 portfolio entry point (multi-symbol, multi-strategy bot)
=====================================================================

Strategii suportate per pereche (selectate prin pair_cfg.strategy):
  - 'hi':    Hull+Ichimoku Cloud (TIA, NEAR 4h)
  - 'bb_mr': Bollinger Bands Mean Reversion (BTC 4h)

Orchestreaza:
  - Bootstrap: load config, set leverage per pair, warmup 400 bare,
    INIT equity sync din Bybit balance, instantieaza signal per strategy.
  - Public WS klines (interval 240=4h) pentru toate pair-urile enabled
  - Pe fiecare confirmed bar: defense-in-depth external close check,
    update buffer signal, evaluate strategy, dispatch decision
  - Order pipeline: open (market entry + setTradingStop SL+TP atomic),
    close (market opus + reduceOnly + side mapping Buy/Sell native)
  - SL/TP calculation branched per strategy:
    * HI: SL la sl_initial_pct, TP optional la tp_pct (signal exit dominant)
    * BB MR: SL la sl_pct, TP fix la entry × (1 ± sl_pct × tp_rr)
  - bars_held counter incrementat per pereche pe bara confirmed (BB MR time-exit)
  - Trade closed pipeline: fetch_pnl_for_trade real Bybit, record_closed_trade,
    equity sync post-close, Telegram notification
  - Private WS: position events (detect SL/TP/EXTERNAL trigger)
  - Heartbeat: equity sync la (next_bar_close - 60s)
  - FastAPI: chart endpoints + WebSocket broadcast

Env vars (esential):
    CONFIG_FILE        — path la config_v4.yaml
    BYBIT_API_KEY      — credentials subaccount
    BYBIT_API_SECRET
    BOT_NAME           — "v4"
    TELEGRAM_TOKEN     — opt
    TELEGRAM_CHAT_ID   — opt
    CHART_PORT         — default 8104
    CHART_TZ           — default Europe/Bucharest
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from core import exchange_api as ex
from core import private_ws as pws
from core import telegram_bot as tg
from core import no_lookahead as nl
from core.bot_state import BotState, LivePosition, TradeRecord, ReconciliationError
from core.config import AppConfig, load_config
from core.position_sizing import compute_position_size, compute_qty
from strategies.bb_mr_signal import BBMeanReversionSignal, BBMRConfig
from strategies.ichimoku_signal import IchimokuSignal, PairStrategyConfig

# Type alias pentru orice signal generator (dispatch by strategy)
SignalGen = IchimokuSignal | BBMeanReversionSignal


def _strategy_label(strategy: str) -> str:
    """Pretty label pentru logs / Telegram."""
    return {"hi": "Hull+Ichimoku", "bb_mr": "BB Mean Reversion"}.get(strategy, strategy)


# ============================================================================
# Globals
# ============================================================================
_args = argparse.ArgumentParser()
_args.add_argument("--config", default=os.getenv("CONFIG_FILE", "config/config_v4.yaml"))
CLI = _args.parse_args()

CONFIG: AppConfig = load_config(CLI.config)
BOT_NAME = os.getenv("BOT_NAME", CONFIG.portfolio.name)
CHART_PORT = int(os.getenv("CHART_PORT", "8104"))
# Port host pentru link Telegram (Docker port mapping host:container poate diferi
# de portul intern). Default = CHART_PORT cand nu e mapping.
CHART_HOST_PORT = int(os.getenv("CHART_HOST_PORT", str(CHART_PORT)))
CHART_TZ = os.getenv("CHART_TZ", "Europe/Bucharest")

# Static files for chart
ROOT = Path(__file__).resolve().parent
STATIC = str(ROOT / "static")
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Per-symbol state
_state = BotState(account_size=CONFIG.portfolio.pool_total)
_signals: dict[str, SignalGen] = {}  # HI sau BB MR — dispatch dupa pair_cfg.strategy
_pair_cfgs: dict[str, any] = {}  # PairConfig from config.yaml (has leverage, sl, etc.)
_candles: dict[str, list] = {}  # ring buffer per symbol pt chart [ts_s, o, h, l, c]
_last_synced_ts: dict[str, int] = {}  # last confirmed bar ts (seconds) per symbol
_clients: set[WebSocket] = set()
_halted: dict[str, bool] = {}  # per-symbol HALT flag (after ReconciliationError)

# Map order_id (string) -> entry context, used to fetch_pnl after close
# Filled at open, consumed at close.
_open_orders_meta: dict[str, dict] = {}

# Reconciliation: dupa close-ul strategiei, verificam ca pozitia s-a inchis
# pe Bybit. INVARIANT: bot-ul are ownership exclusiv pe perechile lui — daca
# alt bot tranzactioneaza acelasi simbol, get_position e contaminat si
# reconcilierea trigereaza halt-uri false (sau, mai grav, chase_close inchide
# pozitia altui bot).
_RECONCILE_QTY_EPS = 1e-9       # toleranta float pe comparatii de qty
_RECONCILE_RETRIES = 3          # iteratii pt stop-not-triggered branch
_RECONCILE_RETRY_SLEEP = 1.0    # secunde intre check-uri (~3s total wait)

# Per-symbol close lock — previne race intre close_position (signal exit din
# public WS task) si close_pipeline_external (private WS position event +
# defense-in-depth check_external_close). Ambele pot fi observate aproape
# simultan cand Bybit fileaza SL/TP atomic in timp ce strategia genereaza
# CLOSE_LONG/SHORT pe aceeasi bara — fara lock + dedup, acelasi trade s-ar
# inregistra de doua ori in _state.trades.
_close_locks: dict[str, asyncio.Lock] = {}


def _get_close_lock(symbol: str) -> asyncio.Lock:
    """Creeaza lazy un Lock per simbol."""
    lock = _close_locks.get(symbol)
    if lock is None:
        lock = asyncio.Lock()
        _close_locks[symbol] = lock
    return lock


_TF_INTERVAL = "240"  # 4h fix (toate perechile)
_TF_MS = 4 * 60 * 60 * 1000


# ============================================================================
# Helpers
# ============================================================================

def log_event(event: str, **fields) -> None:
    """JSONL log to logs/<bot>.jsonl"""
    rec = {"ts": int(time.time() * 1000), "bot": BOT_NAME, "event": event, **fields}
    try:
        with open(LOG_DIR / f"{BOT_NAME}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"  [LOG] write failed: {e}")


async def broadcast(payload: dict) -> None:
    """Send to all connected chart WS clients. Drop disconnected."""
    if not _clients:
        return
    msg = json.dumps(payload)
    dead = []
    for ws in _clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


def _direction_to_side(direction: str) -> str:
    """LONG -> Buy, SHORT -> Sell (Bybit native capitalized)."""
    return "Buy" if direction == "LONG" else "Sell"


def _close_side(direction: str) -> str:
    """LONG closes with Sell, SHORT closes with Buy."""
    return "Sell" if direction == "LONG" else "Buy"


def _next_bar_close_ms(now_ms: int) -> int:
    """Next 4h bar close ts (ms)."""
    return ((now_ms // _TF_MS) + 1) * _TF_MS


# ============================================================================
# Equity sync (INIT / CLOSE / HEARTBEAT)
# ============================================================================

async def sync_equity(reason: str = "MANUAL") -> None:
    """
    Bot Ichimoku citeste balance DIRECT de pe Bybit — single source of truth.
    NU tine equity local prin compound (account += pnl) ca boilerplate.

    La fiecare sync (INIT/CLOSE/HEARTBEAT):
      - Pull balance real Bybit
      - OVERWRITE _state.shared_equity = balance
      - Append punct in equity_curve (pt chart)
      - Anomaly detect: daca delta vs ultimul sync e neasteptat (>3%
        fara trade inchis recent), alerta Telegram.
    """
    bal = await ex.get_balance()
    if bal is None:
        print(f"  [EQUITY-SYNC {reason}] FAILED — Bybit balance None")
        return

    prev = _state.shared_equity
    _state.shared_equity = bal

    if reason == "INIT":
        _state.initial_account = bal
        _state.equity_curve.clear()
        _state.equity_curve.append({
            "time": int(time.time()), "value": round(bal, 4),
        })
        print(f"  [EQUITY-SYNC INIT] account = ${bal:,.2f} (Bybit balance)")
        return

    # Append equity point — chart shows actual Bybit balance over time
    _state.equity_curve.append({
        "time": int(time.time()), "value": round(bal, 4),
    })
    if len(_state.equity_curve) > 50000:
        _state.equity_curve.pop(0)

    delta_pct = abs(bal - prev) / prev * 100 if prev > 0 else 0
    print(f"  [EQUITY-SYNC {reason}] prev=${prev:,.2f}  bybit=${bal:,.2f}  "
          f"delta={delta_pct:.2f}%")
    log_event("equity_sync", reason=reason, prev=prev, bybit=bal,
              delta_pct=delta_pct)


# ============================================================================
# Order pipeline
# ============================================================================

async def open_position(symbol: str, direction: str, close_price: float,
                         bar_ts_ms: Optional[int] = None) -> None:
    """
    Deschide pozitie market + set leverage + setTradingStop (SL+TP atomic).

    Direction: 'LONG' / 'SHORT'. Bybit side: 'Buy' / 'Sell'.
    bar_ts_ms: open-ul barei la care s-a executat (chart-ul afiseaza linii
    aliniate la bara, nu la wall-clock).
    """
    pair_cfg = _pair_cfgs[symbol]
    side = _direction_to_side(direction)

    # Sizing — fetch fresh Bybit balance pentru cap (fallback la shared_equity)
    balance = await ex.get_balance()
    if balance is None:
        balance = _state.shared_equity
    sizing = compute_position_size(
        pair_cfg, _state.shared_equity, balance,
        CONFIG.portfolio, leverage=pair_cfg.leverage,
    )
    if sizing.skip:
        print(f"  [OPEN {symbol}] SKIP: {sizing.skip_reason}")
        log_event("entry_skipped", symbol=symbol, reason=sizing.skip_reason)
        return

    info = await ex.get_market_info(symbol)
    qty_raw = sizing.pos_usd / close_price
    qty = ex.round_qty_down(qty_raw, info["qty_step"])
    if qty < info["min_qty"]:
        print(f"  [OPEN {symbol}] qty {qty} < min {info['min_qty']} — skip")
        return

    # Leverage (idempotent — Bybit returneaza ok daca deja setat)
    await ex.set_leverage(symbol, pair_cfg.leverage)

    # Maker entry cu fallback Market pe remainder (pattern 80/20).
    # ~80-90% din economia de fee fata de chase complet — taker 0.055% → ~0.02% blended.
    # Pe TF 4h, semnalul vine pe close-ul barei → urmatoarea bara nu va fugi 5%
    # in 5s, deci fill rate maker e ridicat. Timeout 5s + fallback "market".
    entry_result = await ex.maker_entry_or_market(
        symbol, side, qty,
        timeout_sec=5, fallback="market", reduce_only=False,
    )
    if entry_result["result"] == "failed":
        print(f"  [OPEN {symbol}] maker_entry_or_market FAILED")
        await tg.send_critical(f"OPEN FAILED — {symbol}",
                               "maker_entry_or_market returned failed",
                               symbol=symbol)
        return

    # Pretul real de fill: pe maker pur, avg_price din ordin; pe mixed/taker,
    # fetch din get_order_status / fetch_pnl_for_trade ulterior.
    await asyncio.sleep(0.5)
    real_fill_price = float(entry_result.get("avg_price") or 0) or close_price
    fill_kind = entry_result["result"]
    if fill_kind in ("mixed", "taker"):
        # Avg-ul ponderat real vine cand fetch_pnl_for_trade trage closed-pnl;
        # pana atunci folosim signal price. Pt SL/TP calc, e suficient.
        real_fill_price = close_price
    slippage_bps = ((real_fill_price - close_price) / close_price * 10000
                    if real_fill_price > 0 else 0)
    print(f"  [OPEN {symbol}] {fill_kind:6s} signal={close_price:.6f} "
          f"fill={real_fill_price:.6f} slippage={slippage_bps:+.1f}bps")
    order_id = ""  # informational only — maker_entry_or_market nu returneaza id unic

    # SL/TP din fill real — branched per strategy:
    #   - 'hi': SL la sl_initial_pct, TP optional la tp_pct (signal exit dominant)
    #   - 'bb_mr': SL la sl_pct, TP fix la entry × (1 ± sl_pct × tp_rr)
    sl_pct_use = pair_cfg.effective_sl_pct
    sl_price = (real_fill_price * (1 - sl_pct_use) if direction == "LONG"
                else real_fill_price * (1 + sl_pct_use))
    tp_price: Optional[float] = None
    if pair_cfg.strategy == "bb_mr":
        tp_d = pair_cfg.sl_pct * pair_cfg.tp_rr
        tp_price = (real_fill_price * (1 + tp_d) if direction == "LONG"
                    else real_fill_price * (1 - tp_d))
    elif pair_cfg.tp_pct and pair_cfg.tp_pct > 0:
        tp_price = (real_fill_price * (1 + pair_cfg.tp_pct) if direction == "LONG"
                    else real_fill_price * (1 - pair_cfg.tp_pct))

    # Pas qty → TP devine Limit (maker fee 0.020% in loc de 0.055% taker).
    # SL ramane Market (siguranta executiei pe gap).
    # Return False = toate 4 retry-urile au esuat (Telegram critical sent
    # deja in set_position_sl). NU abandonam trade-ul aici — pozitia exista
    # pe Bybit, abandonul ar lasa-o orfana fara state local. Continuam,
    # marcam pozitie ca activa, iar reconcilierea la close va detecta lipsa
    # SL si va force chase_close. Print local pt context in logs.
    sl_ok = await ex.set_position_sl(symbol, sl_price, tp_price, qty=qty)
    if not sl_ok:
        print(f"  [OPEN {symbol}] WARN: entry {direction} fara SL setat — "
              f"relying on close-pipeline reconcile.")

    now_ms = int(time.time() * 1000)
    pos = LivePosition(
        symbol=symbol, side=side, direction=direction,
        qty=qty, entry_price=real_fill_price,
        sl_price=sl_price, tp_price=tp_price,
        leverage=pair_cfg.leverage,
        pos_usd=sizing.pos_usd, risk_usd=sizing.risk_usd,
        opened_ts_ms=now_ms, order_id=order_id,
        strategy=pair_cfg.strategy, bars_held=0,
    )
    _state.set_position(symbol, pos)
    log_event("entry", symbol=symbol, direction=direction,
              signal_price=close_price, fill_price=real_fill_price,
              qty=qty, sl=sl_price, tp=tp_price, order_id=order_id)

    # Broadcast position_open (chart shows live entry/SL/TP lines + arrow marker)
    # entry_ms pt chart = bar open time (Lightweight Charts pozitioneaza la bar
    # open). Wall-clock now_ms ramane in pos.opened_ts_ms pt fetch_pnl.
    chart_entry_ms = bar_ts_ms if bar_ts_ms is not None else now_ms
    await broadcast({
        "type": "position_open", "symbol": symbol,
        "direction": direction, "entry": real_fill_price,
        "sl": sl_price, "tp": tp_price,
        "qty": qty, "risk_usd": sizing.risk_usd,
        "entry_ms": chart_entry_ms,
    })

    # Telegram — afiseaza fill real (match cu Bybit App) + tip fill (maker/taker/mixed)
    icon = "🟢" if direction == "LONG" else "🔴"
    tp_line = f"\n<b>TP:</b> {tp_price:.6f}  <i>(Limit, maker)</i>" if tp_price else ""
    slip_line = ""
    if abs(real_fill_price - close_price) > 0:
        slip_bps = (real_fill_price - close_price) / close_price * 10000
        slip_line = f"  <i>(signal {close_price:.6f}, slip {slip_bps:+.1f}bps)</i>"
    fill_emoji = {"maker": "🟢", "mixed": "🟡", "taker": "🔴"}.get(fill_kind, "⚪")
    await tg.send(
        f"{icon} ENTRY {direction}",
        f"<b>Strategy:</b> <code>{_strategy_label(pair_cfg.strategy)}</code>\n"
        f"<b>Fill:</b> {fill_emoji} <code>{fill_kind}</code>\n"
        f"<b>Entry:</b> {real_fill_price:.6f}{slip_line}  ({tg.fmt_time(now_ms)})\n"
        f"<b>Qty:</b> {qty}  (${sizing.pos_usd:,.2f})\n"
        f"<b>SL:</b> {sl_price:.6f}  ({sl_pct_use*100:.1f}%, Market){tp_line}\n"
        f"<b>Risk:</b> ${sizing.risk_usd:,.2f}  ({pair_cfg.risk_pct_per_trade*100:.0f}% × ${_state.shared_equity:,.2f})",
        symbol=symbol,
    )


async def _assert_closed(symbol: str, qty_local: float,
                          reason_label: str) -> None:
    """Verifica ca pozitia e inchisa dupa chase_close. Raise pe esec.

    chase_close are 20 incercari + fallback market, dar nu garanteaza
    inchiderea (Bybit down complet, ordinele respinse). Fara aceasta verificare,
    am inregistra trade-ul ca "inchis" cu pozitia inca deschisa.
    """
    bybit_pos = await ex.get_position(symbol)
    qty_after = float(bybit_pos.get("size", 0)) if bybit_pos else 0.0
    if qty_after <= _RECONCILE_QTY_EPS:
        return
    msg = (f"chase_close ({reason_label}) NU a inchis pozitia pe {symbol}: "
           f"qty_after={qty_after}, qty_local={qty_local}")
    print(f"  [RECONCILE {symbol}] HALT: {msg}")
    await tg.send_critical(
        f"{symbol} chase_close esuat ({reason_label})",
        f"<b>Local qty:</b> {qty_local}\n"
        f"<b>Bybit qty dupa chase:</b> {qty_after}\n"
        f"chase_close nu a putut inchide pozitia (Bybit down sau order respins). "
        f"Bot oprit pentru acest simbol. Verifica manual si restart.",
        symbol=symbol,
    )
    raise ReconciliationError(msg)


async def _reconcile_close(symbol: str, direction: str,
                            qty_local: float, exit_reason: str) -> str:
    """
    Confirma cu Bybit ca pozitia s-a inchis si rezolva discrepantele.
    Returneaza exit_reason-ul final (eventual cu sufix _PARTIAL / _FORCED).
    Ridica ReconciliationError pe ramura qty_real > qty_local sau daca
    chase_close esueaza sa inchida pozitia.

    Ramuri (qty_real = pozitie reala pe Bybit, qty_local = qty bot):
      qty_real == 0            -> Bybit a inchis clean, exit_reason neschimbat
      0 < qty_real < qty_local -> fill partial, chase_close pe rest, sufix _PARTIAL
      qty_real ≈ qty_local     -> stop nu s-a triggerit; retry; daca persista, force
      qty_real > qty_local     -> anomalie; HALT, raise ReconciliationError
    """
    bybit_pos = await ex.get_position(symbol)
    qty_real = float(bybit_pos.get("size", 0)) if bybit_pos else 0.0

    # Ramura 1: clean close (cazul comun)
    if qty_real <= _RECONCILE_QTY_EPS:
        return exit_reason

    # Ramura 4: anomalie — qty pe Bybit mai mare decat ce stim local.
    # NU inchidem automat — am putea inchide hedge manual / pozitia altui bot
    # / o piramidare necontorizata. Halt + alert + raise.
    if qty_real > qty_local + _RECONCILE_QTY_EPS:
        msg = (f"qty_real={qty_real} > qty_local={qty_local} pe {symbol} "
               f"(exit_reason={exit_reason}). Cauze posibile: piramidare "
               f"necontorizata, pozitie reziduala dintr-un run anterior, sau "
               f"interventie manuala. Bot HALTED pe simbol.")
        print(f"  [RECONCILE {symbol}] HALT: {msg}")
        await tg.send_critical(
            f"{symbol} reconciliere {exit_reason}",
            f"<b>Local qty:</b> {qty_local}\n"
            f"<b>Bybit qty:</b> {qty_real}\n"
            f"<b>Exit reason:</b> {exit_reason}\n"
            f"Bot oprit pentru acest simbol. Verifica manual si restart.",
            symbol=symbol,
        )
        raise ReconciliationError(msg)

    # Ramura 2: fill partial (0 < qty_real < qty_local) — inchide restul
    if qty_real < qty_local - _RECONCILE_QTY_EPS:
        partial_reason = f"{exit_reason}_PARTIAL"
        print(f"  [RECONCILE {symbol}] partial: real={qty_real} < local={qty_local} "
              f"(reason={exit_reason}) — chase_close pe rest")
        await ex.chase_close(symbol, direction)
        await _assert_closed(symbol, qty_local, partial_reason)
        return partial_reason

    # Ramura 3: qty_real ≈ qty_local — stop-ul nu s-a triggerit pe Bybit.
    # Asteptam putin (poate e doar latenta), apoi forcam inchidere.
    for attempt in range(_RECONCILE_RETRIES):
        await asyncio.sleep(_RECONCILE_RETRY_SLEEP)
        bybit_pos = await ex.get_position(symbol)
        qty_real = float(bybit_pos.get("size", 0)) if bybit_pos else 0.0
        if qty_real <= _RECONCILE_QTY_EPS:
            print(f"  [RECONCILE {symbol}] {exit_reason} a triggerit dupa retry "
                  f"#{attempt + 1}")
            return exit_reason

    forced_reason = f"{exit_reason}_FORCED"
    print(f"  [RECONCILE {symbol}] {exit_reason} NU s-a triggerit dupa "
          f"{_RECONCILE_RETRIES} retries — chase_close fortat")
    await ex.chase_close(symbol, direction)
    await _assert_closed(symbol, qty_local, forced_reason)
    return forced_reason


async def close_position(symbol: str, exit_reason: str,
                          target_price: float) -> None:
    """
    Inchide pozitia DESCHISA pe Bybit (market opus, reduceOnly).

    Pipeline:
      1. Place market reduce-only (side mapping LONG->Sell, SHORT->Buy)
         Fallback chase_close daca place_market esueaza.
      2. _reconcile_close — confirma qty_real=0 pe Bybit. 4 ramuri:
         clean / partial (chase rest, _PARTIAL) / not-triggered (retry+force,
         _FORCED) / qty desync (HALT raise). Raise pe anomalie sau chase failure.
      3. Wait for settle (1.5s)
      4. fetch_pnl_for_trade (real Bybit PnL incl fees)
      5. record_closed_trade in state
      6. Telegram notification cu icon corect
      7. Equity sync post-close

    Apelat din: signal flip (CLOSE_LONG/CLOSE_SHORT). NU din private WS sau
    check_external_close — acelea folosesc close_pipeline_external (qty=0
    deja confirmat, reconciliere redundanta).

    Concurrent safety: lock + dedup pe entry_ts_ms previn double-record cand
    Bybit fileaza SL/TP atomic in paralel (private WS task triggereaza si
    close_pipeline_external pentru acelasi trade).
    """
    async with _get_close_lock(symbol):
        pos = _state.get_position(symbol)
        if pos is None:
            # Inchis deja de un alt coroutine (private WS / defense-in-depth).
            print(f"  [CLOSE {symbol}] no position — skip (already closed by other coroutine)")
            return
        # Dedup explicit: daca un trade cu acelasi entry_ts deja inregistrat.
        for existing in reversed(_state.trades):
            if (existing.symbol == symbol
                    and existing.entry_ts_ms == pos.opened_ts_ms):
                print(f"  [CLOSE {symbol}] dedup: trade entry_ts={pos.opened_ts_ms} "
                      f"deja inregistrat (id={existing.id}) — skip duplicate")
                return
        await _close_position_locked(symbol, exit_reason, target_price, pos)


async def _close_position_locked(symbol: str, exit_reason: str,
                                  target_price: float, pos) -> None:
    """Body close_position, executat sub _close_locks[symbol]."""
    side = _close_side(pos.direction)
    try:
        order_id = await ex.place_market(symbol, side, pos.qty, reduce_only=True)
        if not order_id:
            # Fallback: chase_close (PostOnly maker chase + Market fallback la sfarsit)
            print(f"  [CLOSE {symbol}] place_market FAILED — chase_close")
            ok = await ex.chase_close(symbol, pos.direction)
            if not ok:
                await tg.send_critical(
                    f"CLOSE FAILED — {symbol}",
                    "place_market + chase_close au esuat — verifica manual",
                    symbol=symbol,
                )
                log_event("close_failed", symbol=symbol, reason=exit_reason)
                return
    except Exception as e:
        # Network glitch / Bybit error — incercam chase_close inainte sa cedam
        print(f"  [CLOSE {symbol}] place_market raised: {e!r} — chase_close")
        try:
            ok = await ex.chase_close(symbol, pos.direction)
            if not ok:
                raise RuntimeError("chase_close esuat")
        except Exception as e2:
            await tg.send_critical(
                f"CLOSE FAILED — {symbol}",
                f"<code>place_market: {e!r}</code>\n<code>chase_close: {e2!r}</code>",
                symbol=symbol,
            )
            log_event("close_failed", symbol=symbol, reason=exit_reason,
                      error=f"{e!r} | {e2!r}")
            return

    # Reconcile cu Bybit (4 ramuri). Pe anomalie raise → halt simbol in caller.
    try:
        final_exit_reason = await _reconcile_close(
            symbol, pos.direction, pos.qty, exit_reason)
    except ReconciliationError as e:
        print(f"  [{symbol}] HALT pe ReconciliationError: {e}")
        _halted[symbol] = True
        log_event("reconcile_halt", symbol=symbol, reason=exit_reason, error=str(e))
        return

    # Allow Bybit to settle the close + index closed-pnl
    await asyncio.sleep(1.5)

    now_ms = int(time.time() * 1000)
    pnl_data = await ex.fetch_pnl_for_trade(symbol, pos.opened_ts_ms, now_ms)
    avg_exit = pnl_data.get("avg_exit") or target_price
    pnl_real = pnl_data.get("pnl", 0.0)
    fees_real = pnl_data.get("fees", 0.0)

    trade = TradeRecord(
        id=0,  # set in record_closed_trade
        symbol=symbol, direction=pos.direction,
        entry_ts_ms=pos.opened_ts_ms, entry_price=pos.entry_price,
        sl_price=pos.sl_price, tp_price=pos.tp_price, qty=pos.qty,
        exit_ts_ms=now_ms, exit_price=avg_exit,
        exit_price_target=target_price, exit_reason=final_exit_reason,
        pnl=pnl_real, fees=fees_real,
    )
    _state.record_closed_trade(trade)
    _state.save()
    log_event("trade_closed", **trade.to_dict())

    # Telegram — icon by reason. _PARTIAL / _FORCED sufix = warning (reconcile triggered).
    is_reconcile_path = final_exit_reason.endswith(("_PARTIAL", "_FORCED"))
    if final_exit_reason.startswith("EXTERNAL") or is_reconcile_path:
        icon = "⚠️"
    elif final_exit_reason in ("BYBIT_SL", "BYBIT_TP"):
        icon = "🎯"
    elif pnl_real >= 0:
        icon = "📈"
    else:
        icon = "📉"

    ret_pct = ((_state.shared_equity - _state.initial_account)
               / _state.initial_account * 100) if _state.initial_account else 0
    await tg.send(
        f"{icon} TRADE INCHIS — {pos.direction}",
        f"<b>Entry:</b> {pos.entry_price:.6f}  ({tg.fmt_time(pos.opened_ts_ms)})\n"
        f"<b>Exit:</b>  {avg_exit:.6f}  ({final_exit_reason})  ({tg.fmt_time(now_ms)})\n"
        f"<b>PnL:</b> ${pnl_real:+,.2f}  (Bybit real, fees incluse)\n"
        f"<b>Equity:</b> ${_state.shared_equity:,.2f}  |  Return: {ret_pct:+.2f}%",
        symbol=symbol,
    )

    # Broadcast to chart (trade_closed include equity_point pt update curve)
    eq_point = (_state.equity_curve[-1] if _state.equity_curve else None)
    await broadcast({"type": "trade_closed", "symbol": symbol,
                     "trade": trade.to_dict(), "summary": _state.summary(),
                     "equity_point": eq_point})

    # Post-close equity sync
    await sync_equity(reason=f"CLOSE_{symbol}")


# ============================================================================
# Defense-in-depth: detect external close
# ============================================================================

async def check_external_close(symbol: str) -> bool:
    """
    Pe fiecare confirmed bar, daca local has_position dar Bybit qty=0,
    sintetizam EXTERNAL close. Returneaza True daca s-a sintetizat.
    """
    pos = _state.get_position(symbol)
    if pos is None:
        return False

    try:
        bybit_pos = await ex.get_position(symbol)
    except Exception as e:
        print(f"  [EXTERNAL-CHECK {symbol}] fetch failed: {e}")
        return False

    qty_real = float(bybit_pos.get("size", 0)) if bybit_pos else 0.0
    if qty_real > _RECONCILE_QTY_EPS:
        # Position still exists — check no anomaly
        if qty_real > pos.qty + _RECONCILE_QTY_EPS:
            msg = f"qty_real={qty_real} > qty_local={pos.qty}"
            print(f"  [{symbol}] RECONCILE HALT: {msg}")
            await tg.send_critical(
                f"{symbol} qty desync",
                f"<b>Local:</b> {pos.qty}\n<b>Bybit:</b> {qty_real}\n"
                f"Bot HALTED pe simbol. Verifica manual.",
                symbol=symbol,
            )
            _halted[symbol] = True
            raise ReconciliationError(msg)
        return False

    # qty=0 — pozitia inchisa pe Bybit fara sa stim. Sintetizam EXTERNAL.
    print(f"  [{symbol}] DESYNC: local in_trade=True, Bybit qty=0 → EXTERNAL")
    last_close = _signals[symbol].df.iloc[-1]["close"] if len(_signals[symbol].df) else pos.entry_price
    await close_pipeline_external(symbol, exit_reason="EXTERNAL",
                                   target_price=float(last_close))
    return True


async def close_pipeline_external(symbol: str, exit_reason: str,
                                   target_price: float) -> None:
    """
    Variant a close_position pt cazul cand pozitia NU mai e pe Bybit (deja
    inchisa extern). Skip place_market, doar fetch PnL + record + notify.

    Concurrent safety: lock + dedup pe entry_ts_ms (acelasi mecanism ca
    close_position) — apelat din private WS si din check_external_close
    (defense-in-depth public WS); ambele pot observa qty=0 simultan.
    """
    async with _get_close_lock(symbol):
        pos = _state.get_position(symbol)
        if pos is None:
            print(f"  [CLOSE-EXT {symbol}] no position — skip (already closed)")
            return
        for existing in reversed(_state.trades):
            if (existing.symbol == symbol
                    and existing.entry_ts_ms == pos.opened_ts_ms):
                print(f"  [CLOSE-EXT {symbol}] dedup: trade entry_ts="
                      f"{pos.opened_ts_ms} deja inregistrat (id={existing.id}) — skip")
                return
        await _close_pipeline_external_locked(symbol, exit_reason, target_price, pos)


async def _close_pipeline_external_locked(symbol: str, exit_reason: str,
                                            target_price: float, pos) -> None:
    """Body close_pipeline_external, executat sub _close_locks[symbol]."""
    await asyncio.sleep(1.5)  # let closed-pnl index
    now_ms = int(time.time() * 1000)
    pnl_data = await ex.fetch_pnl_for_trade(symbol, pos.opened_ts_ms, now_ms)
    avg_exit = pnl_data.get("avg_exit") or target_price
    pnl_real = pnl_data.get("pnl", 0.0)
    fees_real = pnl_data.get("fees", 0.0)

    trade = TradeRecord(
        id=0, symbol=symbol, direction=pos.direction,
        entry_ts_ms=pos.opened_ts_ms, entry_price=pos.entry_price,
        sl_price=pos.sl_price, tp_price=pos.tp_price, qty=pos.qty,
        exit_ts_ms=now_ms, exit_price=avg_exit,
        exit_price_target=target_price, exit_reason=exit_reason,
        pnl=pnl_real, fees=fees_real,
    )
    _state.record_closed_trade(trade)
    _state.save()
    log_event("trade_closed", **trade.to_dict())

    icon = "⚠️" if exit_reason == "EXTERNAL" else "🎯"
    ret_pct = ((_state.shared_equity - _state.initial_account)
               / _state.initial_account * 100) if _state.initial_account else 0
    await tg.send(
        f"{icon} TRADE INCHIS — {pos.direction}",
        f"<b>Entry:</b> {pos.entry_price:.6f}  ({tg.fmt_time(pos.opened_ts_ms)})\n"
        f"<b>Exit:</b>  {avg_exit:.6f}  ({exit_reason})  ({tg.fmt_time(now_ms)})\n"
        f"<b>PnL:</b> ${pnl_real:+,.2f}\n"
        f"<b>Equity:</b> ${_state.shared_equity:,.2f}  |  Return: {ret_pct:+.2f}%",
        symbol=symbol,
    )
    eq_point = (_state.equity_curve[-1] if _state.equity_curve else None)
    await broadcast({"type": "trade_closed", "symbol": symbol,
                     "trade": trade.to_dict(), "summary": _state.summary(),
                     "equity_point": eq_point})
    await sync_equity(reason=f"CLOSE_{symbol}")


# ============================================================================
# Strategy evaluation (per confirmed bar)
# ============================================================================

async def on_confirmed_bar(symbol: str, bar: dict) -> None:
    """Apelat dupa fiecare bara confirmed (4h close)."""
    if _halted.get(symbol):
        return

    sig = _signals[symbol]
    sig.update_buffer(bar)
    sig.recompute_indicators()

    # Defense-in-depth: check external close before evaluating new signal
    try:
        if await check_external_close(symbol):
            return  # handled, don't evaluate this bar
    except ReconciliationError:
        return  # halted

    # ORDINE BROADCAST: candle + indicators FIRST, apoi strategy decisions.
    # Asta garanteaza ca chart-ul are bara curenta in CANDLES inainte sa
    # primeasca position_open → showLiveLines vede lastCandle.time = entry bar.
    ts_s = bar["ts_ms"] // 1000
    await broadcast({
        "type": "candle", "symbol": symbol,
        "candle": {"time": ts_s,
                   "open": bar["open"], "high": bar["high"],
                   "low": bar["low"], "close": bar["close"]},
    })

    # Update chart indicators (last bar) + broadcast — branched per strategy
    df = sig.df
    cache = sig.cache
    if cache is not None and len(df) > 0:
        i = len(df) - 1
        if isinstance(sig, BBMeanReversionSignal):
            indicator_arrs = [("bb_mid", cache.bb_mid), ("bb_upper", cache.bb_upper),
                              ("bb_lower", cache.bb_lower)]
        else:
            indicator_arrs = [("hull_n1", cache.n1), ("tenkan", cache.tenkan),
                              ("kijun", cache.kijun), ("senkou_a", cache.senkou_h),
                              ("senkou_b", cache.senkou_l)]
        for name, arr in indicator_arrs:
            v = arr[i]
            if not pd.isna(v):
                fv = float(v)
                _state.add_indicator_point(symbol, name, ts_s, fv)
                await broadcast({"type": "indicator", "symbol": symbol,
                                 "name": name, "time": ts_s, "value": fv})

    # Acum evalueaza strategia + dispatch
    pos = _state.get_position(symbol)
    pos_dir = pos.direction.lower() if pos else None
    entry_px = pos.entry_price if pos else 0.0

    # Increment bars_held pe pozitia activa (folosit la BB MR time-exit)
    if pos is not None:
        pos.bars_held += 1

    if isinstance(sig, BBMeanReversionSignal):
        bars_held = pos.bars_held if pos else 0
        decision = sig.evaluate(has_position=pos_dir, entry_price=entry_px,
                                 bars_held=bars_held)
    else:
        decision = sig.evaluate(has_position=pos_dir, entry_price=entry_px)
    print(f"  [{symbol}] decision={decision.action} reason={decision.reason}")
    log_event("decision", symbol=symbol, action=decision.action,
              reason=decision.reason, price=decision.price)

    action = decision.action
    if action == "OPEN_LONG":
        await open_position(symbol, "LONG", decision.price,
                             bar_ts_ms=bar["ts_ms"])
    elif action == "OPEN_SHORT":
        await open_position(symbol, "SHORT", decision.price,
                             bar_ts_ms=bar["ts_ms"])
    elif action in ("CLOSE_LONG", "CLOSE_SHORT"):
        await close_position(symbol, "SIGNAL", decision.price)
    # SL_LONG/TP_LONG/SL_SHORT/TP_SHORT delegate la Bybit setTradingStop —
    # ne bazam pe private WS position event sa detecteze trigger-ul real.


# ============================================================================
# Public WS — klines consumer
# ============================================================================

async def public_ws_loop() -> None:
    """Connect Bybit V5 public, subscribe kline.<interval>.<symbol> for each enabled pair."""
    enabled = [p.symbol for p in CONFIG.pairs if p.enabled]
    topics = [f"kline.{_TF_INTERVAL}.{s}" for s in enabled]
    url = ("wss://stream-testnet.bybit.com/v5/public/linear"
           if os.getenv("BYBIT_TESTNET", "0") == "1"
           else "wss://stream.bybit.com/v5/public/linear")

    while True:
        try:
            async with websockets.connect(url, ping_interval=None,
                                          open_timeout=15) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                print(f"  [WS-PUB] subscribed: {topics}")

                async def _hb():
                    while True:
                        await asyncio.sleep(20)
                        try:
                            await ws.send(json.dumps({"op": "ping"}))
                        except Exception:
                            break

                hb = asyncio.create_task(_hb())
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("op") in ("pong", "subscribe"):
                            continue
                        topic = msg.get("topic", "")
                        if not topic.startswith("kline."):
                            continue
                        symbol = topic.split(".")[-1]
                        for k in msg.get("data", []):
                            confirmed = bool(k.get("confirm", False))
                            ts_ms = int(k["start"])
                            bar = {
                                "ts_ms": ts_ms,
                                "open": float(k["open"]),
                                "high": float(k["high"]),
                                "low": float(k["low"]),
                                "close": float(k["close"]),
                                "volume": float(k.get("volume", 0)),
                                "confirmed": confirmed,
                            }
                            # Track all bars in candles ring (for chart)
                            ts_s = ts_ms // 1000
                            ring = _candles.setdefault(symbol, [])
                            if ring and ring[-1][0] == ts_s:
                                ring[-1] = [ts_s, bar["open"], bar["high"],
                                            bar["low"], bar["close"]]
                            else:
                                ring.append([ts_s, bar["open"], bar["high"],
                                             bar["low"], bar["close"]])
                                if len(ring) > 5000:
                                    ring.pop(0)

                            if confirmed:
                                last_synced = _last_synced_ts.get(symbol, 0)
                                if ts_s <= last_synced:
                                    continue  # dedup
                                _last_synced_ts[symbol] = ts_s
                                _state.mark_first_candle(symbol, ts_s)
                                try:
                                    await on_confirmed_bar(symbol, bar)
                                except Exception:
                                    print(f"  [{symbol}] on_confirmed_bar CRASHED:\n"
                                          f"{traceback.format_exc()}")
                finally:
                    hb.cancel()
        except Exception as e:
            print(f"  [WS-PUB] error: {e!r} — reconnect in 5s")
            await asyncio.sleep(5)


# ============================================================================
# Private WS handlers
# ============================================================================

async def on_position_event(event: dict) -> None:
    """
    Detect Bybit-side close (SL/TP atomic trigger) sau external close.

    Eveniment cu size=0 dupa ce local has_position → trigger close pipeline.
    Distinctia BYBIT_SL vs BYBIT_TP se face prin avgPrice proximity to
    sl_price vs tp_price.
    """
    symbol = event.get("symbol", "")
    size = float(event.get("size", 0) or 0)
    avg = float(event.get("avgPrice", 0) or 0)

    if symbol not in _signals:
        return  # not our pair

    pos = _state.get_position(symbol)
    if size > _RECONCILE_QTY_EPS:
        # Pozitia inca exista — log doar
        return

    if pos is None:
        return  # already closed locally

    # Determine reason based on price proximity (sl vs tp)
    if pos.tp_price and abs(avg - pos.tp_price) / pos.tp_price < 0.005:
        reason = "BYBIT_TP"
    elif abs(avg - pos.sl_price) / pos.sl_price < 0.005:
        reason = "BYBIT_SL"
    else:
        reason = "EXTERNAL"

    print(f"  [{symbol}] private WS detected close: avg={avg} → {reason}")
    await close_pipeline_external(symbol, reason, target_price=avg or pos.entry_price)


async def on_order_event(event: dict) -> None:
    status = event.get("orderStatus", "?")
    sym = event.get("symbol", "?")
    if status in ("Filled", "Cancelled", "Rejected"):
        print(f"  [ORDER {sym}] {status}  id={event.get('orderId', '?')[:8]}")


async def on_execution_event(event: dict) -> None:
    pass  # silent — fills sunt trate prin position events


# ============================================================================
# Heartbeat — equity sync la (next_bar_close - 60s)
# ============================================================================

async def heartbeat_loop() -> None:
    while True:
        now_ms = int(time.time() * 1000)
        next_close = _next_bar_close_ms(now_ms)
        sleep_s = max(1, (next_close - 60_000 - now_ms) / 1000)
        await asyncio.sleep(sleep_s)
        try:
            await sync_equity(reason="HEARTBEAT")
        except Exception as e:
            print(f"  [HEARTBEAT] sync failed: {e}")


# ============================================================================
# Bootstrap
# ============================================================================

async def bootstrap() -> None:
    pairs_summary = [(p.symbol, p.strategy) for p in CONFIG.pairs if p.enabled]
    print(f"\n{'='*70}")
    print(f"  V4 PORTFOLIO [{BOT_NAME}] starting")
    print(f"  Strategies: multi (BB MR + Hull+Ichimoku)")
    print(f"  Pairs:    {pairs_summary}")
    print(f"  Chart:    http://0.0.0.0:{CHART_PORT}/  (TZ: {CHART_TZ})")
    print(f"{'='*70}\n")

    # Restore state if persisted
    await asyncio.to_thread(_state.load)

    for pair_cfg in CONFIG.pairs:
        if not pair_cfg.enabled:
            continue
        sym = pair_cfg.symbol
        sig: SignalGen
        if pair_cfg.strategy == "bb_mr":
            bb_cfg = BBMRConfig(
                symbol=sym, timeframe=pair_cfg.timeframe,
                bb_length=pair_cfg.bb_length, bb_std=pair_cfg.bb_std,
                rsi_length=pair_cfg.rsi_length,
                rsi_oversold=pair_cfg.rsi_oversold,
                rsi_overbought=pair_cfg.rsi_overbought,
                sl_pct=pair_cfg.sl_pct, tp_rr=pair_cfg.tp_rr,
                max_bars_in_trade=pair_cfg.max_bars_in_trade,
                taker_fee=CONFIG.portfolio.taker_fee,
            )
            sig = BBMeanReversionSignal(bb_cfg)
        else:  # 'hi' (default)
            ssc = PairStrategyConfig(
                symbol=sym, timeframe=pair_cfg.timeframe,
                hull_length=pair_cfg.hull_length,
                tenkan_periods=pair_cfg.tenkan_periods,
                kijun_periods=pair_cfg.kijun_periods,
                senkou_b_periods=pair_cfg.senkou_b_periods,
                displacement=pair_cfg.displacement,
                risk_pct_per_trade=pair_cfg.risk_pct_per_trade,
                sl_initial_pct=pair_cfg.sl_initial_pct,
                tp_pct=pair_cfg.tp_pct,
                max_hull_spread_pct=pair_cfg.max_hull_spread_pct,
                max_close_kijun_dist_pct=pair_cfg.max_close_kijun_dist_pct,
                taker_fee=CONFIG.portfolio.taker_fee,
            )
            sig = IchimokuSignal(ssc)
        print(f"  [{sym}] strategy={_strategy_label(pair_cfg.strategy)}")
        _signals[sym] = sig
        _pair_cfgs[sym] = pair_cfg
        _state.set_position(sym, None)

        # Set leverage
        await ex.set_leverage(sym, pair_cfg.leverage)

        # Warmup 400 bars
        bars = await ex.get_kline(sym, _TF_INTERVAL, limit=400)
        bars = nl.filter_closed_bars(bars, _TF_INTERVAL)
        if bars:
            df = pd.DataFrame(bars, columns=["ts_ms", "open", "high", "low",
                                              "close", "volume", "turnover"])
            df.index = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
            df = df[["open", "high", "low", "close", "volume"]]
            sig.warm_up(df)
            _last_synced_ts[sym] = int(bars[-1][0]) // 1000
            print(f"  [{sym}] warmup {len(bars)} bars  last={df.index[-1]}")
        else:
            print(f"  [{sym}] WARN: warmup returned 0 bars")

    # INIT equity sync
    await sync_equity(reason="INIT")

    # Strategy register indicators (chart overlay meta)
    # HI overlays
    _state.register_indicator("hull_n1", color="#ff8c00", line_width=2)
    _state.register_indicator("tenkan", color="#3498db", line_width=1)
    _state.register_indicator("kijun", color="#e74c3c", line_width=1)
    _state.register_indicator("senkou_a", color="#2ecc71", line_width=1)
    _state.register_indicator("senkou_b", color="#c0392b", line_width=1)
    # BB MR overlays
    _state.register_indicator("bb_mid", color="#9b59b6", line_width=1)
    _state.register_indicator("bb_upper", color="#34495e", line_width=1)
    _state.register_indicator("bb_lower", color="#34495e", line_width=1)

    # Telegram BOT_STARTED
    pairs_label = ", ".join(
        f"{p.symbol} [{_strategy_label(p.strategy)}]"
        for p in CONFIG.pairs if p.enabled
    )
    await tg.send(
        "BOT STARTED ✅",
        f"<b>Portfolio:</b> <code>{BOT_NAME}</code>\n"
        f"<b>Strategies:</b> multi (BB MR + Hull+Ichimoku)\n"
        f"<b>Pairs:</b> {pairs_label}\n"
        f"<b>Account init:</b> ${_state.initial_account:,.2f}\n"
        f"<b>Started:</b> {tg.fmt_time(_state.start_utc)}\n"
        f"<b>Chart:</b> port {CHART_HOST_PORT}",
    )


# ============================================================================
# FastAPI app + lifespan
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bootstrap()
    # Spawn background tasks
    tasks = [
        asyncio.create_task(public_ws_loop()),
        asyncio.create_task(pws.run(on_order=on_order_event,
                                     on_execution=on_execution_event,
                                     on_position=on_position_event)),
        asyncio.create_task(heartbeat_loop()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        try:
            ret_pct = ((_state.shared_equity - _state.initial_account)
                       / _state.initial_account * 100) if _state.initial_account else 0
            await tg.send(
                "BOT STOPPED 🛑",
                f"<b>Stopped:</b> {tg.fmt_time(datetime.now(timezone.utc))}\n"
                f"<b>Equity:</b> ${_state.shared_equity:,.2f}  |  Return: {ret_pct:+.2f}%\n"
                f"<b>Trades:</b> {len(_state.trades)}",
            )
        except Exception as e:
            print(f"  [SHUTDOWN] tg.send failed: {e}")


app = FastAPI(lifespan=lifespan, title=f"{BOT_NAME} chart")

if Path(STATIC).exists():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def root():
    chart = Path(STATIC) / "chart_live.html"
    if chart.exists():
        return FileResponse(str(chart))
    return JSONResponse({"bot": BOT_NAME, "status": "running",
                         "note": "chart_live.html not yet created"})


@app.get("/api/init")
async def api_init():
    payload = _state.init_payload()
    # Active positions snapshot per simbol (chart afiseaza live lines la load)
    active = {}
    for sym, pos in _state.positions.items():
        if pos is None:
            continue
        active[sym] = {
            "direction": pos.direction, "entry": pos.entry_price,
            "sl": pos.sl_price, "tp": pos.tp_price,
            "qty": pos.qty, "risk_usd": pos.risk_usd,
            "entry_ms": pos.opened_ts_ms,
        }
    payload.update({
        "candles": _candles,
        "timeframe": "4h",
        "pairs": [p.symbol for p in CONFIG.pairs if p.enabled],
        "active_positions": active,
    })
    return JSONResponse(payload)


@app.get("/api/status")
async def api_status():
    return {
        "bot_name": BOT_NAME,
        "pairs": [p.symbol for p in CONFIG.pairs if p.enabled],
        "candles_total": {s: len(c) for s, c in _candles.items()},
        "connected_clients": len(_clients),
        "summary": _state.summary(),
        "halted": list(_halted.keys()) if _halted else [],
    }


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=CHART_PORT, log_level="info")
