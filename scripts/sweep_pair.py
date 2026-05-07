"""Generic parameter sweep — orice pereche cu data 4h disponibila.

Uzitare:
    python scripts/sweep_pair.py --symbol DYDXUSDT --data-dir /tmp/dydx_data \
        --start 2023-10-01 --end 2026-04-25
"""

from __future__ import annotations

import argparse
import sys
from itertools import product
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ichimoku_bot.config import AppConfig, PairConfig, PortfolioConfig, OperationalConfig

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("backtest_mod", str(ROOT / "scripts" / "backtest.py"))
bt = _ilu.module_from_spec(_spec)  # type: ignore
sys.modules["backtest_mod"] = bt
_spec.loader.exec_module(bt)        # type: ignore


def make_cfg(symbol: str, hull: int, kijun: int, snkb: int, sl: float,
             tp: float | None, risk: float, leverage: int) -> AppConfig:
    pair = PairConfig(
        symbol=symbol, timeframe="4h", enabled=True,
        leverage=leverage, hull_length=hull, tenkan_periods=9,
        kijun_periods=kijun, senkou_b_periods=snkb, displacement=24,
        risk_pct_per_trade=risk, sl_initial_pct=sl, tp_pct=tp,
        max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0,
    )
    return AppConfig(
        portfolio=PortfolioConfig(
            name="sweep", pool_total=100.0, leverage=15,
            cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0,
        ),
        pairs=[pair],
        operational=OperationalConfig(max_concurrent_positions=1),
    )


def run_one(symbol: str, data_dir: Path, start: pd.Timestamp, end: pd.Timestamp,
            hull, kijun, snkb, sl, tp, risk, leverage,
            entry_fee: float, exit_fee: float, qty_step: float) -> dict:
    cfg = make_cfg(symbol, hull, kijun, snkb, sl, tp, risk, leverage)
    qty_steps = {symbol: qty_step}
    try:
        result = bt.run_backtest(cfg, data_dir, start, end, qty_steps,
                                 entry_fee=entry_fee, exit_fee=exit_fee)
    except Exception as e:
        return {"error": str(e)}
    trades = result["trades"]
    final = result["final_equity"]
    eq_vals = [v for _, v in result["equity_curve"]]
    if not trades:
        return {"hull": hull, "kijun": kijun, "snkb": snkb, "sl": sl, "tp": tp,
                "risk": risk, "n": 0, "wr": 0.0, "pf": 0.0, "ret": 0.0, "dd": 0.0,
                "final": final}
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gross_win = sum(t.pnl_net for t in wins)
    gross_loss = abs(sum(t.pnl_net for t in losses)) or 1e-9
    pf = gross_win / gross_loss if losses else float("inf")
    peaks = np.maximum.accumulate(eq_vals)
    dd = float(((np.array(eq_vals) - peaks) / peaks * 100).min())
    return {
        "hull": hull, "kijun": kijun, "snkb": snkb, "sl": sl, "tp": tp,
        "risk": risk, "leverage": leverage,
        "n": len(trades), "wr": len(wins) / len(trades) * 100,
        "pf": pf, "ret": (final / 100 - 1) * 100, "dd": dd, "final": final,
    }


