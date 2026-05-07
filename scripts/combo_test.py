"""Multi-pair backtest cu shared equity pentru combinatii 2-cate-2 din AXS, ILV, AERO.

Foloseste setarile optime per pereche (best PF din sweep individual).
"""

from __future__ import annotations

import os
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


# Best PF settings (from sweeps individuale)
PAIR_CONFIG = {
    "AXSUSDT":  {"hull": 10, "kj": 48, "snkb": 52, "sl": 0.10, "tp": None,
                 "step": 0.1, "start": "2023-10-01"},
    "ILVUSDT":  {"hull": 10, "kj": 36, "snkb": 40, "sl": 0.10, "tp": 0.20,
                 "step": 0.01, "start": "2023-10-01"},
    "AEROUSDT": {"hull": 10, "kj": 60, "snkb": 26, "sl": 0.04, "tp": 0.20,
                 "step": 0.1, "start": "2024-07-15"},
}


def make_pair(symbol: str) -> PairConfig:
    c = PAIR_CONFIG[symbol]
    return PairConfig(
        symbol=symbol, timeframe="4h", enabled=True,
        leverage=20, hull_length=c["hull"], tenkan_periods=9,
        kijun_periods=c["kj"], senkou_b_periods=c["snkb"], displacement=24,
        risk_pct_per_trade=0.07, sl_initial_pct=c["sl"], tp_pct=c["tp"],
        max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0,
    )


def run_combo(symbols: tuple[str, ...]) -> dict:
    """Multi-pair backtest cu shared equity pentru combo de perechi."""
    # Aggregate qty steps
    qty_steps = {s: PAIR_CONFIG[s]["step"] for s in symbols}
    # Start = max start (latest of the two)
    start = max(pd.Timestamp(PAIR_CONFIG[s]["start"], tz="UTC") for s in symbols)
    end = pd.Timestamp("2026-04-25", tz="UTC")
    months = (end - start).days / 30.4

    # Combine data into one dir
    combo_dir = Path(f"/tmp/combo_{'_'.join(s.replace('USDT','') for s in symbols)}_data")
    combo_dir.mkdir(exist_ok=True)
    for s in symbols:
        src = Path(f"/tmp/{s.replace('USDT','').lower()}_data/{s}_4h.parquet")
        dst = combo_dir / f"{s}_4h.parquet"
        if not dst.exists():
            shutil.copy(src, dst)

    cfg = AppConfig(
        portfolio=PortfolioConfig(name="combo", pool_total=100.0, leverage=15,
                                  cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0),
        pairs=[make_pair(s) for s in symbols],
        operational=OperationalConfig(max_concurrent_positions=2),  # both pairs simultan
    )

    result = bt.run_backtest(cfg, combo_dir, start, end, qty_steps,
                             entry_fee=0.000305, exit_fee=0.00055)
    trades = result["trades"]
    final = result["final_equity"]
    eq = [v for _, v in result["equity_curve"]]
    if not eq:
        return {"error": "no equity curve"}
    peaks = np.maximum.accumulate(eq)
    dd = float(((np.array(eq) - peaks) / peaks * 100).min())
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gw = sum(t.pnl_net for t in wins); gl = abs(sum(t.pnl_net for t in losses)) or 1e-9
    pf = gw / gl
    annualized = ((final / 100) ** (12 / months) - 1) * 100 if months > 0 else 0

    # Per-pair breakdown
    per_pair = {}
    for s in symbols:
        st = [t for t in trades if t.pair == s]
        if not st:
            per_pair[s] = {"n": 0, "pnl": 0}
            continue
        per_pair[s] = {
            "n": len(st),
            "wr": sum(1 for t in st if t.pnl_net > 0) / len(st) * 100,
            "pnl": sum(t.pnl_net for t in st),
            "sl": sum(1 for t in st if t.exit_reason == "SL"),
            "tp": sum(1 for t in st if t.exit_reason == "TP"),
            "sig": sum(1 for t in st if t.exit_reason == "SIGNAL"),
        }
    return {
        "symbols": symbols, "start": start, "end": end, "months": months,
        "n": len(trades), "final": final, "ret": (final / 100 - 1) * 100,
        "annualized": annualized, "dd": dd, "pf": pf,
        "wr": len(wins) / len(trades) * 100 if trades else 0,
        "per_pair": per_pair,
    }


# Run all 2-pair combos
combos = list(combinations(["AXSUSDT", "ILVUSDT", "AEROUSDT"], 2))
print("\n" + "="*100)
print(f"Multi-pair backtest cu SHARED EQUITY ($100 init, lev 20× per pair, fees 70/30 mix)")
print("="*100)

for combo in combos:
    r = run_combo(combo)
    if "error" in r:
        print(f"\n{combo}: ERROR — {r['error']}")
        continue
    pairs_str = " + ".join(s.replace("USDT", "") for s in combo)
    print(f"\n━━━ {pairs_str} ━━━ ({r['start'].date()} → {r['end'].date()}, {r['months']:.1f} luni)")
    print(f"  Initial $100  →  Final ${r['final']:,.2f}  (Ret {r['ret']:+.1f}%, Annualized {r['annualized']:+.1f}%/an)")
    print(f"  Trades: {r['n']}  |  WR: {r['wr']:.1f}%  |  PF: {r['pf']:.2f}  |  DD: {r['dd']:+.1f}%")
    print(f"  Per pair:")
    for s, pp in r["per_pair"].items():
        if pp["n"] == 0:
            print(f"    {s.replace('USDT',''):<6}: no trades")
            continue
        print(f"    {s.replace('USDT',''):<6}: n={pp['n']:<4}  WR={pp['wr']:<5.1f}%  PnL=${pp['pnl']:+.2f}  "
              f"(SL={pp['sl']}, TP={pp['tp']}, SIG={pp['sig']})")

# Also run single-pair baseline pentru fiecare cu setarile lor pe periodele lor pentru comparatie
print("\n" + "="*100)
print("BASELINE single-pair (start FRESH $100):")
print("="*100)
for sym in ["AXSUSDT", "ILVUSDT", "AEROUSDT"]:
    r = run_combo((sym,))
    if "error" in r:
        continue
    print(f"  {sym.replace('USDT',''):<6} {r['start'].date()} → {r['end'].date()} "
          f"({r['months']:.1f}mo): n={r['n']}  Final=${r['final']:.0f}  "
          f"Ret={r['ret']:+.0f}%  PF={r['pf']:.2f}  DD={r['dd']:+.0f}%")

# Triple combo bonus
print("\n━━━ TRIPLE: AXS + ILV + AERO ━━━")
r3 = run_combo(("AXSUSDT", "ILVUSDT", "AEROUSDT"))
if "error" not in r3:
    print(f"  ({r3['start'].date()} → {r3['end'].date()}, {r3['months']:.1f}mo)")
    print(f"  Initial $100  →  Final ${r3['final']:,.2f}  (Ret {r3['ret']:+.1f}%, Ann {r3['annualized']:+.1f}%/an)")
    print(f"  Trades: {r3['n']}  |  WR: {r3['wr']:.1f}%  |  PF: {r3['pf']:.2f}  |  DD: {r3['dd']:+.1f}%")
    for s, pp in r3["per_pair"].items():
        if pp["n"] == 0: continue
        print(f"    {s.replace('USDT',''):<6}: n={pp['n']:<4}  PnL=${pp['pnl']:+.2f}")
