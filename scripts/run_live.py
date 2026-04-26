"""Entry point pentru bot-ul live (testnet sau mainnet).

Comportament:
  - Citește config/config.yaml + .env (TRADING_MODE, SUB1_*, SUB2_* etc.).
  - Pornește un BybitKlineWS partajat și un SubaccountRunner per subaccount.
  - State persistă în ./state/<subacc>_state.json după fiecare close.

Atenție:
  ÎNAINTE de a rula live, validează în ORDINE:
    1. python scripts/run_replay.py        (match cu strategy.md targets — pas făcut)
    2. python scripts/run_live.py          (cu TRADING_MODE=testnet, 2-4 săptămâni)
    3. live $50/subacc → live $100/subacc  (manual)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vse_bot.main import main

if __name__ == "__main__":
    main()
