import asyncio
import os
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from metaapi_cloud_sdk import MetaApi

load_dotenv()

# ================= CONFIGURATION =================
SYMBOLS = [
    "Volatility 75 Index",
    "Volatility 100 Index",
    "Step Index",
    "Crash 500 Index"
]

TIMEFRAME = "1h"
DAYS_BACK = 30            # Nombre de jours d'historique à tester

STOCH_K = 5
STOCH_D = 3
STOCH_SLOW = 3

LOT_SIZE = 0.1
COOLDOWN_SECONDS = 3600

SL_POINTS = 300
TP_POINTS = 500
TRAILING_POINTS = 100
TRAILING_STEP = 10

# ================= FONCTIONS =================

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

async def fetch_data(connection, symbol):
    """
    Récupère les données historiques depuis MetaApi.
    """
    start_time = datetime.now() - timedelta(days=DAYS_BACK)
    candles = await connection.get_historical_candles(
        symbol=symbol,
        timeframe=TIMEFRAME,
        start_time=start_time
    )
    df = pd.DataFrame(candles)
    df['time'] = pd.to_datetime(df['time'])
    df = df.sort_values('time').reset_index(drop=True)
    return df

def simulate_trades(df, symbol, tick_size):
    """
    Simule les trades sur les données historiques.
    """
    df = calculate_stochastic(df, STOCH_K, STOCH_D, STOCH_SLOW)
    
    trades = []
    in_position = False
    entry_price = 0
    entry_time = None
    direction = None
    sl_price = 0
    tp_price = 0
    trailing_sl = 0
    trailing_active = False
    highest_price = 0
    lowest_price = 0
    last_trade_time = None
    
    for i in range(len(df)):
        row = df.iloc[i]
        current_time = row['time']
        close = row['close']
        high = row['high']
        low = row['low']
        stoch_k = row['%K']
        stoch_d = row['%D']
        
        if pd.isna(stoch_k) or pd.isna(stoch_d):
            continue
        
        # === Gestion de la position ouverte ===
        if in_position:
            # Mise à jour du trailing stop
            if direction == "BUY":
                if high > highest_price:
                    highest_price = high
                    if not trailing_active and (highest_price - entry_price) >= (TRAILING_POINTS * tick_size):
                        trailing_active = True
                        trailing_sl = highest_price - (TRAILING_POINTS * tick_size)
                    elif trailing_active:
                        new_trailing = highest_price - (TRAILING_POINTS * tick_size)
                        if new_trailing > trailing_sl:
                            trailing_sl = new_trailing
                
                # Vérifier SL
                if low <= (trailing_sl if trailing_active else sl_price):
                    exit_price = trailing_sl if trailing_active else sl_price
                    pnl = (exit_price - entry_price) / tick_size
                    trades.append({
                        'symbol': symbol, 'direction': direction,
                        'entry_time': entry_time, 'exit_time': current_time,
                        'entry_price': entry_price, 'exit_price': exit_price,
                        'pnl_points': pnl, 'result': 'SL' if not trailing_active else 'TRAILING'
                    })
                    in_position = False
                    continue
                
                # Vérifier TP
                if high >= tp_price:
                    pnl = (tp_price - entry_price) / tick_size
                    trades.append({
                        'symbol': symbol, 'direction': direction,
                        'entry_time': entry_time, 'exit_time': current_time,
                        'entry_price': entry_price, 'exit_price': tp_price,
                        'pnl_points': pnl, 'result': 'TP'
                    })
                    in_position = False
                    continue
            
            else:  # SELL
                if low < lowest_price:
                    lowest_price = low
                    if not trailing_active and (entry_price - lowest_price) >= (TRAILING_POINTS * tick_size):
                        trailing_active = True
                        trailing_sl = lowest_price + (TRAILING_POINTS * tick_size)
                    elif trailing_active:
                        new_trailing = lowest_price + (TRAILING_POINTS * tick_size)
                        if new_trailing < trailing_sl:
                            trailing_sl = new_trailing
                
                if high >= (trailing_sl if trailing_active else sl_price):
                    exit_price = trailing_sl if trailing_active else sl_price
                    pnl = (entry_price - exit_price) / tick_size
                    trades.append({
                        'symbol': symbol, 'direction': direction,
                        'entry_time': entry_time, 'exit_time': current_time,
                        'entry_price': entry_price, 'exit_price': exit_price,
                        'pnl_points': pnl, 'result': 'SL' if not trailing_active else 'TRAILING'
                    })
                    in_position = False
                    continue
                
                if low <= tp_price:
                    pnl = (entry_price - tp_price) / tick_size
                    trades.append({
                        'symbol': symbol, 'direction': direction,
                        'entry_time': entry_time, 'exit_time': current_time,
                        'entry_price': entry_price, 'exit_price': tp_price,
                        'pnl_points': pnl, 'result': 'TP'
                    })
                    in_position = False
                    continue
        
        # === Détection d'un nouveau signal ===
        if not in_position:
            # Cooldown
            if last_trade_time is not None:
                if (current_time - last_trade_time).total_seconds() < COOLDOWN_SECONDS:
                    continue
            
            signal = get_signal(stoch_k, stoch_d)
            if signal:
                direction = "BUY" if "BUY" in signal else "SELL"
                entry_price = close
                entry_time = current_time
                last_trade_time = current_time
                
                if direction == "BUY":
                    sl_price = entry_price - (SL_POINTS * tick_size)
                    tp_price = entry_price + (TP_POINTS * tick_size)
                    highest_price = entry_price
                else:
                    sl_price = entry_price + (SL_POINTS * tick_size)
                    tp_price = entry_price - (TP_POINTS * tick_size)
                    lowest_price = entry_price
                
                trailing_active = False
                trailing_sl = 0
                in_position = True
    
    return trades

