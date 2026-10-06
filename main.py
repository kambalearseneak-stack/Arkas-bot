import asyncio
import os
import sys
import math
from datetime import datetime

from metaapi_cloud_sdk import MetaApi

# ==============================================================================
# ARKAS STOCHASTIC MTF BOT — VERSION 4.0.1
# ==============================================================================

# ==============================================================================
# 1. CONFIGURATION METAAPI
# ==============================================================================

TOKEN = os.getenv("METAAPI_TOKEN")
ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "fb767521-946d-40e4-b9cf-09e9b130f0dd")

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

ALERT_OVERBOUGHT = 85
ALERT_OVERSOLD = 15

# ==============================================================================
# 4. MONEY MANAGEMENT
# ==============================================================================

LOT_PER_SYMBOL = {
    "Step Index": 0.1,
    "Volatility 75 Index": 0.01
}

VOLUME_LIMITS = {
    "Step Index": {"min": 0.1, "max": 10.0},
    "Volatility 75 Index": {"min": 0.01, "max": 1.0}
}

MAX_POSITIONS_PER_SYMBOL = 1

DEFAULT_SL_POINTS = {
    "Step Index": 50.0,
    "Volatility 75 Index": 500.0
}

# ==============================================================================
# 5. GESTION POSITION
# ==============================================================================

ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 2.0
ATR_TRAIL_MULTIPLIER = 2.0
ATR_MIN_TRAIL_STEP = 0.5

BREAK_EVEN_TRIGGER_R = 1.0
RISK_REWARD_RATIO = 2.0

# ==============================================================================
# 6. EXECUTION
# ==============================================================================

SCAN_INTERVAL = 15
CANDLES_LIMIT = 300

# ==============================================================================
# 7. SPREAD
# ==============================================================================

MAX_SPREAD = {
    "Step Index": 2.0,
    "Volatility 75 Index": 100.0
}

# ==============================================================================
# 8. ANTI-SPAM + ÉTAT
# ==============================================================================

LAST_TRADED_CANDLE = {}
POSITION_STATE = {}

def reset_position_state(symbol):
    POSITION_STATE[symbol] = {"r_reached": False, "alerted": False, "initial_risk": 0.0}

def ensure_position_state(symbol):
    if symbol not in POSITION_STATE:
        reset_position_state(symbol)
    return POSITION_STATE[symbol]

# ==============================================================================
# 9. UTILS
# ==============================================================================

def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

def normalize_price(price, digits):
    return round(float(price), int(digits))

def normalize_volume(volume, minimum, maximum, step):
    if step <= 0:
        step = 0.01
    volume = max(minimum, min(volume, maximum))
    steps = math.floor(volume / step + 1e-9)
    return round(steps * step, 4)

# ==============================================================================
# 10. STOCHASTIQUE ALIGNÉ
# ==============================================================================

def calculate_stochastic(candles, k_period=5, d_period=3, slowing=3):
    if not candles or len(candles) < (k_period + slowing + d_period):
        return None

    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]

    fast_k = []
    for i in range(k_period - 1, len(candles)):
        h = max(highs[i - k_period + 1:i + 1])
        l = min(lows[i - k_period + 1:i + 1])
        fast_k.append(50.0 if h == l else ((closes[i] - l) / (h - l)) * 100)

    slow_k = []
    for i in range(slowing - 1, len(fast_k)):
        slow_k.append(sum(fast_k[i - slowing + 1:i + 1]) / slowing)

    d_line = []
    for i in range(d_period - 1, len(slow_k)):
        d_line.append(sum(slow_k[i - d_period + 1:i + 1]) / d_period)

    aligned_k = slow_k[d_period - 1:]
    if len(aligned_k) != len(d_line) or not aligned_k:
        return None

    return {"k": aligned_k, "d": d_line}

# ==============================================================================
# 11. ATR
# ==============================================================================

