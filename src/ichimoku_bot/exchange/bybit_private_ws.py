"""Bybit V5 Private WebSocket — instance-based pentru multi-subaccount.

Adaptare din boilerplate-ul `core/private_ws.py` — primește api_key/secret
ca parametri (NU env vars), pentru a funcționa cu un client per subaccount.

Topicuri:
  - ``order``     — schimbări de status ordin (Filled/Rejected/Cancelled).
  - ``execution`` — fiecare fill (qty, price, fee).
  - ``position``  — update poziție (size, avgPrice, unrealizedPnl).

De ce:
  Pe close real (SL hit pe Bybit), bot-ul detectează prin ``execution`` event
  + ``position`` event (size=0). Apoi apelează ``fetch_pnl_for_trade`` ca să
  ia PnL real, iar ``state.account += real_pnl`` (regula 5).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable

import websockets

Handler = Callable[[dict], Awaitable[None]]


def _url(testnet: bool) -> str:
    return (
        "wss://stream-testnet.bybit.com/v5/private"
        if testnet
        else "wss://stream.bybit.com/v5/private"
    )


def _auth_args(api_key: str, api_secret: str) -> list:
    """Construiește [key, expires, signature] pentru op=auth."""
    expires = int((time.time() + 10) * 1000)
    msg = f"GET/realtime{expires}"
    sig = hmac.new(api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return [api_key, expires, sig]


class BybitPrivateWS:
    """Stream privat Bybit per subaccount.

    Args:
        api_key: cheia subaccount-ului
        api_secret: secret subaccount
        testnet: True pentru testnet
        on_order, on_execution, on_position: handler-i async (None = skip)
        log_prefix: pentru logs (de ex numele subaccount-ului)
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        on_order: Handler | None = None,
        on_execution: Handler | None = None,
        on_position: Handler | None = None,
        log_prefix: str = "",
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.on_order = on_order
        self.on_execution = on_execution
        self.on_position = on_position
        self.log_prefix = log_prefix or "ws-priv"
        self._stop = asyncio.Event()

    async def run(self) -> None:
        topics: list[str] = []
        if self.on_order:
            topics.append("order")
        if self.on_execution:
            topics.append("execution")
        if self.on_position:
            topics.append("position")
        if not topics:
            print(f"  [{self.log_prefix}] niciun handler — skip private WS")
            return

        url = _url(self.testnet)
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url, ping_interval=None, open_timeout=15
                ) as ws:
                    # 1. Auth
                    await ws.send(json.dumps({
                        "op": "auth",
                        "args": _auth_args(self.api_key, self.api_secret),
                    }))
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    auth_msg = json.loads(raw)
                    if not auth_msg.get("success"):
                        print(f"  [{self.log_prefix}] AUTH FAILED: {auth_msg}")
                        await asyncio.sleep(30)
                        continue
                    print(f"  [{self.log_prefix}] authenticated")

                    # 2. Subscribe
                    await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                    print(f"  [{self.log_prefix}] subscribed: {topics}")
                    backoff = 1.0

                    # 3. Heartbeat
                    async def _hb() -> None:
                        try:
                            while True:
                                await asyncio.sleep(20)
                                await ws.send(json.dumps({"op": "ping"}))
                        except Exception:
                            return

                    hb_task = asyncio.create_task(_hb())
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("op") in ("pong", "auth", "subscribe"):
                                continue
                            topic = msg.get("topic")
                            data = msg.get("data", [])
                            handler = {
                                "order": self.on_order,
                                "execution": self.on_execution,
                                "position": self.on_position,
                            }.get(topic)
                            if not handler:
                                continue
                            for event in data:
                                try:
                                    await handler(event)
                                except Exception:
                                    import traceback
                                    print(
                                        f"  [{self.log_prefix}] {topic} handler error:\n"
                                        f"{traceback.format_exc()}"
                                    )
                    finally:
                        hb_task.cancel()
            except Exception as e:
                print(f"  [{self.log_prefix}] error: {e!r} — reconnect in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def stop(self) -> None:
        self._stop.set()
