import asyncio
import os
from metaapi_cloud_sdk import MetaApi

# ==========================================
# 1. PARAMÈTRES DE CONNEXION METAAPI
# ==========================================
TOKEN = "eyJhbGciOiJSUzUxMiIsInR5cCI6IkpXVCJ9.eyJfaWQiOiJlNzRjNTE2NjhkNjRhZTJkODI2NGE3YTMzZWYxMzE0YyIsImFjY2Vzc1J1bGVzIjpbeyJpZCI6InRyYWRpbmctYWNjb3VudC1tYW5hZ2VtZW50LWFwaSIsIm1ldGhvZHMiOlsidHJhZGluZy1hY2NvdW50LW1hbmFnZW1lbnQtYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcmVzdC1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcnBjLWFwaSIsIm1ldGhvZHMiOlsibWV0YWFwaS1hcGk6d3M6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcmVhbC10aW1lLXN0cmVhbWluZy1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOndzOnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIiwid3JpdGVyIl0sInJlc291cmNlcyI6WyIqOiRVU0VSX0lEJDoqIl19LHsiaWQiOiJtZXRhc3RhdHMtYXBpIiwibWV0aG9kcyI6WyJtZXRhc3RhdHMtYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6InJpc2stbWFuYWdlbWVudC1hcGkiLCJtZXRob2RzIjpbInJpc2stbWFuYWdlbWVudC1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciIsIndyaXRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfSx7ImlkIjoiY29weWZhY3RvcnktYXBpIiwibWV0aG9kcyI6WyJjb3B5ZmFjdG9yeS1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciIsIndyaXRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfSx7ImlkIjoibXQtbWFuYWdlci1hcGkiLCJtZXRob2RzIjpbIm10LW1hbmFnZXItYXBpOnJlc3Q6ZGVhbGluZzoqOioiLCJtdC1tYW5hZ2VyLWFwaTpyZXN0OnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIiwid3JpdGVyIl0sInJlc291cmNlcyI6WyIqOiRVU0VSX0lEJDoqIl19LHsiaWQiOiJiaWxsaW5nLWFwaSIsIm1ldGhvZHMiOlsiYmlsbGluZy1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfV0sImlnbm9yZVJhdGVMaW1pdHMiOmZhbHNlLCJ0b2tlbklkIjoiMjAyMTAyMTMiLCJpbXBlcnNvbmF0ZWQiOmZhbHNlLCJyZWFsVXNlcklkIjoiZTc0YzUxNjY4ZDY0YWUyZDgyNjRhN2EzM2VmMTMxNGMiLCJpYXQiOjE3OTExMjkxMDcsImV4cCI6MTc5ODkwNTEwN30.EMaLTzAsRxC9EhEwx4Wf7DuJdzy0FBWq0k63NRo9wMk9tOH7ESFPGxhGL3M6YVJZl5dJRcLvtMixs6-lwsnAKlXiAmyEFbdVV4VM16pP2thW4ij3GJHoqXtSh5MQOyAf1rMa6LRWCk-krGT6pJduZYk8lJbEhaCIgnLWh1G5-TsSg0EfKkjoygbvvmie2pybvYw3O7dWrxRmNzSpS6nVmdTANjwWARwkh0I4rCqUe1Af-AUI0sEYwk_k6clhY_wyj22JtiRVAxrzQfqTNPVjp82XKHZyIFXpwl5P4Lpr38PNaifLNbRExyY3F7BF9XMbdGJZXvLs3E-JM2sx0Qb9fyRedSpwRQvgOZPfC8JBL7dFYlpP9yS-Cp03bg29wFyLO-ApEKXxk3SHcLM_AqooUP8UlLUwyQF7DbMUd-t2_3LR19WJEsT--MVv9HHm4U4LlGv9Z-M3eseAPKQ3NaCXkaZjefSXNB9M2R8gAITo54IEjpyR8ISfDB_qo8M5TtuQb0Vl5fgomURwPT_T5gra8ac1E4AEFtNameAuCrzeWsOWw5fZCIDc2YKAYmiM6hZdk5v_ez-v_hAdvEYFmAHqDgejm0F9HnnoAyt6huvHOYJmD634ltEysjpFBTYwCkikqzZ_0bNcxxiNmahDnL8kDueTxGRV5epI8wlkY7uYJaw"
ACCOUNT_ID = "de1c262b-17d4-415c-bb1f-c49db0342d25"

# ==========================================
# 2. PARAMÈTRES POUR L'OR (GOLD)
# ==========================================
SYMBOL = "XAUUSD"            # Symbole Or
TIMEFRAME = "15m"            # Unité de temps
LOT_SIZE = 0.01              # Taille du lot
PIPS_TO_BREAK_EVEN = 15      # Profit en pips avant Break-Even


async def manage_break_even(connection):
    """Vérifie les positions ouvertes et applique le Break-Even sur XAUUSD"""
    positions = await connection.get_positions()
    for pos in positions:
        if pos.get("symbol") not in [SYMBOL, "GOLD"]:
            continue

        open_price = pos["openPrice"]
        current_price = pos["currentPrice"]
        position_type = pos["type"]

        profit_pips = 0
        if position_type == "POSITION_TYPE_BUY":
            profit_pips = (current_price - open_price) * 10
        elif position_type == "POSITION_TYPE_SELL":
            profit_pips = (open_price - current_price) * 10

        if profit_pips >= PIPS_TO_BREAK_EVEN and pos.get("stopLoss") != open_price:
            print(f"[{SYMBOL}] Break-Even activé pour la position #{pos['id']}")
            await connection.modify_position(
                pos["id"], stop_loss=open_price, take_profit=pos.get("takeProfit")
            )


async def main():
    api = MetaApi(TOKEN)
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)

    if account.state != "DEPLOYED":
        print("Déploiement du compte MetaApi...")
        await account.deploy()

    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()

    price = await connection.get_symbol_price(SYMBOL)
    print(f"--- BOT CONNECTÉ SUR {SYMBOL} ({TIMEFRAME}) | Bid: {price['bid']} | Ask: {price['ask']} ---")

    await manage_break_even(connection)


if __name__ == "__main__":
    asyncio.run(main())
