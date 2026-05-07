"""Diagnostic Bybit live — citește direct starea pentru un subaccount.

Folosire pe VPS:
  docker exec VSE_2 python scripts/diag_bybit.py

Read-only: fetch_balance + fetch_position + fetch_open_orders pe perechile
configurate ale subaccount-ului. NU modifică nimic.
"""
from __future__ import annotations
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ichimoku_bot.config import load_config
from ichimoku_bot.exchange.bybit_client import BybitClient


async def main() -> int:
    cfg = load_config("config/config.yaml")
    sub_name = os.getenv("SUBACCOUNT_NAME", "")
    target = next((s for s in cfg.subaccounts if s.enabled and (not sub_name or s.name == sub_name)), None)
    if target is None:
        print(f"❌ subaccount '{sub_name}' nu există")
        return 1

    api_key = os.environ.get("BYBIT_API_KEY", "")
    api_secret = os.environ.get("BYBIT_API_SECRET", "")
    if not api_key or not api_secret:
        print("❌ BYBIT_API_KEY / BYBIT_API_SECRET lipsesc")
        return 1

    testnet = os.getenv("TRADING_MODE", "live").lower() != "live"
    print(f"\n═══ Subaccount: {target.name}  (mode: {'testnet' if testnet else '🔴 LIVE'}) ═══")

    client = await BybitClient.create(api_key, api_secret, testnet=testnet)
    try:
        balance = await client.fetch_balance_usdt()
        print(f"\n💰 Balance USDT: ${balance:,.2f}")

        for pair in target.pairs:
            print(f"\n── {pair.symbol} {pair.timeframe} ──")
            try:
                pos = await client.fetch_position(pair.symbol)
                if pos and float(pos.get("contracts") or 0) > 0:
                    print(f"  📍 POZIȚIE DESCHISĂ:")
                    print(f"     side: {pos.get('side')}")
                    print(f"     qty: {pos.get('contracts')}")
                    print(f"     entry: {pos.get('entryPrice')}")
                    print(f"     mark: {pos.get('markPrice')}")
                    print(f"     unrealized PnL: {pos.get('unrealizedPnl')}")
                else:
                    print(f"  ✅ fără poziție deschisă")
            except Exception as e:
                print(f"  ❌ fetch_position fail: {e!r}")

            try:
                orders = await client.fetch_open_orders(pair.symbol)
                if orders:
                    print(f"  📋 {len(orders)} ordine deschise:")
                    for o in orders:
                        info = o.get("info", {})
                        print(f"     id={o.get('id')[:8]} type={o.get('type')} "
                              f"side={o.get('side')} qty={o.get('amount')} "
                              f"trigger={info.get('triggerPrice')} "
                              f"stopOrderType={info.get('stopOrderType')} "
                              f"reduce_only={info.get('reduceOnly')}")
                else:
                    print(f"  ✅ fără ordine deschise")
            except Exception as e:
                print(f"  ❌ fetch_open_orders fail: {e!r}")

        print("\n═══ Concluzie ═══")
        print("  Comparați output-ul cu 'docker logs VSE_2 | grep ONT' pentru a vedea")
        print("  ce a făcut bot-ul pe ultima bară (BAR_RECEIVED, FILTER_REJECTED, etc.)")
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
