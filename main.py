import asyncio
import os
import sys
import math
from datetime import datetime, timezone

from metaapi_cloud_sdk import MetaApi

# ==============================================================================
# ARKAS BOT FUSIONNÉ — STOCH MTF + ORDER BLOCKS
# Sans Volatility 75 — SL/TP serrés sur indices
# ==============================================================================

# ==============================================================================
# 1. CONFIGURATION METAAPI
# ==============================================================================

TOKEN = os.getenv("METAAPI_TOKEN")
ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "fb767521-946d-40e4-b9cf-09e9b130f0dd")
METAAPI_REGION = os.getenv("METAAPI_REGION", "london")

# ==============================================================================
# 2. MARCHÉS
# ==============================================================================

STOCH_SYMBOLS = [
    "Step Index",
    "Volatility 100 Index",
    "Volatility 25 Index",
]

OB_SYMBOLS = [
    "XAUUSD",
    "USDJPY",
    "GBPJPY",
    "BTCUSD",
]

TIMEFRAME_ENTRY = "15m"

# ==============================================================================
# 3. STOCHASTIQUE (indices)
# ==============================================================================

STOCH_K = 5
STOCH_D = 3
STOCH_SLOWING = 3

ALERT_OVERBOUGHT = 85
ALERT_OVERSOLD = 15

ZONE_NEUTRAL_LOW = 45
ZONE_NEUTRAL_HIGH = 55

ATR_PERIOD_STOCH = 14
ATR_SL_MULTIPLIER_STOCH = 1.5
ATR_TRAIL_MULTIPLIER_STOCH = 1.0
ATR_MIN_TRAIL_STEP_STOCH = 0.3
BREAK_EVEN_TRIGGER_R_STOCH = 0.5
RISK_REWARD_RATIO_STOCH = 2.0

LOT_PER_STOCH_SYMBOL = {
    "Step Index": 0.1,
    "Volatility 100 Index": 1.0,
    "Volatility 25 Index": 0.5,
}

MAX_SPREAD = {
    "Step Index": 2.0,
    "Volatility 100 Index": 150.0,
    "Volatility 25 Index": 50.0,
    "XAUUSD": 5.0,
    "USDJPY": 0.5,
    "GBPJPY": 0.8,
    "BTCUSD": 50.0,
}

DEFAULT_SL_POINTS = {
    "Step Index": 50.0,
    "Volatility 100 Index": 700.0,
    "Volatility 25 Index": 200.0,
}

# ==============================================================================
# 4. ORDER BLOCKS (Forex/Métaux/Crypto)
# ==============================================================================

SWING_LEN = 5
MAX_ZONES_TO_TRACK = 3
CANDLE_COUNT_OB = 200

INVALIDATION_METHOD = "Wick"
RR_RATIO_OB = 2.0
MAX_LIMIT_ORDERS_PER_SYMBOL = 2
MIN_QUALITY_SCORE = 4

RISK_PERCENT_OB = 0.5

VOLUME_LIMITS_OB = {
    "XAUUSD": {"min": 0.01, "max": 0.1},
    "USDJPY": {"min": 0.01, "max": 0.1},
    "GBPJPY": {"min": 0.01, "max": 0.1},
    "BTCUSD": {"min": 0.01, "max": 0.1},
}

BREAK_EVEN_R_OB = 1.0
TRAILING_ENABLED_OB = True
TRAILING_START_R_OB = 1.5
TRAILING_DISTANCE_R_OB = 0.7

# ==============================================================================
# 5. EXECUTION
# ==============================================================================

SCAN_INTERVAL = 15
CANDLES_LIMIT = 100

# ==============================================================================
# 6. ÉTAT GLOBAL
# ==============================================================================

LAST_SIGNAL = {}
POSITION_STATE = {}
DEBUG_LAST_LOG = {}
ob_zones = {}

def reset_position_state(symbol):
    POSITION_STATE[symbol] = {"r_reached": False, "alerted": False, "initial_risk": 0.0}

