# VSE Strategy Logic — Implementation Spec (COMPLET)

**Pentru:** Claude Code  
**Versiune:** v2.0 (corrected — include OPP exits, entry timing, reset logic exact)  
**Validation reference:** `trades_setup_target5k.csv` (457 trades, $15,400 PnL)

> ⚠️ **VSE = "Vortex Sniper Elite @DaviddTech"** — strategie fidelă Pine Script v6.  
> ⚠️ **Există 6 indicatori + EXIT DUAL (SuperTrend SAU Opposite Signal).**

---

## 1. Overview

Strategie **VSE** cu **reset cycles @ $5,000** pe perpetuals Bybit, **multi-subaccount**.

### Componente strategie:
1. **McGinley Dynamic** (period 14) — baseline trend
2. **White Line** (period 20) — secondary trend
3. **TTM Squeeze** (period 20, BB 2σ vs Keltner ×2.0) — volatility
4. **Tether Line** (fast 13, slow 55) — momentum
5. **Vortex Indicator** (period 14, threshold 0.05) — directional
6. **SuperTrend** (ATR 22, mult 3.0, wicks=True) — TRAILING STOP

### Style: **Balanced** (DEFAULT validat)

### Flow per subaccount:

```
1. Pe fiecare bară ÎNCHISĂ:
   a. Compute indicators (toate 6)
   b. Build signals (raw_long / raw_short)
   c. Verifică ENTRY: validate cooldown + SL bounds
   d. Verifică EXIT pentru pozițiile open: SL trailing SAU opposite signal
2. Entry executat la OPEN-ul barei URMĂTOARE (NOT close-ul curent!)
3. Trailing stop SuperTrend: update DOAR în direcția favorabilă (max pe long, min pe short)
4. Exit pe opposite signal: NEXT BAR OPEN (NU close-ul barei cu semnalul)
5. Pe close: update equity, balance, check cycle conditions
6. Cycle SUCCESS @ $5k → close ALL positions, withdraw, restart
7. Reset trigger @ equity<$15 → consume rezerva, restart equity (NO KILL)
```

---

## 2. CRITICAL: Entry/Exit Execution Timing

```
TIMING-UL E ESENȚIAL pentru match cu backtest!

ENTRY:
  Bară i închide la T → semnal evaluat pe bara i (closing values)
  ENTRY EXECUTAT la OPEN-ul barei i+1 (next bar)
  entry_price = opens[i+1]   # NU closes[i]!

EXIT pe Opposite Signal:
  Bară j închide la T → opposite signal raw_short[j] (pentru long open)
  EXIT EXECUTAT la OPEN-ul barei j+1
  exit_price = opens[j+1]
  exit_reason = "OPP"

EXIT pe SuperTrend:
  În interiorul barei j, prețul atinge local_stop
  EXIT IMEDIAT la local_stop (intra-bar)
  exit_reason = "TS"

Cooldown:
  bars_since_exit ≥ entry_filter_bars (3 bars) PER PERECHE
```

---

## 3. Cod COMPLET al Indicatorilor

### 3.1 Imports și Config

```python
from dataclasses import dataclass
from typing import Literal
import numpy as np
import pandas as pd
import pandas_ta as ta
from numba import njit


@dataclass
class VSEConfig:
    # ============ CAPITAL (Nou1) ============
    initial_capital: float = 100.0     # pool total per subaccount
    risk_pct: float = 0.20             # 20% × equity per trade
    taker_fee: float = 0.00055         # 0.055%
    leverage_max: int = 20             # 20× pe Bybit perpetuals
    cap_pct_of_max: float = 0.95       # safety buffer 95%
    
    # ============ STYLE ============
    style: Literal["Strict", "Balanced", "Scalper"] = "Balanced"
    
    # ============ INDICATORS ============
    # 1. Baseline McGinley
    mcginley_length: int = 14
    
    # 2. White Line
    whiteline_length: int = 20
    
    # 3. TTM Squeeze
    ttms_length: int = 20
    ttms_bb_mult: float = 2.0
    ttms_kc_mult_widest: float = 2.0
    ttms_green_red: bool = True        # doar momentum pozitiv + rising
    ttms_highlight: bool = True        # doar când no_squeeze
    ttms_cross: bool = True            # edge-trigger pe semnal
    
    # 4. Tether Line
    tether_fast: int = 13
    tether_slow: int = 55
    
    # 5. Vortex
    vortex_length: int = 14
    vortex_threshold: float = 0.05
    
    # 6. SuperTrend (pentru SL/trailing)
    st_atr_length: int = 22
    st_atr_mult: float = 3.0
    st_wicks: bool = True              # folosește high/low (NOT close)
    
    # ============ TRADE FILTERS ============
    entry_filter_bars: int = 3         # cooldown post-exit PER PERECHE
    sl_min_pct: float = 0.005          # 0.5% minim SL distance
    sl_max_pct: float = 0.035          # 3.5% maxim SL distance
    
    # ============ EXIT ============
    use_opposite_signal_exit: bool = True   # ⚠️ CRITICAL — NU dezactiva!
    use_supertrend_trailing: bool = True
```

