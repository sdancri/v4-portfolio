"""Smoke test pentru ICHIMOKU bot — fara live API calls, fara ordine.

Verifica ca toate componentele se incarca + wiring-ul intre ele functioneaza:
  1. Imports modulare
  2. Config load (config.yaml)
  3. Per-pair leverage resolve
  4. no_lookahead.filter_closed_bars (logica anti-bias)
  5. rate_limiter (token bucket async)
  6. telegram_bot.fmt_time
  7. IchimokuSignal warmup pe date reale (parquet)
  8. evaluate() returneaza decizii valide
  9. compute_position_size + qty rounding
  10. chart_server.create_app build (fara server start)
  11. BybitClient interface (instantiate fara conectare)

Uzitare:
    python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Colors for output
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str, exc: Exception | None = None) -> None:
    print(f"  {RED}✗ {msg}{RESET}")
    if exc:
        print(f"    {DIM}{type(exc).__name__}: {exc}{RESET}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}")


def section(title: str) -> None:
    print(f"\n{title}")
    print("─" * len(title))


PASSED = 0
FAILED = 0


def test(label: str):
    """Decorator pentru a numara teste pass/fail."""
    def deco(fn):
        global PASSED, FAILED
        try:
            r = fn()
            ok(label)
            PASSED += 1
            return r
        except Exception as e:
            fail(label, e)
            traceback.print_exc()
            FAILED += 1
            return None
    return deco


# ─── 1. IMPORTS ───────────────────────────────────────────────────────────
section("1. Modulele se importa")

try:
    from ichimoku_bot import telegram_bot as tg
    ok("ichimoku_bot.telegram_bot")
    PASSED += 1
except Exception as e:
    fail("telegram_bot", e); FAILED += 1

try:
    from ichimoku_bot.bot_state import BotState, TradeRecord
    ok("ichimoku_bot.bot_state")
    PASSED += 1
except Exception as e:
    fail("bot_state", e); FAILED += 1

try:
    from ichimoku_bot.config import AppConfig, PairConfig, load_config
    ok("ichimoku_bot.config")
    PASSED += 1
except Exception as e:
    fail("config", e); FAILED += 1

try:
    from ichimoku_bot.no_lookahead import filter_closed_bars, interval_ms
    ok("ichimoku_bot.no_lookahead")
    PASSED += 1
except Exception as e:
    fail("no_lookahead", e); FAILED += 1

try:
    from ichimoku_bot.rate_limiter import wait_token, TokenBucket
    ok("ichimoku_bot.rate_limiter")
    PASSED += 1
except Exception as e:
    fail("rate_limiter", e); FAILED += 1

try:
    from ichimoku_bot.ichimoku_signal import (
        IchimokuSignal, PairStrategyConfig, SignalDecision,
        precompute_indicators,
    )
    ok("ichimoku_bot.ichimoku_signal")
    PASSED += 1
except Exception as e:
    fail("ichimoku_signal", e); FAILED += 1

try:
    from ichimoku_bot.sizing import compute_position_size, compute_qty, SizingResult
    ok("ichimoku_bot.sizing")
    PASSED += 1
except Exception as e:
    fail("sizing", e); FAILED += 1

try:
    from ichimoku_bot.exchange.bybit_client import BybitClient
    ok("ichimoku_bot.exchange.bybit_client")
    PASSED += 1
except Exception as e:
    fail("bybit_client", e); FAILED += 1

try:
    from ichimoku_bot.chart_server import create_app, broadcast, serve_chart
    ok("ichimoku_bot.chart_server")
    PASSED += 1
except Exception as e:
    fail("chart_server", e); FAILED += 1

try:
    import ichimoku_bot.main as _main_mod
    ok("ichimoku_bot.main")
    PASSED += 1
except Exception as e:
    fail("main", e); FAILED += 1


# ─── 2. CONFIG ────────────────────────────────────────────────────────────
section("2. Config loading")

cfg = None
try:
    cfg = load_config("config/config.yaml")
    ok(f"load_config: portfolio={cfg.portfolio.name}, pairs={len(cfg.pairs)}")
    PASSED += 1
except Exception as e:
    fail("load_config", e); FAILED += 1

if cfg:
    try:
        assert cfg.portfolio.pool_total == 100.0
        assert cfg.portfolio.leverage == 15
        assert cfg.portfolio.cap_pct_of_max == 0.95
        assert cfg.portfolio.taker_fee == 0.00055
        ok("portfolio settings (pool=100, lev_default=15, cap=0.95, fee=0.055%)")
        PASSED += 1
    except AssertionError as e:
        fail("portfolio settings", e); FAILED += 1

    try:
        mnt = next(p for p in cfg.pairs if p.symbol == "MNTUSDT")
        dot = next(p for p in cfg.pairs if p.symbol == "DOTUSDT")
        assert mnt.leverage == 20, f"MNT leverage={mnt.leverage}"
        assert dot.leverage == 7, f"DOT leverage={dot.leverage}"
        assert mnt.sl_initial_pct == 0.03
        assert dot.sl_initial_pct == 0.08
        assert mnt.tp_pct is None
        assert dot.tp_pct == 0.12
        ok(f"per-pair: MNT(lev=20×, SL=3%, TP=None), DOT(lev=7×, SL=8%, TP=12%)")
        PASSED += 1
    except (AssertionError, StopIteration) as e:
        fail("per-pair config", e); FAILED += 1

    try:
        assert cfg.leverage_for(mnt) == 20
        assert cfg.leverage_for(dot) == 7
        # Fallback test: pair without override falls back to portfolio.leverage
        from dataclasses import replace
        no_lev = replace(mnt, leverage=None)
        assert cfg.leverage_for(no_lev) == 15
        ok("leverage_for() resolves per-pair + fallback")
        PASSED += 1
    except AssertionError as e:
        fail("leverage_for", e); FAILED += 1


# ─── 3. NO_LOOKAHEAD ──────────────────────────────────────────────────────
section("3. no_lookahead filter")

try:
    assert interval_ms("4h") == 14_400_000
    assert interval_ms("1h") == 3_600_000
    ok("interval_ms maps timeframes correctly")
    PASSED += 1
except (AssertionError, ValueError) as e:
    fail("interval_ms", e); FAILED += 1

try:
    # now mid-hour, 13min into hour boundary 1_699_999_200_000
    now_ms = 1_700_000_000_000
    bars = [
        [1_699_992_000_000, 1, 1, 1, 1, 100],   # closed
        [1_699_995_600_000, 1, 1, 1, 1, 100],   # closed
        [1_699_999_200_000, 1, 1, 1, 1, 100],   # CURRENT (in progress)
    ]
    filtered = filter_closed_bars(bars, "1h", now_ms=now_ms)
    assert len(filtered) == 2, f"expected 2 closed bars, got {len(filtered)}"
    ok(f"filter_closed_bars drops in-progress bar (3→2)")
    PASSED += 1
except AssertionError as e:
    fail("filter_closed_bars", e); FAILED += 1


# ─── 4. RATE LIMITER ──────────────────────────────────────────────────────
section("4. rate_limiter token bucket")

try:
    async def _rl_test():
        import time as _t
        bucket = TokenBucket(rate_per_sec=5, burst=3)
        # First 3 should be instant (burst)
        t0 = _t.monotonic()
        for _ in range(3):
            await bucket.acquire()
        burst_time = _t.monotonic() - t0
        assert burst_time < 0.1, f"burst took {burst_time:.3f}s"
        # Next 2 should throttle
        t1 = _t.monotonic()
        for _ in range(2):
            await bucket.acquire()
        throttle_time = _t.monotonic() - t1
        assert throttle_time > 0.3, f"throttle took {throttle_time:.3f}s, expected >0.3"
        return burst_time, throttle_time
    bt, tt = asyncio.run(_rl_test())
    ok(f"TokenBucket: burst 3 in {bt*1000:.0f}ms, then throttle 2 in {tt*1000:.0f}ms")
    PASSED += 1
except (AssertionError, Exception) as e:
    fail("rate_limiter", e); FAILED += 1


# ─── 5. TELEGRAM fmt_time ─────────────────────────────────────────────────
section("5. telegram_bot.fmt_time")

try:
    s_form = tg.fmt_time(1714329720)         # seconds
    ms_form = tg.fmt_time(1714329720000)     # milliseconds
    assert s_form == ms_form, f"s={s_form} != ms={ms_form}"
    assert "2024" in s_form
    # RO day name
    assert any(d in s_form for d in
               ["Luni", "Marti", "Miercuri", "Joi", "Vineri", "Sambata", "Duminica"])
    ok(f"fmt_time(s/ms) → '{s_form}'")
    PASSED += 1
except AssertionError as e:
    fail("fmt_time", e); FAILED += 1


# ─── 6. INDICATOR WARMUP pe date reale ────────────────────────────────────
section("6. IchimokuSignal warmup pe date reale (parquet)")

import pandas as pd

DATA_DIR = Path("/home/dan/Python/Test_Python/data/ohlcv")
mnt_path = DATA_DIR / "MNTUSDT_4h.parquet"
dot_path = DATA_DIR / "DOTUSDT_4h.parquet"

if not mnt_path.exists() or not dot_path.exists():
    warn(f"data missing in {DATA_DIR} — skipping indicator tests")
else:
    try:
        df_mnt = pd.read_parquet(mnt_path).tail(500)
        df_dot = pd.read_parquet(dot_path).tail(500)
        ok(f"loaded MNT (rows={len(df_mnt)}, range={df_mnt.index[0]} → {df_mnt.index[-1]})")
        ok(f"loaded DOT (rows={len(df_dot)}, range={df_dot.index[0]} → {df_dot.index[-1]})")
        PASSED += 2
    except Exception as e:
        fail("parquet load", e); FAILED += 1
        df_mnt = df_dot = None

    if cfg and df_mnt is not None:
        try:
            mnt_cfg = next(p for p in cfg.pairs if p.symbol == "MNTUSDT")
            ssc = PairStrategyConfig(
                symbol=mnt_cfg.symbol, timeframe=mnt_cfg.timeframe,
                hull_length=mnt_cfg.hull_length,
                tenkan_periods=mnt_cfg.tenkan_periods,
                kijun_periods=mnt_cfg.kijun_periods,
                senkou_b_periods=mnt_cfg.senkou_b_periods,
                displacement=mnt_cfg.displacement,
                risk_pct_per_trade=mnt_cfg.risk_pct_per_trade,
                sl_initial_pct=mnt_cfg.sl_initial_pct,
                tp_pct=mnt_cfg.tp_pct,
                max_hull_spread_pct=mnt_cfg.max_hull_spread_pct,
                max_close_kijun_dist_pct=mnt_cfg.max_close_kijun_dist_pct,
                taker_fee=cfg.portfolio.taker_fee,
            )
            sig = IchimokuSignal(ssc)
            sig.warm_up(df_mnt)
            assert sig.cache is not None
            import numpy as np
            i = len(sig.df) - 1
            assert not np.isnan(sig.cache.n1[i])
            assert not np.isnan(sig.cache.kijun[i])
            ok(f"MNT warmup OK (min_history={ssc.min_history_bars}, cache valid at i={i})")
            PASSED += 1
        except (AssertionError, Exception) as e:
            fail("MNT warmup", e); FAILED += 1

        try:
            decision = sig.evaluate(has_position=None)
            assert isinstance(decision, SignalDecision)
            assert decision.action in {"HOLD", "OPEN_LONG", "OPEN_SHORT"}
            ok(f"MNT.evaluate(no_pos) → {decision.action} @ {decision.price:.4f}")
            PASSED += 1
        except (AssertionError, Exception) as e:
            fail("MNT.evaluate", e); FAILED += 1


# ─── 7. SIZING ────────────────────────────────────────────────────────────
section("7. compute_position_size cu per-pair leverage")

if cfg:
    try:
        equity = 100.0
        for p in cfg.pairs:
            if not p.enabled:
                continue
            eff_lev = cfg.leverage_for(p)
            r = compute_position_size(
                shared_equity=equity, pair_cfg=p,
                portfolio_cfg=cfg.portfolio, balance_broker=equity,
                leverage=eff_lev,
            )
            assert r is not None, f"{p.symbol}: sizing returned None"
            assert r.leverage == eff_lev
            assert r.risk_usd == p.risk_pct_per_trade * equity
            assert abs(r.pos_usd - r.risk_usd / p.sl_initial_pct) < 0.01
            assert r.cap_usd == cfg.portfolio.cap_pct_of_max * equity * cfg.portfolio.leverage_max
            ok(f"{p.symbol}: lev={r.leverage}× risk=${r.risk_usd:.2f} "
               f"pos=${r.pos_usd:.2f} ({r.pos_usd/equity:.2f}× eq) cap=${r.cap_usd:.2f}")
            PASSED += 1
    except (AssertionError, Exception) as e:
        fail("compute_position_size", e); FAILED += 1

    try:
        # qty rounding
        q = compute_qty(pos_usd=233.33, entry_price=0.45, step_size=0.1)
        # 233.33 / 0.45 = 518.51, floored to step 0.1 = 518.5
        assert q == 518.5, f"expected 518.5, got {q}"
        ok(f"compute_qty rounds DOWN to step (518.5 ✓)")
        PASSED += 1
    except (AssertionError, Exception) as e:
        fail("compute_qty", e); FAILED += 1


# ─── 8. CHART_SERVER build ────────────────────────────────────────────────
section("8. chart_server.create_app build")

try:
    # Stub minimal "runner" pentru a permite create_app()
    class _FakeBot:
        account = 100.0
        initial_account = 100.0
        equity_curve: list = []
        trades: list = []
        first_candle_ts = None
        def init_payload(self): return {
            "initial_account": 100.0, "account": 100.0, "first_candle_ts": None,
            "trades": [], "equity_curve": [],
            "summary": {"n_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                        "pnl_total": 0.0, "fees_total": 0.0, "account": 100.0,
                        "return_pct": 0.0},
        }
        def summary(self): return {"n_trades": 0, "account": 100.0}
        def mark_first_candle(self, ts): pass
        def add_closed_trade(self, t): pass

    class _FakeRunner:
        bot = _FakeBot()
        cfg = cfg if cfg else None
        clients: set = set()
        candles_live: list = []
        positions: dict = {}
        paused_symbols: set = set()
        @property
        def paused(self): return False
        @property
        def shared_equity(self): return 100.0
        def primary_pair_key(self):
            if cfg and cfg.pairs:
                return (cfg.pairs[0].symbol, cfg.pairs[0].timeframe)
            return None
        def active_position_payload(self): return None

    runner = _FakeRunner()
    app = create_app(runner)
    assert app is not None
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    expected = {"/", "/api/init", "/api/status", "/ws", "/api/pause",
                "/api/resume", "/api/state"}
    missing = expected - set(routes)
    assert not missing, f"missing routes: {missing}"
    ok(f"create_app: {len(routes)} routes ({', '.join(sorted(expected))})")
    PASSED += 1
except (AssertionError, Exception) as e:
    fail("chart_server.create_app", e); FAILED += 1


# ─── 9. BybitClient interface (no connection) ─────────────────────────────
section("9. BybitClient interface")

try:
    methods = ["fetch_ohlcv", "create_market_order", "set_position_sl",
               "cancel_order", "fetch_balance_usdt", "fetch_position",
               "fetch_pnl_for_trade", "fetch_top_of_book",
               "create_limit_postonly_order", "maker_entry_or_market"]
    for m in methods:
        assert hasattr(BybitClient, m), f"missing method: {m}"
    ok(f"BybitClient has all {len(methods)} expected methods (incl maker_entry_or_market)")
    PASSED += 1
except AssertionError as e:
    fail("BybitClient methods", e); FAILED += 1


# ─── 10. DEDUP & MONOTONIC candles_live ───────────────────────────────────
section("10. _last_confirmed_ts dedup + candles_live monotonic")

if cfg:
    try:
        from ichimoku_bot.main import IchimokuRunner

        # Build a runner with a stub client (no ccxt connection)
        class _StubClient:
            async def fetch_position(self, sym): return None
            async def fetch_market_info(self, sym):
                from ichimoku_bot.exchange.bybit_client import MarketInfo
                return MarketInfo(symbol=sym, qty_step=0.1, qty_min=0.0, tick_size=0.0001)
            async def set_isolated_margin(self, *a, **kw): pass
            async def set_leverage(self, *a, **kw): pass
            async def fetch_ohlcv(self, *a, **kw): return []

        runner = IchimokuRunner(cfg=cfg, client=_StubClient())
        assert hasattr(runner, "_last_confirmed_ts"), "missing _last_confirmed_ts dict"
        assert isinstance(runner._last_confirmed_ts, dict)
        ok("IchimokuRunner has _last_confirmed_ts: dict[str, int]")
        PASSED += 1
    except Exception as e:
        fail("runner init", e); FAILED += 1
        runner = None

    if runner is not None:
        async def _dedup_test():
            # Avoid actually invoking strategy; mock signals dict empty so
            # on_bar returns early (sym not in self.signals).
            # Test the dedup branch by manually populating _last_confirmed_ts.
            sym = "MNTUSDT"
            tf = "4h"
            # Register sym in signals so on_bar passes "sym not in self.signals" check.
            # Add to paused_symbols so on_bar returns BEFORE invoking strategy logic
            # (sig.update_buffer / evaluate / dispatch). We're testing dedup + chart
            # broadcast paths only.
            runner.signals[sym] = "stub"
            runner.paused_symbols.add(sym)
            runner._last_confirmed_ts.clear()
            runner.candles_live.clear()

            # Helper to make a bar dict
            def mk(ts_ms, confirmed, c=0.5):
                return {"symbol": sym, "timeframe": tf, "ts_ms": ts_ms,
                        "open": c, "high": c+0.01, "low": c-0.01, "close": c,
                        "confirmed": confirmed, "volume": 0.0}

            # Patch primary_pair_key to return our pair
            runner.primary_pair_key = lambda: (sym, tf)
            # Stub the broadcast import path used inside on_bar
            import ichimoku_bot.chart_server as cs
            orig_bc = cs.broadcast
            cs.broadcast = lambda r, p: asyncio.sleep(0)  # no-op coro

            # 1. First confirmed bar @ ts=1000s
            await runner.on_bar(mk(1_000_000, True, 0.50))
            assert runner._last_confirmed_ts[sym] == 1000, \
                f"expected last_ts=1000, got {runner._last_confirmed_ts.get(sym)}"
            assert len(runner.candles_live) == 1
            assert runner.candles_live[0][0] == 1000

            # 2. Same bar duplicate (WS retransmit) — must skip
            n_before = len(runner.candles_live)
            await runner.on_bar(mk(1_000_000, True, 0.51))  # different price → if not deduped, would update
            assert len(runner.candles_live) == n_before, "duplicate added a candle"
            assert runner.candles_live[-1][1] == 0.50, "duplicate updated candle (should skip)"

            # 3. Older confirmed (out-of-order delivery) — must skip via dedup
            await runner.on_bar(mk(900_000, True, 0.40))  # ts=900s < last=1000s
            assert runner._last_confirmed_ts[sym] == 1000, "older bar updated last_ts"
            assert len(runner.candles_live) == 1, "older bar leaked into candles_live"

            # 4. New confirmed bar @ ts=2000s — accepted
            await runner.on_bar(mk(2_000_000, True, 0.60))
            assert runner._last_confirmed_ts[sym] == 2000
            assert len(runner.candles_live) == 2
            assert runner.candles_live[-1][0] == 2000

            # 5. Verify monotonic strict increasing
            ts_list = [c[0] for c in runner.candles_live]
            assert ts_list == sorted(ts_list) and len(set(ts_list)) == len(ts_list), \
                "candles_live not strictly monotonic"

            # 6. Unconfirmed tick @ ts=2000 → replace-in-place (different price)
            await runner.on_bar(mk(2_000_000, False, 0.65))
            assert len(runner.candles_live) == 2
            assert runner.candles_live[-1][1] == 0.65, "unconfirmed tick didn't update"

            cs.broadcast = orig_bc
            return True

        try:
            asyncio.run(_dedup_test())
            ok("WS retransmit (same ts) → skipped via _last_confirmed_ts")
            ok("Out-of-order older bar → skipped (last_ts unchanged)")
            ok("candles_live remains strictly monotonic (no duplicates)")
            ok("Unconfirmed tick replaces tail in-place")
            PASSED += 4
        except (AssertionError, Exception) as e:
            fail("dedup + monotonic candles_live", e)
            traceback.print_exc()
            FAILED += 1


# ─── REPORT ───────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
total = PASSED + FAILED
if FAILED == 0:
    print(f"{GREEN}✓ SMOKE TEST PASSED — {PASSED}/{total} checks{RESET}")
    sys.exit(0)
else:
    print(f"{RED}✗ SMOKE TEST FAILED — {PASSED} passed, {FAILED} failed (of {total}){RESET}")
    sys.exit(1)
