"""FastAPI chart server pentru un SubaccountRunner.

Servește chart_live.html + API + WebSocket broadcast pentru clienți.

Reguli implementate:
  - 4: port unic (CHART_PORT env), Bucharest TZ (config în chart HTML).
  - 8: chart_live.html template din boilerplate.
  - 10: prima bară LIVE = first_candle_ts (indicatori sunt calculați din
    warmup, dar candele afișate doar de la prima pornire).
  - 12: timestamp_ms din warmup → secunde live (conversion la broadcast).
  - 13: chart afișează DOAR SL/TP/PnL live, NU indicatori (regula nouă).

Multi-pair note: subaccount-ul are 2 perechi (ex KAIA+AAVE). Chart afișează
candele pe perechea PRINCIPALĂ (prima din config). Trade list și equity
curve arată ambele perechi.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from vse_bot.main import SubaccountRunner


def create_app(runner: "SubaccountRunner") -> FastAPI:
    """Construiește FastAPI app pentru un SubaccountRunner."""
    base = Path(__file__).resolve().parents[2]
    static_dir = base / "static"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Background tasks pornite de main.py — aici doar yield.
        yield

    app = FastAPI(
        title=f"VSE chart — {runner.sub_cfg.name}",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(str(static_dir / "chart_live.html"))

    @app.get("/api/init")
    async def api_init() -> JSONResponse:
        """Init payload: candles + active position + bot state."""
        return JSONResponse({
            "subaccount": runner.sub_cfg.name,
            "primary_pair": runner.primary_pair_key()[0] if runner.primary_pair_key() else "",
            "candles": runner.candles_live,
            "active_position": runner.active_position_payload(),
            **runner.bot.init_payload(),
        })

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        last = runner.candles_live[-1] if runner.candles_live else None
        return {
            "bot_name": os.getenv("BOT_NAME", runner.sub_cfg.name),
            "subaccount": runner.sub_cfg.name,
            "candles_total": len(runner.candles_live),
            "last_candle_ts": last[0] if last else None,
            "connected_clients": len(runner.clients),
            "summary": runner.bot.summary(),
            "state": {
                "equity": runner.state.equity,
                "balance_broker": runner.state.balance_broker,
                "pool_used": runner.state.pool_used,
                "cycle_num": runner.state.cycle_num,
                "reset_count": runner.state.reset_count,
            },
        }

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        runner.clients.add(ws)
        try:
            while True:
                # keep-alive (clienții nu trimit comenzi)
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            runner.clients.discard(ws)

    return app


async def broadcast(runner: "SubaccountRunner", payload: dict) -> None:
    """Trimite payload JSON la toți clienții WebSocket. Idempotent la dead clients."""
    if not runner.clients:
        return
    msg = json.dumps(payload, default=str)
    dead: set = set()
    for ws in list(runner.clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    runner.clients.difference_update(dead)


async def serve_chart(app: FastAPI, port: int) -> None:
    """Pornește uvicorn (server) ca task async — folosit de main.py."""
    import uvicorn
    config = uvicorn.Config(
        app, host="0.0.0.0", port=port, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    await server.serve()