def ensure_position_state(symbol):
    if symbol not in POSITION_STATE:
        reset_position_state(symbol)
    return POSITION_STATE[symbol]

# ==============================================================================
# 7. UTILS
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
# 8. STOCHASTIQUE
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
# 9. ATR
# ==============================================================================

def calculate_atr(candles, period=14):
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
# 10. STRUCTURE
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
    wick_rejection = (body_last > 0 and lw_last >= body_last) or (body_prev > 0 and lw_prev >= body_prev)
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
    wick_rejection = (body_last > 0 and uw_last >= body_last) or (body_prev > 0 and uw_prev >= body_prev)
    _, swing_high = detect_structure(candles[:-1], lookback)
    below_swing = (swing_high is None) or (current_price < swing_high)
    return wick_rejection and below_swing

# ==============================================================================
# 11. RETOURNEMENTS
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
# 12. BOUGIES
# ==============================================================================

async def get_candles(account, symbol, timeframe, limit=CANDLES_LIMIT):
    try:
        candles = await account.get_historical_candles(
            symbol=symbol, timeframe=timeframe, limit=limit
        )
        if not candles or len(candles) < 3:
            return None
        return candles
    except Exception as e:
        log(f"❌ [{symbol}] Erreur bougies : {e}")
        return None

# ==============================================================================
# 13. VÉRIF SYMBOLE
# ==============================================================================

async def is_symbol_tradable(connection, symbol):
    try:
        spec = await connection.get_symbol_specification(symbol)
        if not spec:
            return False, None
        if spec.get("tradeMode") == "SYMBOL_TRADE_MODE_DISABLED":
            return False, spec
        return True, spec
    except Exception as e:
        log(f"❌ [{symbol}] Erreur vérif : {e}")
        return False, None

# ==============================================================================
# 14. VOLUME STOCH
# ==============================================================================

def get_fixed_lot_stoch(symbol, spec):
    min_vol = float(spec.get("minVolume", 0.01))
    max_vol = float(spec.get("maxVolume", 100))
    step = float(spec.get("volumeStep", 0.01))
    target = LOT_PER_STOCH_SYMBOL.get(symbol, min_vol)
    volume = normalize_volume(target, min_vol, max_vol, step)
    log(f"💰 [{symbol}] Lot = {volume}")
    return volume

# ==============================================================================
# 15. STOPS
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
    except Exception:
        return sl_price, tp_price

def can_modify_sl(spec, current_price, new_sl, digits):
    min_distance = get_min_stop_distance(spec, digits)
    if min_distance <= 0:
        return True
    return abs(current_price - new_sl) >= min_distance

# ==============================================================================
# 16. GESTION POSITIONS STOCH
# ==============================================================================

