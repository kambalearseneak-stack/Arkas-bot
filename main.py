import asyncio
import os
import sys
import math
from datetime import datetime, timezone

from metaapi_cloud_sdk import MetaApi

# ==============================================================================
# ARKAS BOT — PA Multi-Critères (Step Index) + OB 5★ Fibo (XAUUSD)
# Score ≥ 3 → Entrée LIMIT au prix du critère le plus fort
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

PA_SYMBOLS = ["Step Index"]
OB_SYMBOLS = ["XAUUSD"]

TIMEFRAME_ENTRY = "15m"

# ==============================================================================
# 3. PRICE ACTION MULTI-CRITÈRES (Step Index)
# ==============================================================================

PA_EMA_PERIOD = 50
PA_STRUCTURE_LOOKBACK = 30
PA_SWING_PIVOT_LEN = 3

PA_TRENDLINE_ENABLED = True
PA_TRENDLINE_MIN_SLOPE = 0.00001

PA_BREAKOUT_BODY_RATIO = 0.6
PA_BREAKOUT_MIN_MARGIN = 0.3
PA_FIBO_TOLERANCE = 0.25

PA_ATR_PERIOD = 14
PA_ATR_SL_MULTIPLIER = 2.0
PA_ATR_TRAIL_MULTIPLIER = 1.0
PA_BE_TRIGGER_R = 1.0
PA_RR_RATIO = 2.0

# ✅ Score minimum
PA_MIN_SCORE = 3

LOT_PER_PA_SYMBOL = {
    "Step Index": 0.1,
}

MAX_SPREAD = {
    "Step Index": 2.0,
    "XAUUSD": 5.0,
}

DEFAULT_SL_POINTS = {
    "Step Index": 50.0,
}

# ==============================================================================
# 4. ORDER BLOCKS (XAUUSD)
# ==============================================================================

SWING_LEN = 5
MAX_ZONES_TO_TRACK = 3
CANDLE_COUNT_OB = 200

POC_BINS = 40
MIN_TOUCHES_POC = 1
GAP_FILTER_ENABLED = True
OVERLAP_FILTER_ENABLED = True

INVALIDATION_METHOD = "Wick"
RR_RATIO_OB = 2.0
MAX_LIMIT_ORDERS_PER_SYMBOL = 2

OB_MAX_SIZE_ATR_MULT = 1.0
OB_MIN_VOLUME_RATIO = 1.3
OB_MAX_BOS_AGE = 20
OB_MAX_PENETRATION_PCT = 0.5
OB_EMA_PERIOD = 50

OB_MIN_SCORE = 3

FIBO_LEVEL_BY_SCORE = {
    5: 0.786,
    4: 0.618,
    3: 0.5,
}

SL_BUFFER_FACTOR = 0.5
RISK_PERCENT_OB = 0.5

VOLUME_LIMITS_OB = {
    "XAUUSD": {"min": 0.01, "max": 0.1},
}

BREAK_EVEN_R_OB = 1.0
TRAILING_ENABLED_OB = True
TRAILING_START_R_OB = 1.5
TRAILING_DISTANCE_R_OB = 0.7

MAX_ORDER_AGE_HOURS = 4
MAX_DISTANCE_FACTOR = 3.0

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

def reset_position_state(symbol):
    POSITION_STATE[symbol] = {
        "r_reached": False,
        "alerted": False,
        "initial_risk": 0.0,
        "trailing_active": False,
    }

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
# 8. ATR
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
# 9. EMA
# ==============================================================================

def calculate_ema(candles, period):
    if not candles or len(candles) < period:
        return None
    closes = [float(c["close"]) for c in candles]
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
    return ema

# ==============================================================================
# 10. BOUGIES
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
# 11. VÉRIF SYMBOLE
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
# 12. STOPS
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
# 13. PRICE ACTION — Swings
# ==============================================================================

def find_pa_swings(candles, lookback=PA_STRUCTURE_LOOKBACK, pivot_len=PA_SWING_PIVOT_LEN):
    if not candles or len(candles) < lookback:
        return [], []
    recent = candles[-lookback:]
    highs = []
    lows = []
    for i in range(pivot_len, len(recent) - pivot_len):
        is_sh = True
        for j in range(1, pivot_len + 1):
            if float(recent[i]["high"]) <= float(recent[i - j]["high"]) or float(recent[i]["high"]) <= float(recent[i + j]["high"]):
                is_sh = False
                break
        if is_sh:
            highs.append({
                "index": len(candles) - lookback + i,
                "price": float(recent[i]["high"]),
            })
        is_sl = True
        for j in range(1, pivot_len + 1):
            if float(recent[i]["low"]) >= float(recent[i - j]["low"]) or float(recent[i]["low"]) >= float(recent[i + j]["low"]):
                is_sl = False
                break
        if is_sl:
            lows.append({
                "index": len(candles) - lookback + i,
                "price": float(recent[i]["low"]),
            })
    return highs, lows

