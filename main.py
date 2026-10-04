import asyncio
import os
import sys
from datetime import datetime
from metaapi_cloud_sdk import MetaApi

# ==============================================================================
# "ARKAS M5 AGGRESSIVE" - BOT TRADING METATRADER VIA METAAPI
# ==============================================================================

# 1. CONFIGURATION
TOKEN = "eyJhbGciOiJSUzUxMiIsInR5cCI6IkpXVCJ9.eyJfaWQiOiJlNzRjNTE2NjhkNjRhZTJkODI2NGE3YTMzZWYxMzE0YyIsImFjY2Vzc1J1bGVzIjpbeyJpZCI6InRyYWRpbmctYWNjb3VudC1tYW5hZ2VtZW50LWFwaSIsIm1ldGhvZHMiOlsidHJhZGluZy1hY2NvdW50LW1hbmFnZW1lbnQtYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcmVzdC1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcnBjLWFwaSIsIm1ldGhvZHMiOlsibWV0YWFwaS1hcGk6d3M6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcmVhbC10aW1lLXN0cmVhbWluZy1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOndzOnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIiwid3JpdGVyIl0sInJlc291cmNlcyI6WyIqOiRVU0VSX0lEJDoqIl19LHsiaWQiOiJtZXRhc3RhdHMtYXBpIiwibWV0aG9kcyI6WyJtZXRhc3RhdHMtYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6InJpc2stbWFuYWdlbWVudC1hcGkiLCJtZXRob2RzIjpbInJpc2stbWFuYWdlbWVudC1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciIsIndyaXRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfSx7ImlkIjoiY29weWZhY3RvcnktYXBpIiwibWV0aG9kcyI6WyJjb3B5ZmFjdG9yeS1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciIsIndyaXRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfSx7ImlkIjoibXQtbWFuYWdlci1hcGkiLCJtZXRob2RzIjpbIm10LW1hbmFnZXItYXBpOnJlc3Q6ZGVhbGluZzoqOioiLCJtdC1tYW5hZ2VyLWFwaTpyZXN0OnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIiwid3JpdGVyIl0sInJlc291cmNlcyI6WyIqOiRVU0VSX0lEJDoqIl19LHsiaWQiOiJiaWxsaW5nLWFwaSIsIm1ldGhvZHMiOlsiYmlsbGluZy1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfV0sImlnbm9yZVJhdGVMaW1pdHMiOmZhbHNlLCJ0b2tlbklkIjoiMjAyMTAyMTMiLCJpbXBlcnNvbmF0ZWQiOmZhbHNlLCJyZWFsVXNlcklkIjoiZTc0YzUxNjY4ZDY0YWUyZDgyNjRhN2EzM2VmMTMxNGMiLCJpYXQiOjE3OTExNTAwMjEsImV4cCI6MTc5ODkyNjAyMX0.L94eqhJdrr35hhxf3vfQgRE1fp1wsXR8ooNEbtnE1i23k039pV5GkKmpkPUykMPgkW_WhcZAjOOHQzduPEkCPm6n6AFc7R_qt1sRapHwZez42OtXJAADshHr0qjcB_al1N4EVnln2_ZFRwI61W4PyHoVmg3I23AVs4e7PhOYDb-pFfp6Wlc3YFBoDis307qiYp8HY060rqKwYOh4mtIofK98DEfaluLV_EzsHQNbbJr0NaIXTWfzSi234TY7fA8haEDyj0UM9lAEC8MDKqK2daLbxIoOINU-Nq5P7HJ8UkEu12Nhewh1gcKSOeA3xsYwoP6ueyyceOVp5-kWH5ClcznoKMSzIjoA5iYHQDO08O-B9KY_PGAo6qaogJPeeiNVAe6HLV0uvKaWRJ8aGEjcry06qNToeWoOm1tQpZWu_vrrAWg10OLldawI28R2x7Kuwcz9MrF7oOSH3Y0CZSbFTEjhYG9w7MAfcmGfG4SLZjpEzJbHgjsKSCZnUSakEhIVFxoA4of7SjcqjU8GkgMK0IUfqmkjYS4Hio5pU_8yml70OV4hqXpUOSt4LgZgKOx6h_KG4P8K2JrRZXFKE0iUjflojP5DdQPJ4_ZuUee0wvrC5rzx3KX6bMjKZIT7F6HHTA-Epo9LBDCeJKipUKpPeDiPWyO2BOERai6RVGdl5SU"  # Remplacez par votre token généré
ACCOUNT_ID = "de1c262b-17d4-415c-bb1f-c49db0342d25"