### 3.2 McGinley Dynamic (cu Numba)

```python
@njit(cache=True)
def _mcginley(src, ema_init, length, n):
    """
    Recursive: mg[i] = mg[i-1] + (src[i] - mg[i-1]) / (length * (src[i]/mg[i-1])^4)
    """
    mg = np.zeros(n)
    mg[0] = ema_init[0]
    for i in range(1, n):
        if np.isnan(mg[i-1]) or mg[i-1] == 0:
            mg[i] = ema_init[i]
            continue
        ratio = src[i] / mg[i-1]
        denom = length * (ratio ** 4)
        if denom == 0 or not np.isfinite(denom):
            mg[i] = mg[i-1]
            continue
        mg[i] = mg[i-1] + (src[i] - mg[i-1]) / denom
    return mg


def mcginley_dynamic(src: pd.Series, length: int) -> pd.Series:
    ema_seed = ta.ema(src, length=length).bfill().values
    mg = _mcginley(src.values, ema_seed, length, len(src))
    return pd.Series(mg, index=src.index)
```

### 3.3 SuperTrend (cu Numba)

```python
@njit(cache=True)
def _st_loop(hl2, hp, lp, sdv, n):
    """SuperTrend cu wicks. Returns: long_stop, short_stop, direction."""
    ls = np.full(n, np.nan)
    ss = np.full(n, np.nan)
    d = np.ones(n, dtype=np.int8)
    cur = 1
    for i in range(n):
        if np.isnan(sdv[i]):
            d[i] = cur
            continue
        cl = hl2[i] - sdv[i]
        cs = hl2[i] + sdv[i]
        pls = ls[i-1] if i > 0 and not np.isnan(ls[i-1]) else cl
        pss = ss[i-1] if i > 0 and not np.isnan(ss[i-1]) else cs
        plo = lp[i-1] if i > 0 else lp[i]
        phi = hp[i-1] if i > 0 else hp[i]
        ls[i] = (cl if cl > pls else pls) if plo > pls else cl
        ss[i] = (cs if cs < pss else pss) if phi < pss else cs
        if cur == -1 and hp[i] > pss:
            cur = 1
        elif cur == 1 and lp[i] < pls:
            cur = -1
        d[i] = cur
    return ls, ss, d
```

### 3.4 Compute ALL 6 Indicators

