import asyncio
import os
import math
from datetime import datetime

from metaapi_cloud_sdk import MetaApi


# ============================================================
# ARKAS M5 AGGRESSIVE — DEMO LIVE
# ============================================================
#
# Fonctionnement :
#   M5 -> analyse -> BUY/SELL -> entrée automatique
#   -> SL -> TP -> Break-Even -> Trailing
#
# IMPORTANT :
# Ce programme est prévu pour un COMPTE DEMO.
# ============================================================


# ============================================================
# 1. CONFIGURATION
# ============================================================

TOKEN = "eyJhbGciOiJSUzUxMiIsInR5cCI6IkpXVCJ9.eyJfaWQiOiJlNzRjNTE2NjhkNjRhZTJkODI2NGE3YTMzZWYxMzE0YyIsImFjY2Vzc1J1bGVzIjpbeyJpZCI6InRyYWRpbmctYWNjb3VudC1tYW5hZ2VtZW50LWFwaSIsIm1ldGhvZHMiOlsidHJhZGluZy1hY2NvdW50LW1hbmFnZW1lbnQtYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcmVzdC1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcnBjLWFwaSIsIm1ldGhvZHMiOlsibWV0YWFwaS1hcGk6d3M6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6Im1ldGFhcGktcmVhbC10aW1lLXN0cmVhbWluZy1hcGkiLCJtZXRob2RzIjpbIm1ldGFhcGktYXBpOndzOnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIiwid3JpdGVyIl0sInJlc291cmNlcyI6WyIqOiRVU0VSX0lEJDoqIl19LHsiaWQiOiJtZXRhc3RhdHMtYXBpIiwibWV0aG9kcyI6WyJtZXRhc3RhdHMtYXBpOnJlc3Q6cHVibGljOio6KiJdLCJyb2xlcyI6WyJyZWFkZXIiLCJ3cml0ZXIiXSwicmVzb3VyY2VzIjpbIio6JFVTRVJfSUQkOioiXX0seyJpZCI6InJpc2stbWFuYWdlbWVudC1hcGkiLCJtZXRob2RzIjpbInJpc2stbWFuYWdlbWVudC1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciIsIndyaXRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfSx7ImlkIjoibXQtbWFuYWdlci1hcGkiLCJtZXRob2RzIjpbIm10LW1hbmFnZXItYXBpOnJlc3Q6ZGVhbGluZzoqOioiLCJtdC1tYW5hZ2VtZW50LWFwaSByZXN0OnB1YmxpYzoqOioiXSwicm9sZXMiOlsicmVhZGVyIiwid3JpdGVyIl0sInJlc291cmNlcyI6WyIqOiRVU0VSX0lEJDoqIl19LHsiaWQiOiJiaWxsaW5nLWFwaSIsIm1ldGhvZHMiOlsiYmlsbGluZy1hcGk6cmVzdDpwdWJsaWM6KjoqIl0sInJvbGVzIjpbInJlYWRlciJdLCJyZXNvdXJjZXMiOlsiKjokVVNFUl9JRCQ6KiJdfV0sImlnbm9yZVJhdGVMaW1pdHMiOmZhbHNlLCJ0b2tlbklkIjoiMjAyMTAyMTMiLCJpbXBlcnNvbmF0ZWQiOmZhbHNlLCJyZWFsVXNlcklkIjoiZTc0YzUxNjY4ZDY0YWUyZDgyNjRhN2EzM2VmMTMxNGMiLCJpYXQiOjE3OTExMjkxMDcsImV4cCI6MTc5ODkwNTEwN30.EMaLTzAsRxC9EhEwx4Wf7DuJdzy0FBWq0k63NRo9wMk9tOH7ESFPGxhGL3M6YVJZl5dJRcLvtMixs6-lwsnAKlXiAmyEFbdVV4VM16pP2thW4ij3GJHoqXtSh5MQOyAf1rMa6LRWCk-krGT6pJduZYk8lJbEhaCIgnLWh1G5-TsSg0EfKkjoygbvvmie2pybvYw3O7dWrxRmNzSpS6nVmdTANjwWARwkh0I4rCqUe1Af-AUI0sEYwk_k6clhY_wyj22JtiRVAxrzQfqTNPVjp82XKHZyIFXpwl5P4Lpr38PNaifLNbRExyY3F7BF9XMbdGJZXvLs3E-JM2sx0Qb9fyRedSpwRQvgOZPfC8JBL7dFYlpP9yS-Cp03bg29wFyLO-ApEKXxk3SHcLM_AqooUP8UlLUwyQF7DbMUd-t2_3LR19WJEsT--MVv9HHm4U4LlGv9Z-M3eseAPKQ3NaCXkaZjefSXNB9M2R8gAITo54IEjpyR8ISfDB_qo8M5TtuQb0Vl5fgomURwPT_T5gra8ac1E4AEFtNameAuCrzeWsOWw5fZCIDc2YKAYmiM6hZdk5v_ez-v_hAdvEYFmAHqDgejm0F9HnnoAyt6huvHOYJmD634ltEysjpFBTYwCkikqzZ_0bNcxxiNmahDnL8kDueTxGRV5epI8wlkY7uYJaw"
ACCOUNT_ID = "de1c262b-17d4-415c-bb1f-c49db0342d25"

