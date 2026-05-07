"""Screening multi-process: ruleaza MNT preset pe TOATE perechile USDT 4h.

Pentru fiecare parquet 1h disponibil:
  - Resample on-the-fly la 4h aliniat UTC
  - Backtest single MNT preset (Hull=8, Kijun=48, SnkB=40, SL=3%, no TP, risk=7%, lev=20x)
  - Skip daca <800 bare 4h disponibile (~4.5 luni)
  - Fees: 70/30 mix entry, taker exit

Output: ranking CSV by PF + Return.

Uzitare:
    python scripts/screen_all_pairs.py
    python scripts/screen_all_pairs.py --workers 8 --min-bars 1500
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ichimoku_bot.config import AppConfig, PairConfig, PortfolioConfig, OperationalConfig

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("backtest_mod", str(ROOT / "scripts" / "backtest.py"))
bt = _ilu.module_from_spec(_spec)  # type: ignore
sys.modules["backtest_mod"] = bt
_spec.loader.exec_module(bt)        # type: ignore


# MNT preset
MNT_PRESET = {
    "hull_length": 8, "tenkan_periods": 9, "kijun_periods": 48,
    "senkou_b_periods": 40, "displacement": 24,
    "risk_pct_per_trade": 0.07, "sl_initial_pct": 0.03, "tp_pct": None,
    "max_hull_spread_pct": 2.0, "max_close_kijun_dist_pct": 6.0,
    "leverage": 20,
}
ENTRY_FEE = 0.000305
EXIT_FEE = 0.00055
DATA_DIR = Path("/home/dan/Python/Test_Python/data/ohlcv")


def make_cfg(symbol: str) -> AppConfig:
    pair = PairConfig(symbol=symbol, timeframe="4h", enabled=True, **MNT_PRESET)
    return AppConfig(
        portfolio=PortfolioConfig(
            name="screen", pool_total=100.0, leverage=15,
            cap_pct_of_max=0.95, taker_fee=0.00055, slippage_bps=0.0,
        ),
        pairs=[pair],
        operational=OperationalConfig(max_concurrent_positions=1),
    )


def screen_one(symbol: str, min_bars: int = 800) -> dict:
    """Resample 1h→4h + backtest single MNT preset."""
    fpath = DATA_DIR / f"{symbol}_1h.parquet"
    if not fpath.exists():
        return {"symbol": symbol, "error": "no_1h_data"}
    try:
        df_1h = pd.read_parquet(fpath)
        if df_1h.index.tz is None:
            df_1h.index = df_1h.index.tz_localize("UTC")
        # Align to UTC midnight 4h boundaries
        first_aligned_idx = df_1h.index[df_1h.index.hour % 4 == 0]
        if len(first_aligned_idx) == 0:
            return {"symbol": symbol, "error": "no_aligned_bars"}
        df_1h = df_1h.loc[first_aligned_idx[0]:]
        df_4h = df_1h.resample(
            "4h", origin="epoch", closed="left", label="left"
        ).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()
        if len(df_4h) < min_bars:
            return {"symbol": symbol, "error": f"too_few_bars:{len(df_4h)}"}
    except Exception as e:
        return {"symbol": symbol, "error": f"load:{e}"}

    # Save to in-memory dir (one file per worker)
    import tempfile, os
    tmpdir = tempfile.mkdtemp(prefix=f"screen_{symbol}_")
    try:
        out = Path(tmpdir) / f"{symbol}_4h.parquet"
        df_4h.to_parquet(out)
        cfg = make_cfg(symbol)
        # Use price-based qty step heuristic (sub-cent → 1.0; cent → 0.1; usd → 0.01)
        max_p = float(df_4h["high"].max())
        if max_p < 0.01:
            qty_step = 100.0
        elif max_p < 0.1:
            qty_step = 10.0
        elif max_p < 1.0:
            qty_step = 1.0
        elif max_p < 10.0:
            qty_step = 0.1
        else:
            qty_step = 0.01
        result = bt.run_backtest(
            cfg, Path(tmpdir),
            df_4h.index[0].tz_convert("UTC"),
            df_4h.index[-1].tz_convert("UTC"),
            qty_steps={symbol: qty_step},
            entry_fee=ENTRY_FEE, exit_fee=EXIT_FEE,
        )
    except Exception as e:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"symbol": symbol, "error": f"backtest:{e}"}
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    trades = result["trades"]
    final = result["final_equity"]
    eq_vals = [v for _, v in result["equity_curve"]]
    n_bars = len(df_4h)
    months = n_bars * 4 / 24 / 30.4   # 4h bars

    if not trades:
        return {"symbol": symbol, "n": 0, "wr": 0.0, "pf": 0.0, "ret": 0.0,
                "dd": 0.0, "final": final, "bars": n_bars, "months": months}

    wins = [t for t in trades if t.pnl_net > 0]
    losses = [t for t in trades if t.pnl_net <= 0]
    gross_win = sum(t.pnl_net for t in wins)
    gross_loss = abs(sum(t.pnl_net for t in losses)) or 1e-9
    pf = gross_win / gross_loss if losses else float("inf")
    peaks = np.maximum.accumulate(eq_vals)
    dd = float(((np.array(eq_vals) - peaks) / peaks * 100).min())
    ret = (final / 100 - 1) * 100
    annualized = ((final / 100) ** (12 / months) - 1) * 100 if months > 0 else 0.0

    return {
        "symbol": symbol, "n": len(trades), "wr": len(wins) / len(trades) * 100,
        "pf": round(pf, 3), "ret": round(ret, 1), "dd": round(dd, 1),
        "final": round(final, 2), "bars": n_bars, "months": round(months, 1),
        "annualized": round(annualized, 1), "fees": round(sum(t.fees for t in trades), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--min-bars", type=int, default=800,
                    help="Skip pairs cu < N bare 4h (default 800 = ~4.5 luni)")
    ap.add_argument("--out", default="/tmp/screen_all_pairs.csv")
    args = ap.parse_args()

    pairs_files = sorted(DATA_DIR.glob("*USDT_1h.parquet"))
    pairs = [f.stem.replace("_1h", "") for f in pairs_files]
    print(f"Screening {len(pairs)} pairs cu MNT preset, {args.workers} workers...")
    print(f"  preset: Hull=8, Kijun=48, SnkB=40, SL=3%, no TP, risk=7%, lev=20x")
    print(f"  fees: entry={ENTRY_FEE*100:.4f}%, exit={EXIT_FEE*100:.4f}%")
    print(f"  min bars: {args.min_bars} (= {args.min_bars*4/24/30.4:.1f} luni)")

    results: list[dict] = []
    skipped = 0
    errored = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(screen_one, sym, args.min_bars): sym for sym in pairs}
        done = 0
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            if "error" in r:
                if r["error"].startswith("too_few_bars"):
                    skipped += 1
                else:
                    errored += 1
            else:
                results.append(r)
            if done % 50 == 0:
                print(f"  [{done}/{len(pairs)}] passed={len(results)} skip={skipped} err={errored}")

    print(f"\nDone: {len(results)} backtest-uri valide, {skipped} skip (too few bars), {errored} errors")

    # Filter: at least 30 trades + DD ≥ -55%
    valid = [r for r in results if r["n"] >= 30 and r["dd"] >= -55.0]
    print(f"After filter (n>=30, DD>=-55%): {len(valid)} pairs")

    # Save full CSV
    df = pd.DataFrame(results).sort_values("pf", ascending=False)
    df.to_csv(args.out, index=False)
    print(f"Full CSV: {args.out}")

    # Print top 30 by PF
    print(f"\n{'─'*100}")
    print(f"TOP 30 by PF (n>=30, DD>=-55%, MNT preset on each pair)")
    print(f"{'─'*100}")
    print(f"{'symbol':<22}{'n':<6}{'WR':<8}{'PF':<7}{'Ret':<11}{'Annual':<10}{'DD':<9}{'months':<8}")
    top30 = sorted(valid, key=lambda x: -x["pf"])[:30]
    for r in top30:
        print(f"  {r['symbol']:<20}{r['n']:<6}{r['wr']:<7.1f}%{r['pf']:<7.2f}"
              f"{r['ret']:<+10.1f}%{r['annualized']:<+9.1f}%{r['dd']:<+8.1f}%{r['months']:<8.1f}")

    print(f"\n{'─'*100}")
    print(f"TOP 20 by Annualized Return (n>=30, DD>=-55%)")
    print(f"{'─'*100}")
    print(f"{'symbol':<22}{'n':<6}{'WR':<8}{'PF':<7}{'Ret':<11}{'Annual':<10}{'DD':<9}{'months':<8}")
    top_ann = sorted(valid, key=lambda x: -x["annualized"])[:20]
    for r in top_ann:
        print(f"  {r['symbol']:<20}{r['n']:<6}{r['wr']:<7.1f}%{r['pf']:<7.2f}"
              f"{r['ret']:<+10.1f}%{r['annualized']:<+9.1f}%{r['dd']:<+8.1f}%{r['months']:<8.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
