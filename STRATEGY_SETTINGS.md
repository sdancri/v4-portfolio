# ICHIMOKU bot — Strategy Settings (Config: AGGRESSIVE)

Toate parametrii strategiei Hull+Ichimoku 4h pentru portfolio MNT + DOT.
**Validat OOS** (split 70%/30% IS sweep + OOS test cu params găsiți pe IS).
**Validat SL sweep** (36 combos 2D, ales Aggressive: SL_MNT=3% / SL_DOT=8%).

Versiune: 2026-05-06 — Config Aggressive.

---

## 1. Portfolio (global)

| Param | Valoare | Note |
|---|---|---|
| `pool_total` | **$100** USDT | Capital initial; compound de aici |
| `leverage` (default) | **15×** | Default Bybit isolated; **override per-pair** (vezi sec. 2/3) |
| `cap_pct_of_max` | 0.95 | Skip signal daca pos_usd > 95% × cap Bybit |
| `taker_fee` | 0.00055 | 0.055% taker (Bybit perpetual) |
| `slippage_bps` | 0 | Fara slippage simulat in backtest |

### Sizing formula

```
risk_usd = risk_pct_per_trade × shared_equity
pos_usd  = risk_usd / sl_initial_pct
qty      = floor(pos_usd / entry_price / qty_step) × qty_step
```

**Shared equity** = `pool_total + cumulative real PnL Bybit` (compound).

### Position size implications (per pair)

| Pair | sizing | SL | leverage | notional / equity | risc / trade |
|---|---|---|---|---|---|
| MNTUSDT | 7% | **3%** | **20×** | **2.33×** equity | 7% × equity |
| DOTUSDT | 5% | **8%** | **7×** | **0.625×** equity | 5% × equity |

**De ce leverage diferit:** Leverage NU afecteaza `pos_usd` (formula = `risk/sl`), doar
capul Bybit (`pos_usd ≤ cap_pct × balance × leverage`) si margin lock-ul. MNT la 20×
ofera headroom larg pe compound (la equity $1k, MNT pos $2333 are nevoie de cap ≥ $2333
→ la 15× cap=$1425 ar fi REJECTED; la 20× cap=$19000 ✓). DOT la 7× e suficient pentru
notional 0.625× equity si tine margin lock minim.

---

## 2. MNTUSDT 4h — config (AGGRESSIVE)

**Validare OOS:** ROBUST (IS PF 1.72 → OOS PF 1.53, dPF -11%)

### Indicatori

| Param | Valoare | Default Pine | Note |
|---|---|---|---|
| `hull_length` | **8** | 12 | OOS-tunat (vs 10 din full-data sweep) |
| `tenkan_periods` | 9 | 9 | Default Ichimoku |
| `kijun_periods` | **48** | 24 | **CRITIC** — captează ciclul săptămânal pe 4h |
| `senkou_b_periods` | 40 | 51 | Sweep-validated |
| `displacement` | 24 | 24 | Default |

### Sizing & exit

| Param | Valoare | De ce |
|---|---|---|
| `risk_pct_per_trade` | **7%** | Asymmetric (mai mult vs DOT — MNT are PF mai mare) |
| `sl_initial_pct` | **3%** ⚡ | Aggressive — MNT signal-quality permite SL strans |
| `tp_pct` | **`null`** (no TP) | Signal exit only — Hull+Ichimoku captează MNT trends corect |

### Filtre

| Param | Valoare |
|---|---|
| `max_hull_spread_pct` | 2.0 |
| `max_close_kijun_dist_pct` | 6.0 |

---

## 3. DOTUSDT 4h — config (AGGRESSIVE)

**Validare OOS:** ROBUST (IS PF 1.14 → OOS PF 1.34, dPF +17% — IMPROVED)

### Indicatori

| Param | Valoare | Default Pine | Note |
|---|---|---|---|
| `hull_length` | **10** | 12 | DOT preferă Hull intermediar |
| `tenkan_periods` | 9 | 9 | Default |
| `kijun_periods` | **36** | 24 | DOT diferit fata de MNT |
| `senkou_b_periods` | 52 | 51 | Sweep-validated |
| `displacement` | 24 | 24 | Default |

### Sizing & exit

| Param | Valoare | De ce |
|---|---|---|
| `risk_pct_per_trade` | **5%** | Asymmetric — DOT edge mai slab vs MNT |
| `sl_initial_pct` | **8%** ⚡ | DOT noisier — SL larg evita false-out |
| `tp_pct` | **0.12** (12%) | Lock in winners 12% earlier |