TIMEFRAME = "5m"

SYMBOLS = [
    "XAUUSD.m",
    "BTCUSD.m",
    "USDJPY.m",
    "GBPJPY.m"
]


# ============================================================
# 2. MONEY MANAGEMENT
# ============================================================

RISK_PERCENT = 1.0

# Une seule position maximum par symbole
MAX_POSITIONS_PER_SYMBOL = 1

# Score minimum pour entrer
MIN_SCORE = 4


# ============================================================
# 3. STRATÉGIE
# ============================================================

EMA_FAST = 20
EMA_SLOW = 50

CANDLE_COUNT = 150

# TP
TP1_R = 1.0
TP2_R = 2.0
TP3_R = 3.0

# Break-even
BREAK_EVEN_R = 1.0

# Trailing
TRAILING_ENABLED = True
TRAILING_START_R = 1.5
TRAILING_DISTANCE_R = 0.7

# Nombre de secondes entre deux scans
SCAN_INTERVAL = 5


# ============================================================
# 4. PROTECTION
# ============================================================

# Spread maximum approximatif en multiples de la taille de bougie récente.
MAX_SPREAD_RANGE_RATIO = 0.25

# Empêche plusieurs entrées sur exactement la même bougie
last_trade_candle = {}


# ============================================================
# 5. LOG
# ============================================================

def log(message):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


# ============================================================
# 6. EMA
# ============================================================

def calculate_ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    ema_value = sum(values[:period]) / period

    for value in values[period:]:
        ema_value = (value - ema_value) * multiplier + ema_value

    return ema_value


# ============================================================
# 7. BOUGIES
# ============================================================

def candle_body(candle):
    return abs(float(candle["close"]) - float(candle["open"]))


def candle_range(candle):
    return float(candle["high"]) - float(candle["low"])


def bullish(candle):
    return float(candle["close"]) > float(candle["open"])


def bearish(candle):
    return float(candle["close"]) < float(candle["open"])


# ============================================================
# 8. STRUCTURE
# ============================================================

def detect_structure(candles):
    if len(candles) < 12:
        return "RANGE"

    recent = candles[-10:]
    highs = [float(c["high"]) for c in recent]
    lows = [float(c["low"]) for c in recent]

    # Structure haussière
    if highs[-1] > highs[-3] and lows[-1] > lows[-3]:
        return "BUY"

    # Structure baissière
    if highs[-1] < highs[-3] and lows[-1] < lows[-3]:
        return "SELL"

    return "RANGE"


# ============================================================
# 9. BOS
# ============================================================

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


# ============================================================
# 10. MOMENTUM
# ============================================================

