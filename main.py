import asyncio
import os
import sys
import math
from datetime import datetime

from metaapi_cloud_sdk import MetaApi


# ==============================================================================
# ARKAS OB BOT v3 — ORDER BLOCKS HAUTE QUALITÉ + 2 ORDRES LIMITES PAR ACTIF
# ==============================================================================
# Basé sur l'indicateur Pine Script "Order Blocks Volume Delta 3D | Flux Charts"
#
# LOGIQUE :
#   1. Détection des swings (pivot high/low sur 5 bougies)
#   2. Détection du BOS (cassure swing + confirmation)
#   3. Création de la zone OB (bougie avec le plus haut volume entre swing et BOS)
#   4. SCORE DE QUALITÉ (0-7) — seules les zones ≥ 4/7 sont gardées
#   5. Détection des retests
#   6. Placement de 2 ORDRES LIMITES MAX par symbole
#   7. Break-Even + Trailing sur les positions exécutées
#   8. Invalidation automatique des zones cassées
# ==============================================================================


# ==============================================================================
# 1. CONFIGURATION METAAPI
# ==============================================================================

TOKEN = os.getenv("METAAPI_TOKEN")

ACCOUNT_ID = os.getenv(
    "METAAPI_ACCOUNT_ID",
    "de1c262b-17d4-415c-bb1f-c49db0342d25"
)


# ==============================================================================
# 2. MARCHÉS
# ==============================================================================

TIMEFRAME = "15m"

SYMBOLS = [
    "XAUUSD.m",
    "BTCUSD.m",
    "USDJPY.m",
    "GBPJPY.m"
]


# ==============================================================================
# 3. STRATÉGIE ORDER BLOCK
# ==============================================================================

SWING_LEN = 5
MAX_ZONES_TO_TRACK = 3
CANDLE_COUNT = 200

INVALIDATION_METHOD = "Wick"

RR_RATIO = 2.0

# 2 ordres limites maximum simultanés par symbole
MAX_LIMIT_ORDERS_PER_SYMBOL = 2

# Score minimum de qualité (0-7)
MIN_QUALITY_SCORE = 4


# ==============================================================================
# 4. MONEY MANAGEMENT
# ==============================================================================

RISK_PERCENT = 0.5

VOLUME_LIMITS = {
    "XAUUSD.m": {"min": 0.01, "max": 0.03},
}


# ==============================================================================
# 5. BREAK-EVEN / TRAILING
# ==============================================================================

BREAK_EVEN_R = 1.0
TRAILING_ENABLED = True
TRAILING_START_R = 1.5
TRAILING_DISTANCE_R = 0.7


# ==============================================================================
# 6. BOUCLE
# ==============================================================================

SCAN_INTERVAL = 10


# ==============================================================================
# 7. ÉTAT GLOBAL
# ==============================================================================

ob_zones = {}
placed_orders = {}


# ==============================================================================
# 8. LOG
# ==============================================================================

def log(message):
    print(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}",
        flush=True
    )


# ==============================================================================
# 9. RÉCUPÉRATION DES BOUGIES
# ==============================================================================

async def get_candles(account, symbol, timeframe, limit):
    try:
        candles = await account.get_historical_candles(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit
        )
        if not candles:
            return None
        if len(candles) > 2:
            candles = candles[:-1]
        return candles
    except Exception as e:
        log(f"❌ [{symbol}] Erreur récupération bougies : {e}")
        return None


# ==============================================================================
# 10. DÉTECTION DES SWINGS
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
            if float(candles[i]["high"]) <= float(candles[i - j]["high"]):
                is_sh = False
                break
            if float(candles[i]["high"]) <= float(candles[i + j]["high"]):
                is_sh = False
                break
        if is_sh:
            highs.append({"index": i, "price": float(candles[i]["high"])})

        is_sl = True
        for j in range(1, swing_len + 1):
            if float(candles[i]["low"]) >= float(candles[i - j]["low"]):
                is_sl = False
                break
            if float(candles[i]["low"]) >= float(candles[i + j]["low"]):
                is_sl = False
                break
        if is_sl:
            lows.append({"index": i, "price": float(candles[i]["low"])})

    return highs, lows


