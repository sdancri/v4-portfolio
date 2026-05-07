"""Async ccxt wrapper pentru Bybit perpetuals (un client per subaccount).

Operații expuse:
  - load_markets / fetch_market_info(symbol) → step size, min qty, tick size.
  - set_leverage / set_margin_mode (isolated) — la startup.
  - fetch_ohlcv(symbol, timeframe, limit) — pentru warm-up indicator.
  - create_market_order, set_position_sl (V5 setTradingStop), cancel_order.
  - fetch_position, fetch_balance.
  - fetch_realized_pnl(symbol, start_ms, end_ms) — pentru reconciliere PnL.

NOTĂ: Nu am credentials live aici. Codul e schelet validat structural; pentru
testnet/mainnet trebuie completat ``.env`` și rulat ``python scripts/run_live.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import ccxt.async_support as ccxt   # type: ignore[import-untyped]

from ichimoku_bot.rate_limiter import wait_token


@dataclass
class MarketInfo:
    symbol: str
    qty_step: float
    qty_min: float
    tick_size: float
    contract_size: float = 1.0


class BybitClient:
    """Wrapper subțire peste ccxt.async pentru un singur subaccount.

    Mod de lucru:
        async with BybitClient.create(api_key, api_secret, testnet=True) as cli:
            await cli.set_leverage("KAIAUSDT", 20)
            ...
    """

    def __init__(self, exchange: Any):
        self.exchange = exchange
        self._markets_loaded = False

    @classmethod
    async def create(
        cls,
        api_key: str,
        api_secret: str,
        testnet: bool = True,
        recv_window_ms: int = 10_000,
    ) -> "BybitClient":
        ex = ccxt.bybit({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 30_000,                  # 30s (default 10s prea strict pe VPS slow)
            "options": {
                "defaultType": "swap",          # USDT perpetuals
                "recvWindow": recv_window_ms,
                # CRITICAL: load_markets fără filter încarcă TOATE categoriile
                # (spot + linear + inverse + OPTION). Endpoint-ul option e lent
                # (mii de strike-uri BTC/ETH/SOL) → timeout pe VPS-uri normale.
                # Restrângem la "linear" — KAIA/AAVE/ONT/ETH sunt USDT perpetuals.
                "fetchMarkets": ["linear"],
            },
        })
        # SKIP fetch_currencies în load_markets — endpoint privat
        # /v5/asset/coin/query-info cere scope "Wallet Read" pe API key (NU avem).
        ex.has["fetchCurrencies"] = False
        if testnet:
            ex.set_sandbox_mode(True)
        return cls(ex)

    # ── Lifecycle ────────────────────────────────────────────────────────
    async def __aenter__(self) -> "BybitClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self.exchange.close()

    # ── Markets ──────────────────────────────────────────────────────────
    async def ensure_markets(self) -> None:
        if not self._markets_loaded:
            await wait_token()
            await self.exchange.load_markets()
            self._markets_loaded = True

    async def fetch_market_info(self, symbol: str) -> MarketInfo:
        await self.ensure_markets()
        m = self.exchange.market(symbol)
        precision = m.get("precision", {}) or {}
        limits = m.get("limits", {}) or {}
        return MarketInfo(
            symbol=symbol,
            qty_step=float(precision.get("amount") or 0.0),
            qty_min=float((limits.get("amount") or {}).get("min") or 0.0),
            tick_size=float(precision.get("price") or 0.0),
            contract_size=float(m.get("contractSize") or 1.0),
        )

    # ── Leverage / margin mode ───────────────────────────────────────────
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self.ensure_markets()
        try:
            await wait_token()
            await self.exchange.set_leverage(leverage, symbol)
        except Exception as e:
            # Bybit returnează 110043 dacă leverage-ul nu s-a schimbat
            if "leverage not modified" not in str(e).lower():
                raise

    async def set_isolated_margin(self, symbol: str, leverage: int) -> None:
        await self.ensure_markets()
        try:
            await wait_token()
            await self.exchange.set_margin_mode(
                marginMode="isolated", symbol=symbol,
                params={"buyLeverage": leverage, "sellLeverage": leverage},
            )
        except Exception as e:
            if "not modified" not in str(e).lower():
                raise

    # ── OHLCV (REST, pentru warm-up) ─────────────────────────────────────
    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> list[list[float]]:
        await self.ensure_markets()
        await wait_token()
        return await self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    # ── Orders ───────────────────────────────────────────────────────────
    async def create_market_order(
        self, symbol: str, side: str, qty: float, reduce_only: bool = False
    ) -> dict[str, Any]:
        params = {"reduceOnly": reduce_only} if reduce_only else {}
        await wait_token()
        return await self.exchange.create_order(
            symbol=symbol, type="market", side=side, amount=qty, params=params
        )

    async def set_position_sl(self, symbol: str, sl_price: float) -> dict[str, Any]:
        """Setează SL pe POZIȚIE (Bybit V5 ``setTradingStop``) — atomic.

        SL-ul devine atribut al poziției, NU order separat. Avantaje:
          - La close (orice motiv), SL dispare singur — fără orphan.
          - Trailing modify: single call atomic — fără race cancel→create.
          - Reconcile simplu: verific position.stopLoss, NU separate orders.

        Args:
            symbol: simbol Bybit ("KAIAUSDT")
            sl_price: noul stop-loss. Folosește 0 pentru a clear SL.

        Bybit V5 endpoint: POST /v5/position/trading-stop
        """
        market_id = self.exchange.market(symbol)["id"]
        params = {
            "category": "linear",
            "symbol": market_id,
            "stopLoss": str(sl_price),
            "tpslMode": "Full",       # SL pe poziția completă
            "slOrderType": "Market",  # market exit la trigger
            "positionIdx": 0,         # one-way mode (n-avem hedge)
        }
        await wait_token()
        return await self.exchange.private_post_v5_position_trading_stop(params)

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        await wait_token()
        return await self.exchange.cancel_order(order_id, symbol)

    # ── Maker entry helpers (port din boilerplate, ccxt-adapted) ─────────
    async def fetch_top_of_book(self, symbol: str) -> dict[str, float] | None:
        """Best bid / best ask via REST ticker (latenta neglijabila)."""
        try:
            await wait_token()
            t = await self.exchange.fetch_ticker(symbol)
            bid = float(t.get("bid") or 0)
            ask = float(t.get("ask") or 0)
            if bid <= 0 or ask <= 0:
                return None
            return {"bid": bid, "ask": ask}
        except Exception as e:
            print(f"  [BYBIT] fetch_top_of_book {symbol} error: {e}")
            return None

    async def create_limit_postonly_order(
        self, symbol: str, side: str, price: float, qty: float,
        reduce_only: bool = False,
    ) -> str | None:
        """Plaseaza Limit PostOnly. Returneaza orderId sau None la rejection.

        PostOnly = ordinul e respins instant daca ar match-ui imediat (taker).
        Fail = piata s-a miscat -> caller-ul face fallback la Market.
        """
        params: dict[str, Any] = {"timeInForce": "PO"}
        if reduce_only:
            params["reduceOnly"] = True
        try:
            await wait_token()
            r = await self.exchange.create_order(
                symbol=symbol, type="limit", side=side,
                amount=qty, price=price, params=params,
            )
            return r.get("id")
        except Exception as e:
            # Bybit retCode 110094 / "PostOnly will take liquidity" = piata
            # s-a miscat; nu e eroare reala — caller decide fallback.
            msg = str(e).lower()
            if "postonly" in msg or "post only" in msg or "110094" in msg:
                return None
            print(f"  [BYBIT] create_limit_postonly {symbol} {side} error: {e}")
            return None

    async def maker_entry_or_market(
        self,
        symbol:      str,
        side:        str,           # "buy" / "sell" (ccxt convention)
        qty:         float,
        timeout_sec: int = 5,
        fallback:    str = "market",   # "market" | "skip"
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        """Entry MAKER cu fallback Market pe REMAINDER (port boilerplate ~80/20).

        Pasi:
          1. Plaseaza Limit PostOnly la best bid (buy) / best ask (sell).
             Daca PostOnly e respins instant -> fallback imediat.
          2. Astepta `timeout_sec` secunde, polling status. Filled -> succes maker.
          3. Timeout -> cancel + verifica cumExecQty:
             - fallback="market": Market doar pe remainder (anti-double-fill).
             - fallback="skip":   accepta partial/zero fill.

        REGULA: NU folosi pentru SL/trailing/BE — siguranta executiei conteaza
        mai mult decat economia de fee acolo.

        Returneaza dict:
            {"result": "maker"|"taker"|"mixed"|"skipped"|"failed",
             "filled_qty": float, "avg_price": float}
        """
        side_lower = side.lower()
        if side_lower not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        top = await self.fetch_top_of_book(symbol)
        px = (top.get("bid") if side_lower == "buy" else top.get("ask")) if top else None
        if not px:
            if fallback == "skip":
                return {"result": "skipped", "filled_qty": 0.0, "avg_price": 0.0}
            r = await self.create_market_order(symbol, side_lower, qty,
                                                reduce_only=reduce_only)
            return {"result": "taker" if r else "failed",
                    "filled_qty": qty if r else 0.0,
                    "avg_price": 0.0}

        # 1. Maker
        oid = await self.create_limit_postonly_order(
            symbol, side_lower, px, qty, reduce_only=reduce_only,
        )
        if not oid:
            if fallback == "skip":
                return {"result": "skipped", "filled_qty": 0.0, "avg_price": 0.0}
            r = await self.create_market_order(symbol, side_lower, qty,
                                                reduce_only=reduce_only)
            return {"result": "taker" if r else "failed",
                    "filled_qty": qty if r else 0.0,
                    "avg_price": 0.0}

        # 2. Poll order status
        for _ in range(max(1, timeout_sec)):
            await asyncio.sleep(1)
            st = await self.fetch_order(symbol, oid)
            if not st:
                continue
            status = (st.get("status") or "").lower()
            if status == "closed":  # ccxt normalizes "Filled" -> "closed"
                return {
                    "result": "maker",
                    "filled_qty": float(st.get("filled") or qty),
                    "avg_price": float(st.get("average") or px),
                }

        # 3. Timeout — cancel + remainder
        try:
            await self.cancel_order(symbol, oid)
        except Exception as e:
            print(f"  [BYBIT] cancel_order {oid} on timeout error: {e}")
        final = await self.fetch_order(symbol, oid)
        cum_qty = float((final or {}).get("filled") or 0.0)
        avg_maker = float((final or {}).get("average") or 0.0)
        remaining = max(qty - cum_qty, 0.0)

        if fallback == "skip":
            return {"result": "skipped", "filled_qty": cum_qty, "avg_price": avg_maker}

        if remaining > 0:
            await self.create_market_order(symbol, side_lower, remaining,
                                            reduce_only=reduce_only)
        if cum_qty > 0:
            return {"result": "mixed", "filled_qty": qty, "avg_price": avg_maker}
        return {"result": "taker", "filled_qty": qty, "avg_price": avg_maker}

    # ── State queries ────────────────────────────────────────────────────
    async def fetch_balance_usdt(self) -> float:
        await wait_token()
        bal = await self.exchange.fetch_balance(params={"type": "swap"})
        usdt = bal.get("USDT") or bal.get("total", {}).get("USDT")
        if usdt is None:
            return 0.0
        return float(usdt.get("free", 0.0) if isinstance(usdt, dict) else usdt)

    async def fetch_position(self, symbol: str) -> dict[str, Any] | None:
        await wait_token()
        positions = await self.exchange.fetch_positions([symbol])
        for p in positions:
            if float(p.get("contracts") or 0) > 0:
                return p
        return None

    async def fetch_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        """Toate ordinele DESCHISE (limit, stop_market, stop_limit) pe symbol."""
        try:
            await wait_token()
            return await self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            print(f"  [BYBIT] fetch_open_orders {symbol} error: {e}")
            return []

    async def fetch_order(self, symbol: str, order_id: str) -> dict[str, Any] | None:
        """Status ordin specific (folosit pentru ack + partial fill detect)."""
        try:
            await wait_token()
            return await self.exchange.fetch_order(order_id, symbol)
        except Exception as e:
            print(f"  [BYBIT] fetch_order {order_id} error: {e}")
            return None

    async def fetch_realized_pnl(
        self, symbol: str, start_ms: int, end_ms: int
    ) -> float:
        """Sum realized PnL pe symbol în interval (Bybit ``closed-pnl``)."""
        await wait_token()
        records = await self.exchange.fetch_my_trades(
            symbol=symbol, since=start_ms, limit=200
        )
        total = 0.0
        for t in records:
            ts_ms = int(t.get("timestamp") or 0)
            if ts_ms < start_ms or ts_ms > end_ms:
                continue
            info = t.get("info", {})
            pnl = info.get("closedPnl") or info.get("realizedPnl") or 0.0
            try:
                total += float(pnl)
            except (TypeError, ValueError):
                pass
        return total

    # ── PnL pentru un single trade (boilerplate-compatible) ──────────────
    async def fetch_pnl_for_trade(
        self,
        symbol: str,
        entry_ts_ms: int,
        exit_ts_ms: int,
        settle_delay_sec: float = 2.0,
    ) -> dict[str, Any]:
        """Trage PnL-ul total pentru un trade logical (port din boilerplate).

        Algoritm:
          1. Wait ``settle_delay_sec`` (Bybit înregistrează closed-pnl cu lag).
          2. Fetch ``/v5/position/closed-pnl`` cu marjă ±60-120s.
          3. Sum ``closedPnl`` pe înregistrările din interval.

        Returnează:
          {pnl, fees, n_fills, avg_entry, avg_exit, raw}
        """
        if settle_delay_sec > 0:
            await asyncio.sleep(settle_delay_sec)

        import time as _time
        start_ms = entry_ts_ms - 60_000
        # end_limit larg: fie 5min după exit, fie now+1min — whichever larger.
        # Bybit înregistrează closed-pnl cu lag până la câteva minute, mai ales
        # pe perechi cu volume mic; window strâns ratează entry-ul.
        end_limit_ms = max(exit_ts_ms + 300_000, int(_time.time() * 1000) + 60_000)

        params = {"category": "linear", "symbol": symbol, "limit": 50,
                  "startTime": str(start_ms)}
        # Retry 3× cu backoff (2s/5s/10s) dacă relevant=[] — Bybit poate înregistra
        # closed-pnl cu delay; aboard prematur lasă pnl=0 fals (port boilerplate).
        retry_delays = [0, 2, 5, 10]
        records: list = []
        relevant: list = []
        for delay in retry_delays:
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await wait_token()
                r = await self.exchange.private_get_v5_position_closed_pnl(params)
                records = (r.get("result") or {}).get("list", []) if r else []
            except Exception as e:
                print(f"  [BYBIT] fetch_pnl_for_trade error (retry={delay}s): {e}")
                continue
            relevant = [
                rec for rec in records
                if start_ms <= int(rec.get("updatedTime", 0)) <= end_limit_ms
            ]
            if relevant:
                break

        if not relevant:
            print(f"  [BYBIT] WARNING: niciun closed-pnl pentru trade "
                  f"{entry_ts_ms}-{exit_ts_ms} după {len(retry_delays)} retry")
            return {"pnl": 0.0, "fees": 0.0, "n_fills": 0,
                    "avg_entry": 0.0, "avg_exit": 0.0, "raw": []}

        pnl_total = sum(float(r["closedPnl"]) for r in relevant)
        qty_total = sum(float(r["qty"]) for r in relevant)
        avg_entry = (sum(float(r["avgEntryPrice"]) * float(r["qty"]) for r in relevant)
                     / qty_total) if qty_total else 0.0
        avg_exit = (sum(float(r["avgExitPrice"]) * float(r["qty"]) for r in relevant)
                    / qty_total) if qty_total else 0.0

        fees = 0.0
        for r in relevant:
            try:
                entry_v = float(r.get("cumEntryValue", 0))
                exit_v = float(r.get("cumExitValue", 0))
                closed_pnl = float(r["closedPnl"])
                side = r.get("side", "Buy")
                raw_pnl = (exit_v - entry_v) if side == "Buy" else (entry_v - exit_v)
                fees += abs(raw_pnl - closed_pnl)
            except Exception:
                pass

        return {
            "pnl": round(pnl_total, 4),
            "fees": round(fees, 4),
            "n_fills": len(relevant),
            "avg_entry": round(avg_entry, 4),
            "avg_exit": round(avg_exit, 4),
            "raw": relevant,
        }