def detect_momentum(candles):
    if len(candles) < 5:
        return None

    c1 = candles[-1]
    c2 = candles[-2]

    if bullish(c1) and float(c1["close"]) > float(c2["close"]):
        return "BUY"

    if bearish(c1) and float(c1["close"]) < float(c2["close"]):
        return "SELL"

    return None


# ============================================================
# 11. CONFIRMATION BOUGIE
# ============================================================

def candle_confirmation(candles):
    last = candles[-1]
    rng = candle_range(last)
    body = candle_body(last)

    if rng <= 0:
        return None

    body_ratio = body / rng

    # Bougie trop faible
    if body_ratio < 0.45:
        return None

    if bullish(last):
        return "BUY"

    if bearish(last):
        return "SELL"

    return None


# ============================================================
# 12. PULLBACK EMA20
# ============================================================

def detect_pullback(candles):
    if len(candles) < EMA_FAST + 5:
        return False

    closes = [float(c["close"]) for c in candles]
    ema20 = calculate_ema(closes, EMA_FAST)

    if ema20 is None:
        return False

    last = candles[-1]
    distance = abs(float(last["close"]) - ema20)
    rng = candle_range(last)

    if rng <= 0:
        return False

    # Zone agressive autour EMA20
    return distance <= rng * 1.5


# ============================================================
# 13. SIGNAL ARKAS
# ============================================================

def calculate_signal(candles):
    if len(candles) < EMA_SLOW + 10:
        return {"signal": "WAIT", "score": 0}

    closes = [float(c["close"]) for c in candles]
    ema20 = calculate_ema(closes, EMA_FAST)
    ema50 = calculate_ema(closes, EMA_SLOW)

    structure = detect_structure(candles)
    bos = detect_bos(candles)
    momentum = detect_momentum(candles)
    confirmation = candle_confirmation(candles)
    pullback = detect_pullback(candles)

    buy_score = 0
    sell_score = 0

    # EMA
    if ema20 > ema50:
        buy_score += 1
    if ema20 < ema50:
        sell_score += 1

    # Structure
    if structure == "BUY":
        buy_score += 1
    if structure == "SELL":
        sell_score += 1

    # BOS
    if bos == "BUY":
        buy_score += 1
    if bos == "SELL":
        sell_score += 1

    # Pullback
    if pullback and ema20 > ema50:
        buy_score += 1
    if pullback and ema20 < ema50:
        sell_score += 1

    # Momentum
    if momentum == "BUY":
        buy_score += 1
    if momentum == "SELL":
        sell_score += 1

    # Confirmation
    if confirmation == "BUY":
        buy_score += 1
    if confirmation == "SELL":
        sell_score += 1

    if buy_score >= MIN_SCORE and buy_score > sell_score:
        return {
            "signal": "BUY",
            "score": buy_score,
            "ema20": ema20,
            "ema50": ema50,
            "structure": structure,
            "bos": bos,
            "momentum": momentum,
            "confirmation": confirmation,
            "pullback": pullback,
        }

    if sell_score >= MIN_SCORE and sell_score > buy_score:
        return {
            "signal": "SELL",
            "score": sell_score,
            "ema20": ema20,
            "ema50": ema50,
            "structure": structure,
            "bos": bos,
            "momentum": momentum,
            "confirmation": confirmation,
            "pullback": pullback,
        }

    return {
        "signal": "WAIT",
        "score": max(buy_score, sell_score),
        "ema20": ema20,
        "ema50": ema50,
        "structure": structure,
        "bos": bos,
        "momentum": momentum,
        "confirmation": confirmation,
        "pullback": pullback,
    }


# ============================================================
# 14. SWING SL
# ============================================================

def calculate_stop_loss(candles, signal):
    recent = candles[-10:]

    if signal == "BUY":
        return min(float(c["low"]) for c in recent)

    if signal == "SELL":
        return max(float(c["high"]) for c in recent)

    return None


# ============================================================
# 15. ARRONDI PRIX
# ============================================================

