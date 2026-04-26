"""Live VSE signal generator pe rolling buffer (un wrapper per pereche).

Spec source: STRATEGY_LOGIC.md secțiunea 3.

Mod de utilizare:
  sig = VSESignalLive(strategy_cfg, indicator_cfg, lookback=400)
  pe fiecare bară confirmată (close):
      result = sig.update(bar)   # dict | None
      dacă None → fără semnal pe bara curentă (sau buffer încă în warm-up)
      dacă dict → entry valid {side, entry_price, sl_price, sl_pct, ts}

Output-ul nu e ordin pe Bybit — e doar semnalul. Decizia de a deschide trade
se face în trade_lifecycle.open_trade_live după ce verifică margin/cooldown
la nivel de subaccount.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from vse_bot.config import IndicatorConfig, StrategyConfig
from vse_bot.indicator import VSEConfig, build_signals, compute_indicators


@dataclass
class LiveSignal:
    side: str          # "long" | "short"
    entry_price: float
    sl_price: float
    sl_pct: float
    ts: pd.Timestamp


class VSESignalLive:
    """Rolling buffer + recompute pe bară nouă; emite signal sau None.

    Filtrele aplicate (în ordine):
      1. warm-up: cere min ``warmup_bars`` (default 100, suficient pt SuperTrend(22)).
      2. raw_long / raw_short pe ULTIMA bară.
      3. duplicate guard (același ts).
      4. SL valid: long_stop / short_stop nu NaN, pe partea corectă a close-ului.
      5. SL bounds: ``sl_min_pct ≤ sl_pct ≤ sl_max_pct``.
      6. cooldown: bare de la last_signal_ts.

    NU aplică ADX gate (spec-ul Nou1 nu îl folosește).
    """

    def __init__(
        self,
        strategy_cfg: StrategyConfig,
        indicator_cfg: IndicatorConfig,
        symbol: str,
        timeframe: str,
        lookback_bars: int = 400,
        warmup_bars: int = 100,
    ) -> None:
        self.strategy_cfg = strategy_cfg
        self.indicator_cfg = indicator_cfg
        self.symbol = symbol
        self.timeframe = timeframe
        self.lookback_bars = lookback_bars
        self.warmup_bars = warmup_bars

        self._vse_cfg = VSEConfig(
            style=strategy_cfg.style,
            mcginley_length=indicator_cfg.mcginley_length,
            whiteline_length=indicator_cfg.whiteline_length,
            ttms_length=indicator_cfg.ttms_length,
            ttms_bb_mult=indicator_cfg.ttms_bb_mult,
            ttms_kc_mult_widest=indicator_cfg.ttms_kc_mult_widest,
            tether_fast=indicator_cfg.tether_fast,
            tether_slow=indicator_cfg.tether_slow,
            vortex_length=indicator_cfg.vortex_length,
            vortex_threshold=indicator_cfg.vortex_threshold,
            st_atr_length=indicator_cfg.st_atr_length,
            st_atr_mult=indicator_cfg.st_atr_mult,
            entry_filter_bars=strategy_cfg.cooldown_bars,
        )

        self._buffer = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"]
        )
        self._last_signal_ts: pd.Timestamp | None = None
        # last_event_ts = last entry sau ultima bară când signal_reverse / SL închideau
        # poziția — folosit pt cooldown. Setat extern de orchestrator (sau în replay).
        self._last_exit_ts: pd.Timestamp | None = None

    # ── Public API ────────────────────────────────────────────────────────
    def warm_up(self, history: pd.DataFrame) -> None:
        """Pre-populate buffer-ul cu istoric (la startup). Nu emite semnale."""
        keep = history.tail(self.lookback_bars)
        self._buffer = keep.copy()

    def mark_position_closed(self, ts: pd.Timestamp) -> None:
        """Cooldown clock: orchestrator trebuie să anunțe închiderea poziției."""
        self._last_exit_ts = ts

    def update(self, new_bar: dict[str, Any]) -> LiveSignal | None:
        """Append bara nouă (CLOSE-confirmed) și încearcă să emită un signal.

        ``new_bar`` trebuie să aibă cheile: ``ts`` (pd.Timestamp UTC), ``open``,
        ``high``, ``low``, ``close``, ``volume``.
        """
        ts = pd.Timestamp(new_bar["ts"])
        if self._last_signal_ts is not None and ts <= self._last_signal_ts:
            # bară în trecut sau duplicat
            return None

        row = pd.DataFrame(
            [{
                "open": float(new_bar["open"]),
                "high": float(new_bar["high"]),
                "low": float(new_bar["low"]),
                "close": float(new_bar["close"]),
                "volume": float(new_bar.get("volume", 0.0)),
            }],
            index=pd.DatetimeIndex([ts], tz="UTC", name="ts"),
        )
        # tz-align with buffer if needed
        if not self._buffer.empty and self._buffer.index.tz is None:
            row.index = row.index.tz_localize(None)
        self._buffer = pd.concat([self._buffer, row]).tail(self.lookback_bars)

        if len(self._buffer) < self.warmup_bars:
            return None

        ind = compute_indicators(self._buffer, self._vse_cfg)
        sig = build_signals(ind, self._vse_cfg)
        last = sig.iloc[-1]

        is_long = bool(last["raw_long"])
        is_short = bool(last["raw_short"])
        if not (is_long or is_short):
            return None

        side = "long" if is_long else "short"
        sl_price_raw = last["long_stop"] if is_long else last["short_stop"]
        if pd.isna(sl_price_raw):
            return None
        sl_price = float(sl_price_raw)
        entry_price = float(last["close"])
        if entry_price <= 0:
            return None

        # SL pe partea corectă a close-ului
        if side == "long" and sl_price >= entry_price:
            return None
        if side == "short" and sl_price <= entry_price:
            return None

        sl_dist = (entry_price - sl_price) if side == "long" else (sl_price - entry_price)
        if sl_dist <= 0:
            return None
        sl_pct = sl_dist / entry_price
        if sl_pct < self.strategy_cfg.sl_min_pct or sl_pct > self.strategy_cfg.sl_max_pct:
            return None

        # Cooldown: bare de la ultimul exit (sau ultimul semnal dacă nu am exit yet)
        cooldown_ref_ts = self._last_exit_ts or self._last_signal_ts
        if cooldown_ref_ts is not None:
            bars_since = self._bars_between(cooldown_ref_ts, ts)
            if bars_since < self.strategy_cfg.cooldown_bars:
                return None

        self._last_signal_ts = ts

        return LiveSignal(
            side=side,
            entry_price=entry_price,
            sl_price=sl_price,
            sl_pct=sl_pct,
            ts=ts,
        )

    # ── Helpers ───────────────────────────────────────────────────────────
    def latest_supertrend(self) -> tuple[float, float] | None:
        """(long_stop, short_stop) pe ultima bară — folosit pentru trailing.

        Întoarce None dacă buffer-ul nu e suficient. Recomputația e completă
        (ieftin pe lookback ~400 bare).
        """
        if len(self._buffer) < self.warmup_bars:
            return None
        ind = compute_indicators(self._buffer, self._vse_cfg)
        last = ind.iloc[-1]
        ls = last["long_stop"]
        ss = last["short_stop"]
        if pd.isna(ls) or pd.isna(ss):
            return None
        return float(ls), float(ss)

    def latest_raw_signals(self) -> tuple[bool, bool] | None:
        """(raw_long, raw_short) pe ultima bară — folosit pentru OPP exit.

        Spec: STRATEGY_LOGIC.md sec 6 — OPP exit pe opposite RAW signal pe
        bara închisă, exit la NEXT bar open.
        """
        if len(self._buffer) < self.warmup_bars:
            return None
        ind = compute_indicators(self._buffer, self._vse_cfg)
        sig = build_signals(ind, self._vse_cfg)
        last = sig.iloc[-1]
        return bool(last["raw_long"]), bool(last["raw_short"])

    def _bars_between(self, t0: pd.Timestamp, t1: pd.Timestamp) -> int:
        """Aproximare folosind index-ul buffer-ului. Returnează 0 dacă t0 nu e găsit."""
        if t0 not in self._buffer.index or t1 not in self._buffer.index:
            return 999  # safe — nu blochează
        i0 = self._buffer.index.get_loc(t0)
        i1 = self._buffer.index.get_loc(t1)
        return int(i1 - i0)
