"""no_lookahead.py — utilitati anti-lookahead (port din boilerplate).

Folosit pentru a elimina BARA CURENTA IN FORMARE din rezultatul fetch_ohlcv
ÎNAINTE de a o adăuga în buffer-ul de warmup. Bybit V5 returnează implicit
și bara în formare; dacă o lăsam să intre în buffer, la prima livrare WS
confirmed pentru aceeași bară, se crea index duplicate → indicators corupți.

Format timeframe: ccxt-style ("1h", "2h", "30m", "1d") — diferit de
boilerplate-ul original care folosea Bybit raw ("60", "120", "30", "D").
"""
from __future__ import annotations

import time

# Durata unei bare în ms, format ccxt
_INTERVAL_MS_CCXT: dict[str, int] = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "2h":  7_200_000,
    "4h":  14_400_000,
    "6h":  21_600_000,
    "12h": 43_200_000,
    "1d":  86_400_000,
    "1w":  604_800_000,
}


def interval_ms(timeframe: str) -> int:
    """ccxt timeframe ('1h', '2h', '30m', '1d') -> milisecunde."""
    ms = _INTERVAL_MS_CCXT.get(timeframe)
    if ms is None:
        raise ValueError(f"Unknown timeframe: {timeframe}")
    return ms


def current_bar_open_ms(now_ms: int, timeframe: str) -> int:
    """Ora deschiderii barei curente (în formare)."""
    imms = interval_ms(timeframe)
    return (now_ms // imms) * imms


def filter_closed_bars(bars: list[list[float]], timeframe: str,
                       now_ms: int | None = None) -> list[list[float]]:
    """Filtrează lista OHLCV (format ccxt: [ts_ms, o, h, l, c, v]) — păstrează
    DOAR barele cu ts < bara curentă deschisă (toate confirmed, fără cea în formare).
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    cutoff = current_bar_open_ms(now_ms, timeframe)
    return [b for b in bars if int(b[0]) < cutoff]
