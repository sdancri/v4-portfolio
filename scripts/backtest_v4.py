"""V4 portfolio backtest — 2022+ on local 4h parquets.

Reuseaza EXACT logica din strategies/ichimoku_signal.py si bb_mr_signal.py
(precompute_indicators + functiile _long_entry / _close_long / passes_filters
pentru HI; bb cross-back + RSI pt BB MR). Shared equity (compound) intre
toate perechile, sizing identic cu live (core/position_sizing.py).

Run:
    python scripts/backtest_v4.py
    python scripts/backtest_v4.py --start 2022-01-01 --end 2026-05-01
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import AppConfig, PairConfig, load_config  # noqa: E402
from strategies.bb_mr_signal import (  # noqa: E402
    BBMRConfig, precompute_indicators as bb_precompute,
)
from strategies.ichimoku_signal import (  # noqa: E402
    PairStrategyConfig, _close_long, _close_short, _long_entry, _short_entry,
    passes_filters, precompute_indicators as hi_precompute,
)

DATA_DIR = Path("/home/dan/Python/Test_Python/data/ohlcv")


# ============================================================================
# Trade record
# ============================================================================

@dataclass
class Trade:
    symbol: str
    strategy: str
    direction: str  # LONG / SHORT
    entry_ts: pd.Timestamp
    entry_price: float
    exit_ts: pd.Timestamp
    exit_price: float
    qty: float
    pos_usd: float
    pnl: float       # net (after fees)
    fees: float
    reason: str
    bars_held: int
    equity_after: float


# ============================================================================
# Per-pair iterators (yield decisions per bar)
# ============================================================================

def _make_hi_cfg(pc: PairConfig) -> PairStrategyConfig:
    return PairStrategyConfig(
        symbol=pc.symbol, timeframe=pc.timeframe,
        hull_length=pc.hull_length,
        tenkan_periods=pc.tenkan_periods,
        kijun_periods=pc.kijun_periods,
        senkou_b_periods=pc.senkou_b_periods,
        displacement=pc.displacement,
        risk_pct_per_trade=pc.risk_pct_per_trade,
        sl_initial_pct=pc.sl_initial_pct,
        tp_pct=pc.tp_pct,
        max_hull_spread_pct=pc.max_hull_spread_pct,
        max_close_kijun_dist_pct=pc.max_close_kijun_dist_pct,
    )


def _make_bb_cfg(pc: PairConfig, taker_fee: float) -> BBMRConfig:
    return BBMRConfig(
        symbol=pc.symbol, timeframe=pc.timeframe,
        bb_length=pc.bb_length, bb_std=pc.bb_std,
        rsi_length=pc.rsi_length,
        rsi_oversold=pc.rsi_oversold,
        rsi_overbought=pc.rsi_overbought,
        sl_pct=pc.sl_pct, tp_rr=pc.tp_rr,
        max_bars_in_trade=pc.max_bars_in_trade,
        taker_fee=taker_fee,
    )


# ============================================================================
# Sizing (mirror core/position_sizing.compute_position_size)
# ============================================================================

def size_position(pc: PairConfig, equity: float, cap_lev: int,
                   cap_pct: float) -> tuple[float, float, bool]:
    """Returns (pos_usd, risk_usd, skip)."""
    sl = pc.effective_sl_pct
    risk_usd = pc.risk_pct_per_trade * equity
    pos_usd = risk_usd / sl
    cap_usd = cap_pct * equity * cap_lev
    if pos_usd > cap_usd:
        return pos_usd, risk_usd, True
    return pos_usd, risk_usd, False


# ============================================================================
# Per-bar decision (returns one of: None, OPEN_LONG, OPEN_SHORT, exit reason)
# ============================================================================

def _hi_decision(i: int, df: pd.DataFrame, cache, cfg: PairStrategyConfig,
                 has_pos: Optional[str], entry_price: float) -> tuple[str, float, str]:
    """Returns (action, exec_price, reason). action ∈ {HOLD, OPEN_LONG, OPEN_SHORT,
    SL_LONG, SL_SHORT, CLOSE_LONG, CLOSE_SHORT}."""
    bar = df.iloc[i]
    close = float(bar["close"]); high = float(bar["high"]); low = float(bar["low"])
    n1, n2 = cache.n1[i], cache.n2[i]
    tk, kj = cache.tenkan[i], cache.kijun[i]
    sh, sl_, ch = cache.senkou_h[i], cache.senkou_l[i], cache.chikou[i]

    if any(np.isnan(x) for x in (n1, n2, tk, kj, sh, sl_, ch)):
        return ("HOLD", close, "indicators_not_ready")

    # EXIT (intra-bar SL FIRST, then optional TP, then signal close)
    if has_pos == "long":
        sl_price = entry_price * (1 - cfg.sl_initial_pct)
        if low <= sl_price:
            return ("SL_LONG", sl_price, "sl_5pct_hit")
        if cfg.tp_pct is not None:
            tp_price = entry_price * (1 + cfg.tp_pct)
            if high >= tp_price:
                return ("TP_LONG", tp_price, f"tp_{cfg.tp_pct*100:.0f}pct_hit")
        if _close_long(close, n1, n2, tk, kj, sh, ch):
            return ("CLOSE_LONG", close, "hull_ichimoku_close_long")
        return ("HOLD", close, "in_long")

    if has_pos == "short":
        sl_price = entry_price * (1 + cfg.sl_initial_pct)
        if high >= sl_price:
            return ("SL_SHORT", sl_price, "sl_5pct_hit")
        if cfg.tp_pct is not None:
            tp_price = entry_price * (1 - cfg.tp_pct)
            if low <= tp_price:
                return ("TP_SHORT", tp_price, f"tp_{cfg.tp_pct*100:.0f}pct_hit")
        if _close_short(close, n1, n2, tk, kj, sl_, ch):
            return ("CLOSE_SHORT", close, "hull_ichimoku_close_short")
        return ("HOLD", close, "in_short")

    # ENTRY (signal + filter)
    ls = _long_entry(close, n1, n2, tk, kj, sh, ch)
    ss = _short_entry(close, n1, n2, tk, kj, sl_, ch)
    if ls or ss:
        ok, why = passes_filters(close, n1, n2, kj, cfg)
        if not ok:
            return ("HOLD", close, f"filter_blocked: {why}")
        if ls:
            return ("OPEN_LONG", close, "hull_ichimoku_long")
        return ("OPEN_SHORT", close, "hull_ichimoku_short")
    return ("HOLD", close, "no_signal")


def _bb_decision(i: int, df: pd.DataFrame, cache, cfg: BBMRConfig,
                 has_pos: Optional[str], entry_price: float,
                 bars_held: int) -> tuple[str, float, str]:
    bar = df.iloc[i]
    close = float(bar["close"]); high = float(bar["high"]); low = float(bar["low"])
    bb_lo = cache.bb_lower[i]; bb_up = cache.bb_upper[i]; rsi = cache.rsi[i]
    if any(np.isnan(x) for x in (bb_lo, bb_up, rsi)):
        return ("HOLD", close, "indicators_not_ready")

    if has_pos == "long":
        sl_price = entry_price * (1 - cfg.sl_pct)
        tp_price = entry_price * (1 + cfg.sl_pct * cfg.tp_rr)
        if low <= sl_price:
            return ("SL_LONG", sl_price, f"sl_{cfg.sl_pct*100:.1f}pct_hit")
        if high >= tp_price:
            return ("TP_LONG", tp_price, f"tp_{cfg.tp_rr:.2f}R_hit")
        if bars_held >= cfg.max_bars_in_trade:
            return ("CLOSE_LONG", close, "max_bars_time_exit")
        return ("HOLD", close, "in_long")

    if has_pos == "short":
        sl_price = entry_price * (1 + cfg.sl_pct)
        tp_price = entry_price * (1 - cfg.sl_pct * cfg.tp_rr)
        if high >= sl_price:
            return ("SL_SHORT", sl_price, f"sl_{cfg.sl_pct*100:.1f}pct_hit")
        if low <= tp_price:
            return ("TP_SHORT", tp_price, f"tp_{cfg.tp_rr:.2f}R_hit")
        if bars_held >= cfg.max_bars_in_trade:
            return ("CLOSE_SHORT", close, "max_bars_time_exit")
        return ("HOLD", close, "in_short")

    if i == 0:
        return ("HOLD", close, "first_bar")
    prev = df.iloc[i - 1]
    bb_lo_p = cache.bb_lower[i - 1]; bb_up_p = cache.bb_upper[i - 1]
    if any(np.isnan(x) for x in (bb_lo_p, bb_up_p)):
        return ("HOLD", close, "no_signal")
    cu = (float(prev["close"]) < bb_lo_p) and (close >= bb_lo)
    cd = (float(prev["close"]) > bb_up_p) and (close <= bb_up)
    long_sig = cu and rsi < (cfg.rsi_oversold + 10)
    short_sig = cd and rsi > (cfg.rsi_overbought - 10)
    if long_sig:
        return ("OPEN_LONG", close, "bb_lower_reversal")
    if short_sig:
        return ("OPEN_SHORT", close, "bb_upper_reversal")
    return ("HOLD", close, "no_signal")


# ============================================================================
# Backtest engine
# ============================================================================

class Position:
    __slots__ = ("direction", "entry_ts", "entry_price", "qty", "pos_usd",
                 "entry_fee", "bars_held")

    def __init__(self, direction: str, entry_ts, entry_price: float,
                 qty: float, pos_usd: float, entry_fee: float):
        self.direction = direction
        self.entry_ts = entry_ts
        self.entry_price = entry_price
        self.qty = qty
        self.pos_usd = pos_usd
        self.entry_fee = entry_fee
        self.bars_held = 0


def run_backtest(cfg: AppConfig, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    pairs_data: dict[str, dict] = {}
    for pc in cfg.pairs:
        if not pc.enabled:
            continue
        path = DATA_DIR / f"{pc.symbol}_{pc.timeframe}.parquet"
        if not path.exists():
            print(f"  [{pc.symbol}] MISSING parquet: {path}")
            continue
        df = pd.read_parquet(path)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        df = df.loc[(df.index >= start) & (df.index <= end)].copy()
        if df.empty:
            print(f"  [{pc.symbol}] no bars in range")
            continue
        if pc.strategy == "bb_mr":
            scfg = _make_bb_cfg(pc, cfg.portfolio.taker_fee)
            cache = bb_precompute(df, scfg)
        else:
            scfg = _make_hi_cfg(pc)
            cache = hi_precompute(df, scfg)
        pairs_data[pc.symbol] = {
            "pair_cfg": pc, "scfg": scfg, "df": df, "cache": cache,
            "pos": None,
        }
        print(f"  [{pc.symbol}] {pc.strategy:5s}  bars={len(df):,}  "
              f"{df.index[0]} → {df.index[-1]}")

    if not pairs_data:
        raise SystemExit("No pairs loaded")

    # Build merged timeline (sorted unique union of all bar timestamps)
    all_ts = sorted(set().union(*[d["df"].index for d in pairs_data.values()]))

    equity = cfg.portfolio.pool_total
    initial_equity = equity
    fee = cfg.portfolio.taker_fee
    cap_lev = cfg.portfolio.leverage_max
    cap_pct = cfg.portfolio.cap_pct_of_max

    trades: list[Trade] = []
    eq_curve = [(all_ts[0], equity)]
    peak = equity
    max_dd = 0.0

    # Per-pair index pointers (so we lookup row in O(1) using set-index)
    # We'll convert df indexes to position lookup
    for sym, d in pairs_data.items():
        d["ts_to_i"] = {ts: i for i, ts in enumerate(d["df"].index)}

    for ts in all_ts:
        # Order: existing pairs evaluated in config order (deterministic)
        for sym, d in pairs_data.items():
            if ts not in d["ts_to_i"]:
                continue
            i = d["ts_to_i"][ts]
            pc: PairConfig = d["pair_cfg"]
            pos: Optional[Position] = d["pos"]
            has_pos = (pos.direction.lower() if pos else None)
            entry_px = pos.entry_price if pos else 0.0
            bars_held = pos.bars_held if pos else 0

            if pc.strategy == "bb_mr":
                action, px, reason = _bb_decision(
                    i, d["df"], d["cache"], d["scfg"], has_pos, entry_px, bars_held)
            else:
                action, px, reason = _hi_decision(
                    i, d["df"], d["cache"], d["scfg"], has_pos, entry_px)

            if pos is not None:
                pos.bars_held += 1

            # Handle exits
            if action.startswith(("SL_", "TP_", "CLOSE_")) and pos is not None:
                # Compute pnl
                if pos.direction == "LONG":
                    raw_pnl = (px - pos.entry_price) * pos.qty
                else:
                    raw_pnl = (pos.entry_price - px) * pos.qty
                exit_fee = px * pos.qty * fee
                total_fees = pos.entry_fee + exit_fee
                pnl_net = raw_pnl - total_fees
                equity += pnl_net
                trades.append(Trade(
                    symbol=sym, strategy=pc.strategy,
                    direction=pos.direction,
                    entry_ts=pos.entry_ts, entry_price=pos.entry_price,
                    exit_ts=ts, exit_price=px,
                    qty=pos.qty, pos_usd=pos.pos_usd,
                    pnl=pnl_net, fees=total_fees,
                    reason=reason, bars_held=pos.bars_held,
                    equity_after=equity,
                ))
                d["pos"] = None
                pos = None
                # Update DD/peak after close
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / peak if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd

            # Handle entry (only if currently flat after possible exit)
            if action in ("OPEN_LONG", "OPEN_SHORT") and d["pos"] is None:
                if equity <= 0:
                    continue  # busted
                pos_usd, risk_usd, skip = size_position(pc, equity, cap_lev, cap_pct)
                if skip:
                    continue
                qty = pos_usd / px
                entry_fee = pos_usd * fee
                direction = "LONG" if action == "OPEN_LONG" else "SHORT"
                d["pos"] = Position(direction, ts, px, qty, pos_usd, entry_fee)

        eq_curve.append((ts, equity))

    # Close any open positions at the last bar (mark to market)
    for sym, d in pairs_data.items():
        pos: Optional[Position] = d["pos"]
        if pos is None:
            continue
        last_close = float(d["df"].iloc[-1]["close"])
        last_ts = d["df"].index[-1]
        if pos.direction == "LONG":
            raw_pnl = (last_close - pos.entry_price) * pos.qty
        else:
            raw_pnl = (pos.entry_price - last_close) * pos.qty
        exit_fee = last_close * pos.qty * fee
        total_fees = pos.entry_fee + exit_fee
        pnl_net = raw_pnl - total_fees
        equity += pnl_net
        trades.append(Trade(
            symbol=sym, strategy=d["pair_cfg"].strategy,
            direction=pos.direction,
            entry_ts=pos.entry_ts, entry_price=pos.entry_price,
            exit_ts=last_ts, exit_price=last_close,
            qty=pos.qty, pos_usd=pos.pos_usd,
            pnl=pnl_net, fees=total_fees,
            reason="EOD_MARK_TO_MARKET", bars_held=pos.bars_held,
            equity_after=equity,
        ))

    return {
        "trades": trades, "equity_curve": eq_curve,
        "initial_equity": initial_equity, "final_equity": equity,
        "max_dd": max_dd, "peak": peak,
        "start": all_ts[0], "end": all_ts[-1],
    }


# ============================================================================
# Reporting
# ============================================================================

def summary_metrics(result: dict) -> dict:
    """Returneaza metrici-cheie pentru tabel comparativ (folosit de multi-window)."""
    trades: list[Trade] = result["trades"]
    eq0 = result["initial_equity"]; eqN = result["final_equity"]
    n_years = (result["end"] - result["start"]).total_seconds() / (365.25 * 86400)
    if not trades:
        return {"n_trades": 0, "wr": 0, "pf": 0, "final": eqN, "cagr": 0,
                "max_dd": result["max_dd"]*100, "n_years": n_years}
    df = pd.DataFrame([t.__dict__ for t in trades])
    wins = df[df["pnl"] > 0]; losses = df[df["pnl"] <= 0]
    gross_win = wins["pnl"].sum(); gross_loss = -losses["pnl"].sum()
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = len(wins) / len(df) * 100
    cagr = ((eqN / eq0) ** (1 / n_years) - 1) * 100 if n_years > 0 and eqN > 0 else 0
    return {
        "n_trades": len(df), "wr": wr, "pf": pf,
        "final": eqN, "cagr": cagr,
        "max_dd": result["max_dd"]*100, "n_years": n_years,
        "per_pair": {sym: {"trades": len(df[df["symbol"]==sym]),
                            "pnl": df[df["symbol"]==sym]["pnl"].sum()}
                     for sym in df["symbol"].unique()},
    }


def report(result: dict) -> None:
    trades: list[Trade] = result["trades"]
    eq0 = result["initial_equity"]; eqN = result["final_equity"]
    n_years = (result["end"] - result["start"]).total_seconds() / (365.25 * 86400)

    if not trades:
        print("\n  NO TRADES.")
        return

    df = pd.DataFrame([t.__dict__ for t in trades])
    wins = df[df["pnl"] > 0]; losses = df[df["pnl"] <= 0]
    gross_win = wins["pnl"].sum(); gross_loss = -losses["pnl"].sum()
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = len(wins) / len(df) * 100
    cagr = ((eqN / eq0) ** (1 / n_years) - 1) * 100 if n_years > 0 and eqN > 0 else 0

    print(f"\n{'='*72}")
    print(f"  V4 PORTFOLIO BACKTEST  ({result['start'].date()} → {result['end'].date()}, "
          f"{n_years:.2f}y)")
    print(f"{'='*72}")
    print(f"  Initial:    ${eq0:>14,.2f}")
    print(f"  Final:      ${eqN:>14,.2f}     "
          f"(return {(eqN/eq0 - 1)*100:+.1f}%, CAGR {cagr:+.1f}%/y)")
    print(f"  Max DD:     {result['max_dd']*100:>14.2f}%")
    print(f"  Trades:     {len(df):>14}")
    print(f"  Win rate:   {wr:>14.2f}%")
    print(f"  Profit factor: {pf:>11.3f}")
    print(f"  Avg win/loss:  ${wins['pnl'].mean() if len(wins) else 0:>+11.2f}  /  "
          f"${(losses['pnl'].mean() if len(losses) else 0):>+.2f}")
    print(f"  Total fees: ${df['fees'].sum():>14,.2f}")

    print(f"\n  {'PER-PAIR BREAKDOWN':-^72}")
    for sym in df["symbol"].unique():
        sub = df[df["symbol"] == sym]
        sub_wins = sub[sub["pnl"] > 0]; sub_losses = sub[sub["pnl"] <= 0]
        sub_pf = (sub_wins["pnl"].sum() / -sub_losses["pnl"].sum()
                  if sub_losses["pnl"].sum() < 0 else float("inf"))
        sub_wr = len(sub_wins) / len(sub) * 100
        print(f"  {sym:10s} {sub.iloc[0]['strategy']:5s}  "
              f"trades={len(sub):>4}  WR={sub_wr:>5.1f}%  "
              f"PF={sub_pf:>5.2f}  pnl=${sub['pnl'].sum():>+11,.2f}")

    # Exit-reason breakdown
    print(f"\n  {'EXIT REASONS':-^72}")
    rc = df["reason"].value_counts()
    for reason, n in rc.items():
        sub = df[df["reason"] == reason]
        print(f"  {reason:35s} n={n:>4}  pnl=${sub['pnl'].sum():>+11,.2f}")

    # Save trades + equity curve
    out_dir = ROOT / "results"
    out_dir.mkdir(exist_ok=True)
    df.to_csv(out_dir / "backtest_v4_trades.csv", index=False)
    eq_df = pd.DataFrame(result["equity_curve"], columns=["ts", "equity"])
    eq_df.to_csv(out_dir / "backtest_v4_equity.csv", index=False)
    print(f"\n  saved → results/backtest_v4_trades.csv  ({len(df)} rows)")
    print(f"  saved → results/backtest_v4_equity.csv ({len(eq_df)} rows)")


# ============================================================================
# Main
# ============================================================================

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/config_v4.yaml")
    p.add_argument("--start", default="2022-01-01")
    p.add_argument("--end", default="2026-12-31")
    args = p.parse_args()

    cfg = load_config(args.config)
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    print(f"\n  V4 backtest  config={args.config}  range={start.date()} → {end.date()}")
    print(f"  Pool ${cfg.portfolio.pool_total}  fee={cfg.portfolio.taker_fee*100:.3f}%  "
          f"cap_lev={cfg.portfolio.leverage_max}×")

    result = run_backtest(cfg, start, end)
    report(result)


if __name__ == "__main__":
    main()
