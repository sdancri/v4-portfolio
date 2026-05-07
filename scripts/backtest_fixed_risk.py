"""Backtest cu risk FIX $7 per trade (NO COMPOUND in sizing).

Sizing: risk_usd = 7$ FIX (nu 7% × equity).
Position size constant indiferent de equity. Equity crește liniar.
PnL absolut = wins-losses cu mărime fixă.
"""

from __future__ import annotations

import sys
from pathlib import Path
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ichimoku_bot.ichimoku_signal import (
    PairStrategyConfig, _close_long, _close_short, _long_entry, _short_entry,
    passes_filters, precompute_indicators,
)
from ichimoku_bot.sizing import compute_qty


PAIRS = {
    "SUNUSDT":  {"hull": 8,  "kj": 24, "snkb": 52, "sl": 0.04, "tp": 0.50, "step": 10.0,
                 "data_dir": "/tmp/sun_data", "start": "2023-10-01"},
    "MNTUSDT":  {"hull": 10, "kj": 48, "snkb": 52, "sl": 0.04, "tp": 0.05, "step": 0.1,
                 "data_dir": "/tmp/mnt_data", "start": "2023-10-01"},
    "ILVUSDT":  {"hull": 8,  "kj": 36, "snkb": 40, "sl": 0.04, "tp": 0.12, "step": 0.01,
                 "data_dir": "/tmp/ilv_data", "start": "2023-10-01"},
    "AEROUSDT": {"hull": 10, "kj": 60, "snkb": 26, "sl": 0.04, "tp": 0.20, "step": 1.0,
                 "data_dir": "/tmp/aero_data", "start": "2024-07-15"},
    "RSRUSDT":  {"hull": 10, "kj": 60, "snkb": 52, "sl": 0.04, "tp": 0.05, "step": 10.0,
                 "data_dir": "/tmp/rsr_data", "start": "2023-10-01"},
    "AKTUSDT":  {"hull": 8,  "kj": 60, "snkb": 52, "sl": 0.04, "tp": 0.08, "step": 0.1,
                 "data_dir": "/tmp/akt_data", "start": "2024-06-26"},
}

FIXED_RISK_USD = 7.0
ENTRY_FEE = 0.000305
EXIT_FEE = 0.00055
LEVERAGE = 20


def make_ssc(symbol):
    c = PAIRS[symbol]
    return PairStrategyConfig(
        symbol=symbol, timeframe="4h", hull_length=c["hull"], tenkan_periods=9,
        kijun_periods=c["kj"], senkou_b_periods=c["snkb"], displacement=24,
        risk_pct_per_trade=0.07, sl_initial_pct=c["sl"], tp_pct=c["tp"],
        max_hull_spread_pct=2.0, max_close_kijun_dist_pct=6.0, taker_fee=0.00055)


def run_pair(symbol, start_str, end_str):
    cfg = PAIRS[symbol]
    sl = cfg["sl"]; tp = cfg["tp"]; step = cfg["step"]
    fpath = Path(cfg["data_dir"]) / f"{symbol}_4h.parquet"
    df_full = pd.read_parquet(fpath)
    if df_full.index.tz is None:
        df_full.index = df_full.index.tz_localize("UTC")
    start = pd.Timestamp(start_str, tz="UTC")
    end = pd.Timestamp(end_str, tz="UTC")
    df = df_full.loc[(df_full.index >= start) & (df_full.index <= end)].copy()
    if df.empty: return None
    ssc = make_ssc(symbol)
    cache = precompute_indicators(df, ssc)

    equity = 100.0
    pos = None
    trades = []
    eq_curve = [equity]

    for i in range(len(df)):
        if i < ssc.min_history_bars: continue
        c = cache
        if any(np.isnan(x) for x in [c.n1[i], c.n2[i], c.tenkan[i], c.kijun[i],
                                      c.senkou_h[i], c.senkou_l[i], c.chikou[i]]):
            continue
        cur = df.iloc[i]
        close = float(cur["close"]); high = float(cur["high"]); low = float(cur["low"])
        n1 = c.n1[i]; n2 = c.n2[i]; tk = c.tenkan[i]; kj = c.kijun[i]
        sh = c.senkou_h[i]; sl_ = c.senkou_l[i]; ch = c.chikou[i]

        if pos is not None:
            exit_price = None; reason = ""
            if pos["dir"] == "LONG":
                if low <= pos["sl"]: exit_price = pos["sl"]; reason = "SL"
                elif tp is not None and high >= pos["tp"]: exit_price = pos["tp"]; reason = "TP"
                elif _close_long(close, n1, n2, tk, kj, sh, ch): exit_price = close; reason = "SIGNAL"
            else:
                if high >= pos["sl"]: exit_price = pos["sl"]; reason = "SL"
                elif tp is not None and low <= pos["tp"]: exit_price = pos["tp"]; reason = "TP"
                elif _close_short(close, n1, n2, tk, kj, sl_, ch): exit_price = close; reason = "SIGNAL"
            if exit_price is not None:
                if pos["dir"] == "LONG":
                    gross = (exit_price - pos["entry"]) * pos["qty"]
                else:
                    gross = (pos["entry"] - exit_price) * pos["qty"]
                exit_fee_amt = exit_price * pos["qty"] * EXIT_FEE
                pnl_net = gross - pos["entry_fee"] - exit_fee_amt
                equity += pnl_net
                trades.append({"reason": reason, "pnl": pnl_net})
                eq_curve.append(equity)
                pos = None

        if pos is None:
            ls = _long_entry(close, n1, n2, tk, kj, sh, ch)
            ss = _short_entry(close, n1, n2, tk, kj, sl_, ch)
            if not (ls or ss): continue
            ok, _ = passes_filters(close, n1, n2, kj, ssc)
            if not ok: continue
            pos_usd = FIXED_RISK_USD / sl  # FIXED $7 risk → fixed pos size
            cap_usd = 0.95 * equity * LEVERAGE
            if pos_usd > cap_usd: continue
            qty = compute_qty(pos_usd, close, step)
            if qty <= 0: continue
            direction = "LONG" if ls else "SHORT"
            sl_price = close * (1 - sl) if direction == "LONG" else close * (1 + sl)
            tp_price = (close * (1 + tp) if direction == "LONG" else close * (1 - tp)) if tp else None
            entry_fee_amt = close * qty * ENTRY_FEE
            pos = {"dir": direction, "entry": close, "qty": qty, "sl": sl_price,
                   "tp": tp_price, "entry_fee": entry_fee_amt}

    return {"trades": trades, "eq_curve": eq_curve, "final": equity}


