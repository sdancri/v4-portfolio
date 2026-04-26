# Bot Configuration — Setări validate

**Pentru:** Claude Code  
**Setup multi-subaccount Bybit perpetuals.**

---

## 1. Strategy Config (Nou1)

```yaml
# Aplicabil ambelor subconturi
strategy:
  name: "VSE_Nou1_NoKill"
  
  # Capital management
  pool_total: 100.0              # USDT initial per subaccount
  equity_start: 50.0             # USDT tradeable inițial
  risk_pct_equity: 0.20          # 20% × equity per trade
  
  # Reset cycle logic
  reset_trigger: 15.0            # equity < $15 → reset
  reset_target: 50.0             # restart equity la $50
  max_resets: null               # NO KILL SWITCH (continuă oricât)
  
  # Withdraw target
  withdraw_target: 5000.0        # cycle SUCCESS la $5,000 balance
  
  # Stop loss bounds
  sl_min_pct: 0.005              # 0.5% minim
  sl_max_pct: 0.035              # 3.5% maxim
  
  # ATR multiplier pentru SL
  atr_period: 14
  atr_multiplier: 1.5
  
  # Cooldown post-exit (per pereche)
  cooldown_bars: 3

  # Exit logic (DUAL — CRITICAL!)
  use_supertrend_trailing: true
  use_opposite_signal_exit: true   # ⚠️ NU dezactiva! 21% din wealth vine din OPP exits

  # Execution timing
  signal_evaluation: "bar_close"
  entry_execution: "next_bar_open"
  opp_exit_execution: "next_bar_open"
  ts_exit_execution: "intra_bar"
  
  # Bybit settings
  leverage: 20
  safety_buffer: 0.95
  taker_fee: 0.00055             # 0.055% per trade side
```

---

## 2. VSE Indicator Params

```yaml
vse_params:
  vortex_period: 14
  mcginley_period: 14
  ttms_period: 20
  
  # Filters (default; fine-tune doar dacă PF live < 1.20)
  use_squeeze_filter: true       # entry doar pe volatility expansion
  use_mcginley_filter: true      # entry doar pe trend confirmation
```

---

## 3. Subaccount Setup

```yaml
subaccounts:
  
  - name: "subacc_1_kaia_aave"
    enabled: true
    pairs:
      - symbol: "KAIAUSDT"
        timeframe: "1h"
      - symbol: "AAVEUSDT"
        timeframe: "1h"
    capital: 100.0
    api_credentials_env_prefix: "SUB1"   # SUB1_API_KEY, SUB1_API_SECRET în .env
    expected_wealth_2.3y: 13364
    
  - name: "subacc_2_ont_eth"
    enabled: true
    pairs:
      - symbol: "ONTUSDT"
        timeframe: "1h"
      - symbol: "ETHUSDT"
        timeframe: "2h"
    capital: 100.0
    api_credentials_env_prefix: "SUB2"
    expected_wealth_2.3y: 11485
```

---

## 4. Environment Variables (.env)

```bash
# Bybit API keys per subaccount (Trade ENABLED, Withdraw DISABLED)
SUB1_API_KEY=xxxxxxxxxxxxxxxx
SUB1_API_SECRET=xxxxxxxxxxxxxxxx

SUB2_API_KEY=xxxxxxxxxxxxxxxx
SUB2_API_SECRET=xxxxxxxxxxxxxxxx

# Master account API (pentru transfers la cycle SUCCESS)
MASTER_API_KEY=xxxxxxxxxxxxxxxx
MASTER_API_SECRET=xxxxxxxxxxxxxxxx

# Mode
TRADING_MODE=testnet            # testnet | live

# Optional: Telegram alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

---

## 5. Bybit Permissions Setup

### Per-subaccount API key:

```
✓ Contract Trade (Read+Write)
✓ Position (Read+Write)
✓ Order (Read+Write)

✗ Withdraw (DISABLED — security)
✗ Sub-account (DISABLED)
```

### Master account API key:

```
✓ Universal Transfer (pentru auto-withdraw la cycle SUCCESS)
✓ Sub-account Read (pentru monitoring balance)

✗ Withdraw (DISABLED — security)
```

---

## 6. Per-Pair Bybit Settings

Setări pre-trade pe Bybit (manual sau via API la startup):

```yaml
bybit_pair_settings:
  KAIAUSDT:
    leverage: 20
    margin_mode: isolated         # SAU cross — vezi notă
    
  AAVEUSDT:
    leverage: 20
    margin_mode: isolated
    
  ONTUSDT:
    leverage: 20
    margin_mode: isolated
    
  ETHUSDT:
    leverage: 20
    margin_mode: isolated
```

### Notă: Isolated vs Cross margin

```
Isolated (recomandat):
  - Margin lock-uit per poziție
  - Lichidare poziție individuală nu afectează alte poziții
  - Mai sigur pentru pool comun multi-pair
  
Cross:
  - Pool comun de margin
  - Mai eficient capital
  - DAR: una pierdere pe KAIA poate "magnetiza" margin de la AAVE
  