def normalize_price(price, digits):
    return round(float(price), int(digits))


# ============================================================
# 16. ARRONDI VOLUME
# ============================================================

def normalize_volume(volume, minimum, maximum, step):
    if step <= 0:
        step = 0.01

    volume = max(minimum, min(volume, maximum))
    steps = math.floor(volume / step)
    volume = steps * step

    decimals = 2
    if step < 0.01:
        decimals = 4

    return round(max(volume, minimum), decimals)


# ============================================================
# 17. CALCUL LOT
# ============================================================

async def calculate_risk_volume(connection, symbol, entry, stop_loss):
    try:
        account = await connection.get_account_information()
        balance = float(account.get("balance", 0))

        if balance <= 0:
            return None

        specification = await connection.get_symbol_specification(symbol)
        minimum = float(specification.get("minVolume", 0.01))
        maximum = float(specification.get("maxVolume", 100))
        step = float(specification.get("volumeStep", 0.01))
        contract_size = float(specification.get("tradeContractSize", 1))

        risk_money = balance * RISK_PERCENT / 100
        distance = abs(float(entry) - float(stop_loss))

        if distance <= 0:
            return None

        # Estimation du risque par lot
        risk_per_lot = distance * contract_size

        if risk_per_lot <= 0:
            return None

        volume = risk_money / risk_per_lot
        volume = normalize_volume(volume, minimum, maximum, step)

        log(
            f"[{symbol}] Balance={balance:.2f} | "
            f"Risque={risk_money:.2f} | "
            f"Volume={volume}"
        )

        return volume

    except Exception as e:
        log(f"[{symbol}] Erreur calcul lot: {e}")
        return None


# ============================================================
# 18. POSITIONS SYMBOL
# ============================================================

async def get_symbol_positions(connection, symbol):
    positions = await connection.get_positions()
    return [p for p in positions if p.get("symbol") == symbol]


# ============================================================
# 19. BREAK-EVEN
# ============================================================

async def manage_break_even(connection):
    positions = await connection.get_positions()

    for pos in positions:
        symbol = pos.get("symbol")

        if symbol not in SYMBOLS:
            continue

        try:
            entry = float(pos["openPrice"])
            current = float(pos["currentPrice"])
            sl = pos.get("stopLoss")

            if sl is None:
                continue

            sl = float(sl)
            original_risk = abs(entry - sl)

            if original_risk <= 0:
                continue

            position_type = pos.get("type")

            # BUY
            if position_type == "POSITION_TYPE_BUY":
                profit = current - entry
                if profit >= original_risk * BREAK_EVEN_R and sl < entry:
                    log(f"[{symbol}] BREAK-EVEN BUY #{pos['id']}")
                    await connection.modify_position(
                        pos["id"], stop_loss=entry, take_profit=pos.get("takeProfit")
                    )

            # SELL
            elif position_type == "POSITION_TYPE_SELL":
                profit = entry - current
                if profit >= original_risk * BREAK_EVEN_R and sl > entry:
                    log(f"[{symbol}] BREAK-EVEN SELL #{pos['id']}")
                    await connection.modify_position(
                        pos["id"], stop_loss=entry, take_profit=pos.get("takeProfit")
                    )

        except Exception as e:
            log(f"[{symbol}] Erreur Break-Even: {e}")


# ============================================================
# 20. TRAILING
# ============================================================

