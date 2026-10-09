import asyncio
import math
import os
import time
from datetime import datetime, timedelta, timezone

from metaapi_cloud_sdk import MetaApi

# ==============================================================================
# ARKAS BOT v3 — PA + Order Blocks + Scalp, avec gestion du risque
# ------------------------------------------------------------------------------
# Changements majeurs par rapport à la v2 :
#  - Bougies : seules les bougies CLÔTURÉES sont analysées (+ cache par timeframe)
#  - Magic number : le bot ne touche que ses propres ordres / positions
#  - Un seul appel positions/ordres par boucle ("book"), pas par symbole
#  - Anti-répétition par cooldown (plus de LAST_SIGNAL bloquant) + prise en compte des ordres en attente
#  - Expiration des ordres limit (côté broker + annulation manuelle par âge / distance)
#  - OB : identité de zone par horodatage (les index glissaient à chaque bougie), zone consommée = plus rejouée
#  - Suivi des positions par ID (BE + trailing en multiples de R, SL arrondis et validés)
#  - Dimensionnement au risque (% du solde) pour TOUTES les stratégies
#  - Perte journalière max, drawdown max (kill switch), plafonds par groupe corrélé
#  - Filtre de session (forex/or), contrôle du côté de l'ordre limit vs prix
# ==============================================================================

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================

TOKEN = os.getenv("METAAPI_TOKEN")
ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")
METAAPI_REGION = os.getenv("METAAPI_REGION", "london")
MAGIC = int(os.getenv("ARKAS_MAGIC", "424242"))

PA_SYMBOLS = ["Step Index", "Volatility 75 Index", "Volatility 100 Index", "Volatility 25 Index"]
OB_SYMBOLS = ["XAUUSD", "BTCUSD"]
SCALP_SYMBOLS = ["USDJPY", "GBPJPY"]

STRATEGY_OF = {}
STRATEGY_OF.update({s: "PA" for s in PA_SYMBOLS})
STRATEGY_OF.update({s: "OB" for s in OB_SYMBOLS})
STRATEGY_OF.update({s: "SC" for s in SCALP_SYMBOLS})

TIMEFRAME_TREND = "1h"
TIMEFRAME_ENTRY = "15m"
TIMEFRAME_SCALP = "5m"

TF_SECONDS = {"1h": 3600, "15m": 900, "5m": 300}
CACHE_TTL = {"1h": 300, "15m": 45, "5m": 15}   # secondes

# --- Paramètres par stratégie (R = risque initial = distance entrée → SL) ------
STRAT = {
    "PA": dict(risk_pct=0.5, rr=1.5, be_r=0.5, trail_start_r=1.0, trail_dist_r=0.5,
               max_order_age_min=360, cooldown_min=30, comment="ARKAS-PA"),
    "OB": dict(risk_pct=0.5, rr=1.5, be_r=0.5, trail_start_r=1.0, trail_dist_r=0.5,
               max_order_age_min=360, cooldown_min=0, comment="ARKAS-OB"),
    "SC": dict(risk_pct=0.25, rr=1.5, be_r=0.5, trail_start_r=0.8, trail_dist_r=0.4,
               max_order_age_min=45, cooldown_min=15, comment="ARKAS-SC"),
}
BE_LOCK_R = 0.1                 # le BE verrouille +0.1R
MIN_SL_STEP_R = 0.1             # pas minimum d'une modification de trailing
MAX_DISTANCE_FACTOR = 4.0       # annule un limit si prix > 4x la distance SL de l'entrée

# --- Risque global --------------------------------------------------------------
MAX_DAILY_LOSS_PCT = 3.0        # pause des nouvelles entrées jusqu'au lendemain (UTC)
MAX_DRAWDOWN_PCT = 10.0         # kill switch depuis le pic d'equity (jusqu'au redémarrage)
MAX_TOTAL_EXPOSURE = 8          # positions + ordres en attente
MAX_RISK_OVERSHOOT = 2.0        # refuse si le lot minimum dépasse 2x le risque cible
MAX_SPREAD_SL_RATIO = 0.30      # spread max = 30% de la distance SL

RISK_GROUPS = {
    "VOL": ["Volatility 75 Index", "Volatility 100 Index", "Volatility 25 Index"],
    "STEP": ["Step Index"],
    "GOLD": ["XAUUSD"],
    "BTC": ["BTCUSD"],
    "JPY": ["USDJPY", "GBPJPY"],
}
MAX_PER_GROUP = {"VOL": 2, "STEP": 1, "GOLD": 3, "BTC": 3, "JPY": 1}

# Lot maximum par symbole (plafond de sécurité ; le lot réel vient du risque)
MAX_LOT = {
    "Step Index": 0.1,
    "Volatility 75 Index": 0.01,
    "Volatility 100 Index": 1.0,
    "Volatility 25 Index": 0.5,
    "XAUUSD": 0.2,
    "BTCUSD": 0.1,
    "USDJPY": 0.05,
    "GBPJPY": 0.05,
}

MAX_SPREAD = {
    "Step Index": 5.0, "Volatility 75 Index": 200.0, "Volatility 100 Index": 300.0,
    "Volatility 25 Index": 100.0, "XAUUSD": 10.0, "BTCUSD": 100.0,
    "USDJPY": 0.005, "GBPJPY": 0.008,
}

# --- Price Action ----------------------------------------------------------------
PA_EMA_PERIOD = 50
PA_STRUCTURE_LOOKBACK = 30
PA_SWING_PIVOT_LEN = 3
PA_TRENDLINE_MIN_SLOPE_ATR = 0.02   # pente minimale en fraction d'ATR par bougie
PA_BREAKOUT_BODY_RATIO = 0.5
PA_BREAKOUT_MIN_MARGIN = 0.2
PA_FIBO_TOLERANCE = 0.35
PA_ATR_PERIOD = 14
PA_ATR_SL_MULTIPLIER = 1.5
PA_MIN_SL_ATR = 0.8                 # SL jamais plus serré que 0.8 ATR
PA_MIN_SCORE = 3
H1_EMA_PERIOD = 50

