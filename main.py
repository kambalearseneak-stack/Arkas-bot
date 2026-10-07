import asyncio
import os
import sys
import math
from datetime import datetime

from metaapi_cloud_sdk import MetaApi

# ==============================================================================
# ARKAS BOT — VERSION 6.0 (Place ordre LIMIT immédiatement au démarrage)
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
    "Volatility 75 Index"
]

# ==============================================================================
# 3. MONEY MANAGEMENT
# ==============================================================================

LOT_PER_SYMBOL = {
    "Step Index": 0.1,
    "Volatility 75 Index": 0.01
}

# ==============================================================================
# 4. PARAMÈTRES DES ORDRES LIMIT
# ==============================================================================

# Distance du prix d'entrée par rapport au prix actuel (en %)
LIMIT_OFFSET_PERCENT = 1.0     # 1% en retrait du prix actuel

# SL et TP (en % du prix d'entrée)
SL_DISTANCE_PERCENT = 2.0      # 2% de SL
TP_DISTANCE_PERCENT = 4.0      # 4% de TP (ratio 1:2)

# ==============================================================================
# 5. EXECUTION
# ==============================================================================

SCAN_INTERVAL = 60             # vérification toutes les 60 secondes
ORDER_EXPIRATION_SECONDS = 3600  # annule après 1 heure

# ==============================================================================
# 6. UTILS
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
# 7. PLACEMENT IMMÉDIAT DES ORDRES LIMIT
# ==============================================================================

async def place_limit_orders(connection, symbol):
    """Place un BUY_LIMIT et un SELL_LIMIT immédiatement."""
    log(f"\n{'='*60}")
    log(f"📋 [{symbol}] Placement des ordres LIMIT")
    log(f"{'='*60}")

    try:
        spec = await connection.get_symbol_specification(symbol)
        if not spec:
            log(f"❌ [{symbol}] Spec introuvable")
            return False

        digits = int(spec.get("digits", 2))
        min_vol = float(spec.get("minVolume", 0.01))
        max_vol = float(spec.get("maxVolume", 100))
        step = float(spec.get("volumeStep", 0.01))

        log(f"📋 [{symbol}] digits={digits} | minVol={min_vol} | maxVol={max_vol} | step={step}")

        price_info = await connection.get_symbol_price(symbol)
        bid = float(price_info["bid"])
        ask = float(price_info["ask"])
        spread = ask - bid
        log(f"💵 [{symbol}] Bid={bid} | Ask={ask} | Spread={spread:.4f}")

        # Prix des ordres limit (en retrait)
        buy_limit_price = normalize_price(bid * (1 - LIMIT_OFFSET_PERCENT / 100.0), digits)
        sell_limit_price = normalize_price(ask * (1 + LIMIT_OFFSET_PERCENT / 100.0), digits)

        # SL et TP pour BUY_LIMIT
        buy_sl = normalize_price(buy_limit_price * (1 - SL_DISTANCE_PERCENT / 100.0), digits)
        buy_tp = normalize_price(buy_limit_price * (1 + TP_DISTANCE_PERCENT / 100.0), digits)

        # SL et TP pour SELL_LIMIT
        sell_sl = normalize_price(sell_limit_price * (1 + SL_DISTANCE_PERCENT / 100.0), digits)
        sell_tp = normalize_price(sell_limit_price * (1 - TP_DISTANCE_PERCENT / 100.0), digits)

        # Volume
        target_vol = LOT_PER_SYMBOL.get(symbol, min_vol)
        volume = normalize_volume(target_vol, min_vol, max_vol, step)

        log(f"📌 [{symbol}] Volume={volume}")
        log(f"📌 [{symbol}] BUY_LIMIT={buy_limit_price} | SL={buy_sl} | TP={buy_tp}")
        log(f"📌 [{symbol}] SELL_LIMIT={sell_limit_price} | SL={sell_sl} | TP={sell_tp}")

        # ---- BUY_LIMIT ----
        log(f"\n🚀 [{symbol}] Envoi BUY_LIMIT...")
        try:
            buy_result = await connection.create_limit_buy_order(
                symbol=symbol,
                volume=volume,
                open_price=buy_limit_price,
                stop_loss=buy_sl,
                take_profit=buy_tp
            )
            buy_order_id = buy_result.get("orderId") if isinstance(buy_result, dict) else buy_result
            log(f"✅ [{symbol}] BUY_LIMIT placé | orderId={buy_order_id}")
        except Exception as e:
            log(f"❌ [{symbol}] BUY_LIMIT erreur : {e}")

        await asyncio.sleep(2)

        # ---- SELL_LIMIT ----
        log(f"\n🚀 [{symbol}] Envoi SELL_LIMIT...")
        try:
            sell_result = await connection.create_limit_sell_order(
                symbol=symbol,
                volume=volume,
                open_price=sell_limit_price,
                stop_loss=sell_sl,
                take_profit=sell_tp
            )
            sell_order_id = sell_result.get("orderId") if isinstance(sell_result, dict) else sell_result
            log(f"✅ [{symbol}] SELL_LIMIT placé | orderId={sell_order_id}")
        except Exception as e:
            log(f"❌ [{symbol}] SELL_LIMIT erreur : {e}")

        await asyncio.sleep(2)

        # ---- Vérification ----
        log(f"\n🔍 [{symbol}] Vérification des ordres en attente...")
        try:
            orders = await connection.get_orders()
            symbol_orders = [o for o in orders if o.get("symbol") == symbol]
            log(f"📊 [{symbol}] {len(symbol_orders)} ordre(s) en attente")
            for o in symbol_orders:
                log(f"   - ID={o.get('id')} | type={o.get('type')} | prix={o.get('openPrice')} | vol={o.get('volume')} | SL={o.get('stopLoss')} | TP={o.get('takeProfit')}")
        except Exception as e:
            log(f"⚠️ [{symbol}] Erreur get_orders : {e}")

        return True

    except Exception as e:
        log(f"❌ [{symbol}] Erreur globale : {e}")
        return False