async def manage_trailing(connection):
    if not TRAILING_ENABLED:
        return

    positions = await connection.get_positions()

    for pos in positions:
        symbol = pos.get("symbol")

        if symbol not in SYMBOLS:
            continue

        try:
            entry = float(pos["openPrice"])
            current = float(pos["currentPrice"])
            sl = pos.get("stopLoss")

            if sl is None:
                continue

            sl = float(sl)
            risk = abs(entry - sl)

            if risk <= 0:
                continue

            position_type = pos.get("type")

            # BUY
            if position_type == "POSITION_TYPE_BUY":
                profit = current - entry
                if profit >= risk * TRAILING_START_R:
                    new_sl = current - risk * TRAILING_DISTANCE_R
                    if new_sl > sl:
                        log(f"[{symbol}] TRAILING BUY SL={new_sl}")
                        await connection.modify_position(
                            pos["id"],
                            stop_loss=new_sl,
                            take_profit=pos.get("takeProfit"),
                        )

            # SELL
            elif position_type == "POSITION_TYPE_SELL":
                profit = entry - current
                if profit >= risk * TRAILING_START_R:
                    new_sl = current + risk * TRAILING_DISTANCE_R
                    if new_sl < sl:
                        log(f"[{symbol}] TRAILING SELL SL={new_sl}")
                        await connection.modify_position(
                            pos["id"],
                            stop_loss=new_sl,
                            take_profit=pos.get("takeProfit"),
                        )

        except Exception as e:
            log(f"[{symbol}] Erreur trailing: {e}")


# ============================================================
# 21. ANALYSE SYMBOL
# ============================================================

async def analyze_symbol(connection, symbol):
    try:
        candles = await connection.get_historical_candles(
            symbol, TIMEFRAME, None, CANDLE_COUNT
        )

        if not candles:
            log(f"[{symbol}] Aucune bougie.")
            return

        # Dernière bougie potentiellement ouverte
        candles = candles[:-1]

        if len(candles) < EMA_SLOW + 10:
            return

        result = calculate_signal(candles)
        signal = result["signal"]
        score = result["score"]

        last_candle = candles[-1]
        candle_time = str(
            last_candle.get("time", last_candle.get("timestamp", ""))
        )

        log(
            f"[{symbol}] SIGNAL={signal} SCORE={score}/6 "
            f"STRUCTURE={result.get('structure')} BOS={result.get('bos')}"
        )

        if signal == "WAIT":
            return

        # ----------------------------------------------------
        # Empêcher répétition sur même bougie
        # ----------------------------------------------------
        if last_trade_candle.get(symbol) == candle_time:
            return

        # ----------------------------------------------------
        # Position existante
        # ----------------------------------------------------
        positions = await get_symbol_positions(connection, symbol)
        if len(positions) >= MAX_POSITIONS_PER_SYMBOL:
            log(f"[{symbol}] Position déjà ouverte.")
            return

        # ----------------------------------------------------
        # PRIX
        # ----------------------------------------------------
        tick = await connection.get_symbol_price(symbol)
        bid = float(tick["bid"])
        ask = float(tick["ask"])

        if signal == "BUY":
            entry = ask
        else:
            entry = bid

        # ----------------------------------------------------
        # SPREAD
        # ----------------------------------------------------
        spread = ask - bid
        recent_range = candle_range(candles[-1])

        if recent_range > 0 and spread > recent_range * MAX_SPREAD_RANGE_RATIO:
            log(f"[{symbol}] Spread trop élevé. Spread={spread}")
            return

        # ----------------------------------------------------
        # SL
        # ----------------------------------------------------
        stop_loss = calculate_stop_loss(candles, signal)
        if stop_loss is None:
            return

        # ----------------------------------------------------
        # Vérification direction SL
        # ----------------------------------------------------
        if signal == "BUY" and stop_loss >= entry:
            log(f"[{symbol}] SL BUY invalide.")
            return

        if signal == "SELL" and stop_loss <= entry:
            log(f"[{symbol}] SL SELL invalide.")
            return

        # ----------------------------------------------------
        # SPÉCIFICATION
        # ----------------------------------------------------
        specification = await connection.get_symbol_specification(symbol)
        digits = int(specification.get("digits", 5))

        entry = normalize_price(entry, digits)
        stop_loss = normalize_price(stop_loss, digits)

        risk_distance = abs(entry - stop_loss)
        if risk_distance <= 0:
            return

        # ----------------------------------------------------
        # TP
        # ----------------------------------------------------
        if signal == "BUY":
            tp1 = entry + (risk_distance * TP1_R)
            tp2 = entry + (risk_distance * TP2_R)
            tp3 = entry + (risk_distance * TP3_R)
        else:
            tp1 = entry - (risk_distance * TP1_R)
            tp2 = entry - (risk_distance * TP2_R)
            tp3 = entry - (risk_distance * TP3_R)

        tp3 = normalize_price(tp3, digits)

        # ----------------------------------------------------
        # LOT
        # ----------------------------------------------------
        volume = await calculate_risk_volume(connection, symbol, entry, stop_loss)
        if volume is None or volume <= 0:
            log(f"[{symbol}] Volume invalide.")
            return

        # ----------------------------------------------------
        # AFFICHAGE
        # ----------------------------------------------------
        print()
        print("=" * 70)
        print(f"🔥 ARKAS M5 — {symbol}")
        print("=" * 70)
        print(f"Direction : {signal}")
        print(f"Score : {score}/6")
        print(f"Entry : {entry}")
        print(f"SL : {stop_loss}")
        print(f"TP1 : {tp1}")
        print(f"TP2 : {tp2}")
        print(f"TP3 : {tp3}")
        print(f"Volume : {volume}")
        print(f"Risque : {RISK_PERCENT}%")
        print("=" * 70)

        # ----------------------------------------------------
        # ENTRÉE AUTOMATIQUE
        # ----------------------------------------------------
        if signal == "BUY":
            order = await connection.create_market_buy_order(
                symbol, volume, stop_loss, tp3
            )
        else:
            order = await connection.create_market_sell_order(
                symbol, volume, stop_loss, tp3
            )

        # ----------------------------------------------------
        # SUCCÈS
        # ----------------------------------------------------
        last_trade_candle[symbol] = candle_time
        log(f"✅ [{symbol}] {signal} OUVERT")
        log(f"Order result: {order}")

    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse/ordre : {e}")


