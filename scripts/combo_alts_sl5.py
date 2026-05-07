"""Combo backtest cu SL=5% pe 10 perechi top din ranking.

Testeaza:
  - Single baseline pentru fiecare
  - Toate 2-pair combinations (45 combos)
  - Top 3-pair combinations (basate pe ce a mers bine la 2-pair)

Sortat dupa PnL absolut.
"""

from __future__ import annotations

import shutil
import sys
from itertools import combinations
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ichimoku_bot.config import AppConfig, PairConfig, PortfolioConfig, OperationalConfig

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("bm", str(ROOT / "scripts" / "backtest.py"))
bt = _ilu.module_from_spec(_spec); sys.modules["bm"] = bt; _spec.loader.exec_module(bt)


# 10 perechi cu setari Hull/Kj/SnkB optime + SL=5% + TP din best @ SL=5%
SL5 = {
    "MNTUSDT":  {"hull": 8,  "kj": 48, "snkb": 40, "sl": 0.05, "tp": None,  "step": 0.1,  "start": "2023-10-01"},
    "AXSUSDT":  {"hull": 10, "kj": 48, "snkb": 26, "sl": 0.05, "tp": None,  "step": 0.1,  "start": "2023-10-01"},
    "ILVUSDT":  {"hull": 10, "kj": 36, "snkb": 40, "sl": 0.05, "tp": 0.20,  "step": 0.01, "start": "2023-10-01"},
    "AEROUSDT": {"hull": 10, "kj": 60, "snkb": 26, "sl": 0.05, "tp": 0.20,  "step": 0.1,  "start": "2024-07-15"},
    "SUNUSDT":  {"hull": 8,  "kj": 24, "snkb": 52, "sl": 0.05, "tp": None,  "step": 10.0, "start": "2023-10-01"},
    "XCNUSDT":  {"hull": 12, "kj": 60, "snkb": 40, "sl": 0.05, "tp": None,  "step": 10.0, "start": "2023-10-01"},
    "RSRUSDT":  {"hull": 10, "kj": 60, "snkb": 52, "sl": 0.05, "tp": 0.05,  "step": 10.0, "start": "2023-10-01"},
    "AKTUSDT":  {"hull": 8,  "kj": 60, "snkb": 52, "sl": 0.05, "tp": 0.08,  "step": 1.0,  "start": "2024-06-26"},
    "ATOMUSDT": {"hull": 12, "kj": 60, "snkb": 26, "sl": 0.05, "tp": 0.20,  "step": 0.1,  "start": "2023-10-01"},
    "PEAQUSDT": {"hull": 16, "kj": 48, "snkb": 52, "sl": 0.05, "tp": 0.12,  "step": 1.0,  "start": "2024-11-19"},
}


def make_pair(symbol: str, c: dict) -> PairConfig:
    return PairConfig(
        symbol=symbol, timeframe="4h", enabled=True,
        leverage=20, hull_length=c["hull"], tenkan_periods=9,
        kijun_periods=c["kj"], senkou_b_periods=c["snkb"], displacement=24,
        risk_pct_per_trade=0.07, sl_initial_pct=c["sl"], tp_pct=c["tp"],
        max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0,
    )


def run_combo(symbols: tuple[str, ...]) -> dict | None:
    qty_steps = {s: SL5[s]["step"] for s in symbols}
    start = max(pd.Timestamp(SL5[s]["start"], tz="UTC") for s in symbols)
    end = pd.Timestamp("2026-04-25", tz="UTC")
    months = (end - start).days / 30.4
    if months < 6:
        return None  # too short period for valid stats

    combo_id = "_".join(sorted(s.replace("USDT","") for s in symbols))
    combo_dir = Path(f"/tmp/sl5alt_{combo_id}")
    combo_dir.mkdir(exist_ok=True)
    for s in symbols:
        src = Path(f"/tmp/{s.replace('USDT','').lower()}_data/{s}_4h.parquet")
        dst = combo_dir / f"{s}_4h.parquet"
        if not dst.exists() and src.exists():
            shutil.copy(src, dst)
        elif not src.exists():
            return None  # missing data

    cfg = AppConfig(
        portfolio=PortfolioConfig(name="alts5", pool_total=100.0, leverage=15,
                                  cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0),
        pairs=[make_pair(s, SL5[s]) for s in symbols],
        operational=OperationalConfig(max_concurrent_positions=len(symbols)),
    )
    try:
        result = bt.run_backtest(cfg, combo_dir, start, end, qty_steps,
                                 entry_fee=0.000305, exit_fee=0.00055)
    except Exception as e:
        return None

    trades = result["trades"]
    final = result["final_equity"]
    eq = [v for _, v in result["equity_curve"]]
    if not eq or len(trades) < 30:
        return None
    peaks = np.maximum.accumulate(eq)
    dd = float(((np.array(eq) - peaks) / peaks * 100).min())
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gw = sum(t.pnl_net for t in wins); gl = abs(sum(t.pnl_net for t in losses)) or 1e-9
    pf = gw / gl
    annualized = ((final / 100) ** (12 / months) - 1) * 100 if months > 0 else 0
    return {
        "symbols": symbols, "months": months, "n": len(trades),
        "final": final, "ret": (final/100-1)*100,
        "ann": annualized, "dd": dd, "pf": pf,
    }


