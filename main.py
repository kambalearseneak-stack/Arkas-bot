import asyncio
import os
import sys
import math
from datetime import datetime

from metaapi_cloud_sdk import MetaApi

# ==============================================================================
# ARKAS STOCHASTIC MTF BOT — STEP INDEX & VOLATILITY 75 INDEX (VERSION CORRIGÉE)
# ==============================================================================

# ==============================================================================
# 1. CONFIGURATION METAAPI
# ==============================================================================

TOKEN = os.getenv("METAAPI_TOKEN")
ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "de1c262b-17d4-415c-bb1f-c49db0342d25")

# ==============================================================================
# 2. MARCHÉS
# ==============================================================================

SYMBOLS = [
    "Step Index",
    "Volatility 75 Index"
]

TIMEFRAME_ANALYSIS = "1h"
TIMEFRAME_ENTRY = "15m"

# ==============================================================================
# 3. PARAMÈTRES STOCHASTIQUE (5, 3, 3)
# ==============================================================================

STOCH_K = 5
STOCH_D = 3
STOCH_SLOWING = 3

LEVEL_BUY = 10
LEVEL_NEUTRAL = 50
LEVEL_SELL = 90

H1_BUY_ZONE = 20
H1_SELL_ZONE = 80

# ==============================================================================
# 4. MONEY MANAGEMENT
# ==============================================================================

RISK_PERCENT = 0.5  # % du balance risqué par trade

VOLUME_LIMITS = {
    "Step Index": {"min": 0.1, "max": 10.0},
    "Volatility 75 Index": {"min": 0.001, "max": 1.0}
}

MAX_POSITIONS_PER_SYMBOL = 1

# SL de secours en points si ATR indisponible
DEFAULT_SL_POINTS = {
    "Step Index": 50.0,
    "Volatility 75 Index": 500.0
}

# Ratio Risque/Récompense initial (TP = 2 × SL)
RISK_REWARD_RATIO = 2.0

# ==============================================================================
# 5. EXECUTION
# ==============================================================================

SCAN_INTERVAL = 15
CANDLES_LIMIT = 300

# ==============================================================================
# 6. UTILS & LOG
# ==============================================================================

def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

def normalize_price(price, digits):
    return round(float(price), int(digits))

def normalize_volume(volume, minimum, maximum, step):
    if step <= 0:
        step = 0.01
    volume = max(minimum, min(volume, maximum))
    steps = math.floor(volume / step)
    volume = steps * step
    return round(volume, 4)

# ==============================================================================
# 7. CALCUL STOCHASTIQUE (ALIGNÉ)
# ==============================================================================

def calculate_stochastic(candles, k_period=5, d_period=3, slowing=3):
    """
    Retourne %K et %D ALIGNÉS sur la même échelle temporelle.
    Les deux listes ont exactement la même longueur.
    """
    if not candles or len(candles) < (k_period + slowing + d_period):
        return None

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]

    # %K rapide (longueur = len - k_period + 1)
    fast_k = []
    for i in range(k_period - 1, len(candles)):
        highest = max(highs[i - k_period + 1:i + 1])
        lowest = min(lows[i - k_period + 1:i + 1])
        if highest == lowest:
            fast_k.append(50.0)
        else:
            fast_k.append(((closes[i] - lowest) / (highest - lowest)) * 100)

    # %K ralenti (slowing)
    slow_k = []
    for i in range(slowing - 1, len(fast_k)):
        slow_k.append(sum(fast_k[i - slowing + 1:i + 1]) / slowing)

    # %D = SMA(d_period) de slow_k
    d_line = []
    for i in range(d_period - 1, len(slow_k)):
        d_line.append(sum(slow_k[i - d_period + 1:i + 1]) / d_period)

    # ALIGNEMENT : on coupe slow_k pour qu'il démarre à la même bougie que d_line
    aligned_k = slow_k[d_period - 1:]

    if len(aligned_k) != len(d_line) or len(aligned_k) == 0:
        return None

    return {
        "k": aligned_k,
        "d": d_line
    }