async def manage_open_positions_stoch(account, connection, symbol):
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
        atr_m15 = calculate_atr(candles_m15, ATR_PERIOD_STOCH)

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
                    state["initial_risk"] = atr_m15 * ATR_SL_MULTIPLIER_STOCH
                else:
                    state["initial_risk"] = DEFAULT_SL_POINTS.get(symbol, 100.0)
            initial_risk = state["initial_risk"]

            if pos_type == "POSITION_TYPE_BUY":
                if (curr_k >= ALERT_OVERBOUGHT or curr_d >= ALERT_OVERBOUGHT) and not state["alerted"]:
                    log(f"⚠️ [{symbol}] ALERTE Stoch haute ({curr_k:.1f}/{curr_d:.1f})")
                    state["alerted"] = True
                profit = bid - open_price
                if profit >= initial_risk * BREAK_EVEN_TRIGGER_R_STOCH and current_sl < open_price:
                    be_sl = normalize_price(open_price + min_distance, digits) if min_distance > 0 else open_price
                    log(f"🛡️ [{symbol}] BE BUY #{pos_id} SL={be_sl}")
                    await connection.modify_position(pos_id, stop_loss=be_sl, take_profit=current_tp)
                    current_sl = be_sl
                    state["r_reached"] = True
                if state["r_reached"] and atr_m15 and atr_m15 > 0:
                    new_sl = normalize_price(bid - atr_m15 * ATR_TRAIL_MULTIPLIER_STOCH, digits)
                    step_min = atr_m15 * ATR_MIN_TRAIL_STEP_STOCH
                    if new_sl > current_sl + step_min and can_modify_sl(spec, bid, new_sl, digits):
                        log(f"📈 [{symbol}] Trailing BUY #{pos_id} → SL={new_sl}")
                        await connection.modify_position(pos_id, stop_loss=new_sl, take_profit=current_tp)
                        current_sl = new_sl
                if detect_bullish_reversal(candles_m15, stoch_m15):
                    log(f"🔴 [{symbol}] Retournement → Fermeture BUY #{pos_id}")
                    await connection.close_position(pos_id)
                    reset_position_state(symbol)
                    continue

            elif pos_type == "POSITION_TYPE_SELL":
                if (curr_k <= ALERT_OVERSOLD or curr_d <= ALERT_OVERSOLD) and not state["alerted"]:
                    log(f"⚠️ [{symbol}] ALERTE Stoch basse ({curr_k:.1f}/{curr_d:.1f})")
                    state["alerted"] = True
                profit = open_price - ask
                if profit >= initial_risk * BREAK_EVEN_TRIGGER_R_STOCH and (current_sl > open_price or current_sl == 0):
                    be_sl = normalize_price(open_price - min_distance, digits) if min_distance > 0 else open_price
                    log(f"🛡️ [{symbol}] BE SELL #{pos_id} SL={be_sl}")
                    await connection.modify_position(pos_id, stop_loss=be_sl, take_profit=current_tp)
                    current_sl = be_sl
                    state["r_reached"] = True
                if state["r_reached"] and atr_m15 and atr_m15 > 0:
                    new_sl = normalize_price(ask + atr_m15 * ATR_TRAIL_MULTIPLIER_STOCH, digits)
                    step_min = atr_m15 * ATR_MIN_TRAIL_STEP_STOCH
                    if new_sl < current_sl - step_min and can_modify_sl(spec, ask, new_sl, digits):
                        log(f"📉 [{symbol}] Trailing SELL #{pos_id} → SL={new_sl}")
                        await connection.modify_position(pos_id, stop_loss=new_sl, take_profit=current_tp)
                        current_sl = new_sl
                if detect_bearish_reversal(candles_m15, stoch_m15):
                    log(f"🟢 [{symbol}] Retournement → Fermeture SELL #{pos_id}")
                    await connection.close_position(pos_id)
                    reset_position_state(symbol)
                    continue

    except Exception as e:
        log(f"❌ [{symbol}] Erreur gestion Stoch : {e}")

# ==============================================================================
# 17. ANALYSE STOCH
# ==============================================================================

