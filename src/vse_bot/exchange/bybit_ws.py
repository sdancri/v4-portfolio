"""Bybit kline WebSocket client (public, multi-symbol).

Subscribe la un set de topicuri ``kline.{interval}.{symbol}`` și emite, prin
callback async, dict-uri ``{symbol, timeframe, ts, open, high, low, close,
volume, confirmed}``. Heartbeat la 20s; auto-reconnect cu backoff.

Pentru replay-mode nu folosim WS — ci doar la run_live.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import websockets

WS_PUBLIC_MAINNET = "wss://stream.bybit.com/v5/public/linear"
WS_PUBLIC_TESTNET = "wss://stream-testnet.bybit.com/v5/public/linear"


_TF_BYBIT = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1w": "W",
}


def _bybit_interval(timeframe: str) -> str:
    if timeframe not in _TF_BYBIT:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return _TF_BYBIT[timeframe]


class BybitKlineWS:
    """WebSocket public Bybit pentru kline streams.

    Callback semnătura: ``async def on_bar(bar: dict) -> None``.
    Bar-urile NEÎNCHISE (``confirmed=False``) sunt și ele trimise — strategia
    decide ce ignoră (VSE folosește doar bare confirmed).
    """

    def __init__(
        self,
        subscriptions: list[tuple[str, str]],   # [(symbol, timeframe), ...]
        on_bar: Callable[[dict], Awaitable[None]],
        testnet: bool = True,
    ) -> None:
        self.subscriptions = subscriptions
        self.on_bar = on_bar
        self.url = WS_PUBLIC_TESTNET if testnet else WS_PUBLIC_MAINNET
        self._stop = asyncio.Event()

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
                    hb_task = asyncio.create_task(self._heartbeat(ws))
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

    async def _dispatch(self, topic: str, data: list[dict]) -> None:
        # topic = kline.60.KAIAUSDT
        parts = topic.split(".")
        if len(parts) != 3:
            return
        interval, symbol = parts[1], parts[2]
        timeframe = _interval_to_tf(interval)
        for k in data:
            bar = {
                "symbol": symbol,
                "timeframe": timeframe,
                "ts_ms": int(k["start"]),
                "open": float(k["open"]),
                "high": float(k["high"]),
                "low": float(k["low"]),
                "close": float(k["close"]),
                "volume": float(k.get("volume", 0.0)),
                "confirmed": bool(k.get("confirm", False)),
            }
            await self.on_bar(bar)


def _interval_to_tf(interval: str) -> str:
    for tf, iv in _TF_BYBIT.items():
        if iv == interval:
            return tf
    return interval