# ==============================================================================
# 8. ATR (pour Stop Loss dynamique)
# ==============================================================================

def calculate_atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return None

    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        prev_close = float(candles[i - 1]["close"])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    if len(trs) < period:
        return None

    atr = sum(trs[-period:]) / period
    return atr

# ==============================================================================
# 9. RÉCUPÉRATION DES BOUGIES
# ==============================================================================

async def get_candles(account, symbol, timeframe, limit=CANDLES_LIMIT):
    try:
        candles = await account.get_historical_candles(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit
        )
        # On retire la bougie en cours (non clôturée)
        return candles[:-1] if candles and len(candles) > 2 else candles
    except Exception as e:
        log(f"❌ [{symbol}] Erreur bougies {timeframe} : {e}")
        return None

# ==============================================================================
# 10. CALCUL VOLUME BASÉ SUR LE RISQUE
# ==============================================================================

async def calculate_volume(connection, symbol, sl_distance_points, spec):
    """
    Volume = (Balance × RISK%) / (SL_distance × valeur du point par lot)
    """
    try:
        # Récupération du solde
        account_info = await connection.get_account_information()
        balance = float(account_info.get("balance", 0))

        if balance <= 0:
            log(f"⚠️ [{symbol}] Balance invalide, utilisation volume minimum")
            return await _fallback_volume(spec, symbol)

        min_vol = float(spec.get("minVolume", 0.01))
        max_vol = float(spec.get("maxVolume", 100))
        step = float(spec.get("volumeStep", 0.01))

        # Valeur du point (tick value) — fallback prudent
        tick_value = float(spec.get("tickValue", 1.0))
        tick_size = float(spec.get("tickSize", 0.01))
        if tick_size <= 0:
            tick_size = 0.01

        # Nombre de points par unité de prix
        point_value_per_lot = tick_value / tick_size

        risk_amount = balance * (RISK_PERCENT / 100.0)
        sl_value_per_lot = sl_distance_points * point_value_per_lot

        if sl_value_per_lot <= 0:
            return await _fallback_volume(spec, symbol)

        raw_volume = risk_amount / sl_value_per_lot

        # Application des limites spécifiques au symbole
        if symbol in VOLUME_LIMITS:
            min_vol = max(min_vol, VOLUME_LIMITS[symbol]["min"])
            max_vol = min(max_vol, VOLUME_LIMITS[symbol]["max"])

        volume = normalize_volume(raw_volume, min_vol, max_vol, step)
        log(f"💰 [{symbol}] Balance={balance:.2f} | Risk={risk_amount:.2f} | SL={sl_distance_points:.2f} pts | Volume={volume}")
        return volume

    except Exception as e:
        log(f"❌ [{symbol}] Erreur calcul volume : {e}")
        return await _fallback_volume(spec, symbol)

async def _fallback_volume(spec, symbol):
    min_vol = float(spec.get("minVolume", 0.01))
    max_vol = float(spec.get("maxVolume", 100))
    step = float(spec.get("volumeStep", 0.01))
    if symbol in VOLUME_LIMITS:
        min_vol = max(min_vol, VOLUME_LIMITS[symbol]["min"])
        max_vol = min(max_vol, VOLUME_LIMITS[symbol]["max"])
    return normalize_volume(min_vol, min_vol, max_vol, step)

# ==============================================================================
# 11. GESTION DES POSITIONS (BE + TRAILING + TP DYNAMIQUE)
# ==============================================================================