async def analyze_stoch(account, connection, symbol):
    try:
        tradable, spec = await is_symbol_tradable(connection, symbol)
        if not tradable or not spec:
            return

        digits = int(spec.get("digits", 2))
        positions = await connection.get_positions()
        active = [p for p in positions if p.get("symbol") == symbol]

        if len(active) >= 1:
            await manage_open_positions_stoch(account, connection, symbol)
            return

        candles_m15 = await get_candles(account, symbol, TIMEFRAME_ENTRY)
        stoch_m15 = calculate_stochastic(candles_m15, STOCH_K, STOCH_D, STOCH_SLOWING)
        if not stoch_m15 or len(stoch_m15["k"]) < 3:
            return

        prev_k, prev_d = stoch_m15["k"][-2], stoch_m15["d"][-2]
        curr_k, curr_d = stoch_m15["k"][-1], stoch_m15["d"][-1]
        older_k = stoch_m15["k"][-3]

        cross_up = prev_k <= prev_d and curr_k > curr_d and curr_k > older_k
        cross_down = prev_k >= prev_d and curr_k < curr_d and curr_k < older_k

        if not (cross_up or cross_down):
            log(f"🔍 [{symbol}] Stoch M15 : K={curr_k:.1f} D={curr_d:.1f}")
            return

        if cross_up and curr_k >= ZONE_NEUTRAL_LOW:
            log(f"🚫 [{symbol}] Croisement haussier ignoré — K={curr_k:.1f}")
            return
        if cross_down and curr_k <= ZONE_NEUTRAL_HIGH:
            log(f"🚫 [{symbol}] Croisement baissier ignoré — K={curr_k:.1f}")
            return

        signal_type = "BUY" if cross_up else "SELL"
        if LAST_SIGNAL.get(symbol) == signal_type:
            log(f"⏸️ [{symbol}] Signal {signal_type} déjà traité")
            return

        price_info = await connection.get_symbol_price(symbol)
        bid, ask = float(price_info["bid"]), float(price_info["ask"])
        spread = ask - bid

        if spread > MAX_SPREAD.get(symbol, 999.0):
            log(f"🚫 [{symbol}] Spread trop élevé ({spread:.2f})")
            return

        atr_m15 = calculate_atr(candles_m15, ATR_PERIOD_STOCH)
        if not atr_m15 or atr_m15 <= 0:
            return
        if cross_up and not structure_confirms_buy(candles_m15, ask):
            return
        if cross_down and not structure_confirms_sell(candles_m15, bid):
            return

        volume = get_fixed_lot_stoch(symbol, spec)
        sl_distance = atr_m15 * ATR_SL_MULTIPLIER_STOCH
        tp_distance = sl_distance * RISK_REWARD_RATIO_STOCH

        if cross_up:
            sl_price = normalize_price(ask - sl_distance, digits)
            tp_price = normalize_price(ask + tp_distance, digits)
            sl_price, tp_price = validate_stops(spec, ask, sl_price, tp_price, digits)
            log(f"🚀 [{symbol}] ACHAT STOCH | Entry={ask} | SL={sl_price} | TP={tp_price} | Vol={volume}")
            try:
                await connection.create_market_buy_order(
                    symbol=symbol, volume=volume, stop_loss=sl_price, take_profit=tp_price
                )
                log(f"✅ [{symbol}] ACHAT STOCH exécuté")
                LAST_SIGNAL[symbol] = "BUY"
                reset_position_state(symbol)
            except Exception as e:
                log(f"❌ [{symbol}] ACHAT erreur : {e}")

        elif cross_down:
            sl_price = normalize_price(bid + sl_distance, digits)
            tp_price = normalize_price(bid - tp_distance, digits)
            sl_price, tp_price = validate_stops(spec, bid, sl_price, tp_price, digits)
            log(f"🔻 [{symbol}] VENTE STOCH | Entry={bid} | SL={sl_price} | TP={tp_price} | Vol={volume}")
            try:
                await connection.create_market_sell_order(
                    symbol=symbol, volume=volume, stop_loss=sl_price, take_profit=tp_price
                )
                log(f"✅ [{symbol}] VENTE STOCH exécutée")
                LAST_SIGNAL[symbol] = "SELL"
                reset_position_state(symbol)
            except Exception as e:
                log(f"❌ [{symbol}] VENTE erreur : {e}")

    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse Stoch : {e}")

# ==============================================================================
# 18. ORDER BLOCK — DÉTECTION
# ==============================================================================

