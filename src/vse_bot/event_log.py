"""JSONL event logger — un fișier per subaccount.

Regula 11: log-uri detaliate cu timestamps pentru:
  - BAR_RECEIVED (candele noi cu confirmed=True)
  - INDICATORS_COMPUTED (când rolling buffer recompute)
  - SIGNAL_EVALUATED (raw_long/raw_short pe ultima bară)
  - FILTER_REJECTED (sl_pct out of bounds, cooldown, margin etc.)
  - TRADE_OPENED, TRADE_CLOSED
  - TRAILING_UPDATE
  - OPP_EXIT_PLANNED, OPP_EXIT_EXECUTED
  - RESET, POOL_LOW, CYCLE_SUCCESS, BOOT
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_event(
    log_dir: Path,
    subacc_name: str,
    event_type: str,
    **kwargs: Any,
) -> None:
    """Append JSON record la <log_dir>/<subacc_name>.jsonl cu timestamp ISO."""
    log_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "subacc": subacc_name,
        **kwargs,
    }
    path = log_dir / f"{subacc_name}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    # Also stdout pentru Docker logs (regula 14: line_buffering deja activ)
    print(f"  [{event_type}] {subacc_name}  " + "  ".join(
        f"{k}={v}" for k, v in kwargs.items()
        if k not in ("raw",) and not isinstance(v, (list, dict))
    ))
