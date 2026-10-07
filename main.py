import asyncio
import os
import sys
import math
from datetime import datetime, timezone

from metaapi_cloud_sdk import MetaApi

# ==============================================================================
# ARKAS BOT — STOCH + ORDER BLOCKS POC (amélioré)
# Basé sur FluxCharts "Order Blocks Volume Delta 3D"
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
]

OB_SYMBOLS = [
    "XAUUSD",
]

TIMEFRAME_ENTRY = "15m"

# ==============================================================================
# 3. STOCHASTIQUE (indice)
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
}

DEFAULT_SL_POINTS = {
    "Step Index": 50.0,
    "Volatility 100 Index": 700.0,
    "Volatility 25 Index": 200.0,
}

# ==============================================================================
# 4. ORDER BLOCKS — Paramètres FluxCharts
# ==============================================================================

SWING_LEN = 5
MAX_ZONES_TO_TRACK = 3
CANDLE_COUNT_OB = 200

# ✅ Améliorations importées de FluxCharts
POC_BINS = 40               # Nombre de bins pour le POC
MIN_TOUCHES_POC = 1         # Nombre minimum de touches au POC
GAP_FILTER_ENABLED = True   # Filtre anti-gap
OVERLAP_FILTER_ENABLED = True
RETEST_MIN_DELAY_BARS = 4   # Minimum 4 bougies après création pour valider le retest

INVALIDATION_METHOD = "Wick"
RR_RATIO_OB = 2.0
MAX_LIMIT_ORDERS_PER_SYMBOL = 2
MIN_QUALITY_SCORE = 4

RISK_PERCENT_OB = 0.5

