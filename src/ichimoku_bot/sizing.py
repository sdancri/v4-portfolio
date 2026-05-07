"""Position sizing pentru ICHIMOKU bot.

Formula:
    risk_usd = risk_pct_per_trade × shared_equity   (ex 7% × $100 = $7)
    pos_usd  = risk_usd / sl_initial_pct            (ex $7 / 0.04 = $175)
    cap_usd  = cap_pct_of_max × balance × leverage_max
               (ex 0.95 × $100 × 12 = $1,140)

DOUA leverage diferite, scopuri DIFERITE:
  - ``per-pair leverage`` (ex MNT=20×): folosit DOAR la ``set_leverage`` API call
    pe Bybit (cere broker-ului sa-i aloce X× margin pe acel symbol).
  - ``leverage_max`` (ex 12×): SAFETY cap intern al botului — limita notional
    pos in ``cap_usd``. Indiferent ce permite Bybit (20×, 25×), botul NU
    deschide pozitii cu notional > 12× equity.

Daca ``pos_usd > cap_usd``:
    Botul forteaza skip signal → returneaza None.
"""

from __future__ import annotations

from dataclasses import dataclass

from ichimoku_bot.config import PairConfig, PortfolioConfig


@dataclass
class SizingResult:
    risk_usd: float
    pos_usd: float
    margin_needed: float
    cap_usd: float
    leverage: int                       # leverage efectiv folosit (per-pair)


def compute_position_size(
    shared_equity: float,
    pair_cfg: PairConfig,
    portfolio_cfg: PortfolioConfig,
    balance_broker: float,
    leverage: int | None = None,
) -> SizingResult | None:
    """Sizing per-pair folosind shared equity (compound).

    Args:
        leverage: leverage efectiv pt aceasta pereche (per-pair override).
                  Daca None, foloseste ``portfolio_cfg.leverage`` ca fallback.

    Returneaza None daca pozitia ar depasi capul Bybit (skip signal).
    """
    sl_pct = pair_cfg.sl_initial_pct
    if sl_pct <= 0:
        raise ValueError(f"sl_initial_pct must be > 0, got {sl_pct}")

    eff_leverage = leverage if leverage is not None else portfolio_cfg.leverage
    # Cap_usd foloseste DOAR leverage_max (safety cap intern), NU per-pair lev.
    # per-pair lev se foloseste DOAR la set_leverage API call.
    risk_usd = pair_cfg.risk_pct_per_trade * shared_equity
    pos_usd = risk_usd / sl_pct
    cap_usd = portfolio_cfg.cap_pct_of_max * balance_broker * portfolio_cfg.leverage_max

    if pos_usd > cap_usd:
        return None

    return SizingResult(
        risk_usd=risk_usd,
        pos_usd=pos_usd,
        margin_needed=pos_usd / eff_leverage,
        cap_usd=cap_usd,
        leverage=eff_leverage,
    )


def compute_qty(pos_usd: float, entry_price: float, step_size: float) -> float:
    """Round qty DOWN la step size al instrumentului."""
    if step_size <= 0:
        return pos_usd / entry_price
    qty_raw = pos_usd / entry_price
    return (qty_raw // step_size) * step_size
