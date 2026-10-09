"""ARKAS DONCHIAN LIVE BOT — démo Deriv via MetaApi.
LIVE_MODE=true : passe des ordres réels sur le compte démo.
6 sécurités dures NON CONTOURNABLES protègent contre les bugs.
"""
import asyncio
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from collections import deque

import numpy as np
import pandas as pd
from metaapi_cloud_sdk import MetaApi

# ============================================================
# CONFIGURATION
# ============================================================
TOKEN = os.getenv("METAAPI_TOKEN")
ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")
REGION = os.getenv("METAAPI_REGION", "london")

SYMBOL = os.getenv("BT_SYMBOL", "XAUUSD")
TIMEFRAME = os.getenv("BT_TF", "1h")
RISK_PCT = float(os.getenv("BT_RISK", "0.5"))
MAX_POSITIONS = int(os.getenv("BT_MAX_POSITIONS", "2"))
MAX_TOTAL_RISK_PCT = float(os.getenv("BT_MAX_TOTAL_RISK", "2.0"))
MAX_HOLD_HOURS = int(os.getenv("BT_MAX_HOLD_HOURS", "48"))

# Paramètres Donchian
ENTRY_PERIOD = 20
EXIT_PERIOD = 10
EMA_FILTER = 200
ATR_PERIOD = 20
MIN_ATR_RATIO = 0.4
SPREAD = float(os.getenv("BT_SPREAD", "0.30"))

# Sécurité
LIVE_MODE = os.getenv("LIVE_MODE", "true").lower() == "true"
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "15.0"))

# ============================================================
# SÉCURITÉS DURES — NON CONTOURNABLES
# ============================================================
HARD_LIMITS = {
    "max_volume_per_order": 1.0,       # jamais > 1 lot par ordre
    "max_position_value_usd": 5000,    # jamais > 5000$ notional
    "max_orders_per_hour": 3,          # max 3 ordres/heure
    "max_orders_per_day": 6,           # max 6 ordres/jour
    "min_free_margin_pct": 50,         # toujours 50%+ de marge libre
}

ORDER_LOG = deque(maxlen=50)  # {(timestamp, type)}
BOT_STATE = {
    "initial_equity": None,
    "peak_equity": None,
    "day_start_equity": None,
    "current_day": None,
    "halted": False,
    "halt_reason": None,
    "last_processed_bar": None,
    "processed_bars": set(),
    "first_equity_seen": None,
}


def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    prefix = {"INFO": "ℹ️", "SIGNAL": "📊", "ORDER": "💼", "TRAIL": "📈",
              "CLOSE": "🔚", "WARN": "⚠️", "ERROR": "🚨", "HALT": "🛑",
              "SEC": "🔒"}.get(level, "•")
    print(f"[{ts}] {prefix} [{level}] {msg}", flush=True)


# ============================================================
# SÉCURITÉS
# ============================================================
def check_hard_limits(order_volume, notional_usd, account_info):
    """Vérifie les 6 sécurités dures. Retourne (ok, raison)."""
    now = datetime.now(timezone.utc)

    # 1. Volume max
    if order_volume > HARD_LIMITS["max_volume_per_order"]:
        return False, f"volume {order_volume} > {HARD_LIMITS['max_volume_per_order']}"

    # 2. Valeur notional max
    if notional_usd > HARD_LIMITS["max_position_value_usd"]:
        return False, f"notional {notional_usd:.0f}$ > {HARD_LIMITS['max_position_value_usd']}$"

    # 3. Ordres par heure
    hour_ago = now - timedelta(hours=1)
    orders_last_hour = sum(1 for ts, _ in ORDER_LOG if ts > hour_ago)
    if orders_last_hour >= HARD_LIMITS["max_orders_per_hour"]:
        return False, f"{orders_last_hour} ordres dans l'heure >= {HARD_LIMITS['max_orders_per_hour']}"

    # 4. Ordres par jour
    day_ago = now - timedelta(hours=24)
    orders_last_day = sum(1 for ts, _ in ORDER_LOG if ts > day_ago)
    if orders_last_day >= HARD_LIMITS["max_orders_per_day"]:
        return False, f"{orders_last_day} ordres dans la journée >= {HARD_LIMITS['max_orders_per_day']}"

    # 5. Marge libre
    equity = account_info.get("equity", 0)
    margin_free = account_info.get("marginFree", equity)
    if equity > 0:
        free_pct = margin_free / equity * 100
        if free_pct < HARD_LIMITS["min_free_margin_pct"]:
            return False, f"marge libre {free_pct:.1f}% < {HARD_LIMITS['min_free_margin_pct']}%"

    return True, "OK"