# ==============================================================================
# 11. DÉTECTION DU BOS
# ==============================================================================

def detect_bos(candles, swing_highs, swing_lows):
    bos_list = []
    n = len(candles)
    if n < 5:
        return bos_list

    for sh in swing_highs:
        sh_idx = sh["index"]
        sh_price = sh["price"]
        for i in range(sh_idx + 1, n):
            prev_close = float(candles[i - 1]["close"])
            curr_close = float(candles[i]["close"])
            if curr_close > sh_price and prev_close <= sh_price:
                bos_list.append({
                    "type": "BULL",
                    "bos_index": i,
                    "swing_index": sh_idx,
                    "swing_price": sh_price
                })
                break

    for sl in swing_lows:
        sl_idx = sl["index"]
        sl_price = sl["price"]
        for i in range(sl_idx + 1, n):
            prev_close = float(candles[i - 1]["close"])
            curr_close = float(candles[i]["close"])
            if curr_close < sl_price and prev_close >= sl_price:
                bos_list.append({
                    "type": "BEAR",
                    "bos_index": i,
                    "swing_index": sl_idx,
                    "swing_price": sl_price
                })
                break

    return bos_list


# ==============================================================================
# 12. SCORE DE QUALITÉ D'UNE ZONE OB (0-7)
# ==============================================================================

def calculate_quality_score(candles, bos, anchor_idx, top, bottom):
    score = 0
    bos_idx = bos["bos_index"]
    zone_type = bos["type"]

    zone_size = top - bottom
    if zone_size <= 0:
        return 0

    # 1. IMPULSION FORTE après le BOS
    swing_price = bos["swing_price"]
    bos_close = float(candles[bos_idx]["close"])
    impulse = abs(bos_close - swing_price)
    if impulse >= zone_size * 2.0:
        score += 1

    # 2. VOLUME ÉLEVÉ sur la bougie d'ancrage
    volumes = [float(c.get("volume", 0) or 0) for c in candles[-50:]]
    avg_vol = sum(volumes) / len(volumes) if volumes else 0
    anchor_vol = float(candles[anchor_idx].get("volume", 0) or 0)
    if avg_vol > 0 and anchor_vol >= avg_vol * 1.3:
        score += 1

    # 3. TAILLE DE LA ZONE ÉQUILIBRÉE
    recent_ranges = []
    for c in candles[-20:]:
        r = float(c["high"]) - float(c["low"])
        recent_ranges.append(r)
    avg_range = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0
    if avg_range > 0:
        ratio = zone_size / avg_range
        if 0.5 <= ratio <= 2.0:
            score += 1

    # 4. BOS CLAIR (cassure nette)
    bos_candle = candles[bos_idx]
    bos_body = abs(float(bos_candle["close"]) - float(bos_candle["open"]))
    bos_range = float(bos_candle["high"]) - float(bos_candle["low"])
    if bos_range > 0 and bos_body / bos_range >= 0.6:
        score += 1

    # 5. FRAÎCHEUR DE LA ZONE
    bars_since_creation = len(candles) - 1 - bos_idx
    if bars_since_creation <= 30:
        score += 1

    # 6. ZONE NON MITIGÉE
    penetration_count = 0
    for i in range(bos_idx + 1, len(candles)):
        c = candles[i]
        c_high = float(c["high"])
        c_low = float(c["low"])
        if c_high >= bottom and c_low <= top:
            penetration = min(c_high, top) - max(c_low, bottom)
            if penetration > zone_size * 0.5:
                penetration_count += 1
    if penetration_count == 0:
        score += 1

    # 7. ALIGNEMENT AVEC LA TENDANCE (EMA50 approximation)
    closes = [float(c["close"]) for c in candles]
    if len(closes) >= 50:
        ema50 = sum(closes[-50:]) / 50
        last_close = closes[-1]
        if zone_type == "BULL" and last_close > ema50:
            score += 1
        elif zone_type == "BEAR" and last_close < ema50:
            score += 1

    return score


# ==============================================================================
# 13. CRÉATION DE LA ZONE OB AVEC FILTRE QUALITÉ
# ==============================================================================