Pentru pool comun KAIA+AAVE (Subacc 1): ISOLATED
Pentru ONT+ETH 2H (Subacc 2): ISOLATED
```

---

## 7. Operational Limits

```yaml
operational:
  # Trade frequency limits (sanity checks)
  max_trades_per_day_per_subacc: 5
  max_concurrent_positions_per_subacc: 2
  
  # API rate limits
  max_klines_requests_per_minute: 60
  max_order_requests_per_minute: 100
  
  # Bot health checks
  heartbeat_interval_seconds: 60
  max_consecutive_api_errors: 5    # după → pause subacc, alert user
  
  # State persistence
  save_state_interval_seconds: 300  # save state la fiecare 5 min
  state_dir: "./state"
  log_dir: "./logs"
```

---

## 8. Position Sizing — Math Sample

Pentru config Nou1 cu equity = $50, sl_pct = 0.02:

```
risk_usd  = 0.20 × $50    = $10.00
pos_usd   = $10 / 0.02    = $500.00
qty       = $500 / price   (depends on symbol)
margin    = $500 / 20      = $25.00

Pentru KAIAUSDT @ $0.40:
  qty = 500 / 0.40 = 1250 KAIA
  
Pentru AAVEUSDT @ $200:
  qty = 500 / 200 = 2.5 AAVE
  
Pentru ONTUSDT @ $0.30:
  qty = 500 / 0.30 = 1666.67 ONT
  
Pentru ETHUSDT @ $3000:
  qty = 500 / 3000 = 0.1667 ETH
```

**Pe Bybit, qty trebuie rotunjit la step size**:
```python
# Pre-trade: fetch instrument info
instrument = await bybit.fetch_market(symbol)
step_size = instrument['precision']['amount']
qty = round_down(qty, step_size)
```

---

## 9. Withdraw Logic — Cycle SUCCESS

### Auto-transfer (preferat dacă API permits):

```python
async def auto_withdraw_to_master(subacc_id, amount):
    """
    Transfer USDT din subaccount → master account.
    """
    result = await bybit.universal_transfer(
        coin='USDT',
        amount=amount,
        from_account_type='UNIFIED',
        to_account_type='UNIFIED',
        from_member_id=subacc_id,
        to_member_id=master_id,
    )
    return result
```

### Manual fallback:

```
1. Bot detectează cycle SUCCESS
2. Alert user (Telegram + log)
3. Bot pauză tradingul pe acel subacc
4. User execută manual transfer în UI Bybit
5. User confirmă în bot (CLI command sau Telegram /confirm_withdraw subacc_1)
6. Bot restart cycle pe subacc
```

---

## 10. Telegram Alerts (opțional)

```yaml
telegram_alerts:
  enabled: false                  # set true pentru live
  
  events:
    cycle_success: true           # critical
    pool_exhaust_warning: true    # critical (balance < $30)
    api_error_5_consecutive: true # critical
    daily_summary: true           # informational
    trade_opened: false           # noisy, off
    trade_closed: false           # noisy, off
```

### Sample alert templates:

```python
ALERT_CYCLE_SUCCESS = """
🎉 CYCLE SUCCESS pe {subacc_name}!

Withdraw: ${amount:.2f}
Cycle duration: {days} zile
Cycle #: {cycle_num}

Action: transfer manual în UI Bybit (sau auto dacă config permits).
"""

ALERT_POOL_WARNING = """
⚠️ POOL WARNING pe {subacc_name}:

Balance fizic: ${balance:.2f} (sub $30!)
Equity: ${equity:.2f}
Reset count: {resets}

Action: review necessary, posibil re-deposit sau abandon subacc.
"""
```

---

## 11. Backtest Replay (validation)

Pentru a verifica că botul live match-uiește backtest-ul:

```python
# Replay mode: rulează botul pe date istorice (parquet)
# vs live (klines real-time de la Bybit)

backtest_replay:
  enabled: true                  # default false
  start_date: "2026-01-01"
  end_date: "2026-04-25"
  
  data_source:
    KAIAUSDT_1h: "/path/to/KAIAUSDT_1h.parquet"
    AAVEUSDT_1h: "/path/to/AAVEUSDT_1h.parquet"
    ONTUSDT_1h:  "/path/to/ONTUSDT_1h.parquet"
    ETHUSDT_2h:  "/path/to/ETHUSDT_2h.parquet"
  
  expected_results:
    subacc_1:
      n_trades: 48
      pf: 1.27
      wealth: 783
    subacc_2:
      n_trades: 23
      pf: 3.28
      wealth: 340
```

Dacă replay match-uiește backtest-ul → botul e corect implementat.

---

## 12. Quick Start Commands

```bash
# Install dependencies
pip install ccxt pandas numpy pyyaml python-dotenv aiohttp

# Run testnet
TRADING_MODE=testnet python main.py

# Run live (după validare testnet)
TRADING_MODE=live python main.py

# Replay backtest (verify implementare)
python main.py --replay --start 2026-01-01 --end 2026-04-25
```

---

## 13. References

- **Strategy logic**: `STRATEGY_LOGIC.md` (acest folder)
- **Math validated**: `strategy.md` (decizii + MC results)
- **Existing code**: `vse_5pair_portfolio.py`, `vortex_sniper.py`, `vse_enhanced.py`