# ==============================================================================
# 14. PRICE ACTION — Trendlines
# ==============================================================================

def build_trendline_bull_pa(candles, swings_lows):
    if len(swings_lows) < 2:
        return None
    p1 = swings_lows[-2]
    p2 = swings_lows[-1]
    if p2["index"] <= p1["index"]:
        return None
    slope = (p2["price"] - p1["price"]) / (p2["index"] - p1["index"])
    if slope <= PA_TRENDLINE_MIN_SLOPE:
        return None
    current_idx = len(candles) - 1
    price_at_current = p2["price"] + slope * (current_idx - p2["index"])
    return price_at_current, slope

def build_trendline_bear_pa(candles, swings_highs):
    if len(swings_highs) < 2:
        return None
    p1 = swings_highs[-2]
    p2 = swings_highs[-1]
    if p2["index"] <= p1["index"]:
        return None
    slope = (p2["price"] - p1["price"]) / (p2["index"] - p1["index"])
    if slope >= -PA_TRENDLINE_MIN_SLOPE:
        return None
    current_idx = len(candles) - 1
    price_at_current = p2["price"] + slope * (current_idx - p2["index"])
    return price_at_current, slope

# ==============================================================================
# 15. ✅ PA — Détection de TOUS les critères indépendants
# ==============================================================================

