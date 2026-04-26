"""BotState — UI tracking + real account (regula 5: account += real PnL Bybit).

Diferit de ``SubaccountState`` (care e cycle-logic pur):
  - ``account`` = $100 + cumulative REAL PnL Bybit (după fiecare trade close).
  - ``trades`` = lista de TradeRecord (afișată în panel chart).
  - ``equity_curve`` = (ts_s, account) după fiecare close.
  - ``first_candle_ts`` = timestamp-ul primei lumânări LIVE primite (regula 4 +
    10: chart afișează DOAR de la prima bară live, dar indicatorii calculați
    din urmă pe warmup).

NU folosim persistence pe disk (regula 2: restart = de la 0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class TradeRecord:
    """Trade închis — PnL REAL Bybit."""
    id: int
    date: str                       # "YYYY-MM-DD"
    direction: str                  # "LONG" | "SHORT"
    symbol: str
    entry_ts_ms: int
    entry_price: float
    sl_price: float
    tp_price: float | None
    qty: float
    exit_ts_ms: int
    exit_price: float
    exit_reason: str                # "TS" | "OPP" | "MANUAL" | "CYCLE_SUCCESS"
    pnl: float                      # USDT REAL Bybit
    fees: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "date": self.date,
            "direction": self.direction,
            "side": "L" if self.direction == "LONG" else "S",
            "symbol": self.symbol,
            "entry_ms": self.entry_ts_ms,
            "entry_price": round(self.entry_price, 6),
            "sl": round(self.sl_price, 6),
            "tp": round(self.tp_price, 6) if self.tp_price else 0,
            "qty": round(self.qty, 6),
            "size_usdt": round(self.qty * self.entry_price, 2),
            "exit_ms": self.exit_ts_ms,
            "exit_price": round(self.exit_price, 6),
            "exit_reason": self.exit_reason,
            "pnl": round(self.pnl, 4),
            "fees": round(self.fees, 4),
            "extra": self.extra,
        }


@dataclass
class BotState:
    """Tracking real Bybit (account + trades + equity curve)."""
    initial_account: float = 100.0
    account: float = 100.0
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    first_candle_ts: int | None = None      # secunde UTC

    def __post_init__(self) -> None:
        if not self.equity_curve:
            self.equity_curve.append((int(datetime.now(timezone.utc).timestamp()), self.account))

    def mark_first_candle(self, ts_s: int) -> None:
        """Setează first_candle_ts dacă nu e deja setat (idempotent)."""
        if self.first_candle_ts is None:
            self.first_candle_ts = ts_s

    def add_closed_trade(self, trade: TradeRecord) -> None:
        """Adaugă trade + actualizează account cu PnL REAL."""
        trade.id = len(self.trades) + 1
        self.trades.append(trade)
        self.account += trade.pnl    # ← Regula 5: doar PnL real Bybit
        # equity curve point la exit_ts
        self.equity_curve.append((trade.exit_ts_ms // 1000, self.account))

    def summary(self) -> dict:
        """Schema match cu chart_live.html boilerplate (folosește pnl_total / fees_total)."""
        if not self.trades:
            return {
                "n_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "pnl_total": 0.0, "fees_total": 0.0,
                "account": self.account,
                "return_pct": 0.0,
            }
        wins = sum(1 for t in self.trades if t.pnl > 0)
        losses = sum(1 for t in self.trades if t.pnl <= 0)
        n = len(self.trades)
        pnl_total = sum(t.pnl for t in self.trades)
        fees_total = sum(t.fees for t in self.trades)
        return {
            "n_trades": n,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / n * 100 if n else 0.0,
            "pnl_total": round(pnl_total, 2),
            "fees_total": round(fees_total, 2),
            "account": round(self.account, 2),
            "return_pct": round((self.account / self.initial_account - 1) * 100, 2),
        }

    def init_payload(self) -> dict:
        """Payload trimis la chart la /api/init."""
        return {
            "initial_account": self.initial_account,
            "account": self.account,
            "first_candle_ts": self.first_candle_ts,
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": [
                {"time": ts, "value": round(eq, 4)}
                for ts, eq in self.equity_curve
            ],
            "summary": self.summary(),
        }