```python
def compute_indicators(df: pd.DataFrame, cfg: VSEConfig) -> pd.DataFrame:
    """
    Input: df cu [open, high, low, close, volume]
    Output: df cu indicatori + raw_long/raw_short signals
    """
    out = df.copy()
    src = out["close"]
    
    # 1. BASELINE: McGinley Dynamic
    out["baseline"] = mcginley_dynamic(src, cfg.mcginley_length)
    out["baseline_trend"] = np.where(
        out["close"] > out["baseline"], 1,
        np.where(out["close"] < out["baseline"], -1, 0)
    )
    
    # 2. WHITE LINE
    wl = cfg.whiteline_length
    out["white_line"] = (out["high"].rolling(wl).max() + out["low"].rolling(wl).min()) / 2
    out["white_trend"] = np.where(
        out["close"] > out["white_line"], 1,
        np.where(out["close"] < out["white_line"], -1, 0)
    )
    
    # 3. TTM SQUEEZE
    ttl = cfg.ttms_length
    basis = ta.sma(src, length=ttl)
    dev = src.rolling(ttl).std(ddof=0)
    bb_upper = basis + cfg.ttms_bb_mult * dev
    bb_lower = basis - cfg.ttms_bb_mult * dev
    
    # Keltner cu TR (NU ATR — fidel Pine)
    tr = ta.true_range(out["high"], out["low"], out["close"])
    kc_dev = ta.sma(tr, length=ttl)
    kc_upper_widest = basis + kc_dev * cfg.ttms_kc_mult_widest
    kc_lower_widest = basis - kc_dev * cfg.ttms_kc_mult_widest
    
    # no_squeeze = volatility expansion
    out["no_squeeze"] = (bb_lower < kc_lower_widest) | (bb_upper > kc_upper_widest)
    
    # Momentum (linreg)
    hh = out["high"].rolling(ttl).max()
    ll = out["low"].rolling(ttl).min()
    price_avg = ((hh + ll) / 2 + basis) / 2
    diff = src - price_avg
    out["ttms_momentum"] = ta.linreg(diff, length=ttl, offset=0)
    
    mom = out["ttms_momentum"]
    mom_prev = mom.shift(1)
    signals = np.where((mom > 0) & (mom > mom_prev), 1,
               np.where((mom > 0) & (mom <= mom_prev), 2,
               np.where((mom < 0) & (mom < mom_prev), -1,
               np.where((mom < 0) & (mom >= mom_prev), -2, 0))))
    out["ttms_signal"] = signals
    
    if cfg.ttms_green_red:
        out["ttms_basic_long"] = signals == 1
        out["ttms_basic_short"] = signals == -1
    else:
        out["ttms_basic_long"] = signals > 0
        out["ttms_basic_short"] = signals < 0
    
    if cfg.ttms_highlight:
        out["ttms_long_signal"] = out["ttms_basic_long"] & out["no_squeeze"]
        out["ttms_short_signal"] = out["ttms_basic_short"] & out["no_squeeze"]
    else:
        out["ttms_long_signal"] = out["ttms_basic_long"]
        out["ttms_short_signal"] = out["ttms_basic_short"]
    
    # Edge-trigger (signal doar la cross)
    if cfg.ttms_cross:
        ls_ = out["ttms_long_signal"].fillna(False)
        ss_ = out["ttms_short_signal"].fillna(False)
        out["ttms_long_final"] = ls_ & ~ls_.shift(1, fill_value=False)
        out["ttms_short_final"] = ss_ & ~ss_.shift(1, fill_value=False)
    else:
        out["ttms_long_final"] = out["ttms_long_signal"]
        out["ttms_short_final"] = out["ttms_short_signal"]
    
    # 4. TETHER LINE
    tf = cfg.tether_fast
    tss = cfg.tether_slow
    out["tether_fast"] = (out["high"].rolling(tf).max() + out["low"].rolling(tf).min()) / 2
    out["tether_slow"] = (out["high"].rolling(tss).max() + out["low"].rolling(tss).min()) / 2
    out["tether_long"] = (out["tether_fast"] > out["tether_slow"]) & (out["close"] > out["tether_slow"])
    out["tether_short"] = (out["tether_fast"] < out["tether_slow"]) & (out["close"] < out["tether_slow"])
    
    # 5. VORTEX
    vl = cfg.vortex_length
    vm_plus = (out["high"] - out["low"].shift(1)).abs()
    vm_minus = (out["low"] - out["high"].shift(1)).abs()
    vplus = vm_plus.rolling(vl).sum()
    vminus = vm_minus.rolling(vl).sum()
    sum_tr = tr.rolling(vl).sum()
    out["vi_plus"] = vplus / sum_tr
    out["vi_minus"] = vminus / sum_tr
    out["vortex_long"] = (out["vi_plus"] - out["vi_minus"]) > cfg.vortex_threshold
    out["vortex_short"] = (out["vi_minus"] - out["vi_plus"]) > cfg.vortex_threshold
    
    # 6. SUPERTREND
    hl2 = (out["high"] + out["low"]) / 2
    atr_st = ta.atr(out["high"], out["low"], out["close"], length=cfg.st_atr_length)
    out["atr_st"] = atr_st
    sd = cfg.st_atr_mult * atr_st
    hp = out["high"] if cfg.st_wicks else out["close"]
    lp = out["low"] if cfg.st_wicks else out["close"]
    ls_a, ss_a, d_a = _st_loop(hl2.values, hp.values, lp.values, sd.values, len(out))
    out["long_stop"] = ls_a
    out["short_stop"] = ss_a
    out["stop_dir"] = d_a
    
    return out
```

### 3.5 Build Signals (Balanced style — DEFAULT)

```python
def build_signals(df: pd.DataFrame, cfg: VSEConfig) -> pd.DataFrame:
    """
    Combină indicatorii conform style.
    Output: raw_long / raw_short (bool columns)
    """
    out = df.copy()
    
    bl_up = out["baseline_trend"] == 1
    bl_dn = out["baseline_trend"] == -1
    wl_up = out["white_trend"] == 1
    wl_dn = out["white_trend"] == -1
    t_l = out["ttms_long_final"].fillna(False)
    t_s = out["ttms_short_final"].fillna(False)
    th_l = out["tether_long"].fillna(False)
    th_s = out["tether_short"].fillna(False)
    v_l = out["vortex_long"].fillna(False)
    v_s = out["vortex_short"].fillna(False)
    
    if cfg.style == "Strict":
        rl = bl_up & wl_up & t_l & th_l & v_l
        rs = bl_dn & wl_dn & t_s & th_s & v_s
    elif cfg.style == "Scalper":
        rl = bl_up & (t_l | th_l) & v_l
        rs = bl_dn & (t_s | th_s) & v_s
    else:  # Balanced (DEFAULT pentru setup-ul tău!)
        count_l = t_l.astype(int) + th_l.astype(int) + v_l.astype(int)
        count_s = t_s.astype(int) + th_s.astype(int) + v_s.astype(int)
        cond_l = bl_up & (count_l >= 2) & t_l
        cond_s = bl_dn & (count_s >= 2) & t_s
        rl = cond_l
        rs = cond_s
    
    out["raw_long"] = rl.fillna(False)
    out["raw_short"] = rs.fillna(False)
    return out
```

---

## 4. Live Adaptation — Generare semnal

