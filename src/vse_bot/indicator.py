"""Vortex Sniper Elite (VSE) indicator bundle — port of the original
``vortex_sniper.py`` (@DaviddTech Pine v6 reference implementation).

Components
----------
1. **Baseline**: McGinley Dynamic (length 14, source = close), numba-JIT.
2. **White Line**: ``(highest(high, 20) + lowest(low, 20)) / 2``.
3. **Confirmation 1**: TTM Squeeze (BB 20x2 vs Keltner 20 x {1.0, 1.5, 2.0}).
   * ``no_squeeze`` = BB escaped the widest Keltner (KC x 2.0) — volatility
     expansion.
   * Momentum: ``linreg(close - avg(highest, lowest, sma), 20)``.
   * Long trigger: ``no_squeeze AND momentum > 0 AND momentum rising``.
4. **Confirmation 2**: Tether Line — dual fast (13) / slow (55), each
   ``(highest + lowest) / 2``.
5. **Confirmation 3**: Vortex — ``(VI+ - VI-) > threshold`` (default 0.05).
6. **Exit trailing**: SuperTrend on ``hl2`` with ATR(22) x 3.0, using wicks.

Three signal styles (same indicator bundle, different composition):

- ``"Scalper"``  — baseline + one confirmation.
- ``"Balanced"`` — baseline + two-of-three confirmations (**default**).
- ``"Strict"``   — baseline + **all** confirmations aligned.

This module only exposes the indicator math + raw signal boolean columns.
The VSE **strategy** (entry filtering, SL sizing, exit selection) lives in
``backtest_lab.strategies.examples.vse_2h_balanced`` (step 12c).

Faithful to the upstream math; the only adaptations are:

* ``import pandas_ta_classic as ta`` (fork with numpy-2 support and
  ``tvmode=True, mamode='rma'`` on ``ta.adx`` that VSE relies on).
* Removed the ``main()`` backtest runner (backtest-lab's engine owns that).
* Removed the CSV loader imports (data comes from the parquet cache).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd
import pandas_ta_classic as ta
from numba import njit

SignalStyle = Literal["Scalper", "Balanced", "Strict"]


@dataclass
class VSEConfig:
    """Parameters for the VSE indicator bundle.

    Defaults mirror the @DaviddTech Pine source exactly.
    """

    # Signal composition style.
    style: SignalStyle = "Balanced"

    # Baseline (McGinley)
    mcginley_length: int = 14

    # White Line
    whiteline_length: int = 20

    # TTM Squeeze
    ttms_length: int = 20
    ttms_bb_mult: float = 2.0
    ttms_kc_mult_widest: float = 2.0
    ttms_green_red: bool = True     # use only (rising & > 0) / (falling & < 0)
    ttms_highlight: bool = True     # require no_squeeze
    ttms_cross: bool = True         # edge-trigger (True only on the first bar)

    # Tether Line
    tether_fast: int = 13
    tether_slow: int = 55

    # Vortex
    vortex_length: int = 14
    vortex_threshold: float = 0.05

    # SuperTrend (exit trailing)
    st_atr_length: int = 22
    st_atr_mult: float = 3.0
    st_wicks: bool = True

    # Entry throttle (bars)
    entry_filter_bars: int = 3


# ── McGinley Dynamic (numba) ──────────────────────────────────────────────
@njit(cache=True)
def _mcginley_kernel(
    src: np.ndarray, ema_seed: np.ndarray, length: int, n: int
) -> np.ndarray:
    """``mg[i] = mg[i-1] + (src[i] - mg[i-1]) / (length * (src[i]/mg[i-1])**4)``

    Pine seeds with ``ema(src, len)`` on the first bar; we do the same via
    ``ema_seed``.
    """
    mg = np.zeros(n)
    mg[0] = ema_seed[0]
    for i in range(1, n):
        if np.isnan(mg[i - 1]) or mg[i - 1] == 0.0:
            mg[i] = ema_seed[i]
            continue
        ratio = src[i] / mg[i - 1]
        denom = length * (ratio ** 4)
        if denom == 0.0 or not np.isfinite(denom):
            mg[i] = mg[i - 1]
            continue
        mg[i] = mg[i - 1] + (src[i] - mg[i - 1]) / denom
    return mg


def mcginley_dynamic(src: pd.Series, length: int) -> pd.Series:
    """McGinley Dynamic line; matches the @DaviddTech Pine implementation."""
    ema_seed = ta.ema(src, length=length).bfill().to_numpy()
    mg = _mcginley_kernel(src.to_numpy(), ema_seed, length, len(src))
    return pd.Series(mg, index=src.index, name="mcginley")


# ── SuperTrend (numba) ─────────────────────────────────────────────────────
@njit(cache=True)
def _supertrend_kernel(
    hl2: np.ndarray,
    hp: np.ndarray,
    lp: np.ndarray,
    sdv: np.ndarray,
    n: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute long_stop / short_stop / direction arrays for SuperTrend.

    ``hp`` and ``lp`` are the comparison highs/lows (wicks = use H/L, no
    wicks = use close/close). ``sdv`` is ``atr * multiplier``.
    """
    ls = np.full(n, np.nan)
    ss = np.full(n, np.nan)
    d = np.ones(n, dtype=np.int8)
    cur = 1
    for i in range(n):
        if np.isnan(sdv[i]):
            d[i] = cur
            continue
        cl = hl2[i] - sdv[i]
        cs = hl2[i] + sdv[i]
        pls = ls[i - 1] if i > 0 and not np.isnan(ls[i - 1]) else cl
        pss = ss[i - 1] if i > 0 and not np.isnan(ss[i - 1]) else cs
        plo = lp[i - 1] if i > 0 else lp[i]
        phi = hp[i - 1] if i > 0 else hp[i]
        ls[i] = (cl if cl > pls else pls) if plo > pls else cl
        ss[i] = (cs if cs < pss else pss) if phi < pss else cs
        if cur == -1 and hp[i] > pss:
            cur = 1
        elif cur == 1 and lp[i] < pls:
            cur = -1
        d[i] = cur
    return ls, ss, d