# --- Order Blocks ---------------------------------------------------------------
SWING_LEN = 5
MAX_ZONES_TO_TRACK = 5
CANDLE_COUNT_OB = 200
POC_BINS = 40
MIN_TOUCHES_POC = 1
GAP_FILTER_ENABLED = True
INVALIDATION_METHOD = "Wick"
MAX_LIMIT_ORDERS_PER_SYMBOL = 2
OB_MAX_SIZE_ATR_MULT = 1.5
OB_MIN_VOLUME_RATIO = 1.1
OB_MAX_BOS_AGE = 30
OB_MAX_PENETRATION_PCT = 0.6
OB_EMA_PERIOD = 50
OB_MIN_SCORE = 3
OB_REQUIRE_H1_ALIGN = True
FIBO_LEVEL_BY_SCORE = {5: 0.786, 4: 0.618, 3: 0.5, 2: 0.382}
SL_BUFFER_FACTOR = 0.3

# --- Scalp -----------------------------------------------------------------------
SCALP_PIP_VALUE = {"USDJPY": 0.01, "GBPJPY": 0.01}
SCALP_ATR_PERIOD = 7
SCALP_EMA = 20
SCALP_STRUCTURE_LOOKBACK = 20
SCALP_SL_PIPS_MIN = {"USDJPY": 8.0, "GBPJPY": 10.0}   # plancher ; SL = max(plancher, 1.0 ATR)
SCALP_MIN_SCORE = 3
SCALP_SESSION_UTC = (7, 17)                           # heures UTC

# --- Exécution -------------------------------------------------------------------
SCAN_INTERVAL = 10
CANDLES_LIMIT = 150

# ==============================================================================
# 2. ÉTAT GLOBAL
# ==============================================================================

POSITION_STATE = {}     # {position_id: {"ir": float}}
COOLDOWN = {}           # {symbol: datetime}
ob_zones = {}           # {symbol: [zone, ...]}
CANDLE_CACHE = {}       # {(symbol, tf): (monotonic_ts, closed_candles)}
SPEC_CACHE = {}         # {symbol: spec}
RISK = {"day": None, "start_equity": 0.0, "peak_equity": 0.0, "balance": 0.0,
        "equity": 0.0, "halted": False, "kill": False}

# ==============================================================================
# 3. UTILS
# ==============================================================================

