"""Reset cycle logic per subaccount — NO KILL SWITCH.

Spec source: STRATEGY_LOGIC.md secțiunea 6.

Pe scurt:
  - Trade close → equity ± PnL, balance ± PnL.
  - balance >= withdraw_target ($5k) → SUCCESS (close all + restart cycle).
  - equity < reset_trigger ($15) → RESET (consume rezerva, equity → $50, NO KILL).
  - balance epuizat (sub ~$10) → no automatic kill; subaccount va înceta să tradeze
    de la sine (margin insuficient pentru pos size minim) și user-ul decide manual.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from vse_bot.config import StrategyConfig

CycleResult = Literal["NONE", "RESET", "SUCCESS", "POOL_LOW"]


@dataclass
class SubaccountState:
    pool_total: float = 100.0
    balance_broker: float = 100.0
    equity: float = 50.0
    pool_used: float = 0.0
    reset_count: int = 0
    cycle_num: int = 1
    cycle_start_ts: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    cycle_peak: float = 100.0
    last_event: str = "INIT"

    @classmethod
    def fresh(cls, cfg: StrategyConfig) -> "SubaccountState":
        return cls(
            pool_total=cfg.pool_total,
            balance_broker=cfg.pool_total,
            equity=cfg.equity_start,
            pool_used=0.0,
            reset_count=0,
            cycle_num=1,
            cycle_start_ts=datetime.now(timezone.utc).isoformat(),
            cycle_peak=cfg.pool_total,
            last_event="INIT",
        )


def on_trade_closed(
    state: SubaccountState,
    pnl_net: float,
    cfg: StrategyConfig,
    *,
    check_success: bool = True,
) -> CycleResult:
    """Update state după închiderea unui trade. Returnează evenimentul.

    Args:
        check_success: dacă True (default), verifică balance >= withdraw_target
            și returnează SUCCESS. Dacă False (mode ``withdraw_check=on_entry``),
            doar acumulează balance/equity; SUCCESS-ul va fi verificat separat
            în `check_cycle_success_at_entry` la următorul entry valid.

    Important: SUCCESS / RESET se verifică DUPĂ FIECARE close pe pool comun
    (multi-position multi-pair).
    """
    state.equity += pnl_net
    state.balance_broker += pnl_net
    if state.balance_broker > state.cycle_peak:
        state.cycle_peak = state.balance_broker

    # 1. SUCCESS — opțional (off pentru mode on_entry)
    if check_success and state.balance_broker >= cfg.withdraw_target:
        state.last_event = "SUCCESS"
        return "SUCCESS"

    # 2. RESET — equity sub trigger
    if state.equity < cfg.reset_trigger:
        deficit = cfg.reset_target - state.equity
        affordable = max(0.0, state.balance_broker - state.equity)
        cost = min(deficit, affordable)
        state.pool_used += cost
        state.equity += cost
        state.reset_count += 1
        if state.balance_broker < 30:
            state.last_event = "POOL_LOW"
            return "POOL_LOW"
        state.last_event = "RESET"
        return "RESET"

    state.last_event = "NONE"
    return "NONE"


def check_cycle_success_at_entry(
    state: SubaccountState, cfg: StrategyConfig
) -> bool:
    """Folosit în mode ``withdraw_check=on_entry``: înainte de a deschide un
    trade nou, verifică dacă balance-ul depășește target-ul (s-a acumulat din
    close-uri multiple ne-finalizate ca SUCCESS).
    """
    return state.balance_broker >= cfg.withdraw_target


def restart_cycle_after_success(
    state: SubaccountState, cfg: StrategyConfig
) -> float:
    """Apel după ce s-a făcut withdraw-ul. Restart cycle clean. Întoarce suma withdrawn."""
    withdraw_amount = max(0.0, state.balance_broker - cfg.pool_total)
    state.balance_broker = cfg.pool_total
    state.equity = cfg.equity_start
    state.pool_used = 0.0
    state.reset_count = 0
    state.cycle_num += 1
    state.cycle_start_ts = datetime.now(timezone.utc).isoformat()
    state.cycle_peak = cfg.pool_total
    state.last_event = "CYCLE_RESTART"
    return withdraw_amount


# ── Persistence ──────────────────────────────────────────────────────────
def save_state(state: SubaccountState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(asdict(state), f, indent=2)


def load_state(path: Path, cfg: StrategyConfig | None = None) -> SubaccountState:
    if not path.exists():
        if cfg is None:
            return SubaccountState()
        return SubaccountState.fresh(cfg)
    with path.open() as f:
        d = json.load(f)
    return SubaccountState(**d)