def find_swing_highs_lows(candles, swing_len):
    highs = []
    lows = []
    n = len(candles)
    if n < swing_len * 2 + 1:
        return highs, lows
    for i in range(swing_len, n - swing_len):
        is_sh = True
        for j in range(1, swing_len + 1):
            if float(candles[i]["high"]) <= float(candles[i - j]["high"]) or float(candles[i]["high"]) <= float(candles[i + j]["high"]):
                is_sh = False
                break
        if is_sh:
            highs.append({"index": i, "price": float(candles[i]["high"])})
        is_sl = True
        for j in range(1, swing_len + 1):
            if float(candles[i]["low"]) >= float(candles[i - j]["low"]) or float(candles[i]["low"]) >= float(candles[i + j]["low"]):
                is_sl = False
                break
        if is_sl:
            lows.append({"index": i, "price": float(candles[i]["low"])})
    return highs, lows

def detect_bos(candles, swing_highs, swing_lows):
    bos_list = []
    n = len(candles)
    if n < 5:
        return bos_list
    for sh in swing_highs:
        for i in range(sh["index"] + 1, n):
            if float(candles[i]["close"]) > sh["price"] and float(candles[i - 1]["close"]) <= sh["price"]:
                bos_list.append({"type": "BULL", "bos_index": i, "swing_index": sh["index"], "swing_price": sh["price"]})
                break
    for sl in swing_lows:
        for i in range(sl["index"] + 1, n):
            if float(candles[i]["close"]) < sl["price"] and float(candles[i - 1]["close"]) >= sl["price"]:
                bos_list.append({"type": "BEAR", "bos_index": i, "swing_index": sl["index"], "swing_price": sl["price"]})
                break
    return bos_list

def calculate_quality_score(candles, bos, anchor_idx, top, bottom):
    score = 0
    bos_idx = bos["bos_index"]
    zone_type = bos["type"]
    zone_size = top - bottom
    if zone_size <= 0:
        return 0

    if abs(float(candles[bos_idx]["close"]) - bos["swing_price"]) >= zone_size * 2.0:
        score += 1

    volumes = [float(c.get("volume", 0) or 0) for c in candles[-50:]]
    avg_vol = sum(volumes) / len(volumes) if volumes else 0
    if avg_vol > 0 and float(candles[anchor_idx].get("volume", 0) or 0) >= avg_vol * 1.3:
        score += 1

    ranges = [float(c["high"]) - float(c["low"]) for c in candles[-20:]]
    avg_range = sum(ranges) / len(ranges) if ranges else 0
    if avg_range > 0 and 0.5 <= zone_size / avg_range <= 2.0:
        score += 1

    bos_body = abs(float(candles[bos_idx]["close"]) - float(candles[bos_idx]["open"]))
    bos_range = float(candles[bos_idx]["high"]) - float(candles[bos_idx]["low"])
    if bos_range > 0 and bos_body / bos_range >= 0.6:
        score += 1

    if len(candles) - 1 - bos_idx <= 30:
        score += 1

    pen = 0
    for i in range(bos_idx + 1, len(candles)):
        ch, cl = float(candles[i]["high"]), float(candles[i]["low"])
        if ch >= bottom and cl <= top and min(ch, top) - max(cl, bottom) > zone_size * 0.5:
            pen += 1
    if pen == 0:
        score += 1

    closes = [float(c["close"]) for c in candles]
    if len(closes) >= 50:
        ema50 = sum(closes[-50:]) / 50
        if (zone_type == "BULL" and closes[-1] > ema50) or (zone_type == "BEAR" and closes[-1] < ema50):
            score += 1
    return score

def create_ob_zone(candles, bos):
    bos_idx = bos["bos_index"]
    swing_idx = bos["swing_index"]
    if bos_idx <= swing_idx:
        return None
    best_idx = None
    best_vol = -1
    for i in range(swing_idx, bos_idx + 1):
        v = float(candles[i].get("volume", 0) or 0)
        if v > best_vol:
            best_vol = v
            best_idx = i
    if best_idx is None:
        return None
    top = float(candles[best_idx]["high"])
    bottom = float(candles[best_idx]["low"])
    if top <= bottom:
        return None
    quality = calculate_quality_score(candles, bos, best_idx, top, bottom)
    if quality < MIN_QUALITY_SCORE:
        return None
    return {
        "type": bos["type"], "top": top, "bottom": bottom,
        "anchor_index": best_idx, "bos_index": bos_idx,
        "active": True, "retested": False,
        "order_placed": False, "order_id": None,
        "quality_score": quality,
    }