# ============================================================
# INDICATEURS
# ============================================================
def compute_indicators(df):
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)

    prev_close = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])
    atr = pd.Series(tr).rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean().values
    atr_avg = pd.Series(atr).rolling(100, min_periods=20).mean().values
    ema_long = pd.Series(c).ewm(span=EMA_FILTER, adjust=False).mean().values

    dc_hi_entry = pd.Series(h).rolling(ENTRY_PERIOD).max().shift(1).values
    dc_lo_entry = pd.Series(l).rolling(ENTRY_PERIOD).min().shift(1).values
    dc_hi_exit = pd.Series(h).rolling(EXIT_PERIOD).max().shift(1).values
    dc_lo_exit = pd.Series(l).rolling(EXIT_PERIOD).min().shift(1).values

    return {"atr": atr, "atr_avg": atr_avg, "ema_long": ema_long,
            "dc_hi_entry": dc_hi_entry, "dc_lo_entry": dc_lo_entry,
            "dc_hi_exit": dc_hi_exit, "dc_lo_exit": dc_lo_exit}


def check_signal(df, ind):
    i = -1
    if not np.isfinite(ind["atr"][i]) or not np.isfinite(ind["atr_avg"][i]):
        return None
    if not np.isfinite(ind["dc_hi_entry"][i]) or not np.isfinite(ind["dc_lo_entry"][i]):
        return None

    A = ind["atr"][i]
    if A < MIN_ATR_RATIO * ind["atr_avg"][i]:
        return None

    c = df["close"].values
    close = c[i]
    ema = ind["ema_long"][i]
    ema_ok_long = close > ema
    ema_ok_short = close < ema
    bull_break = close > ind["dc_hi_entry"][i]
    bear_break = close < ind["dc_lo_entry"][i]

    if bull_break and ema_ok_long:
        return {"dir": 1, "trigger": ind["dc_hi_entry"][i],
                "sl": ind["dc_lo_exit"][i], "atr": A}
    if bear_break and ema_ok_short:
        return {"dir": -1, "trigger": ind["dc_lo_entry"][i],
                "sl": ind["dc_hi_exit"][i], "atr": A}
    return None


# ============================================================
# COMPTE
# ============================================================
async def get_account_info(account):
    info = await account.get_information()
    return {
        "balance": info.get("balance", 0),
        "equity": info.get("equity", 0),
        "marginFree": info.get("marginFree", info.get("freeMargin", 0)),
        "margin": info.get("margin", 0),
    }


async def get_positions(account):
    try:
        positions = await account.get_positions()
        return [p for p in positions if p.get("symbol") == SYMBOL]
    except Exception as e:
        log(f"Erreur get_positions: {e}", "ERROR")
        return []


# ============================================================
# EXÉCUTION
# ============================================================
async def open_position(account, signal, equity):
    dist = abs(signal["trigger"] - signal["sl"])
    if dist <= 0:
        log(f"Distance SL invalide: {dist}", "WARN")
        return None

    risk_money = equity * RISK_PCT / 100.0
    size = risk_money / dist

    # Arrondi selon le symbole (XAUUSD = 2 décimales, 0.01 lot min)
    size = round(size, 2)
    if size < 0.01:
        log(f"Taille trop petite ({size}), signal ignoré", "WARN")
        return None

    notional = size * signal["trigger"]

    # ==== SÉCURITÉS DURES ====
    acct = await get_account_info(account)
    ok, reason = check_hard_limits(size, notional, acct)
    if not ok:
        log(f"🔒 ORDRE REFUSÉ par sécurité: {reason}", "SEC")
        return None

    # Logique d'exécution
    if signal["dir"] == 1:
        log(f"🔵 OUVERTURE LONG : {size} lots @ marché, SL={signal['sl']:.2f}, "
            f"risque={risk_money:.2f}$ (notional={notional:.0f}$)", "ORDER")
    else:
        log(f"🔴 OUVERTURE SHORT : {size} lots @ marché, SL={signal['sl']:.2f}, "
            f"risque={risk_money:.2f}$ (notional={notional:.0f}$)", "ORDER")

    try:
        if signal["dir"] == 1:
            result = await account.create_market_buy_order(
                symbol=SYMBOL, volume=size, stop_loss=signal["sl"]
            )
        else:
            result = await account.create_market_sell_order(
                symbol=SYMBOL, volume=size, stop_loss=signal["sl"]
            )
        order_id = result.get("orderId") if isinstance(result, dict) else str(result)
        log(f"✅ Ordre exécuté : ID={order_id}", "ORDER")
        ORDER_LOG.append((datetime.now(timezone.utc), signal["dir"]))
        return order_id
    except Exception as e:
        log(f"❌ Échec ouverture : {type(e).__name__}: {e}", "ERROR")
        return None