# ==============================================================================
# 8. NETTOYAGE DES ORDRES EXPIRÉS
# ==============================================================================

async def cleanup_expired_orders(connection, symbol):
    """Annule les ordres LIMIT plus vieux que ORDER_EXPIRATION_SECONDS."""
    try:
        orders = await connection.get_orders()
        symbol_orders = [o for o in orders if o.get("symbol") == symbol]

        if not symbol_orders:
            return

        now = datetime.utcnow()
        for order in symbol_orders:
            order_id = order.get("id")
            created = order.get("time") or order.get("brokerTime")

            if not created:
                continue

            try:
                if isinstance(created, str):
                    created_dt = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(tzinfo=None)
                else:
                    created_dt = created
            except Exception:
                continue

            age = (now - created_dt).total_seconds()
            if age > ORDER_EXPIRATION_SECONDS:
                log(f"⏰ [{symbol}] Ordre {order_id} expiré ({age:.0f}s) → annulation")
                try:
                    await connection.cancel_order(order_id)
                    log(f"🗑️ [{symbol}] Ordre {order_id} annulé")
                except Exception as e:
                    log(f"⚠️ [{symbol}] Impossible d'annuler {order_id} : {e}")

    except Exception as e:
        log(f"❌ [{symbol}] Erreur cleanup : {e}")

# ==============================================================================
# 9. BOUCLE PRINCIPALE
# ==============================================================================

async def trading_loop(connection):
    """Boucle : place les ordres au démarrage, puis nettoie les expirés."""
    # === PLACEMENT IMMÉDIAT ===
    log("\n" + "=" * 60)
    log("🚀 PLACEMENT IMMÉDIAT DES ORDRES LIMIT")
    log("=" * 60)

    for symbol in SYMBOLS:
        await place_limit_orders(connection, symbol)
        await asyncio.sleep(2)

    log("\n" + "=" * 60)
    log("✅ Ordres LIMIT placés — Nettoyage périodique activé")
    log("=" * 60)

    # === BOUCLE DE NETTOYAGE ===
    while True:
        try:
            for symbol in SYMBOLS:
                await cleanup_expired_orders(connection, symbol)
            await asyncio.sleep(SCAN_INTERVAL)
        except Exception as e:
            log(f"⚠️ Erreur boucle : {e}")
            await asyncio.sleep(10)

# ==============================================================================
# 10. HEALTH CHECK HTTP POUR RENDER
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
# 11. MAIN
# ==============================================================================

async def main():
    print("=" * 60, flush=True)
    print("🚀 ARKAS BOT — VERSION 6.0", flush=True)
    print("📋 Placement IMMÉDIAT d'ordres LIMIT au démarrage", flush=True)
    print("=" * 60, flush=True)
    print(f"Symboles : {', '.join(SYMBOLS)}", flush=True)
    print(f"Lots : " + " | ".join([f"{s}={LOT_PER_SYMBOL[s]}" for s in SYMBOLS]), flush=True)
    print(f"Offset limite : {LIMIT_OFFSET_PERCENT}%", flush=True)
    print(f"SL : {SL_DISTANCE_PERCENT}% | TP : {TP_DISTANCE_PERCENT}%", flush=True)
    print(f"Expiration : {ORDER_EXPIRATION_SECONDS}s", flush=True)
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
        log("🟢 Connecté à MetaApi")

        await trading_loop(connection)

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