def detect_all_pa_entries(candles):
    """
    Détecte TOUS les critères PA avec leur propre entry/score.
    Retourne une liste de dicts {critere, direction, entry, sl, score, raison}
    """
    entries = []
    
    if not candles or len(candles) < PA_EMA_PERIOD + 10:
        return entries
    
    atr = calculate_atr(candles, PA_ATR_PERIOD)
    if not atr or atr <= 0:
        return entries
    
    current_close = float(candles[-1]["close"])
    current_high = float(candles[-1]["high"])
    current_low = float(candles[-1]["low"])
    current_open = float(candles[-1]["open"])
    
    ema = calculate_ema(candles, PA_EMA_PERIOD)
    swings_highs, swings_lows = find_pa_swings(candles)
    
    tolerance = atr * PA_FIBO_TOLERANCE
    
    # =========================================================================
    # CRITÈRE 1 : RETEST DU NIVEAU CASSÉ (+3)
    # =========================================================================
    if swings_highs:
        last_sh = swings_highs[-1]
        bars_since = len(candles) - 1 - last_sh["index"]
        if 0 <= bars_since <= 15:
            if current_low <= last_sh["price"] + tolerance and current_close > last_sh["price"]:
                entries.append({
                    "critere": "Retest niveau cassé",
                    "direction": "BUY",
                    "entry": last_sh["price"],
                    "sl": last_sh["price"] - atr * PA_ATR_SL_MULTIPLIER,
                    "score": 3,
                    "raison": f"Retest du swing high {last_sh['price']:.2f}"
                })
    
    if swings_lows:
        last_sl = swings_lows[-1]
        bars_since = len(candles) - 1 - last_sl["index"]
        if 0 <= bars_since <= 15:
            if current_high >= last_sl["price"] - tolerance and current_close < last_sl["price"]:
                entries.append({
                    "critere": "Retest niveau cassé",
                    "direction": "SELL",
                    "entry": last_sl["price"],
                    "sl": last_sl["price"] + atr * PA_ATR_SL_MULTIPLIER,
                    "score": 3,
                    "raison": f"Retest du swing low {last_sl['price']:.2f}"
                })
    
    # =========================================================================
    # CRITÈRE 2 : CASSURE DIRECTE (+3)
    # =========================================================================
    body = abs(current_close - current_open)
    rng = current_high - current_low
    
    if rng > 0 and (body / rng) >= PA_BREAKOUT_BODY_RATIO:
        margin_min = atr * PA_BREAKOUT_MIN_MARGIN
        
        if swings_highs:
            last_sh = swings_highs[-1]
            if current_close > last_sh["price"] + margin_min:
                prev_close = float(candles[-2]["close"])
                if prev_close <= last_sh["price"]:
                    entries.append({
                        "critere": "Cassure directe",
                        "direction": "BUY",
                        "entry": current_close,
                        "sl": last_sh["price"] - atr * 0.5,
                        "score": 3,
                        "raison": f"Cassure nette de {last_sh['price']:.2f}"
                    })
        
        if swings_lows:
            last_sl = swings_lows[-1]
            if current_close < last_sl["price"] - margin_min:
                prev_close = float(candles[-2]["close"])
                if prev_close >= last_sl["price"]:
                    entries.append({
                        "critere": "Cassure directe",
                        "direction": "SELL",
                        "entry": current_close,
                        "sl": last_sl["price"] + atr * 0.5,
                        "score": 3,
                        "raison": f"Cassure nette de {last_sl['price']:.2f}"
                    })
    
    # =========================================================================
    # CRITÈRE 3 : FIBO 0.618 (+2)
    # =========================================================================
    if swings_highs and swings_lows:
        last_sh = swings_highs[-1]
        last_sl = swings_lows[-1]
        
        if last_sh["index"] > last_sl["index"]:
            swing_range = last_sh["price"] - last_sl["price"]
            if swing_range > 0:
                fibo_618 = last_sh["price"] - swing_range * 0.618
                if abs(current_close - fibo_618) <= tolerance:
                    entries.append({
                        "critere": "Fibo 0.618",
                        "direction": "BUY",
                        "entry": fibo_618,
                        "sl": last_sh["price"] - swing_range * 0.786,
                        "score": 2,
                        "raison": f"Retracement 0.618 à {fibo_618:.2f}"
                    })
        else:
            swing_range = last_sh["price"] - last_sl["price"]
            if swing_range > 0:
                fibo_618 = last_sl["price"] + swing_range * 0.618
                if abs(current_close - fibo_618) <= tolerance:
                    entries.append({
                        "critere": "Fibo 0.618",
                        "direction": "SELL",
                        "entry": fibo_618,
                        "sl": last_sl["price"] + swing_range * 0.786,
                        "score": 2,
                        "raison": f"Retracement 0.618 à {fibo_618:.2f}"
                    })
    
    # =========================================================================
    # CRITÈRE 4 : FIBO 0.786 (+2)
    # =========================================================================
    if swings_highs and swings_lows:
        last_sh = swings_highs[-1]
        last_sl = swings_lows[-1]
        
        if last_sh["index"] > last_sl["index"]:
            swing_range = last_sh["price"] - last_sl["price"]
            if swing_range > 0:
                fibo_786 = last_sh["price"] - swing_range * 0.786
                if abs(current_close - fibo_786) <= tolerance:
                    entries.append({
                        "critere": "Fibo 0.786",
                        "direction": "BUY",
                        "entry": fibo_786,
                        "sl": last_sh["price"] - swing_range * 0.95,
                        "score": 2,
                        "raison": f"Retracement 0.786 à {fibo_786:.2f}"
                    })
        else:
            swing_range = last_sh["price"] - last_sl["price"]
            if swing_range > 0:
                fibo_786 = last_sl["price"] + swing_range * 0.786
                if abs(current_close - fibo_786) <= tolerance:
                    entries.append({
                        "critere": "Fibo 0.786",
                        "direction": "SELL",
                        "entry": fibo_786,
                        "sl": last_sl["price"] + swing_range * 0.95,
                        "score": 2,
                        "raison": f"Retracement 0.786 à {fibo_786:.2f}"
                    })
    
    # =========================================================================
    # CRITÈRE 5 : RETEST TRENDLINE (+2)
    # =========================================================================
    if PA_TRENDLINE_ENABLED:
        tl_bull = build_trendline_bull_pa(candles, swings_lows)
        if tl_bull:
            tl_price, slope = tl_bull
            if abs(current_low - tl_price) <= tolerance and current_close > tl_price:
                entries.append({
                    "critere": "Retest trendline bull",
                    "direction": "BUY",
                    "entry": tl_price,
                    "sl": tl_price - atr * PA_ATR_SL_MULTIPLIER,
                    "score": 2,
                    "raison": f"Retest trendline haussière à {tl_price:.2f}"
                })
        
        tl_bear = build_trendline_bear_pa(candles, swings_highs)
        if tl_bear:
            tl_price, slope = tl_bear
            if abs(current_high - tl_price) <= tolerance and current_close < tl_price:
                entries.append({
                    "critere": "Retest trendline bear",
                    "direction": "SELL",
                    "entry": tl_price,
                    "sl": tl_price + atr * PA_ATR_SL_MULTIPLIER,
                    "score": 2,
                    "raison": f"Retest trendline baissière à {tl_price:.2f}"
                })
    
    # =========================================================================
    # CRITÈRE 6 : RETEST EMA 50 (+1)
    # =========================================================================
    if ema:
        if abs(current_low - ema) <= tolerance and current_close > ema:
            entries.append({
                "critere": "Retest EMA 50",
                "direction": "BUY",
                "entry": ema,
                "sl": ema - atr * PA_ATR_SL_MULTIPLIER,
                "score": 1,
                "raison": f"Retest EMA 50 à {ema:.2f}"
            })
        
        if abs(current_high - ema) <= tolerance and current_close < ema:
            entries.append({
                "critere": "Retest EMA 50",
                "direction": "SELL",
                "entry": ema,
                "sl": ema + atr * PA_ATR_SL_MULTIPLIER,
                "score": 1,
                "raison": f"Retest EMA 50 à {ema:.2f}"
            })
    
    return entries