TIMEFRAME = "5m"
SYMBOLS = [
    "XAUUSD.m",
    "BTCUSD.m",
    "USDJPY.m",
    "GBPJPY.m"
]

# 2. GESTION DU RISQUE ET DE LA POSITION
RISK_PERCENT = 1.0
MAX_POSITIONS_PER_SYMBOL = 1

# Paramètres de stratégie
EMA_FAST = 20
EMA_SLOW = 50
CANDLE_COUNT = 100

# 3. FONCTIONS UTILITAIRES DE CALCUL TECHNIQUE

def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for price in prices[period:]:
        ema.append((price - ema[-1]) * multiplier + ema[-1])
    return ema[-1]

async def get_candles(connection, symbol, timeframe, limit=100):
    """
    Récupère l'historique des chandelles via la méthode RPC MetaApi appropriée.
    """
    try:
        candles = await connection.get_candle_price_history(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit
        )
        return candles
    except Exception as e:
        print(f"[{datetime.now()}] ❌ [{symbol}] Erreur lors de la récupération des bougies : {e}")
        return None

# 4. ANALYSE DU MARCHÉ ET DÉCISION

async def analyze_and_trade(connection, symbol):
    try:
        # Récupération des positions ouvertes sur ce symbole
        positions = await connection.get_positions()
        symbol_positions = [p for p in positions if p.get('symbol') == symbol]

        if len(symbol_positions) >= MAX_POSITIONS_PER_SYMBOL:
            print(f"[{datetime.now()}] ℹ️ [{symbol}] Position déjà ouverte ({len(symbol_positions)}/{MAX_POSITIONS_PER_SYMBOL}). On saute.")
            return

        candles = await get_candles(connection, symbol, TIMEFRAME, CANDLE_COUNT)
        if not candles or len(candles) < EMA_SLOW:
            print(f"[{datetime.now()}] ⚠️ [{symbol}] Pas assez de bougies récupérées pour l'analyse.")
            return

        close_prices = [c['close'] for c in candles]

        ema_20 = calculate_ema(close_prices, EMA_FAST)
        ema_50 = calculate_ema(close_prices, EMA_SLOW)

        if not ema_20 or not ema_50:
            return

        last_candle = candles[-1]
        prev_candle = candles[-2]

        print(f"[{datetime.now()}] 📊 [{symbol}] Close: {last_candle['close']} | EMA20: {round(ema_20, 5)} | EMA50: {round(ema_50, 5)}")

        # Condition Achat : Croisement Hausser EMA 20 > EMA 50
        if ema_20 > ema_50 and prev_candle['close'] <= calculate_ema(close_prices[:-1], EMA_FAST):
            print(f"[{datetime.now()}] 🟢 [{symbol}] Signal ACHAT (Buy) détecté !")
            # Logique de passage d'ordre ACHAT ici

        # Condition Vente : Croisement Baisse EMA 20 < EMA 50
        elif ema_20 < ema_50 and prev_candle['close'] >= calculate_ema(close_prices[:-1], EMA_FAST):
            print(f"[{datetime.now()}] 🔴 [{symbol}] Signal VENTE (Sell) détecté !")
            # Logique de passage d'ordre VENTE ici

    except Exception as e:
        print(f"[{datetime.now()}] ❌ [{symbol}] Erreur analyse/ordre : {e}")

# 5. BOUCLE PRINCIPALE D'EXÉCUTION

async def main():
    print("==================================================")
    print("🚀 ARKAS M5 AGGRESSIVE - RUNNING ONLINE")
    print("==================================================")

    api = MetaApi(TOKEN)

    try:
        account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
        initial_state = account.state
        print(f"[{datetime.now()}] État du compte MetaApi : {initial_state}")

        if initial_state != 'DEPLOYED':
            print(f"[{datetime.now()}] Déploiement du compte en cours...")
            await account.deploy()

        print(f"[{datetime.now()}] Connexion au terminal MetaTrader via RPC...")
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()
        print(f"[{datetime.now()}] Connecté avec succès au serveur MetaApi !")

        while True:
            for symbol in SYMBOLS:
                await analyze_and_trade(connection, symbol)
                await asyncio.sleep(2)  # Pause légère entre chaque symbole
            
            # Pause de 15 secondes avant le prochain scan
            await asyncio.sleep(15)

    except Exception as e:
        print(f"[{datetime.now()}] ❌ ERREUR FATALE : {e}")
    finally:
        sys.exit(0)

if __name__ == "__main__":
    asyncio.run(main())