```python
class VSESignalLive:
    """
    Live wrapper:
      - Rolling buffer de bare ÎNCHISE (NU current forming bar!)
      - Pe fiecare bară nouă închisă: recompute, check signal
      - Tracks bars_since_last_exit per pereche (cooldown)
    """
    
    def __init__(self, cfg: VSEConfig, lookback_bars: int = 200):
        self.cfg = cfg
        self.lookback = lookback_bars
        self.df_buffer = pd.DataFrame()
        self.last_signal_ts = None
        self.bars_since_last_exit = 10**6   # init high (no recent exit)
    
    def on_position_closed(self):
        """Apelat când o poziție se închide → resetează cooldown."""
        self.bars_since_last_exit = 0
    
    def on_new_bar_closed(self, new_bar: dict) -> dict | None:
        """
        new_bar: {'ts': datetime, 'open': float, 'high': float, 
                  'low': float, 'close': float, 'volume': float}
        APELAT DOAR pe bare ÎNCHISE (NOT current forming bar!)
        Returns: signal dict sau None
        """
        # 1. Append la buffer
        new_row = pd.DataFrame([new_bar]).set_index('ts')
        self.df_buffer = pd.concat([self.df_buffer, new_row]).tail(self.lookback)
        
        # 2. Increment cooldown counter
        self.bars_since_last_exit += 1
        
        # 3. Need minimum bars
        if len(self.df_buffer) < 100:
            return None
        
        # 4. Compute indicators + signals
        df_ind = compute_indicators(self.df_buffer, self.cfg)
        df_sig = build_signals(df_ind, self.cfg)
        
        # 5. Last bar (signal pe închidere)
        last = df_sig.iloc[-1]
        
        # 6. Validate signal
        if not (last['raw_long'] or last['raw_short']):
            return None
        
        # 7. Cooldown check (entry_filter_bars=3)
        if self.bars_since_last_exit < self.cfg.entry_filter_bars:
            return None
        
        # 8. Duplicate check
        if self.last_signal_ts == last.name:
            return None
        
        # 9. SuperTrend stop available?
        side = 'long' if last['raw_long'] else 'short'
        sl_price = last['long_stop'] if side == 'long' else last['short_stop']
        if pd.isna(sl_price):
            return None
        
        # 10. ⚠️ ENTRY PRICE = NEXT BAR OPEN (NU close-ul curent!)
        # În LIVE: signal e detectat la bara închisă, dar entry-ul e la PRIMUL TICK al barei următoare.
        # Bot va plasa MARKET ORDER care execută la prețul curent (~next bar open).
        # Pentru calcul SL, folosim entry_price ESTIMAT = closes[i] (close-ul barei cu signal),
        # apoi RECALCULĂM sl_pct după execuție efectivă.
        
        estimated_entry_price = last['close']   # estimat; real = market price la entry
        sl_dist = abs(estimated_entry_price - sl_price)
        if sl_dist <= 0:
            return None
        sl_pct = sl_dist / estimated_entry_price
        
        # 11. Validate SL bounds
        if sl_pct < self.cfg.sl_min_pct or sl_pct > self.cfg.sl_max_pct:
            return None
        
        self.last_signal_ts = last.name
        
        return {
            'side': side,
            'estimated_entry_price': estimated_entry_price,
            'sl_price': sl_price,
            'sl_pct': sl_pct,
            'signal_ts': last.name,
            'execute_at': 'NEXT_TICK_MARKET',   # market order ASAP
        }
```

---

## 5. Position Sizing

```python
def compute_position_size(equity: float, sl_pct: float, balance_broker: float, 
                           cfg: VSEConfig) -> dict:
    """
    Returns: {risk_usd, pos_usd, was_capped}
    
    NOTĂ: cap_usd folosește balance_broker (NOT equity) pentru margin disponibil real.
    """
    risk_usd = cfg.risk_pct * equity                    # 20% × $50 = $10
    pos_raw = risk_usd / sl_pct                         # ex: $10 / 0.02 = $500
    cap_usd = cfg.cap_pct_of_max * balance_broker * cfg.leverage_max
    
    was_capped = pos_raw > cap_usd
    pos_usd = cap_usd if was_capped else pos_raw
    
    return {
        'risk_usd': risk_usd,
        'pos_usd': pos_usd,
        'was_capped': was_capped,
    }


def compute_qty(pos_usd: float, entry_price_actual: float, step_size: float) -> float:
    """Round qty down to step size (Bybit instrument precision)."""
    qty_raw = pos_usd / entry_price_actual
    return (qty_raw // step_size) * step_size
```

---

## 6. EXIT LOGIC — DUAL: SuperTrend + Opposite Signal ⚠️ CRITICAL

În backtest tău, exit se face la **PRIMUL** din:
- (a) **SuperTrend stop hit** (intra-bar) → exit IMEDIAT la stop_price (TS reason)
- (b) **Opposite VSE signal** detectat → exit la **NEXT BAR OPEN** (OPP reason)

**În CSV:** 16/457 trade-uri (3.5%) sunt OPP exits, dar contribuie cu **$3,269 PnL** (21% din wealth)!

### 6.1 Per-bar exit check loop (apelat după bar close)

