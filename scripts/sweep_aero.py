"""Parameter sweep pentru AEROUSDT 4h cu Hull+Ichimoku.

2 etape pentru a evita explozia combinatorica:
  Stage 1: indicator periods (hull × kijun × snkb)
           sizing fix: risk=5%, SL=5%, TP=None, lev=20x
  Stage 2: pe top 5 combos Stage 1, sweep SL × TP

Foloseste 70/30 mix entry fee (live realistic).

Uzitare:
    python scripts/sweep_aero.py
"""

from __future__ import annotations

import sys
from itertools import product
from pathlib import Path
from copy import deepcopy

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

DATA_DIR = Path("/tmp/aero_data")
START = pd.Timestamp("2024-07-15", tz="UTC")
END = pd.Timestamp("2026-04-25", tz="UTC")
ENTRY_FEE = 0.000305    # 70/30 maker/taker mix (live realistic)
EXIT_FEE = 0.00055      # exits taker (siguranta)


def make_cfg(hull: int, kijun: int, snkb: int, sl: float,
             tp: float | None, risk: float = 0.07,
             leverage: int = 20) -> AppConfig:
    pair = PairConfig(
        symbol="AEROUSDT", timeframe="4h", enabled=True,
        leverage=leverage, hull_length=hull, tenkan_periods=9,
        kijun_periods=kijun, senkou_b_periods=snkb, displacement=24,
        risk_pct_per_trade=risk, sl_initial_pct=sl, tp_pct=tp,
        max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0,
    )
    return AppConfig(
        portfolio=PortfolioConfig(
            name="aero_sweep", pool_total=100.0, leverage=15,
            cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0,
        ),
        pairs=[pair],
        operational=OperationalConfig(max_concurrent_positions=1),
    )


def run_one(hull, kijun, snkb, sl, tp, risk=0.07, leverage=20) -> dict:
    cfg = make_cfg(hull, kijun, snkb, sl, tp, risk, leverage)
    qty_steps = {"AEROUSDT": 0.1}
    try:
        result = bt.run_backtest(cfg, DATA_DIR, START, END, qty_steps,
                                 entry_fee=ENTRY_FEE, exit_fee=EXIT_FEE)
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
        "hull": hull, "kijun": kijun, "snkb": snkb,
        "sl": sl, "tp": tp, "risk": risk, "leverage": leverage,
        "n": len(trades),
        "wr": len(wins) / len(trades) * 100,
        "pf": pf, "ret": (final / 100 - 1) * 100, "dd": dd, "final": final,
    }


def stage1_indicators() -> list[dict]:
    """Indicator periods sweep, sizing fix (risk=5%, SL=5%, no TP)."""
    print("\n=== STAGE 1 — Indicators (hull × kijun × snkb) ===")
    hulls = [8, 10, 12, 16]
    kijuns = [24, 36, 48, 60]
    snkbs = [26, 40, 52]
    combos = list(product(hulls, kijuns, snkbs))
    print(f"Running {len(combos)} combos (risk=5%, SL=5%, no TP, lev=20x)...")
    results = []
    for i, (h, k, s) in enumerate(combos, 1):
        r = run_one(h, k, s, sl=0.05, tp=None, risk=0.05)
        results.append(r)
        if i % 10 == 0:
            print(f"  {i}/{len(combos)} done")
    return results


def stage2_sl_tp(top_combos: list[dict]) -> list[dict]:
    """SL × TP sweep pe top indicator combos."""
    print("\n=== STAGE 2 — SL × TP on top indicator combos ===")
    sls = [0.03, 0.04, 0.05, 0.06, 0.08, 0.10]
    tps_options = [None, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20]
    combos: list[tuple] = []
    for top in top_combos:
        for sl in sls:
            for tp in tps_options:
                combos.append((top["hull"], top["kijun"], top["snkb"], sl, tp))
    print(f"Running {len(combos)} combos (risk=7% AGGRESSIVE per MNT preset)...")
    results = []
    for i, (h, k, s, sl, tp) in enumerate(combos, 1):
        r = run_one(h, k, s, sl, tp, risk=0.07)
        results.append(r)
        if i % 25 == 0:
            print(f"  {i}/{len(combos)} done")
    return results


def report(results: list[dict], title: str, top_n: int = 10,
           dd_filter: float = -55.0) -> None:
    print(f"\n--- {title} ---")
    valid = [r for r in results if "error" not in r and r["n"] >= 30 and r["dd"] >= dd_filter]
    if not valid:
        print(f"  (no results passing filters: n>=30, dd>={dd_filter}%)")
        return
    by_ret = sorted(valid, key=lambda x: -x["ret"])[:top_n]
    print(f"\nTop {top_n} by Return (n>=30, DD>={dd_filter}%):")
    print(f"  {'hull':<5}{'kj':<5}{'snkB':<5}{'SL':<6}{'TP':<8}{'risk':<6}"
          f"{'n':<5}{'WR':<7}{'PF':<7}{'Ret':<10}{'DD':<8}")
    for r in by_ret:
        tp_str = f"{r['tp']*100:.0f}%" if r['tp'] else "—"
        print(f"  {r['hull']:<5}{r['kijun']:<5}{r['snkb']:<5}"
              f"{r['sl']*100:<5.1f}%{tp_str:<8}{r['risk']*100:<5.0f}%"
              f"{r['n']:<5}{r['wr']:<6.1f}%{r['pf']:<7.2f}"
              f"{r['ret']:<+9.1f}%{r['dd']:<+7.1f}%")
    by_pf = sorted(valid, key=lambda x: -x["pf"])[:top_n]
    print(f"\nTop {top_n} by PF (n>=30, DD>={dd_filter}%):")
    for r in by_pf:
        tp_str = f"{r['tp']*100:.0f}%" if r['tp'] else "—"
        print(f"  {r['hull']:<5}{r['kijun']:<5}{r['snkb']:<5}"
              f"{r['sl']*100:<5.1f}%{tp_str:<8}{r['risk']*100:<5.0f}%"
              f"{r['n']:<5}{r['wr']:<6.1f}%{r['pf']:<7.2f}"
              f"{r['ret']:<+9.1f}%{r['dd']:<+7.1f}%")


def main() -> int:
    print(f"AEROUSDT 4h sweep — {START.date()} → {END.date()}")
    print(f"Fees: entry={ENTRY_FEE*100:.4f}% (70/30 mix), exit={EXIT_FEE*100:.4f}% (taker)")
    print(f"Initial capital: $100, Leverage 20x")

    # Stage 1
    s1_results = stage1_indicators()
    report(s1_results, "Stage 1: Indicators (sizing fix)", top_n=8)

    # Pick top 5 indicator combos by PF (mai robust ca Return alone pe sample mic)
    s1_valid = [r for r in s1_results if "error" not in r and r["n"] >= 30]
    s1_top = sorted(s1_valid, key=lambda x: -x["pf"])[:5]
    print(f"\nTop 5 indicator combos forward la Stage 2:")
    for t in s1_top:
        print(f"  hull={t['hull']}  kijun={t['kijun']}  snkb={t['snkb']}  PF={t['pf']:.2f}  Ret={t['ret']:+.1f}%")

    # Stage 2: SL × TP
    s2_results = stage2_sl_tp(s1_top)
    report(s2_results, "Stage 2: SL × TP × Indicators (risk=7% Aggressive)", top_n=10)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