# ── full pipeline ──────────────────────────────────────────────────────────
def compute_indicators(  # noqa: PLR0915
    df: pd.DataFrame,
    cfg: VSEConfig | None = None,
) -> pd.DataFrame:
    """Enrich ``df`` with every VSE indicator column.

    Input columns required: ``open``, ``high``, ``low``, ``close``.
    Returns a copy with added columns (see module docstring for the bundle).
    """
    cfg = cfg or VSEConfig()
    out = df.copy()
    src = out["close"]

    # Baseline: McGinley
    out["baseline"] = mcginley_dynamic(src, cfg.mcginley_length)
    out["baseline_trend"] = np.where(
        out["close"] > out["baseline"],
        1,
        np.where(out["close"] < out["baseline"], -1, 0),
    )

    # White Line
    wl_len = cfg.whiteline_length
    out["white_line"] = (
        out["high"].rolling(wl_len).max() + out["low"].rolling(wl_len).min()
    ) / 2
    out["white_trend"] = np.where(
        out["close"] > out["white_line"],
        1,
        np.where(out["close"] < out["white_line"], -1, 0),
    )

    # TTM Squeeze
    ttl = cfg.ttms_length
    basis = ta.sma(src, length=ttl)
    dev = src.rolling(ttl).std(ddof=0)
    bb_upper = basis + cfg.ttms_bb_mult * dev
    bb_lower = basis - cfg.ttms_bb_mult * dev
    tr = ta.true_range(out["high"], out["low"], out["close"])
    kc_dev = ta.sma(tr, length=ttl)
    kc_upper_widest = basis + kc_dev * cfg.ttms_kc_mult_widest
    kc_lower_widest = basis - kc_dev * cfg.ttms_kc_mult_widest
    out["no_squeeze"] = (bb_lower < kc_lower_widest) | (bb_upper > kc_upper_widest)

    # TTMS momentum — linreg(close - avg(HH, LL, SMA), length)
    hh = out["high"].rolling(ttl).max()
    ll = out["low"].rolling(ttl).min()
    price_avg = ((hh + ll) / 2 + basis) / 2
    diff = src - price_avg
    out["ttms_momentum"] = ta.linreg(diff, length=ttl, offset=0)

    mom = out["ttms_momentum"]
    mom_prev = mom.shift(1)
    signals = np.where(
        (mom > 0) & (mom > mom_prev),
        1,
        np.where(
            (mom > 0) & (mom <= mom_prev),
            2,
            np.where(
                (mom < 0) & (mom < mom_prev),
                -1,
                np.where((mom < 0) & (mom >= mom_prev), -2, 0),
            ),
        ),
    )
    out["ttms_signal"] = signals
    if cfg.ttms_green_red:
        out["ttms_basic_long"] = signals == 1
        out["ttms_basic_short"] = signals == -1
    else:
        out["ttms_basic_long"] = signals > 0
        out["ttms_basic_short"] = signals < 0
    if cfg.ttms_highlight:
        out["ttms_long_signal"] = out["ttms_basic_long"] & out["no_squeeze"]
        out["ttms_short_signal"] = out["ttms_basic_short"] & out["no_squeeze"]
    else:
        out["ttms_long_signal"] = out["ttms_basic_long"]
        out["ttms_short_signal"] = out["ttms_basic_short"]
    if cfg.ttms_cross:
        ls_bool = out["ttms_long_signal"].fillna(False).astype(bool)
        ss_bool = out["ttms_short_signal"].fillna(False).astype(bool)
        out["ttms_long_final"] = ls_bool & ~ls_bool.shift(1, fill_value=False)
        out["ttms_short_final"] = ss_bool & ~ss_bool.shift(1, fill_value=False)
    else:
        out["ttms_long_final"] = out["ttms_long_signal"]
        out["ttms_short_final"] = out["ttms_short_signal"]

    # Tether (dual)
    tf = cfg.tether_fast
    tss = cfg.tether_slow
    fast_hh = out["high"].rolling(tf).max()
    fast_ll = out["low"].rolling(tf).min()
    slow_hh = out["high"].rolling(tss).max()
    slow_ll = out["low"].rolling(tss).min()
    out["tether_fast"] = (fast_hh + fast_ll) / 2
    out["tether_slow"] = (slow_hh + slow_ll) / 2
    out["tether_long"] = (out["tether_fast"] > out["tether_slow"]) & (
        out["close"] > out["tether_slow"]
    )
    out["tether_short"] = (out["tether_fast"] < out["tether_slow"]) & (
        out["close"] < out["tether_slow"]
    )

    # Vortex
    vl = cfg.vortex_length
    vm_plus = (out["high"] - out["low"].shift(1)).abs()
    vm_minus = (out["low"] - out["high"].shift(1)).abs()
    vplus = vm_plus.rolling(vl).sum()
    vminus = vm_minus.rolling(vl).sum()
    sum_tr = tr.rolling(vl).sum()
    out["vi_plus"] = vplus / sum_tr
    out["vi_minus"] = vminus / sum_tr
    out["vortex_long"] = (out["vi_plus"] - out["vi_minus"]) > cfg.vortex_threshold
    out["vortex_short"] = (out["vi_minus"] - out["vi_plus"]) > cfg.vortex_threshold

    # SuperTrend
    hl2 = (out["high"] + out["low"]) / 2
    atr_st = ta.atr(out["high"], out["low"], out["close"], length=cfg.st_atr_length)
    out["atr_st"] = atr_st
    sd = cfg.st_atr_mult * atr_st
    hp = out["high"] if cfg.st_wicks else out["close"]
    lp = out["low"] if cfg.st_wicks else out["close"]
    ls_a, ss_a, d_a = _supertrend_kernel(
        hl2.to_numpy(), hp.to_numpy(), lp.to_numpy(), sd.to_numpy(), len(out)
    )
    out["long_stop"] = ls_a
    out["short_stop"] = ss_a
    out["stop_dir"] = d_a
    return out


