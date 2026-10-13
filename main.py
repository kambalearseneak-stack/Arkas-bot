import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from metaapi_cloud_sdk import MetaApi

# Charger les variables d'environnement
load_dotenv()

# ================= CONFIGURATION =================
TOKEN = os.getenv("METAAPI_TOKEN")
ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")

SYMBOLS = [
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

# SL/TP/Trailing en POINTS
SL_POINTS = 300
TP_POINTS = 500
TRAILING_POINTS = 100
TRAILING_STEP = 10

# Mode
LIVE_MODE = os.getenv("LIVE_MODE", "true").lower() == "true"

# Variables globales pour le cooldown
last_trade_time = {}

# ================= LOG =================
def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    prefix = {"INFO": "ℹ️", "SIGNAL": "📊", "ORDER": "💼", "WARN": "⚠️",
              "ERROR": "🚨"}.get(level, "•")
    print(f"[{ts}] {prefix} [{level}] {msg}", flush=True)

# ================= INDICATEURS =================
def calculate_stochastic(df, k_period=5, d_period=3, slow=3):
    df = df.copy()
    df['high_k'] = df['high'].rolling(window=k_period).max()
    df['low_k'] = df['low'].rolling(window=k_period).min()
    denominator = (df['high_k'] - df['low_k']).replace(0, 0.00001)
    df['%K'] = 100 * ((df['close'] - df['low_k']) / denominator)
    df['%K'] = df['%K'].rolling(window=slow).mean()
    df['%D'] = df['%K'].rolling(window=d_period).mean()
    return df

def get_signal(stoch_k, stoch_d):
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
    now = time.time()
    last_time = last_trade_time.get(symbol, 0)
    if now - last_time < COOLDOWN_SECONDS:
        return True, int(COOLDOWN_SECONDS - (now - last_time))
    return False, 0

# ================= COMPTE =================
async def get_account_info(rpc):
    try:
        info = await rpc.get_account_information()
        return {
            "balance": float(info.get("balance", 0)),
            "equity": float(info.get("equity", 0)),
            "marginFree": float(info.get("freeMargin", info.get("marginFree", 0))),
        }
    except Exception as e:
        log(f"Erreur get_account_info: {type(e).__name__}: {e}", "ERROR")
        return {"balance": 0, "equity": 0, "marginFree": 0}

# ================= ORDRES =================
async def execute_order(rpc, symbol, signal, current_price, tick_size, symbol_specs):
    order_type = "BUY" if "BUY" in signal else "SELL"
    
    # ✅ CORRECTION : Arrondi basé sur les décimales du symbole
    digits = symbol_specs.get(symbol, {}).get('digits', 2)
    
    if order_type == "BUY":
        sl_price = round(current_price - (SL_POINTS * tick_size), digits)
        tp_price = round(current_price + (TP_POINTS * tick_size), digits)
    else:
        sl_price = round(current_price + (SL_POINTS * tick_size), digits)
        tp_price = round(current_price - (TP_POINTS * tick_size), digits)
    
    trailing_options = {
        'trailingStopLoss': {
            'distance': round(TRAILING_POINTS * tick_size, digits),
            'step': round(TRAILING_STEP * tick_size, digits)
        }
    }
    
    if not LIVE_MODE:
        log(f"[DRY_RUN] {order_type} {LOT_SIZE} lots {symbol} @ {current_price:.2f} | SL={sl_price:.2f} | TP={tp_price:.2f}", "ORDER")
        return "dry-run"
    
    try:
        if order_type == "BUY":
            result = await rpc.create_market_buy_order(
                symbol=symbol,
                volume=LOT_SIZE,
                stop_loss=sl_price,
                take_profit=tp_price,
                options=trailing_options
            )
        else:
            result = await rpc.create_market_sell_order(
                symbol=symbol,
                volume=LOT_SIZE,
                stop_loss=sl_price,
                take_profit=tp_price,
                options=trailing_options
            )
        
        log(f"✅ [{symbol}] {order_type} exécuté @ {current_price:.2f} | SL={sl_price:.2f} | TP={tp_price:.2f}", "ORDER")
        return result
    except Exception as e:
        log(f"❌ [{symbol}] Erreur ordre : {type(e).__name__}: {e}", "ERROR")
        return None

# ================= TRAITEMENT =================
async def process_symbol(account, rpc, symbol, symbol_specs):
    try:
        candles = await account.get_historical_candles(
            symbol=symbol,
            timeframe=TIMEFRAME,
            start_time=None,
            limit=100
        )
        
        if not candles or len(candles) < 20:
            log(f"⚠️ [{symbol}] Pas assez de données.", "WARN")
            return
        
        df = pd.DataFrame([
            {"time": r["time"], "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"]}
            for r in candles
        ])
        df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
        df = df.sort_values("time").reset_index(drop=True)
        
        df = calculate_stochastic(df, STOCH_K, STOCH_D, STOCH_SLOW)
        
        latest = df.iloc[-1]
        stoch_k = latest['%K']
        stoch_d = latest['%D']
        current_price = latest['close']
        
        if pd.isna(stoch_k) or pd.isna(stoch_d):
            return
        
        log(f"📊 [{symbol}] %K: {stoch_k:.2f} | %D: {stoch_d:.2f} | Prix: {current_price:.2f}")
        
        signal = get_signal(stoch_k, stoch_d)
        
        if signal:
            cooldown, remaining = is_cooldown_active(symbol)
            if cooldown:
                log(f"⏳ [{symbol}] Cooldown actif. Prochain trade dans {remaining}s.", "WARN")
                return
            
            tick_size = symbol_specs.get(symbol, {}).get('tickSize', 0.01)
            result = await execute_order(rpc, symbol, signal, current_price, tick_size, symbol_specs)
            
            if result:
                last_trade_time[symbol] = time.time()
    
    except Exception as e:
        log(f"⚠️ [{symbol}] Erreur traitement : {type(e).__name__}: {e}", "ERROR")