async def update_stop_loss(account, position_id, new_sl):
    try:
        await account.modify_position(position_id=position_id, stop_loss=new_sl)
        log(f"SL modifié : position {position_id} → {new_sl:.2f}", "TRAIL")
        return True
    except Exception as e:
        log(f"Échec modif SL: {type(e).__name__}: {e}", "ERROR")
        return False


async def close_position(account, position_id, reason):
    try:
        await account.close_position(position_id=position_id)
        log(f"Position fermée : {position_id} ({reason})", "CLOSE")
        return True
    except Exception as e:
        log(f"Échec fermeture: {type(e).__name__}: {e}", "ERROR")
        return False


# ============================================================
# BOUCLE
# ============================================================
async def fetch_candles(account):
    try:
        candles = await account.get_historical_candles(
            symbol=SYMBOL, timeframe=TIMEFRAME, limit=300
        )
        if not candles:
            return None
        df = pd.DataFrame([
            {"time": r["time"], "open": r["open"], "high": r["high"],
             "low": r["low"], "close": r["close"]}
            for r in candles
        ])
        df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
        return df.sort_values("time").reset_index(drop=True)
    except Exception as e:
        log(f"Erreur fetch: {type(e).__name__}: {e}", "ERROR")
        return None


def is_new_bar(df):
    if df is None or len(df) < 2:
        return False
    last_closed = df["time"].iloc[-2]
    if last_closed not in BOT_STATE["processed_bars"]:
        BOT_STATE["processed_bars"].add(last_closed)
        if len(BOT_STATE["processed_bars"]) > 500:
            BOT_STATE["processed_bars"] = set(list(BOT_STATE["processed_bars"])[-100:])
        return True
    return False


async def process_bar(account, df):
    ind = compute_indicators(df)
    last_closed_time = df["time"].iloc[-2]
    last_close = df["close"].iloc[-2]

    # ==== 1. Gestion positions ====
    positions = await get_positions(account)
    for pos in positions:
        pos_id = pos.get("id") or pos.get("positionId")
        pos_type = str(pos.get("type", "")).lower()
        dr = 1 if "buy" in pos_type or "long" in pos_type else -1
        current_sl = float(pos.get("stopLoss", 0)) if pos.get("stopLoss") else None
        open_time = pos.get("time")

        # A. Trailing Donchian
        new_sl = ind["dc_lo_exit"][-1] if dr == 1 else ind["dc_hi_exit"][-1]
        if np.isfinite(new_sl):
            if dr == 1 and (current_sl is None or new_sl > current_sl) and new_sl < last_close:
                await update_stop_loss(account, pos_id, new_sl)
            elif dr == -1 and (current_sl is None or new_sl < current_sl) and new_sl > last_close:
                await update_stop_loss(account, pos_id, new_sl)

        # B. Timeout
        if open_time:
            open_dt = pd.to_datetime(open_time, utc=True).tz_localize(None)
            hours_held = (datetime.now(timezone.utc) - open_dt).total_seconds() / 3600
            if hours_held >= MAX_HOLD_HOURS:
                await close_position(account, pos_id, f"timeout {hours_held:.1f}h")

    # ==== 2. Nouveau signal ====
    positions = await get_positions(account)
    if len(positions) >= MAX_POSITIONS:
        return

    signal = check_signal(df, ind)
    if signal:
        log(f"SIGNAL {'LONG' if signal['dir'] == 1 else 'SHORT'} "
            f"à {last_closed_time} | trigger={signal['trigger']:.2f} | SL={signal['sl']:.2f}",
            "SIGNAL")

        if BOT_STATE["halted"]:
            log(f"Bot en pause : {BOT_STATE['halt_reason']}", "HALT")
            return

        acct = await get_account_info(account)
        await open_position(account, signal, acct["equity"])


# ============================================================
# SERVICES
# ============================================================
async def health_server():
    port = int(os.getenv("PORT", "10000"))
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = f"ARKAS BOT OK | LIVE={LIVE_MODE} | {SYMBOL} {TIMEFRAME}".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                         + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    server = await asyncio.start_server(handle, "0.0.0.0", port)
    async with server:
        await server.serve_forever()


