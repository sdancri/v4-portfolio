# ICHIMOKU bot

Trading bot multi-pair (MNT + DOT 4h) pentru Bybit perpetual, bazat pe strategia
Hull MA + Ichimoku Cloud cu **shared equity compounding**.

## Performance backtest (validat OOS)

| Period | Setup | N | WR | PF | Ret% | DD% | Final |
|---|---|---|---|---|---|---|---|
| 2022-01 → 2026-05 | MNT(7%, no_TP) + DOT(5%, TP=12%) | 1,009 | 39.8% | **1.34** | **+6,685%** | -42% | $6,785 |

Strategy validate OOS pe MNTUSDT (ROBUST: PF 1.72→1.53) si DOTUSDT (ROBUST: PF 1.14→1.34).

## Stack

- **MNTUSDT 4h** — Hull=8, Kijun=48, SnkB=40, sizing 7%, TP=signal-only
- **DOTUSDT 4h** — Hull=10, Kijun=36, SnkB=52, sizing 5%, TP=12%
- SL fix 5% pe ambele
- Filtre: max_hull_spread 2%, max_close_kijun_dist 6%
- Capital initial: $100 (compound shared)
- Leverage: 15× isolated

## Deploy

### 1. Setup credentials

```bash
cp .env.example .env
# Editeaza .env:
#   BYBIT_API_KEY=...
#   BYBIT_API_SECRET=...
#   TRADING_MODE=testnet     # incepe cu testnet 2-4 saptamani
#   TELEGRAM_TOKEN=...       # optional
#   TELEGRAM_CHAT_ID=...     # optional
```

### 2. Validare locala (testnet)

```bash
docker compose up -d ichimoku
docker logs -f ichimoku
# Chart: http://localhost:8103/
```

### 3. Deploy in Portainer (DigitalOcean VPS)

1. Build & push imagine: `docker build -t sdancri/ichimoku:latest . && docker push sdancri/ichimoku:latest`
2. Portainer → Stacks → Add stack → paste `docker-compose.yml`
3. Adauga env vars: `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `TRADING_MODE=live`
4. Deploy

## Telegram notifications

| Eveniment | Mesaj |
|---|---|
| Boot | `BOT STARTED ✅` |
| Strategy ready (warmup done) | `STRATEGY READY ✅` |
| Entry | `🚀 ENTRY LONG — MNTUSDT` (sau `📉 ENTRY SHORT`) |
| Exit | `📈 TRADE CLOSED — MNTUSDT (TP)` (sau SL/SIGNAL/EXTERNAL/BYBIT_SL) |
| Stop | `🛑 BOT STOPPED` |
| Crash | `RECONCILE PAUSED` / `OPEN FAILED` / `CLOSE FAILED` (interventie manuala) |

## Structura

```
ICHIMOKU/
├── config/
│   └── config.yaml            # parametri MNT/DOT (Hull, Kijun, TP, sizing)
├── src/ichimoku_bot/
│   ├── main.py                # IchimokuRunner — orchestrator
│   ├── ichimoku_signal.py     # Hull+Ichimoku indicator + signal eval
│   ├── config.py              # config schema
│   ├── sizing.py              # position sizing
│   ├── bot_state.py           # equity tracking + trade records
│   ├── chart_server.py        # FastAPI chart UI
│   ├── telegram_bot.py        # TG notifications
│   ├── event_log.py           # JSONL events
│   └── exchange/              # Bybit V5 client + WS
├── scripts/
│   ├── run_live.py            # entry point
│   ├── diag_bybit.py          # diagnose API
│   └── preflight_check.py     # pre-deployment checks
├── static/                    # chart UI
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Diferente fata de VSE

| | VSE | ICHIMOKU |
|---|---|---|
| Strategie | VSE multi-indicator | Hull+Ichimoku |
| Cycle manager | yes (reset/withdraw) | **NO** (compound only) |
| OPP exit + reverse | yes | NO (signal exit only) |
| Trailing stop | SuperTrend | NO (SL fix 5%) |
| Take profit | NO (trailing only) | per pair (None / 12%) |
| TF | 1h / 2h | 4h |
| Subaccounts/process | 1 | 1 |

## Validare deployment

⚠️ Validare in **ORDINE**:
1. **Replay backtest** — validat (vezi tabel performance)
2. **Testnet 2-4 saptamani** — `TRADING_MODE=testnet`, $100 paper
3. **Live $50** — capital mic, 1-2 saptamani monitorizare
4. **Live $100+** — dupa ce comportamentul live se confirma

NO state persistence — restart = istoric de la 0. La startup detecteaza
pozitii reziduale Bybit si pauzeaza simbolul afectat.
