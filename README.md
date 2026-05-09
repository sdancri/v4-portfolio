# V4 Portfolio Bot

Multi-strategy trading bot pe Bybit perpetuals (4h) — refactor pe stilul Boilerplate.

## Strategie

V4 combinaă două strategii pe **3 perechi**, shared equity pool:

| Pereche | Strategie | Logică |
|---|---|---|
| **BTCUSDT** | `bb_mr` | Bollinger Bands Mean Reversion (cross-back + RSI extrem) |
| **TIAUSDT** | `hi` | Hull MA + Ichimoku Cloud (trend follow) |
| **NEARUSDT** | `hi` | Hull MA + Ichimoku Cloud (trend follow) |

## Backtest 2022-2026 ($100 init, 10% sizing)

- **PnL paper:** +$592K (PF 1.33, DD -53%, CAGR 616%/an)
- **Cycle istoric** $100→$5K: 31.7 luni (cycle 1) și 11.5 luni (cycle 2)
- **TIA = 86% din profit** — concentration risk, monitorizat
- **Reset profit MANUAL** la $5K (user marchează profitul + reset bot la $100)

Per pair PnL contribution full period:
- BTC BB MR: PF 2.62, +$124K
- TIA H+I: PF 1.53, +$510K (motor portfolio)
- NEAR H+I: PF 1.03, +$29K (marginal — monitor)

## Diferență vs Ichimoku1/2 template

- **Multi-strategy dispatch**: signal generator selectat per pereche (`pair_cfg.strategy`)
- BB MR signal generator nou la `strategies/bb_mr_signal.py`
- `LivePosition` extins cu `strategy` + `bars_held` (BB MR time-exit)
- SL/TP calculation branched per strategie:
  - HI: SL la `sl_initial_pct`, TP optional la `tp_pct` (signal exit dominant)
  - BB MR: SL la `sl_pct`, TP fix la entry × (1 ± sl_pct × tp_rr)
- Chart broadcast indicator-uri specifice per signal type (BB bands sau Hull/Ichimoku)
- `effective_sl_pct` property in PairConfig (sizing-corectness per strategy)

## Deploy pe VPS via Portainer

### 1. Pregătire VPS

Portainer + Docker instalate. Bot-ul folosește image-ul public `sdancri/v4_portfolio:latest` din DockerHub.

### 2. Stack in Portainer

În Portainer → **Stacks** → **Add stack** → **Web editor** și copiază [compose.V4.yml](compose.V4.yml). Setează environment variables (Portainer UI sau `.env` upload):

| Var | Valoare |
|---|---|
| `BYBIT_API_KEY` | API key subaccount V4 (read+trade, no withdraw) |
| `BYBIT_API_SECRET` | API secret |
| `BYBIT_TESTNET` | `0` (mainnet) sau `1` (testnet) |
| `TELEGRAM_TOKEN` | (opțional) bot token Telegram |
| `TELEGRAM_CHAT_ID` | (opțional) chat ID destinație |
| `RESET_TOKEN` | string arbitrar — schimbă pentru wipe state la restart |
| `CHART_TZ` | `Europe/Bucharest` (default) |

Click **Deploy the stack**. Portainer pull image automat din DockerHub.

### 3. Acces chart

`http://<vps-ip>:8104/` — chart Lightweight Charts cu candles, indicators, trades, equity curve.

### 4. Update bot

Image-ul e taggat `:latest` și marcat `pull_policy: always`. Re-deploy in Portainer pull versiunea nouă din DockerHub fără rebuild local.

### 5. Volumes persistente

`./logs/V4` și `./data/V4` mapate la host — păstrează istoricul trades + equity_curve între restart-uri.

## Quick deploy (CLI direct, fără Portainer)

```bash
docker pull sdancri/v4_portfolio:latest
docker compose -f compose.V4.yml up -d
```

## Config

`config/config_v4.yaml` — see file for inline tuning notes per pereche.

## Backtest local

```bash
python scripts/backtest_v4.py                    # 2022-prezent, single window
python scripts/backtest_v4_multi.py              # 5 windows în paralel (multiprocessing)
```

Date OHLCV 4h se citesc din `/home/dan/Python/Test_Python/data/ohlcv/<SYMBOL>_4h.parquet`.

## Reset profit cycling (manual)

La fiecare $5K profit:
1. Stop bot (Portainer)
2. Withdraw $4,900 din subaccount Bybit (păstrează $100)
3. Schimbă `RESET_TOKEN` în env (forțează wipe state la următor start)
4. Restart bot — equity sync din Bybit balance, istoric resetat
