"""ICHIMOKU bot — orchestrator portfolio (Hull+Ichimoku, MNT+DOT 4h, shared equity).

Simplificari fata de VSE:
  - NO cycle_manager (no withdraw_target, reset_target, max_resets)
  - NO opp_exit planning, NO SuperTrend trailing
  - Per-pair: tp_pct, sizing_pct, hull/kijun/snkb (din config.yaml)
  - Shared equity compound: equity = pool_total + cumulative real PnL Bybit

Per Setari BOT regula 2: restart = istoric de la 0. NO state persistence.
"""

from __future__ import annotations

import asyncio
import io as _io
import os
import sys as _sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

# Line-buffering stdout/stderr pentru Docker logs live.
_sys.stdout = _io.TextIOWrapper(
    _sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
)
_sys.stderr = _io.TextIOWrapper(
    _sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
)

from ichimoku_bot import telegram_bot as tg
from ichimoku_bot.bot_state import BotState, TradeRecord
from ichimoku_bot.config import AppConfig, PairConfig, load_config
from ichimoku_bot.event_log import log_event
from ichimoku_bot.exchange.bybit_client import BybitClient, MarketInfo
from ichimoku_bot.exchange.bybit_private_ws import BybitPrivateWS
from ichimoku_bot.exchange.bybit_ws import BybitKlineWS
from ichimoku_bot.ichimoku_signal import IchimokuSignal, PairStrategyConfig, SignalDecision
from ichimoku_bot.no_lookahead import filter_closed_bars
from ichimoku_bot.sizing import compute_position_size, compute_qty


# ============================================================================
# LIVE POSITION TRACKER (in-memory, simple)
# ============================================================================

from dataclasses import dataclass


@dataclass
class LivePosition:
    symbol: str
    side: str                    # "long" | "short"
    qty: float
    entry_price: float
    sl_price: float
    pos_usd: float
    risk_usd: float
    opened_ts: datetime
    order_entry_id: str = ""


# ============================================================================
# RUNNER — orchestrator unic per portfolio
# ============================================================================