def build_signals(df: pd.DataFrame, cfg: VSEConfig | None = None) -> pd.DataFrame:
    """Add boolean ``raw_long`` / ``raw_short`` columns from the VSE bundle.

    Expects the full output of :func:`compute_indicators` as input.
    """
    cfg = cfg or VSEConfig()
    out = df.copy()
    bl_up = out["baseline_trend"] == 1
    bl_dn = out["baseline_trend"] == -1
    wl_up = out["white_trend"] == 1
    wl_dn = out["white_trend"] == -1
    t_l = out["ttms_long_final"].fillna(False).astype(bool)
    t_s = out["ttms_short_final"].fillna(False).astype(bool)
    th_l = out["tether_long"].fillna(False).astype(bool)
    th_s = out["tether_short"].fillna(False).astype(bool)
    v_l = out["vortex_long"].fillna(False).astype(bool)
    v_s = out["vortex_short"].fillna(False).astype(bool)

    if cfg.style == "Strict":
        rl = bl_up & wl_up & t_l & th_l & v_l
        rs = bl_dn & wl_dn & t_s & th_s & v_s
    elif cfg.style == "Scalper":
        rl = bl_up & (t_l | th_l) & v_l
        rs = bl_dn & (t_s | th_s) & v_s
    else:  # Balanced
        count_l = t_l.astype(int) + th_l.astype(int) + v_l.astype(int)
        count_s = t_s.astype(int) + th_s.astype(int) + v_s.astype(int)
        # TTMS trigger is the edge we anchor on, so require it explicitly.
        rl = bl_up & (count_l >= 2) & t_l
        rs = bl_dn & (count_s >= 2) & t_s

    out["raw_long"] = rl.fillna(False).astype(bool)
    out["raw_short"] = rs.fillna(False).astype(bool)
    return out


__all__ = [
    "SignalStyle",
    "VSEConfig",
    "build_signals",
    "compute_indicators",
    "mcginley_dynamic",
]
