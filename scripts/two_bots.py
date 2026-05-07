"""2 boti × 3 perechi disjunkte cu SL=5%.

Selectez splits din top 8 alts: SUN, MNT, ILV, RSR, XCN, AXS, AERO, ATOM.
Fiecare bot ruleaza independent ($100 init, shared equity intra-bot).
Total PnL = Bot1 + Bot2.
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


SL5 = {
    "MNTUSDT":  {"hull": 8,  "kj": 48, "snkb": 40, "sl": 0.05, "tp": None,  "step": 0.1,  "start": "2023-10-01"},
    "AXSUSDT":  {"hull": 10, "kj": 48, "snkb": 26, "sl": 0.05, "tp": None,  "step": 0.1,  "start": "2023-10-01"},
    "ILVUSDT":  {"hull": 10, "kj": 36, "snkb": 40, "sl": 0.05, "tp": 0.20,  "step": 0.01, "start": "2023-10-01"},
    "AEROUSDT": {"hull": 10, "kj": 60, "snkb": 26, "sl": 0.05, "tp": 0.20,  "step": 0.1,  "start": "2024-07-15"},
    "SUNUSDT":  {"hull": 8,  "kj": 24, "snkb": 52, "sl": 0.05, "tp": None,  "step": 10.0, "start": "2023-10-01"},
    "XCNUSDT":  {"hull": 12, "kj": 60, "snkb": 40, "sl": 0.05, "tp": None,  "step": 10.0, "start": "2023-10-01"},
    "RSRUSDT":  {"hull": 10, "kj": 60, "snkb": 52, "sl": 0.05, "tp": 0.05,  "step": 10.0, "start": "2023-10-01"},
    "ATOMUSDT": {"hull": 12, "kj": 60, "snkb": 26, "sl": 0.05, "tp": 0.20,  "step": 0.1,  "start": "2023-10-01"},
}


def make_pair(symbol: str, c: dict) -> PairConfig:
    return PairConfig(
        symbol=symbol, timeframe="4h", enabled=True,
        leverage=20, hull_length=c["hull"], tenkan_periods=9,
        kijun_periods=c["kj"], senkou_b_periods=c["snkb"], displacement=24,
        risk_pct_per_trade=0.07, sl_initial_pct=c["sl"], tp_pct=c["tp"],
        max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0,
    )


def run_bot(symbols: tuple[str, ...], start_str: str | None = None,
            end_str: str | None = None) -> dict | None:
    qty_steps = {s: SL5[s]["step"] for s in symbols}
    pair_starts = [pd.Timestamp(SL5[s]["start"], tz="UTC") for s in symbols]
    start = max(pair_starts) if start_str is None else max(max(pair_starts), pd.Timestamp(start_str, tz="UTC"))
    end = pd.Timestamp("2026-04-25", tz="UTC") if end_str is None else pd.Timestamp(end_str, tz="UTC")
    months = (end - start).days / 30.4
    if months < 2: return None

    combo_id = "_".join(sorted(s.replace("USDT","") for s in symbols))
    combo_dir = Path(f"/tmp/bot_{combo_id}")
    combo_dir.mkdir(exist_ok=True)
    for s in symbols:
        src = Path(f"/tmp/{s.replace('USDT','').lower()}_data/{s}_4h.parquet")
        dst = combo_dir / f"{s}_4h.parquet"
        if not dst.exists() and src.exists(): shutil.copy(src, dst)

    cfg = AppConfig(
        portfolio=PortfolioConfig(name="bot", pool_total=100.0, leverage=15,
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
    if not eq or len(trades) < 5: return None
    peaks = np.maximum.accumulate(eq)
    dd = float(((np.array(eq) - peaks) / peaks * 100).min())
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gw = sum(t.pnl_net for t in wins); gl = abs(sum(t.pnl_net for t in losses)) or 1e-9
    return {"symbols": symbols, "months": months, "n": len(trades),
            "final": final, "ret": (final/100-1)*100, "dd": dd, "pf": gw/gl,
            "ann": ((final/100)**(12/months)-1)*100 if months > 0 else 0}


# Possible 2-bot splits — focus on COMPLEMENTARY (different cycles)
SPLITS = [
    # Format: (label, bot1_pairs, bot2_pairs)
    ("A: PnL-max",
     ("SUNUSDT", "MNTUSDT", "ILVUSDT"),
     ("AXSUSDT", "RSRUSDT", "XCNUSDT")),
    ("B: Consistency-max",
     ("SUNUSDT", "MNTUSDT", "RSRUSDT"),
     ("ILVUSDT", "AXSUSDT", "ATOMUSDT")),
    ("C: PnL+Consistency mix",
     ("SUNUSDT", "MNTUSDT", "ILVUSDT"),
     ("AERO", "RSRUSDT", "AXSUSDT")),  # AERO va fi USDT-suffixed
    ("D: Diversificat cycles",
     ("SUNUSDT", "ILVUSDT", "RSRUSDT"),
     ("MNTUSDT", "AERO", "ATOMUSDT")),
    ("E: Best 2 + Best 2",
     ("MNTUSDT", "SUNUSDT", "AERO"),
     ("ILVUSDT", "RSRUSDT", "XCNUSDT")),
    ("F: 30mo + 30mo (max sample)",
     ("SUNUSDT", "MNTUSDT", "ILVUSDT"),
     ("RSRUSDT", "XCNUSDT", "ATOMUSDT")),
]

# Normalize AERO suffix
SPLITS = [(lbl, tuple(s if s.endswith("USDT") else s+"USDT" for s in b1),
           tuple(s if s.endswith("USDT") else s+"USDT" for s in b2))
          for lbl, b1, b2 in SPLITS]


PERIODS = [
    ("FULL",   None,         None),
    ("2024",   "2024-01-01", "2025-01-01"),
    ("2025",   "2025-01-01", "2026-01-01"),
    ("2026YTD","2026-01-01", "2026-04-25"),
]

print(f"\n{'='*120}")
print("2-BOT SPLITS (each bot $100 init, independent shared equity, lev 20×)")
print(f"{'='*120}")

for label, bot1, bot2 in SPLITS:
    print(f"\n━━━ {label} ━━━")
    print(f"  Bot 1: {' + '.join(s.replace('USDT','') for s in bot1)}")
    print(f"  Bot 2: {' + '.join(s.replace('USDT','') for s in bot2)}")

    # Full period
    r1 = run_bot(bot1)
    r2 = run_bot(bot2)
    if r1 is None or r2 is None:
        print(f"  ERROR running combo")
        continue
    total = r1["final"] + r2["final"]
    print(f"\n  FULL period:")
    print(f"    Bot 1 ({r1['months']:.1f}mo): $100 → ${r1['final']:>10.2f}  PF {r1['pf']:.2f}  DD {r1['dd']:+.0f}%  n={r1['n']}")
    print(f"    Bot 2 ({r2['months']:.1f}mo): $100 → ${r2['final']:>10.2f}  PF {r2['pf']:.2f}  DD {r2['dd']:+.0f}%  n={r2['n']}")
    print(f"    TOTAL: $200 → ${total:>10.2f}  ({(total/200-1)*100:+.0f}%)")

    # Per year
    print(f"  Per year (start fresh $100/bot):")
    for plabel, pstart, pend in PERIODS[1:]:
        y1 = run_bot(bot1, pstart, pend)
        y2 = run_bot(bot2, pstart, pend)
        if y1 and y2:
            yt = y1["final"] + y2["final"]
            print(f"    {plabel:<8}: Bot1 ${y1['final']:.0f} (PF{y1['pf']:.2f} DD{y1['dd']:+.0f}%)  +  "
                  f"Bot2 ${y2['final']:.0f} (PF{y2['pf']:.2f} DD{y2['dd']:+.0f}%)  =  ${yt:.0f} "
                  f"({(yt/200-1)*100:+.0f}%)")