class IchimokuRunner:
    """Un singur portfolio (subaccount) cu N perechi (default MNT + DOT)."""

    def __init__(self, cfg: AppConfig, client: BybitClient) -> None:
        self.cfg = cfg
        self.client = client

        # Real account state (account += real PnL Bybit dupa fiecare close)
        self.bot: BotState = BotState(
            initial_account=cfg.portfolio.pool_total,
            account=cfg.portfolio.pool_total,
        )

        # Per-pair signal generators
        self.signals: dict[str, IchimokuSignal] = {}
        self.positions: dict[str, LivePosition | None] = {}
        self.market_info: dict[str, MarketInfo] = {}
        self.paused_symbols: set[str] = set()

        # Dedup ultimul ts confirmed per pereche — protectie vs WS
        # retransmit (reconnect) si REST sync overlap. Bare confirmed cu
        # ts <= ultimul procesat sunt sarite. Per-pair pentru ca fiecare
        # simbol are propriul stream cu ritm independent.
        self._last_confirmed_ts: dict[str, int] = {}

        # Chart UI (primary pair pentru chart broadcast)
        self.clients: set = set()
        self.candles_live: list[list] = []

    @property
    def paused(self) -> bool:
        return bool(self.paused_symbols)

    def primary_pair_key(self) -> tuple[str, str] | None:
        if not self.cfg.pairs:
            return None
        p = self.cfg.pairs[0]
        return (p.symbol, p.timeframe)

    def active_position_payload(self) -> dict | None:
        primary = self.primary_pair_key()
        if not primary:
            return None
        pos = self.positions.get(primary[0])
        if pos is None:
            return None
        # TP target — pe primary pair (daca tp_pct setat)
        primary_cfg = self.cfg.pairs[0]
        tp = 0
        if primary_cfg.tp_pct is not None:
            if pos.side == "long":
                tp = pos.entry_price * (1 + primary_cfg.tp_pct)
            else:
                tp = pos.entry_price * (1 - primary_cfg.tp_pct)
        return {
            "symbol": pos.symbol,
            "direction": "LONG" if pos.side == "long" else "SHORT",
            "entry": pos.entry_price,
            "sl": pos.sl_price,
            "tp": tp,
            "qty": pos.qty,
            "risk_usd": pos.risk_usd,
            "entry_ms": int(pos.opened_ts.timestamp() * 1000),
        }

    @property
    def shared_equity(self) -> float:
        return self.bot.account

    # ── Equity sync cu Bybit (truth source) ──────────────────────────────
    async def _sync_equity(self, source: str) -> None:
        """Sync ``bot.account`` cu balance-ul real Bybit.

        Bybit este TRUTH SOURCE; local e cache. Always override.
        Telegram alert doar la drift > 1% din current equity (ignora fees mici).

        Args:
            source: "INIT" | "CLOSE" | "HEARTBEAT" — pentru log.
        """
        try:
            real = await self.client.fetch_balance_usdt()
        except Exception as e:
            print(f"  [SYNC {source}] fetch_balance failed: {e}")
            return
        if real <= 0:
            print(f"  [SYNC {source}] invalid balance ({real}), skip")
            return
        drift = real - self.bot.account
        pct = abs(drift) / max(self.bot.account, 1.0) * 100
        print(f"  [SYNC {source}] local=${self.bot.account:.2f} bybit=${real:.2f} "
              f"drift=${drift:+.2f} ({pct:.2f}%)")
        if abs(drift) > self.bot.account * 0.01:  # >1% drift = real anomalie
            await tg.send(
                f"⚠️ EQUITY DRIFT — {source}",
                f"Local: <code>${self.bot.account:.2f}</code>\n"
                f"Bybit: <code>${real:.2f}</code>\n"
                f"Diff:  <code>${drift:+.2f}</code> ({pct:.2f}%)\n"
                f"Local override → Bybit (truth source)",
            )
        self.bot.account = real

    # ── Heartbeat task — periodic equity sync (60s default) ─────────────
    async def heartbeat_loop(self) -> None:
        interval = self.cfg.operational.heartbeat_interval_seconds
        while True:
            try:
                await asyncio.sleep(interval)
                await self._sync_equity("HEARTBEAT")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"  [HEARTBEAT] error: {e}")

    # ── Setup ────────────────────────────────────────────────────────────
    async def setup(self) -> None:
        log_event(
            self.cfg.operational.log_dir, self.cfg.portfolio.name, "BOOT",
            equity=self.shared_equity, pool=self.cfg.portfolio.pool_total,
            note="FRESH state (no persistence per regula 2)",
        )

        for pair_cfg in self.cfg.pairs:
            if not pair_cfg.enabled:
                continue
            mi = await self.client.fetch_market_info(pair_cfg.symbol)
            self.market_info[pair_cfg.symbol] = mi
            pair_leverage = self.cfg.leverage_for(pair_cfg)
            await self.client.set_isolated_margin(pair_cfg.symbol, pair_leverage)
            await self.client.set_leverage(pair_cfg.symbol, pair_leverage)
            print(f"  [SETUP] {pair_cfg.symbol}: leverage={pair_leverage}x")

            ssc = PairStrategyConfig(
                symbol=pair_cfg.symbol, timeframe=pair_cfg.timeframe,
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
                taker_fee=self.cfg.portfolio.taker_fee,
            )
            sig = IchimokuSignal(ssc)

            # Warmup cu 400 bare istorice (suficient pt min_history=53 + buffer).
            ohlcv = await self.client.fetch_ohlcv(pair_cfg.symbol, pair_cfg.timeframe, limit=400)
            # Anti-lookahead: drop bara curenta in curs (ultimul row poate fi
            # un candle inca in formare). Indicatorii trebuie calculati doar
            # pe close-uri finale.
            ohlcv = filter_closed_bars(ohlcv, pair_cfg.timeframe)
            df = _ohlcv_list_to_df(ohlcv)
            if not df.empty:
                sig.warm_up(df)
            self.signals[pair_cfg.symbol] = sig
            self.positions[pair_cfg.symbol] = None

        # STARTUP equity sync — Bybit balance e truth source.
        # NU porni cu account = pool_total hardcoded; ia balance-ul real.
        await self._sync_equity("INIT")

        # Telegram READY notice
        pairs_str = ", ".join(
            f"{p.symbol} {p.timeframe} ({self.cfg.leverage_for(p)}x)"
            for p in self.cfg.pairs if p.enabled
        )
        await tg.send(
            "STRATEGY READY ✅",
            f"<b>Strategy:</b> <code>Hull+Ichimoku</code>\n"
            f"Portfolio: <code>{self.cfg.portfolio.name}</code>\n"
            f"Pairs:     <code>{pairs_str}</code>\n"
            f"Pool init: ${self.cfg.portfolio.pool_total:,.2f}"
        )

        # Reconcile cu Bybit (orphan positions detection)
        await self._reconcile_with_bybit()

    async def _reconcile_with_bybit(self) -> None:
        """Detecteaza pozitii reziduale pe Bybit la pornire."""
        residue: list[str] = []
        for pair in self.cfg.pairs:
            if not pair.enabled:
                continue
            sym = pair.symbol
            try:
                pos = await self.client.fetch_position(sym)
            except Exception as e:
                residue.append(f"{sym}: fetch_position failed ({e!r})")
                continue
            has_position = pos is not None and float(pos.get("contracts") or 0) > 0
            if not has_position:
                continue
            self.paused_symbols.add(sym)
            info = pos.get("info") or {}
            sl_set = float(info.get("stopLoss") or 0)
            warning = "FĂRĂ SL" if sl_set <= 0 else f"SL={sl_set}"
            residue.append(
                f"{sym}: position={pos.get('contracts')} @ {pos.get('entryPrice')} "
                f"({warning}) → {sym} PAUSED"
            )

        if residue:
            details = "\n".join(f"  - {r}" for r in residue)
            print(f"  [RECONCILE] ⚠️  REZIDUE Bybit detectat\n{details}")
            log_event(
                self.cfg.operational.log_dir, self.cfg.portfolio.name,
                "RECONCILE_PAUSED", residue=residue,
            )
            await tg.send_critical(
                "RECONCILE PAUSED",
                f"Portfolio: <code>{self.cfg.portfolio.name}</code>\n"
                f"Reziduri Bybit detectate la pornire:\n<pre>{details}</pre>\n"
                f"Bot PAUZAT pe simbolurile afectate."
            )
        else:
            log_event(
                self.cfg.operational.log_dir, self.cfg.portfolio.name,
                "RECONCILE_OK", note="No positions on Bybit",
            )

    # ── Bar handling ─────────────────────────────────────────────────────
    async def on_bar(self, bar: dict[str, Any]) -> None:
        sym = bar["symbol"]
        if sym not in self.signals:
            return
        ts_s = int(bar["ts_ms"]) // 1000
        confirmed = bool(bar.get("confirmed"))

        # Dedup defensive DOAR pentru confirmed: WS poate livra bare confirmed
        # duplicate (retransmit pe reconnect, sync REST overlap). Fiecare bara
        # confirmed are ts unic per pereche, deci `<=` filtreaza corect.
        # Pentru unconfirmed NU dedup-uim: toate tick-urile intra-bar din Bybit
        # impart acelasi ts_s (open al barei); dedup pe ts_s ar bloca toate
        # update-urile in afara primului — chart-ul ar vedea bara doar la
        # open si la close.
        if confirmed:
            if ts_s <= self._last_confirmed_ts.get(sym, 0):
                return
            self._last_confirmed_ts[sym] = ts_s

        # Chart broadcast — DOAR pe primary pair
        if (sym, bar["timeframe"]) == self.primary_pair_key():
            from ichimoku_bot.chart_server import broadcast as _bc
            import math as _math
            ref = bar["close"] or 0
            if ref > 0:
                mag = _math.floor(_math.log10(ref))
                _prec = max(2, min(8, 4 - mag))
            else:
                _prec = 6
            candle_arr = [
                ts_s, round(bar["open"], _prec), round(bar["high"], _prec),
                round(bar["low"], _prec), round(bar["close"], _prec),
            ]
            if confirmed:
                # Defense-in-depth peste _last_confirmed_ts dedup: garantam ca
                # candles_live ramane STRICT MONOTON crescator pe ts. Lightweight
                # Charts crasha SILENT pe duplicate / out-of-order -> chart blank
                # greu de debugged. Cazul comun (append la coada) ramane O(1).
                if not self.candles_live or self.candles_live[-1][0] < ts_s:
                    self.candles_live.append(candle_arr)
                elif self.candles_live[-1][0] == ts_s:
                    self.candles_live[-1] = candle_arr   # duplicate la coada — replace
                else:
                    # Out-of-order (rar): scan invers pana gasim pozitia corecta.
                    idx = len(self.candles_live) - 1
                    while idx >= 0 and self.candles_live[idx][0] > ts_s:
                        idx -= 1
                    if idx >= 0 and self.candles_live[idx][0] == ts_s:
                        self.candles_live[idx] = candle_arr
                    else:
                        self.candles_live.insert(idx + 1, candle_arr)
            else:
                # Unconfirmed: ramane comportamentul anterior — replace-in-place
                # daca acelasi ts (tick intra-bar), append daca nou.
                if self.candles_live and self.candles_live[-1][0] == ts_s:
                    self.candles_live[-1] = candle_arr
                else:
                    self.candles_live.append(candle_arr)
            if confirmed:
                self.bot.mark_first_candle(ts_s)
            if len(self.candles_live) > 20000:
                self.candles_live.pop(0)
            await _bc(self, {
                "type": "candle", "confirmed": confirmed,
                "data": {"time": ts_s, "open": bar["open"], "high": bar["high"],
                         "low": bar["low"], "close": bar["close"]},
            })

        # Strategy processing DOAR pe bare confirmed
        if not confirmed:
            return
        if sym in self.paused_symbols:
            return

        sig = self.signals[sym]
        sig.update_buffer(bar)
        sig.recompute_indicators()

        # Defense-in-depth: detect state desync
        pos = self.positions.get(sym)
        if pos is not None:
            try:
                bybit_pos = await self.client.fetch_position(sym)
                bybit_qty = float(bybit_pos.get("contracts") or 0) if bybit_pos else 0
            except Exception as e:
                bybit_qty = -1.0
                print(f"  [SYNC_CHECK] {sym} fetch_position failed: {e}")
            if bybit_qty == 0:
                print(f"  [SYNC_CHECK] {sym}: state desync — finalize close EXTERNAL")
                log_event(self.cfg.operational.log_dir, self.cfg.portfolio.name,
                          "STATE_DESYNC_CLOSE", symbol=sym)
                entry_ts_ms = int(pos.opened_ts.timestamp() * 1000)
                exit_ts_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
                pnl_data = await self.client.fetch_pnl_for_trade(sym, entry_ts_ms, exit_ts_ms)
                await self._on_trade_closed(
                    sym, pos, pnl_data["avg_exit"] or pos.sl_price,
                    pnl_data["pnl"], pnl_data["fees"], "EXTERNAL",
                )
                pos = None

        # Evaluate decision (signal-based + SL/TP intra-bar)
        has_pos = pos.side if pos else None
        entry = pos.entry_price if pos else 0.0
        decision = sig.evaluate(has_pos, entry)
        await self._dispatch_decision(sym, decision, bar)

    async def _dispatch_decision(self, sym: str, dec: SignalDecision, bar: dict) -> None:
        """Execute decisia engine-ului prin Bybit calls."""
        if dec.action == "HOLD":
            return

        pair_cfg = next(p for p in self.cfg.pairs if p.symbol == sym)
        pos = self.positions.get(sym)

        # ENTRY
        if dec.action in ("OPEN_LONG", "OPEN_SHORT") and pos is None:
            await self._open_trade(sym, pair_cfg, dec, bar)
            return

        # EXITS
        if pos is None:
            return  # nothing to close
        if dec.action in ("SL_LONG", "SL_SHORT"):
            await self._close_trade(sym, pos, dec.price, "SL", bar)
        elif dec.action in ("TP_LONG", "TP_SHORT"):
            await self._close_trade(sym, pos, dec.price, "TP", bar)
        elif dec.action in ("CLOSE_LONG", "CLOSE_SHORT"):
            await self._close_trade(sym, pos, dec.price, "SIGNAL", bar)

    async def _open_trade(self, sym: str, pair_cfg: PairConfig,
                          dec: SignalDecision, bar: dict) -> None:
        """Deschide pozitie: market order + set position SL pe Bybit."""
        side = "long" if dec.action == "OPEN_LONG" else "short"
        sizing = compute_position_size(
            shared_equity=self.shared_equity, pair_cfg=pair_cfg,
            portfolio_cfg=self.cfg.portfolio, balance_broker=self.shared_equity,
            leverage=self.cfg.leverage_for(pair_cfg),
        )
        if sizing is None:
            print(f"  [SIZING] {sym} {side} skipped — pos_usd > Bybit cap")
            return

        mi = self.market_info[sym]
        qty = compute_qty(sizing.pos_usd, dec.price, mi.qty_step)
        if qty <= 0:
            print(f"  [SIZING] {sym} qty=0 (price too high vs sizing)")
            return

        sl_price = (dec.price * (1 - pair_cfg.sl_initial_pct)
                    if side == "long"
                    else dec.price * (1 + pair_cfg.sl_initial_pct))

        # ENTRY ca MAKER cu fallback Market (pattern 80/20):
        # plaseaza Limit PostOnly la best bid/ask, asteapta `timeout_sec`,
        # daca nu s-a umplut -> cancel + Market doar pe REMAINDER. Captureaza
        # ~80-90% din economia de fee fata de un chase complet. Pentru EXIT
        # (SL/TP/SIGNAL) NU folosim acest pattern — siguranta executiei conteaza
        # mai mult decat economia de fee.
        ccxt_side = "buy" if side == "long" else "sell"
        try:
            result = await self.client.maker_entry_or_market(
                sym, ccxt_side, qty,
                timeout_sec=int(os.getenv("MAKER_ENTRY_TIMEOUT_SEC", "5")),
                fallback="market",
            )
            if result["filled_qty"] <= 0:
                print(f"  [OPEN_TRADE FAILED] {sym} {side}: maker_entry result={result['result']}")
                log_event(self.cfg.operational.log_dir, self.cfg.portfolio.name,
                          "OPEN_FAILED", symbol=sym, side=side,
                          error=f"maker_entry result={result['result']}")
                return
            entry_filled = float(result["avg_price"] or dec.price)
            order_id = ""  # maker_entry_or_market doesn't return single order_id
            print(f"  [OPEN] {sym} {side} entry={result['result']} "
                  f"filled={result['filled_qty']} avg={entry_filled}")
            await self.client.set_position_sl(sym, sl_price)
        except Exception as e:
            print(f"  [OPEN_TRADE FAILED] {sym} {side}: {e!r}")
            log_event(self.cfg.operational.log_dir, self.cfg.portfolio.name,
                      "OPEN_FAILED", symbol=sym, side=side, error=str(e))
            await tg.send_critical(
                f"OPEN FAILED — {sym} {side.upper()}",
                f"<code>{e!r}</code>"
            )
            return

        pos = LivePosition(
            symbol=sym, side=side, qty=qty, entry_price=entry_filled,
            sl_price=sl_price, pos_usd=sizing.pos_usd, risk_usd=sizing.risk_usd,
            opened_ts=datetime.now(tz=timezone.utc),
            order_entry_id=order_id,
        )
        self.positions[sym] = pos

        # Broadcast position_open la chart — DOAR pe primary pair (chart-ul
        # afiseaza o singura pereche; payload-ul deja exista pe /api/init pt
        # persistenta la refresh).
        primary = self.primary_pair_key()
        if primary and sym == primary[0]:
            tp_price = 0.0
            if pair_cfg.tp_pct is not None:
                tp_price = (
                    entry_filled * (1 + pair_cfg.tp_pct) if side == "long"
                    else entry_filled * (1 - pair_cfg.tp_pct)
                )
            from ichimoku_bot.chart_server import broadcast as _bc
            await _bc(self, {
                "type":      "position_open",
                "direction": "LONG" if side == "long" else "SHORT",
                "entry":     entry_filled,
                "sl":        sl_price,
                "tp":        tp_price,
                "qty":       qty,
                "risk_usd":  sizing.risk_usd,
                "entry_ms":  int(pos.opened_ts.timestamp() * 1000),
            })

        log_event(self.cfg.operational.log_dir, self.cfg.portfolio.name,
                  "OPEN", symbol=sym, side=side, qty=qty,
                  entry=entry_filled, sl=sl_price, risk=sizing.risk_usd,
                  fill_type=result["result"])
        tp_line = (
            f"\nTP:       {pair_cfg.tp_pct*100:.0f}% target"
            if pair_cfg.tp_pct else "\nTP:       signal exit only"
        )
        dir_icon = "🚀" if side == "long" else "📉"
        # Fill icon: maker (saved fee) / mixed (partial maker) / taker (full market)
        fill_icon = {"maker": "🟢", "mixed": "🟡", "taker": "🔴"}.get(result["result"], "⚪")
        await tg.send(
            f"{dir_icon} ENTRY {side.upper()} — {sym}",
            f"<b>Strategy:</b> <code>Hull+Ichimoku</code>\n"
            f"Time:     {tg.fmt_time(int(pos.opened_ts.timestamp()))}\n"
            f"Entry:    {entry_filled}  {fill_icon} {result['result']}\n"
            f"SL:       {sl_price}  ({pair_cfg.sl_initial_pct*100:.1f}%)"
            f"{tp_line}\n"
            f"Qty:      {qty}\n"
            f"Notional: ${sizing.pos_usd:.2f}\n"
            f"Risk:     ${sizing.risk_usd:.2f}  ({pair_cfg.risk_pct_per_trade*100:.0f}% × ${self.shared_equity:.2f})"
        )

    async def _close_trade(self, sym: str, pos: LivePosition,
                            exit_price: float, reason: str, bar: dict) -> None:
        """Inchide pozitie pe Bybit (market order opus). Reason: SL|TP|SIGNAL|EXTERNAL.

        IMPORTANT: ccxt cere side="buy"/"sell", NU "long"/"short". Pentru
        a inchide LONG (pos.side="long") trimitem SELL; pentru SHORT trimitem BUY.
        """
        try:
            ccxt_close_side = "sell" if pos.side == "long" else "buy"
            order = await self.client.create_market_order(sym, ccxt_close_side, pos.qty, reduce_only=True)
            avg_exit = float(order.get("price") or exit_price)
            # Trage PnL real de pe Bybit
            entry_ts_ms = int(pos.opened_ts.timestamp() * 1000)
            exit_ts_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
            pnl_data = await self.client.fetch_pnl_for_trade(sym, entry_ts_ms, exit_ts_ms)
            await self._on_trade_closed(sym, pos, pnl_data.get("avg_exit") or avg_exit,
                                        pnl_data.get("pnl", 0.0), pnl_data.get("fees", 0.0), reason)
        except Exception as e:
            print(f"  [CLOSE FAILED] {sym}: {e!r}")
            log_event(self.cfg.operational.log_dir, self.cfg.portfolio.name,
                      "CLOSE_FAILED", symbol=sym, error=str(e))
            await tg.send_critical(
                f"CLOSE FAILED — {sym}",
                f"<code>{e!r}</code>"
            )

    async def _on_trade_closed(self, sym: str, pos: LivePosition, exit_price: float,
                                pnl: float, fees: float, reason: str) -> None:
        """Finalize trade close: actualizez account + trade record + telegram."""
        trade = TradeRecord(
            id=0,  # auto-set in BotState.add_closed_trade
            date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            direction="LONG" if pos.side == "long" else "SHORT",
            symbol=sym,
            entry_ts_ms=int(pos.opened_ts.timestamp() * 1000),
            entry_price=pos.entry_price, sl_price=pos.sl_price, tp_price=None,
            qty=pos.qty,
            exit_ts_ms=int(pd.Timestamp.utcnow().timestamp() * 1000),
            exit_price=exit_price, exit_reason=reason,
            pnl=pnl, fees=fees,
        )
        self.bot.add_closed_trade(trade)
        self.positions[sym] = None

        # Equity sync POST-CLOSE — Bybit e truth source.
        # 1.5s delay: Bybit are lag intre fill execution si balance settlement.
        # Fara delay, uneori prinzi balance-ul dinainte de creditarea PnL-ului.
        await asyncio.sleep(1.5)
        await self._sync_equity("CLOSE")

        # Broadcast position_close + trade_closed la chart — DOAR pe primary
        # pair (chart-ul afiseaza o singura pereche). trade_closed actualizeaza
        # panel-ul de trade-uri + equity curve in real-time.
        primary = self.primary_pair_key()
        if primary and sym == primary[0]:
            from ichimoku_bot.chart_server import broadcast as _bc
            await _bc(self, {"type": "position_close"})
            equity_pt = self.bot.equity_curve[-1] if self.bot.equity_curve else None
            await _bc(self, {
                "type":    "trade_closed",
                "trade":   trade.to_dict(),
                "equity":  {"time": equity_pt[0], "value": round(equity_pt[1], 4)}
                            if equity_pt else None,
                "summary": self.bot.summary(),
            })

        log_event(
            self.cfg.operational.log_dir, self.cfg.portfolio.name, "CLOSE",
            symbol=sym, side=pos.side, reason=reason,
            entry=pos.entry_price, exit=exit_price, pnl=pnl, fees=fees,
            account=self.bot.account,
        )

        # Icon: ⚠️ pe EXTERNAL/BYBIT_SL (defense-in-depth a sintetizat close-ul);
        # 📈 win / 📉 loss pe close clean.
        if reason in ("EXTERNAL", "BYBIT_SL"):
            sign_icon = "⚠️"
        else:
            sign_icon = "📈" if pnl >= 0 else "📉"
        ret_pct = (self.bot.account / self.bot.initial_account - 1) * 100
        await tg.send(
            f"{sign_icon} TRADE INCHIS — {pos.side.upper()} {sym}",
            f"<b>Strategy:</b> <code>Hull+Ichimoku</code>\n"
            f"Entry: {pos.entry_price}  ({tg.fmt_time(int(pos.opened_ts.timestamp()))})\n"
            f"Exit:  {exit_price}  ({reason})  ({tg.fmt_time(trade.exit_ts_ms)})\n"
            f"PnL: <b>${pnl:+,.2f}</b>  (Bybit real, fees ${fees:.2f} incluse)\n"
            f"Account: ${self.bot.account:,.2f}  |  Return: {ret_pct:+.2f}%"
        )

    # ── Bybit private WS event ───────────────────────────────────────────
    async def on_bybit_position_event(self, event: dict) -> None:
        """Position update event de pe private WS (e.g. SL hit by Bybit)."""
        sym = event.get("symbol", "")
        if sym not in self.positions:
            return
        pos = self.positions.get(sym)
        if pos is None:
            return
        size = float(event.get("size") or 0)
        if size == 0:
            print(f"  [WS_POS_EVENT] {sym}: size=0 — Bybit a inchis pozitia")
            entry_ts_ms = int(pos.opened_ts.timestamp() * 1000)
            exit_ts_ms = int(pd.Timestamp.utcnow().timestamp() * 1000)
            pnl_data = await self.client.fetch_pnl_for_trade(sym, entry_ts_ms, exit_ts_ms)
            await self._on_trade_closed(
                sym, pos, pnl_data.get("avg_exit") or pos.sl_price,
                pnl_data.get("pnl", 0.0), pnl_data.get("fees", 0.0), "BYBIT_SL"
            )


