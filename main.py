import asyncio
import os
import sys
import math
from datetime import datetime, timezone

from metaapi_cloud_sdk import MetaApi

# ==============================================================================
# ARKAS STOCHASTIC MTF BOT — PRO 4.1 (get_historical_candles fix)
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

SYMBOLS = [
    "Step Index",
    "Volatility 75 Index",
    "Volatility 10 Index",
    "Volatility 100 Index",
    "Volatility 25 Index"
]

TIMEFRAME_ENTRY = "15m"

# ==============================================================================
# 3. STOCHASTIQUE (5, 3, 3)
# ==============================================================================

STOCH_K = 5
STOCH_D = 3
STOCH_SLOWING = 3

LEVEL_BUY = 10
LEVEL_NEUTRAL = 50
LEVEL_SELL = 90

ALERT_OVERBOUGHT = 85
ALERT_OVERSOLD = 15

ZONE_NEUTRAL_LOW = 45
ZONE_NEUTRAL_HIGH = 55

# ==============================================================================
# 4. MONEY MANAGEMENT
# ==============================================================================

LOT_PER_SYMBOL = {
    "Step Index": 0.1,
    "Volatility 75 Index": 0.01,
    "Volatility 10 Index": 0.5,
    "Volatility 100 Index": 1.0,
    "Volatility 25 Index": 0.5
}

VOLUME_LIMITS = {
    "Step Index": {"min": 0.1, "max": 10.0},
    "Volatility 75 Index": {"min": 0.01, "max": 1.0},
    "Volatility 10 Index": {"min": 0.5, "max": 5.0},
    "Volatility 100 Index": {"min": 1.0, "max": 2.0},
    "Volatility 25 Index": {"min": 0.5, "max": 5.0}
}

MAX_POSITIONS_PER_SYMBOL = 1

DEFAULT_SL_POINTS = {
    "Step Index": 50.0,
    "Volatility 75 Index": 500.0,
    "Volatility 10 Index": 100.0,
    "Volatility 100 Index": 700.0,
    "Volatility 25 Index": 200.0
}

# ==============================================================================
# 5. GESTION POSITION
# ==============================================================================

ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 3.0
ATR_TRAIL_MULTIPLIER = 1.0
ATR_MIN_TRAIL_STEP = 0.3

BREAK_EVEN_TRIGGER_R = 0.5
RISK_REWARD_RATIO = 2.0

# ==============================================================================
# 6. EXECUTION
# ==============================================================================

SCAN_INTERVAL = 15
CANDLES_LIMIT = 100   # ✅ Réduit pour forcer données fraîches

# ==============================================================================
# 7. SPREAD
# ==============================================================================

MAX_SPREAD = {
    "Step Index": 2.0,
    "Volatility 75 Index": 100.0,
    "Volatility 10 Index": 20.0,
    "Volatility 100 Index": 150.0,
    "Volatility 25 Index": 50.0
}

# ==============================================================================
# 8. ÉTAT
# ==============================================================================

LAST_SIGNAL = {}
POSITION_STATE = {}
DEBUG_LAST_LOG = {}

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
# 10. STOCHASTIQUE
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
# 14. BOUGIES — get_historical_candles SIMPLE
# ==============================================================================

