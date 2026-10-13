import asyncio
import os
import time
from datetime import datetime, timedelta
import pandas as pd
from dotenv import load_dotenv
from metaapi_cloud_sdk import MetaApi

# Charger les variables d'environnement
load_dotenv()

# ================= CONFIGURATION =================
SYMBOLS = [
    "Volatility 75 Index",
    "Volatility 100 Index",
    "Step Index",
    "Crash 500 Index"
]

TIMEFRAME = "1h"
STOCH_K = 5
STOCH_D = 3
STOCH_SLOW = 3
LOT_SIZE = 0.1
COOLDOWN_SECONDS = 3600

SL_POINTS = 300
TP_POINTS = 500
TRAILING_POINTS = 100
TRAILING_STEP = 10

# Variables globales pour le cooldown
last_trade_time = {}

# ================= FONCTIONS TECHNIQUES =================

def calculate_stochastic(df, k_period=5, d_period=3, slow=3):
    """
    Calcule le Stochastic Oscillator en temps réel sur un DataFrame.
    """
    df = df.copy()
    df['high_k'] = df['high'].rolling(window=k_period).max()
    df['low_k'] = df['low'].rolling(window=k_period).min()
    
    denominator = (df['high_k'] - df['low_k']).replace(0, 0.00001)
    
    df['%K'] = 100 * ((df['close'] - df['low_k']) / denominator)
    df['%K'] = df['%K'].rolling(window=slow).mean()
    df['%D'] = df['%K'].rolling(window=d_period).mean()
    
    return df

def get_signal(stoch_k, stoch_d):
    """
    Détermine le signal selon les niveaux de l'indicateur.
    """
    if stoch_k < 10 and stoch_d < 10:
        return "FULL_BUY"
    elif stoch_k < 20 and stoch_d < 20:
        return "BUY"
    elif stoch_k > 90 and stoch_d > 90:
        return "FULL_SELL"
    elif stoch_k > 80 and stoch_d > 80:
        return "SELL"
    return None

def is_cooldown_active(symbol):
    """
    Vérifie si le cooldown est actif pour un symbole donné.
    """
    now = time.time()
    last_time = last_trade_time.get(symbol, 0)
    if now - last_time < COOLDOWN_SECONDS:
        return True, int(COOLDOWN_SECONDS - (now - last_time))
    return False, 0

# ================= GESTION DES ORDRES =================

async def execute_order(connection, symbol, signal, current_price, tick_size):
    """
    Exécute un ordre avec SL, TP et Trailing Stop.
    """
    order_type = "BUY" if "BUY" in signal else "SELL"
    
    if order_type == "BUY":
        sl_price = current_price - (SL_POINTS * tick_size)
        tp_price = current_price + (TP_POINTS * tick_size)
    else:
        sl_price = current_price + (SL_POINTS * tick_size)
        tp_price = current_price - (TP_POINTS * tick_size)
    
    trailing_options = {
        'trailingStopLoss': {
            'distance': TRAILING_POINTS * tick_size,
            'step': TRAILING_STEP * tick_size
        }
    }
    
    try:
        if order_type == "BUY":
            result = await connection.create_market_buy_order(
                symbol=symbol,
                volume=LOT_SIZE,
                stop_loss=sl_price,
                take_profit=tp_price,
                options=trailing_options
            )
        else:
            result = await connection.create_market_sell_order(
                symbol=symbol,
                volume=LOT_SIZE,
                stop_loss=sl_price,
                take_profit=tp_price,
                options=trailing_options
            )
        
        print(f"✅ [{symbol}] Ordre {order_type} exécuté | Prix: {current_price} | SL: {sl_price:.2f} | TP: {tp_price:.2f}")
        return result
    
    except Exception as e:
        print(f"❌ [{symbol}] Erreur lors de l'exécution de l'ordre : {e}")
        return None

# ================= BOUCLE PRINCIPALE =================

async def process_symbol(connection, symbol, symbol_specs):
    """
    Traite un symbole : récupère les données, calcule le signal et exécute.
    """
    try:
        # 1. Récupérer les données récentes (CORRIGÉ : get_candles)
        candles = await connection.get_candles(
            symbol=symbol,
            timeframe=TIMEFRAME,
            start_time=datetime.now() - timedelta(hours=50),
            limit=100
        )
        
        if not candles or len(candles) < 20:
            print(f"⚠️ [{symbol}] Pas assez de données historiques.")
            return
        
        # 2. Convertir en DataFrame
        df = pd.DataFrame(candles)
        
        # 3. Calculer le Stochastic en temps réel
        df = calculate_stochastic(df, STOCH_K, STOCH_D, STOCH_SLOW)
        
        # 4. Prendre la dernière valeur (bougie en cours)
        latest = df.iloc[-1]
        stoch_k = latest['%K']
        stoch_d = latest['%D']
        current_price = latest['close']
        
        if pd.isna(stoch_k) or pd.isna(stoch_d):
            return
        
        print(f"📊 [{symbol}] %K: {stoch_k:.2f} | %D: {stoch_d:.2f} | Prix: {current_price}")
        
        # 5. Détecter le signal
        signal = get_signal(stoch_k, stoch_d)
        
        if signal:
            cooldown, remaining = is_cooldown_active(symbol)
            if cooldown:
                print(f"⏳ [{symbol}] Cooldown actif. Prochain trade dans {remaining}s.")
                return
            
            tick_size = symbol_specs.get(symbol, {}).get('tickSize', 0.01)
            
            result = await execute_order(connection, symbol, signal, current_price, tick_size)
            
            if result:
                last_trade_time[symbol] = time.time()
    
    except Exception as e:
        print(f"⚠️ [{symbol}] Erreur dans le traitement : {e}")

async def run_bot():
    token = os.getenv("METAAPI_TOKEN")
    account_id = os.getenv("METAAPI_ACCOUNT_ID")
    
    if not token or not account_id:
        print("❌ Erreur : Variables METAAPI_TOKEN ou METAAPI_ACCOUNT_ID manquantes.")
        return

    api = MetaApi(token)
    connection = None
    
    try:
        account = await api.metatrader_account_api.get_account(account_id)
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()
        
        print(f"✅ Connecté au compte {account_id}")
        print(f"🚀 Bot démarré pour {len(SYMBOLS)} indices : {', '.join(SYMBOLS)}")
        
        # Récupérer les spécifications de tous les symboles
        symbol_specs = {}
        for symbol in SYMBOLS:
            try:
                specs = await connection.get_symbol_specification(symbol)
                symbol_specs[symbol] = specs
                print(f"📋 [{symbol}] TickSize: {specs.get('tickSize')}")
            except Exception as e:
                print(f"⚠️ Impossible de récupérer les specs de {symbol} : {e}")
        
        # Boucle principale
        while True:
            try:
                tasks = [process_symbol(connection, symbol, symbol_specs) for symbol in SYMBOLS]
                await asyncio.gather(*tasks)
                
                # Attendre 30 secondes avant la prochaine vérification
                await asyncio.sleep(30)
                
            except Exception as e:
                print(f"⚠️ Erreur dans la boucle principale : {e}")
                await asyncio.sleep(60)
    
    except Exception as e:
        print(f"❌ Erreur de connexion : {e}")
    
    finally:
        if connection:
            try:
                await connection.close()
                print("🔌 Connexion fermée.")
            except:
                pass

if __name__ == "__main__":
    asyncio.run(run_bot())
