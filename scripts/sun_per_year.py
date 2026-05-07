"""SUN PnL pe fiecare an separat (start fresh $100 pe each year).

Compara 3 variante SL: Aggressive 3%, Balanced 5%, Conservative 10%.
"""

from __future__ import annotations

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


def make_cfg(sl: float, tp: float | None = None,
             hull: int = 8, kj: int = 24, snkb: int = 52) -> AppConfig:
    pair = PairConfig(symbol="SUNUSDT", timeframe="4h", enabled=True,
                      leverage=20, hull_length=hull, tenkan_periods=9,
                      kijun_periods=kj, senkou_b_periods=snkb, displacement=24,
                      risk_pct_per_trade=0.07, sl_initial_pct=sl, tp_pct=tp,
                      max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0)
    return AppConfig(
        portfolio=PortfolioConfig(name="sun", pool_total=100.0, leverage=15,
                                  cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0),
        pairs=[pair],
        operational=OperationalConfig(max_concurrent_positions=1),
    )


def run(start: str, end: str, sl: float, tp: float | None) -> dict:
    cfg = make_cfg(sl, tp)
    r = bt.run_backtest(cfg, Path("/tmp/sun_data"),
                        pd.Timestamp(start, tz="UTC"),
                        pd.Timestamp(end, tz="UTC"),
                        qty_steps={"SUNUSDT": 10.0},
                        entry_fee=0.000305, exit_fee=0.00055)
    trades = r["trades"]
    final = r["final_equity"]
    eq = [v for _, v in r["equity_curve"]]
    if not eq:
        return {"n": 0, "wr": 0, "pf": 0, "ret": 0, "dd": 0, "final": final}
    peaks = np.maximum.accumulate(eq)
    dd = float(((np.array(eq) - peaks) / peaks * 100).min())
    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gw = sum(t.pnl_net for t in wins); gl = abs(sum(t.pnl_net for t in losses)) or 1e-9
    return {
        "n": len(trades),
        "wr": len(wins) / len(trades) * 100 if trades else 0,
        "pf": gw / gl,
        "ret": (final / 100 - 1) * 100,
        "dd": dd,
        "final": final,
    }


periods = [
    ("2023 Q4 (Oct-Dec)",  "2023-10-01", "2024-01-01"),
    ("2024 (full year)",   "2024-01-01", "2025-01-01"),
    ("2025 (full year)",   "2025-01-01", "2026-01-01"),
    ("2026 YTD (Jan-Apr)", "2026-01-01", "2026-04-25"),
    ("FULL (Oct23 - Apr26)", "2023-10-01", "2026-04-25"),
]
variants = [
    ("Aggressive   (SL=3%, no TP)",  0.03, None),
    ("Balanced     (SL=5%, no TP)",  0.05, None),
    ("Conservative (SL=10%, no TP)", 0.10, None),
]

print(f"\nSUNUSDT 4h — Hull=8, Kj=24, SnkB=52, lev=20×, risk=7%")
print(f"Start FRESH $100 la fiecare perioada (nu compound intre ani)")
print(f"Fees: entry 0.0305% (70/30 mix), exit 0.055% (taker)\n")

for label, sl, tp in variants:
    print(f"━━━ {label} ━━━")
    print(f"{'Period':<24}{'n':<5}{'WR':<7}{'PF':<7}{'Return':<10}{'PnL':<11}{'DD':<8}")
    print("─" * 72)
    for plabel, start, end in periods:
        r = run(start, end, sl, tp)
        print(f"{plabel:<24}{r['n']:<5}{r['wr']:<6.1f}%{r['pf']:<7.2f}"
              f"{r['ret']:<+9.1f}%${r['final']-100:<+9.2f}{r['dd']:<+7.1f}%")
    print()