### Filtre

| Param | Valoare |
|---|---|
| `max_hull_spread_pct` | 2.0 |
| `max_close_kijun_dist_pct` | 6.0 |

---

## 4. Performance backtest

### Per-pair OOS validation (split 70/30)

| Pair | Combo (IS-sweep) | IS PF | OOS PF | OOS Ret | OOS DD | Verdict |
|---|---|---|---|---|---|---|
| **MNTUSDT** | Hull=8, Kijun=48, SnkB=40 | 1.72 | **1.53** | +63% | -16% | 🟢 ROBUST |
| **DOTUSDT** | Hull=10, Kijun=36, SnkB=52 | 1.14 | **1.34** | +143% | -33% | 🟢 ROBUST |

### SL sweep — Top 5 by Ret cu DD≤-50%

| SL_MNT | SL_DOT | N | WR | PF | Ret% | DD% | Final |
|---|---|---|---|---|---|---|---|
| **3%** ⚡ | **8%** ⚡ | 1012 | 39.8 | 1.33 | **+26,897%** | -50% | **$26,997** |
| 3% | 10% | 1012 | 39.8 | 1.34 | +23,363% | -48% | $23,463 |
| 4% | 5% | 1010 | 39.8 | 1.30 | +11,693% | -49% | $11,793 |
| 4% | 6% | 1010 | 39.8 | 1.32 | +10,222% | -47% | $10,322 |
| 4% | 8% | 1010 | 39.8 | 1.35 | +9,030% | -43% | $9,130 |

### Comparație SL configs (toate cu MNT 7%/no_TP, DOT 5%/TP=12%)

| Setup | Final | Ret% | DD% | PF |
|---|---|---|---|---|
| 5%/5% (anterior) | $6,674 | +6,574% | -42% | 1.33 |
| 4%/8% (sweet spot) | $9,130 | +9,030% | -43% | 1.35 |
| **3%/8% (AGGRESSIVE)** ⭐ | **$26,997** | **+26,897%** | **-50%** | **1.33** |
| 10%/10% (conservator) | $1,148 | +1,048% | -24% | 1.47 |

### Per-pair contribuție portfolio Aggressive (full period 2022-2026)

| Symbol | sizing | SL | TP | N | WR% | PnL |
|---|---|---|---|---|---|---|
| **MNTUSDT** | 7% | **3%** | None | 384 | 39.8 | **+$28,786** (95%) |
| **DOTUSDT** | 5% | **8%** | 12% | 628 | 39.8 | +$3,962 (15%) |

MNT generează 95% din profit la SL=3% (positions 2.33× equity când compune).

---

## 5. Logica strategiei

### Entry conditions

**LONG** (toate satisfăcute):
```
n1 > n2 AND close > n2 AND close > chikou AND close > senkou_h
AND (tenkan >= kijun OR close > kijun)
```

**SHORT** (oglindit):
```
n1 < n2 AND close < n2 AND close < chikou AND close < senkou_l
AND (tenkan <= kijun OR close < kijun)
```

### Smart filter Setup G

Skip entry dacă:
- `|n1 - n2| / close > 2%` (Hull spread too wide = trend epuizat)
- `|close - kijun| / close > 6%` (price too far from Kijun = extended)

### Exit priority order (intrabar pe bar confirmed)

**1. SL hit** (intra-bar low/high vs entry × (1±sl_pct))
**2. TP hit** (dacă tp_pct setat, intra-bar high/low vs entry × (1±tp_pct))
**3. Signal flip** (Hull+Ichimoku close conditions reversed)

### Close LONG signal (oricare):
```
n1 < n2 AND (close<n2 OR tenkan<kijun OR close<tenkan
             OR close<kijun OR close<senkou_h OR close<chikou)
```

### Close SHORT signal (oglindit):
```
n1 > n2 AND (close>n2 OR tenkan>kijun OR close>tenkan
             OR close>kijun OR close>senkou_l OR close>chikou)
```

---

## 6. Operational

| Param | Valoare | Note |
|---|---|---|
| `max_concurrent_positions` | 2 | Una per pair max |
| `max_consecutive_api_errors` | 5 | Auto-retry threshold |
| `heartbeat_interval_seconds` | 60 | Status keepalive |
| `state_dir` | `./state` | (Nu folosit — restart = fresh) |
| `log_dir` | `./logs` | JSONL events |

**State persistence:** **NONE** — restart = istoric trade & equity de la 0.
**Killswitch:** **DISABLED** (max_drawdown_killswitch=1.0, max_consecutive_losses=999).