def report(results: list[dict], title: str, top_n: int = 10,
           dd_filter: float = -55.0) -> None:
    print(f"\n--- {title} ---")
    valid = [r for r in results if "error" not in r and r["n"] >= 30 and r["dd"] >= dd_filter]
    if not valid:
        print(f"  (no results passing filters: n>=30, dd>={dd_filter}%)")
        return
    by_ret = sorted(valid, key=lambda x: -x["ret"])[:top_n]
    print(f"\nTop {top_n} by Return (n>=30, DD>={dd_filter}%):")
    print(f"  {'hull':<5}{'kj':<5}{'snkB':<5}{'SL':<7}{'TP':<8}"
          f"{'n':<5}{'WR':<7}{'PF':<7}{'Ret':<10}{'DD':<8}")
    for r in by_ret:
        tp_str = f"{r['tp']*100:.0f}%" if r['tp'] else "—"
        print(f"  {r['hull']:<5}{r['kijun']:<5}{r['snkb']:<5}"
              f"{r['sl']*100:<5.1f}%  {tp_str:<8}"
              f"{r['n']:<5}{r['wr']:<6.1f}%{r['pf']:<7.2f}"
              f"{r['ret']:<+9.1f}%{r['dd']:<+7.1f}%")
    by_pf = sorted(valid, key=lambda x: -x["pf"])[:top_n]
    print(f"\nTop {top_n} by PF (n>=30, DD>={dd_filter}%):")
    print(f"  {'hull':<5}{'kj':<5}{'snkB':<5}{'SL':<7}{'TP':<8}"
          f"{'n':<5}{'WR':<7}{'PF':<7}{'Ret':<10}{'DD':<8}")
    for r in by_pf:
        tp_str = f"{r['tp']*100:.0f}%" if r['tp'] else "—"
        print(f"  {r['hull']:<5}{r['kijun']:<5}{r['snkb']:<5}"
              f"{r['sl']*100:<5.1f}%  {tp_str:<8}"
              f"{r['n']:<5}{r['wr']:<6.1f}%{r['pf']:<7.2f}"
              f"{r['ret']:<+9.1f}%{r['dd']:<+7.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--qty-step", type=float, default=0.1)
    ap.add_argument("--leverage", type=int, default=20)
    ap.add_argument("--entry-fee", type=float, default=0.000305)  # 70/30 mix
    ap.add_argument("--exit-fee", type=float, default=0.00055)    # taker
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    sym = args.symbol

    print(f"\n{sym} 4h sweep — {start.date()} → {end.date()}")
    print(f"Fees: entry={args.entry_fee*100:.4f}%, exit={args.exit_fee*100:.4f}%")
    print(f"Initial $100, lev {args.leverage}x, qty_step {args.qty_step}")

    # Stage 1: indicator periods
    hulls = [8, 10, 12, 16]
    kijuns = [24, 36, 48, 60]
    snkbs = [26, 40, 52]
    s1_combos = list(product(hulls, kijuns, snkbs))
    print(f"\n=== STAGE 1 — Indicators ({len(s1_combos)} combos, risk=5%, SL=5%, no TP) ===")
    s1 = []
    for i, (h, k, s) in enumerate(s1_combos, 1):
        r = run_one(sym, data_dir, start, end, h, k, s, sl=0.05, tp=None,
                    risk=0.05, leverage=args.leverage,
                    entry_fee=args.entry_fee, exit_fee=args.exit_fee,
                    qty_step=args.qty_step)
        s1.append(r)
        if i % 10 == 0:
            print(f"  {i}/{len(s1_combos)} done")
    report(s1, "Stage 1: Indicators (sizing fix)", top_n=8)

    s1_valid = [r for r in s1 if "error" not in r and r["n"] >= 30]
    if not s1_valid:
        print("\nNo valid Stage 1 results — abort.")
        return 1
    s1_top = sorted(s1_valid, key=lambda x: -x["pf"])[:5]
    print(f"\nTop 5 indicator combos forward la Stage 2:")
    for t in s1_top:
        print(f"  hull={t['hull']}  kijun={t['kijun']}  snkb={t['snkb']}  "
              f"PF={t['pf']:.2f}  Ret={t['ret']:+.1f}%")

    # Stage 2: SL × TP (risk=7% Aggressive)
    sls = [0.03, 0.04, 0.05, 0.06, 0.08, 0.10]
    tps = [None, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20]
    s2_combos = []
    for top in s1_top:
        for sl in sls:
            for tp in tps:
                s2_combos.append((top["hull"], top["kijun"], top["snkb"], sl, tp))
    print(f"\n=== STAGE 2 — SL × TP ({len(s2_combos)} combos, risk=7%) ===")
    s2 = []
    for i, (h, k, s, sl, tp) in enumerate(s2_combos, 1):
        r = run_one(sym, data_dir, start, end, h, k, s, sl=sl, tp=tp,
                    risk=0.07, leverage=args.leverage,
                    entry_fee=args.entry_fee, exit_fee=args.exit_fee,
                    qty_step=args.qty_step)
        s2.append(r)
        if i % 25 == 0:
            print(f"  {i}/{len(s2_combos)} done")
    report(s2, "Stage 2: SL × TP × top indicators (risk=7% Aggressive)", top_n=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