async def manage_open_positions(account, connection, symbol):
    try:
        positions = await connection.get_positions()
        symbol_positions = [p for p in positions if p.get("symbol") == symbol]

        if not symbol_positions:
            return

        candles_m15 = await get_candles(account, symbol, TIMEFRAME_ENTRY)
        stoch_m15 = calculate_stochastic(candles_m15, STOCH_K, STOCH_D, STOCH_SLOWING)

        if not stoch_m15:
            return

        curr_k = stoch_m15["k"][-1]
        curr_d = stoch_m15["d"][-1]

        # ATR M15 pour le trailing
        atr_m15 = calculate_atr(candles_m15, 14)

        spec = await connection.get_symbol_specification(symbol)
        digits = int(spec.get("digits", 2))

        for pos in symbol_positions:
            pos_id = pos["id"]
            pos_type = pos.get("type")
            open_price = float(pos["openPrice"])
            current_sl = float(pos.get("stopLoss", 0) or 0)
            current_tp = pos.get("takeProfit")
            volume = float(pos.get("volume", 0))

            # ---------- TP DYNAMIQUE ----------
            if pos_type == "POSITION_TYPE_BUY" and (curr_k >= LEVEL_SELL or curr_d >= LEVEL_SELL):
                log(f"🎯 [{symbol}] TP dynamique M15 (Stoch >= 90) → Fermeture BUY #{pos_id}")
                await connection.close_position(pos_id)
                continue

            if pos_type == "POSITION_TYPE_SELL" and (curr_k <= LEVEL_BUY or curr_d <= LEVEL_BUY):
                log(f"🎯 [{symbol}] TP dynamique M15 (Stoch <= 10) → Fermeture SELL #{pos_id}")
                await connection.close_position(pos_id)
                continue

            # ---------- BREAK-EVEN + TRAILING ----------
            if pos_type == "POSITION_TYPE_BUY":
                # Break-even dès zone 50
                if curr_k >= LEVEL_NEUTRAL and current_sl < open_price:
                    log(f"🛡️ [{symbol}] BE activé BUY #{pos_id} → SL = {open_price}")
                    await connection.modify_position(pos_id, stop_loss=open_price, take_profit=current_tp)

                # Trailing au-delà du BE
                if atr_m15 and curr_k > LEVEL_NEUTRAL and current_sl >= open_price:
                    new_sl = normalize_price(open_price + (curr_k - LEVEL_NEUTRAL) * atr_m15 / 100.0, digits)
                    if new_sl > current_sl:
                        log(f"📈 [{symbol}] Trailing BUY #{pos_id} → SL = {new_sl}")
                        await connection.modify_position(pos_id, stop_loss=new_sl, take_profit=current_tp)

            elif pos_type == "POSITION_TYPE_SELL":
                if curr_k <= LEVEL_NEUTRAL and (current_sl > open_price or current_sl == 0):
                    log(f"🛡️ [{symbol}] BE activé SELL #{pos_id} → SL = {open_price}")
                    await connection.modify_position(pos_id, stop_loss=open_price, take_profit=current_tp)

                if atr_m15 and curr_k < LEVEL_NEUTRAL and current_sl != 0 and current_sl <= open_price:
                    new_sl = normalize_price(open_price - (LEVEL_NEUTRAL - curr_k) * atr_m15 / 100.0, digits)
                    if new_sl < current_sl:
                        log(f"📉 [{symbol}] Trailing SELL #{pos_id} → SL = {new_sl}")
                        await connection.modify_position(pos_id, stop_loss=new_sl, take_profit=current_tp)

    except Exception as e:
        log(f"❌ [{symbol}] Erreur gestion positions : {e}")

# ==============================================================================
# 12. ANALYSE MULTI-TIMEFRAME ET EXÉCUTION
# ==============================================================================