```python
async def check_exit_conditions(subacc, position, df_ind):
    """
    Apelat pe FIECARE bară închisă pentru pozițiile open.
    
    Logică:
      1. Update trailing stop (SuperTrend)
      2. Check opposite signal → EXIT planificat la next bar open
      
    SL hit-ul efectiv (TS) e gestionat de Bybit prin stop_market order.
    """
    last = df_ind.iloc[-1]
    
    # ===== A) Update trailing stop (SuperTrend) =====
    if position['side'] == 'long':
        new_stop = last['long_stop']
        if not pd.isna(new_stop) and new_stop > position['sl_price']:
            await modify_stop_loss(subacc, position, new_stop)
            position['sl_price'] = new_stop
    else:  # short
        new_stop = last['short_stop']
        if not pd.isna(new_stop) and new_stop < position['sl_price']:
            await modify_stop_loss(subacc, position, new_stop)
            position['sl_price'] = new_stop
    
    # ===== B) Check opposite signal → planează exit la next bar open =====
    opposite_signal_active = False
    if position['side'] == 'long' and last['raw_short']:
        opposite_signal_active = True
    elif position['side'] == 'short' and last['raw_long']:
        opposite_signal_active = True
    
    if opposite_signal_active:
        # Exit la următoarea oră (next bar open, market order)
        position['exit_planned'] = True
        position['exit_reason_planned'] = 'OPP'
        log_info(f"Opposite signal pe {position['symbol']} — exit planificat next bar open")


async def execute_planned_exits(subacc, positions, current_bar_open_ts):
    """
    Apelat la deschiderea fiecărei bare noi.
    Execută exits planificate (OPP).
    """
    for pos in positions:
        if pos.get('exit_planned'):
            await close_position(subacc, pos, reason=pos['exit_reason_planned'])
            pos['exit_planned'] = False
```

### 6.2 SL hit (TS) handling

```
TS exits sunt gestionate de Bybit AUTOMAT prin stop_market order:
  Pe entry, plasăm 2 ordere:
    1. Market order entry (long/short)
    2. stop_market order cu reduce_only=True la sl_price
  
  Când prețul atinge sl_price, Bybit execută automat closure.
  Bot detectează prin polling /position sau WebSocket position update:
    if position.qty == 0:
        # SL hit, fetch trade history pentru fee + exit_price
        trade_record = await fetch_last_closed_trade(symbol)
        on_trade_closed(state, pnl_net=trade_record['pnl_net'])
```

### 6.3 Trailing stop modify

```python
async def modify_stop_loss(subacc, position, new_stop_price):
    """Modify SL order pe Bybit."""
    try:
        await subacc.edit_order(
            id=position['order_sl_id'],
            symbol=position['symbol'],
            type='stop_market',
            side='sell' if position['side']=='long' else 'buy',
            amount=position['qty'],
            params={'stopPrice': new_stop_price, 'reduceOnly': True}
        )
    except Exception as e:
        # Bybit poate respinge dacă noul stop e prea aproape de market
        # Fallback: cancel + replace
        await subacc.cancel_order(position['order_sl_id'], position['symbol'])
        new_order = await subacc.create_order(
            symbol=position['symbol'],
            type='stop_market',
            side='sell' if position['side']=='long' else 'buy',
            amount=position['qty'],
            params={'stopPrice': new_stop_price, 'reduceOnly': True}
        )
        position['order_sl_id'] = new_order['id']
```

---

## 7. State Management & balance_broker formula

### 7.1 SubaccountState

```python
from dataclasses import dataclass, field
from datetime import datetime

@dataclass
class SubaccountState:
    """
    Memoizes state. balance_broker e CALCULAT (NU stored) — vezi property below.
    """
    pool_total: float = 100.0          # capital initial fix
    equity: float = 50.0               # echity tradeable curent
    pool_used: float = 0.0             # cât din rezervă a fost consumat
    reset_count: int = 0               # nr reseturi în cycle curent
    cycle_num: int = 1
    cycle_start_ts: datetime = field(default_factory=datetime.utcnow)
    cycle_peak: float = 100.0
    
    @property
    def balance_broker(self) -> float:
        """
        Formula EXACT din vse_5pair_portfolio.py linia 107:
        balance_broker = (pool_total - pool_used) + (equity - equity_start)
                       = $100 - pool_used + (equity - $50)
        """
        equity_start = 50.0
        return (self.pool_total - self.pool_used) + (self.equity - equity_start)
```

### 7.2 Reset Cycle Logic (NO KILL — formula exactă)

```python
def on_trade_closed(state: SubaccountState, pnl_net: float, cfg: VSEConfig) -> str:
    """
    Apelat după FIECARE close (TS sau OPP).
    Returns: 'NONE' | 'RESET' | 'SUCCESS' | 'POOL_LOW'
    """
    # 1. Update equity (NOT balance_broker — calculat automat din property)
    state.equity += pnl_net
    state.cycle_peak = max(state.cycle_peak, state.balance_broker)
    
    # 2. Cycle SUCCESS check (POST-close, nu la entry-uri!)
    if state.balance_broker >= 5000:
        return 'SUCCESS'
    
    # 3. Reset trigger (equity < $15)
    if state.equity < 15:
        # Formula exactă din vse_5pair_portfolio.py:
        needed = 50.0 - state.equity                      # ex: 50 - 12 = $38
        avail = (state.pool_total - 50.0) - state.pool_used  # rezerva rămasă
        
        if needed <= avail:
            # Reset normal
            state.pool_used += needed
            state.equity = 50.0                           # restart strict la $50
            state.reset_count += 1
        else:
            # Rezerva INSUFICIENTĂ — consume tot ce-a rămas
            state.pool_used += avail
            state.equity += avail
            # NO KILL: bot continuă să tradeze cu echity redus
            # Dacă echity ajunge negativ → margin Bybit oprește trade-urile natural
        
        if state.balance_broker < 30:
            log_warning(f"Pool low: ${state.balance_broker:.2f}")
            return 'POOL_LOW'
        return 'RESET'
    
    return 'NONE'
```