def update_zones(candles, zones, symbol):
    if not zones:
        return zones
    last = candles[-1]
    lh, ll, lc = float(last["high"]), float(last["low"]), float(last["close"])
    for zone in zones:
        if not zone["active"]:
            continue
        if lh >= zone["bottom"] and ll <= zone["top"]:
            zone["retested"] = True
        invalid = False
        if zone["type"] == "BULL":
            invalid = ll < zone["bottom"] if INVALIDATION_METHOD == "Wick" else lc < zone["bottom"]
        else:
            invalid = lh > zone["top"] if INVALIDATION_METHOD == "Wick" else lc > zone["top"]
        if invalid:
            zone["active"] = False
            log(f"🔴 [{symbol}] Zone OB {zone['type']} invalidée")
    return zones

# ==============================================================================
# 19. ORDER BLOCK — VOLUME + PLACEMENT
# ==============================================================================

async def calculate_volume_ob(connection, symbol, entry, stop_loss):
    try:
        info = await connection.get_account_information()
        balance = float(info.get("balance", 0))
        if balance <= 0:
            return None
        spec = await connection.get_symbol_specification(symbol)
        minimum = float(spec.get("minVolume", 0.01))
        maximum = float(spec.get("maxVolume", 100))
        step = float(spec.get("volumeStep", 0.01))
        contract_size = float(spec.get("contractSize", spec.get("tradeContractSize", 1)))
        if contract_size <= 0:
            return None
        if symbol in VOLUME_LIMITS_OB:
            minimum = max(minimum, VOLUME_LIMITS_OB[symbol]["min"])
            maximum = min(maximum, VOLUME_LIMITS_OB[symbol]["max"])
        risk_money = balance * RISK_PERCENT_OB / 100
        price_distance = abs(entry - stop_loss)
        if price_distance <= 0:
            return None
        risk_per_lot = price_distance * contract_size
        if risk_per_lot <= 0:
            return None
        raw = risk_money / risk_per_lot
        volume = normalize_volume(raw, minimum, maximum, step)
        return volume
    except Exception as e:
        log(f"[{symbol}] Erreur volume OB : {e}")
        return None

async def count_active_limit_orders(connection, symbol):
    try:
        orders = await connection.get_orders()
        return sum(
            1 for o in orders
            if o.get("symbol") == symbol
            and o.get("type", "") in ("ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_SELL_LIMIT")
        )
    except Exception as e:
        log(f"[{symbol}] Erreur comptage ordres : {e}")
        return 999

async def place_limit_order_ob(connection, symbol, zone):
    try:
        price = await connection.get_symbol_price(symbol)
        bid, ask = float(price["bid"]), float(price["ask"])
        spec = await connection.get_symbol_specification(symbol)
        digits = int(spec.get("digits", 5))

        if zone["type"] == "BULL":
            entry = normalize_price(zone["top"], digits)
            sl = normalize_price(zone["bottom"], digits)
            if ask <= entry:
                return None
            risk = entry - sl
            tp = normalize_price(entry + risk * RR_RATIO_OB, digits)
        else:
            entry = normalize_price(zone["bottom"], digits)
            sl = normalize_price(zone["top"], digits)
            if bid >= entry:
                return None
            risk = sl - entry
            tp = normalize_price(entry - risk * RR_RATIO_OB, digits)

        if risk <= 0:
            return None
        volume = await calculate_volume_ob(connection, symbol, entry, sl)
        if volume is None or volume <= 0:
            return None

        log(f"🟢 [{symbol}] {'BUY' if zone['type']=='BULL' else 'SELL'} LIMIT | Entry={entry} | SL={sl} | TP={tp} | Vol={volume} | Score={zone['quality_score']}/7")

        if zone["type"] == "BULL":
            order = await connection.create_limit_buy_order(
                symbol=symbol, volume=volume, open_price=entry, stop_loss=sl, take_profit=tp
            )
        else:
            order = await connection.create_limit_sell_order(
                symbol=symbol, volume=volume, open_price=entry, stop_loss=sl, take_profit=tp
            )
        zone["order_placed"] = True
        zone["order_id"] = order.get("orderId") if isinstance(order, dict) else order
        return order
    except Exception as e:
        log(f"❌ [{symbol}] Erreur ordre OB : {e}")
        return None