def create_ob_zone(candles, bos):
    bos_idx = bos["bos_index"]
    swing_idx = bos["swing_index"]

    if bos_idx <= swing_idx:
        return None

    best_idx = None
    best_vol = -1
    for i in range(swing_idx, bos_idx + 1):
        vol = float(candles[i].get("volume", 0) or 0)
        if vol > best_vol:
            best_vol = vol
            best_idx = i

    if best_idx is None:
        return None

    top = float(candles[best_idx]["high"])
    bottom = float(candles[best_idx]["low"])

    if top <= bottom:
        return None

    quality = calculate_quality_score(candles, bos, best_idx, top, bottom)

    # On ne garde QUE les zones avec un score >= MIN_QUALITY_SCORE
    if quality < MIN_QUALITY_SCORE:
        return None

    return {
        "type": bos["type"],
        "top": top,
        "bottom": bottom,
        "anchor_index": best_idx,
        "bos_index": bos_idx,
        "created_at": candles[bos_idx].get("time", ""),
        "active": True,
        "retested": False,
        "order_placed": False,
        "order_id": None,
        "quality_score": quality,
    }


# ==============================================================================
# 14. MISE À JOUR DES ZONES
# ==============================================================================

def update_zones(candles, zones, symbol):
    if not zones:
        return zones

    last = candles[-1]
    last_high = float(last["high"])
    last_low = float(last["low"])
    last_close = float(last["close"])

    for zone in zones:
        if not zone["active"]:
            continue

        touches = last_high >= zone["bottom"] and last_low <= zone["top"]
        if touches:
            zone["retested"] = True

        invalid = False
        if zone["type"] == "BULL":
            if INVALIDATION_METHOD == "Wick":
                invalid = last_low < zone["bottom"]
            else:
                invalid = last_close < zone["bottom"]
        else:
            if INVALIDATION_METHOD == "Wick":
                invalid = last_high > zone["top"]
            else:
                invalid = last_close > zone["top"]

        if invalid:
            zone["active"] = False
            log(f"🔴 [{symbol}] Zone OB {zone['type']} invalidée "
                f"(top={zone['top']}, bottom={zone['bottom']})")

    return zones


# ==============================================================================
# 15. NORMALISATION
# ==============================================================================

def normalize_price(price, digits):
    return round(float(price), int(digits))


def normalize_volume(volume, minimum, maximum, step):
    if step <= 0:
        step = 0.01
    volume = max(minimum, min(volume, maximum))
    steps = math.floor(volume / step)
    volume = steps * step
    if step >= 0.1:
        decimals = 1
    elif step >= 0.01:
        decimals = 2
    elif step >= 0.001:
        decimals = 3
    else:
        decimals = 4
    return round(max(volume, minimum), decimals)


# ==============================================================================
# 16. CALCUL VOLUME
# ==============================================================================

async def calculate_volume(connection, symbol, entry, stop_loss):
    try:
        account_info = await connection.get_account_information()
        balance = float(account_info.get("balance", 0))
        if balance <= 0:
            return None

        specification = await connection.get_symbol_specification(symbol)

        minimum = float(specification.get("minVolume", 0.01))
        maximum = float(specification.get("maxVolume", 100))
        step = float(specification.get("volumeStep", 0.01))
        contract_size = float(
            specification.get("contractSize",
                              specification.get("tradeContractSize", 1))
        )
        if contract_size <= 0:
            return None

        if symbol in VOLUME_LIMITS:
            limits = VOLUME_LIMITS[symbol]
            minimum = max(minimum, limits["min"])
            maximum = min(maximum, limits["max"])

        risk_money = balance * RISK_PERCENT / 100
        price_distance = abs(entry - stop_loss)
        if price_distance <= 0:
            return None

        risk_per_lot = price_distance * contract_size
        if risk_per_lot <= 0:
            return None

        raw_volume = risk_money / risk_per_lot
        volume = normalize_volume(raw_volume, minimum, maximum, step)

        if symbol in VOLUME_LIMITS:
            limits = VOLUME_LIMITS[symbol]
            volume = max(limits["min"], min(volume, limits["max"]))
            volume = normalize_volume(volume, limits["min"], limits["max"], step)

        return volume
    except Exception as e:
        log(f"[{symbol}] Erreur calcul volume : {e}")
        return None


