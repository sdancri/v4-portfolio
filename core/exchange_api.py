"""
exchange_api.py — Bybit V5 API client (raw httpx)
==================================================

Foloseste httpx DIRECT (NU ccxt) — calls explicite cu category=linear pe
fiecare endpoint, fara abstractii ccxt unificat.

API:
  Market data:
    get_ticker(symbol)                       -> {bid1, ask1, last, mark}
    get_kline(symbol, interval, limit)       -> list[list] [ts,o,h,l,c,v,turnover]
    get_market_info(symbol)                  -> {qty_step, qty_prec, price_prec}

  Account:
    get_balance()                            -> float (USDT available)
    get_position(symbol)                     -> {size, avgPrice, side, ...} | None

  Orders:
    place_market(symbol, side, qty, reduce_only)            -> orderId | None
    place_limit_postonly(symbol, side, price, qty, ...)     -> orderId | None
    maker_entry_or_market(symbol, side, qty, ...)           -> {result, filled_qty, avg_price}
    chase_close(symbol, direction, ...)                     -> bool (force-close)
    cancel_order(symbol, order_id)
    cancel_all(symbol)
    get_open_orders(symbol)                                 -> list[dict]
    get_order_status(symbol, order_id)                      -> dict | None
    set_leverage(symbol, leverage)                          -> bool
    set_position_sl(symbol, sl_price, tp_price=None, qty=None) -> bool
                                                             # qty given → TP=Limit (maker)

  PnL:
    fetch_closed_pnl(symbol, start_ms, limit)               -> list[dict]
    fetch_pnl_for_trade(symbol, entry_ts_ms, exit_ts_ms)    -> {pnl, fees, avg_entry, avg_exit, ...}

Side convention: Bybit V5 native — "Buy" / "Sell" (capitalizat). Pentru long
inchis trimitem "Sell"; pentru short inchis trimitem "Buy".

Env vars:
    BYBIT_API_KEY, BYBIT_API_SECRET — credentials
    BYBIT_TESTNET=1                 — testnet (default mainnet)
    BYBIT_CATEGORY=linear           — default
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import time
import urllib.parse
from typing import Optional

import httpx

from core import rate_limiter as rl


# ----------------------------------------------------------------------------
# Config & auth
# ----------------------------------------------------------------------------

def _cat() -> str:
    return os.getenv("BYBIT_CATEGORY", "linear")


def _base() -> str:
    return ("https://api-testnet.bybit.com"
            if os.getenv("BYBIT_TESTNET", "0") == "1"
            else "https://api.bybit.com")


def _creds() -> tuple[str, str]:
    return os.getenv("BYBIT_API_KEY", ""), os.getenv("BYBIT_API_SECRET", "")


def _sign(key: str, secret: str, payload: str) -> dict:
    ts = str(int(time.time() * 1000))
    recv = "5000"
    msg = ts + key + recv + payload
    sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "X-BAPI-API-KEY": key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-SIGN": sig,
        "X-BAPI-RECV-WINDOW": recv,
        "Content-Type": "application/json",
    }


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------

async def _post(endpoint: str, body: dict) -> Optional[dict]:
    key, secret = _creds()
    if not key or not secret:
        print(f"  [BYBIT] API keys not set — skip {endpoint}")
        return None
    body_str = json.dumps(body)
    try:
        await rl.wait_token()
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{_base()}{endpoint}",
                             headers=_sign(key, secret, body_str),
                             content=body_str)
            try:
                d = r.json()
            except Exception as je:
                print(f"  [BYBIT] {endpoint} HTTP {r.status_code} non-JSON: "
                      f"{(r.text or '<empty>')[:200]!r} ({je})")
                return None
        if d.get("retCode") != 0:
            print(f"  [BYBIT] {endpoint} {d['retCode']}: {d['retMsg']}")
            return None
        return d.get("result")
    except Exception as e:
        print(f"  [BYBIT] {endpoint} error: {e}")
        return None


async def _get(endpoint: str, params: dict, signed: bool = True) -> Optional[dict]:
    key, secret = _creds()
    try:
        await rl.wait_token()
        async with httpx.AsyncClient(timeout=10) as c:
            if signed:
                if not key or not secret:
                    return None
                qs = urllib.parse.urlencode(params)
                r = await c.get(f"{_base()}{endpoint}",
                                headers=_sign(key, secret, qs),
                                params=params)
            else:
                r = await c.get(f"{_base()}{endpoint}", params=params)
            d = r.json()
        if d.get("retCode") != 0:
            print(f"  [BYBIT] {endpoint} {d['retCode']}: {d['retMsg']}")
            return None
        return d.get("result")
    except Exception as e:
        print(f"  [BYBIT] {endpoint} error: {e}")
        return None


# ----------------------------------------------------------------------------
# Market info cache (per symbol qty_step / price_step)
# ----------------------------------------------------------------------------

_market_cache: dict[str, dict] = {}


async def get_market_info(symbol: str) -> dict:
    """Returns {qty_step, qty_prec, price_prec, min_qty, tick_size}. Cached."""
    if symbol in _market_cache:
        return _market_cache[symbol]
    r = await _get("/v5/market/instruments-info",
                   {"category": _cat(), "symbol": symbol},
                   signed=False)
    if not r or not r.get("list"):
        return {"qty_step": 0.001, "qty_prec": 3, "price_prec": 2,
                "min_qty": 0.0, "tick_size": 0.01}
    info = r["list"][0]
    lot = info["lotSizeFilter"]
    px = info["priceFilter"]
    qty_step = float(lot["qtyStep"])
    tick_size = float(px["tickSize"])

    def _prec(step: float) -> int:
        s = f"{step:.10f}".rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0

    out = {
        "qty_step": qty_step,
        "qty_prec": _prec(qty_step),
        "price_prec": _prec(tick_size),
        "min_qty": float(lot["minOrderQty"]),
        "tick_size": tick_size,
    }
    _market_cache[symbol] = out
    return out


def _fmt_qty(qty: float, qty_prec: int) -> str:
    return f"{qty:.{qty_prec}f}"


def _fmt_price(price: float, price_prec: int) -> str:
    return f"{price:.{price_prec}f}"


def round_qty_down(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor(qty / step) * step


# ============================================================================
# Market data
# ============================================================================

async def get_ticker(symbol: str) -> Optional[dict]:
    r = await _get("/v5/market/tickers",
                   {"category": _cat(), "symbol": symbol},
                   signed=False)
    if not r or not r.get("list"):
        return None
    try:
        t = r["list"][0]
        return {
            "last": float(t["lastPrice"]),
            "bid1": float(t["bid1Price"]),
            "ask1": float(t["ask1Price"]),
            "mark": float(t.get("markPrice", t["lastPrice"])),
        }
    except Exception:
        return None


async def get_kline(symbol: str, interval: str, limit: int = 1000,
                    start: Optional[int] = None,
                    end: Optional[int] = None) -> list[list]:
    """
    interval: "1","3","5","15","30","60","120","240","360","720","D","W","M"
    Bybit returns list[[ts_ms, open, high, low, close, volume, turnover]] DESC.
    Returnam ASCENDING (sortat dupa ts) — convenabil pentru indicators.
    """
    params = {"category": _cat(), "symbol": symbol,
              "interval": interval, "limit": limit}
    if start is not None:
        params["start"] = int(start)
    if end is not None:
        params["end"] = int(end)
    r = await _get("/v5/market/kline", params, signed=False)
    bars = r.get("list", []) if r else []
    # Bybit returneaza DESC; reverse pentru ASC
    bars = list(reversed(bars))
    # Cast strings → floats, ts_ms → int
    return [
        [int(b[0]), float(b[1]), float(b[2]), float(b[3]),
         float(b[4]), float(b[5]), float(b[6])]
        for b in bars
    ]


# ============================================================================
# Account
# ============================================================================

async def get_balance() -> Optional[float]:
    """USDT available — UNIFIED account."""
    r = await _get("/v5/account/wallet-balance",
                   {"accountType": "UNIFIED", "coin": "USDT"})
    if not r:
        return None
    try:
        for coin in r["list"][0]["coin"]:
            if coin["coin"] == "USDT":
                return float(coin["availableToWithdraw"] or coin["walletBalance"])
    except Exception:
        pass
    return None


async def get_position(symbol: str) -> Optional[dict]:
    """
    Full position info: size, avgPrice, side ("Buy"/"Sell"/""), unrealisedPnl, etc.
    Returns None if no position or API error.
    """
    r = await _get("/v5/position/list",
                   {"category": _cat(), "symbol": symbol})
    if not r:
        return None
    for p in r.get("list", []):
        if p["symbol"] == symbol and float(p.get("size", 0)) > 0:
            return p
    return None


# ============================================================================
# Orders
# ============================================================================

async def place_market(symbol: str, side: str, qty: float,
                       reduce_only: bool = False) -> Optional[str]:
    """side: 'Buy' / 'Sell' (Bybit native, capitalized)."""
    info = await get_market_info(symbol)
    r = await _post("/v5/order/create", {
        "category": _cat(),
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": _fmt_qty(qty, info["qty_prec"]),
        "timeInForce": "IOC",
        "reduceOnly": reduce_only,
    })
    return r.get("orderId") if r else None


async def place_limit_postonly(symbol: str, side: str, price: float, qty: float,
                               reduce_only: bool = False) -> Optional[str]:
    info = await get_market_info(symbol)
    r = await _post("/v5/order/create", {
        "category": _cat(),
        "symbol": symbol,
        "side": side,
        "orderType": "Limit",
        "price": _fmt_price(price, info["price_prec"]),
        "qty": _fmt_qty(qty, info["qty_prec"]),
        "timeInForce": "PostOnly",
        "reduceOnly": reduce_only,
    })
    return r.get("orderId") if r else None


async def cancel_order(symbol: str, order_id: Optional[str]) -> None:
    if not order_id:
        return
    await _post("/v5/order/cancel", {
        "category": _cat(), "symbol": symbol, "orderId": order_id,
    })


async def cancel_all(symbol: str) -> None:
    await _post("/v5/order/cancel-all", {"category": _cat(), "symbol": symbol})


async def get_open_orders(symbol: str) -> list[dict]:
    r = await _get("/v5/order/realtime",
                   {"category": _cat(), "symbol": symbol})
    return r.get("list", []) if r else []


async def get_order_status(symbol: str, order_id: str) -> Optional[dict]:
    r = await _get("/v5/order/realtime",
                   {"category": _cat(), "symbol": symbol, "orderId": order_id})
    if not r or not r.get("list"):
        return None
    return r["list"][0]


async def set_leverage(symbol: str, leverage: int) -> bool:
    """Sets leverage for both buy and sell side. Bybit: 'leverage already set' is OK."""
    r = await _post("/v5/position/set-leverage", {
        "category": _cat(),
        "symbol": symbol,
        "buyLeverage": str(leverage),
        "sellLeverage": str(leverage),
    })
    # _post returns None on retCode != 0 — for already-set we'll see retMsg in log;
    # treat as success.
    return True


async def set_position_sl(symbol: str, sl_price: float,
                          tp_price: Optional[float] = None,
                          qty: Optional[float] = None) -> bool:
    """
    setTradingStop: atasaza SL (si optional TP) la pozitia DESCHISA, atomic.
    Bybit triggereaza intra-bar pe LastPrice (high/low).

    Pattern fee management (din boilerplate):
        Action | Type                                   | Fee
        -------+----------------------------------------+----------------------------
        Entry  | maker_entry_or_market                  | maker 0.01% / taker 0.055%
        SL     | atomic Bybit Market (siguranta gap)    | taker 0.055%
        TP     | atomic Bybit Limit cu tpLimitPrice     | maker 0.020%
               | (~0.05% past trigger pt fill prob)     |

    SL = ALWAYS Market (siguranta executiei pe gap, nu economisim aici).

    TP = Limit (maker) cand `qty` e dat. La trigger, Bybit plaseaza intern un
    Limit reduce-only la `tpLimitPrice` = tp_price ± 0.05% (long: above, short:
    below) → ramane in book ca maker, nu cross spread. Daca pretul whipsaza
    inapoi prin TP fara fill volume, limit-ul "stick" pana revine — backstop
    e signal exit / time-exit din strategia.

    Constraint Bybit: tpOrderType=Limit cere tpslMode=Partial cu tpSize/slSize
    explicite. tpslMode=Full + Limit = respins ("only Market when Full").

    Direction inferata din sl_price vs tp_price:
        LONG:  sl < entry < tp  → tpLimitPrice > tp (slightly above)
        SHORT: tp < entry < sl  → tpLimitPrice < tp (slightly below)

    Fallback: qty=None → TP Market (legacy). Apelat asa cand caller nu cunoaste
    qty (ex: post-fill before bar_meta complete).

    Retry 3x backoff 1/2/4s — race condition place_market → set-trading-stop
    (Bybit poate avea cateva sute ms pana cand pozitia apare activa).
    """
    info = await get_market_info(symbol)
    payload: dict = {
        "category": _cat(),
        "symbol": symbol,
        "positionIdx": 0,  # one-way mode
    }

    if tp_price is not None and qty is not None and qty > 0:
        # MAKER TP via Limit + Partial mode (economisesc 0.035%/hit)
        is_long = sl_price < tp_price
        tp_limit = tp_price * (1 + 0.0005) if is_long else tp_price * (1 - 0.0005)
        qty_str = _fmt_qty(qty, info["qty_prec"])
        payload.update({
            "tpslMode": "Partial",
            "stopLoss": _fmt_price(sl_price, info["price_prec"]),
            "slTriggerBy": "LastPrice",
            "slOrderType": "Market",
            "slSize": qty_str,
            "takeProfit": _fmt_price(tp_price, info["price_prec"]),
            "tpTriggerBy": "LastPrice",
            "tpOrderType": "Limit",
            "tpLimitPrice": _fmt_price(tp_limit, info["price_prec"]),
            "tpSize": qty_str,
        })
    elif tp_price is not None:
        # Fallback legacy: TP Market (qty necunoscut)
        payload.update({
            "tpslMode": "Full",
            "stopLoss": _fmt_price(sl_price, info["price_prec"]),
            "slTriggerBy": "LastPrice",
            "slOrderType": "Market",
            "takeProfit": _fmt_price(tp_price, info["price_prec"]),
            "tpTriggerBy": "LastPrice",
            "tpOrderType": "Market",
        })
    else:
        # SL only
        payload.update({
            "tpslMode": "Full",
            "stopLoss": _fmt_price(sl_price, info["price_prec"]),
            "slTriggerBy": "LastPrice",
            "slOrderType": "Market",
        })

    for attempt, delay in enumerate([0, 1.0, 2.0, 4.0]):
        if delay > 0:
            await asyncio.sleep(delay)
        r = await _post("/v5/position/trading-stop", payload)
        if r is not None:
            if attempt > 0:
                print(f"  [BYBIT] set_position_sl OK dupa retry #{attempt}")
            return True
        print(f"  [BYBIT] set_position_sl FAIL #{attempt+1}/4")
    print(f"  [BYBIT] set_position_sl FAILED definitiv pe {symbol} sl={sl_price} — "
          f"pozitia ruleaza FARA protectie!")
    # Alerta CRITICA via Telegram — user-ul trebuie sa stie imediat (loguri
    # nu sunt checked in real time pe productie). Best-effort: tg.send fail
    # NU altereaza return-ul (caller-ul vede oricum False si stie sa actioneze).
    # Import local pt evitare circular dependency.
    try:
        from core import telegram_bot as tg
        tp_line = f"\n<b>tp:</b> {tp_price}" if tp_price is not None else ""
        await tg.send_critical(
            f"{symbol} SL NOT SET",
            f"<b>set_position_sl FAILED</b> dupa 4 retry-uri (~7s)\n"
            f"<b>sl:</b> {sl_price}{tp_line}\n"
            f"<b>Pozitia ruleaza FARA protectie.</b>\n"
            f"Reconcilierea va detecta la primul close si va force chase_close.",
            symbol=symbol,
        )
    except Exception as tg_e:
        print(f"  [BYBIT] tg.send_critical failed: {tg_e}")
    return False


# ============================================================================
# Maker entry helper — Limit PostOnly cu fallback Market pe remainder
# ============================================================================
#
# Bybit V5 nu are "chase order" nativ. Pattern "try maker once, fallback
# Market pe ce a ramas dupa timeout" — captureaza ~80-90% din economia de fee
# fata de un chase complet, ~50 linii.
#
# Bug-uri evitate:
#   1. NU folosim get_position pt detectare fill — fragil cu pyramidari
#      (pozitia preexistenta poate face check-ul fals-pozitiv).
#      In schimb interogam orderStatus din /v5/order/realtime.
#   2. La timeout, market doar pe `qty - cumExecQty` (NU pe qty intreg) —
#      altfel double-fill garantat la partial.
#   3. Pe PostOnly rejection (piata s-a miscat in fereastra de plasare),
#      place_limit_postonly returneaza None instant → fallback Market imediat,
#      nu astepta timeout-ul degeaba.

async def maker_entry_or_market(symbol: str, side: str, qty: float,
                                top: Optional[dict] = None,
                                timeout_sec: int = 5,
                                fallback: str = "market",
                                min_qty: float = 0.0,
                                reduce_only: bool = False) -> dict:
    """
    Entry MAKER cu fallback configurabil pe remainder.

    Pasi:
      1. Plaseaza Limit PostOnly la best bid (Buy) / best ask (Sell).
         Daca PostOnly e respins instant → fallback imediat.
      2. Astepta `timeout_sec` × 1s. Verifica orderStatus dupa fiecare secunda.
         Daca orderStatus == "Filled" → succes ca maker.
      3. Timeout → cancel ordinul. Verifica `cumExecQty`.
         - fallback="market": Market pe REMAINDER (anti-double-fill).
         - fallback="skip":   nu mai trimite Market — accepti underfill.

    REGULA fallback:
        - Daca pierderea de a NU intra/iesi < costul taker  → fallback="skip"
        - Daca pierderea de a NU intra/iesi >= costul taker → fallback="market"
        - Orice exit de PROTECTIE (SL/trail/BE) → NU folosi pattern-ul,
          place_market direct.

    GHID timeout_sec:
        ENTRIES                                 timeout  fallback
          - Breakout / volatil                    3s     market
          - Mean reversion / trend 4h calm        5-7s   market
        EXITS PROFIT                            timeout  fallback
          - TP final (close all)                 10s     market
        ADAOSURI POZITIE (pyramidare)             5s     skip

    Args:
      top:         {"bid","ask"} sau None → REST get_ticker intern.
      reduce_only: True pt EXIT-uri. False pt ENTRY/pyramidare.

    Returneaza:
      {
        "result":     "maker"   - filled 100% maker
                      "taker"   - rejection imediata SAU 100% market fallback
                      "mixed"   - partial maker + market remainder
                      "skipped" - timeout cu fallback="skip"
                      "failed"  - place_market a esuat,
        "filled_qty": float,
        "avg_price":  float,    # avg maker; pe mixed/taker e estimativ
                                # (avg ponderat real vine din fetch_pnl_for_trade)
      }
    """
    info = await get_market_info(symbol)

    if top is None:
        t = await get_ticker(symbol)
        top = {"bid": t["bid1"], "ask": t["ask1"]} if t else {}
    px = top.get("bid") if side == "Buy" else top.get("ask")
    if not px:
        if fallback == "skip":
            return {"result": "skipped", "filled_qty": 0.0, "avg_price": 0.0}
        market_id = await place_market(symbol, side, qty, reduce_only=reduce_only)
        return {"result": "taker" if market_id else "failed",
                "filled_qty": qty if market_id else 0.0,
                "avg_price": 0.0}

    # 1. Plasare maker. None = rejection PostOnly sau alt error.
    oid = await place_limit_postonly(symbol, side, px, qty,
                                     reduce_only=reduce_only)
    if not oid:
        # Bug fix: NU astepta timeout. Fallback imediat (sau skip).
        if fallback == "skip":
            return {"result": "skipped", "filled_qty": 0.0, "avg_price": 0.0}
        market_id = await place_market(symbol, side, qty, reduce_only=reduce_only)
        return {"result": "taker" if market_id else "failed",
                "filled_qty": qty if market_id else 0.0,
                "avg_price": 0.0}

    # 2. Poll orderStatus (NU position qty — bug fix: evita probleme cu pyramiding)
    for _ in range(timeout_sec):
        await asyncio.sleep(1)
        st = await get_order_status(symbol, oid)
        if st and st.get("orderStatus") == "Filled":
            return {"result": "maker",
                    "filled_qty": float(st.get("cumExecQty", qty) or qty),
                    "avg_price": float(st.get("avgPrice", px) or px)}

    # 3. Timeout — cancel + market doar pe remainder (bug fix: anti-double-fill)
    await cancel_order(symbol, oid)
    final = await get_order_status(symbol, oid)
    cum_qty = float(final.get("cumExecQty", 0) or 0) if final else 0.0
    avg_maker = float(final.get("avgPrice", 0) or 0) if final else 0.0
    remaining = max(qty - cum_qty, 0.0)

    if fallback == "skip":
        return {"result": "skipped",
                "filled_qty": cum_qty, "avg_price": avg_maker}

    # fallback == "market": completeaza pe remainder
    qty_step = info["qty_step"]
    if remaining > max(min_qty, qty_step):
        await place_market(symbol, side, remaining, reduce_only=reduce_only)

    if cum_qty > 0:
        return {"result": "mixed",
                "filled_qty": qty,
                "avg_price": avg_maker}  # avg afisat e cel maker; ponderat real
                                          # vine din fetch_pnl_for_trade
    return {"result": "taker",
            "filled_qty": qty, "avg_price": 0.0}


# ============================================================================
# Chase-close (force-close maker chase cu fallback Market)
# ============================================================================

async def chase_close(symbol: str, direction: str,
                      max_attempts: int = 20,
                      interval_sec: float = 3.0) -> bool:
    """
    Force-close maker chase cu fallback Market — pentru cazul cand
    place_market(reduce_only=True) initial esueaza (Bybit error, network glitch).
    Garanteaza inchidere pozitie.

    Pasi (max_attempts × interval_sec, default 20×3=60s):
      1. Cancel ordine deschise (curatenie).
      2. Loop pana la max_attempts:
         a. Check qty din get_position. Daca 0 → done (success).
         b. Cancel limit-ul anterior daca exista.
         c. Plaseaza limit PostOnly reduce-only la best ask (long) / bid (short).
         d. Sleep interval_sec.
      3. Daca max_attempts atinse si pozitia inca exista → fallback Market.

    Returneaza True daca pozitia a fost inchisa, False daca toate fallback-urile
    au esuat.

    Apelat din: main.close_position cand place_market(reduce_only=True) initial
    esueaza, sau din reconciliation manual (qty desync).
    """
    await cancel_all(symbol)
    close_side = "Sell" if direction == "LONG" else "Buy"
    last_id: Optional[str] = None

    for attempt in range(max_attempts):
        bybit_pos = await get_position(symbol)
        qty = float(bybit_pos.get("size", 0)) if bybit_pos else 0.0
        if qty <= 0:
            print(f"  [BYBIT] chase_close {symbol}: inchis ({attempt} attempts)")
            return True

        if last_id:
            await cancel_order(symbol, last_id)
            last_id = None

        t = await get_ticker(symbol)
        if not t:
            await asyncio.sleep(interval_sec)
            continue

        price = t["ask1"] if direction == "LONG" else t["bid1"]
        last_id = await place_limit_postonly(symbol, close_side, price, qty,
                                             reduce_only=True)
        if last_id:
            print(f"  [BYBIT] chase_close {attempt+1}/{max_attempts}: "
                  f"{close_side} @ {price} qty={qty}")
        await asyncio.sleep(interval_sec)

    # Fallback market
    bybit_pos = await get_position(symbol)
    qty = float(bybit_pos.get("size", 0)) if bybit_pos else 0.0
    if qty > 0:
        if last_id:
            await cancel_order(symbol, last_id)
        print(f"  [BYBIT] chase_close {symbol} FAILED — fallback MARKET qty={qty}")
        order_id = await place_market(symbol, close_side, qty, reduce_only=True)
        return order_id is not None
    return True


# ============================================================================
# PnL
# ============================================================================

async def fetch_closed_pnl(symbol: str,
                           start_ms: Optional[int] = None,
                           limit: int = 50) -> list[dict]:
    params: dict = {"category": _cat(), "symbol": symbol, "limit": min(limit, 100)}
    if start_ms:
        params["startTime"] = int(start_ms)
    r = await _get("/v5/position/closed-pnl", params)
    return r.get("list", []) if r else []


async def fetch_pnl_for_trade(symbol: str,
                              entry_ts_ms: int,
                              exit_ts_ms: int,
                              settle_delay_sec: float = 2.0) -> dict:
    """
    Trage PnL real (incl. fees) pt un trade logical, cu retry pt indexing lag.
    Window: [entry-60s, max(exit+5min, now+60s)] ca sa prinda fill-uri tarzii.
    """
    if settle_delay_sec > 0:
        await asyncio.sleep(settle_delay_sec)

    start_ms = entry_ts_ms - 60_000
    end_limit_ms = max(exit_ts_ms + 300_000, int(time.time() * 1000) + 60_000)

    records: list = []
    relevant: list = []
    for attempt, retry_delay in enumerate([0, 2.0, 5.0, 10.0]):
        if retry_delay > 0:
            await asyncio.sleep(retry_delay)
        records = await fetch_closed_pnl(symbol, start_ms=start_ms, limit=50)
        relevant = [
            r for r in records
            if start_ms <= int(r.get("updatedTime", 0)) <= end_limit_ms
        ]
        if relevant:
            if attempt > 0:
                print(f"  [BYBIT] closed-pnl gasit dupa retry #{attempt} "
                      f"({len(relevant)} records)")
            break
        if attempt < 3:
            print(f"  [BYBIT] closed-pnl gol (retry {attempt + 1}/3 in "
                  f"{[2.0, 5.0, 10.0][attempt]:g}s)")

    if not relevant:
        print(f"  [BYBIT] WARNING: niciun closed-pnl pt trade "
              f"{entry_ts_ms}-{exit_ts_ms} dupa 4 incercari")
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
