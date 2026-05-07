"""rate_limiter.py — Token bucket pentru REST requests (port din boilerplate).

Bybit V5 limits (2026):
  - Market data (public, unsigned):  120 req/5s per IP    = 24 req/s
  - Order management (signed):        10 req/s per UID
  - Position endpoints (signed):      10 req/s per UID

ccxt are deja un rate limiter intern (``enableRateLimit=True`` la init), dar
e per-request based pe ``rateLimit`` ms hardcodat de ccxt — nu e burst-aware
si nu coordoneaza intre task-uri concurrent. Acest TokenBucket e un strat
suplimentar global (defense-in-depth) care:

  - protejeaza la burst-uri (ex multiple ``fetch_pnl_for_trade`` la close)
  - lasa ccxt sa-si faca treaba normala, dar cap-uieste rata totala
  - permite tuning via env (``RATE_LIMIT_PER_SEC``, ``RATE_LIMIT_BURST``)

Defaults conservative (5 req/s, burst 10) — Bybit testnet are aceleasi
limite ca production.
"""

from __future__ import annotations

import asyncio
import os
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate = float(rate_per_sec)
        self.burst = int(burst)
        self.tokens: float = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        """Asteapta un token, il consuma, returneaza secundele asteptate."""
        async with self._lock:
            waited = 0.0
            while True:
                now = time.monotonic()
                elapsed = now - self._last
                self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
                self._last = now

                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return waited

                need = 1.0 - self.tokens
                wait = need / self.rate
                waited += wait
                await asyncio.sleep(wait)


_bucket = TokenBucket(
    rate_per_sec=float(os.getenv("RATE_LIMIT_PER_SEC", "5")),
    burst=int(os.getenv("RATE_LIMIT_BURST", "10")),
)


async def wait_token() -> None:
    """Apelat inainte de fiecare REST call. Async, non-blocant."""
    waited = await _bucket.acquire()
    if waited > 0.1:
        print(f"  [RATE] Throttled {waited*1000:.0f}ms pt protectie rate limits")