# ── Helpers ──────────────────────────────────────────────────────────────
def _ohlcv_list_to_df(ohlcv: list[list[float]]) -> pd.DataFrame:
    if not ohlcv:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(ohlcv, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("ts").drop(columns=["ts_ms"])
    return df


# ── Top-level ────────────────────────────────────────────────────────────
async def run_live(config_path: str = "config/config.yaml") -> None:
    load_dotenv()
    cfg = load_config(config_path)
    cfg.operational.log_dir.mkdir(parents=True, exist_ok=True)

    testnet = os.getenv("TRADING_MODE", "testnet").lower() != "live"

    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    if not api_key or not api_secret:
        raise SystemExit(
            "Lipsesc BYBIT_API_KEY / BYBIT_API_SECRET. Verifica env vars din container."
        )

    enabled_pairs = [p for p in cfg.pairs if p.enabled]
    if not enabled_pairs:
        raise SystemExit("Niciun pereche enabled in config.yaml")

    print(f"\n{'=' * 70}")
    print(f"  ICHIMOKU BOT — {cfg.portfolio.name}")
    print(f"  mode: {'testnet (paper)' if testnet else '🔴 MAINNET — REAL MONEY'}")
    pairs_repr = [(p.symbol, p.timeframe, f"{cfg.leverage_for(p)}x") for p in enabled_pairs]
    print(f"  pairs: {pairs_repr}")
    print(f"  pool_total: ${cfg.portfolio.pool_total:,.2f}  (default leverage {cfg.portfolio.leverage}x)")
    print(f"{'=' * 70}\n")

    client = await BybitClient.create(api_key, api_secret, testnet=testnet)
    try:
        runner = IchimokuRunner(cfg=cfg, client=client)
        await runner.setup()
    except Exception as e:
        print(f"  [SETUP FAILED] {e!r} — closing client and exit")
        await client.close()
        raise

    subscriptions = [(p.symbol, p.timeframe) for p in enabled_pairs]

    async def on_bar(bar: dict) -> None:
        await runner.on_bar(bar)

    async def on_stale(symbol: str, tf: str, age_sec: float) -> None:
        log_event(cfg.operational.log_dir, cfg.portfolio.name, "WS_STALE",
                  symbol=symbol, tf=tf, age_sec=round(age_sec, 0))
        await tg.send(
            "⚠️ WS STALE — auto-reconnect",
            f"Portfolio: <code>{cfg.portfolio.name}</code>\n"
            f"Symbol: <code>{symbol} {tf}</code>\n"
            f"Age: {age_sec:.0f}s fara bara confirmed."
        )

    public_ws = BybitKlineWS(subscriptions, on_bar, testnet=testnet, on_stale=on_stale)

    private_ws = BybitPrivateWS(
        api_key=api_key, api_secret=api_secret, testnet=testnet,
        on_position=runner.on_bybit_position_event,
        log_prefix=f"ws-priv {cfg.portfolio.name}",
    )

    from ichimoku_bot.chart_server import create_app, serve_chart
    chart_port = int(os.getenv("CHART_PORT", "8101"))
    app = create_app(runner)
    print(f"  [chart] http://0.0.0.0:{chart_port}/  (TZ: Europe/Bucharest)")

    mode_label = "testnet (paper)" if testnet else "🔴 <b>MAINNET — REAL MONEY</b>"
    pairs_str = ", ".join(f"{p.symbol} ({cfg.leverage_for(p)}x)" for p in enabled_pairs)
    await tg.send(
        "BOT STARTED ✅",
        f"<b>Strategy:</b> <code>Hull+Ichimoku 4h</code>\n"
        f"Portfolio: <code>{cfg.portfolio.name}</code>\n"
        f"Mode:      {mode_label}\n"
        f"Pool init: ${cfg.portfolio.pool_total:,.2f}\n"
        f"Pairs:     {pairs_str}\n"
        f"Chart:     port {chart_port}"
    )

    try:
        await asyncio.gather(
            public_ws.run(),
            private_ws.run(),
            serve_chart(app, chart_port),
            runner.heartbeat_loop(),       # B — periodic equity sync (60s)
        )
    finally:
        try:
            n_trades = len(runner.bot.trades)
            ret_pct = (runner.bot.account / runner.bot.initial_account - 1) * 100
            await tg.send(
                "🛑 BOT STOPPED",
                f"<b>Strategy:</b> <code>Hull+Ichimoku</code>\n"
                f"Portfolio: <code>{cfg.portfolio.name}</code>\n"
                f"Account:   ${runner.bot.account:,.2f}  |  Return: {ret_pct:+.2f}%\n"
                f"Trades:    {n_trades}"
            )
        except Exception as e:
            print(f"  [SHUTDOWN] tg.send failed: {e}")
        await client.close()


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    args = ap.parse_args()
    asyncio.run(run_live(args.config))


if __name__ == "__main__":
    main()
