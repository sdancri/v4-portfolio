"""Pre-flight sanity check înainte de a porni botul live.

Verifică:
  1. Config valid (params critice match strategy.md)
  2. .env populat (API keys per subaccount)
  3. Bybit reachable (testnet sau mainnet)
  4. Markets disponibile (KAIA/AAVE/ONT/ETH USDT perpetuals)
  5. Min qty / step size pentru fiecare pereche
  6. State directory writable
  7. Replay match (rulează scurt pe ultima săptămână)

Uzitare:
    python scripts/preflight_check.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

from ichimoku_bot.config import load_config
from ichimoku_bot.exchange.bybit_client import BybitClient


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def _warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def _err(msg: str) -> None:
    print(f"  ✗ {msg}")


def check_config(cfg) -> bool:
    print("\n[1/6] Config sanity")
    ok = True
    s = cfg.strategy
    if s.equity_start != 50.0:
        _warn(f"equity_start = {s.equity_start} (spec Nou1 = 50)")
    else:
        _ok(f"equity_start = $50")
    if abs(s.risk_pct_equity - 0.20) > 1e-6:
        _warn(f"risk_pct_equity = {s.risk_pct_equity} (spec Nou1 = 0.20)")
    else:
        _ok(f"risk_pct_equity = 20%")
    if s.max_resets is not None:
        _warn(f"max_resets = {s.max_resets} (spec Nou1 = NO KILL)")
    else:
        _ok(f"max_resets = NO KILL ✓")
    if s.withdraw_target == 5000.0:
        _ok(f"withdraw_target = $5,000 (spec original)")
    elif s.withdraw_target == 10000.0:
        _ok(f"withdraw_target = $10,000 (decizie post-replay: wealth ~$31k)")
    else:
        _warn(f"withdraw_target = ${s.withdraw_target:.0f} (non-standard)")
    if s.opp_exit_mode in ("pure", "with_reverse"):
        _ok(f"opp_exit_mode = {s.opp_exit_mode}")
    else:
        _err(f"opp_exit_mode = {s.opp_exit_mode} (invalid)")
        ok = False
    if s.style != "Balanced":
        _warn(f"style = {s.style} (spec = Balanced)")
    else:
        _ok(f"style = Balanced")
    return ok


def check_subaccounts(cfg) -> bool:
    print("\n[2/6] Subaccounts setup")
    expected = {
        "subacc_1_kaia_aave": [("KAIAUSDT", "1h"), ("AAVEUSDT", "1h")],
        "subacc_2_ont_eth":   [("ONTUSDT", "1h"), ("ETHUSDT", "2h")],
    }
    ok = True
    for sub in cfg.subaccounts:
        pairs = [(p.symbol, p.timeframe) for p in sub.pairs]
        exp = expected.get(sub.name)
        if exp != pairs:
            _warn(f"{sub.name}: pairs {pairs} (expected {exp})")
        else:
            _ok(f"{sub.name}: {pairs}")
    return ok


def check_env() -> bool:
    print("\n[3/6] Environment variables (.env)")
    load_dotenv()
    mode = os.getenv("TRADING_MODE", "testnet").lower()
    print(f"  TRADING_MODE = {mode}")
    if mode == "live":
        _warn("MAINNET — banii sunt reali!")
    else:
        _ok("testnet (safe)")
    ok = True
    for prefix in ("SUB1", "SUB2"):
        key = os.environ.get(f"{prefix}_API_KEY", "")
        secret = os.environ.get(f"{prefix}_API_SECRET", "")
        if not key:
            _err(f"{prefix}_API_KEY missing")
            ok = False
        elif len(key) < 8:
            _err(f"{prefix}_API_KEY pare invalid (sub 8 chars)")
            ok = False
        else:
            _ok(f"{prefix}_API_KEY OK ({key[:4]}…{key[-3:]})")
        if not secret:
            _err(f"{prefix}_API_SECRET missing")
            ok = False
        else:
            _ok(f"{prefix}_API_SECRET OK ({len(secret)} chars)")
    return ok


async def check_bybit_connectivity(cfg) -> bool:
    print("\n[4/6] Bybit connectivity + markets")
    testnet = os.getenv("TRADING_MODE", "testnet").lower() != "live"
    api_key = os.environ.get("SUB1_API_KEY", "")
    api_secret = os.environ.get("SUB1_API_SECRET", "")
    if not api_key or not api_secret:
        _err("SUB1 credentials missing — skip connectivity check")
        return False

    client = await BybitClient.create(api_key, api_secret, testnet=testnet)
    try:
        await client.ensure_markets()
        _ok(f"Connected to Bybit ({'testnet' if testnet else 'MAINNET'})")
        ok = True
        for sub in cfg.subaccounts:
            for pair in sub.pairs:
                try:
                    mi = await client.fetch_market_info(pair.symbol)
                    _ok(
                        f"{pair.symbol} {pair.timeframe}: "
                        f"qty_step={mi.qty_step}, qty_min={mi.qty_min}, "
                        f"tick={mi.tick_size}"
                    )
                except Exception as e:
                    _err(f"{pair.symbol}: {e}")
                    ok = False
        return ok
    finally:
        await client.close()


def check_directories(cfg) -> bool:
    print("\n[5/6] Directories")
    ok = True
    for d in (cfg.operational.state_dir, cfg.operational.log_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
            test_file = d / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
            _ok(f"{d} writable")
        except Exception as e:
            _err(f"{d}: {e}")
            ok = False
    return ok


def check_replay_smoke(cfg) -> bool:
    print("\n[6/6] Replay smoke (last 30 days only)")
    import pandas as pd

    from ichimoku_bot.replay import run_replay

    # Test rapid pe ultima lună
    cfg.replay.start = (pd.Timestamp(cfg.replay.end) - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        results = run_replay(cfg)
        for sub in cfg.subaccounts:
            r = results[sub.name]
            _ok(
                f"{sub.name}: {r.n_trades} trades, "
                f"PnL ${sum(t.pnl_net for t in r.trades):+.0f}"
            )
        return True
    except Exception as e:
        _err(f"replay failed: {e}")
        return False


async def main() -> int:
    print("=" * 70)
    print("VSE BOT — PRE-FLIGHT CHECK")
    print("=" * 70)

    cfg = load_config(ROOT / "config" / "config.yaml")

    results = []
    results.append(("Config",         check_config(cfg)))
    results.append(("Subaccounts",    check_subaccounts(cfg)))
    results.append(("Env vars",       check_env()))
    results.append(("Directories",    check_directories(cfg)))
    results.append(("Replay smoke",   check_replay_smoke(cfg)))
    # Bybit connectivity la sfârșit (slow + needs creds)
    if all(ok for _, ok in results[:3]):
        bybit_ok = await check_bybit_connectivity(cfg)
        results.append(("Bybit",      bybit_ok))
    else:
        print("\n[4/6] Skipped (config sau env nu sunt OK)")

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    all_ok = True
    for name, ok in results:
        status = "✓" if ok else "✗"
        print(f"  {status} {name}")
        all_ok = all_ok and ok

    if all_ok:
        print("\n✓ ALL CHECKS PASSED — gata pentru testnet/live")
        return 0
    else:
        print("\n✗ FAILURES detected — fix înainte de a rula botul live")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