# ==============================================================================
# 16. PRICE ACTION — Gestion positions
# ==============================================================================

async def manage_positions_pa(account, connection, symbol):
    try:
        positions = await connection.get_positions()
        sym_pos = [p for p in positions if p.get("symbol") == symbol]
        if not sym_pos:
            reset_position_state(symbol)
            return
        
        candles = await get_candles(account, symbol, TIMEFRAME_ENTRY)
        if not candles:
            return
        
        atr = calculate_atr(candles, PA_ATR_PERIOD)
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
                elif atr:
                    state["initial_risk"] = atr * PA_ATR_SL_MULTIPLIER
                else:
                    state["initial_risk"] = DEFAULT_SL_POINTS.get(symbol, 100.0)
            ir = state["initial_risk"]
            
            if pos_type == "POSITION_TYPE_BUY":
                profit = bid - open_price
                if profit >= ir * PA_BE_TRIGGER_R and current_sl < open_price:
                    be_sl = normalize_price(open_price + min_distance, digits) if min_distance > 0 else open_price
                    log(f"🛡️ [{symbol}] +1R → BE BUY #{pos_id} SL={be_sl}")
                    await connection.modify_position(pos_id, stop_loss=be_sl, take_profit=current_tp)
                    current_sl = be_sl
                    state["r_reached"] = True
                
                if state["r_reached"] and atr and atr > 0:
                    new_sl = normalize_price(bid - atr * PA_ATR_TRAIL_MULTIPLIER, digits)
                    if new_sl > current_sl and can_modify_sl(spec, bid, new_sl, digits):
                        log(f"📈 [{symbol}] Trailing BUY #{pos_id} SL={new_sl}")
                        await connection.modify_position(pos_id, stop_loss=new_sl, take_profit=current_tp)
                        current_sl = new_sl
            
            elif pos_type == "POSITION_TYPE_SELL":
                profit = open_price - ask
                if profit >= ir * PA_BE_TRIGGER_R and (current_sl > open_price or current_sl == 0):
                    be_sl = normalize_price(open_price - min_distance, digits) if min_distance > 0 else open_price
                    log(f"🛡️ [{symbol}] +1R → BE SELL #{pos_id} SL={be_sl}")
                    await connection.modify_position(pos_id, stop_loss=be_sl, take_profit=current_tp)
                    current_sl = be_sl
                    state["r_reached"] = True
                
                if state["r_reached"] and atr and atr > 0:
                    new_sl = normalize_price(ask + atr * PA_ATR_TRAIL_MULTIPLIER, digits)
                    if new_sl < current_sl and can_modify_sl(spec, ask, new_sl, digits):
                        log(f"📉 [{symbol}] Trailing SELL #{pos_id} SL={new_sl}")
                        await connection.modify_position(pos_id, stop_loss=new_sl, take_profit=current_tp)
                        current_sl = new_sl
    except Exception as e:
        log(f"❌ [{symbol}] Erreur gestion PA : {e}")

# ==============================================================================
# 17. PRICE ACTION — Analyse principale
# ==============================================================================