def log(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

def now_utc():
    return datetime.now(timezone.utc)

def normalize_price(price, digits):
    return round(float(price), int(digits))

def normalize_volume(volume, minimum, maximum, step):
    if step <= 0:
        step = 0.01
    volume = max(minimum, min(volume, maximum))
    steps = math.floor(volume / step + 1e-9)
    return round(max(steps * step, minimum), 4)

def to_dt(value):
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value
    except Exception:
        return None

def calculate_atr(candles, period=14):
    if not candles or len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"]); l = float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period

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
# 4. DONNÉES : bougies clôturées (cache), specs
# ==============================================================================

async def get_candles(account, symbol, timeframe, limit=CANDLES_LIMIT):
    """Retourne les bougies CLÔTURÉES uniquement (la bougie en formation est retirée)."""
    key = (symbol, timeframe)
    cached = CANDLE_CACHE.get(key)
    if cached and (time.monotonic() - cached[0]) < CACHE_TTL.get(timeframe, 30) and len(cached[1]) >= min(limit, 50):
        return cached[1]
    try:
        candles = await account.get_historical_candles(symbol=symbol, timeframe=timeframe, limit=limit)
        if not candles or len(candles) < 5:
            return None
        closed = candles[:-1]
        CANDLE_CACHE[key] = (time.monotonic(), closed)
        return closed
    except Exception as e:
        log(f"❌ [{symbol}] Erreur bougies {timeframe} : {e}")
        return None

async def get_spec(connection, symbol):
    if symbol in SPEC_CACHE:
        return SPEC_CACHE[symbol]
    try:
        spec = await connection.get_symbol_specification(symbol)
        if spec:
            SPEC_CACHE[symbol] = spec
        return spec
    except Exception as e:
        log(f"❌ [{symbol}] Erreur spec : {e}")
        return None

async def is_symbol_tradable(connection, symbol):
    spec = await get_spec(connection, symbol)
    if not spec or spec.get("tradeMode") == "SYMBOL_TRADE_MODE_DISABLED":
        return False, spec
    return True, spec

def get_min_stop_distance(spec, digits):
    lvl = max(float(spec.get("stopsLevel", 0) or 0), float(spec.get("freezeLevel", 0) or 0))
    return lvl * (10 ** -digits) if lvl > 0 else 0.0

def validate_stops(spec, entry, sl, tp, digits):
    md = get_min_stop_distance(spec, digits)
    if md <= 0:
        return sl, tp
    buf = 10 ** -digits
    if abs(entry - sl) < md:
        sl = normalize_price((entry - md - buf) if sl < entry else (entry + md + buf), digits)
    if abs(entry - tp) < md:
        tp = normalize_price((entry + md + buf) if tp > entry else (entry - md - buf), digits)
    return sl, tp

# ==============================================================================
# 5. SESSION, RISQUE GLOBAL, BOOK
# ==============================================================================

def session_ok(symbol):
    strat = STRATEGY_OF.get(symbol)
    if strat == "PA" or symbol == "BTCUSD":
        return True                     # 24/7
    n = now_utc()
    wd, h = n.weekday(), n.hour + n.minute / 60
    if wd == 5 or (wd == 6 and h < 22) or (wd == 4 and h >= 21):
        return False                    # week-end forex / or
    if strat == "SC":
        return SCALP_SESSION_UTC[0] <= h < SCALP_SESSION_UTC[1] and wd < 5
    return True

async def update_risk_state(connection):
    """Met à jour equity / balance et les garde-fous. Retourne True si de nouvelles entrées sont permises."""
    info = await connection.get_account_information()
    equity = float(info.get("equity", 0) or 0)
    balance = float(info.get("balance", 0) or 0)
    if equity <= 0:
        return False
    today = now_utc().date()
    if RISK["day"] != today:
        RISK.update(day=today, start_equity=equity, halted=False)
        log(f"📅 Nouveau jour UTC — equity de départ {equity:.2f}")
    RISK["peak_equity"] = max(RISK["peak_equity"], equity)
    RISK["balance"], RISK["equity"] = balance, equity

    daily_loss = (RISK["start_equity"] - equity) / RISK["start_equity"] * 100 if RISK["start_equity"] > 0 else 0
    drawdown = (RISK["peak_equity"] - equity) / RISK["peak_equity"] * 100 if RISK["peak_equity"] > 0 else 0

    if not RISK["kill"] and drawdown >= MAX_DRAWDOWN_PCT:
        RISK["kill"] = True
        log(f"⛔ KILL SWITCH : drawdown {drawdown:.1f}% ≥ {MAX_DRAWDOWN_PCT}% — plus de nouvelles entrées")
    if not RISK["halted"] and daily_loss >= MAX_DAILY_LOSS_PCT:
        RISK["halted"] = True
        log(f"⛔ Perte journalière {daily_loss:.1f}% ≥ {MAX_DAILY_LOSS_PCT}% — pause jusqu'à demain (UTC)")
    return not (RISK["kill"] or RISK["halted"])

async def load_book(connection):
    positions = [p for p in await connection.get_positions() if p.get("magic") == MAGIC]
    orders = [o for o in await connection.get_orders() if o.get("magic") == MAGIC]
    return {"positions": positions, "orders": orders}

def book_count(book, symbols=None):
    items = book["positions"] + book["orders"]
    if symbols is None:
        return len(items)
    return sum(1 for x in items if x.get("symbol") in symbols)

def group_of(symbol):
    for g, syms in RISK_GROUPS.items():
        if symbol in syms:
            return g
    return None

def can_open(book, symbol):
    if book_count(book) >= MAX_TOTAL_EXPOSURE:
        return False
    g = group_of(symbol)
    if g and book_count(book, RISK_GROUPS[g]) >= MAX_PER_GROUP.get(g, 1):
        return False
    return True

def in_cooldown(symbol, strat):
    last = COOLDOWN.get(symbol)
    minutes = STRAT[strat]["cooldown_min"]
    return bool(last and minutes > 0 and now_utc() - last < timedelta(minutes=minutes))

# ==============================================================================
# 6. TENDANCE H1
# ==============================================================================

async def h1_trend(account, symbol):
    candles = await get_candles(account, symbol, TIMEFRAME_TREND, 120)
    if not candles or len(candles) < H1_EMA_PERIOD + 5:
        return None
    ema = calculate_ema(candles, H1_EMA_PERIOD)
    if not ema:
        return None
    close = float(candles[-1]["close"])
    return "BUY" if close > ema else "SELL" if close < ema else None

# ==============================================================================
# 7. DIMENSIONNEMENT AU RISQUE + ENVOI D'ORDRE
# ==============================================================================

def loss_per_lot(spec, distance, quote_to_usd):
    tick_size = float(spec.get("tickSize") or 0)
    tick_value = float(spec.get("tickValue") or 0)
    if tick_size > 0 and tick_value > 0:
        return distance / tick_size * tick_value
    cs = float(spec.get("contractSize") or spec.get("tradeContractSize") or 1)
    return distance * cs * quote_to_usd

async def compute_volume(connection, spec, symbol, strat, entry, sl):
    cfg = STRAT[strat]
    risk_money = RISK["balance"] * cfg["risk_pct"] / 100
    if risk_money <= 0:
        return None
    quote_to_usd = 1.0
    if symbol.endswith("JPY"):
        p = await connection.get_symbol_price("USDJPY")
        quote_to_usd = 1.0 / float(p["bid"])
    lpl = loss_per_lot(spec, abs(entry - sl), quote_to_usd)
    if lpl <= 0:
        return None
    mn = float(spec.get("minVolume", 0.01)); mx = float(spec.get("maxVolume", 100))
    st = float(spec.get("volumeStep", 0.01))
    if symbol in MAX_LOT:
        mx = min(mx, MAX_LOT[symbol])
    vol = normalize_volume(risk_money / lpl, mn, mx, st)
    real_risk = vol * lpl
    if real_risk > risk_money * MAX_RISK_OVERSHOOT:
        log(f"🚫 [{symbol}] Lot min trop risqué ({real_risk:.2f} vs cible {risk_money:.2f})")
        return None
    return vol

async def send_limit(connection, direction, symbol, volume, entry, sl, tp, strat):
    cfg = STRAT[strat]
    options = {
        "magic": MAGIC,
        "comment": cfg["comment"],
        "expiration": {"type": "ORDER_TIME_SPECIFIED",
                       "time": now_utc() + timedelta(minutes=cfg["max_order_age_min"])},
    }
    fn = connection.create_limit_buy_order if direction == "BUY" else connection.create_limit_sell_order
    try:
        return await fn(symbol=symbol, volume=volume, open_price=entry,
                        stop_loss=sl, take_profit=tp, options=options)
    except Exception as e:
        if "expir" in str(e).lower():       # broker sans expiration : on retente sans (annulation manuelle)
            options.pop("expiration", None)
            return await fn(symbol=symbol, volume=volume, open_price=entry,
                            stop_loss=sl, take_profit=tp, options=options)
        raise

async def place_trade(connection, spec, book, symbol, strat, direction, entry, sl, reason, atr_ref=None):
    """Valide le côté du limit, le spread, calcule SL/TP/lot et envoie l'ordre."""
    try:
        cfg = STRAT[strat]
        digits = int(spec.get("digits", 2))
        price = await connection.get_symbol_price(symbol)
        bid, ask = float(price["bid"]), float(price["ask"])
        spread = ask - bid
        if spread > MAX_SPREAD.get(symbol, 999.0):
            log(f"🚫 [{symbol}] Spread trop élevé ({spread})")
            return False

        md = get_min_stop_distance(spec, digits)
        entry = normalize_price(entry, digits)
        # Côté valide : buy limit sous l'ask, sell limit au-dessus du bid
        if direction == "BUY" and entry > ask - md - 10 ** -digits:
            log(f"⏭️ [{symbol}] BUY LIMIT {entry} invalide (ask={ask}) — ignoré")
            return False
        if direction == "SELL" and entry < bid + md + 10 ** -digits:
            log(f"⏭️ [{symbol}] SELL LIMIT {entry} invalide (bid={bid}) — ignoré")
            return False

        sl_dist = abs(entry - sl)
        if atr_ref:
            sl_dist = max(sl_dist, atr_ref * PA_MIN_SL_ATR)
        if sl_dist <= 0:
            return False
        if spread > sl_dist * MAX_SPREAD_SL_RATIO:
            log(f"🚫 [{symbol}] Spread {spread} trop grand vs SL {sl_dist:.5f}")
            return False

        tp_dist = sl_dist * cfg["rr"]
        if direction == "BUY":
            sl_p = normalize_price(entry - sl_dist, digits); tp_p = normalize_price(entry + tp_dist, digits)
        else:
            sl_p = normalize_price(entry + sl_dist, digits); tp_p = normalize_price(entry - tp_dist, digits)
        sl_p, tp_p = validate_stops(spec, entry, sl_p, tp_p, digits)

        volume = await compute_volume(connection, spec, symbol, strat, entry, sl_p)
        if not volume:
            return False

        log(f"🚀 [{symbol}] {strat} {direction} LIMIT | Entry={entry} SL={sl_p} TP={tp_p} Vol={volume} | {reason}")
        res = await send_limit(connection, direction, symbol, volume, entry, sl_p, tp_p, strat)
        order_id = res.get("orderId") if isinstance(res, dict) else res
        book["orders"].append({"id": order_id, "symbol": symbol})   # visible pour la suite de la boucle
        COOLDOWN[symbol] = now_utc()
        return order_id or True
    except Exception as e:
        log(f"❌ [{symbol}] Erreur envoi ordre : {e}")
        return False

# ==============================================================================
# 8. PRICE ACTION
# ==============================================================================

def find_pa_swings(candles, lookback=PA_STRUCTURE_LOOKBACK, pivot_len=PA_SWING_PIVOT_LEN):
    if not candles or len(candles) < lookback:
        return [], []
    recent = candles[-lookback:]
    base = len(candles) - lookback
    highs, lows = [], []
    for i in range(pivot_len, len(recent) - pivot_len):
        hi = float(recent[i]["high"]); lo = float(recent[i]["low"])
        if all(hi > float(recent[i - j]["high"]) and hi > float(recent[i + j]["high"]) for j in range(1, pivot_len + 1)):
            highs.append({"index": base + i, "price": hi})
        if all(lo < float(recent[i - j]["low"]) and lo < float(recent[i + j]["low"]) for j in range(1, pivot_len + 1)):
            lows.append({"index": base + i, "price": lo})
    return highs, lows

def trendline(candles, swings, atr, bull):
    if len(swings) < 2:
        return None
    p1, p2 = swings[-2], swings[-1]
    if p2["index"] <= p1["index"]:
        return None
    slope = (p2["price"] - p1["price"]) / (p2["index"] - p1["index"])
    min_slope = atr * PA_TRENDLINE_MIN_SLOPE_ATR
    if (bull and slope <= min_slope) or (not bull and slope >= -min_slope):
        return None
    return p2["price"] + slope * (len(candles) - 1 - p2["index"])

def detect_all_pa_entries(candles, atr):
    entries = []
    if len(candles) < PA_EMA_PERIOD + 10:
        return entries
    last, prev = candles[-1], candles[-2]
    c, h, l, o = (float(last[k]) for k in ("close", "high", "low", "open"))
    ema = calculate_ema(candles, PA_EMA_PERIOD)
    sh_list, sl_list = find_pa_swings(candles)
    tol = atr * PA_FIBO_TOLERANCE
    buf = atr * PA_ATR_SL_MULTIPLIER

    def add(critere, direction, entry, sl, score, raison):
        entries.append({"critere": critere, "direction": direction, "entry": entry,
                        "sl": sl, "score": score, "raison": raison})

    # 1. Retest d'un niveau cassé (+3)
    if sh_list:
        s = sh_list[-1]
        if len(candles) - 1 - s["index"] <= 15 and l <= s["price"] + tol and c > s["price"]:
            add("Retest", "BUY", s["price"], s["price"] - buf, 3, f"Retest SH {s['price']:.2f}")
    if sl_list:
        s = sl_list[-1]
        if len(candles) - 1 - s["index"] <= 15 and h >= s["price"] - tol and c < s["price"]:
            add("Retest", "SELL", s["price"], s["price"] + buf, 3, f"Retest SL {s['price']:.2f}")

    # 2. Cassure : on place un limit sur le NIVEAU cassé (retest), pas au close (+3)
    rng = h - l
    if rng > 0 and abs(c - o) / rng >= PA_BREAKOUT_BODY_RATIO:
        margin = atr * PA_BREAKOUT_MIN_MARGIN
        pc = float(prev["close"])
        if sh_list and c > sh_list[-1]["price"] + margin and pc <= sh_list[-1]["price"]:
            lvl = sh_list[-1]["price"]
            add("Cassure", "BUY", lvl, lvl - buf, 3, f"Cassure {lvl:.2f}")
        if sl_list and c < sl_list[-1]["price"] - margin and pc >= sl_list[-1]["price"]:
            lvl = sl_list[-1]["price"]
            add("Cassure", "SELL", lvl, lvl + buf, 3, f"Cassure {lvl:.2f}")

    # 3. Fibo 0.618 (+2)
    if sh_list and sl_list:
        sh, sl_ = sh_list[-1], sl_list[-1]
        rg = abs(sh["price"] - sl_["price"])
        if rg > 0:
            if sh["index"] > sl_["index"]:
                fibo = sh["price"] - rg * 0.618
                if abs(c - fibo) <= tol:
                    add("Fibo", "BUY", fibo, sh["price"] - rg * 0.786, 2, f"Fibo 0.618 = {fibo:.2f}")
            else:
                fibo = sl_["price"] + rg * 0.618
                if abs(c - fibo) <= tol:
                    add("Fibo", "SELL", fibo, sl_["price"] + rg * 0.786, 2, f"Fibo 0.618 = {fibo:.2f}")

    # 4. Trendline (+2)
    tl = trendline(candles, sl_list, atr, True)
    if tl is not None and abs(l - tl) <= tol and c > tl:
        add("Trendline", "BUY", tl, tl - buf, 2, f"Trendline bull {tl:.2f}")
    tl = trendline(candles, sh_list, atr, False)
    if tl is not None and abs(h - tl) <= tol and c < tl:
        add("Trendline", "SELL", tl, tl + buf, 2, f"Trendline bear {tl:.2f}")

    # 5. EMA 50 (+1)
    if ema:
        if abs(l - ema) <= tol and c > ema:
            add("EMA", "BUY", ema, ema - buf, 1, f"EMA50 {ema:.2f}")
        if abs(h - ema) <= tol and c < ema:
            add("EMA", "SELL", ema, ema + buf, 1, f"EMA50 {ema:.2f}")
    return entries

async def analyze_pa(account, connection, book, symbol):
    try:
        if not session_ok(symbol) or not can_open(book, symbol):
            return
        if book_count(book, [symbol]) >= 1 or in_cooldown(symbol, "PA"):
            return
        ok, spec = await is_symbol_tradable(connection, symbol)
        if not ok:
            return
        trend = await h1_trend(account, symbol)
        if not trend:
            return
        candles = await get_candles(account, symbol, TIMEFRAME_ENTRY)
        if not candles or len(candles) < PA_EMA_PERIOD + 10:
            return
        atr = calculate_atr(candles, PA_ATR_PERIOD)
        if not atr or atr <= 0:
            return

        entries = [e for e in detect_all_pa_entries(candles, atr) if e["direction"] == trend]
        score = sum(e["score"] for e in entries)
        if score < PA_MIN_SCORE:
            return
        best = max(entries, key=lambda e: e["score"])
        log(f"📊 [{symbol}] PA {trend} score={score} ({', '.join(e['critere'] for e in entries)})")
        await place_trade(connection, spec, book, symbol, "PA", trend, best["entry"], best["sl"],
                          f"{best['raison']} | score {score}", atr_ref=atr)
    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse PA : {e}")

# ==============================================================================
# 9. SCALP (USDJPY / GBPJPY) — cassure M5 clôturée, entrée sur retest
# ==============================================================================

def detect_scalp_signal(candles):
    """Retourne (direction, niveau_retest, score, raison) sur la dernière bougie M5 clôturée."""
    n = SCALP_STRUCTURE_LOOKBACK
    if not candles or len(candles) < n + 5:
        return None, None, 0, ""
    atr = calculate_atr(candles, SCALP_ATR_PERIOD)
    ema = calculate_ema(candles, SCALP_EMA)
    ema_prev = calculate_ema(candles[:-3], SCALP_EMA)
    if not atr or not ema or not ema_prev:
        return None, None, 0, ""

    last = candles[-1]
    c, h, l, o = (float(last[k]) for k in ("close", "high", "low", "open"))
    rng = h - l
    if rng <= 0:
        return None, None, 0, ""
    window = candles[-n - 1:-1]                       # n bougies avant la dernière
    micro_high = max(float(x["high"]) for x in window)
    micro_low = min(float(x["low"]) for x in window)
    body_ratio = abs(c - o) / rng

    for direction, level, broke, ema_ok, ema_slope in (
        ("BUY", micro_high, c > micro_high, c > ema, ema > ema_prev),
        ("SELL", micro_low, c < micro_low, c < ema, ema < ema_prev),
    ):
        if not broke:
            continue
        score, why = 1, ["cassure micro"]
        if body_ratio >= 0.6:
            score += 1; why.append("corps fort")
        if ema_ok and ema_slope:
            score += 1; why.append("EMA alignée")
        if rng >= atr:
            score += 1; why.append("impulsion ≥ ATR")
        return direction, level, score, " + ".join(why)
    return None, None, 0, ""

async def analyze_scalp(account, connection, book, symbol):
    try:
        if not session_ok(symbol) or not can_open(book, symbol):
            return
        if book_count(book, [symbol]) >= 1 or in_cooldown(symbol, "SC"):
            return
        ok, spec = await is_symbol_tradable(connection, symbol)
        if not ok:
            return
        trend = await h1_trend(account, symbol)
        if not trend:
            return
        candles = await get_candles(account, symbol, TIMEFRAME_SCALP, 100)
        direction, level, score, why = detect_scalp_signal(candles)
        if not direction or score < SCALP_MIN_SCORE or direction != trend:
            return

        digits = int(spec.get("digits", 3))
        atr = calculate_atr(candles, SCALP_ATR_PERIOD)
        pip = SCALP_PIP_VALUE.get(symbol, 0.01)
        sl_dist = max(SCALP_SL_PIPS_MIN.get(symbol, 8.0) * pip, atr)
        sl = level - sl_dist if direction == "BUY" else level + sl_dist
        await place_trade(connection, spec, book, symbol, "SC", direction,
                          normalize_price(level, digits), sl, f"{why} | score {score}")
    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse Scalp : {e}")

# ==============================================================================
# 10. ORDER BLOCKS
# ==============================================================================

def find_swing_highs_lows(candles, swing_len):
    highs, lows = [], []
    n = len(candles)
    if n < swing_len * 2 + 1:
        return highs, lows
    for i in range(swing_len, n - swing_len):
        hi = float(candles[i]["high"]); lo = float(candles[i]["low"])
        if all(hi > float(candles[i - j]["high"]) and hi > float(candles[i + j]["high"]) for j in range(1, swing_len + 1)):
            highs.append({"index": i, "price": hi})
        if all(lo < float(candles[i - j]["low"]) and lo < float(candles[i + j]["low"]) for j in range(1, swing_len + 1)):
            lows.append({"index": i, "price": lo})
    return highs, lows

def detect_bos(candles, swing_highs, swing_lows):
    out = []
    n = len(candles)
    for sh in swing_highs:
        for i in range(sh["index"] + 1, n):
            if float(candles[i]["close"]) > sh["price"] and float(candles[i - 1]["close"]) <= sh["price"]:
                out.append({"type": "BULL", "bos_index": i, "swing_index": sh["index"], "swing_price": sh["price"]})
                break
    for sl in swing_lows:
        for i in range(sl["index"] + 1, n):
            if float(candles[i]["close"]) < sl["price"] and float(candles[i - 1]["close"]) >= sl["price"]:
                out.append({"type": "BEAR", "bos_index": i, "swing_index": sl["index"], "swing_price": sl["price"]})
                break
    out.sort(key=lambda b: b["bos_index"])
    return out

def find_poc(candles, from_idx, to_idx, n_bins=POC_BINS):
    if from_idx >= to_idx or from_idx < 0 or to_idx >= len(candles):
        return None, 0
    min_p = min(float(candles[i]["low"]) for i in range(from_idx, to_idx + 1))
    max_p = max(float(candles[i]["high"]) for i in range(from_idx, to_idx + 1))
    if min_p >= max_p:
        return None, 0
    step = (max_p - min_p) / n_bins or 1e-9
    diff = [0.0] * (n_bins + 1)
    for i in range(from_idx, to_idx + 1):
        s_bin = max(0, min(n_bins - 1, int((float(candles[i]["low"]) - min_p) / step)))
        e_bin = max(0, min(n_bins - 1, int((float(candles[i]["high"]) - min_p) / step)))
        diff[s_bin] += 1.0
        if e_bin + 1 < n_bins:
            diff[e_bin + 1] -= 1.0
    best_bin, best_cnt, run = 0, 0.0, 0.0
    for i in range(n_bins):
        run += diff[i]
        if run > best_cnt:
            best_cnt, best_bin = run, i
    return min_p + (best_bin + 0.5) * step, int(best_cnt)

def count_touches_poc(candles, from_idx, to_idx, poc):
    if poc is None:
        return 0
    return sum(1 for i in range(from_idx, to_idx + 1) if float(candles[i]["low"]) <= poc <= float(candles[i]["high"]))

def find_best_poc_candle(candles, from_idx, to_idx, poc, is_bull):
    if poc is None or from_idx >= to_idx:
        return None
    best_idx, best_extreme = None, None
    for i in range(to_idx, from_idx - 1, -1):
        lo = float(candles[i]["low"]); hi = float(candles[i]["high"])
        if not (lo <= poc <= hi):
            continue
        if is_bull:
            if best_extreme is None or lo < best_extreme:
                best_extreme, best_idx = lo, i
        elif best_extreme is None or hi > best_extreme:
            best_extreme, best_idx = hi, i
    return best_idx

def has_gap_between(candles, anchor_idx, bos_idx, is_bull):
    a, b = min(anchor_idx, bos_idx), max(anchor_idx, bos_idx)
    for i in range(a + 1, b + 1):
        if is_bull and float(candles[i]["low"]) > float(candles[i - 1]["high"]):
            return True
        if not is_bull and float(candles[i]["high"]) < float(candles[i - 1]["low"]):
            return True
    return False

def calculate_ob_score(candles, bos, anchor_idx, top, bottom):
    score = 0
    bos_idx, zone_type = bos["bos_index"], bos["type"]
    zone_size = top - bottom
    if zone_size <= 0:
        return 0
    atr = calculate_atr(candles, 14)
    if not atr or zone_size <= atr * OB_MAX_SIZE_ATR_MULT:
        score += 1
    vols = [float(c.get("tickVolume", c.get("volume", 0)) or 0) for c in candles[-50:]]
    avg_v = sum(vols) / len(vols) if vols else 0
    anchor_vol = float(candles[anchor_idx].get("tickVolume", candles[anchor_idx].get("volume", 0)) or 0)
    if avg_v <= 0 or anchor_vol >= avg_v * OB_MIN_VOLUME_RATIO:
        score += 1
    if len(candles) - 1 - bos_idx <= OB_MAX_BOS_AGE:
        score += 1
    pen = 0
    for i in range(bos_idx + 1, len(candles)):
        ch, cl = float(candles[i]["high"]), float(candles[i]["low"])
        if ch >= bottom and cl <= top and min(ch, top) - max(cl, bottom) > zone_size * OB_MAX_PENETRATION_PCT:
            pen += 1
    if pen == 0:
        score += 1
    ema = calculate_ema(candles, OB_EMA_PERIOD)
    last_close = float(candles[-1]["close"])
    if ema and ((zone_type == "BULL" and last_close > ema) or (zone_type == "BEAR" and last_close < ema)):
        score += 1
    return score

def create_ob_zone(candles, bos):
    bos_idx, swing_idx = bos["bos_index"], bos["swing_index"]
    is_bull = bos["type"] == "BULL"
    if bos_idx <= swing_idx:
        return None
    poc, _ = find_poc(candles, swing_idx, bos_idx)
    if poc is None or count_touches_poc(candles, swing_idx, bos_idx, poc) < MIN_TOUCHES_POC:
        return None
    anchor_idx = find_best_poc_candle(candles, swing_idx, bos_idx, poc, is_bull)
    if anchor_idx is None:
        return None
    if GAP_FILTER_ENABLED and has_gap_between(candles, anchor_idx, bos_idx, is_bull):
        return None
    top = float(candles[anchor_idx]["high"]); bottom = float(candles[anchor_idx]["low"])
    if top <= bottom:
        return None
    # Zone déjà invalidée depuis le BOS ?
    for i in range(bos_idx + 1, len(candles)):
        if is_bull and (float(candles[i]["low"]) if INVALIDATION_METHOD == "Wick" else float(candles[i]["close"])) < bottom:
            return None
        if not is_bull and (float(candles[i]["high"]) if INVALIDATION_METHOD == "Wick" else float(candles[i]["close"])) > top:
            return None
    score = calculate_ob_score(candles, bos, anchor_idx, top, bottom)
    if score < OB_MIN_SCORE:
        return None
    return {"type": bos["type"], "top": top, "bottom": bottom,
            "anchor_time": str(candles[anchor_idx]["time"]),   # identité stable (les index glissent)
            "active": True, "order_placed": False, "order_id": None, "order_ts": None,
            "quality_score": score, "fibo_level": FIBO_LEVEL_BY_SCORE.get(score, 0.5)}

def update_zones(candles, zones, symbol):
    last = candles[-1]
    lh, ll, lc = float(last["high"]), float(last["low"]), float(last["close"])
    for z in zones:
        if not z["active"]:
            continue
        if z["type"] == "BULL":
            inv = (ll if INVALIDATION_METHOD == "Wick" else lc) < z["bottom"]
        else:
            inv = (lh if INVALIDATION_METHOD == "Wick" else lc) > z["top"]
        if inv:
            z["active"] = False
            log(f"🔴 [{symbol}] Zone OB {z['type']} invalidée")

def cleanup_zones(book, symbol):
    """Un ordre disparu (exécuté, SL/TP, expiré, annulé) = zone consommée : on ne la rejoue pas."""
    ids = {o.get("id") for o in book["orders"]}
    held = {p.get("symbol") for p in book["positions"]}
    for z in ob_zones.get(symbol, []):
        oid = z.get("order_id")
        if not oid or oid in ids:
            continue
        if z.get("order_ts") and time.monotonic() - z["order_ts"] < 30:
            continue                      # laisse le temps à la synchro
        z["active"] = False
        z["order_id"] = None
        log(f"♻️ [{symbol}] Zone OB consommée (ordre {oid} terminé{' / position ouverte' if symbol in held else ''})")

async def analyze_ob(account, connection, book, symbol):
    try:
        cleanup_zones(book, symbol)
        if not session_ok(symbol):
            return
        ok, spec = await is_symbol_tradable(connection, symbol)
        if not ok:
            return
        candles = await get_candles(account, symbol, TIMEFRAME_ENTRY, CANDLE_COUNT_OB)
        if not candles or len(candles) < SWING_LEN * 3:
            return

        zones = ob_zones.setdefault(symbol, [])
        sh, sl = find_swing_highs_lows(candles, SWING_LEN)
        for bos in detect_bos(candles, sh, sl)[-5:]:
            z = create_ob_zone(candles, bos)
            if z and not any(x["anchor_time"] == z["anchor_time"] and x["type"] == z["type"] for x in zones):
                zones.append(z)
                log(f"🆕 [{symbol}] Zone OB {z['type']} {z['quality_score']}/5 | top={z['top']:.2f} bot={z['bottom']:.2f}")
        update_zones(candles, zones, symbol)

        # Purge : on garde les zones encore dans la fenêtre de bougies (nécessaire à la déduplication)
        times = {str(c["time"]) for c in candles}
        ob_zones[symbol] = zones = [z for z in zones if z["anchor_time"] in times]

        trend = await h1_trend(account, symbol) if OB_REQUIRE_H1_ALIGN else None
        if OB_REQUIRE_H1_ALIGN and not trend:
            return

        last_close = float(candles[-1]["close"])
        atr = calculate_atr(candles, 14) or 0.0

        def dist(z):
            if last_close > z["top"]: return last_close - z["top"]
            if last_close < z["bottom"]: return z["bottom"] - last_close
            return 0.0

        active = [z for z in zones if z["active"]]
        if OB_REQUIRE_H1_ALIGN:
            active = [z for z in active if (z["type"] == "BULL") == (trend == "BUY")]
        active.sort(key=lambda z: (-z["quality_score"], dist(z)))
        active = active[:MAX_ZONES_TO_TRACK]

        n_orders = sum(1 for o in book["orders"] if o.get("symbol") == symbol)
        for z in active:
            if n_orders >= MAX_LIMIT_ORDERS_PER_SYMBOL or not can_open(book, symbol):
                break
            if z["order_placed"]:
                continue
            size = z["top"] - z["bottom"]
            fibo = z["fibo_level"]
            if z["type"] == "BULL":
                direction = "BUY"
                entry = z["top"] - size * fibo
                sl_p = z["bottom"] - size * SL_BUFFER_FACTOR
            else:
                direction = "SELL"
                entry = z["bottom"] + size * fibo
                sl_p = z["top"] + size * SL_BUFFER_FACTOR
            res = await place_trade(connection, spec, book, symbol, "OB", direction, entry, sl_p,
                                    f"OB {z['quality_score']}/5 fibo {fibo}")
            if res:
                z["order_placed"] = True
                z["order_id"] = res if res is not True else None
                z["order_ts"] = time.monotonic()
                n_orders += 1
    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse OB : {e}")

# ==============================================================================
# 11. GESTION DES ORDRES EN ATTENTE
# ==============================================================================

async def manage_pending(connection, book, cancel_all=False):
    for o in list(book["orders"]):
        try:
            if o.get("type") not in ("ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_SELL_LIMIT"):
                continue
            symbol = o.get("symbol")
            strat = STRATEGY_OF.get(symbol)
            if not strat:
                continue
            reason = None
            if cancel_all:
                reason = "garde-fou risque"
            else:
                created = to_dt(o.get("time"))
                if created and (now_utc() - created) > timedelta(minutes=STRAT[strat]["max_order_age_min"]):
                    reason = "trop ancien"
                else:
                    entry = float(o.get("openPrice", 0)); sl = float(o.get("stopLoss", 0) or 0)
                    if entry and sl:
                        price = await connection.get_symbol_price(symbol)
                        mid = (float(price["bid"]) + float(price["ask"])) / 2
                        if abs(mid - entry) > MAX_DISTANCE_FACTOR * abs(entry - sl):
                            reason = "prix trop éloigné"
            if reason:
                log(f"🗑️ [{symbol}] Annulation ordre {o['id']} ({reason})")
                await connection.cancel_order(o["id"])
                book["orders"].remove(o)
        except Exception as e:
            log(f"[{o.get('symbol')}] Erreur annulation : {e}")

# ==============================================================================
# 12. GESTION DES POSITIONS (BE + trailing en R, par ID)
# ==============================================================================

async def manage_positions(connection, book):
    live_ids = {p["id"] for p in book["positions"]}
    for pid in [k for k in POSITION_STATE if k not in live_ids]:
        POSITION_STATE.pop(pid, None)

    for pos in book["positions"]:
        symbol = pos.get("symbol")
        strat = STRATEGY_OF.get(symbol)
        if not strat:
            continue
        try:
            cfg = STRAT[strat]
            spec = await get_spec(connection, symbol)
            digits = int(spec.get("digits", 2))
            md = get_min_stop_distance(spec, digits)
            pid = pos["id"]
            openp = float(pos["openPrice"])
            cur = float(pos.get("currentPrice") or 0)
            sl = float(pos.get("stopLoss") or 0)
            tp = pos.get("takeProfit")
            if cur <= 0:
                continue

            st = POSITION_STATE.setdefault(pid, {"ir": 0.0})
            if st["ir"] <= 0:                       # R initial : déduit du TP (robuste au redémarrage)
                if tp:
                    st["ir"] = abs(float(tp) - openp) / cfg["rr"]
                elif sl:
                    st["ir"] = abs(openp - sl)
            ir = st["ir"]
            if ir <= 0:
                continue

            is_buy = pos.get("type") == "POSITION_TYPE_BUY"
            profit = (cur - openp) if is_buy else (openp - cur)
            cands = []
            if profit >= ir * cfg["be_r"]:
                cands.append(openp + ir * BE_LOCK_R if is_buy else openp - ir * BE_LOCK_R)
            if profit >= ir * cfg["trail_start_r"]:
                cands.append(cur - ir * cfg["trail_dist_r"] if is_buy else cur + ir * cfg["trail_dist_r"])
            if not cands:
                continue

            new_sl = normalize_price(max(cands) if is_buy else min(cands), digits)
            improves = (sl == 0) or (new_sl > sl if is_buy else new_sl < sl)
            if not improves:
                continue
            if sl and abs(new_sl - sl) < ir * MIN_SL_STEP_R:
                continue
            if abs(cur - new_sl) < md:             # trop proche du prix pour le broker
                continue
            if (is_buy and new_sl >= cur) or (not is_buy and new_sl <= cur):
                continue
            log(f"🛡️ [{symbol}] SL #{pid} {sl} → {new_sl}")
            await connection.modify_position(pid, stop_loss=new_sl, take_profit=tp)
        except Exception as e:
            log(f"[{symbol}] Erreur gestion position : {e}")

# ==============================================================================
# 13. HEALTH CHECK
# ==============================================================================

async def health_check_server():
    port = int(os.getenv("PORT", 10000))

    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"Arkas Bot OK"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
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
# 14. BOUCLE PRINCIPALE
# ==============================================================================

async def trading_loop(account, connection):
    errors = 0
    while True:
        try:
            entries_allowed = await update_risk_state(connection)
            book = await load_book(connection)

            await manage_pending(connection, book, cancel_all=not entries_allowed)
            await manage_positions(connection, book)

            if entries_allowed:
                for s in PA_SYMBOLS:
                    await analyze_pa(account, connection, book, s)
                    await asyncio.sleep(0.5)
                for s in OB_SYMBOLS:
                    await analyze_ob(account, connection, book, s)
                    await asyncio.sleep(0.5)
                for s in SCALP_SYMBOLS:
                    await analyze_scalp(account, connection, book, s)
                    await asyncio.sleep(0.5)

            errors = 0
            await asyncio.sleep(SCAN_INTERVAL)
        except Exception as e:
            errors += 1
            wait = min(60, 5 * errors)
            log(f"⚠️ Erreur boucle ({errors}) : {e} — pause {wait}s")
            await asyncio.sleep(wait)

# ==============================================================================
# 15. MAIN
# ==============================================================================

async def main():
    print("=" * 60, flush=True)
    print("🚀 ARKAS BOT v3 — PA + OB + Scalp (risque contrôlé)", flush=True)
    print(f"PA    : {', '.join(PA_SYMBOLS)}", flush=True)
    print(f"OB    : {', '.join(OB_SYMBOLS)}", flush=True)
    print(f"SCALP : {', '.join(SCALP_SYMBOLS)}", flush=True)
    print(f"Région : {METAAPI_REGION} | Magic : {MAGIC}", flush=True)
    print("=" * 60, flush=True)

    if not TOKEN:
        raise RuntimeError("METAAPI_TOKEN manquant.")
    if not ACCOUNT_ID:
        raise RuntimeError("METAAPI_ACCOUNT_ID manquant.")

    asyncio.create_task(health_check_server())
    api = MetaApi(TOKEN, {"region": METAAPI_REGION})

    try:
        account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
        log(f"Compte : {account.name} ({account.state})")
        if account.state != "DEPLOYED":
            log("Déploiement...")
            await account.deploy()
        await account.wait_connected()
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized(60)

        info = await connection.get_account_information()
        if info.get("currency") != "USD":
            log(f"⚠️ Devise du compte = {info.get('currency')} : le calcul du lot suppose USD, vérifiez-le.")
        log("🟢 BOT v3 CONNECTÉ")
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
