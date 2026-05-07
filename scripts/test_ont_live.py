"""Trage OHLCV recente Bybit ONTUSDT 1H și rulează logica VSE Python locală.

Verifică dacă Python ar detecta semnalul pe care Pine îl arată.
Public API (fetch_ohlcv) — nu necesită API key.
"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ccxt.async_support as ccxt
import pandas as pd

from ichimoku_bot.config import load_config
from ichimoku_bot.vse_signal_live import VSESignalLive


async def main() -> int:
    cfg = load_config("config/config.yaml")

    # Bybit public — fără API key
    ex = ccxt.bybit({
        "enableRateLimit": True,
        "timeout": 30_000,
        "options": {"defaultType": "swap", "fetchMarkets": ["linear"]},
    })
    ex.has["fetchCurrencies"] = False

    try:
        print("📡 Trag OHLCV ONTUSDT 1h ultimele 500 bare...")
        ohlcv = await ex.fetch_ohlcv("ONTUSDT", "1h", limit=500)
        if not ohlcv:
            print("❌ Nu am primit date")
            return 1

        df = pd.DataFrame(ohlcv, columns=["ts_ms", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
        df = df.set_index("ts").drop(columns=["ts_ms"])
        print(f"   ✓ {len(df)} bare: {df.index[0]} → {df.index[-1]}")
        print(f"   ultima bară: open={df['open'].iloc[-1]} high={df['high'].iloc[-1]} "
              f"low={df['low'].iloc[-1]} close={df['close'].iloc[-1]}")

        # Splitează: 400 pentru warmup, restul pentru replay barră cu barră
        warmup_df = df.iloc[:400]
        live_bars = df.iloc[400:]

        sig_engine = VSESignalLive(
            strategy_cfg=cfg.strategy,
            indicator_cfg=cfg.indicator,
            symbol="ONTUSDT",
            timeframe="1h",
        )
        sig_engine.warm_up(warmup_df)
        print(f"\n✓ warmup OK pe {len(warmup_df)} bare\n")

        print(f"🔍 Replay {len(live_bars)} bare live + verific semnale:\n")
        signals_found = []
        for ts, row in live_bars.iterrows():
            bar = {
                "ts": ts, "open": row["open"], "high": row["high"],
                "low": row["low"], "close": row["close"],
                "volume": row["volume"],
            }
            sig = sig_engine.update(bar)
            if sig is not None:
                signals_found.append((ts, sig))
                print(f"  🎯 SEMNAL {sig.side.upper()} la {ts}")
                print(f"     entry={sig.entry_price:.6f}  sl={sig.sl_price:.6f}  "
                      f"sl_pct={sig.sl_pct * 100:.3f}%")

        # Inspect last bar with raw signals (chiar dacă FILTER respinge)
        from ichimoku_bot.indicator import compute_indicators, build_signals
        all_buf = sig_engine._buffer.copy()  # type: ignore
        ind = compute_indicators(all_buf, sig_engine._vse_cfg)  # type: ignore
        sig = build_signals(ind, sig_engine._vse_cfg)  # type: ignore
        last = sig.iloc[-1]

        print(f"\n═══ ULTIMA BARĂ ({all_buf.index[-1]}) ═══")
        print(f"  close: {last['close']:.6f}")
        print(f"  raw_long:  {bool(last['raw_long'])}")
        print(f"  raw_short: {bool(last['raw_short'])}")
        print(f"  long_stop:  {last.get('long_stop', 'N/A')}")
        print(f"  short_stop: {last.get('short_stop', 'N/A')}")

        if last["raw_short"]:
            sl = last["short_stop"]
            sl_pct = (sl - last["close"]) / last["close"]
            print(f"\n  → raw SHORT detectat. SL={sl:.6f}, sl_pct={sl_pct * 100:.3f}%")
            if sl_pct < cfg.strategy.sl_min_pct:
                print(f"  ❌ FILTER REJECT: sl_pct {sl_pct*100:.3f}% < min {cfg.strategy.sl_min_pct*100:.2f}%")
            elif sl_pct > cfg.strategy.sl_max_pct:
                print(f"  ❌ FILTER REJECT: sl_pct {sl_pct*100:.3f}% > max {cfg.strategy.sl_max_pct*100:.2f}%")
            else:
                print(f"  ✓ SL bounds OK")

        if last["raw_long"]:
            sl = last["long_stop"]
            sl_pct = (last["close"] - sl) / last["close"]
            print(f"\n  → raw LONG detectat. SL={sl:.6f}, sl_pct={sl_pct * 100:.3f}%")
            if sl_pct < cfg.strategy.sl_min_pct:
                print(f"  ❌ FILTER REJECT: sl_pct {sl_pct*100:.3f}% < min {cfg.strategy.sl_min_pct*100:.2f}%")
            elif sl_pct > cfg.strategy.sl_max_pct:
                print(f"  ❌ FILTER REJECT: sl_pct {sl_pct*100:.3f}% > max {cfg.strategy.sl_max_pct*100:.2f}%")
            else:
                print(f"  ✓ SL bounds OK")

        print(f"\n═══ SUMMARY ULTIMELE 100 BARE ═══")
        print(f"  Semnale valide emise: {len(signals_found)}")
        if signals_found:
            for ts, s in signals_found[-5:]:
                print(f"    {ts}  {s.side}  sl_pct={s.sl_pct*100:.3f}%")
    finally:
        await ex.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
