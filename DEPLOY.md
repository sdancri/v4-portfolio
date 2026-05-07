# Deploy ICHIMOKU bot pe VPS via Portainer

Ghid pas-cu-pas pentru deploy production. Image: `sdancri/ichimoku:latest`.

---

## 1. Preconditii

| | |
|---|---|
| VPS | Linux (Debian/Ubuntu recomandat) cu Docker + Portainer instalate |
| Resurse minime | 1 vCPU, 1 GB RAM, 5 GB disk |
| Port | **8103** liber (chart UI) — sau alt port via `CHART_PORT` |
| Bybit | Subaccount cu API key (Trade ENABLED, Withdraw DISABLED, IP whitelist VPS) |
| Telegram (opt) | Bot token + chat ID (de la @BotFather si @userinfobot) |

### Setari Bybit obligatorii (UI Bybit, NU via API)
- **Margin mode**: Isolated pentru ambele MNT, DOT
- **Leverage**: ≥ **20×** pentru MNTUSDT, ≥ **7×** pentru DOTUSDT
  (bot-ul incearca `setLeverage` la pornire, dar daca UI restrictioneaza, trade-urile pot fi respinse)

---

## 2. Optiunea A — Stack din Git (recomandat)

Avantaj: redeploy-uri rapide via "Pull and redeploy".

1. Portainer → **Stacks** → **Add stack**
2. Build method: **Repository**
3. Repository URL: `https://github.com/sdancri/ichimoku-bot`
4. Compose path: `docker-compose.yml`
5. **Environment variables** — adauga:
   ```
   BYBIT_API_KEY=<your_key>
   BYBIT_API_SECRET=<your_secret>
   TRADING_MODE=testnet
   TELEGRAM_TOKEN=<optional>
   TELEGRAM_CHAT_ID=<optional>
   ```
6. **Deploy the stack**

---

## 3. Optiunea B — Stack din image DockerHub (web editor)

Avantaj: nu cere acces Git pe VPS.

1. Portainer → **Stacks** → **Add stack**
2. Build method: **Web editor**
3. Lipeste continutul din `docker-compose.yml` (din repo)
4. Environment variables — la fel ca optiunea A
5. Deploy

---

## 4. Verificare dupa deploy

### 4.1 Status container
Portainer → Containers → `ichimoku` → ar trebui sa fie **healthy** (verde) dupa ~45s
(start_period). `unhealthy` = verifica logs.

### 4.2 Logs (primele 30s)
Portainer → Container → **Logs** → cauta:
```
ICHIMOKU BOT — ichimoku_mnt_dot
mode: testnet (paper)   <— sau 'MAINNET — REAL MONEY'
[SETUP] MNTUSDT: leverage=20x
[SETUP] DOTUSDT: leverage=7x
[chart] http://0.0.0.0:8103/  (TZ: Europe/Bucharest)
```

### 4.3 Telegram (daca ai configurat)
La pornire trebuie sa primesti:
```
🤖 [ichimoku_mnt_dot] BOT STARTED ✅
Strategy: Hull+Ichimoku 4h
Portfolio: ichimoku_mnt_dot
Mode:      testnet (paper)
Pool init: $100.00
Pairs:     MNTUSDT (20x), DOTUSDT (7x)
Chart:     port 8103
```
Si dupa warmup:
```
🤖 [ichimoku_mnt_dot] STRATEGY READY ✅
```

### 4.4 Chart UI
Browser: `http://<vps-ip>:8103/`
- Apar candele primary pair (MNTUSDT 4h)
- Status bar: `Trades 0`, `Account $100.00`
- WebSocket connected (badge verde stanga-jos)

### 4.5 API status (pentru Docker healthcheck si monitoring)
```bash
curl http://<vps-ip>:8103/api/status
# {"healthy": true, "warnings": [], "candles_total": 1, ...}
```

---

## 5. Trecere la live (mainnet)

**DUPA 2-4 saptamani de testnet stabil:**
1. Verifica ca log-urile nu au erori repetate
2. Telegram messages curate (entry/exit conform asteptari)
3. Portainer → Stack → Editor → schimba `TRADING_MODE=live`
4. **Update the stack** (re-deploy cu settings noi)
5. Monitorizeaza primul trade live cu atentie marita

**Setup recomandat live:**
- Pool start: **$50** (1-2 saptamani) → daca OK, $100
- Telegram critic alerts pe HALT/CLOSE FAILED — NU ignora

---

## 6. Operational

