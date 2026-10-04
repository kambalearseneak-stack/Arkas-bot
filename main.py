import asyncio
import os
from metaapi_cloud_sdk import MetaApi

# ==========================================
# 1. PARAMÈTRES DE CONNEXION METAAPI
# ==========================================
TOKEN = os.getenv(
    "METAAPI_TOKEN",
    "EyJhbGciOiJSUzUxMiIsInR5cCI6IkpXVCJ9.eyJfaWQiOiJlNzRjNTE2NjhkNjRhZTJkODI2NGE3YTMzZWYxMzE0YyIsImFjY2Vzc1J1bGVzIjpbeyJpZCI6InRyYWRpbmctYWNjb3VudC1tYW5hZ2VtZW50LWFwaSIsIm1ldGhvZHMiOlsidHJhZGluZy1hY2NvdW50LW1hbmFnZW1lbnQtYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiXSwicmVzb3VyY2VzIjpbImFjY291bnQ6JFVTRVJfSUQkOmRlMWMyNjJiLTE3ZDQtNDE1Yy1iYjFmLWM0OWRiMDM0MmQyNSJdfSx7ImlkIjoibWV0YWFwaS1yZXN0LWFwaSIsIm1ldGhvZHMiOlsibWV0YWFwaS1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciIsIndyaXRlciJdLCJyZXNvdXJjZXMiOlsiYWNjb3VudDokVVNFUl9JRCQ6ZGUxYzI2MmItMTdkNC00MTVjLWJiMWYtYzQ5ZGIwMzQyZDI1Il19LHsiaWQiOiJtZXRhYXBpLXJwYy1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOndzOnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIiwid3JpdGVyIl0sInJlc291cmNlcyI6WyJhY2NvdW50OiRVU0VSX0lEJDpkZTFjMjYyYi0xN2Q0LTQxNWMtYmIxZi1jNDlkYjAzNDJkMjUiXX0seyJpZCI6Im1ldGFhcGktcmVhbC10aW1lLXN0cmVhbWluZy1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOndzOnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIiwid3JpdGVyIl0sInJlc291cmNlcyI6WyJhY2NvdW50OiRVU0VSX0lEJDpkZTFjMjYyYi0xN2Q0LTQxNWMtYmIxZi1jNDlkYjAzNDJkMjUiXX0seyJpZCI6Im1ldGFzdGF0cy1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOndzOnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIl0sInJlc291cmNlcyI6WyJhY2NvdW50OiRVU0VSX0lEJDpkZTFjMjYyYi0xN2Q0LTQxNWMtYmIxZi1jNDlkYjAzNDJkMjUiXX0seyJpZCI6InJpc2stbWFuYWdlbWVudC1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOndzOnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIl0sInJlc291cmNlcyI6WyJhY2NvdW50OiRVU0VSX0lEJDpkZTFjMjYyYi0xN2Q0LTQxNWMtYmIxZi1jNDlkYjAzNDJkMjUiXX1dLCJpZ25vcmVSYXRlTGltaXRzIjpmYWxzZSwidG9rZW5JZCI6IjIwMjEwMjEzIiwiaW1wZXJzb25hdGVkIjpmYWxzZSwicmVhbFVzZXJJZCI6ImU3NGM1MTY2OGQ2NGFlMmQ4MjY0YTdhMzNlZjEzMTRjIiwiaWF0IjoxNzkxMTIyNDkyLCJleHAiOjE3OTg4OTg0OTJ9.gW6UZ-jCI-P1BcsU1RbAAFWXq5vhO7X8EoBvUAL5YhPaXC3x5eoFaRkekMYosCFqaG2CUBDF2N2RzwrPziFgE9uZDtQmBGGmPeYBKepQg-TvqVd7Jo3bNkygc4qseVWUr6zGH9IGxJYKWlyD9t4DwCPY07qCcMhzDLnXD4YRtrX97L0WYAy2Nr76q7QiZ3JWlz4Sz-0dRv5bJfnYAamiagC6YDVKXaGAyMFADgcL8vM-mnNcqGt1Fj2DlqRb-MTUf0KrX_CEKTTwVh64HT1EV_WYz80Kq4u72RMSWRjVBKmpmdjn69tvD204-Gxed8UOsEdSH_FBQ_3keVs4KXISSekgHYx0khLZSdjolQcsbKVoT6u-UWIALA83P_odEnJIt4uSWAAeWPjKDVEI1aXLVcymKq0CefMIlD-NYNlE0qLwruAI93zaPbOPsk23GCS7bGg-F_MsG58puU_NFgJs4VRBJXG7N07gZOuAtr5sgtda0nPOvzY3GFY353aeQwW9Xp4bMz1nzYSBOaf3H2wNuiCk0AQtrtIN3EHbAIK0YCuXt7GxGLLxOMKUeOBMYn1ZjcIfefwSj-_nTLmha_dOZVv8h_8rc2un7MwtNvLpCGm87n_efidtzPk5n39BmvqtLRTsCpNBXjawaDv7PAtcVAFxamqb2TYic4IL3duqsWg",
)
ACCOUNT_ID = "de1c262b-17d4-415c-bb1f-c49db0342d25"

