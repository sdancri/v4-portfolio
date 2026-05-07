"""SL=5% combos pe 10 alts + breakdown ANUAL pentru top combos.

Detect bad years (e.g. SUN excelent 2024 dar slab 2025/2026).
"""

from __future__ import annotations

import shutil
import sys
from itertools import combinations
from pathlib import Path
from statistics import mean, stdev

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ichimoku_bot.config import AppConfig, PairConfig, PortfolioConfig, OperationalConfig

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("bm", str(ROOT / "scripts" / "backtest.py"))
bt = _ilu.module_from_spec(_spec); sys.modules["bm"] = bt; _spec.loader.exec_module(bt)


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

PERIODS = [
    ("FULL",   None,         None),
    ("2024",   "2024-01-01", "2025-01-01"),
    ("2025",   "2025-01-01", "2026-01-01"),
    ("2026YTD","2026-01-01", "2026-04-25"),
]


def make_pair(symbol: str, c: dict) -> PairConfig:
    return PairConfig(
        symbol=symbol, timeframe="4h", enabled=True,
        leverage=20, hull_length=c["hull"], tenkan_periods=9,
        kijun_periods=c["kj"], senkou_b_periods=c["snkb"], displacement=24,
        risk_pct_per_trade=0.07, sl_initial_pct=c["sl"], tp_pct=c["tp"],
        max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0,
    )


def run_combo(symbols: tuple[str, ...], start_str: str | None = None,
              end_str: str | None = None) -> dict | None:
    qty_steps = {s: SL5[s]["step"] for s in symbols}
    pair_starts = [pd.Timestamp(SL5[s]["start"], tz="UTC") for s in symbols]
    if start_str is None:
        start = max(pair_starts)
    else:
        start = max(max(pair_starts), pd.Timestamp(start_str, tz="UTC"))
    if end_str is None:
        end = pd.Timestamp("2026-04-25", tz="UTC")
    else:
        end = pd.Timestamp(end_str, tz="UTC")
    months = (end - start).days / 30.4
    if months < 2:
        return None

    combo_id = "_".join(sorted(s.replace("USDT","") for s in symbols))
    combo_dir = Path(f"/tmp/sl5alt_{combo_id}")
    combo_dir.mkdir(exist_ok=True)
    for s in symbols:
        src = Path(f"/tmp/{s.replace('USDT','').lower()}_data/{s}_4h.parquet")
        dst = combo_dir / f"{s}_4h.parquet"
        if not dst.exists() and src.exists():
            shutil.copy(src, dst)

    cfg = AppConfig(
        portfolio=PortfolioConfig(name="x", pool_total=100.0, leverage=15,
                                  cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0),
        pairs=[make_pair(s, SL5[s]) for s in symbols],
        operational=OperationalConfig(max_concurrent_positions=len(symbols)),
    )
    try:
        result = bt.run_backtest(cfg, combo_dir, start, end, qty_steps,
                                 entry_fee=0.000305, exit_fee=0.00055)
    except Exception:
        return None
    trades = result["trades"]
    final = result["final_equity"]
    eq = [v for _, v in result["equity_curve"]]
    if not eq or len(trades) < 5:
        return None
    peaks = np.maximum.accumulate(eq)
    dd = float(((np.array(eq) - peaks) / peaks * 100).min())
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gw = sum(t.pnl_net for t in wins); gl = abs(sum(t.pnl_net for t in losses)) or 1e-9
    return {
        "symbols": symbols, "months": months, "n": len(trades),
        "final": final, "ret": (final/100-1)*100,
        "ann": ((final/100)**(12/months)-1)*100 if months > 0 else 0,
        "dd": dd, "pf": gw/gl,
    }


all_pairs = list(SL5.keys())
print(f"\nTesting SL=5% combos pe {len(all_pairs)} perechi (FULL period + per-year)...")

# Run all combos on FULL period
print("\n[1/2] Running FULL period combos...")
single_full = []
two_full = []
three_full = []
for s in all_pairs:
    r = run_combo((s,))
    if r: single_full.append(r)
for combo in combinations(all_pairs, 2):
    r = run_combo(combo)
    if r and r["n"] >= 30: two_full.append(r)
single_full.sort(key=lambda x: -x["final"])
top7 = [r["symbols"][0] for r in single_full[:7]]
for combo in combinations(top7, 3):
    r = run_combo(combo)
    if r and r["n"] >= 30: three_full.append(r)
two_full.sort(key=lambda x: -x["final"])
three_full.sort(key=lambda x: -x["final"])

# Helper for annual breakdown
def annual_breakdown(symbols: tuple[str, ...]) -> dict:
    out = {}
    for plabel, pstart, pend in PERIODS[1:]:
        r = run_combo(symbols, pstart, pend)
        out[plabel] = r
    return out


