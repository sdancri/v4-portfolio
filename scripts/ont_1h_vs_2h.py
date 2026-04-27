"""ONT 1H (replay) vs ONT 2H (Pine) — diagnostic care TF e mai bun."""
from __future__ import annotations
import pandas as pd
from pathlib import Path

PINE_2H = Path("/home/dan/Descărcări/VSE_2H_Bal_OppExit_BYBIT_ONTUSDT.P_2026-04-26.csv")
REPLAY = Path("/home/dan/Python/VSE_2Perechi/replay_trades.csv")


def load_pine_2h() -> pd.DataFrame:
    df = pd.read_csv(PINE_2H)
    df.columns = [c.strip() for c in df.columns]
    df["dt"] = pd.to_datetime(df["Date and time"], utc=True)
    entries = df[df["Type"].str.startswith("Entry")].copy()
    exits = df[df["Type"].str.startswith("Exit")].copy()
    entries = entries.rename(columns={"dt": "ts_entry"})
    exits = exits.rename(columns={"dt": "ts_exit", "Net P&L USD": "pnl_net", "Signal": "exit_reason"})
    entries["side"] = entries["Type"].str.replace("Entry ", "", regex=False)
    out = entries[["Trade #", "side", "ts_entry"]].merge(
        exits[["Trade #", "ts_exit", "exit_reason", "pnl_net"]], on="Trade #")
    return out.sort_values("ts_entry").reset_index(drop=True)


def load_replay_ont_1h() -> pd.DataFrame:
    df = pd.read_csv(REPLAY)
    df = df[df["symbol"] == "ONT"].copy()
    df["ts_entry"] = pd.to_datetime(df["ts_entry"], utc=True)
    df["ts_exit"] = pd.to_datetime(df["ts_exit"], utc=True)
    return df.sort_values("ts_entry").reset_index(drop=True)


def stats(name: str, pnl: pd.Series, n_long: int, n_short: int) -> None:
    n = len(pnl)
    wins = (pnl > 0).sum()
    losses = (pnl < 0).sum()
    total = pnl.sum()
    avg = pnl.mean()
    best = pnl.max()
    worst = pnl.min()
    print(f"  {name:20s} n={n:4d}  long={n_long:3d}  short={n_short:3d}  "
          f"WR={wins/n*100:5.1f}%  total=${total:+10.2f}  avg=${avg:+6.2f}  "
          f"best=${best:+8.2f}  worst=${worst:+8.2f}")


def main() -> None:
    pine_2h = load_pine_2h()
    rep_1h = load_replay_ont_1h()

    print("═══ DATE RANGE ═══")
    print(f"  Pine ONT 2H : {pine_2h['ts_entry'].min()} → {pine_2h['ts_entry'].max()}  ({len(pine_2h)} trade-uri)")
    print(f"  Replay 1H   : {rep_1h['ts_entry'].min()} → {rep_1h['ts_entry'].max()}  ({len(rep_1h)} trade-uri)")

    common_start = max(pine_2h['ts_entry'].min(), rep_1h['ts_entry'].min())
    common_end = min(pine_2h['ts_entry'].max(), rep_1h['ts_entry'].max())
    print(f"\n[common range: {common_start} → {common_end}]")

    pine_c = pine_2h[(pine_2h["ts_entry"] >= common_start) & (pine_2h["ts_entry"] <= common_end)].copy()
    rep_c = rep_1h[(rep_1h["ts_entry"] >= common_start) & (rep_1h["ts_entry"] <= common_end)].copy()

    print("\n═══ PERFORMANCE (range comun) ═══")
    print("  Atenție: Pine sizing=100% equity (compounding agresiv)")
    print("           Replay sizing=20%×equity / sl_pct cu cap 0.95×balance×20\n")
    stats("Pine ONT 2H", pine_c["pnl_net"],
          int((pine_c["side"]=="long").sum()), int((pine_c["side"]=="short").sum()))
    stats("Replay ONT 1H", rep_c["pnl_net"],
          int((rep_c["side"]=="long").sum()), int((rep_c["side"]=="short").sum()))

    print("\n═══ EXIT REASON ═══")
    print("  Pine 2H:")
    for k, v in pine_c["exit_reason"].value_counts().items():
        print(f"    {k}: {v}")
    print("  Replay 1H:")
    for k, v in rep_c["exit_reason"].value_counts().items():
        print(f"    {k}: {v}")

    print("\n═══ FREQUENCY ═══")
    span_days = (common_end - common_start).total_seconds() / 86400
    print(f"  Span: {span_days:.0f} zile")
    print(f"  Pine 2H   : {len(pine_c)} trade ({len(pine_c)/span_days*30:.1f}/lună)")
    print(f"  Replay 1H : {len(rep_c)} trade ({len(rep_c)/span_days*30:.1f}/lună)")

    print("\n═══ NET % RETURN (PnL / pos_value mediu, ca proxy pt R-multiple) ═══")
    print(f"  Pine 2H   : avg {pine_c['pnl_net'].mean()/50:+.2%} per trade  ({(pine_c['pnl_net'] > 0).mean()*100:.1f}% win rate)")
    if "pnl_pct_pos" in rep_c.columns:
        print(f"  Replay 1H : avg pnl_pct_pos {rep_c['pnl_pct_pos'].mean():+.2f}%  ({(rep_c['pnl_net'] > 0).mean()*100:.1f}% win rate)")

    print("\n═══ EQUITY CURVE FINAL (Pine cu compounding agresiv) ═══")
    print(f"  Pine ONT 2H total PnL pe range comun = ${pine_c['pnl_net'].sum():.2f}")
    print(f"  Replay ONT 1H (sizing real cu cap)   = ${rep_c['pnl_net'].sum():.2f}")
    print("\n  → Pine total mare e ARTEFACT de sizing (100% equity, no cap, fără funding).")
    print("  → Pentru decizia 1H vs 2H pe ONT, comparăm WR și avg-pnl-per-trade, nu total $.")


if __name__ == "__main__":
    main()