### 7.3 Cycle SUCCESS handling

```python
async def handle_cycle_success(state, subacc, cfg, all_open_positions):
    """
    Apelat când balance_broker >= $5,000 după un close.
    """
    withdraw_amount = state.balance_broker - state.pool_total
    log_info(f"🎉 CYCLE SUCCESS: withdraw ${withdraw_amount:.2f}")
    
    # 1. Close ALL open positions (TOATE — și KAIA și AAVE pe Subacc 1)
    for pos in all_open_positions:
        await close_position(subacc, pos, reason='CYCLE_SUCCESS')
    
    # 2. Notify
    await alert_telegram(f"SUCCESS pe {subacc.name}: withdraw ${withdraw_amount:.2f}")
    
    # 3. Auto-transfer la master (sau manual)
    if cfg.auto_withdraw:
        await transfer_to_master(subacc, withdraw_amount)
    
    # 4. Restart cycle
    state.equity = 50.0
    state.pool_used = 0.0
    state.reset_count = 0
    state.cycle_num += 1
    state.cycle_start_ts = datetime.utcnow()
    state.cycle_peak = state.pool_total
```

---

## 8. Multi-position pe pool comun (KAIA+AAVE pe Subacc 1)

```
Pool comun = 1 pereche poate avea 1 trade activ + altă pereche poate avea 1 trade activ.
Maxim 2 poziții simultan per subaccount (1 per simbol).

Cooldown e PER PERECHE, NOT per subaccount:
  KAIA exit la 14:00 → cooldown KAIA până la 17:00 (3 bars × 1H)
  AAVE poate intra la 15:00 INDEPENDENT
```

### 8.1 Order de procesare per bar nouă

```python
async def on_new_bar_closed(subacc, state, all_signal_engines, all_open_positions):
    """
    La fiecare bară închisă (1H pentru KAIA/AAVE/ONT, 2H pentru ETH):
      1. Procesează exits planificate (OPP din bar precedentă)
      2. Update trailing stops + check opposite signals (planează exit OPP)
      3. Check signals noi pentru ENTRY (cu cooldown)
      4. Execute entries planificate la NEXT BAR OPEN
    """
    # Step 1: Execute planned exits (din bar precedentă)
    for pos in all_open_positions:
        if pos.get('exit_planned'):
            await close_position(subacc, pos, reason=pos['exit_reason_planned'])
            # On close → on_trade_closed → check cycle
            event = on_trade_closed(state, pos['pnl_net'], cfg)
            if event == 'SUCCESS':
                await handle_cycle_success(state, subacc, cfg, all_open_positions)
                return  # cycle ended, restart
            
            # Reset cooldown pentru perechea închisă
            all_signal_engines[pos['symbol']].on_position_closed()
    
    # Step 2: Update trailing + check opposite signals (planează exits)
    for pos in all_open_positions:
        df_ind = compute_indicators(latest_buffer[pos['symbol']], cfg)
        df_sig = build_signals(df_ind, cfg)
        await check_exit_conditions(subacc, pos, df_sig)
    
    # Step 3: Check signals noi pentru entry
    for symbol, engine in all_signal_engines.items():
        if symbol in [p['symbol'] for p in all_open_positions]:
            continue   # deja avem poziție, NU dublăm
        
        signal = engine.on_new_bar_closed(latest_bar[symbol])
        if signal:
            # Step 4: Execute entry la NEXT TICK (market order)
            position = await open_trade_live(subacc, signal, state, cfg)
            if position:
                all_open_positions.append(position)
```

### 8.2 Open trade live

