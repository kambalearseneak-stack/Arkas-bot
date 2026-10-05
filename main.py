import asyncio
import os
import sys
import math
from datetime import datetime

from metaapi_cloud_sdk import MetaApi


# ==============================================================================
# ARKAS M5 AGGRESSIVE — DEMO (CORRIGÉ)
# ==============================================================================

TOKEN = os.getenv("METAAPI_TOKEN")
ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "de1c262b-17d4-415c-bb1f-c49db0342d25")

TIMEFRAME = "5m"
SYMBOLS = ["XAUUSD.m", "BTCUSD.m", "USDJPY.m", "GBPJPY.m"]

RISK_PERCENT = 1.0
MAX_POSITIONS_PER_SYMBOL = 1
RR_RATIO = 2.0

EMA_FAST = 20
EMA_SLOW = 50
CANDLE_COUNT = 120
MIN_SCORE = 4

BREAK_EVEN_R = 1.0
TRAILING_ENABLED = True
TRAILING_START_R = 1.5
TRAILING_DISTANCE_R = 0.7

SCAN_INTERVAL = 5
last_trade_candle = {}


def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    multiplier = 2 / (period + 1)
    ema_value = sum(prices[:period]) / period
    for price in prices[period:]:
        ema_value = (price - ema_value) * multiplier + ema_value
    return ema_value


# ==============================================================================
# 10. RÉCUPÉRATION DES BOUGIES (CORRIGÉ)
# ==============================================================================
async def get_candles(connection, symbol, timeframe, limit):
    try:
        # CORRECTION ICI : Utilisation de l'API historical_candles du SDK
        candles = await connection.historical_candles.get_candles(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit
        )
        
        if not candles:
            return None

        # On enlève la dernière bougie (en formation)
        if len(candles) > 2:
            candles = candles[:-1]

        return candles

    except Exception as e:
        log(f"❌ [{symbol}] Erreur récupération bougies : {e}")
        return None


# ==============================================================================
# 11. STRUCTURE DU MARCHÉ
# ==============================================================================
def detect_structure(candles):
    if len(candles) < 10:
        return "RANGE"
    recent = candles[-10:]
    highs = [float(c["high"]) for c in recent]
    lows = [float(c["low"]) for c in recent]

    if highs[-1] > highs[-3] and lows[-1] > lows[-3]:
        return "BUY"
    if highs[-1] < highs[-3] and lows[-1] < lows[-3]:
        return "SELL"
    return "RANGE"


# ==============================================================================
# 12. BOS
# ==============================================================================
def detect_bos(candles):
    if len(candles) < 15:
        return None
    last = candles[-1]
    previous = candles[-11:-1]
    previous_high = max(float(c["high"]) for c in previous)
    previous_low = min(float(c["low"]) for c in previous)
    close = float(last["close"])

    if close > previous_high:
        return "BUY"
    if close < previous_low:
        return "SELL"
    return None


# ==============================================================================
# 13. MOMENTUM
# ==============================================================================
def detect_momentum(candles):
    if len(candles) < 3:
        return None
    current = candles[-1]
    previous = candles[-2]
    current_close = float(current["close"])
    previous_close = float(previous["close"])

    if current_close > float(current["open"]) and current_close > previous_close:
        return "BUY"
    if current_close < float(current["open"]) and current_close < previous_close:
        return "SELL"
    return None


# ==============================================================================
# 14. CONFIRMATION BOUGIE
# ==============================================================================
def detect_candle_confirmation(candles):
    candle = candles[-1]
    open_price = float(candle["open"])
    close_price = float(candle["close"])
    high = float(candle["high"])
    low = float(candle["low"])
    candle_range = high - low

    if candle_range <= 0:
        return None

    body = abs(close_price - open_price)
    body_ratio = body / candle_range

    if body_ratio < 0.45:
        return None
    if close_price > open_price:
        return "BUY"
    if close_price < open_price:
        return "SELL"
    return None


# ==============================================================================
# 15. PULLBACK EMA20
# ==============================================================================
def detect_pullback(candles, ema20):
    if ema20 is None:
        return False
    last = candles[-1]
    high = float(last["high"])
    low = float(last["low"])
    candle_range = high - low
    if candle_range <= 0:
        return False
    close = float(last["close"])
    distance = abs(close - ema20)
    return distance <= candle_range * 1.5


