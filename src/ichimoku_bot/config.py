"""Config loader pentru ICHIMOKU bot — citeste config.yaml + .env.

Schema simplificata fata de VSE:
- NO cycle_manager / reset_target / withdraw_target
- per-pair: hull_length, kijun_periods, senkou_b_periods, tp_pct, sizing_pct
- portfolio compound: equity = pool_total + cumulative PnL
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class PortfolioConfig:
    """Setari globale portfolio (subaccount)."""
    name: str
    pool_total: float                   # capital initial pe subaccount (USDT)
    leverage: int = 15
    cap_pct_of_max: float = 0.95
    taker_fee: float = 0.00055
    slippage_bps: float = 0.0


@dataclass
class PairConfig:
    """Per-pair Hull+Ichimoku params + sizing."""
    symbol: str
    timeframe: str = "4h"
    enabled: bool = True
    # Hull MA
    hull_length: int = 8
    # Ichimoku
    tenkan_periods: int = 9
    kijun_periods: int = 48
    senkou_b_periods: int = 40
    displacement: int = 24
    # Sizing
    risk_pct_per_trade: float = 0.05    # % din shared equity / trade
    sl_initial_pct: float = 0.05        # SL fix
    tp_pct: Optional[float] = None      # TP fix optional (ex 0.12 = 12%)
    # Per-pair leverage (Bybit isolated). Override pe portfolio.leverage —
    # folosit la set_leverage + cap_usd notional. Default = portfolio default
    # (None aici inseamna "foloseste portfolio.leverage").
    leverage: Optional[int] = None
    # Smart filters
    max_hull_spread_pct: float = 2.0
    max_close_kijun_dist_pct: float = 6.0


@dataclass
class OperationalConfig:
    max_concurrent_positions: int = 2
    max_consecutive_api_errors: int = 5
    heartbeat_interval_seconds: int = 60
    save_state_interval_seconds: int = 300
    state_dir: Path = Path("./state")
    log_dir: Path = Path("./logs")


@dataclass
class AppConfig:
    portfolio: PortfolioConfig
    pairs: list[PairConfig]
    operational: OperationalConfig
    raw: dict[str, Any] = field(default_factory=dict)

    def leverage_for(self, pair_cfg: "PairConfig") -> int:
        """Leverage efectiv pentru o pereche: pair override sau portfolio default."""
        return pair_cfg.leverage if pair_cfg.leverage is not None else self.portfolio.leverage


def load_config(path: str | Path = "config/config.yaml") -> AppConfig:
    p = Path(path)
    with p.open() as f:
        raw = yaml.safe_load(f)

    pf = raw["portfolio"]
    portfolio = PortfolioConfig(
        name=pf["name"],
        pool_total=float(pf["pool_total"]),
        leverage=int(pf.get("leverage", 15)),
        cap_pct_of_max=float(pf.get("cap_pct_of_max", 0.95)),
        taker_fee=float(pf.get("taker_fee", 0.00055)),
        slippage_bps=float(pf.get("slippage_bps", 0.0)),
    )

    pairs = []
    for pc in raw["pairs"]:
        pairs.append(PairConfig(
            symbol=pc["symbol"],
            timeframe=pc.get("timeframe", "4h"),
            enabled=bool(pc.get("enabled", True)),
            hull_length=int(pc.get("hull_length", 8)),
            tenkan_periods=int(pc.get("tenkan_periods", 9)),
            kijun_periods=int(pc.get("kijun_periods", 48)),
            senkou_b_periods=int(pc.get("senkou_b_periods", 40)),
            displacement=int(pc.get("displacement", 24)),
            risk_pct_per_trade=float(pc.get("risk_pct_per_trade", 0.05)),
            sl_initial_pct=float(pc.get("sl_initial_pct", 0.05)),
            tp_pct=pc.get("tp_pct"),
            leverage=(int(pc["leverage"]) if pc.get("leverage") is not None else None),
            max_hull_spread_pct=float(pc.get("max_hull_spread_pct", 2.0)),
            max_close_kijun_dist_pct=float(pc.get("max_close_kijun_dist_pct", 6.0)),
        ))

    op = raw.get("operational", {})
    operational = OperationalConfig(
        max_concurrent_positions=int(op.get("max_concurrent_positions", 2)),
        max_consecutive_api_errors=int(op.get("max_consecutive_api_errors", 5)),
        heartbeat_interval_seconds=int(op.get("heartbeat_interval_seconds", 60)),
        save_state_interval_seconds=int(op.get("save_state_interval_seconds", 300)),
        state_dir=Path(op.get("state_dir", "./state")),
        log_dir=Path(op.get("log_dir", "./logs")),
    )

    return AppConfig(portfolio=portfolio, pairs=pairs, operational=operational, raw=raw)
