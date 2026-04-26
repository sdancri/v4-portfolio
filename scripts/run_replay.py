"""Rulează replay-ul pentru toate subconturile din config.yaml și raportează
match-ul cu țintele din strategy.md.

Uzitare:
    python scripts/run_replay.py [--config config/config.yaml]

Așteptat (per strategy.md, perioada 2024-01-01 — 2026-04-25):
    subacc_1_kaia_aave   wealth ≈ $13,364
    subacc_2_ont_eth     wealth ≈ $11,485
    portfolio total      wealth ≈ $24,849
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vse_bot.config import load_config
from vse_bot.replay import run_replay


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    print("=" * 72)
    print(f"REPLAY  |  strategy={cfg.strategy.name}  style={cfg.strategy.style}")
    print(f"period: {cfg.replay.start}  →  {cfg.replay.end}")
    print(f"data:   {cfg.replay.data_dir}")
    print("=" * 72)

    results = run_replay(cfg)

    total_wealth = 0.0
    total_trades = 0
    for sub in cfg.subaccounts:
        if not sub.enabled:
            continue
        r = results[sub.name]
        target = sub.expected_wealth_2_3y
        delta = r.wealth - target
        delta_pct = (delta / target * 100) if target else 0.0
        print(f"\n— {r.subacc_name} —")
        print(f"  pairs: {[(p.symbol, p.timeframe) for p in sub.pairs]}")
        print(f"  trades:           {r.n_trades}")
        print(f"  cycles SUCCESS:   {r.n_cycles_success}")
        print(f"  resets:           {r.n_resets}")
        print(f"  pool_low events:  {r.n_pool_low}")
        print(f"  total withdrawn:  ${r.total_withdrawn:>12,.2f}")
        print(f"  final balance:    ${r.final_state.balance_broker:>12,.2f}")
        print(f"  peak balance:     ${r.peak_balance:>12,.2f}")
        print(f"  WEALTH:           ${r.wealth:>12,.2f}   "
              f"(target ${target:,.0f}  Δ ${delta:+,.0f}  {delta_pct:+.1f}%)")
        total_wealth += r.wealth
        total_trades += r.n_trades

    print("\n" + "=" * 72)
    print(f"PORTFOLIO TOTAL: wealth ${total_wealth:,.2f}  "
          f"(target $24,849)  trades {total_trades}")
    print("=" * 72)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
