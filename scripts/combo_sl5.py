"""Combo backtest cu SL=5% pe toate perechile (mid-ground between aggressive 3% si consistent 10%).

Pos size = $7 / 0.05 = $140 = 1.4× equity per trade
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


# SL=5% — TP-uri din best Return @ SL=5% per pereche
SL5 = {
    "MNTUSDT":  {"hull": 8,  "kj": 48, "snkb": 40, "sl": 0.05, "tp": None,
                 "step": 0.1, "start": "2023-10-01"},
    "AXSUSDT":  {"hull": 10, "kj": 48, "snkb": 26, "sl": 0.05, "tp": None,
                 "step": 0.1, "start": "2023-10-01"},
    "ILVUSDT":  {"hull": 10, "kj": 36, "snkb": 40, "sl": 0.05, "tp": 0.20,
                 "step": 0.01, "start": "2023-10-01"},
    "AEROUSDT": {"hull": 10, "kj": 60, "snkb": 26, "sl": 0.05, "tp": 0.20,
                 "step": 0.1, "start": "2024-07-15"},
}


def make_pair(symbol: str, c: dict) -> PairConfig:
    return PairConfig(
        symbol=symbol, timeframe="4h", enabled=True,
        leverage=20, hull_length=c["hull"], tenkan_periods=9,
        kijun_periods=c["kj"], senkou_b_periods=c["snkb"], displacement=24,
        risk_pct_per_trade=0.07, sl_initial_pct=c["sl"], tp_pct=c["tp"],
        max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0,
    )


def run_combo(symbols: tuple[str, ...]) -> dict:
    qty_steps = {s: SL5[s]["step"] for s in symbols}
    start = max(pd.Timestamp(SL5[s]["start"], tz="UTC") for s in symbols)
    end = pd.Timestamp("2026-04-25", tz="UTC")
    months = (end - start).days / 30.4

    combo_dir = Path(f"/tmp/sl5_{'_'.join(s.replace('USDT','') for s in symbols)}")
    combo_dir.mkdir(exist_ok=True)
    for s in symbols:
        src = Path(f"/tmp/{s.replace('USDT','').lower()}_data/{s}_4h.parquet")
        dst = combo_dir / f"{s}_4h.parquet"
        if not dst.exists() and src.exists():
            shutil.copy(src, dst)

    cfg = AppConfig(
        portfolio=PortfolioConfig(name="sl5", pool_total=100.0, leverage=15,
                                  cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0),
        pairs=[make_pair(s, SL5[s]) for s in symbols],
        operational=OperationalConfig(max_concurrent_positions=len(symbols)),
    )

    result = bt.run_backtest(cfg, combo_dir, start, end, qty_steps,
                             entry_fee=0.000305, exit_fee=0.00055)
    trades = result["trades"]
    final = result["final_equity"]
    eq = [v for _, v in result["equity_curve"]]
    if not eq:
        return {"error": "no equity"}
    peaks = np.maximum.accumulate(eq)
    dd = float(((np.array(eq) - peaks) / peaks * 100).min())
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gw = sum(t.pnl_net for t in wins); gl = abs(sum(t.pnl_net for t in losses)) or 1e-9
    pf = gw / gl
    annualized = ((final / 100) ** (12 / months) - 1) * 100 if months > 0 else 0
    per_pair = {s: {"n": sum(1 for t in trades if t.pair == s),
                    "pnl": sum(t.pnl_net for t in trades if t.pair == s)}
                for s in symbols}
    return {
        "symbols": symbols, "start": start, "end": end, "months": months,
        "n": len(trades), "final": final, "ret": (final/100-1)*100,
        "annualized": annualized, "dd": dd, "pf": pf,
        "wr": len(wins)/len(trades)*100 if trades else 0,
        "per_pair": per_pair,
    }


print("\n" + "="*100)
print("SL=5% COMBOS (mid-ground: aggressive 3% vs consistent 10%)")
print("Settings: MNT(noTP), AXS(noTP), ILV(TP=20%), AERO(TP=20%)")
print("Position size: $7 / 0.05 = $140 = 1.4× equity per trade")
print("="*100)

all_pairs = ("MNTUSDT", "AXSUSDT", "ILVUSDT", "AEROUSDT")

print("\n--- BASELINE single-pair ---")
for s in all_pairs:
    r = run_combo((s,))
    if "error" in r: continue
    print(f"  {s.replace('USDT',''):<6} ({r['months']:.1f}mo): n={r['n']:<4}  Final=${r['final']:>10.2f}  "
          f"Ret={r['ret']:+9.1f}%  PF={r['pf']:.2f}  DD={r['dd']:+5.1f}%")

print("\n--- 2-PAIR ---")
for combo in combinations(all_pairs, 2):
    r = run_combo(combo)
    if "error" in r: continue
    pairs_str = " + ".join(s.replace("USDT", "") for s in combo)
    print(f"\n  {pairs_str}  ({r['months']:.1f}mo)")
    print(f"    $100 → ${r['final']:>11.2f}  Ret {r['ret']:+9.1f}%  Ann {r['annualized']:+8.1f}%/an"
          f"  PF {r['pf']:.2f}  DD {r['dd']:+5.1f}%  n={r['n']}")

print("\n--- 3-PAIR ---")
for combo in combinations(all_pairs, 3):
    r = run_combo(combo)
    if "error" in r: continue
    pairs_str = " + ".join(s.replace("USDT", "") for s in combo)
    print(f"\n  {pairs_str}  ({r['months']:.1f}mo)")
    print(f"    $100 → ${r['final']:>11.2f}  Ret {r['ret']:+9.1f}%  Ann {r['annualized']:+8.1f}%/an"
          f"  PF {r['pf']:.2f}  DD {r['dd']:+5.1f}%  n={r['n']}")
    for s, pp in r["per_pair"].items():
        print(f"      {s.replace('USDT',''):<6}: n={pp['n']:<4}  PnL=${pp['pnl']:+11.2f}")

print("\n--- 4-PAIR (all) ---")
r = run_combo(all_pairs)
if "error" not in r:
    print(f"\n  MNT + AXS + ILV + AERO  ({r['months']:.1f}mo)")
    print(f"    $100 → ${r['final']:>11.2f}  Ret {r['ret']:+9.1f}%  Ann {r['annualized']:+8.1f}%/an"
          f"  PF {r['pf']:.2f}  DD {r['dd']:+5.1f}%  n={r['n']}")
    for s, pp in r["per_pair"].items():
        print(f"      {s.replace('USDT',''):<6}: n={pp['n']:<4}  PnL=${pp['pnl']:+11.2f}")