### 6.1 Pause/Resume
```bash
# Pause toate perechile
curl -X POST http://<vps-ip>:8103/api/pause
# Pause doar MNT
curl -X POST "http://<vps-ip>:8103/api/pause?symbol=MNTUSDT"
# Resume toate
curl -X POST http://<vps-ip>:8103/api/resume
```
La pauza: bot nu mai deschide trade-uri NOI pe pereche, dar SL pe Bybit ramane activ. Pozitiile existente raman.

### 6.2 Update la o noua versiune
- **Optiunea A** (Git): Portainer → Stack → "Pull and redeploy"
- **Optiunea B** (DockerHub): Portainer → Stack → "Update the stack" cu re-pull image
- `pull_policy: always` in compose forteaza fetch ultima imagine

### 6.3 Logs persistent
Volume `./logs:/app/logs` salveaza JSONL events pe disk VPS (la `/var/lib/docker/volumes/<stack>_logs/_data/`). Verifica:
```bash
docker exec ichimoku tail -f /app/logs/ichimoku_mnt_dot.jsonl
```

### 6.4 Monitorizare trade-uri
Telegram = sursa principala. Pentru sumar:
```bash
curl http://<vps-ip>:8103/api/init | jq .summary
# {"n_trades": 5, "wins": 3, "win_rate": 60.0, "pnl_total": 12.34, ...}
```

---

## 7. Securitate

### 7.1 Chart UI expunere externa
Default = `0.0.0.0:8103` (accesibil de oriunde). Optiuni:

**A. Limita la VPS local + reverse proxy SSL** (recomandat)
- Modifica compose: `ports: ["127.0.0.1:8103:8103"]`
- Caddy/Nginx reverse proxy la `chart.your-domain.com` cu Basic Auth + SSL

**B. Firewall whitelist IP**
```bash
ufw allow from <your-home-ip> to any port 8103
ufw deny 8103
```

**C. Lasa public (chart e read-only)** — risc minim dar oricine cu URL vede tradeurile

### 7.2 API keys
- **NU** committa `.env` in Git
- Foloseste Portainer Environment variables (criptate la rest)
- Bybit API: enable doar **Read + Trade**, NU Withdraw
- IP whitelist Bybit doar pe VPS

---

## 8. Troubleshooting

| Simptom | Cauza probabila | Fix |
|---------|----------------|-----|
| Container `unhealthy` dupa start | Bybit API key invalid sau IP not whitelisted | Verifica logs: `[SETUP FAILED]` → fix in Bybit UI |
| `[RECONCILE] PAUSED — RESIDUAL` la pornire | Pozitie reziduala pe Bybit (din run anterior) | Inchide manual pe Bybit UI sau resume API |
| Telegram nu trimite | TELEGRAM_TOKEN/CHAT_ID greseaza | Test cu `curl https://api.telegram.org/bot$TOKEN/getMe` |
| Chart blank | WebSocket disconnect / timezone glitch | Hard reload browser (Ctrl+Shift+R), check `/api/status` |
| `[RATE] Throttled` frecvent in logs | Burst peste limita 5 req/s | Urca `RATE_LIMIT_PER_SEC=10` (max Bybit signed) |
| Trade-uri respinge cu "leverage" eroare | Leverage UI Bybit < setare bot | Bybit UI → MNT 20×, DOT 7× |

### Logs prefix-uri pentru grep
- `[WS]` kline public stream
- `[WS-PRIV]` private (orders/positions)
- `[ORDER]` schimbare status ordin
- `[OPEN]` / `[STATE]` trade lifecycle
- `[RATE]` rate limiter throttle (rar = OK; frecvent = urca limit)
- `[RECONCILE]` defense-in-depth state sync
- `[TG]` Telegram (errors)

---

## 9. Performanta asteptata (referinta backtest 2022-2026)

| Scenariu fee | Final $ | Return | DD | PF |
|--------------|--------:|-------:|----:|----:|
| Baseline (taker/taker) | $23,874 | +23,774% | -49.5% | 1.23 |
| Realist (70/30 maker entry mix) | $32,619 | +32,519% | -48.6% | 1.25 |
| Optimist (100% maker entry) | $37,283 | +37,184% | -48.2% | 1.25 |

Live va fi **intre realist si optimist** depinde de fill-rate al maker
postonly la entry. Astepta **DD-50% inevitabil** pe seriile losing — strategia
e Aggressive prin design.

---

## 10. Resurse aditionale

- **Repo**: https://github.com/sdancri/ichimoku-bot
- **DockerHub**: https://hub.docker.com/r/sdancri/ichimoku
- **Strategy spec**: [STRATEGY_SETTINGS.md](STRATEGY_SETTINGS.md)
- **Boilerplate referinta**: https://github.com/sdancri/trading-bot-boilerplate