# ================= MAIN =================
async def main():
    if not TOKEN or not ACCOUNT_ID:
        raise RuntimeError("METAAPI_TOKEN et METAAPI_ACCOUNT_ID requis.")

    log("=" * 65)
    log("ARKAS STOCHASTIC BOT")
    log(f"Symboles  : {', '.join(SYMBOLS)}")
    log(f"Timeframe : {TIMEFRAME}")
    log(f"Mode      : {'🔴 LIVE' if LIVE_MODE else '🟢 DRY-RUN'}")
    log("=" * 65)

    api = MetaApi(TOKEN)
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    if account.state != "DEPLOYED":
        await account.deploy()
    await account.wait_connected()
    log("✅ Compte MetaApi connecté")

    rpc = account.get_rpc_connection()
    await rpc.connect()
    await rpc.wait_synchronized()
    log("✅ Connexion RPC prête et synchronisée")

    info = await get_account_info(rpc)
    log(f"Balance : {info['balance']:.2f} | Equity : {info['equity']:.2f}")

    symbol_specs = {}
    for symbol in SYMBOLS:
        try:
            specs = await rpc.get_symbol_specification(symbol)
            symbol_specs[symbol] = specs
            log(f"📋 [{symbol}] TickSize: {specs.get('tickSize')} | Digits: {specs.get('digits')}")
        except Exception as e:
            log(f"⚠️ Impossible de récupérer les specs de {symbol} : {e}", "WARN")
            symbol_specs[symbol] = {"tickSize": 0.01, "digits": 2}

    log(f"📡 Surveillance de {len(SYMBOLS)} indices démarrée")
    log("Boucle : vérification toutes les 30 secondes")

    while True:
        try:
            tasks = [process_symbol(account, rpc, symbol, symbol_specs) for symbol in SYMBOLS]
            await asyncio.gather(*tasks)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log(f"Erreur boucle: {type(e).__name__}: {e}", "ERROR")
            await asyncio.sleep(60)

    log("Bot arrêté.")

if __name__ == "__main__":
    asyncio.run(main())