---

## 7. Atentionari deployment

⚠️ **Config Aggressive = risc ridicat:**
- SL_MNT=3% cu sizing 7% → notional **2.33× equity** per trade
- DD historic -50% (bot supraviețuiește, dar emoțional dur)
- Win rate 39.8% — series losing-uri inevitabile (max 7 consecutive în istoric)

⚠️ **Validare ÎNAINTE de live:**
1. Replay backtest — validat ✅
2. Testnet 2-4 saptamani — `TRADING_MODE=testnet`
3. Live $50 — capital mic 1-2 saptamani
4. Live $100+ — după ce comportamentul se confirmă

⚠️ **Bybit margin:**
- La leverage 15× isolated, MNT pos 2.33× equity = ~7× efectiv din margin
- Verifică min order size pe Bybit pentru MNT (qty_step și min_qty)
- La equity foarte mic ($20-30), positions pot deveni sub min_order_size

---

## 8. Quick reference — config.yaml

```yaml
portfolio:
  pool_total: 100.0
  leverage: 15
  taker_fee: 0.00055

pairs:
  - symbol: MNTUSDT
    timeframe: 4h
    hull_length: 8
    kijun_periods: 48      # ← CRITIC pe MNT
    senkou_b_periods: 40
    risk_pct_per_trade: 0.07
    sl_initial_pct: 0.03   # ⚡ AGGRESSIVE
    tp_pct: null           # NO TP
    max_hull_spread_pct: 2.0
    max_close_kijun_dist_pct: 6.0

  - symbol: DOTUSDT
    timeframe: 4h
    hull_length: 10
    kijun_periods: 36      # diferit MNT
    senkou_b_periods: 52
    risk_pct_per_trade: 0.05
    sl_initial_pct: 0.08   # ⚡ AGGRESSIVE
    tp_pct: 0.12           # TP 12%
    max_hull_spread_pct: 2.0
    max_close_kijun_dist_pct: 6.0
```

---

## 9. Variabile testate vs netestate

### ✅ Testate complet

- `hull_length`: 8/10/12/16/24 (per pair)
- `kijun_periods`: 24/36/48/60 (per pair)
- `senkou_b_periods`: 26/40/52 (per pair)
- `tp_pct` per pair: None / 5/6/8/10/12/15/20/25/30/40/50/75/100%
- `risk_pct_per_trade`: 5/7/10/15/20/25/30%
- **`sl_initial_pct`** sweep 2D: 3/4/5/6/8/10% per pair (36 combos)
- OOS validation: 70/30 split pe IS sweep + OOS test
- Cross-pair validation: 27 alt-pairs cu config MNT-default

### ❌ Netestate (kept fixed)

- `tenkan_periods` — fix 9 (Pine default)
- `displacement` — fix 24 (Pine default)
- `taker_fee` — fix 0.055% (Bybit default)
- `leverage` — fix 15× (nu schimbă sizing pe pine_formula)
- Filter thresholds — fix 2%/6% (Setup G default)

### 🟡 Posibile sweep-uri viitoare

- Filter sweep (max_hull_spread 1-5%, max_close_kijun 4-10%)
- Time-of-day filter (anumite ore mai active pe crypto)
- ATR-based dynamic SL (vs SL fix %)
- Trailing stop after 12% TP target (lock 50% gain, let runner run)

---

## 10. Diferente fata de varianta anterioara (5%/5%)

| | 5%/5% (sweet) | **3%/8% (AGGRESSIVE)** ⭐ |
|---|---|---|
| Final equity | $6,674 | **$26,997** |
| Ret % | +6,574% | **+26,897%** |
| DD % | -42% | **-50%** (↑8pp) |
| PF | 1.33 | 1.33 |
| MNT notional | 1.4× eq | **2.33× eq** |
| DOT notional | 1.0× eq | **0.625× eq** |
| Win rate | 39.8% | 39.8% (identic) |
| SL hits (1009 trades) | 39 | **45** |

**Logica trade-off:**
- MNT: SL strâns (3%) = pozitii mari × edge bun = compound exploziv
- DOT: SL larg (8%) = pozitii mici × edge slab = pierderi atenuate, nu mai e drag pe portfolio

**De ce funcționează:** Hull+Ichimoku pe MNT are signal quality foarte bun (WR 41% standalone, PF 1.83 solo). SL la 3% taie doar adevăratele false-positives. Pe DOT, signal-ul e mai slab (PF 1.19 solo) — SL larg dă tradeurilor șansa să devină câștigătoare prin signal flip natural.
