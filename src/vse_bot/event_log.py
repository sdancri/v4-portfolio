"""JSONL event logger — un fișier per subaccount + rotation 10MB × 7 backups.

Regula 11: log-uri detaliate cu timestamps pentru:
  - BAR_RECEIVED (candele noi cu confirmed=True)
  - INDICATORS_COMPUTED (când rolling buffer recompute)
  - SIGNAL_EVALUATED (raw_long/raw_short pe ultima bară)
  - FILTER_REJECTED (sl_pct out of bounds, cooldown, margin etc.)
  - TRADE_OPENED, TRADE_CLOSED
  - TRAILING_UPDATE
  - OPP_EXIT_PLANNED, OPP_EXIT_EXECUTED
  - RESET, POOL_LOW, CYCLE_SUCCESS, BOOT
  - RECONCILE_OK / RECONCILE_PAUSED
  - MANUAL_PAUSE, MANUAL_RESUME, WITHDRAW_CONFIRMED

Rotation: 10MB max per file, max 7 backup-uri per subaccount.
La rotation: <subacc>.jsonl → <subacc>.jsonl.1, .1 → .2, etc.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# Cache logger per subaccount ca să nu duplicăm handler-ele
_loggers: dict[str, logging.Logger] = {}


def _get_logger(log_dir: Path, subacc_name: str) -> logging.Logger:
    """Returnează (sau creează) logger cu RotatingFileHandler 10MB × 7 backups."""
    if subacc_name in _loggers:
        return _loggers[subacc_name]
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"vse_bot.{subacc_name}")
    logger.setLevel(logging.INFO)
    logger.propagate = False    # nu trimite la root logger
    # Evită handler dublu dacă _get_logger e apelat din nou (caz rar)
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_dir / f"{subacc_name}.jsonl",
            maxBytes=10 * 1024 * 1024,    # 10 MB
            backupCount=7,                 # subacc.jsonl.1 … .7
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))   # JSONL pur
        logger.addHandler(handler)
    _loggers[subacc_name] = logger
    return logger


def log_event(
    log_dir: Path,
    subacc_name: str,
    event_type: str,
    **kwargs: Any,
) -> None:
    """Append JSON record la <log_dir>/<subacc_name>.jsonl cu timestamp ISO.

    Folosește RotatingFileHandler — la 10MB se rotește automat.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "subacc": subacc_name,
        **kwargs,
    }
    logger = _get_logger(log_dir, subacc_name)
    logger.info(json.dumps(record, default=str))
    # Also stdout pentru Docker logs (regula 14: line_buffering deja activ)
    print(f"  [{event_type}] {subacc_name}  " + "  ".join(
        f"{k}={v}" for k, v in kwargs.items()
        if k not in ("raw",) and not isinstance(v, (list, dict))
    ))
