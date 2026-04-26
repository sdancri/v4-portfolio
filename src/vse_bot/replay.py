"""Replay engine — backtest pe parquet pentru validare match cu strategy.md.

Semantica execuției (fidel ``backtest_lab/engine/executor.py``):

  1. Signal evaluat pe bara închisă ``t-1``. Entry se face la ``open[t]``.
  2. Pe bara ``t``, în timp ce ești în poziție:
     - check intra-bar pe ``high[t]`` / ``low[t]`` vs ``sl`` (NaN pe TP — VSE e
       trailing-only). Dacă SL hit → exit la sl_price (reason ``"sl"``).
     - apoi update trailing pe close: ``sl = max(sl, long_stop[t])`` pe long,
       ``sl = min(sl, short_stop[t])`` pe short. (NaN-safe.)
  3. Signal opus pe pair când ești în poziție pe pair → close la open al barei
     următoare (reason ``"signal_reverse"``), apoi (re-)entry la același open.
  4. Pe ultima bară a istoricului poziția deschisă închide la close (reason
     ``"end_of_data"``).

Diferențele față de executor.py din lab:
  - Sizing folosește equity CURENT (compounding), conform spec-ului VSE Nou1.
  - Cycle logic (SUCCESS / RESET) integrat după fiecare close.
  - Pool comun multi-pair: balance & equity sunt shared între pairs din subaccount.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from vse_bot.config import (
    AppConfig,
    IndicatorConfig,
    PairConfig,
    StrategyConfig,
    SubaccountConfig,
)
from vse_bot.cycle_manager import (
    SubaccountState,
    check_cycle_success_at_entry,
    on_trade_closed,
    restart_cycle_after_success,
)
from vse_bot.indicator import VSEConfig, build_signals, compute_indicators
from vse_bot.sizing import compute_position_size


# ── Helpers ──────────────────────────────────────────────────────────────
def _slice_history(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    if df.index.tz is None:
        df = df.tz_localize("UTC")
    return df.loc[(df.index >= start_ts) & (df.index <= end_ts)]


def _build_vse_config(
    strat: StrategyConfig, ind: IndicatorConfig
) -> VSEConfig:
    return VSEConfig(
        style=strat.style,
        mcginley_length=ind.mcginley_length,
        whiteline_length=ind.whiteline_length,
        ttms_length=ind.ttms_length,
        ttms_bb_mult=ind.ttms_bb_mult,
        ttms_kc_mult_widest=ind.ttms_kc_mult_widest,
        tether_fast=ind.tether_fast,
        tether_slow=ind.tether_slow,
        vortex_length=ind.vortex_length,
        vortex_threshold=ind.vortex_threshold,
        st_atr_length=ind.st_atr_length,
        st_atr_mult=ind.st_atr_mult,
        entry_filter_bars=strat.cooldown_bars,
    )


# ── Per-pair pre-compute ──────────────────────────────────────────────────
@dataclass
class PairData:
    """Tot ce avem nevoie pentru un pair pe replay (numpy arrays pentru viteză).

    Două seturi de signals separate:
      - ``raw_long`` / ``raw_short``: semnalul BRUT din build_signals (NU filtrat).
        Folosit pentru OPP exit (Opposite Signal Exit) — 21% din wealth, conform
        STRATEGY_LOGIC.md v2.0 sec 6.
      - ``signal_dir`` / ``sl_at_signal``: filtrate prin SL bounds + warmup.
        Folosit pentru entry. Cooldown post-EXIT se aplică în ``replay_subaccount``.
    """
    symbol: str
    timeframe: str
    times: pd.DatetimeIndex
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    long_stop: np.ndarray
    short_stop: np.ndarray
    raw_long: np.ndarray
    raw_short: np.ndarray
    signal_dir: np.ndarray
    sl_at_signal: np.ndarray


def prepare_pair(
    df: pd.DataFrame,
    pair: PairConfig,
    cfg_strategy: StrategyConfig,
    cfg_indicator: IndicatorConfig,
) -> PairData:
    """Calculează indicatorii + emite TOATE candidatele de entry valide.

    LIVE-CORECT (NU replică bug-urile engine-ului `build_entry_list_enhanced`):
      - Filtrează doar pe SL bounds + warmup (NaN check).
      - Cooldown post-EXIT se aplică LA RUNTIME în ``replay_subaccount``
        (anchor pe REAL exit — TS sau OPP, primul declanșat).

    Spec source: STRATEGY_LOGIC.md v2.0 sec 0:
      "Bot trebuie să: (3) Cooldown bazat pe REAL exit (TS sau OPP)".
    """
    vse_cfg = _build_vse_config(cfg_strategy, cfg_indicator)
    ind = compute_indicators(df, vse_cfg)
    sig = build_signals(ind, vse_cfg)

    n = len(sig)
    raw_long = sig["raw_long"].to_numpy(dtype=bool)
    raw_short = sig["raw_short"].to_numpy(dtype=bool)
    closes = sig["close"].to_numpy(dtype=np.float64)
    long_stop = sig["long_stop"].to_numpy(dtype=np.float64)
    short_stop = sig["short_stop"].to_numpy(dtype=np.float64)
    atr = sig["atr_st"].to_numpy(dtype=np.float64)

    sl_min = cfg_strategy.sl_min_pct
    sl_max = cfg_strategy.sl_max_pct

    signal_dir = np.zeros(n, dtype=np.int64)
    sl_at_signal = np.full(n, np.nan, dtype=np.float64)

    for t in range(n):
        if np.isnan(atr[t]) or np.isnan(long_stop[t]) or np.isnan(short_stop[t]):
            continue
        c = closes[t]
        if c <= 0:
            continue
        if raw_long[t]:
            sl = long_stop[t]
            if sl >= c:
                continue
            sl_pct = (c - sl) / c
            if sl_pct < sl_min or sl_pct > sl_max:
                continue
            signal_dir[t] = 1
            sl_at_signal[t] = sl
        elif raw_short[t]:
            sl = short_stop[t]
            if sl <= c:
                continue
            sl_pct = (sl - c) / c
            if sl_pct < sl_min or sl_pct > sl_max:
                continue
            signal_dir[t] = -1
            sl_at_signal[t] = sl

    return PairData(
        symbol=pair.symbol,
        timeframe=pair.timeframe,
        times=sig.index,
        opens=sig["open"].to_numpy(dtype=np.float64),
        highs=sig["high"].to_numpy(dtype=np.float64),
        lows=sig["low"].to_numpy(dtype=np.float64),
        closes=closes,
        long_stop=long_stop,
        short_stop=short_stop,
        raw_long=raw_long,
        raw_short=raw_short,
        signal_dir=signal_dir,
        sl_at_signal=sl_at_signal,
    )


# ── Trade tracking ────────────────────────────────────────────────────────
@dataclass
class _OpenPos:
    symbol: str
    direction: int          # +1 long, -1 short
    entry_idx: int
    entry_time: pd.Timestamp
    entry_price: float
    sl_price: float         # current trailing stop
    sl_initial: float
    size: float             # base units
    notional: float
    equity_at_entry: float


@dataclass
class TradeRecord:
    symbol: str
    direction: int
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    sl_initial: float
    size: float
    notional: float
    pnl_gross: float
    fees: float
    pnl_net: float
    pnl_pct: float
    pnl_R: float
    exit_reason: str
    cycle_num_at_entry: int
    equity_at_entry: float


@dataclass
class CycleEvent:
    ts: pd.Timestamp
    kind: str               # SUCCESS | RESET | POOL_LOW
    cycle_num: int
    balance_after: float
    equity_after: float
    pool_used_after: float
    withdraw_amount: float = 0.0


@dataclass
class ReplayResult:
    subacc_name: str
    total_pnl: float
    wealth: float           # = withdraw-uri SUCCESS + (final balance − pool_total)
    n_trades: int
    n_cycles_success: int
    n_resets: int
    n_pool_low: int
    peak_balance: float
    total_withdrawn: float
    final_state: SubaccountState
    trades: list[TradeRecord] = field(default_factory=list)
    cycle_events: list[CycleEvent] = field(default_factory=list)


# ── Per-subaccount replay ─────────────────────────────────────────────────
def _fees_for_trade(
    size: float, entry_price: float, exit_price: float, taker_fee: float
) -> float:
    return size * (entry_price + exit_price) * taker_fee


def _close_pos_to_record(
    pos: _OpenPos,
    *,
    exit_time: pd.Timestamp,
    exit_price: float,
    reason: str,
    cfg: StrategyConfig,
    cycle_num: int,
) -> TradeRecord:
    per_unit = (exit_price - pos.entry_price) * pos.direction
    pnl_gross = per_unit * pos.size
    fees = _fees_for_trade(pos.size, pos.entry_price, exit_price, cfg.taker_fee)
    pnl_net = pnl_gross - fees
    pnl_pct = pnl_net / pos.notional if pos.notional > 0 else 0.0
    risk = pos.size * abs(pos.entry_price - pos.sl_initial)
    pnl_R = pnl_net / risk if risk > 0 else 0.0
    return TradeRecord(
        symbol=pos.symbol,
        direction=pos.direction,
        entry_time=pos.entry_time,
        entry_price=pos.entry_price,
        exit_time=exit_time,
        exit_price=exit_price,
        sl_initial=pos.sl_initial,
        size=pos.size,
        notional=pos.notional,
        pnl_gross=pnl_gross,
        fees=fees,
        pnl_net=pnl_net,
        pnl_pct=pnl_pct,
        pnl_R=pnl_R,
        exit_reason=reason,
        cycle_num_at_entry=cycle_num,
        equity_at_entry=pos.equity_at_entry,
    )


def replay_subaccount(
    pair_data: list[PairData],
    sub_cfg: SubaccountConfig,
    strat_cfg: StrategyConfig,
) -> ReplayResult:
    """Master timeline replay. Cycle state e shared între perechi.

    Algorithm:
      events = sorted union of (ts, pair_idx, local_t)
      pentru fiecare event:
        Phase A (open of bar):
          A1. signal_reverse close pe pereche dacă pos open și prev_dir == -dir
          A2. entry pe pereche dacă nu pos open și prev_dir != 0 (margin OK)
        Phase B (intra-bar):
          B1. SL hit? exit la sl_price.
        Phase C (close of bar):
          C1. trailing update: sl = max(sl, long_stop[t]) pe long, mirror pe short.
        După orice close: cycle event handler.
    """
    events: list[tuple[pd.Timestamp, int, int]] = []
    for pi, pdata in enumerate(pair_data):
        for ti, ts in enumerate(pdata.times):
            events.append((ts, pi, ti))
    events.sort(key=lambda e: (e[0], e[1]))

    state = SubaccountState.fresh(strat_cfg)
    open_pos: dict[int, _OpenPos] = {}
    last_exit_idx: dict[int, int] = {}
    # Track motivul ultimului exit per pair: "opp" sau "sl" (sau lipsește).
    # Folosit cu mode ``opp_exit_mode = with_reverse`` — pe OPP exit, NU
    # aplicăm cooldown pentru entry pe direcția opusă (close + reopen).
    last_exit_reason: dict[int, str] = {}
    trades: list[TradeRecord] = []
    cycle_events: list[CycleEvent] = []
    counters = {"SUCCESS": 0, "RESET": 0, "POOL_LOW": 0}
    peak_balance = state.balance_broker
    total_withdrawn = 0.0
    cooldown_bars = strat_cfg.cooldown_bars
    on_entry_mode = strat_cfg.withdraw_check_mode == "on_entry"
    check_success_on_close = not on_entry_mode
    # opp_exit_mode: "pure" (default) sau "with_reverse"
    #   - pure: după OPP exit, cooldown 3 bars înainte de orice entry.
    #   - with_reverse: după OPP exit, entry pe direcția opusă POATE deschide
    #     IMEDIAT pe aceeași bară (close + reopen). Match cu varianta empirică
    #     ce a generat target-ul $13,847.
    opp_exit_mode = getattr(strat_cfg, "opp_exit_mode", "pure")

    def _force_close_all(at_ts: pd.Timestamp, reason: str) -> None:
        """Închide toate pozițiile la close-ul ultimei bare ≤ at_ts pe pair."""
        for pi in list(open_pos.keys()):
            pos = open_pos.pop(pi)
            pdata = pair_data[pi]
            idx = pdata.times.searchsorted(at_ts, side="right") - 1
            if idx < 0:
                idx = 0
            tr = _close_pos_to_record(
                pos,
                exit_time=pdata.times[idx],
                exit_price=float(pdata.closes[idx]),
                reason=reason,
                cfg=strat_cfg,
                cycle_num=state.cycle_num,
            )
            trades.append(tr)
            # NU re-aplica cycle logic aici (suntem deja în SUCCESS handler)
            state.balance_broker += tr.pnl_net
            state.equity += tr.pnl_net

    def _trigger_success(ts: pd.Timestamp) -> None:
        """SUCCESS: force-close toate pozițiile + restart cycle. Folosit în
        ambele moduri (on_close direct, on_entry la entry-time check).
        """
        nonlocal total_withdrawn
        _force_close_all(ts, "cycle_success")
        withdraw = restart_cycle_after_success(state, strat_cfg)
        total_withdrawn += withdraw
        counters["SUCCESS"] += 1
        cycle_events.append(CycleEvent(
            ts=ts, kind="SUCCESS",
            cycle_num=state.cycle_num - 1,
            balance_after=state.balance_broker,
            equity_after=state.equity,
            pool_used_after=state.pool_used,
            withdraw_amount=withdraw,
        ))

    def _handle_close(tr: TradeRecord, ts: pd.Timestamp) -> None:
        nonlocal peak_balance
        ev = on_trade_closed(
            state, tr.pnl_net, strat_cfg, check_success=check_success_on_close
        )
        if state.balance_broker > peak_balance:
            peak_balance = state.balance_broker

        if ev == "SUCCESS":
            _trigger_success(ts)
        elif ev == "RESET":
            counters["RESET"] += 1
            cycle_events.append(CycleEvent(
                ts=ts, kind="RESET",
                cycle_num=state.cycle_num,
                balance_after=state.balance_broker,
                equity_after=state.equity,
                pool_used_after=state.pool_used,
            ))
        elif ev == "POOL_LOW":
            counters["POOL_LOW"] += 1
            cycle_events.append(CycleEvent(
                ts=ts, kind="POOL_LOW",
                cycle_num=state.cycle_num,
                balance_after=state.balance_broker,
                equity_after=state.equity,
                pool_used_after=state.pool_used,
            ))

    for ts, pi, ti in events:
        pdata = pair_data[pi]

        # Phase A1: OPP exit (Opposite Signal Exit) — close ONLY at next bar open.
        # Folosește RAW signal (NU filtered prin SL bounds), ca în engine-ul lab.
        # Spec source: STRATEGY_LOGIC.md v2.0 sec 6 — 21% din wealth provine din OPP.
        if ti >= 1 and pi in open_pos:
            pos = open_pos[pi]
            opp = (
                (pos.direction == 1 and bool(pdata.raw_short[ti - 1]))
                or (pos.direction == -1 and bool(pdata.raw_long[ti - 1]))
            )
            if opp:
                tr = _close_pos_to_record(
                    pos,
                    exit_time=pdata.times[ti],
                    exit_price=float(pdata.opens[ti]),
                    reason="opp",
                    cfg=strat_cfg,
                    cycle_num=state.cycle_num,
                )
                trades.append(tr)
                del open_pos[pi]
                last_exit_idx[pi] = ti
                last_exit_reason[pi] = "opp"
                _handle_close(tr, ts)

        # Phase A2: entry — cooldown post-EXIT pe REAL exit (TS sau OPP).
        prev_signal = int(pdata.signal_dir[ti - 1]) if ti >= 1 else 0
        # Mode "with_reverse": dacă ultimul exit a fost OPP pe ACEEAȘI bară
        # ŞI semnalul nou e pe direcția opusă față de poziția închisă, sărim
        # cooldown-ul (close + reopen pe aceeași bară).
        bypass_cooldown = (
            opp_exit_mode == "with_reverse"
            and last_exit_reason.get(pi) == "opp"
            and last_exit_idx.get(pi) == ti
        )
        cooldown_ok = bypass_cooldown or (
            pi not in last_exit_idx
            or (ti - last_exit_idx[pi]) >= cooldown_bars
        )
        # Mode on_entry: check SUCCESS DOAR aici (înainte de entry nou).
        if (
            on_entry_mode
            and ti >= 1
            and prev_signal != 0
            and cooldown_ok
            and check_cycle_success_at_entry(state, strat_cfg)
        ):
            _trigger_success(ts)
        if (
            ti >= 1
            and pi not in open_pos
            and prev_signal != 0
            and cooldown_ok
        ):
            prev_dir = prev_signal
            sl_signal = float(pdata.sl_at_signal[ti - 1])
            entry_price = float(pdata.opens[ti])
            if entry_price > 0 and not np.isnan(sl_signal) and sl_signal > 0:
                sl_dist = abs(entry_price - sl_signal)
                if sl_dist > 0:
                    sl_pct_at_open = sl_dist / entry_price
                    # Sizing intern: pos = (risk × 100) / SL%
                    risk_usd = strat_cfg.risk_pct_equity * state.equity
                    pos_internal = risk_usd / sl_pct_at_open
                    # Cap dacă pos > balance_real × leverage (Bybit ar refuza).
                    # În replay folosim balance_broker calc local ca proxy pt
                    # Bybit balance real (live va folosi fetch_balance_usdt).
                    # Margin folosit deja pe alte pozitii reduce balance disponibil.
                    used_margin = sum(
                        p.notional / strat_cfg.leverage for p in open_pos.values()
                    )
                    avail_balance = state.balance_broker - used_margin
                    if avail_balance > 0:
                        max_bybit = avail_balance * strat_cfg.leverage
                        if pos_internal > max_bybit:
                            pos_final = strat_cfg.cap_pct_of_max * max_bybit
                        else:
                            pos_final = pos_internal
                        if pos_final > 0:
                            size_base = pos_final / entry_price
                            open_pos[pi] = _OpenPos(
                                symbol=pdata.symbol,
                                direction=prev_dir,
                                entry_idx=ti,
                                entry_time=pdata.times[ti],
                                entry_price=entry_price,
                                sl_price=sl_signal,
                                sl_initial=sl_signal,
                                size=size_base,
                                notional=pos_final,
                                equity_at_entry=state.equity,
                            )

        # Phase B: intra-bar SL hit
        if pi in open_pos:
            pos = open_pos[pi]
            sl_hit = False
            if pos.direction == 1 and pdata.lows[ti] <= pos.sl_price:
                sl_hit = True
            elif pos.direction == -1 and pdata.highs[ti] >= pos.sl_price:
                sl_hit = True

            if sl_hit:
                tr = _close_pos_to_record(
                    pos,
                    exit_time=pdata.times[ti],
                    exit_price=float(pos.sl_price),
                    reason="sl",
                    cfg=strat_cfg,
                    cycle_num=state.cycle_num,
                )
                trades.append(tr)
                del open_pos[pi]
                last_exit_idx[pi] = ti
                last_exit_reason[pi] = "sl"
                _handle_close(tr, ts)
            else:
                # Phase C: trailing update pe close
                if pos.direction == 1:
                    ls = pdata.long_stop[ti]
                    if not np.isnan(ls) and ls > pos.sl_price:
                        pos.sl_price = float(ls)
                else:
                    ss = pdata.short_stop[ti]
                    if not np.isnan(ss) and ss < pos.sl_price:
                        pos.sl_price = float(ss)

    # End of data: force-close eventualele poziții deschise
    if open_pos:
        last_ts = events[-1][0]
        for pi in list(open_pos.keys()):
            pos = open_pos.pop(pi)
            pdata = pair_data[pi]
            idx = len(pdata.times) - 1
            tr = _close_pos_to_record(
                pos,
                exit_time=pdata.times[idx],
                exit_price=float(pdata.closes[idx]),
                reason="end_of_data",
                cfg=strat_cfg,
                cycle_num=state.cycle_num,
            )
            trades.append(tr)
            _handle_close(tr, last_ts)

    final_extra = state.balance_broker - strat_cfg.pool_total
    wealth = total_withdrawn + final_extra

    return ReplayResult(
        subacc_name=sub_cfg.name,
        total_pnl=sum(t.pnl_net for t in trades),
        wealth=wealth,
        n_trades=len(trades),
        n_cycles_success=counters["SUCCESS"],
        n_resets=counters["RESET"],
        n_pool_low=counters["POOL_LOW"],
        peak_balance=peak_balance,
        total_withdrawn=total_withdrawn,
        final_state=state,
        trades=trades,
        cycle_events=cycle_events,
    )


# ── Top-level driver ──────────────────────────────────────────────────────
def load_pair_history(
    data_dir: Path,
    pair: PairConfig,
    start: str,
    end: str,
) -> pd.DataFrame:
    fname = f"{pair.symbol}_{pair.timeframe}.parquet"
    path = data_dir / fname
    if not path.exists():
        raise FileNotFoundError(f"Parquet missing: {path}")
    df = pd.read_parquet(path)
    return _slice_history(df, start, end)


def run_replay(cfg: AppConfig) -> dict[str, ReplayResult]:
    results: dict[str, ReplayResult] = {}
    for sub in cfg.subaccounts:
        if not sub.enabled:
            continue
        pair_data: list[PairData] = []
        for pair in sub.pairs:
            df = load_pair_history(
                cfg.replay.data_dir, pair, cfg.replay.start, cfg.replay.end
            )
            pdata = prepare_pair(df, pair, cfg.strategy, cfg.indicator)
            pair_data.append(pdata)
        results[sub.name] = replay_subaccount(pair_data, sub, cfg.strategy)
    return results
