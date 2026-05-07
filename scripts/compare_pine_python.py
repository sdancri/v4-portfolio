"""Compare trade events Pine vs Python pentru MNTUSDT 4h.

Foloseste CSV-ul exportat din TradingView Strategy Tester (List of Trades →
Export). Rul Python backtest, filtreaza trade-urile MNT, compara entry/exit
ts + direction + exit reason. Ignora qty/price (size).

Uzitare:
    python scripts/compare_pine_python.py /tmp/pine_mnt.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ichimoku_bot.config import load_config

# Import scripts/backtest.py without requiring scripts to be a package.
# Must register in sys.modules BEFORE exec for dataclasses to find the module.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("backtest_mod", str(ROOT / "scripts" / "backtest.py"))
bt = _ilu.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["backtest_mod"] = bt
_spec.loader.exec_module(bt)       # type: ignore[union-attr]


@dataclass
class Trade:
    direction: str       # "LONG" | "SHORT"
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    entry_signal: str    # "L" | "S"
    exit_reason: str     # "SIG" | "L_SL" | "S_SL" | "L_TP" | "S_TP"


def parse_pine_csv(path: Path) -> list[Trade]:
    """Pine CSV: pentru fiecare trade, 2 randuri ordonate (Exit on top, Entry below)."""
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    trades: list[Trade] = []
    # Group by Trade #
    by_id: dict[str, list[dict]] = {}
    for r in rows:
        tid = r["Trade #"]
        by_id.setdefault(tid, []).append(r)

    for tid in sorted(by_id, key=lambda x: int(x)):
        pair = by_id[tid]
        exit_row = next(r for r in pair if r["Type"].startswith("Exit"))
        entry_row = next(r for r in pair if r["Type"].startswith("Entry"))
        direction = "LONG" if entry_row["Type"] == "Entry long" else "SHORT"
        # Pine exports timestamps in chart timezone (here: Europe/Bucharest cu DST).
        # Convert la UTC pentru a match-ui Python (toate datele OHLCV sunt UTC).
        entry_local = pd.Timestamp(entry_row["Date and time"]).tz_localize(
            "Europe/Bucharest", ambiguous=False, nonexistent="shift_forward",
        )
        exit_local = pd.Timestamp(exit_row["Date and time"]).tz_localize(
            "Europe/Bucharest", ambiguous=False, nonexistent="shift_forward",
        )
        trades.append(Trade(
            direction=direction,
            entry_ts=entry_local.tz_convert("UTC"),
            exit_ts=exit_local.tz_convert("UTC"),
            entry_signal=entry_row["Signal"],
            exit_reason=exit_row["Signal"],
        ))
    return trades


def run_python_backtest(symbol: str, start: str, end: str) -> list[Trade]:
    """Rul backtest Python, intoarce trade-urile pentru pereche specificata."""
    cfg = load_config(str(ROOT / "config/config.yaml"))
    data_dir = Path("/home/dan/Python/Test_Python/data/ohlcv")
    qty_steps = {"MNTUSDT": 0.1, "DOTUSDT": 0.01}
    result = bt.run_backtest(
        cfg, data_dir,
        pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC"),
        qty_steps,
    )
    out: list[Trade] = []
    for t in result["trades"]:
        if t.pair != symbol:
            continue
        out.append(Trade(
            direction=t.direction,
            entry_ts=t.entry_ts,
            exit_ts=t.exit_ts if t.exit_ts is not None else t.entry_ts,
            entry_signal="L" if t.direction == "LONG" else "S",
            exit_reason=("L_SL" if t.exit_reason == "SL" and t.direction == "LONG"
                         else "S_SL" if t.exit_reason == "SL"
                         else "L_TP" if t.exit_reason == "TP" and t.direction == "LONG"
                         else "S_TP" if t.exit_reason == "TP"
                         else "SIG"),
        ))
    return out


def fmt_ts(ts: pd.Timestamp) -> str:
    return ts.strftime("%Y-%m-%d %H:%M")


def compare(pine: list[Trade], py: list[Trade]) -> None:
    print(f"\nPine trades : {len(pine)}")
    print(f"Python trades: {len(py)}")

    # Index Python by entry_ts (4h grid → unique per pair)
    py_by_ts: dict[pd.Timestamp, Trade] = {t.entry_ts: t for t in py}
    pine_by_ts: dict[pd.Timestamp, Trade] = {t.entry_ts: t for t in pine}

    matched_entry = 0
    matched_full = 0     # entry + exit + reason match
    diff_exit_ts = 0
    diff_exit_reason = 0
    diff_direction = 0
    only_pine: list[Trade] = []
    only_py: list[Trade] = []

    for ts, p in pine_by_ts.items():
        if ts not in py_by_ts:
            only_pine.append(p)
            continue
        matched_entry += 1
        py_t = py_by_ts[ts]
        if py_t.direction != p.direction:
            diff_direction += 1
            continue
        exit_ts_match = py_t.exit_ts == p.exit_ts
        reason_match = py_t.exit_reason == p.exit_reason
        if not exit_ts_match:
            diff_exit_ts += 1
        if not reason_match:
            diff_exit_reason += 1
        if exit_ts_match and reason_match:
            matched_full += 1

    for ts in py_by_ts:
        if ts not in pine_by_ts:
            only_py.append(py_by_ts[ts])

    print(f"\n┌─ Match summary ──────────────────────────────────")
    print(f"│ Entries matched (same ts, both sides) : {matched_entry}")
    print(f"│ Full match (entry+exit+reason)        : {matched_full}")
    print(f"│ Direction mismatch                    : {diff_direction}")
    print(f"│ Different exit_ts                     : {diff_exit_ts}")
    print(f"│ Different exit_reason                 : {diff_exit_reason}")
    print(f"│ Only in Pine (not in Python)          : {len(only_pine)}")
    print(f"│ Only in Python (not in Pine)          : {len(only_py)}")
    print(f"└──────────────────────────────────────────────────")

    if only_pine[:5]:
        print("\nFirst 5 trades ONLY in Pine:")
        for t in only_pine[:5]:
            print(f"  Pine [{t.direction}] entry={fmt_ts(t.entry_ts)} → exit={fmt_ts(t.exit_ts)} ({t.exit_reason})")

    if only_py[:5]:
        print("\nFirst 5 trades ONLY in Python:")
        for t in only_py[:5]:
            print(f"  Py   [{t.direction}] entry={fmt_ts(t.entry_ts)} → exit={fmt_ts(t.exit_ts)} ({t.exit_reason})")

    # Show first 5 mismatches in exit
    mismatches = []
    for ts, p in pine_by_ts.items():
        if ts not in py_by_ts:
            continue
        py_t = py_by_ts[ts]
        if py_t.direction != p.direction:
            continue
        if py_t.exit_ts != p.exit_ts or py_t.exit_reason != p.exit_reason:
            mismatches.append((p, py_t))
    if mismatches:
        print(f"\nFirst 5 trades WITH SAME ENTRY but different exit:")
        for p, py_t in mismatches[:5]:
            tag = []
            if p.exit_ts != py_t.exit_ts:
                tag.append("ts")
            if p.exit_reason != py_t.exit_reason:
                tag.append("reason")
            print(f"  Entry {fmt_ts(p.entry_ts)} {p.direction}  Δ:{','.join(tag)}")
            print(f"    Pine exit: {fmt_ts(p.exit_ts)}  reason={p.exit_reason}")
            print(f"    Py   exit: {fmt_ts(py_t.exit_ts)}  reason={py_t.exit_reason}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pine_csv")
    ap.add_argument("--symbol", default="MNTUSDT")
    ap.add_argument("--start", default="2023-10-01")
    ap.add_argument("--end", default="2026-05-07")
    args = ap.parse_args()

    pine = parse_pine_csv(Path(args.pine_csv))
    print(f"Loaded {len(pine)} Pine trades from {args.pine_csv}")

    py = run_python_backtest(args.symbol, args.start, args.end)
    print(f"Generated {len(py)} Python trades for {args.symbol}")

    compare(pine, py)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