all_pairs = list(SL5.keys())
print(f"\nTesting SL=5% combos pe {len(all_pairs)} perechi...")

# Single baseline
print("\n--- BASELINE single-pair (SL=5%) ---")
print(f"{'Pair':<10}{'mo':<6}{'n':<6}{'Final':<11}{'Ret':<11}{'Ann':<11}{'PF':<7}{'DD':<7}")
single_results = []
for s in all_pairs:
    r = run_combo((s,))
    if r is None: continue
    single_results.append(r)
    print(f"  {s.replace('USDT',''):<8}{r['months']:<6.1f}{r['n']:<6}${r['final']:<10.0f}"
          f"{r['ret']:<+9.0f}% {r['ann']:<+9.0f}%{r['pf']:<7.2f}{r['dd']:<+6.0f}%")

# 2-pair combos
print(f"\n--- 2-PAIR COMBOS (top 20 by PnL) ---")
two_results = []
for combo in combinations(all_pairs, 2):
    r = run_combo(combo)
    if r is not None:
        two_results.append(r)
two_results.sort(key=lambda x: -x["final"])
print(f"{'Pair1':<8}{'Pair2':<8}{'mo':<6}{'n':<6}{'Final':<11}{'Ann':<11}{'PF':<7}{'DD':<7}")
for r in two_results[:20]:
    s1, s2 = [s.replace("USDT","") for s in r["symbols"]]
    print(f"  {s1:<7}+{s2:<7}{r['months']:<6.1f}{r['n']:<6}${r['final']:<10.0f}"
          f"{r['ann']:<+9.0f}%{r['pf']:<7.2f}{r['dd']:<+6.0f}%")

# 3-pair combos — focus on top 6 pairs by single PnL
single_results.sort(key=lambda x: -x["final"])
top_singles = [r["symbols"][0] for r in single_results[:7]]
print(f"\n--- 3-PAIR COMBOS (din top 7 single: {[s.replace('USDT','') for s in top_singles]}) ---")
three_results = []
for combo in combinations(top_singles, 3):
    r = run_combo(combo)
    if r is not None:
        three_results.append(r)
three_results.sort(key=lambda x: -x["final"])
print(f"{'Combo':<28}{'mo':<6}{'n':<6}{'Final':<13}{'Ann':<11}{'PF':<7}{'DD':<7}")
for r in three_results[:15]:
    pairs_str = "+".join(s.replace("USDT","") for s in r["symbols"])
    print(f"  {pairs_str:<26}{r['months']:<6.1f}{r['n']:<6}${r['final']:<12.0f}"
          f"{r['ann']:<+9.0f}%{r['pf']:<7.2f}{r['dd']:<+6.0f}%")

# Save full results
import csv
with open("/tmp/combo_sl5_alts.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["pairs", "months", "n", "final", "ret", "ann", "pf", "dd"])
    for r in single_results + two_results + three_results:
        w.writerow(["+".join(s.replace("USDT","") for s in r["symbols"]),
                   round(r["months"], 1), r["n"], round(r["final"], 2),
                   round(r["ret"], 1), round(r["ann"], 1), round(r["pf"], 3), round(r["dd"], 1)])
print(f"\nFull CSV: /tmp/combo_sl5_alts.csv ({len(single_results)+len(two_results)+len(three_results)} rows)")
