"""Bybit kline WebSocket client (public, multi-symbol) cu STALE detection.

Features:
  - Subscribe la topicuri ``kline.{interval}.{symbol}``.
  - Emite prin callback async dict-uri cu OHLC + confirmed flag.
  - Heartbeat ping la 20s.
  - Auto-reconnect cu backoff la error.
  - **Stale detection**: watchdog periodic (every 60s) verifică ultima bară
    confirmed primită per (symbol, tf). Dacă > 2 × tf_seconds fără bară →
    callback ``on_stale`` + force reconnect.

TF seconds:
  1m=60, 5m=300, 15m=900, 1h=3600, 2h=7200, 4h=14400, 1d=86400
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

import websockets

WS_PUBLIC_MAINNET = "wss://stream.bybit.com/v5/public/linear"
WS_PUBLIC_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"


_TF_BYBIT = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1w": "W",
}

_TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "12h": 43200,
    "1d": 86400, "1w": 604800,
}


def _bybit_interval(timeframe: str) -> str:
    if timeframe not in _TF_BYBIT:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return _TF_BYBIT[timeframe]


def _tf_to_seconds(timeframe: str) -> int:
    return _TF_SECONDS.get(timeframe, 3600)


class BybitKlineWS:
    """WebSocket public Bybit pentru kline streams cu stale watchdog.

    Callback ``on_bar``:    async def f(bar: dict) -> None
    Callback ``on_stale``:  async def f(symbol: str, tf: str, age_sec: float) -> None

    Stale threshold: ``stale_factor × tf_seconds`` (default 2.0).
    Pe stale detect: emite ``on_stale`` și **închide WS-ul** → reconnect prin loop.
    """

    def __init__(
        self,
        subscriptions: list[tuple[str, str]],
        on_bar: Callable[[dict], Awaitable[None]],
        testnet: bool = True,
        on_stale: Callable[[str, str, float], Awaitable[None]] | None = None,
        stale_factor: float = 2.0,
        watchdog_interval_sec: float = 60.0,
    ) -> None:
        self.subscriptions = subscriptions
        self.on_bar = on_bar
        self.on_stale = on_stale
        self.url = WS_PUBLIC_TESTNET if testnet else WS_PUBLIC_MAINNET
        self.stale_factor = stale_factor
        self.watchdog_interval = watchdog_interval_sec
        self._stop = asyncio.Event()
        # Track ultima bară CONFIRMED primită per (symbol, tf). Inițial = now
        # (ca să nu trigger stale înainte ca prima bară să apară natural).
        self._last_bar_ts: dict[tuple[str, str], float] = {
            (sym, tf): time.time() for sym, tf in subscriptions
        }

    async def run(self) -> None:
        topics = [
            f"kline.{_bybit_interval(tf)}.{sym}"
            for sym, tf in self.subscriptions
        ]
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self.url, ping_interval=None, open_timeout=15
                ) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                    backoff = 1.0
                    # Reset timestamps la (re)connect — dăm bot-ului benefit of doubt
                    now = time.time()
                    for k in self._last_bar_ts:
                        self._last_bar_ts[k] = now

                    hb_task = asyncio.create_task(self._heartbeat(ws))
                    wd_task = asyncio.create_task(self._watchdog(ws))
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("op") in ("pong", "subscribe"):
                                continue
                            topic = msg.get("topic", "")
                            if not topic.startswith("kline."):
                                continue
                            await self._dispatch(topic, msg.get("data", []))
                    finally:
                        hb_task.cancel()
                        wd_task.cancel()
            except Exception as e:
                print(f"[ws] {e!r} — reconnect in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def stop(self) -> None:
        self._stop.set()

    async def _heartbeat(self, ws) -> None:  # type: ignore[no-untyped-def]
        try:
            while True:
                await asyncio.sleep(20)
                await ws.send(json.dumps({"op": "ping"}))
        except Exception:
            return

    async def _watchdog(self, ws) -> None:  # type: ignore[no-untyped-def]
        """Verifică periodic dacă vreun (symbol, tf) e stale.

        Stale = (now - last_bar_ts) > stale_factor × tf_seconds.
        Pe stale: emite on_stale callback + închide ws (force reconnect).
        """
        try:
            while True:
                await asyncio.sleep(self.watchdog_interval)
                now = time.time()
                stale: list[tuple[str, str, float]] = []
                for (sym, tf), last_ts in self._last_bar_ts.items():
                    age = now - last_ts
                    threshold = self.stale_factor * _tf_to_seconds(tf)
                    if age > threshold:
                        stale.append((sym, tf, age))
                if stale:
                    for sym, tf, age in stale:
                        print(
                            f"  [WS-WATCHDOG] STALE {sym} {tf}: "
                            f"{age:.0f}s fără bară confirmed (threshold "
                            f"{self.stale_factor}×{_tf_to_seconds(tf)}s)"
                        )
                        if self.on_stale:
                            try:
                                await self.on_stale(sym, tf, age)
                            except Exception as e:
                                print(f"  [WS-WATCHDOG] on_stale error: {e!r}")
                    # Force reconnect — închide ws-ul, run() loop reconnectează
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    return
        except Exception:
            return

    async def _dispatch(self, topic: str, data: list[dict]) -> None:
        # topic = kline.60.KAIAUSDT
        parts = topic.split(".")
        if len(parts) != 3:
            return
        interval, symbol = parts[1], parts[2]
        timeframe = _interval_to_tf(interval)
        for k in data:
            confirmed = bool(k.get("confirm", False))
            bar = {
                "symbol": symbol,
                "timeframe": timeframe,
                "ts_ms": int(k["start"]),
                "open": float(k["open"]),
                "high": float(k["high"]),
                "low": float(k["low"]),
                "close": float(k["close"]),
                "volume": float(k.get("volume", 0.0)),
                "confirmed": confirmed,
            }
            # Update watchdog DOAR pe bare confirmed
            if confirmed:
                self._last_bar_ts[(symbol, timeframe)] = time.time()
            await self.on_bar(bar)


def _interval_to_tf(interval: str) -> str:
    for tf, iv in _TF_BYBIT.items():
        if iv == interval:
            return tf
    return interval