# ============================================================
# 22. MAIN
# ============================================================

async def main():
    if not TOKEN:
        raise RuntimeError("METAAPI_TOKEN n'est pas configuré.")
    if not ACCOUNT_ID:
        raise RuntimeError("METAAPI_ACCOUNT_ID n'est pas configuré.")

    print()
    print("=" * 70)
    print(" ARKAS M5 AGGRESSIVE — DEMO")
    print("=" * 70)
    print(f"Timeframe : {TIMEFRAME}")
    print(f"Symboles : {', '.join(SYMBOLS)}")
    print(f"Risque : {RISK_PERCENT}%")
    print(f"Score min : {MIN_SCORE}/6")
    print("MODE : EXECUTION DEMO")
    print("=" * 70)

    api = MetaApi(TOKEN)
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)

    log(f"Compte : {account.name}")
    log(f"État MetaApi : {account.state}")

    if account.state != "DEPLOYED":
        log("Déploiement du compte...")
        await account.deploy()

    connection = account.get_rpc_connection()
    log("Connexion MT5...")
    await connection.connect()
    log("Synchronisation...")
    await connection.wait_synchronized()
    log("🟢 ARKAS CONNECTÉ")

    # --------------------------------------------------------
    # TEST PRIX
    # --------------------------------------------------------
    for symbol in SYMBOLS:
        try:
            price = await connection.get_symbol_price(symbol)
            log(f"[{symbol}] BID={price['bid']} ASK={price['ask']}")
        except Exception as e:
            log(f"[{symbol}] Prix indisponible : {e}")

    # --------------------------------------------------------
    # BOUCLE PRINCIPALE
    # --------------------------------------------------------
    while True:
        try:
            for symbol in SYMBOLS:
                await analyze_symbol(connection, symbol)
            await manage_break_even(connection)
            await manage_trailing(connection)
            await asyncio.sleep(SCAN_INTERVAL)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log(f"Erreur boucle principale : {e}")
            await asyncio.sleep(5)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nARKAS arrêté.")
    except Exception as e:
        print(f"\nERREUR FATALE : {e}")