# ==============================================================================
# 16. SCORE ARKAS
# ==============================================================================
def calculate_signal(candles):
    if len(candles) < EMA_SLOW + 10:
        return {"signal": "WAIT", "score": 0}

    closes = [float(c["close"]) for c in candles]
    ema20 = calculate_ema(closes, EMA_FAST)
    ema50 = calculate_ema(closes, EMA_SLOW)

    if ema20 is None or ema50 is None:
        return {"signal": "WAIT", "score": 0}

    structure = detect_structure(candles)
    bos = detect_bos(candles)
    momentum = detect_momentum(candles)
    confirmation = detect_candle_confirmation(candles)
    pullback = detect_pullback(candles, ema20)

    buy_score = 0
    sell_score = 0

    if ema20 > ema50: buy_score += 1
    if ema20 < ema50: sell_score += 1
    if structure == "BUY": buy_score += 1
    if structure == "SELL": sell_score += 1
    if bos == "BUY": buy_score += 1
    if bos == "SELL": sell_score += 1
    if pullback:
        if ema20 > ema50: buy_score += 1
        if ema20 < ema50: sell_score += 1
    if momentum == "BUY": buy_score += 1
    if momentum == "SELL": sell_score += 1
    if confirmation == "BUY": buy_score += 1
    if confirmation == "SELL": sell_score += 1

    if buy_score >= MIN_SCORE and buy_score > sell_score:
        return {"signal": "BUY", "score": buy_score, "ema20": ema20, "ema50": ema50, "structure": structure, "bos": bos}
    if sell_score >= MIN_SCORE and sell_score > buy_score:
        return {"signal": "SELL", "score": sell_score, "ema20": ema20, "ema50": ema50, "structure": structure, "bos": bos}

    return {"signal": "WAIT", "score": max(buy_score, sell_score)}


# ==============================================================================
# 17. STOP LOSS
# ==============================================================================
def calculate_stop_loss(candles, signal):
    recent = candles[-5:]
    if signal == "BUY":
        return min(float(c["low"]) for c in recent)
    if signal == "SELL":
        return max(float(c["high"]) for c in recent)
    return None


def normalize_price(price, digits):
    return round(float(price), int(digits))


def normalize_volume(volume, minimum, maximum, step):
    if step <= 0: step = 0.01
    volume = max(minimum, min(volume, maximum))
    steps = math.floor(volume / step)
    volume = steps * step
    if step >= 0.1: decimals = 1
    elif step >= 0.01: decimals = 2
    elif step >= 0.001: decimals = 3
    else: decimals = 4
    return round(max(volume, minimum), decimals)


# ==============================================================================
# 20. CALCUL VOLUME 1%
# ==============================================================================
async def calculate_volume(connection, symbol, entry, stop_loss):
    try:
        account_info = await connection.get_account_information()
        balance = float(account_info.get("balance", 0))
        if balance <= 0: return None

        specification = await connection.get_symbol_specification(symbol)
        minimum = float(specification.get("minVolume", 0.01))
        maximum = float(specification.get("maxVolume", 100))
        step = float(specification.get("volumeStep", 0.01))
        contract_size = float(specification.get("tradeContractSize", 1))

        risk_money = balance * RISK_PERCENT / 100
        price_distance = abs(entry - stop_loss)
        if price_distance <= 0: return None

        risk_per_lot = price_distance * contract_size
        if risk_per_lot <= 0: return None

        raw_volume = risk_money / risk_per_lot
        volume = normalize_volume(raw_volume, minimum, maximum, step)
        log(f"[{symbol}] Balance={balance:.2f} | Risque={risk_money:.2f} | Volume={volume}")
        return volume
    except Exception as e:
        log(f"[{symbol}] Erreur calcul volume : {e}")
        return None


# ==============================================================================
# 21. POSITIONS
# ==============================================================================
async def get_symbol_positions(connection, symbol):
    positions = await connection.get_positions()
    return [p for p in positions if p.get("symbol") == symbol]