def print_stats(all_trades):
    """
    Affiche les statistiques de performance globales.
    """
    if not all_trades:
        print("❌ Aucun trade généré sur la période testée.")
        return
    
    df = pd.DataFrame(all_trades)
    
    total_trades = len(df)
    wins = df[df['pnl_points'] > 0]
    losses = df[df['pnl_points'] <= 0]
    win_rate = len(wins) / total_trades * 100
    
    total_pnl = df['pnl_points'].sum()
    avg_win = wins['pnl_points'].mean() if len(wins) > 0 else 0
    avg_loss = losses['pnl_points'].mean() if len(losses) > 0 else 0
    
    print("\n" + "="*60)
    print("📊 RÉSULTATS DU BACKTEST")
    print("="*60)
    print(f"Nombre total de trades : {total_trades}")
    print(f"Trades gagnants        : {len(wins)} ({win_rate:.2f}%)")
    print(f"Trades perdants        : {len(losses)} ({100-win_rate:.2f}%)")
    print(f"P&L total (points)     : {total_pnl:.2f}")
    print(f"Gain moyen (points)    : {avg_win:.2f}")
    print(f"Perte moyenne (points) : {avg_loss:.2f}")
    
    if avg_loss != 0:
        print(f"Ratio Gain/Perte       : {abs(avg_win/avg_loss):.2f}")
    
    print("\n--- Détail par type de sortie ---")
    for result_type in df['result'].unique():
        subset = df[df['result'] == result_type]
        print(f"{result_type} : {len(subset)} trades | P&L: {subset['pnl_points'].sum():.2f} pts")
    
    print("\n--- Détail par symbole ---")
    for symbol in df['symbol'].unique():
        subset = df[df['symbol'] == symbol]
        s_wins = subset[subset['pnl_points'] > 0]
        s_win_rate = len(s_wins) / len(subset) * 100 if len(subset) > 0 else 0
        print(f"{symbol} : {len(subset)} trades | Win Rate: {s_win_rate:.2f}% | P&L: {subset['pnl_points'].sum():.2f} pts")
    
    print("="*60)

# ================= MAIN =================

async def main():
    token = os.getenv("METAAPI_TOKEN")
    account_id = os.getenv("METAAPI_ACCOUNT_ID")
    
    if not token or not account_id:
        print("❌ Variables METAAPI_TOKEN ou METAAPI_ACCOUNT_ID manquantes.")
        return
    
    api = MetaApi(token)
    account = await api.metatrader_account_api.get_account(account_id)
    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()
    
    print(f"✅ Connecté au compte {account_id}")
    print(f"📅 Backtest sur {DAYS_BACK} jours en {TIMEFRAME}")
    
    all_trades = []
    
    for symbol in SYMBOLS:
        print(f"\n🔍 Traitement de {symbol}...")
        try:
            specs = await connection.get_symbol_specification(symbol)
            tick_size = specs.get('tickSize', 0.01)
            
            df = await fetch_data(connection, symbol)
            print(f"   {len(df)} bougies récupérées.")
            
            trades = simulate_trades(df, symbol, tick_size)
            print(f"   {len(trades)} trades simulés.")
            
            all_trades.extend(trades)
        
        except Exception as e:
            print(f"   ❌ Erreur sur {symbol} : {e}")
    
    await connection.close()
    print_stats(all_trades)

if __name__ == "__main__":
    asyncio.run(main())