async def analyze_pa(account, connection, symbol):
    try:
        tradable, spec = await is_symbol_tradable(connection, symbol)
        if not tradable or not spec:
            return
        
        digits = int(spec.get("digits", 2))
        
        positions = await connection.get_positions()
        active = [p for p in positions if p.get("symbol") == symbol]
        if len(active) >= 1:
            await manage_positions_pa(account, connection, symbol)
            return
        
        candles = await get_candles(account, symbol, TIMEFRAME_ENTRY)
        if not candles or len(candles) < PA_EMA_PERIOD + 10:
            return
        
        atr = calculate_atr(candles, PA_ATR_PERIOD)
        if not atr or atr <= 0:
            return
        
        # ✅ Détection de TOUS les critères
        entries = detect_all_pa_entries(candles)
        
        if not entries:
            log(f"🔍 [{symbol}] PA : aucun critère détecté")
            return
        
        # Log de tous les critères détectés
        buy_entries = [e for e in entries if e["direction"] == "BUY"]
        sell_entries = [e for e in entries if e["direction"] == "SELL"]
        
        score_buy = sum(e["score"] for e in buy_entries)
        score_sell = sum(e["score"] for e in sell_entries)
        
        log(f"📊 [{symbol}] Critères PA détectés :")
        for e in entries:
            log(f"     • {e['critere']} ({e['direction']}) +{e['score']} pts — {e['raison']}")
        log(f"     → BUY total = {score_buy} pts | SELL total = {score_sell} pts")
        
        # ✅ Décision : prendre la direction avec le meilleur score
        if score_buy >= score_sell and score_buy >= PA_MIN_SCORE:
            best_direction = "BUY"
            best_entries = buy_entries
            total_score = score_buy
        elif score_sell > score_buy and score_sell >= PA_MIN_SCORE:
            best_direction = "SELL"
            best_entries = sell_entries
            total_score = score_sell
        else:
            log(f"⏸️ [{symbol}] Score total {max(score_buy, score_sell)} < {PA_MIN_SCORE} → ignoré")
            return
        
        # Anti-répétition
        if LAST_SIGNAL.get(symbol) == best_direction:
            log(f"⏸️ [{symbol}] Signal {best_direction} déjà traité")
            return
        
        # ✅ Choisir le critère avec le score max
        best_entries.sort(key=lambda e: -e["score"])
        best_entry = best_entries[0]
        
        # Prix + spread
        price_info = await connection.get_symbol_price(symbol)
        bid, ask = float(price_info["bid"]), float(price_info["ask"])
        
        if (ask - bid) > MAX_SPREAD.get(symbol, 999.0):
            log(f"🚫 [{symbol}] Spread trop élevé")
            return
        
        volume = normalize_volume(
            LOT_PER_PA_SYMBOL.get(symbol, 0.01),
            float(spec.get("minVolume", 0.01)),
            float(spec.get("maxVolume", 100)),
            float(spec.get("volumeStep", 0.01))
        )
        
        # Entry / SL / TP du meilleur critère
        entry_price = best_entry["entry"]
        sl_base = best_entry["sl"]
        sl_distance = abs(entry_price - sl_base)
        
        if sl_distance <= 0:
            sl_distance = atr * PA_ATR_SL_MULTIPLIER
        
        tp_distance = sl_distance * PA_RR_RATIO
        
        # ✅ Ordre LIMIT
        if best_direction == "BUY":
            sl_price = normalize_price(entry_price - sl_distance, digits)
            tp_price = normalize_price(entry_price + tp_distance, digits)
            sl_price, tp_price = validate_stops(spec, entry_price, sl_price, tp_price, digits)
            
            log(f"📋 [{symbol}] PA BUY LIMIT (score {total_score}) | Entry={entry_price} | SL={sl_price} | TP={tp_price} | Vol={volume}")
            log(f"     → Critère : {best_entry['critere']} — {best_entry['raison']}")
            try:
                await connection.create_limit_buy_order(
                    symbol=symbol, volume=volume, open_price=entry_price,
                    stop_loss=sl_price, take_profit=tp_price
                )
                log(f"✅ [{symbol}] BUY LIMIT placé")
                LAST_SIGNAL[symbol] = "BUY"
                reset_position_state(symbol)
            except Exception as e:
                log(f"❌ [{symbol}] BUY LIMIT erreur : {e}")
        
        elif best_direction == "SELL":
            sl_price = normalize_price(entry_price + sl_distance, digits)
            tp_price = normalize_price(entry_price - tp_distance, digits)
            sl_price, tp_price = validate_stops(spec, entry_price, sl_price, tp_price, digits)
            
            log(f"📋 [{symbol}] PA SELL LIMIT (score {total_score}) | Entry={entry_price} | SL={sl_price} | TP={tp_price} | Vol={volume}")
            log(f"     → Critère : {best_entry['critere']} — {best_entry['raison']}")
            try:
                await connection.create_limit_sell_order(
                    symbol=symbol, volume=volume, open_price=entry_price,
                    stop_loss=sl_price, take_profit=tp_price
                )
                log(f"✅ [{symbol}] SELL LIMIT placé")
                LAST_SIGNAL[symbol] = "SELL"
                reset_position_state(symbol)
            except Exception as e:
                log(f"❌ [{symbol}] SELL LIMIT erreur : {e}")
    
    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse PA : {e}")

# ==============================================================================
# 18. ORDER BLOCK — Swings + BOS
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
# 19. POC
# ==============================================================================

def find_poc(candles, from_idx, to_idx, n_bins=POC_BINS):
    if from_idx >= to_idx or from_idx < 0 or to_idx >= len(candles):
        return None, 0
    min_p = float('inf')
    max_p = float('-inf')
    for i in range(from_idx, to_idx + 1):
        lo = float(candles[i]["low"])
        hi = float(candles[i]["high"])
        if lo < min_p: min_p = lo
        if hi > max_p: max_p = hi
    if min_p >= max_p:
        return None, 0
    step = (max_p - min_p) / n_bins
    if step <= 0: step = 1e-9
    diff = [0.0] * (n_bins + 1)
    for i in range(from_idx, to_idx + 1):
        lo = float(candles[i]["low"])
        hi = float(candles[i]["high"])
        s_bin = max(0, min(n_bins - 1, int((lo - min_p) / step)))
        e_bin = max(0, min(n_bins - 1, int((hi - min_p) / step)))
        diff[s_bin] += 1.0
        if e_bin + 1 < n_bins:
            diff[e_bin + 1] -= 1.0
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
    if poc is None:
        return 0
    count = 0
    for i in range(from_idx, to_idx + 1):
        if float(candles[i]["low"]) <= poc <= float(candles[i]["high"]):
            count += 1
    return count