async def cleanup_zones(connection, symbol):
    if symbol not in ob_zones:
        return
    try:
        orders = await connection.get_orders()
        active_ids = {o.get("id") or o.get("orderId") for o in orders}
        for zone in ob_zones[symbol]:
            oid = zone.get("order_id")
            if oid and oid not in active_ids:
                zone["order_placed"] = False
                zone["order_id"] = None
    except Exception:
        pass

# ==============================================================================
# 20. GESTION POSITIONS OB (BE + Trailing)
# ==============================================================================

async def manage_positions_ob(connection):
    try:
        positions = await connection.get_positions()
        for pos in positions:
            symbol = pos.get("symbol")
            if symbol not in OB_SYMBOLS:
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
                ptype = pos.get("type")

                if ptype == "POSITION_TYPE_BUY":
                    profit = current - entry
                    if profit >= risk * BREAK_EVEN_R_OB and sl < entry:
                        log(f"🟢 [{symbol}] BE BUY #{pos['id']}")
                        await connection.modify_position(pos["id"], stop_loss=entry, take_profit=pos.get("takeProfit"))
                        sl = entry
                    if TRAILING_ENABLED_OB and profit >= abs(entry - sl) * TRAILING_START_R_OB:
                        new_sl = current - abs(entry - sl) * TRAILING_DISTANCE_R_OB
                        if new_sl > sl:
                            log(f"📈 [{symbol}] Trailing BUY #{pos['id']} SL {sl} → {new_sl}")
                            await connection.modify_position(pos["id"], stop_loss=new_sl, take_profit=pos.get("takeProfit"))
                elif ptype == "POSITION_TYPE_SELL":
                    profit = entry - current
                    if profit >= risk * BREAK_EVEN_R_OB and sl > entry:
                        log(f"🔴 [{symbol}] BE SELL #{pos['id']}")
                        await connection.modify_position(pos["id"], stop_loss=entry, take_profit=pos.get("takeProfit"))
                        sl = entry
                    if TRAILING_ENABLED_OB and profit >= abs(entry - sl) * TRAILING_START_R_OB:
                        new_sl = current + abs(entry - sl) * TRAILING_DISTANCE_R_OB
                        if new_sl < sl:
                            log(f"📉 [{symbol}] Trailing SELL #{pos['id']} SL {sl} → {new_sl}")
                            await connection.modify_position(pos["id"], stop_loss=new_sl, take_profit=pos.get("takeProfit"))
            except Exception as e:
                log(f"[{symbol}] Erreur gestion OB : {e}")
    except Exception as e:
        log(f"Erreur gestion OB globale : {e}")

# ==============================================================================
# 21. ANALYSE OB
# ==============================================================================

