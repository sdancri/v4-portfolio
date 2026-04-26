"""Smoke test: indicator + signal generator pe parquet real KAIA 1H."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
import pytest

from vse_bot.config import load_config
from vse_bot.indicator import build_signals, compute_indicators
from vse_bot.replay import _build_vse_config, prepare_pair


KAIA_PATH = Path("/home/dan/Python/Test_Python/data/ohlcv/KAIAUSDT_1h.parquet")


@pytest.fixture
def cfg():
    return load_config(ROOT / "config" / "config.yaml")


@pytest.mark.skipif(not KAIA_PATH.exists(), reason="parquet missing")
def test_compute_indicators_runs_on_real_data(cfg):
    df = pd.read_parquet(KAIA_PATH).iloc[:500]
    vse = _build_vse_config(cfg.strategy, cfg.indicator)
    out = compute_indicators(df, vse)
    assert "baseline" in out.columns
    assert "long_stop" in out.columns
    assert "short_stop" in out.columns
    # Cele mai multe valori valide după warm-up
    assert out["long_stop"].iloc[100:].notna().sum() > 0


@pytest.mark.skipif(not KAIA_PATH.exists(), reason="parquet missing")
def test_build_signals_emits_some_long_or_short(cfg):
    df = pd.read_parquet(KAIA_PATH).iloc[:2000]
    vse = _build_vse_config(cfg.strategy, cfg.indicator)
    sig = build_signals(compute_indicators(df, vse), vse)
    n_long = int(sig["raw_long"].sum())
    n_short = int(sig["raw_short"].sum())
    # KAIA pe 2000 bare ar trebui să dea minim câteva semnale
    assert n_long + n_short > 0


@pytest.mark.skipif(not KAIA_PATH.exists(), reason="parquet missing")
def test_prepare_pair_filters_signals_with_sl_bounds(cfg):
    from vse_bot.config import PairConfig

    df = pd.read_parquet(KAIA_PATH).iloc[:2000]
    pdata = prepare_pair(
        df,
        PairConfig(symbol="KAIAUSDT", timeframe="1h"),
        cfg.strategy,
        cfg.indicator,
    )
    # signal_dir e 0 / +1 / -1
    import numpy as np
    assert set(np.unique(pdata.signal_dir)).issubset({-1, 0, 1})