def find_best_poc_candle(candles, from_idx, to_idx, poc, is_bull):
    if poc is None or from_idx >= to_idx:
        return None
    best_idx = None
    best_extreme = None
    for i in range(to_idx, from_idx - 1, -1):
        lo = float(candles[i]["low"])
        hi = float(candles[i]["high"])
        if not (lo <= poc <= hi):
            continue
        if is_bull:
            if best_extreme is None or lo < best_extreme:
                best_extreme = lo
                best_idx = i
        else:
            if best_extreme is None or hi > best_extreme:
                best_extreme = hi
                best_idx = i
    return best_idx

# ==============================================================================
# 20. GAP + OVERLAP
# ==============================================================================

def has_gap_between(candles, anchor_idx, bos_idx, is_bull):
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

def ob_overlaps_active(zones, top, bottom):
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
# 21. SCORE OB 5★
# ==============================================================================

def calculate_ob_score(candles, bos, anchor_idx, top, bottom):
    score = 0
    bos_idx = bos["bos_index"]
    zone_type = bos["type"]
    zone_size = top - bottom
    if zone_size <= 0:
        return 0
    
    atr = calculate_atr(candles, 14)
    if atr and atr > 0 and zone_size <= atr * OB_MAX_SIZE_ATR_MULT:
        score += 1
    elif not atr:
        score += 1
    
    vols = [float(c.get("volume", 0) or 0) for c in candles[-50:]]
    avg_v = sum(vols) / len(vols) if vols else 0
    anchor_vol = float(candles[anchor_idx].get("volume", 0) or 0)
    if avg_v > 0 and anchor_vol >= avg_v * OB_MIN_VOLUME_RATIO:
        score += 1
    elif avg_v <= 0:
        score += 1
    
    bars_since = len(candles) - 1 - bos_idx
    if bars_since <= OB_MAX_BOS_AGE:
        score += 1
    
    pen = 0
    for i in range(bos_idx + 1, len(candles)):
        ch, cl = float(candles[i]["high"]), float(candles[i]["low"])
        if ch >= bottom and cl <= top:
            if min(ch, top) - max(cl, bottom) > zone_size * OB_MAX_PENETRATION_PCT:
                pen += 1
    if pen == 0:
        score += 1
    
    ema = calculate_ema(candles, OB_EMA_PERIOD)
    last_close = float(candles[-1]["close"])
    if ema:
        if (zone_type == "BULL" and last_close > ema) or (zone_type == "BEAR" and last_close < ema):
            score += 1
    
    return score

# ==============================================================================
# 22. CRÉATION ZONE OB
# ==============================================================================

def create_ob_zone_poc(candles, bos, existing_zones):
    bos_idx = bos["bos_index"]
    swing_idx = bos["swing_index"]
    is_bull = bos["type"] == "BULL"
    if bos_idx <= swing_idx:
        return None
    
    poc, _ = find_poc(candles, swing_idx, bos_idx)
    if poc is None:
        return None
    
    touches = count_touches_poc(candles, swing_idx, bos_idx, poc)
    if touches < MIN_TOUCHES_POC:
        return None
    
    anchor_idx = find_best_poc_candle(candles, swing_idx, bos_idx, poc, is_bull)
    if anchor_idx is None:
        return None
    
    if GAP_FILTER_ENABLED and has_gap_between(candles, anchor_idx, bos_idx, is_bull):
        return None
    
    top = float(candles[anchor_idx]["high"])
    bottom = float(candles[anchor_idx]["low"])
    if top <= bottom:
        return None
    
    if OVERLAP_FILTER_ENABLED and ob_overlaps_active(existing_zones, top, bottom):
        return None
    
    score = calculate_ob_score(candles, bos, anchor_idx, top, bottom)
    
    if score < OB_MIN_SCORE:
        log(f"⏭️ [{is_bull and 'BULL' or 'BEAR'}] Zone refusée — {score}/5 < {OB_MIN_SCORE}")
        return None
    
    fibo = FIBO_LEVEL_BY_SCORE.get(score, 0.5)
    stars = "⭐" * score
    log(f"🆕 [{is_bull and 'BULL' or 'BEAR'}] Zone OB {score}/5 {stars} | Fibo={fibo} | top={top:.2f} bot={bottom:.2f}")
    
    return {
        "type": bos["type"], "top": top, "bottom": bottom,
        "anchor_index": anchor_idx, "bos_index": bos_idx,
        "poc": poc, "touches": touches,
        "active": True, "retested": False,
        "created_index": bos_idx,
        "order_placed": False, "order_id": None,
        "quality_score": score,
        "fibo_level": fibo,
    }