def report(result, label, months):
    trades = result["trades"]
    final = result["final"]
    if not trades: return f"{label}: no trades"
    eq = result["eq_curve"]
    peaks = np.maximum.accumulate(eq)
    dd = float(((np.array(eq) - peaks) / peaks * 100).min())
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gw = sum(t["pnl"] for t in wins); gl = abs(sum(t["pnl"] for t in losses)) or 1e-9
    pf = gw / gl
    avg_win = gw / len(wins) if wins else 0
    avg_loss = gl / len(losses) if losses else 0
    sl_h = sum(1 for t in trades if t["reason"] == "SL")
    tp_h = sum(1 for t in trades if t["reason"] == "TP")
    sig = sum(1 for t in trades if t["reason"] == "SIGNAL")
    return (f"  n={len(trades):<5} WR={len(wins)/len(trades)*100:<5.1f}% PF={pf:<5.2f}  "
            f"Final=${final:<8.2f}  Ret={(final/100-1)*100:+7.1f}%  DD={dd:+5.1f}%\n"
            f"  exits: SL={sl_h}  TP={tp_h}  SIG={sig}  avg_W=${avg_win:.2f}  avg_L=$-{avg_loss:.2f}")


print(f"\n{'='*100}")
print(f"FIXED RISK $7/trade (no compound) — full period per pereche")
print(f"Fees: entry 0.0305% (mix), exit 0.055% (taker)")
print(f"{'='*100}")

for sym in PAIRS:
    cfg = PAIRS[sym]
    months = (pd.Timestamp("2026-04-25") - pd.Timestamp(cfg["start"])).days / 30.4
    r = run_pair(sym, cfg["start"], "2026-04-25")
    if r:
        print(f"\n{sym} ({months:.1f}mo, start {cfg['start']}):")
        print(report(r, sym, months))

# Now combine ICHI1 and ICHI2 totals (sum of standalone PnL since fixed risk = independent)
ichi1_pairs = ["SUNUSDT", "MNTUSDT", "ILVUSDT"]
ichi2_pairs = ["AEROUSDT", "RSRUSDT", "AKTUSDT"]

print(f"\n{'='*100}")
print(f"ICHI1 sum vs ICHI2 sum (fixed risk = pairs trade independent, no compound interaction)")
print(f"{'='*100}")
for label, plist in [("ICHI1 sum", ichi1_pairs), ("ICHI2 sum", ichi2_pairs)]:
    pnl = 0; n = 0; sl_h = 0; tp_h = 0; sig = 0
    for sym in plist:
        cfg = PAIRS[sym]
        r = run_pair(sym, cfg["start"], "2026-04-25")
        if r:
            pnl += r["final"] - 100
            n += len(r["trades"])
            sl_h += sum(1 for t in r["trades"] if t["reason"] == "SL")
            tp_h += sum(1 for t in r["trades"] if t["reason"] == "TP")
            sig += sum(1 for t in r["trades"] if t["reason"] == "SIGNAL")
    print(f"  {label}: PnL=${pnl:+.0f}  n={n}  exits: SL={sl_h} TP={tp_h} SIG={sig}")