# Print results — single
print("\n--- SINGLE (SL=5%) sortat dupa Final, cu breakdown anual ---")
print(f"{'Pair':<8}{'mo':<5}{'Final':<10}{'PF':<6}{'DD':<6}  "
      f"{'2024':<22}{'2025':<22}{'2026YTD':<22}{'min PF':<8}")
for r in single_full:
    sym = r["symbols"][0]
    yr = annual_breakdown((sym,))
    pfs = [y["pf"] for y in yr.values() if y is not None]
    min_pf = min(pfs) if pfs else 0
    print(f"  {sym.replace('USDT',''):<6}{r['months']:<5.1f}${r['final']:<9.0f}"
          f"{r['pf']:<6.2f}{r['dd']:<+5.0f}%", end="")
    for plabel in ["2024", "2025", "2026YTD"]:
        y = yr.get(plabel)
        if y is None:
            print(f"  {'—':<20}", end="")
        else:
            print(f"  PF{y['pf']:<4.2f} R{y['ret']:<+5.0f}%", end="")
    print(f"  {min_pf:<7.2f}")

# Print results — top 15 2-pair with annual
print("\n--- TOP 15 2-PAIR COMBOS sortat dupa Final, cu breakdown anual ---")
print(f"{'Combo':<22}{'mo':<5}{'Final':<11}{'PF':<6}{'DD':<6}  "
      f"{'2024':<22}{'2025':<22}{'2026YTD':<22}{'min PF':<8}")
for r in two_full[:15]:
    yr = annual_breakdown(r["symbols"])
    pfs = [y["pf"] for y in yr.values() if y is not None]
    min_pf = min(pfs) if pfs else 0
    pairs_str = "+".join(s.replace("USDT","") for s in r["symbols"])
    print(f"  {pairs_str:<20}{r['months']:<5.1f}${r['final']:<10.0f}"
          f"{r['pf']:<6.2f}{r['dd']:<+5.0f}%", end="")
    for plabel in ["2024", "2025", "2026YTD"]:
        y = yr.get(plabel)
        if y is None:
            print(f"  {'—':<20}", end="")
        else:
            print(f"  PF{y['pf']:<4.2f} R{y['ret']:<+5.0f}%", end="")
    print(f"  {min_pf:<7.2f}")

# Print results — top 10 3-pair with annual
print("\n--- TOP 10 3-PAIR COMBOS sortat dupa Final, cu breakdown anual ---")
print(f"{'Combo':<28}{'mo':<5}{'Final':<13}{'PF':<6}{'DD':<6}  "
      f"{'2024':<22}{'2025':<22}{'2026YTD':<22}{'min PF':<8}")
for r in three_full[:10]:
    yr = annual_breakdown(r["symbols"])
    pfs = [y["pf"] for y in yr.values() if y is not None]
    min_pf = min(pfs) if pfs else 0
    pairs_str = "+".join(s.replace("USDT","") for s in r["symbols"])
    print(f"  {pairs_str:<26}{r['months']:<5.1f}${r['final']:<12.0f}"
          f"{r['pf']:<6.2f}{r['dd']:<+5.0f}%", end="")
    for plabel in ["2024", "2025", "2026YTD"]:
        y = yr.get(plabel)
        if y is None:
            print(f"  {'—':<20}", end="")
        else:
            print(f"  PF{y['pf']:<4.2f} R{y['ret']:<+5.0f}%", end="")
    print(f"  {min_pf:<7.2f}")

# Save CSV
import csv
with open("/tmp/combo_sl5_alts_annual.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["pairs", "n_pairs", "months", "n_trades", "final", "ret", "ann", "pf", "dd",
                "pf_2024", "ret_2024", "dd_2024",
                "pf_2025", "ret_2025", "dd_2025",
                "pf_2026YTD", "ret_2026YTD", "dd_2026YTD",
                "min_pf"])
    for r in single_full + two_full + three_full:
        yr = annual_breakdown(r["symbols"])
        pfs = [y["pf"] for y in yr.values() if y is not None]
        row = ["+".join(s.replace("USDT","") for s in r["symbols"]),
               len(r["symbols"]), round(r["months"], 1), r["n"],
               round(r["final"], 0), round(r["ret"], 0), round(r["ann"], 0),
               round(r["pf"], 2), round(r["dd"], 0)]
        for plabel in ["2024", "2025", "2026YTD"]:
            y = yr.get(plabel)
            if y is None:
                row.extend(["", "", ""])
            else:
                row.extend([round(y["pf"], 2), round(y["ret"], 0), round(y["dd"], 0)])
        row.append(round(min(pfs), 2) if pfs else "")
        w.writerow(row)
print(f"\nCSV: /tmp/combo_sl5_alts_annual.csv")
