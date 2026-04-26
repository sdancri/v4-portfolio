"""Position sizing — STRATEGY_LOGIC.md sec 4 + Setari BOT regula 9.

Formula:
    risk_usd = risk_pct_equity × equity         (ex 20% × $50 = $10)
    pos_usd  = risk_usd / sl_pct                (ex $10 / 0.02 = $500)
    cap_usd  = cap_pct_of_max × balance_broker × leverage
              (ex 0.95 × $100 × 20 = $1,900)

Dacă ``pos_usd > cap_usd``:
    Bybit ar refuza ordinul → skip signal complet (NU cap-uim software).
    ``compute_position_size`` returnează ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass

from vse_bot.config import StrategyConfig


@dataclass
class SizingResult:
    risk_usd: float
    pos_usd: float
    margin_needed: float
    cap_usd: float


def compute_position_size(
    equity: float,
    sl_pct: float,
    balance_broker: float,
    cfg: StrategyConfig,
) -> SizingResult | None:
    """Returns sizing OR None dacă pos depășește cap-ul (Bybit ar refuza).

    Argumente:
        equity: equity curent (compounding pe cycle).
        sl_pct: distanța SL ca fracție din entry_price (ex 0.02 = 2%).
        balance_broker: balance fizic Bybit (pool + cumulative PnL pe cycle).
        cfg: strategy config (risk_pct, leverage, cap_pct).
    """
    if sl_pct <= 0:
        raise ValueError(f"sl_pct must be > 0, got {sl_pct}")

    risk_usd = cfg.risk_pct_equity * equity
    pos_usd = risk_usd / sl_pct
    cap_usd = cfg.cap_pct_of_max * balance_broker * cfg.leverage

    if pos_usd > cap_usd:
        # Bybit ar refuza — skip signal complet.
        return None

    return SizingResult(
        risk_usd=risk_usd,
        pos_usd=pos_usd,
        margin_needed=pos_usd / cfg.leverage,
        cap_usd=cap_usd,
    )


def compute_qty(pos_usd: float, entry_price: float, step_size: float) -> float:
    """Round qty DOWN la step size al instrumentului."""
    if step_size <= 0:
        return pos_usd / entry_price
    qty_raw = pos_usd / entry_price
    return (qty_raw // step_size) * step_size
