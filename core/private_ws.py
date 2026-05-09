"""
private_ws.py — Bybit V5 Private WebSocket
============================================
Stream autentificat:
  - order      — schimbari status ordin
  - execution  — fiecare fill individual
  - position   — pozitie size/avgPrice/unrealizedPnl

De ce ne trebuie:
  Cand bot-ul plaseaza un order, place_market returneaza order_id imediat —
  asta NU inseamna executie. Pentru a confirma fill (sau detect Bybit-side
  SL/TP triggers, sau external close), ascultam evenimente la pozitie.

Integrare in main:
    import core.private_ws as pws
    asyncio.create_task(pws.run(on_order, on_execution, on_position))
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from typing import Awaitable, Callable, Optional

import websockets

Handler = Callable[[dict], Awaitable[None]]


def _url() -> str:
    return ("wss://stream-testnet.bybit.com/v5/private"
            if os.getenv("BYBIT_TESTNET", "0") == "1"
            else "wss://stream.bybit.com/v5/private")


def _auth_args(api_key: str, api_secret: str) -> list:
    expires = int((time.time() + 10) * 1000)
    msg = f"GET/realtime{expires}"
    sig = hmac.new(api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return [api_key, expires, sig]


async def run(on_order: Optional[Handler] = None,
              on_execution: Optional[Handler] = None,
              on_position: Optional[Handler] = None,
              topics: tuple[str, ...] = ("order", "execution", "position")) -> None:
    """
    Task infinit — connect, auth, subscribe, reconnect on error.
    """
    key = os.getenv("BYBIT_API_KEY", "")
    secret = os.getenv("BYBIT_API_SECRET", "")
    if not key or not secret:
        print("  [WS-PRIV] API keys lipsesc — stream privat dezactivat")
        return

    active_topics = []
    if "order" in topics and on_order:
        active_topics.append("order")
    if "execution" in topics and on_execution:
        active_topics.append("execution")
    if "position" in topics and on_position:
        active_topics.append("position")
    if not active_topics:
        print("  [WS-PRIV] niciun handler — skip")
        return

    while True:
        try:
            async with websockets.connect(_url(),
                                          ping_interval=None,
                                          open_timeout=15) as ws:
                # Auth
                await ws.send(json.dumps({
                    "op": "auth",
                    "args": _auth_args(key, secret),
                }))
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                auth_msg = json.loads(raw)
                if not auth_msg.get("success"):
                    print(f"  [WS-PRIV] AUTH FAILED: {auth_msg}")
                    await asyncio.sleep(30)
                    continue
                print(f"  [WS-PRIV] authenticated")

                # Subscribe
                await ws.send(json.dumps({
                    "op": "subscribe",
                    "args": active_topics,
                }))
                print(f"  [WS-PRIV] subscribed: {active_topics}")

                # Heartbeat
                async def _hb() -> None:
                    while True:
                        await asyncio.sleep(20)
                        try:
                            await ws.send(json.dumps({"op": "ping"}))
                        except Exception:
                            break

                hb = asyncio.create_task(_hb())
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("op") in ("pong", "auth", "subscribe"):
                            continue
                        topic = msg.get("topic")
                        data = msg.get("data", [])

                        handler = {
                            "order": on_order,
                            "execution": on_execution,
                            "position": on_position,
                        }.get(topic)

                        if not handler:
                            continue

                        for event in data:
                            try:
                                await handler(event)
                            except Exception:
                                import traceback
                                print(f"  [WS-PRIV] {topic} handler error:\n"
                                      f"{traceback.format_exc()}")
                finally:
                    hb.cancel()

        except Exception as e:
            print(f"  [WS-PRIV] error: {e!r} — reconnect in 5s")
            await asyncio.sleep(5)
