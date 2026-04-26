"""Unit tests pentru cycle_manager — cazuri-cheie din STRATEGY_LOGIC."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pytest

from vse_bot.config import load_config
from vse_bot.cycle_manager import (
    SubaccountState,
    on_trade_closed,
    restart_cycle_after_success,
)


@pytest.fixture
def cfg():
    return load_config(ROOT / "config" / "config.yaml").strategy


def test_fresh_state_matches_pool_total(cfg):
    s = SubaccountState.fresh(cfg)
    assert s.balance_broker == cfg.pool_total == 100.0
    assert s.equity == cfg.equity_start == 50.0
    assert s.pool_used == 0.0
    assert s.reset_count == 0
    assert s.cycle_num == 1


def test_winning_trade_updates_balance_and_equity(cfg):
    s = SubaccountState.fresh(cfg)
    ev = on_trade_closed(s, pnl_net=12.34, cfg=cfg)
    assert ev == "NONE"
    assert s.equity == pytest.approx(62.34)
    assert s.balance_broker == pytest.approx(112.34)


def test_loss_below_reset_trigger_triggers_reset(cfg):
    s = SubaccountState.fresh(cfg)
    # Drag equity sub $15: mare pierdere -$40 → equity 10
    ev = on_trade_closed(s, pnl_net=-40.0, cfg=cfg)
    assert ev == "RESET"
    # equity revine la $50 (sau cât permite balance-ul)
    assert s.equity == pytest.approx(50.0)
    assert s.reset_count == 1
    # Pool used = $40 ($50 target − $10 înainte de reset)
    assert s.pool_used == pytest.approx(40.0)
    # Balance neschimbat (rezerva e doar contabilă, nu transfer)
    assert s.balance_broker == pytest.approx(60.0)


def test_huge_win_triggers_success(cfg):
    s = SubaccountState.fresh(cfg)
    # PnL care duce balance peste withdraw_target (whatever e configurat)
    pnl = cfg.withdraw_target - cfg.pool_total + 50.0   # +$50 peste prag
    ev = on_trade_closed(s, pnl_net=pnl, cfg=cfg)
    assert ev == "SUCCESS"
    assert s.balance_broker == pytest.approx(cfg.pool_total + pnl)


def test_pool_low_after_extreme_loss(cfg):
    s = SubaccountState.fresh(cfg)
    # Pierderea -$80 → balance 20, equity -30 → reset, dar balance < $30 → POOL_LOW
    ev = on_trade_closed(s, pnl_net=-80.0, cfg=cfg)
    assert ev == "POOL_LOW"
    assert s.reset_count == 1


def test_no_kill_continues_after_many_resets(cfg):
    s = SubaccountState.fresh(cfg)
    # 10 reseturi consecutive — NO KILL: state-ul rămâne tradeabil
    for _ in range(10):
        on_trade_closed(s, pnl_net=-40.0, cfg=cfg)
    # cu max_resets = None nu e oprire; balance e desigur foarte scăzut
    assert s.reset_count >= 1
    # Nu există un câmp "killed" — strategia oprește efectiv doar când
    # balance ajunge sub margin minim, lucru gestionat de orchestrator.


def test_restart_cycle_after_success_returns_pool(cfg):
    s = SubaccountState.fresh(cfg)
    pnl = cfg.withdraw_target - cfg.pool_total + 50.0
    on_trade_closed(s, pnl_net=pnl, cfg=cfg)   # SUCCESS
    withdraw = restart_cycle_after_success(s, cfg)
    assert withdraw == pytest.approx(pnl)
    assert s.balance_broker == pytest.approx(cfg.pool_total)
    assert s.equity == pytest.approx(cfg.equity_start)
    assert s.cycle_num == 2
    assert s.reset_count == 0