```python
async def open_trade_live(subacc, signal, state, cfg):
    """Place market order entry + stop_market SL."""
    sizing = compute_position_size(state.equity, signal['sl_pct'], 
                                    state.balance_broker, cfg)
    
    if not can_afford(sizing, state.balance_broker, cfg):
        log_warning(f"Skip: margin insufficient")
        return None
    
    # 1. Market entry (execută la prețul curent ≈ next_bar_open în timing)
    market = await subacc.fetch_market(signal['symbol'])
    step_size = market['precision']['amount']
    
    # qty calc cu estimated_entry_price (vom recalcula după ce primim avg fill price)
    qty = compute_qty(sizing['pos_usd'], signal['estimated_entry_price'], step_size)
    
    bybit_side = 'buy' if signal['side'] == 'long' else 'sell'
    order_entry = await subacc.create_order(
        symbol=signal['symbol'],
        type='market',
        side=bybit_side,
        amount=qty,
    )
    
    # 2. Get actual fill price
    actual_entry_price = order_entry.get('average') or order_entry.get('price')
    
    # 3. Recalculate SL distance based on actual fill (slippage)
    actual_sl_pct = abs(actual_entry_price - signal['sl_price']) / actual_entry_price
    if actual_sl_pct < cfg.sl_min_pct or actual_sl_pct > cfg.sl_max_pct:
        # Slippage extreme — close pozitia imediat
        log_warning(f"Slippage out of bounds, closing immediately")
        await subacc.create_order(symbol=signal['symbol'], type='market',
                                   side='sell' if signal['side']=='long' else 'buy',
                                   amount=qty, params={'reduceOnly': True})
        return None
    
    # 4. Place SL stop_market (reduce_only)
    sl_side = 'sell' if signal['side'] == 'long' else 'buy'
    order_sl = await subacc.create_order(
        symbol=signal['symbol'],
        type='stop_market',
        side=sl_side,
        amount=qty,
        params={
            'stopPrice': signal['sl_price'],
            'reduceOnly': True,
        },
    )
    
    return {
        'symbol': signal['symbol'],
        'side': signal['side'],
        'entry_price': actual_entry_price,
        'sl_price': signal['sl_price'],
        'qty': qty,
        'pos_usd': qty * actual_entry_price,
        'risk_usd': sizing['risk_usd'],
        'opened_ts': datetime.utcnow(),
        'status': 'OPEN',
        'order_entry_id': order_entry['id'],
        'order_sl_id': order_sl['id'],
        'exit_planned': False,
    }
```

---

## 9. Edge Cases CRITICAL

### 9.1 Bara încă în formare

```
PROCESS DOAR pe bare ÎNCHISE!

Pe ETH 2H: bare închid la 12:00, 14:00, 16:00... UTC
Pe KAIA/AAVE/ONT 1H: bare închid la :00 fix UTC

NU procesa current forming bar (latest tick != closed bar).
Verifică timestamp: dacă (now - bar_close_ts) < 30 sec → bara încă activă, ignoră.
```

### 9.2 Restart bot mid-cycle

```python
async def reconcile_on_startup(subacc, state):
    """
    1. Load state din JSON (cycle_num, equity, pool_used, etc.)
    2. Fetch poziții deschise pe Bybit
    3. Verifică match cu state.open_positions (din JSON)
    4. Dacă diferă → ALERT user manual review (NU auto-closure!)
    """
    actual_positions = await subacc.fetch_positions()
    saved_positions = state.open_positions  # din JSON
    
    if len(actual_positions) != len(saved_positions):
        log_warning("Positions mismatch! Manual review needed.")
        await alert_telegram(f"⚠️ State mismatch on {subacc.name}: "
                            f"saved {len(saved_positions)} vs actual {len(actual_positions)}")
        # NU procedeaza automat — așteaptă user input
        return False
    
    # Sync stops (Bybit poate avea SL diferit dacă bot a fost down în timpul trailing)
    for pos in saved_positions:
        actual = next((p for p in actual_positions if p['symbol'] == pos['symbol']), None)
        if actual and actual['stop_loss'] != pos['sl_price']:
            log_info(f"SL desync on {pos['symbol']}, syncing to actual")
            pos['sl_price'] = actual['stop_loss']
    
    return True
```

### 9.3 Slippage handling

```
Pe market orders, slippage real != backtest:
  Backtest assumes execution la opens[i+1] (next bar open exact)
  Live: execution la primul tick disponibil (~next_open ± 1-3 ticks)
  
Mitigare:
  1. Folosește limit orders cu post-only? NU recomand (pierzi entry-uri)
  2. Folosește market orders cu acceptare slippage 0.3%
  3. Dacă slippage > 0.5%, close pozitia imediat (vezi open_trade_live step 3)
  
Pe Bybit, perechi cu volum >$10M/zi (KAIA, AAVE, ONT, ETH) au slippage tipic 0.05-0.15%.
```

### 9.4 Bybit API errors

```python
async def safe_api_call(coro, max_retries=3):
    """Retry cu exponential backoff."""
    for attempt in range(max_retries):
        try:
            return await coro
        except (NetworkError, ExchangeError) as e:
            if attempt == max_retries - 1:
                log_error(f"API failed after {max_retries} retries: {e}")
                raise
            wait = 2 ** attempt
            log_warning(f"API retry {attempt+1}/{max_retries} in {wait}s: {e}")
            await asyncio.sleep(wait)
```

---

## 10. Validation Checklist

### 10.1 Backtest replay (CRITICAL — match cu CSV)

```
Compară outputul botului în mode "replay" cu trades_setup_target5k.csv:
  - 457 trade-uri totale
  - Subacc 1: 225 trades, $5,427 PnL, 2 cycles (1 SUCCESS, 1 TIMEOUT_PROFIT)
  - Subacc 2: 232 trades, $9,973 PnL, 2 cycles (1 SUCCESS, 1 TIMEOUT_PROFIT)
  - 16 OPP exits (raw_short pentru long sau invers)
  - Cycle 1 Subacc 1 SUCCESS la 2026-01-06 16:00, balance $5,355
  - Cycle 1 Subacc 2 SUCCESS la balance $8,722 (single big close)

Match >95% trades (timestamp + side + symbol + ~0.5% PnL diff) → IMPLEMENTARE CORECTĂ.
```