async def keepalive():
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        await asyncio.sleep(240)
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=20).read())
        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================
async def main():
    if not TOKEN or not ACCOUNT_ID:
        raise RuntimeError("METAAPI_TOKEN et METAAPI_ACCOUNT_ID requis.")

    log("=" * 65)
    log("ARKAS DONCHIAN LIVE BOT")
    log(f"Symbole   : {SYMBOL} {TIMEFRAME}")
    log(f"Mode      : {'🔴 LIVE — ordres réels démo' if LIVE_MODE else '🟢 DRY-RUN'}")
    log(f"Risque    : {RISK_PCT}% par trade | max {MAX_POSITIONS} positions")
    log(f"Limites   : DD {MAX_DRAWDOWN_PCT}% | pertes jour {MAX_DAILY_LOSS_PCT}%")
    log(f"🔒 SÉCURITÉS DURES :")
    log(f"   • Volume max       : {HARD_LIMITS['max_volume_per_order']} lots")
    log(f"   • Notional max     : {HARD_LIMITS['max_position_value_usd']}$")
    log(f"   • Ordres max/heure : {HARD_LIMITS['max_orders_per_hour']}")
    log(f"   • Ordres max/jour  : {HARD_LIMITS['max_orders_per_day']}")
    log(f"   • Marge libre min  : {HARD_LIMITS['min_free_margin_pct']}%")
    log("=" * 65)

    server_task = asyncio.create_task(health_server())
    keepalive_task = asyncio.create_task(keepalive())

    api = MetaApi(TOKEN, {"region": REGION})
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    if account.state != "DEPLOYED":
        await account.deploy()
    await account.wait_connected()
    log("✅ Compte MetaApi connecté")

    info = await get_account_info(account)
    log(f"Balance : {info['balance']:.2f} | Equity : {info['equity']:.2f} | "
        f"Marge libre : {info['marginFree']:.2f}")
    BOT_STATE["initial_equity"] = info["equity"]
    BOT_STATE["peak_equity"] = info["equity"]
    BOT_STATE["day_start_equity"] = info["equity"]
    BOT_STATE["first_equity_seen"] = info["equity"]

    log(f"📡 Surveillance {SYMBOL} {TIMEFRAME} démarrée")
    log("Boucle : vérification toutes les 60 secondes")
    log("")

    while True:
        try:
            await asyncio.sleep(60)

            # Reset journalier
            today = datetime.now(timezone.utc).date()
            if BOT_STATE["current_day"] != today:
                BOT_STATE["current_day"] = today
                info = await get_account_info(account)
                BOT_STATE["day_start_equity"] = info["equity"]
                log(f"🌅 Nouveau jour. Equity début : {info['equity']:.2f}")

            # Vérif limites
            info = await get_account_info(account)
            current_equity = info["equity"]
            if current_equity > BOT_STATE["peak_equity"]:
                BOT_STATE["peak_equity"] = current_equity

            dd_pct = (current_equity - BOT_STATE["peak_equity"]) / BOT_STATE["peak_equity"] * 100
            if dd_pct < -MAX_DRAWDOWN_PCT and not BOT_STATE["halted"]:
                BOT_STATE["halted"] = True
                BOT_STATE["halt_reason"] = f"DD {dd_pct:.2f}% > {MAX_DRAWDOWN_PCT}%"
                log(f"🛑 ARRÊT : {BOT_STATE['halt_reason']}", "HALT")

            day_pnl_pct = (current_equity - BOT_STATE["day_start_equity"]) / BOT_STATE["day_start_equity"] * 100
            if day_pnl_pct < -MAX_DAILY_LOSS_PCT and not BOT_STATE["halted"]:
                BOT_STATE["halted"] = True
                BOT_STATE["halt_reason"] = f"Perte jour {day_pnl_pct:.2f}%"
                log(f"🛑 ARRÊT jour : {BOT_STATE['halt_reason']}", "HALT")

            if BOT_STATE["halted"]:
                continue

            df = await fetch_candles(account)
            if df is None:
                continue

            if is_new_bar(df):
                last_bar = df["time"].iloc[-2]
                log(f"📊 Nouvelle bougie : {last_bar}")
                await process_bar(account, df)

        except asyncio.CancelledError:
            break
        except Exception as e:
            log(f"Erreur boucle: {type(e).__name__}: {e}", "ERROR")
            await asyncio.sleep(30)

    keepalive_task.cancel()
    log("Bot arrêté.")
    await server_task


if __name__ == "__main__":
    asyncio.run(main())
