"""2 boti diferiti:
  Bot 1 AGGRESSIVE — SUN + MNT + ILV @ SL=3% (max PnL, DD ~-45%)
  Bot 2 CONSISTENT — AERO + AKT + AXS @ SL=8-10% (no bad years, DD ~-20%)

Fiecare bot $100 init, independent. Total $200 capital.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ichimoku_bot.config import AppConfig, PairConfig, PortfolioConfig, OperationalConfig

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("bm", str(ROOT / "scripts" / "backtest.py"))
bt = _ilu.module_from_spec(_spec); sys.modules["bm"] = bt; _spec.loader.exec_module(bt)


# Bot 1 AGGRESSIVE (SL=3% — max PnL)
BOT1 = {
    "SUNUSDT": {"hull": 8, "kj": 24, "snkb": 52, "sl": 0.03, "tp": None, "step": 10.0, "start": "2023-10-01"},
    "MNTUSDT": {"hull": 8, "kj": 48, "snkb": 40, "sl": 0.03, "tp": None, "step": 0.1,  "start": "2023-10-01"},
    "ILVUSDT": {"hull": 8, "kj": 36, "snkb": 40, "sl": 0.03, "tp": 0.12, "step": 0.01, "start": "2023-10-01"},
}

# Bot 2 CONSISTENT (SL=8-10% — no bad years)
BOT2 = {
    "AEROUSDT": {"hull": 10, "kj": 60, "snkb": 26, "sl": 0.10, "tp": 0.20, "step": 0.1, "start": "2024-07-15"},
    "AKTUSDT":  {"hull": 8,  "kj": 60, "snkb": 52, "sl": 0.10, "tp": 0.08, "step": 1.0, "start": "2024-06-26"},
    "AXSUSDT":  {"hull": 10, "kj": 48, "snkb": 52, "sl": 0.08, "tp": None, "step": 0.1, "start": "2023-10-01"},
}


def make_pair(symbol: str, c: dict) -> PairConfig:
    return PairConfig(
        symbol=symbol, timeframe="4h", enabled=True,
        leverage=20, hull_length=c["hull"], tenkan_periods=9,
        kijun_periods=c["kj"], senkou_b_periods=c["snkb"], displacement=24,
        risk_pct_per_trade=0.07, sl_initial_pct=c["sl"], tp_pct=c["tp"],
        max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0,
    )


def run_bot(bot_cfg: dict, label: str, start_str: str | None = None,
            end_str: str | None = None) -> dict | None:
    symbols = tuple(bot_cfg.keys())
    qty_steps = {s: bot_cfg[s]["step"] for s in symbols}
    pair_starts = [pd.Timestamp(bot_cfg[s]["start"], tz="UTC") for s in symbols]
    start = max(pair_starts) if start_str is None else max(max(pair_starts), pd.Timestamp(start_str, tz="UTC"))
    end = pd.Timestamp("2026-04-25", tz="UTC") if end_str is None else pd.Timestamp(end_str, tz="UTC")
    months = (end - start).days / 30.4
    if months < 1: return None

    combo_dir = Path(f"/tmp/{label}_dir")
    combo_dir.mkdir(exist_ok=True)
    for s in symbols:
        src = Path(f"/tmp/{s.replace('USDT','').lower()}_data/{s}_4h.parquet")
        dst = combo_dir / f"{s}_4h.parquet"
        if not dst.exists() and src.exists(): shutil.copy(src, dst)

    cfg = AppConfig(
        portfolio=PortfolioConfig(name=label, pool_total=100.0, leverage=15,
                                  cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0),
        pairs=[make_pair(s, bot_cfg[s]) for s in symbols],
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
    if not eq or len(trades) < 5: return None
    peaks = np.maximum.accumulate(eq)
    dd = float(((np.array(eq) - peaks) / peaks * 100).min())
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gw = sum(t.pnl_net for t in wins); gl = abs(sum(t.pnl_net for t in losses)) or 1e-9
    per_pair = {s: {"n": sum(1 for t in trades if t.pair == s),
                    "pnl": sum(t.pnl_net for t in trades if t.pair == s)}
                for s in symbols}
    return {"months": months, "n": len(trades), "final": final,
            "ret": (final/100-1)*100, "dd": dd, "pf": gw/gl,
            "wr": len(wins)/len(trades)*100,
            "ann": ((final/100)**(12/months)-1)*100 if months > 0 else 0,
            "per_pair": per_pair}


PERIODS = [("2024", "2024-01-01", "2025-01-01"),
           ("2025", "2025-01-01", "2026-01-01"),
           ("2026YTD", "2026-01-01", "2026-04-25")]

print(f"\n{'='*100}")
print("2 BOTI — AGGRESSIVE vs CONSISTENT")
print(f"{'='*100}")
print(f"\nBot 1 AGGRESSIVE (SL=3%, pos 2.33× equity):")
print(f"  SUN (8/24/52, no TP)  +  MNT (8/48/40, no TP)  +  ILV (8/36/40, TP=12%)")
print(f"\nBot 2 CONSISTENT (SL=8-10%, pos 0.7-0.9× equity):")
print(f"  AERO (10/60/26, SL=10%, TP=20%)  +  AKT (8/60/52, SL=10%, TP=8%)  +  AXS (10/48/52, SL=8%, no TP)")

# FULL period
r1 = run_bot(BOT1, "bot1_aggr")
r2 = run_bot(BOT2, "bot2_cons")

print(f"\n{'─'*100}")
print(f"FULL period results")
print(f"{'─'*100}")
if r1:
    print(f"\nBot 1 AGGRESSIVE  ({r1['months']:.1f} luni):")
    print(f"  $100 → ${r1['final']:>11,.2f}  Ret {r1['ret']:+,.0f}%  Ann {r1['ann']:+,.0f}%/an")
    print(f"  PF {r1['pf']:.2f}  DD {r1['dd']:+.0f}%  WR {r1['wr']:.1f}%  n={r1['n']}")
    for s, pp in r1["per_pair"].items():
        print(f"    {s.replace('USDT',''):<6}: n={pp['n']:<4}  PnL=${pp['pnl']:+13,.2f}")

if r2:
    print(f"\nBot 2 CONSISTENT  ({r2['months']:.1f} luni):")
    print(f"  $100 → ${r2['final']:>11,.2f}  Ret {r2['ret']:+,.0f}%  Ann {r2['ann']:+,.0f}%/an")
    print(f"  PF {r2['pf']:.2f}  DD {r2['dd']:+.0f}%  WR {r2['wr']:.1f}%  n={r2['n']}")
    for s, pp in r2["per_pair"].items():
        print(f"    {s.replace('USDT',''):<6}: n={pp['n']:<4}  PnL=${pp['pnl']:+13,.2f}")

if r1 and r2:
    total = r1["final"] + r2["final"]
    print(f"\n  TOTAL ($200 init): ${total:>11,.2f}  ({(total/200-1)*100:+,.0f}%)")

# Per year
print(f"\n{'─'*100}")
print(f"Per-year breakdown (start FRESH $100/bot la fiecare an)")
print(f"{'─'*100}")
print(f"  {'Year':<8}{'Bot1 AGGR':<32}{'Bot2 CONS':<32}{'Total $200':<20}")
for plabel, pstart, pend in PERIODS:
    y1 = run_bot(BOT1, "bot1_y", pstart, pend)
    y2 = run_bot(BOT2, "bot2_y", pstart, pend)
    if y1 and y2:
        yt = y1["final"] + y2["final"]
        print(f"  {plabel:<8}${y1['final']:<7.0f} PF{y1['pf']:.2f} DD{y1['dd']:+.0f}% n={y1['n']:<4}    "
              f"${y2['final']:<7.0f} PF{y2['pf']:.2f} DD{y2['dd']:+.0f}% n={y2['n']:<4}    "
              f"${yt:<8.0f} ({(yt/200-1)*100:+.0f}%)")