### 10.2 Testnet (2-4 săptămâni)

- [ ] Signal generation match cu CSV (>90% overlap)
- [ ] Entry pe NEXT BAR OPEN (verify 5 trade-uri manual)
- [ ] OPP exits funcționează corect (force scenario)
- [ ] TS trailing update la fiecare bar
- [ ] Cooldown 3 bars per pereche (force back-to-back signals)
- [ ] PnL calculation match backtest cu fee 0.055% × 2
- [ ] Reset trigger funcționează (force equity < $15)
- [ ] Cycle SUCCESS detection POST-close (force balance > $5k)
- [ ] Multi-position pe pool comun (KAIA + AAVE simultane)
- [ ] State persistence (kill bot mid-trade, restart, reconcile)

### 10.3 Live $50 per subacc (4 săptămâni)

- [ ] Slippage < 0.3% per trade
- [ ] Frecvență ~17 trade-uri/lună (combined)
- [ ] PF live > 1.30
- [ ] Drawdown max < $20

### 10.4 Live $100 per subacc

- [ ] Cycle 1 SUCCESS în 12-24 luni
- [ ] Withdraw flow funcționează (auto sau manual)

---

## 11. Settings Summary (Quick Reference)

```yaml
# === CONFIG STRATEGY ===
strategy: VSE Balanced + reset cycles
config:
  pool_total: 100.0
  equity_start: 50.0
  risk_pct: 0.20
  reset_trigger: 15.0
  reset_target: 50.0
  max_resets: NONE        # NO KILL SWITCH
  withdraw_target: 5000.0
  leverage_max: 20
  cap_pct_of_max: 0.95
  taker_fee: 0.00055

# === INDICATORS (ALL DEFAULT — NU schimba!) ===
indicators:
  mcginley_length: 14
  whiteline_length: 20
  ttms_length: 20
  ttms_bb_mult: 2.0
  ttms_kc_mult_widest: 2.0
  ttms_green_red: true
  ttms_highlight: true
  ttms_cross: true
  tether_fast: 13
  tether_slow: 55
  vortex_length: 14
  vortex_threshold: 0.05
  st_atr_length: 22
  st_atr_mult: 3.0
  st_wicks: true

# === TRADE FILTERS ===
filters:
  style: "Balanced"
  entry_filter_bars: 3        # cooldown post-exit per pereche
  sl_min_pct: 0.005           # 0.5%
  sl_max_pct: 0.035           # 3.5%

# === EXIT (DUAL!) ===
exits:
  use_supertrend_trailing: true
  use_opposite_signal_exit: true   # ⚠️ CRITICAL — 21% din wealth!
  
# === EXECUTION TIMING ===
timing:
  signal_evaluation: "bar_close"        # pe close-ul barei
  entry_execution: "next_bar_open"      # market order la deschidere bară
  ts_exit: "intra_bar"                  # SL hit imediat când prețul atinge
  opp_exit: "next_bar_open"             # opposite signal → exit la next open
```

---

## 12. References

- **Cod existent (REUTILIZABIL — NU rescrie!)**:
  - `vortex_sniper.py` — toate indicatorii (compute_indicators, build_signals)
  - `vse_5pair_portfolio.py` — backtest engine cu reset cycles
  - `vse_opp_exit.py` — simulate_exit_opposite (CRITICAL — copy direct!)
  - `vse_enhanced.py` — build_entry_list_enhanced
- **Validation CSV**: `trades_setup_target5k.csv` (457 trades reference)
- **Strategy doc**: `strategy.md` (decizii + matematică)
- **Setări**: `CONFIG.md` (multi-subaccount setup)
- **Pine Script source**: "Vortex Sniper Elite @DaviddTech"

---

## 13. Quick Implementation Path

```bash
# 1. Reutilizează din vortex_sniper.py:
#    - compute_indicators()
#    - build_signals()
#    NU rescrie de la zero!

# 2. Reutilizează din vse_opp_exit.py:
#    - simulate_exit_opposite() logic
#    Adaptează pentru live (per-bar, NOT one-shot)

# 3. Implementează:
#    - VSESignalLive (sec 4)
#    - SubaccountState + balance_broker formula (sec 7)
#    - on_trade_closed (sec 7.2) — formula reset EXACT
#    - check_exit_conditions cu OPP (sec 6.1)
#    - on_new_bar_closed orchestrator (sec 8.1)
#    - open_trade_live (sec 8.2)

# 4. Boilerplate-ul tău existent:
#    - ccxt async client per subaccount
#    - WebSocket klines stream PER PERECHE PER TF
#    - charting / dashboard
#    - state JSON persistence

# 5. Config (CONFIG.md) → YAML loading

# 6. Validation:
#    - REPLAY backtest pe parquet
#    - Compară cu trades_setup_target5k.csv
#    - Match >95% → OK pentru testnet
#    - Mismatch → debug indicators sau exit logic
```