async def get_candles(account, symbol, timeframe, limit=CANDLES_LIMIT):
    """Récupère les bougies via get_historical_candles."""
    try:
        candles = await account.get_historical_candles(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit
        )
        if not candles or len(candles) < 3:
            return None
        return candles
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
                    log(f"⚠️ [{symbol}] ALERTE Stoch M15 haute ({curr_k:.1f}/{curr_d:.1f})")
                    state["alerted"] = True

                profit = bid - open_price
                if profit >= initial_risk * BREAK_EVEN_TRIGGER_R and current_sl < open_price:
                    be_sl = normalize_price(open_price + min_distance, digits) if min_distance > 0 else open_price
                    log(f"🛡️ [{symbol}] +{BREAK_EVEN_TRIGGER_R}R → BE BUY #{pos_id} SL={be_sl}")
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
                    log(f"⚠️ [{symbol}] ALERTE Stoch M15 basse ({curr_k:.1f}/{curr_d:.1f})")
                    state["alerted"] = True

                profit = open_price - ask
                if profit >= initial_risk * BREAK_EVEN_TRIGGER_R and (current_sl > open_price or current_sl == 0):
                    be_sl = normalize_price(open_price - min_distance, digits) if min_distance > 0 else open_price
                    log(f"🛡️ [{symbol}] +{BREAK_EVEN_TRIGGER_R}R → BE SELL #{pos_id} SL={be_sl}")
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
        active_positions = [p for p in positions if p.get("symbol") == symbol]

        if len(active_positions) >= MAX_POSITIONS_PER_SYMBOL:
            await manage_open_positions(account, connection, symbol)
            return

        candles_m15 = await get_candles(account, symbol, TIMEFRAME_ENTRY)
        stoch_m15 = calculate_stochastic(candles_m15, STOCH_K, STOCH_D, STOCH_SLOWING)
        if not stoch_m15 or len(stoch_m15["k"]) < 3:
            return

        # DEBUG : afficher la dernière bougie
        if candles_m15 and len(candles_m15) >= 3:
            last = candles_m15[-1]
            last_time = last.get("time") or last.get("brokerTime") or "?"
            if DEBUG_LAST_LOG.get(symbol) != last_time:
                DEBUG_LAST_LOG[symbol] = last_time
                log(f"🔬 [{symbol}] Dernière bougie M15 : {last_time} | C={last['close']}")
                log(f"🔬 [{symbol}] Stoch K={round(stoch_m15['k'][-1], 2)} D={round(stoch_m15['d'][-1], 2)}")

        prev_k, prev_d = stoch_m15["k"][-2], stoch_m15["d"][-2]
        curr_k, curr_d = stoch_m15["k"][-1], stoch_m15["d"][-1]
        older_k = stoch_m15["k"][-3]

        cross_up = prev_k <= prev_d and curr_k > curr_d and curr_k > older_k
        cross_down = prev_k >= prev_d and curr_k < curr_d and curr_k < older_k

        if not (cross_up or cross_down):
            log(f"🔍 [{symbol}] M15 : K={curr_k:.1f} D={curr_d:.1f}")
            return

        if cross_up and curr_k >= ZONE_NEUTRAL_LOW:
            log(f"🚫 [{symbol}] Croisement haussier ignoré — K={curr_k:.1f} ≥ {ZONE_NEUTRAL_LOW}")
            return
        if cross_down and curr_k <= ZONE_NEUTRAL_HIGH:
            log(f"🚫 [{symbol}] Croisement baissier ignoré — K={curr_k:.1f} ≤ {ZONE_NEUTRAL_HIGH}")
            return

        signal_type = "BUY" if cross_up else "SELL"

        if LAST_SIGNAL.get(symbol) == signal_type:
            log(f"⏸️ [{symbol}] Signal {signal_type} déjà traité — attente inversion")
            return

        price_info = await connection.get_symbol_price(symbol)
        bid, ask = float(price_info["bid"]), float(price_info["ask"])
        spread = ask - bid

        if spread > MAX_SPREAD.get(symbol, 999.0):
            log(f"🚫 [{symbol}] Spread trop élevé ({spread:.2f})")
            return

        atr_m15 = calculate_atr(candles_m15, ATR_PERIOD)
        if not atr_m15 or atr_m15 <= 0:
            log(f"⚠️ [{symbol}] ATR indisponible")
            return

        if cross_up and not structure_confirms_buy(candles_m15, ask):
            log(f"🚫 [{symbol}] Structure prix invalide BUY")
            return
        if cross_down and not structure_confirms_sell(candles_m15, bid):
            log(f"🚫 [{symbol}] Structure prix invalide SELL")
            return

        volume = get_fixed_lot(symbol, spec)

        sl_distance = atr_m15 * ATR_SL_MULTIPLIER
        tp_distance = sl_distance * RISK_REWARD_RATIO

        if cross_up:
            sl_price = normalize_price(ask - sl_distance, digits)
            tp_price = normalize_price(ask + tp_distance, digits)
            sl_price, tp_price = validate_stops(spec, ask, sl_price, tp_price, digits)

            log(f"🚀 [{symbol}] ACHAT MARKET | Entry={ask} | SL={sl_price} | TP={tp_price} | Vol={volume}")
            try:
                await connection.create_market_buy_order(
                    symbol=symbol, volume=volume, stop_loss=sl_price, take_profit=tp_price
                )
                log(f"✅ [{symbol}] ACHAT exécuté")
                LAST_SIGNAL[symbol] = "BUY"
                reset_position_state(symbol)
            except Exception as e:
                log(f"❌ [{symbol}] ACHAT erreur : {e}")

        elif cross_down:
            sl_price = normalize_price(bid + sl_distance, digits)
            tp_price = normalize_price(bid - tp_distance, digits)
            sl_price, tp_price = validate_stops(spec, bid, sl_price, tp_price, digits)

            log(f"🔻 [{symbol}] VENTE MARKET | Entry={bid} | SL={sl_price} | TP={tp_price} | Vol={volume}")
            try:
                await connection.create_market_sell_order(
                    symbol=symbol, volume=volume, stop_loss=sl_price, take_profit=tp_price
                )
                log(f"✅ [{symbol}] VENTE exécutée")
                LAST_SIGNAL[symbol] = "SELL"
                reset_position_state(symbol)
            except Exception as e:
                log(f"❌ [{symbol}] VENTE erreur : {e}")

    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse/trading : {e}")

# ==============================================================================
# 20. BOUCLE 24/7
# ==============================================================================

async def trading_loop(account, connection):
    while True:
        try:
            for symbol in SYMBOLS:
                await analyze_and_trade(account, connection, symbol)
            await asyncio.sleep(SCAN_INTERVAL)
        except Exception as e:
            log(f"⚠️ Erreur boucle : {e}")
            await asyncio.sleep(10)

# ==============================================================================
# 21. HEALTH CHECK HTTP POUR RENDER
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
    log(f"🌐 Health check HTTP sur port {port}")
    async with server:
        await server.serve_forever()

# ==============================================================================
# 22. MAIN
# ==============================================================================

async def main():
    print("=" * 60, flush=True)
    print("🚀 ARKAS STOCHASTIC MTF BOT — PRO 4.1", flush=True)
    print("=" * 60, flush=True)
    print(f"Symboles : {', '.join(SYMBOLS)}", flush=True)
    print(f"Timeframe : {TIMEFRAME_ENTRY}", flush=True)
    print(f"Lots : " + " | ".join([f"{s}={LOT_PER_SYMBOL[s]}" for s in SYMBOLS]), flush=True)
    print(f"Candles limit : {CANDLES_LIMIT}", flush=True)
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
        log("🟢 BOT PRO 4.1 CONNECTÉ")

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
