"""Compară trade-urile Pine (TradingView KAIAUSDT.P) cu replay-ul Python pe KAIA."""
from __future__ import annotations
import pandas as pd
from pathlib import Path

import sys
SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "KAIA"
PINE_MAP = {
    "KAIA": "/home/dan/Descărcări/VSE_Balanced_—_verify_(entries+exits)_BYBIT_KAIAUSDT.P_2026-04-26.csv",
    "ETH":  "/home/dan/Descărcări/VSE_Balanced_—_verify_(entries+exits)_BYBIT_ETHUSDT.P_2026-04-26.csv",
}
PINE = Path(PINE_MAP[SYMBOL])
REPLAY = Path("/home/dan/Python/VSE_2Perechi/replay_trades.csv")


def load_pine() -> pd.DataFrame:
    df = pd.read_csv(PINE)
    df.columns = [c.strip() for c in df.columns]
    df["dt"] = pd.to_datetime(df["Date and time"], utc=True)
    entries = df[df["Type"].str.startswith("Entry")].copy()
    exits = df[df["Type"].str.startswith("Exit")].copy()

    entries = entries.rename(columns={
        "dt": "ts_entry", "Price USDT": "entry_price", "Signal": "entry_sig",
        "Size (qty)": "qty", "Size (value)": "pos_value",
    })
    exits = exits.rename(columns={
        "dt": "ts_exit", "Price USDT": "exit_price", "Signal": "exit_reason",
        "Net P&L USDT": "pnl_net",
    })

    entries["side"] = entries["Type"].str.replace("Entry ", "", regex=False)
    keep_e = ["Trade #", "side", "ts_entry", "entry_price", "entry_sig", "qty", "pos_value"]
    keep_x = ["Trade #", "ts_exit", "exit_price", "exit_reason", "pnl_net"]
    out = entries[keep_e].merge(exits[keep_x], on="Trade #")
    return out.sort_values("ts_entry").reset_index(drop=True)


def load_replay_kaia() -> pd.DataFrame:
    df = pd.read_csv(REPLAY)
    df = df[df["symbol"] == SYMBOL].copy()
    df["ts_entry"] = pd.to_datetime(df["ts_entry"], utc=True)
    df["ts_exit"] = pd.to_datetime(df["ts_exit"], utc=True)
    return df.sort_values("ts_entry").reset_index(drop=True)


def detect_shift(pine: pd.DataFrame, rep: pd.DataFrame) -> int:
    """Detectează shift orar între Pine și replay (modulo same-bar matches)."""
    rep_ts = set(rep["ts_entry"])
    best_h, best_n = 0, 0
    for h in range(-3, 4):
        shifted = {t + pd.Timedelta(hours=h) for t in rep_ts}
        n = len(set(pine["ts_entry"]) & shifted)
        if n > best_n:
            best_n, best_h = n, h
    return best_h


