"""Pentru fiecare pereche, ruleaza setarea optima pe FIECARE AN separat
si masoara consistenta (variabilitate year-over-year a PF + Return).

Top pairs sortate dupa min(PF) across years (cel mai consistent).
"""

from __future__ import annotations

import sys
from pathlib import Path
from statistics import mean, stdev
from typing import Optional

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ichimoku_bot.config import AppConfig, PairConfig, PortfolioConfig, OperationalConfig

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("bm", str(ROOT / "scripts" / "backtest.py"))
bt = _ilu.module_from_spec(_spec); sys.modules["bm"] = bt; _spec.loader.exec_module(bt)


# Best PF combo identificat din sweep full pentru fiecare pereche
PAIR_CONFIG: dict[str, dict] = {
    "SUN":      {"hull": 8,  "kj": 24, "snkb": 52, "sl": 0.10, "tp": None,  "step": 10.0},
    "XCN":      {"hull": 12, "kj": 60, "snkb": 40, "sl": 0.10, "tp": None,  "step": 10.0},
    "RSR":      {"hull": 10, "kj": 60, "snkb": 52, "sl": 0.04, "tp": 0.05,  "step": 10.0},
    "ILV":      {"hull": 10, "kj": 36, "snkb": 40, "sl": 0.10, "tp": 0.20,  "step": 0.01},
    "ATOM":     {"hull": 12, "kj": 60, "snkb": 26, "sl": 0.08, "tp": 0.20,  "step": 0.1},
    "AKT":      {"hull": 8,  "kj": 60, "snkb": 52, "sl": 0.10, "tp": 0.08,  "step": 1.0},
    "AERO":     {"hull": 10, "kj": 60, "snkb": 26, "sl": 0.04, "tp": 0.20,  "step": 0.1},
    "AXS":      {"hull": 10, "kj": 48, "snkb": 52, "sl": 0.10, "tp": None,  "step": 0.1},
    "STG":      {"hull": 16, "kj": 60, "snkb": 26, "sl": 0.05, "tp": 0.15,  "step": 1.0},
    "SKL":      {"hull": 16, "kj": 48, "snkb": 40, "sl": 0.10, "tp": None,  "step": 1.0},
    "DYDX":     {"hull": 10, "kj": 60, "snkb": 52, "sl": 0.06, "tp": 0.20,  "step": 0.1},
    "XRP":      {"hull": 8,  "kj": 24, "snkb": 40, "sl": 0.08, "tp": None,  "step": 0.1},
    "YGG":      {"hull": 16, "kj": 48, "snkb": 52, "sl": 0.10, "tp": 0.20,  "step": 0.1},
    "MNT":      {"hull": 8,  "kj": 48, "snkb": 40, "sl": 0.03, "tp": None,  "step": 0.1},
    "POPCAT":   {"hull": 16, "kj": 60, "snkb": 52, "sl": 0.10, "tp": 0.20,  "step": 0.1},
    "PEAQ":    {"hull": 16, "kj": 48, "snkb": 52, "sl": 0.08, "tp": 0.12,  "step": 1.0},
}

PERIODS = [
    ("2023Q4", "2023-10-01", "2024-01-01"),
    ("2024",   "2024-01-01", "2025-01-01"),
    ("2025",   "2025-01-01", "2026-01-01"),
    ("2026YTD","2026-01-01", "2026-04-25"),
]


def make_cfg(symbol: str, c: dict) -> AppConfig:
    pair = PairConfig(symbol=symbol + "USDT", timeframe="4h", enabled=True,
                      leverage=20, hull_length=c["hull"], tenkan_periods=9,
                      kijun_periods=c["kj"], senkou_b_periods=c["snkb"], displacement=24,
                      risk_pct_per_trade=0.07, sl_initial_pct=c["sl"], tp_pct=c["tp"],
                      max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0)
    return AppConfig(
        portfolio=PortfolioConfig(name="cons", pool_total=100.0, leverage=15,
                                  cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0),
        pairs=[pair],
        operational=OperationalConfig(max_concurrent_positions=1),
    )


def run_period(symbol: str, c: dict, start: str, end: str) -> Optional[dict]:
    data_dir = Path(f"/tmp/{symbol.lower()}_data")
    if not data_dir.exists():
        return None
    cfg = make_cfg(symbol, c)
    try:
        r = bt.run_backtest(cfg, data_dir,
                            pd.Timestamp(start, tz="UTC"),
                            pd.Timestamp(end, tz="UTC"),
                            qty_steps={f"{symbol}USDT": c["step"]},
                            entry_fee=0.000305, exit_fee=0.00055)
    except Exception as e:
        return None
    trades = r["trades"]
    final = r["final_equity"]
    if not trades or len(trades) < 5:
        return None
    eq = [v for _, v in r["equity_curve"]]
    peaks = np.maximum.accumulate(eq)
    dd = float(((np.array(eq) - peaks) / peaks * 100).min())
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gw = sum(t.pnl_net for t in wins); gl = abs(sum(t.pnl_net for t in losses)) or 1e-9
    return {
        "n": len(trades),
        "wr": len(wins) / len(trades) * 100,
        "pf": gw / gl,
        "ret": (final / 100 - 1) * 100,
        "dd": dd,
    }


# Rulam pentru fiecare pereche pe fiecare perioada
print("\nConsistenta pe perioade (start FRESH $100 la fiecare perioada)")
print(f"{'Pair':<8}", end="")
for plabel, _, _ in PERIODS:
    print(f"{plabel:<26}", end="")
print(f"{'min PF':<9}{'avg PF':<9}{'std PF':<9}")
print("─" * 145)

all_results = {}
for sym, cfg in PAIR_CONFIG.items():
    row = {}
    for plabel, start, end in PERIODS:
        row[plabel] = run_period(sym, cfg, start, end)
    pfs = [r["pf"] for r in row.values() if r is not None]
    if not pfs:
        continue
    rets = [r["ret"] for r in row.values() if r is not None]
    all_results[sym] = {
        "row": row,
        "min_pf": min(pfs),
        "avg_pf": mean(pfs),
        "std_pf": stdev(pfs) if len(pfs) > 1 else 0,
        "min_ret": min(rets),
        "n_periods": len(pfs),
    }

# Sortare dupa min(PF) descrescator (cel mai consistent = cel mai bun PF in cel mai prost an)
sorted_pairs = sorted(all_results.items(), key=lambda x: -x[1]["min_pf"])

for sym, data in sorted_pairs:
    row = data["row"]
    print(f"{sym:<8}", end="")
    for plabel, _, _ in PERIODS:
        r = row.get(plabel)
        if r is None:
            print(f"{'  —':<26}", end="")
        else:
            cell = f"PF{r['pf']:<5.2f} R{r['ret']:<+6.0f}% DD{r['dd']:<+5.0f}%"
            print(f"{cell:<26}", end="")
    print(f"{data['min_pf']:<9.2f}{data['avg_pf']:<9.2f}{data['std_pf']:<9.2f}")

print("\n--- Top consistenta (sortat dupa min PF in worst year) ---")
for i, (sym, data) in enumerate(sorted_pairs[:10], 1):
    print(f"  {i:>2}. {sym:<8}  min_PF={data['min_pf']:.2f}  avg_PF={data['avg_pf']:.2f}  "
          f"std={data['std_pf']:.2f}  worst_Ret={data['min_ret']:+.1f}%  ({data['n_periods']} periods)")