# ==============================================================================
# 23. MISE À JOUR ZONES
# ==============================================================================

def update_zones(candles, zones, symbol):
    if not zones:
        return zones
    last = candles[-1]
    lh, ll, lc = float(last["high"]), float(last["low"]), float(last["close"])
    for z in zones:
        if not z["active"]:
            continue
        inv = False
        if z["type"] == "BULL":
            inv = ll < z["bottom"] if INVALIDATION_METHOD == "Wick" else lc < z["bottom"]
        else:
            inv = lh > z["top"] if INVALIDATION_METHOD == "Wick" else lc > z["top"]
        if inv:
            z["active"] = False
            log(f"🔴 [{symbol}] Zone OB {z['type']} invalidée")
    return zones

# ==============================================================================
# 24. VOLUME OB
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

# ==============================================================================
# 25. PLACEMENT ORDRE LIMIT OB
# ==============================================================================

async def place_limit_order_ob(connection, symbol, zone):
    try:
        price = await connection.get_symbol_price(symbol)
        bid, ask = float(price["bid"]), float(price["ask"])
        spec = await connection.get_symbol_specification(symbol)
        digits = int(spec.get("digits", 5))
        
        zone_size = zone["top"] - zone["bottom"]
        if zone_size <= 0:
            return None
        
        fibo = zone.get("fibo_level", 0.5)
        
        if zone["type"] == "BULL":
            entry = normalize_price(zone["top"] - zone_size * fibo, digits)
            sl = normalize_price(zone["bottom"] - zone_size * SL_BUFFER_FACTOR, digits)
            if ask <= entry:
                return None
            risk = entry - sl
            if risk <= 0:
                return None
            tp = normalize_price(entry + risk * RR_RATIO_OB, digits)
        else:
            entry = normalize_price(zone["bottom"] + zone_size * fibo, digits)
            sl = normalize_price(zone["top"] + zone_size * SL_BUFFER_FACTOR, digits)
            if bid >= entry:
                return None
            risk = sl - entry
            if risk <= 0:
                return None
            tp = normalize_price(entry - risk * RR_RATIO_OB, digits)
        
        sl, tp = validate_stops(spec, entry, sl, tp, digits)
        
        volume = await calculate_volume_ob(connection, symbol, entry, sl)
        if not volume or volume <= 0:
            return None
        
        log(f"📋 [{symbol}] {'BUY' if zone['type']=='BULL' else 'SELL'} LIMIT OB | "
            f"Entry={entry} | SL={sl} | TP={tp} | Vol={volume} | "
            f"Score={zone['quality_score']}/5 | Fibo={fibo}")
        
        if zone["type"] == "BULL":
            order = await connection.create_limit_buy_order(
                symbol=symbol, volume=volume, open_price=entry,
                stop_loss=sl, take_profit=tp
            )
        else:
            order = await connection.create_limit_sell_order(
                symbol=symbol, volume=volume, open_price=entry,
                stop_loss=sl, take_profit=tp
            )
        
        zone["order_placed"] = True
        zone["order_id"] = order.get("orderId") if isinstance(order, dict) else order
        return order
    except Exception as e:
        log(f"❌ [{symbol}] Erreur ordre OB : {e}")
        return None

# ==============================================================================
# 26. CLEANUP + INVALIDATION OB
# ==============================================================================

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

async def invalidate_limit_orders(connection, symbol, candles):
    if symbol not in ob_zones:
        return
    try:
        orders = await connection.get_orders()
        symbol_orders = [o for o in orders if o.get("symbol") == symbol]
        if not symbol_orders:
            return
        price_info = await connection.get_symbol_price(symbol)
        current_price = (float(price_info["bid"]) + float(price_info["ask"])) / 2
        
        for order in symbol_orders:
            order_id = order.get("id") or order.get("orderId")
            order_price = float(order.get("openPrice", 0))
            order_time = order.get("time") or order.get("brokerTime")
            if not order_id:
                continue
            
            zone = None
            for z in ob_zones[symbol]:
                if z.get("order_id") == order_id:
                    zone = z
                    break
            if zone is None:
                continue
            
            if not zone.get("active", False):
                log(f"❌ [{symbol}] Zone invalidée → annulation {order_id}")
                try:
                    await connection.cancel_order(order_id)
                    zone["order_placed"] = False
                    zone["order_id"] = None
                except Exception:
                    pass
                continue
            
            zone_size = zone["top"] - zone["bottom"]
            if zone_size > 0 and abs(current_price - order_price) > zone_size * MAX_DISTANCE_FACTOR:
                log(f"❌ [{symbol}] Prix trop loin → annulation {order_id}")
                try:
                    await connection.cancel_order(order_id)
                    zone["order_placed"] = False
                    zone["order_id"] = None
                except Exception:
                    pass
                continue
            
            if order_time:
                try:
                    if isinstance(order_time, str):
                        odt = datetime.fromisoformat(order_time.replace("Z", "+00:00"))
                        if odt.tzinfo is None:
                            odt = odt.replace(tzinfo=timezone.utc)
                    else:
                        odt = order_time
                    age_h = (datetime.now(timezone.utc) - odt).total_seconds() / 3600
                    if age_h > MAX_ORDER_AGE_HOURS:
                        log(f"⏰ [{symbol}] Ordre vieux ({age_h:.1f}h) → annulation {order_id}")
                        try:
                            await connection.cancel_order(order_id)
                            zone["order_placed"] = False
                            zone["order_id"] = None
                        except Exception:
                            pass
                except Exception:
                    pass
    except Exception as e:
        log(f"❌ [{symbol}] Erreur invalidation : {e}")