# ==============================================================================
# 17. COMPTER LES ORDRES LIMITES ACTIFS
# ==============================================================================

async def count_active_limit_orders(connection, symbol):
    try:
        orders = await connection.get_orders()
        count = 0
        for o in orders:
            if o.get("symbol") != symbol:
                continue
            otype = o.get("type", "")
            if otype in ("ORDER_TYPE_BUY_LIMIT", "ORDER_TYPE_SELL_LIMIT"):
                count += 1
        return count
    except Exception as e:
        log(f"[{symbol}] Erreur récupération ordres : {e}")
        return 999


# ==============================================================================
# 18. PLACEMENT D'UN ORDRE LIMITE
# ==============================================================================

async def place_limit_order(connection, symbol, zone):
    try:
        price = await connection.get_symbol_price(symbol)
        bid = float(price["bid"])
        ask = float(price["ask"])

        specification = await connection.get_symbol_specification(symbol)
        digits = int(specification.get("digits", 5))

        if zone["type"] == "BULL":
            entry = normalize_price(zone["top"], digits)
            stop_loss = normalize_price(zone["bottom"], digits)

            if ask <= entry:
                log(f"⚠️ [{symbol}] BUY LIMIT impossible : ask={ask} <= entry={entry}")
                return None

            risk = entry - stop_loss
            take_profit = normalize_price(entry + risk * RR_RATIO, digits)
        else:
            entry = normalize_price(zone["bottom"], digits)
            stop_loss = normalize_price(zone["top"], digits)

            if bid >= entry:
                log(f"⚠️ [{symbol}] SELL LIMIT impossible : bid={bid} >= entry={entry}")
                return None

            risk = stop_loss - entry
            take_profit = normalize_price(entry - risk * RR_RATIO, digits)

        if risk <= 0:
            return None

        volume = await calculate_volume(connection, symbol, entry, stop_loss)
        if volume is None or volume <= 0:
            return None

        print()
        print("==================================================")
        if zone["type"] == "BULL":
            print(f"🟢 ARKAS OB — BUY LIMIT — {symbol} (score={zone.get('quality_score')}/7)")
        else:
            print(f"🔴 ARKAS OB — SELL LIMIT — {symbol} (score={zone.get('quality_score')}/7)")
        print(f"Entry : {entry}")
        print(f"SL    : {stop_loss}")
        print(f"TP    : {take_profit}")
        print(f"Volume: {volume}")
        print("==================================================")

        if zone["type"] == "BULL":
            order = await connection.create_limit_buy_order(
                symbol=symbol,
                volume=volume,
                open_price=entry,
                stop_loss=stop_loss,
                take_profit=take_profit
            )
        else:
            order = await connection.create_limit_sell_order(
                symbol=symbol,
                volume=volume,
                open_price=entry,
                stop_loss=stop_loss,
                take_profit=take_profit
            )

        log(f"✅ [{symbol}] Ordre LIMITE {zone['type']} placé (orderId={order.get('orderId')})")

        zone["order_placed"] = True
        zone["order_id"] = order.get("orderId")

        return order
    except Exception as e:
        log(f"❌ [{symbol}] Erreur placement ordre limite : {e}")
        return None


# ==============================================================================
# 19. NETTOYAGE DES ZONES DONT L'ORDRE N'EXISTE PLUS
# ==============================================================================

async def cleanup_zones(connection, symbol):
    if symbol not in ob_zones:
        return

    try:
        orders = await connection.get_orders()
        active_order_ids = set()
        for o in orders:
            oid = o.get("id") or o.get("orderId")
            if oid:
                active_order_ids.add(oid)

        for zone in ob_zones[symbol]:
            oid = zone.get("order_id")
            if oid and oid not in active_order_ids:
                zone["order_placed"] = False
                zone["order_id"] = None
    except Exception as e:
        log(f"[{symbol}] Erreur cleanup zones : {e}")


# ==============================================================================
# 20. BREAK-EVEN
# ==============================================================================

