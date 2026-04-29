"""Replicate EXACT warmup-ul container-ului VSE_2 ONT 1h.

Container la 09:14 UTC azi: fetch_ohlcv(limit=400) → buffer warmup
Apoi LIVE: 09:00, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 16:00 UTC

Verific dacă ar fi detectat semnalul SHORT pe 13:00 UTC.
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ccxt.async_support as ccxt
import pandas as pd

from vse_bot.config import load_config
from vse_bot.vse_signal_live import VSESignalLive
from vse_bot.indicator import compute_indicators, build_signals


async def main() -> int:
    cfg = load_config("config/config.yaml")

    ex = ccxt.bybit({
        "enableRateLimit": True,
        "timeout": 30_000,
        "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
    })
    ex.has["fetchCurrencies"] = False

    try:
        # Simulez container la 09:14 UTC: fetch_ohlcv(limit=400) returnează ultimele
        # 400 bare PÂNĂ LA 09:00 UTC (bara cea mai recentă confirmed la 09:14).
        # Container azi pornit 09:14:37 UTC.
        # Cele 400 bare ≈ ultimele 16-17 zile, ending la 09:00 UTC azi (29-04-2026).

        # Fetch acum 500 bare să avem și warmup-ul la cum era la 09:14, plus
        # LIVE până la prezent.
        ohlcv = await ex.fetch_ohlcv("ONTUSDT", "1h", limit=500)
        df = pd.DataFrame(ohlcv, columns=["ts_ms", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
        df = df.set_index("ts").drop(columns=["ts_ms"])

        # Cutoff la 09:00 UTC azi — exact ce avea container-ul la pornire
        startup_cutoff = pd.Timestamp("2026-04-29 09:00:00", tz="UTC")
        warmup_df = df[df.index <= startup_cutoff].tail(400)
        live_bars = df[df.index > startup_cutoff]

        print(f"Warmup: {len(warmup_df)} bare, ultima: {warmup_df.index[-1]}")
        print(f"LIVE: {len(live_bars)} bare:")
        for ts, _ in live_bars.iterrows():
            print(f"  {ts}")

        sig_engine = VSESignalLive(
            strategy_cfg=cfg.strategy,
            indicator_cfg=cfg.indicator,
            symbol="ONTUSDT",
            timeframe="1h",
        )
        sig_engine.warm_up(warmup_df)

        print(f"\n🔍 Replay LIVE bare ca în container:")
        for ts, row in live_bars.iterrows():
            bar = {
                "ts": ts, "open": row["open"], "high": row["high"],
                "low": row["low"], "close": row["close"],
                "volume": row["volume"],
            }
            sig = sig_engine.update(bar)

            # Inspect raw signals direct
            buf = sig_engine._buffer
            ind = compute_indicators(buf, sig_engine._vse_cfg)
            sigdf = build_signals(ind, sig_engine._vse_cfg)
            last = sigdf.iloc[-1]

            verdict = "—"
            if sig is not None:
                verdict = f"🎯 SIGNAL {sig.side.upper()} sl_pct={sig.sl_pct*100:.3f}%"
            elif last["raw_long"] or last["raw_short"]:
                # raw signal but rejected
                if last["raw_short"]:
                    sl = last["short_stop"]
                    sl_pct = (sl - last["close"]) / last["close"] if not pd.isna(sl) else None
                    verdict = (f"🚫 raw SHORT, sl_pct="
                               f"{sl_pct*100:.3f}%" if sl_pct else "🚫 raw SHORT, sl=NaN")
                if last["raw_long"]:
                    sl = last["long_stop"]
                    sl_pct = (last["close"] - sl) / last["close"] if not pd.isna(sl) else None
                    verdict = (f"🚫 raw LONG, sl_pct="
                               f"{sl_pct*100:.3f}%" if sl_pct else "🚫 raw LONG, sl=NaN")

            print(f"  {ts}  close={row['close']:.5f}  raw_L={bool(last['raw_long'])} "
                  f"raw_S={bool(last['raw_short'])}  → {verdict}")

    finally:
        await ex.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