def compare(pine: pd.DataFrame, rep: pd.DataFrame) -> None:
    print("═══ COUNTS ═══")
    print(f"  Pine    : {len(pine)} trades")
    print(f"  Replay  : {len(rep)} trades")
    print(f"  Diff    : {len(pine) - len(rep):+d}")

    print("\n═══ DATE RANGE ═══")
    print(f"  Pine    : {pine['ts_entry'].min()} → {pine['ts_entry'].max()}")
    print(f"  Replay  : {rep['ts_entry'].min()} → {rep['ts_entry'].max()}")

    print("\n═══ SIDE DISTRIBUTION ═══")
    print(f"  Pine   long={int((pine['side']=='long').sum())}  short={int((pine['side']=='short').sum())}")
    print(f"  Replay long={int((rep['side']=='long').sum())}  short={int((rep['side']=='short').sum())}")

    print("\n═══ EXIT REASON DISTRIBUTION ═══")
    print("  Pine:")
    for k, v in pine["exit_reason"].value_counts().items():
        print(f"    {k}: {v}")
    print("  Replay:")
    for k, v in rep["exit_reason"].value_counts().items():
        print(f"    {k}: {v}")

    # Filter Pine: ignore "Margin call" — sizing in Pine (100% equity × 20x lev) liquidează
    # frecvent; bot-ul folosește cap=0.95×balance×20 deci poziții mai mici, fără MC.
    pine_no_mc = pine[pine["exit_reason"] != "Margin call"].copy()
    print(f"\n═══ PINE FĂRĂ Margin call: {len(pine_no_mc)} trade-uri ═══")
    print(f"  long={int((pine_no_mc['side']=='long').sum())}  short={int((pine_no_mc['side']=='short').sum())}")

    # Show match counts pentru toate shift-urile, nu doar best
    print("\n═══ MATCH COUNT PE SHIFT (replay → Pine) ═══")
    for h in range(-3, 5):
        shifted = {t + pd.Timedelta(hours=h) for t in rep["ts_entry"]}
        n = len(set(pine_no_mc["ts_entry"]) & shifted)
        print(f"  shift {h:+d}h: {n} matches")
    shift = detect_shift(pine_no_mc, rep)
    print(f"\n═══ TIMESTAMP SHIFT BEST: {shift:+d}h (replay → Pine) ═══")
    rep_shifted = rep.copy()
    rep_shifted["ts_entry"] = rep_shifted["ts_entry"] + pd.Timedelta(hours=shift)

    rep_only = rep_shifted[(rep_shifted["ts_entry"] >= pine_no_mc["ts_entry"].min()) & (rep_shifted["ts_entry"] <= pine_no_mc["ts_entry"].max())].copy()
    print(f"\n═══ ENTRY TIMESTAMP MATCH (Pine fără MC vs replay shift {shift:+d}h: {len(rep_only)} trades) ═══")
    pine_ts = set(pine_no_mc["ts_entry"])
    rep_ts = set(rep_only["ts_entry"])
    common = pine_ts & rep_ts
    print(f"  Pine ts unique           : {len(pine_ts)}")
    print(f"  Replay ts unique         : {len(rep_ts)}")
    print(f"  Common ts (exact match)  : {len(common)}")
    print(f"  Match rate vs Pine       : {len(common)/len(pine_ts)*100:.1f}%")
    print(f"  Match rate vs Replay     : {len(common)/len(rep_ts)*100:.1f}%")

    print("\n═══ MERGED ON TS_ENTRY (first 10 matches) ═══")
    merged = pine_no_mc.merge(rep_only, on="ts_entry", how="inner", suffixes=("_pine", "_rep"))
    # Suffixes apar pe coloanele duplicate; păstrează _pine/_rep unde e cazul.
    cols = [c for c in ["ts_entry", "side_pine", "side_rep", "entry_price_pine",
            "entry_price_rep", "exit_reason_pine", "exit_reason_rep", "pnl_net_pine"]
            if c in merged.columns]
    if len(merged):
        print(merged[cols].head(10).to_string(index=False))
        same_side = (merged["side_pine"] == merged["side_rep"]).sum()
        print(f"\n  Side match  : {same_side}/{len(merged)} ({same_side/len(merged)*100:.1f}%)")
        same_exit = (merged["exit_reason_pine"].str.upper() == merged["exit_reason_rep"].str.upper()).sum()
        print(f"  Exit match  : {same_exit}/{len(merged)} ({same_exit/len(merged)*100:.1f}%)")
    else:
        print("  no exact match on ts_entry")

    print("\n═══ PINE TRADES NOT IN REPLAY (first 10) ═══")
    pine_only = pine_no_mc[~pine_no_mc["ts_entry"].isin(rep_ts)]
    print(f"  Total: {len(pine_only)}")
    if len(pine_only):
        print(pine_only[["ts_entry", "side", "entry_price", "exit_reason"]].head(10).to_string(index=False))

    print("\n═══ REPLAY TRADES NOT IN PINE (first 10) ═══")
    rep_only_no_match = rep_only[~rep_only["ts_entry"].isin(pine_ts)]
    print(f"  Total: {len(rep_only_no_match)}")
    if len(rep_only_no_match):
        print(rep_only_no_match[["ts_entry", "side", "entry_price", "exit_reason"]].head(10).to_string(index=False))


def fuzzy_match(pine_no_mc: pd.DataFrame, rep: pd.DataFrame, tol_h: int = 4) -> None:
    """Match Pine vs replay cu toleranță în ore (capturează shift TZ + bar boundary)."""
    print(f"\n═══ FUZZY MATCH (toleranță ±{tol_h}h, prețul entry trebuie să fie identic) ═══")
    pine_arr = pine_no_mc[["ts_entry", "side", "entry_price", "exit_reason"]].to_dict("records")
    rep_arr = rep[["ts_entry", "side", "entry_price", "exit_reason"]].to_dict("records")
    matched_pine = set()
    matched_rep = set()
    side_ok = exit_ok = 0
    for i, p in enumerate(pine_arr):
        for j, r in enumerate(rep_arr):
            if j in matched_rep:
                continue
            if abs((p["ts_entry"] - r["ts_entry"]).total_seconds()) <= tol_h * 3600:
                if abs(p["entry_price"] - r["entry_price"]) < 1e-4:
                    matched_pine.add(i)
                    matched_rep.add(j)
                    if p["side"] == r["side"]:
                        side_ok += 1
                    if str(p["exit_reason"]).upper() == str(r["exit_reason"]).upper():
                        exit_ok += 1
                    break
    n = len(matched_pine)
    print(f"  Matches: {n}/{len(pine_arr)} Pine ({n/len(pine_arr)*100:.1f}%) | {n}/{len(rep_arr)} Replay ({n/len(rep_arr)*100:.1f}%)")
    if n:
        print(f"  Side match  : {side_ok}/{n} ({side_ok/n*100:.1f}%)")
        print(f"  Exit match  : {exit_ok}/{n} ({exit_ok/n*100:.1f}%)")
    print(f"  Pine NOT matched   : {len(pine_arr) - n}")
    print(f"  Replay NOT matched : {len(rep_arr) - n}")


if __name__ == "__main__":
    pine = load_pine()
    rep = load_replay_kaia()
    # Aliniază range-ul Pine la replay (replay pornește mai târziu)
    pine = pine[(pine["ts_entry"] >= rep["ts_entry"].min() - pd.Timedelta(hours=6)) &
                (pine["ts_entry"] <= rep["ts_entry"].max() + pd.Timedelta(hours=6))].copy()
    print(f"\n[FILTRU] Pine restricted la range-ul replay: {len(pine)} trade-uri\n")
    compare(pine, rep)
    pine_no_mc = pine[pine["exit_reason"] != "Margin call"].copy()
    fuzzy_match(pine_no_mc, rep)