async def manage_break_even(connection):
    try:
        positions = await connection.get_positions()
        for position in positions:
            symbol = position.get("symbol")
            if symbol not in SYMBOLS:
                continue

            try:
                entry = float(position["openPrice"])
                current = float(position["currentPrice"])
                stop_loss = position.get("stopLoss")

                if stop_loss is None:
                    continue
                stop_loss = float(stop_loss)

                risk = abs(entry - stop_loss)
                if risk <= 0:
                    continue

                position_type = position.get("type")

                if position_type == "POSITION_TYPE_BUY":
                    profit = current - entry
                    if profit >= risk * BREAK_EVEN_R and stop_loss < entry:
                        log(f"🟢 [{symbol}] BREAK-EVEN BUY #{position['id']}")
                        await connection.modify_position(
                            position["id"],
                            stop_loss=entry,
                            take_profit=position.get("takeProfit")
                        )
                elif position_type == "POSITION_TYPE_SELL":
                    profit = entry - current
                    if profit >= risk * BREAK_EVEN_R and stop_loss > entry:
                        log(f"🔴 [{symbol}] BREAK-EVEN SELL #{position['id']}")
                        await connection.modify_position(
                            position["id"],
                            stop_loss=entry,
                            take_profit=position.get("takeProfit")
                        )
            except Exception as e:
                log(f"[{symbol}] Erreur BE position : {e}")
    except Exception as e:
        log(f"Erreur Break-Even global : {e}")


# ==============================================================================
# 21. TRAILING STOP
# ==============================================================================

async def manage_trailing(connection):
    if not TRAILING_ENABLED:
        return
    try:
        positions = await connection.get_positions()
        for position in positions:
            symbol = position.get("symbol")
            if symbol not in SYMBOLS:
                continue

            try:
                entry = float(position["openPrice"])
                current = float(position["currentPrice"])
                stop_loss = position.get("stopLoss")
                if stop_loss is None:
                    continue
                stop_loss = float(stop_loss)

                current_distance = abs(entry - stop_loss)
                if current_distance <= 0:
                    continue

                position_type = position.get("type")

                if position_type == "POSITION_TYPE_BUY":
                    profit = current - entry
                    if profit >= current_distance * TRAILING_START_R:
                        new_sl = current - (current_distance * TRAILING_DISTANCE_R)
                        if new_sl > stop_loss:
                            log(f"📈 [{symbol}] TRAILING BUY SL {stop_loss} -> {new_sl}")
                            await connection.modify_position(
                                position["id"],
                                stop_loss=new_sl,
                                take_profit=position.get("takeProfit")
                            )
                elif position_type == "POSITION_TYPE_SELL":
                    profit = entry - current
                    if profit >= current_distance * TRAILING_START_R:
                        new_sl = current + (current_distance * TRAILING_DISTANCE_R)
                        if new_sl < stop_loss:
                            log(f"📉 [{symbol}] TRAILING SELL SL {stop_loss} -> {new_sl}")
                            await connection.modify_position(
                                position["id"],
                                stop_loss=new_sl,
                                take_profit=position.get("takeProfit")
                            )
            except Exception as e:
                log(f"[{symbol}] Erreur trailing : {e}")
    except Exception as e:
        log(f"Erreur trailing global : {e}")


# ==============================================================================
# 22. ANALYSE D'UN SYMBOLE
# ==============================================================================

