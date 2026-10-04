import asyncio
import os
from metaapi_cloud_sdk import MetaApi

TOKEN = os.getenv("METAAPI_TOKEN", "EyJhbGciOiJSUzUxMiIsInR5cCI6IkpXVCJ9.eyJfaWQiOiJlNzRjNTE2NjhkNjRhZTJkODI2NGE3YTMzZWYxMzE0YyIsImFjY2Vzc1J1bGVzIjpbeyJpZCI6InRyYWRpbmctYWNjb3VudC1tYW5hZ2VtZW50LWFwaSIsIm1ldGhvZHMiOlsidHJhZGluZy1hY2NvdW50LW1hbmFnZW1lbnQtYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiXSwicmVzb3VyY2VzIjpbImFjY2Vzc1J1bGVzIjpbImFjY291bnQ6JFVTRVJfSUQkOmRlMWMyNjJiLTE3ZDQtNDE1Yy1iYjFmLWM0OWRiMDM0MmQyNSJdfV0sImlnbm9yZVJhdGVMaW1pdHMiOmZhbHNlLCJ0b2tlbklkIjoiMjAyMTAyMTMiLCJpbXBlcnNvbmF0ZWQiOmZhbHNlLCJyZWFsVXNlcklkIjoiZTc0YzUxNjY4ZDY0YWUyZDgyNjRhN2EzM2VmMTMxNGMiLCJpYXQiOjE3OTExMjI0OTIsImV4cCI6MTc5ODg5ODQ5Mn0.gW6UZ-jCI-P1BcsU1RbAAFWXq5vhO7X8EoBvUAL5YhPaXC3x5eoFaRkekMYosCFqaG2CUBDF2N2RzwrPziFgE9uZDtQmBGGmPeYBKepQg-TvqVd7Jo3bNkygc4qseVWUr6zGH9IGxJYKWlyD9t4DwCPY07qCcMhzDLnXD4YRtrX97L0WYAy2Nr76q7QiZ3JWlz4Sz-0dRv5bJfnYAamiagC6YDVKXaGAyMFADgcL8vM-mnNcqGt1Fj2DlqRb-MTUf0KrX_CEKTTwVh64HT1EV_WYz80Kq4u72RMSWRjVBKmpmdjn69tvD204-Gxed8UOsEdSH_FBQ_3keVs4KXISSekgHYx0khLZSdjolQcsbKVoT6u-UWIALA83P_odEnJIt4uSWAAeWPjKDVEI1aXLVcymKq0CefMIlD-NYNlE0qLwruAI93zaPbOPsk23GCS7bGg-F_MsG58puU_NFgJs4VRBJXG7N07gZOuAtr5sgtda0nPOvzY3GFY353aeQwW9Xp4bMz1nzYSBOaf3H2wNuiCk0AQtrtIN3EHbAIK0YCuXt7GxGLLxOMKUeOBMYn1ZjcIfefwSj-_nTLmha_dOZVv8h_8rc2un7MwtNvLpCGm87n_efidtzPk5n39BmvqtLRTsCpNBXjawaDv7PAtcVAFxamqb2TYic4IL3duqsWg")
ACCOUNT_ID = "de1c262b-17d4-415c-bb1f-c49db0342d25"
SYMBOL = "EURUSD"
RISK_PIPS_BE = 15  # Mettre en Break-Even après 15 pips de profit

async def apply_break_even(connection):
    """Gère le déplacement du Stop Loss à PFX (Break-Even)"""
    positions = await connection.get_positions()
    for pos in positions:
        open_price = pos['openPrice']
        current_price = pos['currentPrice']
        position_type = pos['type']
        
        # Calcul du profit en pips
        profit_pips = 0
        if position_type == 'POSITION_TYPE_BUY':
            profit_pips = (current_price - open_price) * 10000
        elif position_type == 'POSITION_TYPE_SELL':
            profit_pips = (open_price - current_price) * 10000

        # Si le profit dépasse le seuil et que le SL n'est pas encore au Break-Even
        if profit_pips >= RISK_PIPS_BE and pos.get('stopLoss') != open_price:
            print(f"Déplacement au Break-Even pour la position {pos['id']}")
            await connection.modify_position(pos['id'], stop_loss=open_price, take_profit=pos.get('takeProfit'))

async def place_order_block_limit(connection, order_type, price, sl, tp):
    """Place un ordre Limit sur un Order Block ou Breaker Block"""
    print(f"Placement d'un ordre {order_type} Limit à {price} (SL: {sl}, TP: {tp})")
    if order_type == "BUY":
        await connection.create_limit_buy_order(SYMBOL, 0.01, price, stop_loss=sl, take_profit=tp)
    elif order_type == "SELL":
        await connection.create_limit_sell_order(SYMBOL, 0.01, price, stop_loss=sl, take_profit=tp)

async def main():
    api = MetaApi(TOKEN)
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    
    if account.state != 'DEPLOYED':
        await account.deploy()
        
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()

    print("--- Bot connecté et actif ---")
    
    # 1. Vérifier et gérer le Break-Even sur les positions ouvertes
    await apply_break_even(connection)

asyncio.run(main())
