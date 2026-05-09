"""Multi-window backtest: ruleaza 2022-, 2023-, 2024-, 2025-, 2026- in paralel.

Foloseste multiprocessing.Pool pentru a rula 5 backtest-uri simultane,
fiecare pe alta fereastra de start. Toate se opresc la max date din parquet.

Run:
    python scripts/backtest_v4_multi.py
"""
from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config  # noqa: E402
from scripts.backtest_v4 import run_backtest, summary_metrics  # noqa: E402


def _worker(args: tuple) -> tuple:
    """Worker process: ruleaza backtest pt fereastra (start, end) si returneaza metrici."""
    start_str, end_str, config_path = args
    cfg = load_config(config_path)
    start = pd.Timestamp(start_str, tz="UTC")
    end = pd.Timestamp(end_str, tz="UTC")
    result = run_backtest(cfg, start, end)
    metrics = summary_metrics(result)
    metrics["start_str"] = start_str
    metrics["start"] = result["start"]
    metrics["end"] = result["end"]
    return start_str, metrics


def main():
    config_path = "config/config_v4.yaml"
    end_str = "2026-12-31"
    windows = [
        ("2022-01-01", end_str),
        ("2023-01-01", end_str),
        ("2024-01-01", end_str),
        ("2025-01-01", end_str),
        ("2026-01-01", end_str),
    ]

    print(f"\n  V4 multi-window backtest — {len(windows)} ferestre in paralel")
    print(f"  Pool $100 init  fee 0.055%  cap_lev 10×  (config: {config_path})")
    print(f"  Workers: {len(windows)} processes\n")

    args = [(s, e, config_path) for s, e in windows]
    with mp.Pool(processes=len(windows)) as pool:
        results = pool.map(_worker, args)

    # Sort by start date
    results.sort(key=lambda x: x[0])

    # Comparative table
    print(f"\n{'='*98}")
    print(f"  COMPARATIVE — V4 portfolio (BTC+TIA+NEAR, $100 init, 10% risk, fee 0.055%)")
    print(f"{'='*98}")
    print(f"  {'Window':<14}{'Years':>6}{'Final $':>14}{'CAGR':>10}"
          f"{'MaxDD':>9}{'Trades':>8}{'WR':>7}{'PF':>7}")
    print(f"  {'-'*14}{'-'*6}{'-'*14}{'-'*10}{'-'*9}{'-'*8}{'-'*7}{'-'*7}")
    for start_str, m in results:
        pf_str = f"{m['pf']:.2f}" if m['pf'] != float('inf') else "inf"
        print(f"  {start_str+'-':<14}{m['n_years']:>5.2f}y"
              f"{m['final']:>14,.0f}{m['cagr']:>9.1f}%"
              f"{m['max_dd']:>8.1f}%{m['n_trades']:>8}"
              f"{m['wr']:>6.1f}%{pf_str:>7}")
    print(f"{'='*98}\n")

    # Per-pair breakdown
    print(f"  {'PER-PAIR PnL ($) — pe fereastra':-^98}")
    print(f"  {'Window':<14}{'BTCUSDT':>14}{'TIAUSDT':>14}{'NEARUSDT':>14}{'Total':>14}")
    print(f"  {'-'*14}{'-'*14}{'-'*14}{'-'*14}{'-'*14}")
    for start_str, m in results:
        per = m.get("per_pair", {})
        btc = per.get("BTCUSDT", {}).get("pnl", 0)
        tia = per.get("TIAUSDT", {}).get("pnl", 0)
        near = per.get("NEARUSDT", {}).get("pnl", 0)
        total = btc + tia + near
        print(f"  {start_str+'-':<14}"
              f"{btc:>+14,.0f}{tia:>+14,.0f}{near:>+14,.0f}{total:>+14,.0f}")
    print()


if __name__ == "__main__":
    main()
