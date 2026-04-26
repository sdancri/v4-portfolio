"""Config loader — citește config.yaml + .env, validează minimul necesar."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class StrategyConfig:
    name: str
    pool_total: float
    equity_start: float
    risk_pct_equity: float
    reset_trigger: float
    reset_target: float
    max_resets: int | None
    withdraw_target: float
    sl_min_pct: float
    sl_max_pct: float
    cooldown_bars: int
    leverage: int
    cap_pct_of_max: float
    taker_fee: float
    slippage_bps: float
    style: str
    # "on_close" (default, conservator, cap strict) | "on_entry" (lasă cycle să
    # ride close-uri profitabile multiple înainte de a verifica target).
    withdraw_check_mode: str = "on_close"
    # "pure" (default) | "with_reverse"
    #   - pure: după OPP exit, cooldown 3 bars înainte de orice entry.
    #   - with_reverse: după OPP exit, entry pe direcția opusă POATE deschide
    #     IMEDIAT pe aceeași bară. Match cu target-ul $13,847 din strategy.md
    #     (varianta empirică folosită pentru benchmark).
    opp_exit_mode: str = "pure"


@dataclass
class IndicatorConfig:
    mcginley_length: int = 14
    whiteline_length: int = 20
    ttms_length: int = 20
    ttms_bb_mult: float = 2.0
    ttms_kc_mult_widest: float = 2.0
    tether_fast: int = 13
    tether_slow: int = 55
    vortex_length: int = 14
    vortex_threshold: float = 0.05
    st_atr_length: int = 22
    st_atr_mult: float = 3.0


@dataclass
class PairConfig:
    symbol: str
    timeframe: str


@dataclass
class SubaccountConfig:
    name: str
    enabled: bool
    pairs: list[PairConfig]
    expected_wealth_2_3y: float = 0.0


@dataclass
class ReplayConfig:
    data_dir: Path
    start: str
    end: str


@dataclass
class OperationalConfig:
    max_concurrent_positions_per_subacc: int = 2
    max_consecutive_api_errors: int = 5
    heartbeat_interval_seconds: int = 60
    save_state_interval_seconds: int = 300
    state_dir: Path = Path("./state")
    log_dir: Path = Path("./logs")


@dataclass
class AppConfig:
    strategy: StrategyConfig
    indicator: IndicatorConfig
    subaccounts: list[SubaccountConfig]
    replay: ReplayConfig
    operational: OperationalConfig
    raw: dict[str, Any] = field(default_factory=dict)


def load_config(path: str | Path = "config/config.yaml") -> AppConfig:
    p = Path(path)
    with p.open() as f:
        raw = yaml.safe_load(f)

    s = raw["strategy"]
    strategy = StrategyConfig(
        name=s["name"],
        pool_total=float(s["pool_total"]),
        equity_start=float(s["equity_start"]),
        risk_pct_equity=float(s["risk_pct_equity"]),
        reset_trigger=float(s["reset_trigger"]),
        reset_target=float(s["reset_target"]),
        max_resets=s.get("max_resets"),
        withdraw_target=float(s["withdraw_target"]),
        sl_min_pct=float(s["sl_min_pct"]),
        sl_max_pct=float(s["sl_max_pct"]),
        cooldown_bars=int(s["cooldown_bars"]),
        leverage=int(s["leverage"]),
        cap_pct_of_max=float(s["cap_pct_of_max"]),
        taker_fee=float(s["taker_fee"]),
        slippage_bps=float(s.get("slippage_bps", 0.0)),
        style=s["style"],
        withdraw_check_mode=s.get("withdraw_check_mode", "on_close"),
        opp_exit_mode=s.get("opp_exit_mode", "pure"),
    )

    indicator = IndicatorConfig(**raw.get("vse_indicator", {}))

    subaccounts = [
        SubaccountConfig(
            name=sa["name"],
            enabled=bool(sa.get("enabled", True)),
            pairs=[PairConfig(symbol=p["symbol"], timeframe=p["timeframe"])
                   for p in sa["pairs"]],
            expected_wealth_2_3y=float(sa.get("expected_wealth_2_3y", 0.0)),
        )
        for sa in raw["subaccounts"]
    ]

    rep = raw.get("replay", {})
    replay = ReplayConfig(
        data_dir=Path(rep.get("data_dir", "./data/ohlcv")),
        start=str(rep.get("start", "2024-01-01")),
        end=str(rep.get("end", "2026-04-25")),
    )

    op = raw.get("operational", {})
    operational = OperationalConfig(
        max_concurrent_positions_per_subacc=int(
            op.get("max_concurrent_positions_per_subacc", 2)),
        max_consecutive_api_errors=int(op.get("max_consecutive_api_errors", 5)),
        heartbeat_interval_seconds=int(op.get("heartbeat_interval_seconds", 60)),
        save_state_interval_seconds=int(op.get("save_state_interval_seconds", 300)),
        state_dir=Path(op.get("state_dir", "./state")),
        log_dir=Path(op.get("log_dir", "./logs")),
    )

    return AppConfig(
        strategy=strategy,
        indicator=indicator,
        subaccounts=subaccounts,
        replay=replay,
        operational=operational,
        raw=raw,
    )