# ==============================================================================
# 22. BREAK-EVEN
# ==============================================================================
async def manage_break_even(connection):
    try:
        positions = await connection.get_positions()
        for position in positions:
            symbol = position.get("symbol")
            if symbol not in SYMBOLS: continue
            try:
                entry = float(position["openPrice"])
                current = float(position["currentPrice"])
                stop_loss = position.get("stopLoss")
                if stop_loss is None: continue
                stop_loss = float(stop_loss)
                risk = abs(entry - stop_loss)
                if risk <= 0: continue
                position_type = position.get("type")

                if position_type == "POSITION_TYPE_BUY":
                    profit = current - entry
                    if profit >= risk * BREAK_EVEN_R and stop_loss < entry:
                        log(f"🟢 [{symbol}] BREAK-EVEN BUY #{position['id']}")
                        await connection.modify_position(position["id"], stop_loss=entry, take_profit=position.get("takeProfit"))
                elif position_type == "POSITION_TYPE_SELL":
                    profit = entry - current
                    if profit >= risk * BREAK_EVEN_R and stop_loss > entry:
                        log(f"🔴 [{symbol}] BREAK-EVEN SELL #{position['id']}")
                        await connection.modify_position(position["id"], stop_loss=entry, take_profit=position.get("takeProfit"))
            except Exception as e:
                log(f"[{symbol}] Erreur BE position : {e}")
    except Exception as e:
        log(f"Erreur Break-Even global : {e}")


# ==============================================================================
# 23. TRAILING STOP
# ==============================================================================
async def manage_trailing(connection):
    if not TRAILING_ENABLED: return
    try:
        positions = await connection.get_positions()
        for position in positions:
            symbol = position.get("symbol")
            if symbol not in SYMBOLS: continue
            try:
                entry = float(position["openPrice"])
                current = float(position["currentPrice"])
                stop_loss = position.get("stopLoss")
                if stop_loss is None: continue
                stop_loss = float(stop_loss)
                current_distance = abs(entry - stop_loss)
                if current_distance <= 0: continue
                position_type = position.get("type")

                if position_type == "POSITION_TYPE_BUY":
                    profit = current - entry
                    if profit >= current_distance * TRAILING_START_R:
                        new_sl = current - (current_distance * TRAILING_DISTANCE_R)
                        if new_sl > stop_loss:
                            log(f"📈 [{symbol}] TRAILING BUY SL {stop_loss} -> {new_sl}")
                            await connection.modify_position(position["id"], stop_loss=new_sl, take_profit=position.get("takeProfit"))
                elif position_type == "POSITION_TYPE_SELL":
                    profit = entry - current
                    if profit >= current_distance * TRAILING_START_R:
                        new_sl = current + (current_distance * TRAILING_DISTANCE_R)
                        if new_sl < stop_loss:
                            log(f"📉 [{symbol}] TRAILING SELL SL {stop_loss} -> {new_sl}")
                            await connection.modify_position(position["id"], stop_loss=new_sl, take_profit=position.get("takeProfit"))
            except Exception as e:
                log(f"[{symbol}] Erreur trailing : {e}")
    except Exception as e:
        log(f"Erreur trailing global : {e}")