async def analyze_symbol(account, connection, symbol):
    try:
        # 1. Nettoyage des zones dont l'ordre n'existe plus
        await cleanup_zones(connection, symbol)

        # 2. Bougies
        candles = await get_candles(account, symbol, TIMEFRAME, CANDLE_COUNT)
        if not candles or len(candles) < SWING_LEN * 3:
            return

        # 3. Swings
        swing_highs, swing_lows = find_swing_highs_lows(candles, SWING_LEN)

        # 4. BOS
        bos_list = detect_bos(candles, swing_highs, swing_lows)

        # 5. Zones OB (avec filtre qualité)
        if symbol not in ob_zones:
            ob_zones[symbol] = []

        recent_bos = bos_list[-5:] if len(bos_list) >= 5 else bos_list

        for bos in recent_bos:
            zone = create_ob_zone(candles, bos)
            if zone is None:
                continue

            already_exists = False
            for z in ob_zones[symbol]:
                if z.get("anchor_index") == zone["anchor_index"] and z["type"] == zone["type"]:
                    already_exists = True
                    break

            if not already_exists:
                ob_zones[symbol].append(zone)
                log(f"🆕 [{symbol}] Nouvelle zone OB {zone['type']} "
                    f"top={zone['top']} bottom={zone['bottom']} "
                    f"(score={zone['quality_score']}/7)")

        # 6. Mise à jour des zones
        ob_zones[symbol] = update_zones(candles, ob_zones[symbol], symbol)

        # 7. Zones actives triées par qualité puis par proximité
        active_zones = [z for z in ob_zones[symbol] if z["active"]]

        last_close = float(candles[-1]["close"])

        def distance_to_price(z):
            if last_close > z["top"]:
                return last_close - z["top"]
            elif last_close < z["bottom"]:
                return z["bottom"] - last_close
            else:
                return 0.0

        # Tri : score décroissant puis distance croissante
        active_zones.sort(key=lambda z: (-z.get("quality_score", 0), distance_to_price(z)))

        # On limite aux MAX_ZONES_TO_TRACK meilleures zones
        active_zones = active_zones[:MAX_ZONES_TO_TRACK]

        # 8. Comptage des ordres limites déjà actifs
        active_limit_orders = await count_active_limit_orders(connection, symbol)

        log(f"[{symbol}] Zones actives={len(active_zones)} | "
            f"Ordres limites actifs={active_limit_orders}/{MAX_LIMIT_ORDERS_PER_SYMBOL}")

        # 9. Placement des ordres limites (si on n'a pas atteint la limite)
        for zone in active_zones:
            if active_limit_orders >= MAX_LIMIT_ORDERS_PER_SYMBOL:
                break

            if zone.get("order_placed", False):
                continue

            if not zone["retested"]:
                continue

            result = await place_limit_order(connection, symbol, zone)
            if result is not None:
                active_limit_orders += 1

    except Exception as e:
        log(f"❌ [{symbol}] Erreur analyse : {e}")


# ==============================================================================
# 23. MAIN
# ==============================================================================

async def main():
    print()
    print("==================================================")
    print("🚀 ARKAS OB BOT v3 — QUALITÉ + 2 ORDRES PAR ACTIF")
    print("==================================================")
    print(f"Timeframe       : {TIMEFRAME}")
    print(f"Symboles        : {', '.join(SYMBOLS)}")
    print(f"Risque          : {RISK_PERCENT}%")
    print(f"RR              : 1:{RR_RATIO}")
    print(f"Swing Len       : {SWING_LEN}")
    print(f"Zones max       : {MAX_ZONES_TO_TRACK}")
    print(f"Score min       : {MIN_QUALITY_SCORE}/7")
    print(f"Ordres/symbole  : {MAX_LIMIT_ORDERS_PER_SYMBOL}")
    print(f"Limites         : {VOLUME_LIMITS}")
    print(f"Invalidation    : {INVALIDATION_METHOD}")
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

        connection = account.get_rpc_connection()
        log("Connexion au serveur MT5...")
        await connection.connect()
        log("Synchronisation...")
        await connection.wait_synchronized()
        log("🟢 ARKAS OB BOT CONNECTÉ AVEC SUCCÈS")

        while True:
            try:
                for symbol in SYMBOLS:
                    await analyze_symbol(account, connection, symbol)
                    await asyncio.sleep(2)

                # Gestion des positions ouvertes
                await manage_break_even(connection)
                await manage_trailing(connection)

                await asyncio.sleep(SCAN_INTERVAL)

            except Exception as e:
                log(f"⚠️ Erreur dans la boucle : {e}")
                await asyncio.sleep(10)

    except Exception as e:
        log(f"❌ ERREUR FATALE : {e}")
        raise
    finally:
        try:
            await api.close()
        except Exception:
            pass


# ==============================================================================
# 24. START
# ==============================================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 ARKAS OB BOT arrêté.")
    except Exception as e:
        print(f"\n❌ ARKAS OB BOT arrêté : {e}")