async def analyze_ob(account, connection, symbol):
    try:
        await cleanup_zones(connection, symbol)

        candles = await get_candles(account, symbol, TIMEFRAME_ENTRY, CANDLE_COUNT_OB)
        if not candles or len(candles) < SWING_LEN * 3:
            return

        swing_highs, swing_lows = find_swing_highs_lows(candles, SWING_LEN)
        bos_list = detect_bos(candles, swing_highs, swing_lows)

        if symbol not in ob_zones:
            ob_zones[symbol] = []

        recent_bos = bos_list[-5:] if len(bos_list) >= 5 else bos_list
        for bos in recent_bos:
            zone = create_ob_zone(candles, bos)
            if zone is None:
                continue
            if any(z.get("anchor_index") == zone["anchor_index"] and z["type"] == zone["type"] for z in ob_zones[symbol]):
                continue
            ob_zones[symbol].append(zone)
            log(f"🆕 [{symbol}] Zone OB {zone['type']} top={zone['top']} bot={zone['bottom']} score={zone['quality_score']}/7")

        ob_zones[symbol] = update_zones(candles, ob_zones[symbol], symbol)
        active_zones = [z for z in ob_zones[symbol] if z["active"]]

        last_close = float(candles[-1]["close"])
        def dist(z):
            if last_close > z["top"]:
                return last_close - z["top"]
            if last_close < z["bottom"]:
                return z["bottom"] - last_close
            return 0.0
        active_zones.sort(key=lambda z: (-z.get("quality_score", 0), dist(z)))
        active_zones = active_zones[:MAX_ZONES_TO_TRACK]

        active_orders = await count_active_limit_orders(connection, symbol)
        log(f"[{symbol}] OB zones actives={len(active_zones)} | Ordres LIMIT={active_orders}/{MAX_LIMIT_ORDERS_PER_SYMBOL}")

        for zone in active_zones:
            if active_orders >= MAX_LIMIT_ORDERS_PER_SYMBOL:
                break
            if zone.get("order_placed", False):
                continue
            result = await place_limit_order_ob(connection, symbol, zone)
            if result is not None:
                active_orders += 1

    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse OB : {e}")

# ==============================================================================
# 22. HEALTH CHECK HTTP
# ==============================================================================

async def health_check_server():
    port = int(os.getenv("PORT", 10000))
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"Arkas Bot OK"
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
    server = await asyncio.start_server(handle, "0.0.0.0", port)
    log(f"🌐 Health check HTTP port {port}")
    async with server:
        await server.serve_forever()

# ==============================================================================
# 23. BOUCLE PRINCIPALE
# ==============================================================================

async def trading_loop(account, connection):
    while True:
        try:
            for symbol in STOCH_SYMBOLS:
                await analyze_stoch(account, connection, symbol)
                await asyncio.sleep(1)

            for symbol in OB_SYMBOLS:
                await analyze_ob(account, connection, symbol)
                await asyncio.sleep(1)

            await manage_positions_ob(connection)

            await asyncio.sleep(SCAN_INTERVAL)
        except Exception as e:
            log(f"⚠️ Erreur boucle : {e}")
            await asyncio.sleep(10)

# ==============================================================================
# 24. MAIN
# ==============================================================================

async def main():
    print("=" * 60, flush=True)
    print("🚀 ARKAS BOT FUSIONNÉ — STOCH + ORDER BLOCKS", flush=True)
    print("=" * 60, flush=True)
    print(f"STOCH : {', '.join(STOCH_SYMBOLS)}", flush=True)
    print(f"OB    : {', '.join(OB_SYMBOLS)}", flush=True)
    print(f"Timeframe : {TIMEFRAME_ENTRY}", flush=True)
    print(f"SL Stoch : ATR × {ATR_SL_MULTIPLIER_STOCH}", flush=True)
    print(f"Région MetaApi : {METAAPI_REGION}", flush=True)
    print("=" * 60, flush=True)

    if not TOKEN:
        raise RuntimeError("METAAPI_TOKEN manquant.")

    asyncio.create_task(health_check_server())

    api = MetaApi(TOKEN, {"region": METAAPI_REGION})

    try:
        account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
        log(f"Compte : {account.name} ({account.state}) | Région: {METAAPI_REGION}")

        if account.state != "DEPLOYED":
            log("Déploiement...")
            await account.deploy()

        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized(60)
        log("🟢 BOT FUSIONNÉ CONNECTÉ")

        await trading_loop(account, connection)

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