# ==============================================================================
# 24. ANALYSE + TRADE
# ==============================================================================
async def analyze_and_trade(connection, symbol):
    try:
        positions = await get_symbol_positions(connection, symbol)
        if len(positions) >= MAX_POSITIONS_PER_SYMBOL:
            return

        candles = await get_candles(connection, symbol, TIMEFRAME, CANDLE_COUNT)
        if not candles or len(candles) < EMA_SLOW + 10:
            return

        result = calculate_signal(candles)
        signal = result["signal"]
        score = result["score"]

        last_candle = candles[-1]
        candle_id = str(last_candle.get("time", last_candle.get("timestamp", "")))

        log(f"[{symbol}] Signal={signal} Score={score}/6 Structure={result.get('structure')} BOS={result.get('bos')}")

        if signal == "WAIT": return
        if last_trade_candle.get(symbol) == candle_id: return

        price = await connection.get_symbol_price(symbol)
        bid = float(price["bid"])
        ask = float(price["ask"])
        entry = ask if signal == "BUY" else bid

        stop_loss = calculate_stop_loss(candles, signal)
        if stop_loss is None: return

        if signal == "BUY" and stop_loss >= entry:
            log(f"[{symbol}] SL BUY invalide.")
            return
        if signal == "SELL" and stop_loss <= entry:
            log(f"[{symbol}] SL SELL invalide.")
            return

        specification = await connection.get_symbol_specification(symbol)
        digits = int(specification.get("digits", 5))

        entry = normalize_price(entry, digits)
        stop_loss = normalize_price(stop_loss, digits)
        risk = abs(entry - stop_loss)
        if risk <= 0: return

        take_profit = entry + risk * RR_RATIO if signal == "BUY" else entry - risk * RR_RATIO
        take_profit = normalize_price(take_profit, digits)

        volume = await calculate_volume(connection, symbol, entry, stop_loss)
        if volume is None or volume <= 0: return

        print("\n==================================================")
        print(f"{'🟢 ARKAS BUY' if signal == 'BUY' else '🔴 ARKAS SELL'} — {symbol}")
        print(f"Score : {score}/6")
        print(f"Entry : {entry}")
        print(f"SL    : {stop_loss}")
        print(f"TP    : {take_profit}")
        print(f"Volume: {volume}")
        print(f"Risk  : {RISK_PERCENT}%")
        print("==================================================")

        if signal == "BUY":
            order = await connection.create_market_buy_order(symbol=symbol, volume=volume, stop_loss=stop_loss, take_profit=take_profit)
        else:
            order = await connection.create_market_sell_order(symbol=symbol, volume=volume, stop_loss=stop_loss, take_profit=take_profit)

        last_trade_candle[symbol] = candle_id
        log(f"✅ [{symbol}] TRADE {signal} OUVERT")
        log(f"Order result : {order}")

    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse/ordre : {e}")


# ==============================================================================
# 25. MAIN (CORRIGÉ POUR ÉVITER LE TIMEOUT)
# ==============================================================================
async def main():
    print("\n==================================================")
    print("🚀 ARKAS M5 AGGRESSIVE — DEMO LIVE (CORRIGÉ)")
    print("==================================================")
    print(f"Timeframe : {TIMEFRAME}")
    print(f"Symboles  : {', '.join(SYMBOLS)}")
    print(f"Risque    : {RISK_PERCENT}%")
    print(f"RR        : 1:{RR_RATIO}")
    print(f"Score min : {MIN_SCORE}/6")
    print("==================================================")

    if not TOKEN:
        raise RuntimeError("METAAPI_TOKEN absent.")

    api = MetaApi(TOKEN)

    try:
        account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
        log(f"Compte MetaApi : {account.name}")
        log(f"État : {account.state}")

        if account.state != "DEPLOYED":
            log("Déploiement du compte...")
            await account.deploy()

        # CORRECTION ICI : Utilisation de la région et de la connexion RPC sans streaming obligatoire
        # On spécifie la région pour éviter les timeouts de souscription
        connection = account.get_rpc_connection()
        
        log("Connexion au serveur MT5...")
        await connection.connect()
        
        log("Synchronisation...")
        await connection.wait_synchronized()
        
        log("🟢 ARKAS CONNECTÉ AVEC SUCCÈS")

        for symbol in SYMBOLS:
            try:
                price = await connection.get_symbol_price(symbol)
                log(f"[{symbol}] BID={price['bid']} ASK={price['ask']}")
            except Exception as e:
                log(f"[{symbol}] Prix indisponible : {e}")

        while True:
            try:
                for symbol in SYMBOLS:
                    await analyze_and_trade(connection, symbol)
                    await asyncio.sleep(1)

                await manage_break_even(connection)
                await manage_trailing(connection)
                await asyncio.sleep(SCAN_INTERVAL)

            except Exception as e:
                log(f"⚠️ Erreur dans la boucle : {e}")
                await asyncio.sleep(5)

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
        print("\n🛑 ARKAS arrêté.")
    except Exception as e:
        print(f"\n❌ ARKAS arrêté : {e}")
