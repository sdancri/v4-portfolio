"""Trade lifecycle live (open / trail / close) — peste un client Bybit-ccxt.

Spec source: STRATEGY_LOGIC.md secțiunea 5 (trailing) + 7 (lifecycle).

Workflow:
  1. ``open_trade_live(client, signal, state, cfg, symbol_meta)``:
     - calculează size, verifică margin, deschide market entry,
     - plasează SL stop-market reduce-only.
     - returnează ``LivePosition`` cu ID-urile ordinelor.

  2. ``update_trailing_stop(client, pos, new_stop)``:
     - dacă noul stop e MAI BUN (sus pe long, jos pe short), modifică ordinul SL.
     - altfel no-op.

  3. ``close_position_market(client, pos, reason)``:
     - market close (dacă SL n-a tras încă).
     - return-ul include exit_price aproximativ; PnL real se trage din
       ``client.fetch_realized_pnl`` după ce Bybit înregistrează evenimentul.

Notă: nu testăm aici WS execution events (deja sunt prin ``BybitClient`` în
main.py). Funcțiile de aici sunt sincron-async coordonator-side, nu fac polling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vse_bot.config import StrategyConfig
from vse_bot.cycle_manager import SubaccountState
from vse_bot.sizing import compute_qty
from vse_bot.vse_signal_live import LiveSignal

if TYPE_CHECKING:
    from vse_bot.exchange.bybit_client import BybitClient


@dataclass
class LivePosition:
    symbol: str
    side: str                  # "long" | "short"
    qty: float
    entry_price: float
    sl_price: float
    sl_initial: float
    pos_usd: float
    risk_usd: float
    opened_ts: datetime
    order_entry_id: str
    order_sl_id: str
    extra: dict[str, Any]
    # OPP exit planning — set la TRUE când raw opposite signal apare pe bara
    # curentă; exit-ul se execută la NEXT bar open (per spec STRATEGY_LOGIC sec 6).
    opp_exit_planned: bool = False


async def open_trade_live(
    *,
    client: "BybitClient",
    signal: LiveSignal,
    symbol: str,
    state: SubaccountState,
    cfg: StrategyConfig,
    qty_step: float,
    qty_min: float,
    used_margin_other: float = 0.0,
) -> LivePosition | None:
    """Plasează entry market + SL stop-market reduce-only.

    Sizing logic (per spec utilizator):
      1. ``risk_usd = 0.20 × state.equity`` (state.equity = $50 + compound REAL Bybit).
      2. ``pos_internal = risk_usd / sl_pct`` (formula bot internă).
      3. **Query BALANȚĂ Bybit REALĂ** înainte de trade.
      4. ``max_bybit = balance_real × leverage`` (max permis de Bybit, 100%).
         ``cap_value = 0.95 × max_bybit`` (5% sub max pt siguranță).
      5. **Dacă** ``pos_internal > max_bybit`` (peste ce permite Bybit) →
         ``pos_final = cap_value`` (intrăm cât permite Bybit -5%).
         **Altfel** → ``pos_final = pos_internal``.

    Returnează ``None`` doar dacă:
      - balance_real ≤ 0,
      - qty < min step Bybit,
      - Bybit reject la create_order.
    """
    # 1+2. Sizing intern (bot equity-based)
    risk_usd = cfg.risk_pct_equity * state.equity
    if signal.sl_pct <= 0:
        return None
    pos_internal = risk_usd / signal.sl_pct

    # 3. Query balance REALĂ Bybit
    balance_real = await client.fetch_balance_usdt()
    if balance_real <= 0:
        print(f"  [SIZING] balance_real=0 — skip {signal.side} {symbol}")
        return None

    # 4+5. Cap dacă pos depășește max-ul Bybit
    max_bybit = balance_real * cfg.leverage
    cap_value = cfg.cap_pct_of_max * max_bybit
    if pos_internal > max_bybit:
        pos_final = cap_value
        print(
            f"  [SIZING] CAP {signal.side} {symbol}: pos_internal=${pos_internal:.2f} "
            f"> max_bybit=${max_bybit:.2f} → pos=${pos_final:.2f} "
            f"(balance_real=${balance_real:.2f}, lev={cfg.leverage})"
        )
    else:
        pos_final = pos_internal

    qty = compute_qty(pos_final, signal.entry_price, qty_step)
    if qty < qty_min:
        print(
            f"  [SIZING] SKIP {signal.side} {symbol}: qty={qty} < min={qty_min}"
        )
        return None

    bybit_side = "buy" if signal.side == "long" else "sell"

    # 1. Market entry
    entry_order = await client.create_market_order(
        symbol=symbol,
        side=bybit_side,
        qty=qty,
        reduce_only=False,
    )

    # 2. Stop-market SL reduce-only (opposite side)
    sl_side = "sell" if signal.side == "long" else "buy"
    sl_order = await client.create_stop_market(
        symbol=symbol,
        side=sl_side,
        qty=qty,
        stop_price=signal.sl_price,
        reduce_only=True,
    )

    return LivePosition(
        symbol=symbol,
        side=signal.side,
        qty=qty,
        entry_price=signal.entry_price,    # va fi reconciliată cu fill-price din WS
        sl_price=signal.sl_price,
        sl_initial=signal.sl_price,
        pos_usd=pos_final,
        risk_usd=risk_usd,
        opened_ts=datetime.now(timezone.utc),
        order_entry_id=entry_order["id"],
        order_sl_id=sl_order["id"],
        extra={
            "sl_pct_at_signal": signal.sl_pct,
            "balance_real_at_entry": balance_real,
            "pos_internal": pos_internal,
            "was_capped": pos_internal > max_bybit,
            "max_bybit_at_entry": max_bybit,
        },
    )


async def update_trailing_stop(
    *,
    client: "BybitClient",
    pos: LivePosition,
    new_stop: float,
) -> bool:
    """Modifică SL-ul DOAR dacă e îmbunătățire (sus pe long, jos pe short).

    Returnează True dacă a updated, False dacă no-op.
    """
    if pos.side == "long":
        if new_stop <= pos.sl_price:
            return False
    else:
        if new_stop >= pos.sl_price:
            return False

    await client.modify_stop_price(
        symbol=pos.symbol,
        order_id=pos.order_sl_id,
        new_stop_price=new_stop,
    )
    pos.sl_price = float(new_stop)
    return True


async def close_position_market(
    *,
    client: "BybitClient",
    pos: LivePosition,
    reason: str,
) -> dict[str, Any]:
    """Force-close pe market. Folosit la signal_reverse, cycle_success, panic."""
    bybit_side = "sell" if pos.side == "long" else "buy"
    await client.cancel_order(symbol=pos.symbol, order_id=pos.order_sl_id)
    result = await client.create_market_order(
        symbol=pos.symbol,
        side=bybit_side,
        qty=pos.qty,
        reduce_only=True,
    )
    result["reason"] = reason
    return result
