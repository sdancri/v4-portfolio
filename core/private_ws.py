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

    # Triple defense vs WS zombie (acelasi pattern ca public_ws_loop in main.py):
    #  (1) ping_interval=20, ping_timeout=10  →  library WS protocol ping
    #  (2) _hb trimite Bybit app-ping (compat istoric)
    #  (3) _watchdog forteaza close daca nu primim niciun mesaj > zombie_timeout
    # CRITIC pe private WS: daca Bybit opreste silent stream-ul, bot-ul NU mai
    # primeste Filled/Rejected/position events → reconcile contaminat si
    # on_order_event/on_execution_event silent broken zile la rand.
    ws_zombie_timeout = int(os.getenv("WS_ZOMBIE_TIMEOUT", "60"))

    while True:
        try:
            async with websockets.connect(_url(),
                                          ping_interval=20,
                                          ping_timeout=10,
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

                # Zombie detection — pe private, perioade lungi "no msg" sunt
                # normale fara trade-uri active, dar pong-urile la 20s mentin
                # last_msg_ts proaspat. Watchdog detecteaza absenta SI a
                # pong-urilor (zombie real).
                last_msg_ts = time.time()

                # Heartbeat app-level (Bybit inchide la >30s silence)
                async def _hb() -> None:
                    while True:
                        await asyncio.sleep(20)
                        try:
                            await ws.send(json.dumps({"op": "ping"}))
                        except Exception:
                            break

                async def _watchdog() -> None:
                    while True:
                        await asyncio.sleep(10)
                        idle = time.time() - last_msg_ts
                        if idle > ws_zombie_timeout:
                            print(f"  [WS-PRIV] ZOMBIE detected: no msg "
                                  f"{idle:.0f}s > {ws_zombie_timeout}s — "
                                  f"forcing close → reconnect")
                            try:
                                await ws.close()
                            except Exception:
                                pass
                            return

                hb = asyncio.create_task(_hb())
                wd = asyncio.create_task(_watchdog())
                try:
                    async for raw in ws:
                        last_msg_ts = time.time()
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
                    wd.cancel()

        except Exception as e:
            print(f"  [WS-PRIV] error: {e!r} — reconnect in 5s")
            await asyncio.sleep(5)
