"""no_lookahead.py — Utilitati anti-lookahead (port din boilerplate).

Lookahead = folosirea de date care nu ar fi fost disponibile la momentul
cand strategia ia decizia. La warmup-ul indicatorilor, ultimul kline din
``fetch_ohlcv`` poate fi bara IN CURS (incomplete) — daca o folosim, signal
generation e bias-uit pe close-ul nefinal.

Functia principala folosita in proiect: ``filter_closed_bars`` — elimina
barele al caror open_ms >= bara curenta deschisa.

Adaptat la timeframe ccxt ("1h", "4h", "1d") in loc de Bybit V5 raw ("60",
"240", "D"). API publica:

    bars = await client.fetch_ohlcv("MNTUSDT", "4h", limit=400)
    bars = [{"ts": ms, "open":..., ...} for row in bars]
    closed = filter_closed_bars(bars, "4h")
"""

from __future__ import annotations

import time


# Durata barei pentru fiecare timeframe ccxt (ms)
_INTERVAL_MS = {
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
    """Timeframe ccxt ('4h', '1h', '1d', ...) -> milisecunde."""
    ms = _INTERVAL_MS.get(timeframe.lower())
    if ms is None:
        raise ValueError(f"Unknown timeframe: {timeframe}")
    return ms


def current_bar_open_ms(now_ms: int, timeframe: str) -> int:
    """Open time al barei IN CURS (cea care inca se formeaza)."""
    imms = interval_ms(timeframe)
    return (now_ms // imms) * imms


def last_closed_bar_open_ms(now_ms: int, timeframe: str) -> int:
    """Open time al ultimei bare INCHISE (disponibile la decizie)."""
    return current_bar_open_ms(now_ms, timeframe) - interval_ms(timeframe)


def filter_closed_bars(
    bars: list[list[float]] | list[dict],
    timeframe: str,
    now_ms: int | None = None,
) -> list:
    """Elimina barele a caror ts >= bara curenta deschisa.

    Acepta atat formatul ccxt raw (list[list[float]] cu ts pe index 0) cat
    si formatul dict (cu cheia "ts"). Returneaza acelasi tip ca input-ul.
    """
    if not bars:
        return bars
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    cutoff = current_bar_open_ms(now_ms, timeframe)
    if isinstance(bars[0], dict):
        return [b for b in bars if int(b["ts"]) < cutoff]
    return [row for row in bars if int(row[0]) < cutoff]