# ==============================================================================
# 27. GESTION POSITIONS OB
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
                
                state = ensure_position_state(symbol)
                if state["initial_risk"] <= 0:
                    state["initial_risk"] = risk
                ir = state["initial_risk"]
                
                ptype = pos.get("type")
                
                if ptype == "POSITION_TYPE_BUY":
                    profit = current - entry
                    if profit >= ir * BREAK_EVEN_R_OB and sl < entry:
                        log(f"🛡️ [{symbol}] BE BUY #{pos['id']} SL {sl} → {entry}")
                        await connection.modify_position(pos["id"], stop_loss=entry, take_profit=pos.get("takeProfit"))
                        sl = entry
                        state["r_reached"] = True
                    if TRAILING_ENABLED_OB and profit >= ir * TRAILING_START_R_OB:
                        new_sl = current - ir * TRAILING_DISTANCE_R_OB
                        if new_sl > sl:
                            log(f"📈 [{symbol}] Trailing BUY #{pos['id']} SL {sl} → {new_sl}")
                            await connection.modify_position(pos["id"], stop_loss=new_sl, take_profit=pos.get("takeProfit"))
                
                elif ptype == "POSITION_TYPE_SELL":
                    profit = entry - current
                    if profit >= ir * BREAK_EVEN_R_OB and sl > entry:
                        log(f"🛡️ [{symbol}] BE SELL #{pos['id']} SL {sl} → {entry}")
                        await connection.modify_position(pos["id"], stop_loss=entry, take_profit=pos.get("takeProfit"))
                        sl = entry
                        state["r_reached"] = True
                    if TRAILING_ENABLED_OB and profit >= ir * TRAILING_START_R_OB:
                        new_sl = current + ir * TRAILING_DISTANCE_R_OB
                        if new_sl < sl:
                            log(f"📉 [{symbol}] Trailing SELL #{pos['id']} SL {sl} → {new_sl}")
                            await connection.modify_position(pos["id"], stop_loss=new_sl, take_profit=pos.get("takeProfit"))
            
            except Exception as e:
                log(f"[{symbol}] Erreur OB : {e}")
    except Exception as e:
        log(f"Erreur OB globale : {e}")

# ==============================================================================
# 28. ANALYSE OB
# ==============================================================================

async def analyze_ob(account, connection, symbol):
    try:
        await cleanup_zones(connection, symbol)
        candles = await get_candles(account, symbol, TIMEFRAME_ENTRY, CANDLE_COUNT_OB)
        if not candles or len(candles) < SWING_LEN * 3:
            return
        
        await invalidate_limit_orders(connection, symbol, candles)
        
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
            r = await place_limit_order_ob(connection, symbol, z)
            if r is not None:
                n_orders += 1
    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse OB : {e}")

# ==============================================================================
# 29. HEALTH CHECK
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
# 30. BOUCLE
# ==============================================================================

async def trading_loop(account, connection):
    while True:
        try:
            for s in PA_SYMBOLS:
                await analyze_pa(account, connection, s)
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
# 31. MAIN
# ==============================================================================

async def main():
    print("=" * 60, flush=True)
    print("🚀 ARKAS BOT — PA Multi-Critères + OB 5★ Fibo", flush=True)
    print("=" * 60, flush=True)
    print(f"PA    : {', '.join(PA_SYMBOLS)} (Score ≥ {PA_MIN_SCORE})", flush=True)
    print(f"OB    : {', '.join(OB_SYMBOLS)} (Score ≥ {OB_MIN_SCORE}★)", flush=True)
    print(f"Timeframe : {TIMEFRAME_ENTRY}", flush=True)
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
