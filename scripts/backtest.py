"""Backtest Hull+Ichimoku 4h pe MNT + DOT (shared equity, compound).

Foloseste exact logica strategiei live (precompute_indicators + _long/_short
entry + _close_long/_close_short + filters). Sizing din ``sizing.compute_position_size``
cu leverage per-pair.

Date: parquet OHLCV in /home/dan/Python/Test_Python/data/ohlcv/{SYMBOL}_{TF}.parquet

Uzitare:
    python scripts/backtest.py
    python scripts/backtest.py --start 2022-01-01 --end 2026-05-06
    python scripts/backtest.py --data-dir /alt/path
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ichimoku_bot.config import AppConfig, PairConfig, load_config
from ichimoku_bot.ichimoku_signal import (
    PairStrategyConfig,
    _close_long,
    _close_short,
    _long_entry,
    _short_entry,
    passes_filters,
    precompute_indicators,
)
from ichimoku_bot.sizing import compute_position_size, compute_qty


@dataclass
class BTTrade:
    pair: str
    direction: str  # "LONG" | "SHORT"
    entry_ts: pd.Timestamp
    entry_price: float
    qty: float
    pos_usd: float
    risk_usd: float
    sl_price: float
    tp_price: float | None
    leverage: int
    exit_ts: pd.Timestamp | None = None
    exit_price: float = 0.0
    exit_reason: str = ""           # SL | TP | SIGNAL
    pnl_gross: float = 0.0
    fees: float = 0.0
    pnl_net: float = 0.0


@dataclass
class PairState:
    cfg: PairConfig
    ssc: PairStrategyConfig
    df: pd.DataFrame
    cache: Any                       # IndicatorCache
    qty_step: float = 0.0
    open_trade: BTTrade | None = None


def load_pair_data(symbol: str, timeframe: str, data_dir: Path,
                   start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    fpath = data_dir / f"{symbol}_{timeframe}.parquet"
    if not fpath.exists():
        raise FileNotFoundError(f"Data missing: {fpath}")
    df = pd.read_parquet(fpath)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df.loc[(df.index >= start) & (df.index <= end)].copy()
    df = df.sort_index()
    return df


def setup_pair(pair_cfg: PairConfig, app_cfg: AppConfig, data_dir: Path,
               start: pd.Timestamp, end: pd.Timestamp,
               qty_step: float) -> PairState:
    df = load_pair_data(pair_cfg.symbol, pair_cfg.timeframe, data_dir, start, end)
    if df.empty:
        raise ValueError(f"{pair_cfg.symbol}: no data in [{start}, {end}]")

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
        taker_fee=app_cfg.portfolio.taker_fee,
    )
    cache = precompute_indicators(df, ssc)
    return PairState(cfg=pair_cfg, ssc=ssc, df=df, cache=cache, qty_step=qty_step)


def fmt_pct(x: float) -> str:
    return f"{x:+.2f}%"


def run_backtest(cfg: AppConfig, data_dir: Path, start: pd.Timestamp,
                 end: pd.Timestamp, qty_steps: dict[str, float],
                 entry_fee: float | None = None,
                 exit_fee: float | None = None) -> dict:
    enabled = [p for p in cfg.pairs if p.enabled]
    states = {p.symbol: setup_pair(p, cfg, data_dir, start, end, qty_steps.get(p.symbol, 0.0))
              for p in enabled}

    # Build union of timestamps across all pairs (sorted, unique)
    all_ts = sorted(set().union(*(s.df.index for s in states.values())))

    equity = cfg.portfolio.pool_total
    initial_equity = equity
    # Per-side fees (default both = portfolio.taker_fee for backward compat).
    fee_entry = entry_fee if entry_fee is not None else cfg.portfolio.taker_fee
    fee_exit = exit_fee if exit_fee is not None else cfg.portfolio.taker_fee
    trades: list[BTTrade] = []
    equity_curve: list[tuple[pd.Timestamp, float]] = [(all_ts[0], equity)]

    for ts in all_ts:
        # Phase 1: per pair, check exits + entries based on this bar's data
        for sym, st in states.items():
            if ts not in st.df.index:
                continue
            i = st.df.index.get_loc(ts)
            if i < st.ssc.min_history_bars:
                continue
            c = st.cache
            # Skip bars where indicators are NaN (warmup window)
            if any(np.isnan(x) for x in [c.n1[i], c.n2[i], c.tenkan[i], c.kijun[i],
                                          c.senkou_h[i], c.senkou_l[i], c.chikou[i]]):
                continue

            cur = st.df.iloc[i]
            close = float(cur["close"])
            high = float(cur["high"])
            low = float(cur["low"])
            n1 = c.n1[i]; n2 = c.n2[i]; tk = c.tenkan[i]; kj = c.kijun[i]
            sh = c.senkou_h[i]; sl_ = c.senkou_l[i]; ch = c.chikou[i]

            # ── EXIT: in trade ────────────────────────────────────────────
            t = st.open_trade
            if t is not None:
                exit_price = None
                reason = ""
                if t.direction == "LONG":
                    if low <= t.sl_price:
                        exit_price = t.sl_price; reason = "SL"
                    elif t.tp_price is not None and high >= t.tp_price:
                        exit_price = t.tp_price; reason = "TP"
                    elif _close_long(close, n1, n2, tk, kj, sh, ch):
                        exit_price = close; reason = "SIGNAL"
                else:  # SHORT
                    if high >= t.sl_price:
                        exit_price = t.sl_price; reason = "SL"
                    elif t.tp_price is not None and low <= t.tp_price:
                        exit_price = t.tp_price; reason = "TP"
                    elif _close_short(close, n1, n2, tk, kj, sl_, ch):
                        exit_price = close; reason = "SIGNAL"

                if exit_price is not None:
                    if t.direction == "LONG":
                        gross = (exit_price - t.entry_price) * t.qty
                    else:
                        gross = (t.entry_price - exit_price) * t.qty
                    exit_fee_amt = exit_price * t.qty * fee_exit
                    t.exit_ts = ts
                    t.exit_price = exit_price
                    t.exit_reason = reason
                    t.pnl_gross = gross
                    t.fees += exit_fee_amt
                    t.pnl_net = gross - t.fees
                    equity += t.pnl_net
                    trades.append(t)
                    st.open_trade = None
                    equity_curve.append((ts, equity))
                    if equity <= 0:
                        # Total wipeout — stop
                        return {
                            "trades": trades, "equity_curve": equity_curve,
                            "final_equity": equity, "initial_equity": initial_equity,
                            "wiped": True,
                        }

            # ── ENTRY: no trade ──────────────────────────────────────────
            if st.open_trade is None:
                ls = _long_entry(close, n1, n2, tk, kj, sh, ch)
                ss = _short_entry(close, n1, n2, tk, kj, sl_, ch)
                if not (ls or ss):
                    continue
                ok, _why = passes_filters(close, n1, n2, kj, st.ssc)
                if not ok:
                    continue
                # Enforce max_concurrent_positions across all pairs
                open_count = sum(1 for s2 in states.values() if s2.open_trade is not None)
                if open_count >= cfg.operational.max_concurrent_positions:
                    continue

                direction = "LONG" if ls else "SHORT"
                eff_lev = cfg.leverage_for(st.cfg)
                sizing = compute_position_size(
                    shared_equity=equity, pair_cfg=st.cfg,
                    portfolio_cfg=cfg.portfolio, balance_broker=equity,
                    leverage=eff_lev,
                )
                if sizing is None:
                    continue
                qty = compute_qty(sizing.pos_usd, close, st.qty_step)
                if qty <= 0:
                    continue

                if direction == "LONG":
                    sl_price = close * (1 - st.cfg.sl_initial_pct)
                    tp_price = close * (1 + st.cfg.tp_pct) if st.cfg.tp_pct else None
                else:
                    sl_price = close * (1 + st.cfg.sl_initial_pct)
                    tp_price = close * (1 - st.cfg.tp_pct) if st.cfg.tp_pct else None

                entry_fee_amt = close * qty * fee_entry
                t = BTTrade(
                    pair=sym, direction=direction,
                    entry_ts=ts, entry_price=close,
                    qty=qty, pos_usd=close * qty, risk_usd=sizing.risk_usd,
                    sl_price=sl_price, tp_price=tp_price,
                    leverage=eff_lev, fees=entry_fee_amt,
                )
                st.open_trade = t

    return {
        "trades": trades, "equity_curve": equity_curve,
        "final_equity": equity, "initial_equity": initial_equity,
        "wiped": False,
    }


def report(result: dict, cfg: AppConfig, start: pd.Timestamp, end: pd.Timestamp) -> None:
    trades: list[BTTrade] = result["trades"]
    eq_curve = result["equity_curve"]
    final = result["final_equity"]
    init = result["initial_equity"]

    # Per-pair breakdown
    per_pair: dict[str, dict] = {}
    for p in cfg.pairs:
        if not p.enabled:
            continue
        ts = [t for t in trades if t.pair == p.symbol]
        wins = [t for t in ts if t.pnl_net > 0]
        losses = [t for t in ts if t.pnl_net <= 0]
        gross_win = sum(t.pnl_net for t in wins)
        gross_loss = abs(sum(t.pnl_net for t in losses)) or 1e-9
        per_pair[p.symbol] = {
            "n": len(ts),
            "wins": len(wins),
            "wr": len(wins) / len(ts) * 100 if ts else 0.0,
            "pf": gross_win / gross_loss if losses else float("inf"),
            "pnl": sum(t.pnl_net for t in ts),
            "fees": sum(t.fees for t in ts),
            "sl_hits": sum(1 for t in ts if t.exit_reason == "SL"),
            "tp_hits": sum(1 for t in ts if t.exit_reason == "TP"),
            "sig_exits": sum(1 for t in ts if t.exit_reason == "SIGNAL"),
        }

    # Aggregate
    n = len(trades)
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    wr = len(wins) / n * 100 if n else 0.0
    gross_win = sum(t.pnl_net for t in wins)
    gross_loss = abs(sum(t.pnl_net for t in losses)) or 1e-9
    pf = gross_win / gross_loss if losses else float("inf")
    fees_total = sum(t.fees for t in trades)
    ret_pct = (final / init - 1) * 100

    # Max drawdown on equity curve
    eq_vals = [v for _, v in eq_curve]
    peaks = np.maximum.accumulate(eq_vals)
    dd_pct = ((np.array(eq_vals) - peaks) / peaks * 100).min() if eq_vals else 0.0

    print("=" * 78)
    print(f"BACKTEST RESULTS  |  {start.date()} → {end.date()}  |  Hull+Ichimoku 4h")
    print("=" * 78)
    print(f"\nPortfolio: {cfg.portfolio.name}")
    print(f"  pool_total:    ${init:,.2f}")
    print(f"  final equity:  ${final:,.2f}")
    print(f"  return:        {fmt_pct(ret_pct)}")
    print(f"  max drawdown:  {dd_pct:.2f}%")
    print(f"  n_trades:      {n}")
    print(f"  win rate:      {wr:.1f}%")
    print(f"  profit factor: {pf:.2f}")
    print(f"  total fees:    ${fees_total:,.2f}")
    if result["wiped"]:
        print("\n  ⚠️  EQUITY WIPED OUT — backtest stopped early")

    print("\nPer-pair breakdown:")
    for sym, st in per_pair.items():
        pair_cfg = next(p for p in cfg.pairs if p.symbol == sym)
        eff_lev = cfg.leverage_for(pair_cfg)
        tp_disp = (f"{pair_cfg.tp_pct*100:.0f}%"
                   if pair_cfg.tp_pct is not None else "none")
        print(f"\n  {sym} ({pair_cfg.timeframe}, {eff_lev}× leverage, "
              f"risk {pair_cfg.risk_pct_per_trade*100:.0f}%, SL {pair_cfg.sl_initial_pct*100:.1f}%, "
              f"TP {tp_disp}):")
        print(f"    n={st['n']}  WR={st['wr']:.1f}%  PF={st['pf']:.2f}  "
              f"PnL=${st['pnl']:+,.2f}  fees=${st['fees']:,.2f}")
        print(f"    exits: SL={st['sl_hits']}  TP={st['tp_hits']}  SIGNAL={st['sig_exits']}")

    # Compare to STRATEGY_SETTINGS expectations (full-period backtest)
    print("\n" + "─" * 78)
    print("Reference (STRATEGY_SETTINGS.md, full period 2022-2026, AGGRESSIVE):")
    print(f"  expected: $26,997 final  | +26,897%  | DD -50%  | PF 1.33  | WR 39.8%  | n=1012")
    print(f"  current:  ${final:,.2f}  | {fmt_pct(ret_pct)}  | DD {dd_pct:.1f}%  | "
          f"PF {pf:.2f}  | WR {wr:.1f}%  | n={n}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--data-dir", default="/home/dan/Python/Test_Python/data/ohlcv")
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-05-06")
    ap.add_argument("--entry-fee", type=float, default=None,
                    help="Override entry fee (default: taker 0.055%%). "
                         "Maker = 0.0002, mixed 70/30 = 0.000305")
    ap.add_argument("--exit-fee", type=float, default=None,
                    help="Override exit fee (default: taker 0.055%%)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    data_dir = Path(args.data_dir)
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")

    # Default qty steps (placeholder; for backtest fee accuracy these don't
    # matter much. Use conservative MNT 0.1, DOT 0.01).
    qty_steps = {"MNTUSDT": 0.1, "DOTUSDT": 0.01}

    print(f"\nLoading config: {args.config}")
    print(f"Data dir: {data_dir}")
    print(f"Period:   {start.date()} → {end.date()}")
    fe = args.entry_fee if args.entry_fee is not None else cfg.portfolio.taker_fee
    fx = args.exit_fee if args.exit_fee is not None else cfg.portfolio.taker_fee
    print(f"Fees:     entry={fe*100:.4f}%   exit={fx*100:.4f}%")
    for p in cfg.pairs:
        if p.enabled:
            print(f"  - {p.symbol} {p.timeframe}  lev={cfg.leverage_for(p)}x  "
                  f"risk={p.risk_pct_per_trade*100:.0f}%  SL={p.sl_initial_pct*100:.1f}%")

    result = run_backtest(cfg, data_dir, start, end, qty_steps,
                          entry_fee=args.entry_fee, exit_fee=args.exit_fee)
    report(result, cfg, start, end)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