VOLUME_LIMITS_OB = {
    "XAUUSD": {"min": 0.01, "max": 0.1},
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
ob_zones = {}
LAST_RETEST_BAR = {}  # {symbol: {"BULL": bar_time, "BEAR": bar_time}}

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
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
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
    above = (swing_low is None) or (current_price > swing_low)
    return wick_rejection and above

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
    below = (swing_high is None) or (current_price < swing_high)
    return wick_rejection and below

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
    if swing_low is not None and float(candles_m15[-1]["close"]) < swing_low:
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
    if swing_high is not None and float(candles_m15[-1]["close"]) > swing_high:
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
    sl = float(spec.get("stopsLevel", 0) or 0)
    fl = float(spec.get("freezeLevel", 0) or 0)
    lvl = max(sl, fl)
    return lvl * (10 ** -digits) if lvl > 0 else 0.0

def validate_stops(spec, entry, sl, tp, digits):
    try:
        md = get_min_stop_distance(spec, digits)
        if md <= 0:
            return sl, tp
        buf = 10 ** -digits
        if abs(entry - sl) < md:
            sl = (entry - md - buf) if sl < entry else (entry + md + buf)
            sl = normalize_price(sl, digits)
        if abs(entry - tp) < md:
            tp = (entry + md + buf) if tp > entry else (entry - md - buf)
            tp = normalize_price(tp, digits)
        return sl, tp
    except Exception:
        return sl, tp

def can_modify_sl(spec, current, new_sl, digits):
    md = get_min_stop_distance(spec, digits)
    if md <= 0:
        return True
    return abs(current - new_sl) >= md

# ==============================================================================
# 16. GESTION POSITIONS STOCH
# ==============================================================================

async def manage_open_positions_stoch(account, connection, symbol):
    try:
        positions = await connection.get_positions()
        sym_pos = [p for p in positions if p.get("symbol") == symbol]
        if not sym_pos:
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

        for pos in sym_pos:
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
            ir = state["initial_risk"]

            if pos_type == "POSITION_TYPE_BUY":
                if (curr_k >= ALERT_OVERBOUGHT or curr_d >= ALERT_OVERBOUGHT) and not state["alerted"]:
                    log(f"⚠️ [{symbol}] ALERTE haute ({curr_k:.1f}/{curr_d:.1f})")
                    state["alerted"] = True
                profit = bid - open_price
                if profit >= ir * BREAK_EVEN_TRIGGER_R_STOCH and current_sl < open_price:
                    be_sl = normalize_price(open_price + min_distance, digits) if min_distance > 0 else open_price
                    log(f"🛡️ [{symbol}] BE BUY #{pos_id} SL={be_sl}")
                    await connection.modify_position(pos_id, stop_loss=be_sl, take_profit=current_tp)
                    current_sl = be_sl
                    state["r_reached"] = True
                if state["r_reached"] and atr_m15 and atr_m15 > 0:
                    new_sl = normalize_price(bid - atr_m15 * ATR_TRAIL_MULTIPLIER_STOCH, digits)
                    step_min = atr_m15 * ATR_MIN_TRAIL_STEP_STOCH
                    if new_sl > current_sl + step_min and can_modify_sl(spec, bid, new_sl, digits):
                        log(f"📈 [{symbol}] Trailing BUY #{pos_id} SL={new_sl}")
                        await connection.modify_position(pos_id, stop_loss=new_sl, take_profit=current_tp)
                        current_sl = new_sl
                if detect_bullish_reversal(candles_m15, stoch_m15):
                    log(f"🔴 [{symbol}] Retournement → Fermeture BUY #{pos_id}")
                    await connection.close_position(pos_id)
                    reset_position_state(symbol)
                    continue

            elif pos_type == "POSITION_TYPE_SELL":
                if (curr_k <= ALERT_OVERSOLD or curr_d <= ALERT_OVERSOLD) and not state["alerted"]:
                    log(f"⚠️ [{symbol}] ALERTE basse ({curr_k:.1f}/{curr_d:.1f})")
                    state["alerted"] = True
                profit = open_price - ask
                if profit >= ir * BREAK_EVEN_TRIGGER_R_STOCH and (current_sl > open_price or current_sl == 0):
                    be_sl = normalize_price(open_price - min_distance, digits) if min_distance > 0 else open_price
                    log(f"🛡️ [{symbol}] BE SELL #{pos_id} SL={be_sl}")
                    await connection.modify_position(pos_id, stop_loss=be_sl, take_profit=current_tp)
                    current_sl = be_sl
                    state["r_reached"] = True
                if state["r_reached"] and atr_m15 and atr_m15 > 0:
                    new_sl = normalize_price(ask + atr_m15 * ATR_TRAIL_MULTIPLIER_STOCH, digits)
                    step_min = atr_m15 * ATR_MIN_TRAIL_STEP_STOCH
                    if new_sl < current_sl - step_min and can_modify_sl(spec, ask, new_sl, digits):
                        log(f"📉 [{symbol}] Trailing SELL #{pos_id} SL={new_sl}")
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
            log(f"🚫 [{symbol}] Croisement haussier ignoré K={curr_k:.1f}")
            return
        if cross_down and curr_k <= ZONE_NEUTRAL_HIGH:
            log(f"🚫 [{symbol}] Croisement baissier ignoré K={curr_k:.1f}")
            return
        sig = "BUY" if cross_up else "SELL"
        if LAST_SIGNAL.get(symbol) == sig:
            log(f"⏸️ [{symbol}] Signal {sig} déjà traité")
            return
        price_info = await connection.get_symbol_price(symbol)
        bid, ask = float(price_info["bid"]), float(price_info["ask"])
        if (ask - bid) > MAX_SPREAD.get(symbol, 999.0):
            log(f"🚫 [{symbol}] Spread trop élevé")
            return
        atr_m15 = calculate_atr(candles_m15, ATR_PERIOD_STOCH)
        if not atr_m15 or atr_m15 <= 0:
            return
        if cross_up and not structure_confirms_buy(candles_m15, ask):
            return
        if cross_down and not structure_confirms_sell(candles_m15, bid):
            return
        volume = get_fixed_lot_stoch(symbol, spec)
        sl_dist = atr_m15 * ATR_SL_MULTIPLIER_STOCH
        tp_dist = sl_dist * RISK_REWARD_RATIO_STOCH
        if cross_up:
            sl_p = normalize_price(ask - sl_dist, digits)
            tp_p = normalize_price(ask + tp_dist, digits)
            sl_p, tp_p = validate_stops(spec, ask, sl_p, tp_p, digits)
            log(f"🚀 [{symbol}] ACHAT | Entry={ask} | SL={sl_p} | TP={tp_p} | Vol={volume}")
            try:
                await connection.create_market_buy_order(symbol=symbol, volume=volume, stop_loss=sl_p, take_profit=tp_p)
                log(f"✅ [{symbol}] ACHAT exécuté")
                LAST_SIGNAL[symbol] = "BUY"
                reset_position_state(symbol)
            except Exception as e:
                log(f"❌ [{symbol}] ACHAT erreur : {e}")
        elif cross_down:
            sl_p = normalize_price(bid + sl_dist, digits)
            tp_p = normalize_price(bid - tp_dist, digits)
            sl_p, tp_p = validate_stops(spec, bid, sl_p, tp_p, digits)
            log(f"🔻 [{symbol}] VENTE | Entry={bid} | SL={sl_p} | TP={tp_p} | Vol={volume}")
            try:
                await connection.create_market_sell_order(symbol=symbol, volume=volume, stop_loss=sl_p, take_profit=tp_p)
                log(f"✅ [{symbol}] VENTE exécutée")
                LAST_SIGNAL[symbol] = "SELL"
                reset_position_state(symbol)
            except Exception as e:
                log(f"❌ [{symbol}] VENTE erreur : {e}")
    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse Stoch : {e}")

# ==============================================================================
# 18. ORDER BLOCK — SWINGS + BOS (FluxCharts style)
# ==============================================================================

def find_swing_highs_lows(candles, swing_len):
    highs, lows = [], []
    n = len(candles)
    if n < swing_len * 2 + 1:
        return highs, lows
    for i in range(swing_len, n - swing_len):
        is_sh = all(float(candles[i]["high"]) > float(candles[i - j]["high"]) and float(candles[i]["high"]) > float(candles[i + j]["high"]) for j in range(1, swing_len + 1))
        if is_sh:
            highs.append({"index": i, "price": float(candles[i]["high"])})
        is_sl = all(float(candles[i]["low"]) < float(candles[i - j]["low"]) and float(candles[i]["low"]) < float(candles[i + j]["low"]) for j in range(1, swing_len + 1))
        if is_sl:
            lows.append({"index": i, "price": float(candles[i]["low"])})
    return highs, lows

def detect_bos(candles, swing_highs, swing_lows):
    bos_list = []
    n = len(candles)
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

# ==============================================================================
# 19. POC — Point Of Control (cœur de FluxCharts)
# ==============================================================================

def find_poc(candles, from_idx, to_idx, n_bins=POC_BINS):
    """
    Trouve le POC (prix le plus touché) entre deux indices.
    Retourne (poc_price, touches_count) ou (None, 0).
    """
    if from_idx >= to_idx or from_idx < 0 or to_idx >= len(candles):
        return None, 0

    # Bornes de prix
    min_p = float('inf')
    max_p = float('-inf')
    for i in range(from_idx, to_idx + 1):
        lo = float(candles[i]["low"])
        hi = float(candles[i]["high"])
        if lo < min_p:
            min_p = lo
        if hi > max_p:
            max_p = hi

    if min_p >= max_p:
        return None, 0

    step = (max_p - min_p) / n_bins
    if step <= 0:
        step = 1e-9

    # Différence counts
    diff = [0.0] * (n_bins + 1)
    for i in range(from_idx, to_idx + 1):
        lo = float(candles[i]["low"])
        hi = float(candles[i]["high"])
        s_bin = int((lo - min_p) / step)
        e_bin = int((hi - min_p) / step)
        s_bin = max(0, min(n_bins - 1, s_bin))
        e_bin = max(0, min(n_bins - 1, e_bin))
        diff[s_bin] += 1.0
        if e_bin + 1 < n_bins:
            diff[e_bin + 1] -= 1.0

    # Find best bin
    best_bin = 0
    best_cnt = 0.0
    run = 0.0
    for i in range(n_bins):
        run += diff[i]
        if run > best_cnt:
            best_cnt = run
            best_bin = i

    poc_price = min_p + (best_bin + 0.5) * step
    return poc_price, int(best_cnt)

def count_touches_poc(candles, from_idx, to_idx, poc):
    """
    Compte combien de bougies touchent le POC entre from_idx et to_idx.
    """
    if poc is None:
        return 0
    count = 0
    for i in range(from_idx, to_idx + 1):
        if float(candles[i]["low"]) <= poc <= float(candles[i]["high"]):
            count += 1
    return count

def find_best_poc_candle(candles, from_idx, to_idx, poc, is_bull):
    """
    FluxCharts : trouve la bougie qui touche le POC ET a le plus haut/low extrême
    en partant de la fin (recherche inversée).
    """
    if poc is None or from_idx >= to_idx:
        return None

    best_idx = None
    run_extreme = None

    for i in range(to_idx, from_idx - 1, -1):
        lo = float(candles[i]["low"])
        hi = float(candles[i]["high"])
        touches = lo <= poc <= hi

        if is_bull:
            if run_extreme is None or lo < run_extreme:
                run_extreme = lo
            if touches and lo == run_extreme:
                best_idx = i
        else:
            if run_extreme is None or hi > run_extreme:
                run_extreme = hi
            if touches and hi == run_extreme:
                best_idx = i

    return best_idx

# ==============================================================================
# 20. GAP FILTER (FluxCharts)
# ==============================================================================

def has_gap_between(candles, anchor_idx, bos_idx, is_bull):
    """Vérifie si un gap existe entre la zone d'ancrage et le BOS."""
    from_i = min(anchor_idx, bos_idx)
    to_i = max(anchor_idx, bos_idx)
    if to_i - from_i < 1:
        return False

    for i in range(from_i + 1, to_i + 1):
        prev_hi = float(candles[i - 1]["high"])
        prev_lo = float(candles[i - 1]["low"])
        now_hi = float(candles[i]["high"])
        now_lo = float(candles[i]["low"])
        if is_bull:
            if now_lo > prev_hi:
                return True
        else:
            if now_hi < prev_lo:
                return True
    return False

# ==============================================================================
# 21. OVERLAP FILTER (FluxCharts)
# ==============================================================================

def ob_overlaps_active(zones, top, bottom):
    """Vérifie si une zone chevauche une zone active existante."""
    z_top = max(top, bottom)
    z_bot = min(top, bottom)

    for z in zones:
        if not z.get("active", False):
            continue
        o_top = max(z["top"], z["bottom"])
        o_bot = min(z["top"], z["bottom"])
        if z_top >= o_bot and z_bot <= o_top:
            return True
    return False

# ==============================================================================
# 22. CRÉATION ZONE OB (méthode POC FluxCharts)
# ==============================================================================

def create_ob_zone_poc(candles, bos, existing_zones):
    """
    Crée une zone OB à partir du POC (FluxCharts).
    Retourne la zone ou None.
    """
    bos_idx = bos["bos_index"]
    swing_idx = bos["swing_index"]
    is_bull = bos["type"] == "BULL"

    if bos_idx <= swing_idx:
        return None

    # 1. Trouve le POC
    poc, approx_touches = find_poc(candles, swing_idx, bos_idx)
    if poc is None:
        return None

    # 2. Compte les touches réelles
    touches = count_touches_poc(candles, swing_idx, bos_idx, poc)
    if touches < MIN_TOUCHES_POC:
        return None

    # 3. Trouve la bougie d'ancrage
    anchor_idx = find_best_poc_candle(candles, swing_idx, bos_idx, poc, is_bull)
    if anchor_idx is None:
        return None

    # 4. GAP FILTER
    if GAP_FILTER_ENABLED and has_gap_between(candles, anchor_idx, bos_idx, is_bull):
        log(f"⏭️ Gap détecté entre anchor {anchor_idx} et BOS {bos_idx} — zone refusée")
        return None

    # 5. Zone
    top = float(candles[anchor_idx]["high"])
    bottom = float(candles[anchor_idx]["low"])
    if top <= bottom:
        return None

    # 6. OVERLAP FILTER
    if OVERLAP_FILTER_ENABLED and ob_overlaps_active(existing_zones, top, bottom):
        log(f"⏭️ Zone [{bottom}-{top}] chevauche une zone active — refusée")
        return None

    # 7. Score de qualité
    quality = calculate_quality_score(candles, bos, anchor_idx, top, bottom)

    return {
        "type": bos["type"], "top": top, "bottom": bottom,
        "anchor_index": anchor_idx, "bos_index": bos_idx,
        "poc": poc, "touches": touches,
        "active": True, "retested": False,
        "created_index": bos_idx,
        "order_placed": False, "order_id": None,
        "quality_score": quality,
    }

def calculate_quality_score(candles, bos, anchor_idx, top, bottom):
    score = 0
    bos_idx = bos["bos_index"]
    zone_type = bos["type"]
    zone_size = top - bottom
    if zone_size <= 0:
        return 0

    # 1. Impulsion
    if abs(float(candles[bos_idx]["close"]) - bos["swing_price"]) >= zone_size * 2.0:
        score += 1

    # 2. Volume moyen
    vols = [float(c.get("volume", 0) or 0) for c in candles[-50:]]
    avg_v = sum(vols) / len(vols) if vols else 0
    if avg_v > 0 and float(candles[anchor_idx].get("volume", 0) or 0) >= avg_v * 1.3:
        score += 1

    # 3. Taille équilibrée
    ranges = [float(c["high"]) - float(c["low"]) for c in candles[-20:]]
    avg_r = sum(ranges) / len(ranges) if ranges else 0
    if avg_r > 0 and 0.5 <= zone_size / avg_r <= 2.0:
        score += 1

    # 4. BOS clair
    body = abs(float(candles[bos_idx]["close"]) - float(candles[bos_idx]["open"]))
    rng = float(candles[bos_idx]["high"]) - float(candles[bos_idx]["low"])
    if rng > 0 and body / rng >= 0.6:
        score += 1

    # 5. Fraîcheur
    if len(candles) - 1 - bos_idx <= 30:
        score += 1

    # 6. Non mitiée
    pen = 0
    for i in range(bos_idx + 1, len(candles)):
        ch, cl = float(candles[i]["high"]), float(candles[i]["low"])
        if ch >= bottom and cl <= top and min(ch, top) - max(cl, bottom) > zone_size * 0.5:
            pen += 1
    if pen == 0:
        score += 1

    # 7. Alignement tendance
    closes = [float(c["close"]) for c in candles]
    if len(closes) >= 50:
        ema = sum(closes[-50:]) / 50
        if (zone_type == "BULL" and closes[-1] > ema) or (zone_type == "BEAR" and closes[-1] < ema):
            score += 1

    return score

# ==============================================================================
# 23. RETEST (FluxCharts strict)
# ==============================================================================

def check_retest_strict(candles, zone):
    """
    Retest strict : la bougie précédente ouvre ET clôture au-delà de la zone
    ET la mèche touche la zone. Minimum RETEST_MIN_DELAY_BARS après création.
    """
    if not zone.get("active", False):
        return False

    if len(candles) < 3:
        return False

    last = candles[-1]
    prev = candles[-2]
    curr_idx = len(candles) - 1

    # Doit être suffisamment âgé
    if curr_idx - zone["created_index"] < RETEST_MIN_DELAY_BARS:
        return False

    if zone["type"] == "BULL":
        # Bougie précédente : open > top ET close > top ET low touche la zone
        opens_above = float(prev["open"]) > zone["top"]
        closes_above = float(prev["close"]) > zone["top"]
        wick_touches = zone["bottom"] <= float(prev["low"]) <= zone["top"]
        return opens_above and closes_above and wick_touches
    else:
        opens_below = float(prev["open"]) < zone["bottom"]
        closes_below = float(prev["close"]) < zone["bottom"]
        wick_touches = zone["bottom"] <= float(prev["high"]) <= zone["top"]
        return opens_below and closes_below and wick_touches

# ==============================================================================
# 24. MISE À JOUR ZONES
# ==============================================================================

def update_zones(candles, zones, symbol):
    if not zones:
        return zones
    last = candles[-1]
    lh, ll, lc = float(last["high"]), float(last["low"]), float(last["close"])
    for z in zones:
        if not z["active"]:
            continue

        # Invalidation
        inv = False
        if z["type"] == "BULL":
            inv = ll < z["bottom"] if INVALIDATION_METHOD == "Wick" else lc < z["bottom"]
        else:
            inv = lh > z["top"] if INVALIDATION_METHOD == "Wick" else lc > z["top"]
        if inv:
            z["active"] = False
            log(f"🔴 [{symbol}] Zone OB {z['type']} invalidée")

        # Retest strict
        if not z.get("retested", False) and check_retest_strict(candles, z):
            z["retested"] = True
            log(f"🔄 [{symbol}] Retest validé sur zone OB {z['type']}")
    return zones

# ==============================================================================
# 25. ORDER BLOCK — VOLUME + PLACEMENT
# ==============================================================================

async def calculate_volume_ob(connection, symbol, entry, sl):
    try:
        info = await connection.get_account_information()
        bal = float(info.get("balance", 0))
        if bal <= 0:
            return None
        spec = await connection.get_symbol_specification(symbol)
        mn = float(spec.get("minVolume", 0.01))
        mx = float(spec.get("maxVolume", 100))
        st = float(spec.get("volumeStep", 0.01))
        cs = float(spec.get("contractSize", spec.get("tradeContractSize", 1)))
        if cs <= 0:
            return None
        if symbol in VOLUME_LIMITS_OB:
            mn = max(mn, VOLUME_LIMITS_OB[symbol]["min"])
            mx = min(mx, VOLUME_LIMITS_OB[symbol]["max"])
        rm = bal * RISK_PERCENT_OB / 100
        pd = abs(entry - sl)
        if pd <= 0:
            return None
        rpl = pd * cs
        if rpl <= 0:
            return None
        return normalize_volume(rm / rpl, mn, mx, st)
    except Exception as e:
        log(f"[{symbol}] Erreur volume OB : {e}")
        return None

async def count_active_limit_orders(connection, symbol):
    try:
        orders = await connection.get_orders()
        return sum(1 for o in orders if o.get("symbol") == symbol and o.get("type", "") in ("ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_SELL_LIMIT"))
    except Exception:
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
        if not volume or volume <= 0:
            return None
        log(f"🟢 [{symbol}] {'BUY' if zone['type']=='BULL' else 'SELL'} LIMIT | Entry={entry} | SL={sl} | TP={tp} | Vol={volume} | Score={zone['quality_score']}/7 | POC={zone.get('poc')}")
        if zone["type"] == "BULL":
            order = await connection.create_limit_buy_order(symbol=symbol, volume=volume, open_price=entry, stop_loss=sl, take_profit=tp)
        else:
            order = await connection.create_limit_sell_order(symbol=symbol, volume=volume, open_price=entry, stop_loss=sl, take_profit=tp)
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
        for z in ob_zones[symbol]:
            oid = z.get("order_id")
            if oid and oid not in active_ids:
                z["order_placed"] = False
                z["order_id"] = None
    except Exception:
        pass

# ==============================================================================
# 26. GESTION POSITIONS OB
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
                            log(f"📈 [{symbol}] Trailing BUY SL {sl} → {new_sl}")
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
                            log(f"📉 [{symbol}] Trailing SELL SL {sl} → {new_sl}")
                            await connection.modify_position(pos["id"], stop_loss=new_sl, take_profit=pos.get("takeProfit"))
            except Exception as e:
                log(f"[{symbol}] Erreur OB : {e}")
    except Exception as e:
        log(f"Erreur OB globale : {e}")

# ==============================================================================
# 27. ANALYSE OB
# ==============================================================================

async def analyze_ob(account, connection, symbol):
    try:
        await cleanup_zones(connection, symbol)
        candles = await get_candles(account, symbol, TIMEFRAME_ENTRY, CANDLE_COUNT_OB)
        if not candles or len(candles) < SWING_LEN * 3:
            return
        sh, sl = find_swing_highs_lows(candles, SWING_LEN)
        bos_list = detect_bos(candles, sh, sl)
        if symbol not in ob_zones:
            ob_zones[symbol] = []
        recent = bos_list[-5:] if len(bos_list) >= 5 else bos_list
        for bos in recent:
            z = create_ob_zone_poc(candles, bos, ob_zones[symbol])
            if z is None:
                continue
            if any(zz.get("anchor_index") == z["anchor_index"] and zz["type"] == z["type"] for zz in ob_zones[symbol]):
                continue
            ob_zones[symbol].append(z)
            log(f"🆕 [{symbol}] Zone OB POC {z['type']} top={z['top']} bot={z['bottom']} POC={z.get('poc')} score={z['quality_score']}/7")
        ob_zones[symbol] = update_zones(candles, ob_zones[symbol], symbol)
        active = [z for z in ob_zones[symbol] if z["active"]]
        last_close = float(candles[-1]["close"])
        def dist(z):
            if last_close > z["top"]: return last_close - z["top"]
            if last_close < z["bottom"]: return z["bottom"] - last_close
            return 0.0
        active.sort(key=lambda z: (-z.get("quality_score", 0), dist(z)))
        active = active[:MAX_ZONES_TO_TRACK]
        n_orders = await count_active_limit_orders(connection, symbol)
        log(f"[{symbol}] OB zones={len(active)} | Ordres LIMIT={n_orders}/{MAX_LIMIT_ORDERS_PER_SYMBOL}")
        for z in active:
            if n_orders >= MAX_LIMIT_ORDERS_PER_SYMBOL:
                break
            if z.get("order_placed", False):
                continue
            # ⚠️ ORDRE LIMIT placé dès création de zone (sans attendre retest)
            r = await place_limit_order_ob(connection, symbol, z)
            if r is not None:
                n_orders += 1
    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse OB : {e}")

# ==============================================================================
# 28. HEALTH CHECK
# ==============================================================================

async def health_check_server():
    port = int(os.getenv("PORT", 10000))
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"Arkas Bot OK"
            resp = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: " +
                    str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            writer.write(resp)
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
# 29. BOUCLE
# ==============================================================================

async def trading_loop(account, connection):
    while True:
        try:
            for s in STOCH_SYMBOLS:
                await analyze_stoch(account, connection, s)
                await asyncio.sleep(1)
            for s in OB_SYMBOLS:
                await analyze_ob(account, connection, s)
                await asyncio.sleep(1)
            await manage_positions_ob(connection)
            await asyncio.sleep(SCAN_INTERVAL)
        except Exception as e:
            log(f"⚠️ Erreur boucle : {e}")
            await asyncio.sleep(10)

# ==============================================================================
# 30. MAIN
# ==============================================================================

async def main():
    print("=" * 60, flush=True)
    print("🚀 ARKAS BOT — STOCH + OB POC (FluxCharts amélioré)", flush=True)
    print("=" * 60, flush=True)
    print(f"STOCH : {', '.join(STOCH_SYMBOLS)}", flush=True)
    print(f"OB    : {', '.join(OB_SYMBOLS)}", flush=True)
    print(f"Timeframe : {TIMEFRAME_ENTRY}", flush=True)
    print(f"POC bins : {POC_BINS} | Gap filter : {GAP_FILTER_ENABLED} | Overlap filter : {OVERLAP_FILTER_ENABLED}", flush=True)
    print(f"Région : {METAAPI_REGION}", flush=True)
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
        log("🟢 BOT CONNECTÉ")
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
