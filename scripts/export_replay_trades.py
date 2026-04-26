"""Export trade-urile replay-ului în CSV cu același format ca trades_setup_target5k.csv,
ca să se poată face diff direct cu engine-ul de referință.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from vse_bot.config import load_config
from vse_bot.replay import run_replay


_EXIT_REASON_MAP = {
    "sl": "TS",                    # SL = SuperTrend trailing (matchează engine-ul)
    "opp": "OPP",                  # Opposite Signal Exit (raw signal, next-bar-open)
    "end_of_data": "EOD",
    "cycle_success": "CYCLE_SUCCESS",
}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--out", default="replay_trades.csv")
    args = ap.parse_args()

    cfg = load_config(args.config)
    results = run_replay(cfg)

    rows: list[dict] = []
    cycle_success_ts: dict[str, list[pd.Timestamp]] = {}
    for sub_name, r in results.items():
        cycle_success_ts[sub_name] = [
            ce.ts for ce in r.cycle_events if ce.kind == "SUCCESS"
        ]

    for sub_name, r in results.items():
        # Track cycle-num per trade via cycle_num_at_entry
        # Per-pair trade index în cycle
        trade_in_cycle: dict[int, int] = {}
        balance_running = cfg.strategy.pool_total
        equity_running = cfg.strategy.equity_start
        pool_used = 0.0
        reset_count = 0
        success_ts_list = list(cycle_success_ts[sub_name])

        for t in sorted(r.trades, key=lambda x: x.entry_time):
            cyc = t.cycle_num_at_entry
            trade_in_cycle[cyc] = trade_in_cycle.get(cyc, 0) + 1
            sl_pct = abs(t.entry_price - t.sl_initial) / t.entry_price * 100
            duration_h = (t.exit_time - t.entry_time).total_seconds() / 3600
            equity_before = t.equity_at_entry
            equity_after = equity_before + t.pnl_net
            balance_before = balance_running
            balance_after = balance_before + t.pnl_net

            # Cycle status: doar pe trade-ul care a închis cycle ca SUCCESS
            cs = ""
            if success_ts_list and t.exit_time >= success_ts_list[0] and t.entry_time < success_ts_list[0]:
                cs = "SUCCESS_TRIGGER (cycle ends here)"

            rows.append({
                "subaccount": sub_name,
                "cycle": cyc,
                "trade_in_cycle": trade_in_cycle[cyc],
                "symbol": t.symbol.replace("USDT", ""),
                "side": "long" if t.direction > 0 else "short",
                "ts_entry": t.entry_time,
                "entry_price": t.entry_price,
                "sl_price": t.sl_initial,
                "sl_pct": round(sl_pct, 3),
                "pos_usd": round(t.notional, 2),
                "risk_usd": round(cfg.strategy.risk_pct_equity * equity_before, 2),
                "qty": t.size,
                "leverage": round(t.notional / max(equity_before, 1e-9), 2),
                "ts_exit": t.exit_time,
                "exit_price": t.exit_price,
                "exit_reason": _EXIT_REASON_MAP.get(t.exit_reason, t.exit_reason),
                "duration_h": round(duration_h, 1),
                "pnl_net": round(t.pnl_net, 2),
                "pnl_pct_pos": round(t.pnl_pct * 100, 2),
                "r_multiple": round(t.pnl_R, 2),
                "equity_before": round(equity_before, 2),
                "equity_after": round(equity_after, 2),
                "balance_before": round(balance_before, 2),
                "balance_after": round(balance_after, 2),
                "pool_used": round(pool_used, 2),
                "reset_count": reset_count,
                "cycle_status": cs,
            })

            balance_running = balance_after

    out = Path(args.out)
    df = pd.DataFrame(rows)
    df.to_csv(out, index=False)
    print(f"Wrote {len(df)} trades to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