# ==========================================
# 2. PARAMÈTRES PERSONNALISABLES DU BOT
# ==========================================
SYMBOL = "EURUSD"            # Actif à trader (ex: EURUSD, GBPUSD, XAUUSD)
TIMEFRAME = "15m"            # Unité de temps (1m, 5m, 15m, 1h, etc.)
LOT_SIZE = 0.01              # Taille du lot
PIPS_TO_BREAK_EVEN = 15      # Nombre de pips en profit pour passer à Break-Even


async def manage_break_even(connection):
    """Vérifie toutes les positions ouvertes et déplace le Stop-Loss au prix d'entrée si le profit cible est atteint."""
    positions = await connection.get_positions()
    for pos in positions:
        if pos.get("symbol") != SYMBOL:
            continue

        open_price = pos["openPrice"]
        current_price = pos["currentPrice"]
        position_type = pos["type"]

        # Calcul du profit en pips
        profit_pips = 0
        if position_type == "POSITION_TYPE_BUY":
            profit_pips = (current_price - open_price) * 10000
        elif position_type == "POSITION_TYPE_SELL":
            profit_pips = (open_price - current_price) * 10000

        # Si le profit est suffisant et le SL n'est pas encore au Break-Even
        if profit_pips >= PIPS_TO_BREAK_EVEN and pos.get("stopLoss") != open_price:
            print(f"[{SYMBOL}] Activation du Break-Even pour la position #{pos['id']}")
            await connection.modify_position(
                pos["id"], stop_loss=open_price, take_profit=pos.get("takeProfit")
            )


async def place_order_block_limit(connection, order_type, entry_price, sl_price, tp_price):
    """Place un ordre Buy Limit ou Sell Limit sur une zone de bloc détectée."""
    print(f"[{SYMBOL}] Placement ordre {order_type} LIMIT @ {entry_price} (SL: {sl_price}, TP: {tp_price})")
    if order_type == "BUY":
        await connection.create_limit_buy_order(
            symbol=SYMBOL,
            volume=LOT_SIZE,
            price=entry_price,
            stop_loss=sl_price,
            take_profit=tp_price,
        )
    elif order_type == "SELL":
        await connection.create_limit_sell_order(
            symbol=SYMBOL,
            volume=LOT_SIZE,
            price=entry_price,
            stop_loss=sl_price,
            take_profit=tp_price,
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

    print(f"--- BOT ACTIF SUR {SYMBOL} ({TIMEFRAME}) | LOT: {LOT_SIZE} ---")

    # Exécution de la gestion du Break-Even sur les ordres en cours
    await manage_break_even(connection)


if __name__ == "__main__":
    asyncio.run(main())