def calculate_atr(candles, period=ATR_PERIOD):
    if not candles or len(candles) < period + 1:
        return None

    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i]["high"])
        low = float(candles[i]["low"])
        prev_close = float(candles[i - 1]["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    if len(trs) < period:
        return None

    return sum(trs[-period:]) / period

# ==============================================================================
# 12. STRUCTURE
# ==============================================================================

def detect_structure(candles, lookback=5):
    if not candles or len(candles) < lookback * 2 + 1:
        return None, None

    recent = candles[-lookback * 2:]
    lows = [float(c["low"]) for c in recent]
    highs = [float(c["high"]) for c in recent]

    last_swing_low = None
    for i in range(1, len(lows) - 1):
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
            last_swing_low = lows[i]

    last_swing_high = None
    for i in range(1, len(highs) - 1):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
            last_swing_high = highs[i]

    return last_swing_low, last_swing_high

def structure_confirms_buy(candles, current_price, lookback=5):
    if len(candles) < 3:
        return False

    last, prev = candles[-1], candles[-2]

    body_last = abs(float(last["close"]) - float(last["open"]))
    lw_last = min(float(last["close"]), float(last["open"])) - float(last["low"])
    body_prev = abs(float(prev["close"]) - float(prev["open"]))
    lw_prev = min(float(prev["close"]), float(prev["open"])) - float(prev["low"])

    wick_rejection = (
        (body_last > 0 and lw_last >= body_last) or
        (body_prev > 0 and lw_prev >= body_prev)
    )

    swing_low, _ = detect_structure(candles[:-1], lookback)
    above_swing = (swing_low is None) or (current_price > swing_low)

    return wick_rejection and above_swing

def structure_confirms_sell(candles, current_price, lookback=5):
    if len(candles) < 3:
        return False

    last, prev = candles[-1], candles[-2]

    body_last = abs(float(last["close"]) - float(last["open"]))
    uw_last = float(last["high"]) - max(float(last["close"]), float(last["open"]))
    body_prev = abs(float(prev["close"]) - float(prev["open"]))
    uw_prev = float(prev["high"]) - max(float(prev["close"]), float(prev["open"]))

    wick_rejection = (
        (body_last > 0 and uw_last >= body_last) or
        (body_prev > 0 and uw_prev >= body_prev)
    )

    _, swing_high = detect_structure(candles[:-1], lookback)
    below_swing = (swing_high is None) or (current_price < swing_high)

    return wick_rejection and below_swing

# ==============================================================================
# 13. RETOURNEMENTS
# ==============================================================================

def detect_bullish_reversal(candles_m15, stoch):
    if not stoch or len(stoch["k"]) < 3:
        return False

    prev_k, prev_d = stoch["k"][-2], stoch["d"][-2]
    curr_k, curr_d = stoch["k"][-1], stoch["d"][-1]

    cross_down = prev_k >= prev_d and curr_k < curr_d
    in_high_zone = curr_k >= 60 or prev_k >= 70

    if cross_down and in_high_zone:
        return True

    swing_low, _ = detect_structure(candles_m15[:-1], 5)
    if swing_low is not None:
        last_close = float(candles_m15[-1]["close"])
        if last_close < swing_low:
            return True

    return False

def detect_bearish_reversal(candles_m15, stoch):
    if not stoch or len(stoch["k"]) < 3:
        return False

    prev_k, prev_d = stoch["k"][-2], stoch["d"][-2]
    curr_k, curr_d = stoch["k"][-1], stoch["d"][-1]

    cross_up = prev_k <= prev_d and curr_k > curr_d
    in_low_zone = curr_k <= 40 or prev_k <= 30

    if cross_up and in_low_zone:
        return True

    _, swing_high = detect_structure(candles_m15[:-1], 5)
    if swing_high is not None:
        last_close = float(candles_m15[-1]["close"])
        if last_close > swing_high:
            return True

    return False

# ==============================================================================
# 14. BOUGIES
# ==============================================================================

async def get_candles(account, symbol, timeframe, limit=CANDLES_LIMIT):
    try:
        candles = await account.get_historical_candles(
            symbol=symbol, timeframe=timeframe, limit=limit
        )
        return candles[:-1] if candles and len(candles) > 2 else candles
    except Exception as e:
        log(f"❌ [{symbol}] Erreur bougies {timeframe} : {e}")
        return None

# ==============================================================================
# 15. VÉRIF SYMBOLE
# ==============================================================================

async def is_symbol_tradable(connection, symbol):
    try:
        spec = await connection.get_symbol_specification(symbol)
        if not spec:
            return False, None
        if spec.get("tradeMode") == "SYMBOL_TRADE_MODE_DISABLED":
            log(f"⚠️ [{symbol}] Trading désactivé par le broker")
            return False, spec
        return True, spec
    except Exception as e:
        log(f"❌ [{symbol}] Erreur vérification symbole : {e}")
        return False, None

# ==============================================================================
# 16. VOLUME
# ==============================================================================

def get_fixed_lot(symbol, spec):
    min_vol = float(spec.get("minVolume", 0.01))
    max_vol = float(spec.get("maxVolume", 100))
    step = float(spec.get("volumeStep", 0.01))
    target = LOT_PER_SYMBOL.get(symbol, min_vol)
    volume = normalize_volume(target, min_vol, max_vol, step)
    log(f"💰 [{symbol}] Lot = {volume}")
    return volume

# ==============================================================================
# 17. STOPS
# ==============================================================================

def get_min_stop_distance(spec, digits):
    stops_level = float(spec.get("stopsLevel", 0) or 0)
    freeze_level = float(spec.get("freezeLevel", 0) or 0)
    level = max(stops_level, freeze_level)
    return level * (10 ** -digits) if level > 0 else 0.0

def validate_stops(spec, entry_price, sl_price, tp_price, digits):
    try:
        min_distance = get_min_stop_distance(spec, digits)
        if min_distance <= 0:
            return sl_price, tp_price

        buffer = 10 ** -digits

        if abs(entry_price - sl_price) < min_distance:
            sl_price = (entry_price - min_distance - buffer) if sl_price < entry_price else (entry_price + min_distance + buffer)
            sl_price = normalize_price(sl_price, digits)

        if abs(entry_price - tp_price) < min_distance:
            tp_price = (entry_price + min_distance + buffer) if tp_price > entry_price else (entry_price - min_distance - buffer)
            tp_price = normalize_price(tp_price, digits)

        return sl_price, tp_price
    except Exception as e:
        log(f"⚠️ Erreur validation stops : {e}")
        return sl_price, tp_price

def can_modify_sl(spec, current_price, new_sl, digits):
    min_distance = get_min_stop_distance(spec, digits)
    if min_distance <= 0:
        return True
    return abs(current_price - new_sl) >= min_distance

# ==============================================================================
# 18. GESTION POSITIONS
# ==============================================================================

async def manage_open_positions(account, connection, symbol):
    try:
        positions = await connection.get_positions()
        symbol_positions = [p for p in positions if p.get("symbol") == symbol]

        if not symbol_positions:
            reset_position_state(symbol)
            return

        candles_m15 = await get_candles(account, symbol, TIMEFRAME_ENTRY)
        stoch_m15 = calculate_stochastic(candles_m15, STOCH_K, STOCH_D, STOCH_SLOWING)
        if not stoch_m15:
            return

        curr_k = stoch_m15["k"][-1]
        curr_d = stoch_m15["d"][-1]
        atr_m15 = calculate_atr(candles_m15, ATR_PERIOD)

        spec = await connection.get_symbol_specification(symbol)
        digits = int(spec.get("digits", 2))
        min_distance = get_min_stop_distance(spec, digits)

        price_info = await connection.get_symbol_price(symbol)
        bid, ask = float(price_info["bid"]), float(price_info["ask"])

        state = ensure_position_state(symbol)

        for pos in symbol_positions:
            pos_id = pos["id"]
            pos_type = pos.get("type")
            open_price = float(pos["openPrice"])
            current_sl = float(pos.get("stopLoss", 0) or 0)
            current_tp = pos.get("takeProfit")

            if state["initial_risk"] <= 0:
                if current_sl and current_sl != 0:
                    state["initial_risk"] = abs(open_price - current_sl)
                elif atr_m15:
                    state["initial_risk"] = atr_m15 * ATR_SL_MULTIPLIER
                else:
                    state["initial_risk"] = DEFAULT_SL_POINTS.get(symbol, 100.0)

            initial_risk = state["initial_risk"]

            if pos_type == "POSITION_TYPE_BUY":
                if (curr_k >= ALERT_OVERBOUGHT or curr_d >= ALERT_OVERBOUGHT) and not state["alerted"]:
                    log(f"⚠️ [{symbol}] ALERTE : Stoch M15 zone haute ({curr_k:.1f}/{curr_d:.1f})")
                    state["alerted"] = True

                profit = bid - open_price
                if profit >= initial_risk * BREAK_EVEN_TRIGGER_R and current_sl < open_price:
                    be_sl = normalize_price(open_price + min_distance, digits) if min_distance > 0 else open_price
                    log(f"🛡️ [{symbol}] +1R atteint → BE activé BUY #{pos_id} SL={be_sl}")
                    await connection.modify_position(pos_id, stop_loss=be_sl, take_profit=current_tp)
                    current_sl = be_sl
                    state["r_reached"] = True

                if state["r_reached"] and atr_m15 and atr_m15 > 0:
                    new_sl = normalize_price(bid - atr_m15 * ATR_TRAIL_MULTIPLIER, digits)
                    step_min = atr_m15 * ATR_MIN_TRAIL_STEP
                    if new_sl > current_sl + step_min and can_modify_sl(spec, bid, new_sl, digits):
                        log(f"📈 [{symbol}] Trailing BUY #{pos_id} → SL={new_sl}")
                        await connection.modify_position(pos_id, stop_loss=new_sl, take_profit=current_tp)
                        current_sl = new_sl

                if detect_bullish_reversal(candles_m15, stoch_m15):
                    log(f"🔴 [{symbol}] Retournement baissier → Fermeture BUY #{pos_id}")
                    await connection.close_position(pos_id)
                    reset_position_state(symbol)
                    continue

            elif pos_type == "POSITION_TYPE_SELL":
                if (curr_k <= ALERT_OVERSOLD or curr_d <= ALERT_OVERSOLD) and not state["alerted"]:
                    log(f"⚠️ [{symbol}] ALERTE : Stoch M15 zone basse ({curr_k:.1f}/{curr_d:.1f})")
                    state["alerted"] = True

                profit = open_price - ask
                if profit >= initial_risk * BREAK_EVEN_TRIGGER_R and (current_sl > open_price or current_sl == 0):
                    be_sl = normalize_price(open_price - min_distance, digits) if min_distance > 0 else open_price
                    log(f"🛡️ [{symbol}] +1R atteint → BE activé SELL #{pos_id} SL={be_sl}")
                    await connection.modify_position(pos_id, stop_loss=be_sl, take_profit=current_tp)
                    current_sl = be_sl
                    state["r_reached"] = True

                if state["r_reached"] and atr_m15 and atr_m15 > 0:
                    new_sl = normalize_price(ask + atr_m15 * ATR_TRAIL_MULTIPLIER, digits)
                    step_min = atr_m15 * ATR_MIN_TRAIL_STEP
                    if new_sl < current_sl - step_min and can_modify_sl(spec, ask, new_sl, digits):
                        log(f"📉 [{symbol}] Trailing SELL #{pos_id} → SL={new_sl}")
                        await connection.modify_position(pos_id, stop_loss=new_sl, take_profit=current_tp)
                        current_sl = new_sl

                if detect_bearish_reversal(candles_m15, stoch_m15):
                    log(f"🟢 [{symbol}] Retournement haussier → Fermeture SELL #{pos_id}")
                    await connection.close_position(pos_id)
                    reset_position_state(symbol)
                    continue

    except Exception as e:
        log(f"❌ [{symbol}] Erreur gestion positions : {e}")

# ==============================================================================
# 19. ANALYSE MTF
# ==============================================================================

async def analyze_and_trade(account, connection, symbol):
    try:
        tradable, spec = await is_symbol_tradable(connection, symbol)
        if not tradable or not spec:
            return

        digits = int(spec.get("digits", 2))

        positions = await connection.get_positions()
        active = [p for p in positions if p.get("symbol") == symbol]

        if len(active) >= MAX_POSITIONS_PER_SYMBOL:
            await manage_open_positions(account, connection, symbol)
            return

        candles_h1 = await get_candles(account, symbol, TIMEFRAME_ANALYSIS)
        stoch_h1 = calculate_stochastic(candles_h1, STOCH_K, STOCH_D, STOCH_SLOWING)
        if not stoch_h1:
            return

        h1_k, h1_d = stoch_h1["k"][-1], stoch_h1["d"][-1]
        h1_buy_setup = h1_k <= H1_BUY_ZONE and h1_d <= H1_BUY_ZONE
        h1_sell_setup = h1_k >= H1_SELL_ZONE and h1_d >= H1_SELL_ZONE

        if not (h1_buy_setup or h1_sell_setup):
            return

        candles_m15 = await get_candles(account, symbol, TIMEFRAME_ENTRY)
        stoch_m15 = calculate_stochastic(candles_m15, STOCH_K, STOCH_D, STOCH_SLOWING)
        if not stoch_m15 or len(stoch_m15["k"]) < 3:
            return

        prev_k, prev_d = stoch_m15["k"][-2], stoch_m15["d"][-2]
        curr_k, curr_d = stoch_m15["k"][-1], stoch_m15["d"][-1]
        older_k = stoch_m15["k"][-3]

        buy_confirmed = h1_buy_setup and prev_k <= prev_d and curr_k > curr_d and curr_k > older_k
        sell_confirmed = h1_sell_setup and prev_k >= prev_d and curr_k < curr_d and curr_k < older_k

        if not (buy_confirmed or sell_confirmed):
            return

        last_candle_time = candles_m15[-1].get("time") or candles_m15[-1].get("brokerTime")
        if last_candle_time is None:
            return
        if LAST_TRADED_CANDLE.get(symbol) == last_candle_time:
            return

        price_info = await connection.get_symbol_price(symbol)
        bid, ask = float(price_info["bid"]), float(price_info["ask"])

        if buy_confirmed and not structure_confirms_buy(candles_m15, ask):
            log(f"🚫 [{symbol}] Structure prix invalide pour BUY")
            return
        if sell_confirmed and not structure_confirms_sell(candles_m15, bid):
            log(f"🚫 [{symbol}] Structure prix invalide pour SELL")
            return

        spread = ask - bid
        if spread > MAX_SPREAD.get(symbol, 999.0):
            log(f"🚫 [{symbol}] Spread trop élevé ({spread:.2f})")
            return

        atr_m15 = calculate_atr(candles_m15, ATR_PERIOD)
        sl_distance = atr_m15 * ATR_SL_MULTIPLIER if atr_m15 and atr_m15 > 0 else DEFAULT_SL_POINTS.get(symbol, 100.0)
        tp_distance = sl_distance * RISK_REWARD_RATIO

        volume = get_fixed_lot(symbol, spec)

        if buy_confirmed:
            entry = ask
            sl = normalize_price(entry - sl_distance, digits)
            tp = normalize_price(entry + tp_distance, digits)
            sl, tp = validate_stops(spec, entry, sl, tp, digits)

            log(f"🚀 [{symbol}] ACHAT | Entry={entry} | SL={sl} | TP={tp} | Vol={volume}")
            await connection.create_market_buy_order(
                symbol=symbol, volume=volume, stop_loss=sl, take_profit=tp
            )
            LAST_TRADED_CANDLE[symbol] = last_candle_time
            reset_position_state(symbol)

        elif sell_confirmed:
            entry = bid
            sl = normalize_price(entry + sl_distance, digits)
            tp = normalize_price(entry - tp_distance, digits)
            sl, tp = validate_stops(spec, entry, sl, tp, digits)

            log(f"🔻 [{symbol}] VENTE | Entry={entry} | SL={sl} | TP={tp} | Vol={volume}")
            await connection.create_market_sell_order(
                symbol=symbol, volume=volume, stop_loss=sl, take_profit=tp
            )
            LAST_TRADED_CANDLE[symbol] = last_candle_time
            reset_position_state(symbol)

    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse/trading : {e}")

# ==============================================================================
# 20. MAIN
# ==============================================================================

async def main():
    print("==================================================")
    print("🚀 BOT STOCHASTIQUE MTF — VERSION 4.0.1")
    print("==================================================")
    print(f"Symboles : {', '.join(SYMBOLS)}")
    print(f"Timeframes : {TIMEFRAME_ANALYSIS} → {TIMEFRAME_ENTRY}")
    print(f"Lots : " + " | ".join([f"{s}={LOT_PER_SYMBOL[s]}" for s in SYMBOLS]))
    print(f"BE à +{BREAK_EVEN_TRIGGER_R}R | Trailing ATR × {ATR_TRAIL_MULTIPLIER}")
    print(f"TP initial : {RISK_REWARD_RATIO}R")
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
        await connection.wait_synchronized(60)
        log("🟢 BOT CONNECTÉ")

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
