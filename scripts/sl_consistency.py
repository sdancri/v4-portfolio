"""Per pereche: sweep SL pe fiecare an separat.

Scop: gasi SL-ul cu cel mai bun PF MINIM across years (consistent).
Folosesc indicators + TP din best-PF combo full-period, doar SL variaza.
"""

from __future__ import annotations

import sys
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


PAIRS = {
    "SUN":  {"hull": 8,  "kj": 24, "snkb": 52, "tp": None,  "step": 10.0},
    "XCN":  {"hull": 12, "kj": 60, "snkb": 40, "tp": None,  "step": 10.0},
    "ILV":  {"hull": 10, "kj": 36, "snkb": 40, "tp": 0.20,  "step": 0.01},
    "AXS":  {"hull": 10, "kj": 48, "snkb": 52, "tp": None,  "step": 0.1},
    "AERO": {"hull": 10, "kj": 60, "snkb": 26, "tp": 0.20,  "step": 0.1},
    "AKT":  {"hull": 8,  "kj": 60, "snkb": 52, "tp": 0.08,  "step": 1.0},
    "ATOM": {"hull": 12, "kj": 60, "snkb": 26, "tp": 0.20,  "step": 0.1},
    "MNT":  {"hull": 8,  "kj": 48, "snkb": 40, "tp": None,  "step": 0.1},
}
SLS = [0.03, 0.04, 0.05, 0.06, 0.08, 0.10]
PERIODS = [
    ("2023Q4", "2023-10-01", "2024-01-01"),
    ("2024",   "2024-01-01", "2025-01-01"),
    ("2025",   "2025-01-01", "2026-01-01"),
    ("2026YTD","2026-01-01", "2026-04-25"),
]


def make_cfg(symbol: str, sl: float, c: dict) -> AppConfig:
    pair = PairConfig(
        symbol=symbol + "USDT", timeframe="4h", enabled=True,
        leverage=20, hull_length=c["hull"], tenkan_periods=9,
        kijun_periods=c["kj"], senkou_b_periods=c["snkb"], displacement=24,
        risk_pct_per_trade=0.07, sl_initial_pct=sl, tp_pct=c["tp"],
        max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0,
    )
    return AppConfig(
        portfolio=PortfolioConfig(name="x", pool_total=100.0, leverage=15,
                                  cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0),
        pairs=[pair],
        operational=OperationalConfig(max_concurrent_positions=1),
    )


def run_period(symbol: str, sl: float, c: dict, start: str, end: str) -> dict | None:
    data_dir = Path(f"/tmp/{symbol.lower()}_data")
    if not data_dir.exists():
        return None
    cfg = make_cfg(symbol, sl, c)
    try:
        r = bt.run_backtest(cfg, data_dir,
                            pd.Timestamp(start, tz="UTC"),
                            pd.Timestamp(end, tz="UTC"),
                            qty_steps={f"{symbol}USDT": c["step"]},
                            entry_fee=0.000305, exit_fee=0.00055)
    except Exception:
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
    return {"n": len(trades), "pf": gw/gl, "ret": (final/100-1)*100, "dd": dd}


for sym, c in PAIRS.items():
    print(f"\n━━━ {sym} ━━━ (Hull={c['hull']}, Kj={c['kj']}, SnkB={c['snkb']}, "
          f"TP={c['tp']*100 if c['tp'] else 'none'}{'%' if c['tp'] else ''})")
    print(f"{'SL':<6}", end="")
    for plabel, _, _ in PERIODS:
        print(f"{plabel:<22}", end="")
    print(f"{'min PF':<8}{'avg PF':<8}{'std':<7}{'min Ret':<9}")
    print("─" * 130)

    sl_results = []
    for sl in SLS:
        row = {plabel: run_period(sym, sl, c, start, end)
               for plabel, start, end in PERIODS}
        pfs = [r["pf"] for r in row.values() if r is not None]
        rets = [r["ret"] for r in row.values() if r is not None]
        if not pfs:
            continue
        mn_pf = min(pfs); avg_pf = mean(pfs); std_pf = stdev(pfs) if len(pfs)>1 else 0
        mn_ret = min(rets)
        sl_results.append({"sl": sl, "row": row, "min_pf": mn_pf, "avg_pf": avg_pf,
                          "std": std_pf, "min_ret": mn_ret})
        print(f"{sl*100:<5.0f}%", end="")
        for plabel, _, _ in PERIODS:
            r = row.get(plabel)
            if r is None:
                print(f"  —{'':<19}", end="")
            else:
                cell = f"PF{r['pf']:<4.2f} R{r['ret']:<+5.0f}%"
                print(f"{cell:<22}", end="")
        print(f"{mn_pf:<8.2f}{avg_pf:<8.2f}{std_pf:<7.2f}{mn_ret:<+8.0f}%")

    if sl_results:
        # Best by min PF
        best = max(sl_results, key=lambda x: x["min_pf"])
        print(f"  ⭐ BEST CONSTANCY: SL={best['sl']*100:.0f}%  min_PF={best['min_pf']:.2f}  "
              f"avg_PF={best['avg_pf']:.2f}  worst_Ret={best['min_ret']:+.0f}%")