async def analyze_and_trade(account, connection, symbol):
    try:
        positions = await connection.get_positions()
        active_positions = [p for p in positions if p.get("symbol") == symbol]

        if len(active_positions) >= MAX_POSITIONS_PER_SYMBOL:
            await manage_open_positions(account, connection, symbol)
            return

        # ---------- H1 : filtre zone extrême ----------
        candles_h1 = await get_candles(account, symbol, TIMEFRAME_ANALYSIS)
        stoch_h1 = calculate_stochastic(candles_h1, STOCH_K, STOCH_D, STOCH_SLOWING)
        if not stoch_h1:
            return

        h1_k, h1_d = stoch_h1["k"][-1], stoch_h1["d"][-1]
        h1_buy_setup = h1_k <= H1_BUY_ZONE and h1_d <= H1_BUY_ZONE
        h1_sell_setup = h1_k >= H1_SELL_ZONE and h1_d >= H1_SELL_ZONE

        if not (h1_buy_setup or h1_sell_setup):
            return

        # ---------- M15 : confirmation croisement ----------
        candles_m15 = await get_candles(account, symbol, TIMEFRAME_ENTRY)
        stoch_m15 = calculate_stochastic(candles_m15, STOCH_K, STOCH_D, STOCH_SLOWING)
        if not stoch_m15:
            return

        prev_k, prev_d = stoch_m15["k"][-2], stoch_m15["d"][-2]
        curr_k, curr_d = stoch_m15["k"][-1], stoch_m15["d"][-1]

        buy_confirmed = h1_buy_setup and (prev_k <= prev_d) and (curr_k > curr_d)
        sell_confirmed = h1_sell_setup and (prev_k >= prev_d) and (curr_k < curr_d)

        if not (buy_confirmed or sell_confirmed):
            return

        # ---------- Préparation ordre ----------
        price_info = await connection.get_symbol_price(symbol)
        spec = await connection.get_symbol_specification(symbol)
        digits = int(spec.get("digits", 2))
        bid, ask = float(price_info["bid"]), float(price_info["ask"])

        # SL basé sur ATR M15 (fallback = points par défaut)
        atr_m15 = calculate_atr(candles_m15, 14)
        if atr_m15 and atr_m15 > 0:
            sl_distance = atr_m15 * 2.0
        else:
            sl_distance = DEFAULT_SL_POINTS.get(symbol, 100.0)

        tp_distance = sl_distance * RISK_REWARD_RATIO

        volume = await calculate_volume(connection, symbol, sl_distance, spec)

        if buy_confirmed:
            sl = normalize_price(ask - sl_distance, digits)
            tp = normalize_price(ask + tp_distance, digits)
            log(f"🚀 [{symbol}] ACHAT M15 | Entry={ask} | SL={sl} | TP={tp} | Vol={volume}")
            await connection.create_market_buy_order(
                symbol=symbol, volume=volume, stop_loss=sl, take_profit=tp
            )

        elif sell_confirmed:
            sl = normalize_price(bid + sl_distance, digits)
            tp = normalize_price(bid - tp_distance, digits)
            log(f"🔻 [{symbol}] VENTE M15 | Entry={bid} | SL={sl} | TP={tp} | Vol={volume}")
            await connection.create_market_sell_order(
                symbol=symbol, volume=volume, stop_loss=sl, take_profit=tp
            )

    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse/trading : {e}")

# ==============================================================================
# 13. MAIN
# ==============================================================================

async def main():
    print("==================================================")
    print("🚀 BOT STOCHASTIQUE MTF — VERSION CORRIGÉE")
    print("==================================================")
    print(f"Symboles : {', '.join(SYMBOLS)}")
    print(f"Timeframes : Analyse {TIMEFRAME_ANALYSIS} → Entrée {TIMEFRAME_ENTRY}")
    print(f"Risque par trade : {RISK_PERCENT}% | R/R : 1:{RISK_REWARD_RATIO}")
    print("==================================================")

    if not TOKEN:
        raise RuntimeError("METAAPI_TOKEN manquant.")

    api = MetaApi(TOKEN)

    try:
        account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
        log(f"Compte MetaApi : {account.name} ({account.state})")

        if account.state != "DEPLOYED":
            log("Déploiement du compte...")
            await account.deploy()

        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized({"timeoutInSeconds": 60})
        log("🟢 BOT STOCHASTIQUE CONNECTÉ")

        while True:
            try:
                for symbol in SYMBOLS:
                    await analyze_and_trade(account, connection, symbol)

                await asyncio.sleep(SCAN_INTERVAL)

            except Exception as e:
                log(f"⚠️ Erreur boucle : {e}")
                await asyncio.sleep(10)

    except Exception as e:
        log(f"❌ ERREUR FATALE : {e}")
        raise
    finally:
        try:
            await api.close()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot arrêté.")
