"""
Backtest SCALP XAUUSD (M5) 100 % MetaApi — rapport dans les logs, AUCUN ordre passé.

Stratégie testée (pullback en tendance) :
  - Tendance : close H1 vs EMA 50 H1 (reconstruit depuis le M5, bougies H1 clôturées seulement)
  - Alignement M5 : EMA rapide (20) au-dessus / en dessous de l'EMA lente (50)
  - Signal : la bougie M5 clôturée touche l'EMA 20 puis la rejette (couleur dans le sens du trade)
  - Entrée à l'ouverture de la bougie suivante ; SL = bas/haut de la bougie + 0.1 ATR, borné [0.8 ; 2.0] ATR
  - TP = 1.5 x le risque ; sortie forcée après 24 bougies (2 h) ou à la fin de la session
  - Filtres : session liquide, spread <= 15 % du stop
  - Garde-fous : stop du jour après 2 pertes d'affilée, ou -2 % dans la journée

Variables d'environnement (facultatives, en plus de METAAPI_TOKEN / METAAPI_ACCOUNT_ID / METAAPI_REGION) :
  BT_SYMBOL   (défaut XAUUSD)
  BT_YEARS    (défaut 0.75 : environ 9 mois de M5)
  BT_SESSION  (défaut "9,18" : heures de début et de fin, EN HEURE DU BROKER)
  BT_SPREAD   (défaut 0.30 : spread aller-retour en prix)
"""
import argparse
import asyncio
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from metaapi_cloud_sdk import MetaApi

TOKEN = os.getenv("METAAPI_TOKEN")
ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")
REGION = os.getenv("METAAPI_REGION", "london")
SYMBOL = os.getenv("BT_SYMBOL", "XAUUSD")
YEARS = float(os.getenv("BT_YEARS", "0.75"))
SESSION = tuple(int(x) for x in os.getenv("BT_SESSION", "9,18").split(","))
SPREAD = float(os.getenv("BT_SPREAD", "0.30"))
TIMEFRAME = "5m"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------------------
# Historique
# ------------------------------------------------------------------------------
async def fetch_history(account, symbol, timeframe, years):
    target = datetime.now(timezone.utc) - timedelta(days=365.25 * years)
    rows, start, prev_oldest = [], None, None
    while True:
        batch = await account.get_historical_candles(symbol=symbol, timeframe=timeframe,
                                                     start_time=start, limit=1000)
        if not batch:
            break
        rows = list(batch) + rows
        oldest = batch[0]["time"]
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        if len(rows) % 5000 < 1000:
            log(f"[{symbol}] {len(rows)} bougies, la plus ancienne : {oldest:%Y-%m-%d}")
        if oldest <= target or (prev_oldest is not None and oldest >= prev_oldest) or len(rows) > 90000:
            break
        prev_oldest = oldest
        start = oldest - timedelta(seconds=1)
        await asyncio.sleep(0.2)
    if len(rows) < 3:
        return None
    df = pd.DataFrame([{"time": r["time"], "open": r["open"], "high": r["high"],
                        "low": r["low"], "close": r["close"]} for r in rows])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    return df.iloc[:-1].reset_index(drop=True)       # retire la bougie en formation


# ------------------------------------------------------------------------------
# Moteur
# ------------------------------------------------------------------------------
def h1_trend_series(df, ema_p):
    s = df.set_index("time")["close"].resample("1h").last().dropna()
    ema = s.ewm(span=ema_p, adjust=False).mean()
    tr = np.sign(s - ema)
    tr.index = tr.index + pd.Timedelta(hours=1)       # connu seulement une fois l'heure terminée
    return tr.reindex(pd.DatetimeIndex(df["time"]), method="ffill").fillna(0).values


def make_params(**over):
    p = argparse.Namespace(spread=SPREAD, slippage=0.05, risk=0.25, rr=1.5, atr=14,
                           ema_fast=20, ema_slow=50, h1_ema=50, sl_min_atr=0.8, sl_max_atr=2.0,
                           max_spread_ratio=0.15, max_hold=24, max_consec=2, daily_loss=2.0,
                           sess_start=SESSION[0], sess_end=SESSION[1], capital=10000.0,
                           long_only=False)
    for k, v in over.items():
        setattr(p, k, v)
    return p


def run_scalp(df, p, start_idx=0):
    d = df.reset_index(drop=True)
    o, h, l, c = (d[k].values.astype(float) for k in ("open", "high", "low", "close"))
    n = len(d)
    pc = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    atr = pd.Series(tr).rolling(p.atr).mean().values
    ema_f = pd.Series(c).ewm(span=p.ema_fast, adjust=False).mean().values
    ema_s = pd.Series(c).ewm(span=p.ema_slow, adjust=False).mean().values
    trend = h1_trend_series(d, p.h1_ema)
    hours = d["time"].dt.hour.values
    in_sess = (hours >= p.sess_start) & (hours < p.sess_end) & (d["time"].dt.weekday.values < 5)
    days = d["time"].dt.normalize().values
    t = d["time"].values

    half = p.spread / 2.0
    equity = p.capital
    pos, pending = None, None
    trades = []
    curve = np.full(n, np.nan)
    cur_day, day_start, halted, consec = None, equity, False, 0
    info = {"signals": 0, "skipped_spread": 0}

    def close_trade(i, raw, reason):
        nonlocal equity, pos, consec, halted
        dr = pos["dir"]
        fill = raw - dr * (half + p.slippage)
        pnl = (fill - pos["entry"]) * dr * pos["size"]
        equity += pnl
        trades.append({"entry_time": pos["entry_time"], "exit_time": t[i],
                       "dir": "LONG" if dr == 1 else "SHORT", "pnl": pnl,
                       "R": pnl / pos["risk_money"],
                       "cost_R": (p.spread + 2 * p.slippage) / pos["dist"],
                       "bars": i - pos["entry_i"] + 1, "reason": reason})
        consec = consec + 1 if pnl <= 0 else 0
        if consec >= p.max_consec or (equity - day_start) / day_start * 100 <= -p.daily_loss:
            halted = True
        pos = None

    for i in range(n):
        if days[i] != cur_day:
            cur_day, day_start, halted, consec = days[i], equity, False, 0

        # 1) entrée à l'ouverture
        if pending is not None and pos is None:
            dr, dist = pending
            if in_sess[i] and not halted and equity > 0:
                entry = o[i] + dr * (half + p.slippage)
                risk_money = equity * p.risk / 100.0
                pos = {"dir": dr, "entry": entry, "stop": entry - dr * dist,
                       "tp": entry + dr * dist * p.rr, "dist": dist,
                       "size": risk_money / dist, "risk_money": risk_money,
                       "entry_time": t[i], "entry_i": i}
        pending = None

        # 2) gestion : SL avant TP dans une même bougie (hypothèse prudente)
        if pos is not None:
            if pos["dir"] == 1:
                if o[i] <= pos["stop"]:
                    close_trade(i, o[i], "stop (gap)")
                elif o[i] >= pos["tp"]:
                    close_trade(i, o[i], "objectif (gap)")
                elif l[i] <= pos["stop"]:
                    close_trade(i, pos["stop"], "stop")
                elif h[i] >= pos["tp"]:
                    close_trade(i, pos["tp"], "objectif")
            else:
                if o[i] >= pos["stop"]:
                    close_trade(i, o[i], "stop (gap)")
                elif o[i] <= pos["tp"]:
                    close_trade(i, o[i], "objectif (gap)")
                elif h[i] >= pos["stop"]:
                    close_trade(i, pos["stop"], "stop")
                elif l[i] <= pos["tp"]:
                    close_trade(i, pos["tp"], "objectif")
            if pos is not None:
                if i - pos["entry_i"] + 1 >= p.max_hold:
                    close_trade(i, c[i], "temps")
                elif i + 1 >= n or not in_sess[i + 1]:
                    close_trade(i, c[i], "fin de session")

        # 3) signal à la clôture
        if (pos is None and i >= start_idx and in_sess[i] and not halted
                and i + 1 < n and in_sess[i + 1]
                and not np.isnan(atr[i]) and trend[i] != 0):
            dr, raw = 0, 0.0
            if trend[i] == 1 and ema_f[i] > ema_s[i] and l[i] <= ema_f[i] and c[i] > ema_f[i] and c[i] > o[i]:
                dr, raw = 1, c[i] - l[i]
            elif (not p.long_only and trend[i] == -1 and ema_f[i] < ema_s[i]
                  and h[i] >= ema_f[i] and c[i] < ema_f[i] and c[i] < o[i]):
                dr, raw = -1, h[i] - c[i]
            if dr:
                dist = min(max(raw + 0.1 * atr[i], p.sl_min_atr * atr[i]), p.sl_max_atr * atr[i])
                info["signals"] += 1
                if p.spread > p.max_spread_ratio * dist:
                    info["skipped_spread"] += 1
                else:
                    pending = (dr, dist)

        curve[i] = equity if pos is None else equity + (c[i] - pos["entry"]) * pos["dir"] * pos["size"]

    if pos is not None:
        close_trade(n - 1, c[-1], "fin des données")
        curve[-1] = equity
    return pd.DataFrame(trades), pd.Series(curve, index=d["time"]).ffill(), info


# ------------------------------------------------------------------------------
# Métriques et rapport (affichage étroit, lisible sur téléphone)
# ------------------------------------------------------------------------------
def metrics(trades, curve, capital, start_time=None):
    if start_time is not None:
        curve = curve[curve.index >= start_time]
    m = {"n": len(trades)}
    if len(curve) < 2:
        return m
    base = curve.iloc[0] if start_time is not None else capital
    m["ret"] = (curve.iloc[-1] / base - 1) * 100
    m["dd"] = ((curve / curve.cummax()) - 1).min() * 100
    m["days"] = max((curve.index[-1] - curve.index[0]).days, 1)
    if len(trades):
        r = trades["R"]
        gp, gl = r[r > 0].sum(), -r[r <= 0].sum()
        m["win"] = (r > 0).mean() * 100
        m["exp"] = r.mean()
        m["exp_gross"] = (r + trades["cost_R"]).mean()
        m["cost"] = trades["cost_R"].mean()
        m["pf"] = gp / gl if gl > 0 else float("inf")
        streak = best = 0
        for x in r.values:
            streak = streak + 1 if x <= 0 else 0
            best = max(best, streak)
        m["streak"] = best
        m["bars"] = trades["bars"].mean()
        m["reasons"] = trades["reason"].value_counts().to_dict()
    return m


def show(title, m, info, bh=None):
    print(f"\n=== {title} ===", flush=True)
    if not m.get("n"):
        print("Aucun trade.", flush=True)
        return
    print(f"Jours             : {m['days']}", flush=True)
    print(f"Trades            : {m['n']} ({m['n'] / m['days']:.2f}/jour)", flush=True)
    print(f"Signaux rejetés   : {info['skipped_spread']}/{info['signals']} (spread)", flush=True)
    print(f"Réussite          : {m['win']:.1f} %", flush=True)
    print(f"Espérance NETTE   : {m['exp']:+.3f} R/trade", flush=True)
    print(f"Avant coûts       : {m['exp_gross']:+.3f} R/trade", flush=True)
    print(f"Coût moyen        : {m['cost']:.3f} R/trade", flush=True)
    print(f"Profit factor     : {m['pf']:.2f}", flush=True)
    print(f"Rendement         : {m['ret']:+.1f} %", flush=True)
    print(f"Drawdown max      : {m['dd']:.1f} %", flush=True)
    print(f"Pires pertes suite: {m['streak']}", flush=True)
    print(f"Durée moyenne     : {m['bars']:.1f} bougies", flush=True)
    print(f"Sorties           : {m['reasons']}", flush=True)
    if bh is not None:
        print(f"Buy & hold (réf.) : {bh:+.1f} %", flush=True)


def report(symbol, df, oos=0.3):
    p = make_params()
    print("\n" + "#" * 44, flush=True)
    print(f"# SCALP {symbol} M5 | {len(df)} bougies", flush=True)
    print(f"# {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d %H:%M}", flush=True)
    print(f"# Dernière bougie (heure broker) : {df['time'].iloc[-1]:%H:%M}", flush=True)
    print(f"# Session {p.sess_start}h-{p.sess_end}h | spread {p.spread} | risque {p.risk}%", flush=True)
    print("#" * 44, flush=True)
    if len(df) < 5000:
        print("Historique insuffisant (< 5000 bougies) pour conclure.", flush=True)
        return

    split = int(len(df) * (1 - oos))
    split_time = df["time"].iloc[split]
    tr, cv, inf = run_scalp(df.iloc[:split], p)
    bh = (df["close"].iloc[split - 1] / df["close"].iloc[0] - 1) * 100
    show("ECHANTILLON (70 %)", metrics(tr, cv, p.capital), inf, bh)
    tr, cv, inf = run_scalp(df, p, start_idx=split)
    bh = (df["close"].iloc[-1] / df["close"].iloc[split] - 1) * 100
    show("HORS-ECHANTILLON (30 %)", metrics(tr, cv, p.capital, split_time), inf, bh)

    print("\n=== SENSIBILITE AU SPREAD (net R | rend. | DD | n) ===", flush=True)
    for sp in (0.15, 0.30, 0.50, 0.80):
        tr, cv, _ = run_scalp(df, make_params(spread=sp))
        m = metrics(tr, cv, p.capital)
        line = (f"{m['exp']:+.2f}R {m['ret']:+.1f}% {m['dd']:.0f}% n={m['n']}" if m.get("n") else "aucun trade")
        print(f"spread {sp:.2f} : {line}", flush=True)

    print("\n=== ROBUSTESSE : RR x EMA rapide (net R | rend. | n) ===", flush=True)
    for rr in (1.0, 1.5, 2.0):
        for ef in (10, 20, 30):
            tr, cv, _ = run_scalp(df, make_params(rr=rr, ema_fast=ef))
            m = metrics(tr, cv, p.capital)
            line = f"{m['exp']:+.2f}R {m['ret']:+.1f}% n={m['n']}" if m.get("n") else "-"
            print(f"RR {rr} EMA{ef} : {line}", flush=True)
    print("\nLecture : l'espérance NETTE doit rester > 0 sur l'échantillon, "
          "le hors-échantillon ET quand le spread monte.", flush=True)


# ------------------------------------------------------------------------------
# Service (health check + keep-alive Render) et main
# ------------------------------------------------------------------------------
async def health_server():
    port = int(os.getenv("PORT", 10000))

    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"Backtest scalp OK"
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
    """Évite la mise en veille de l'instance gratuite pendant le téléchargement."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return
    while True:
        await asyncio.sleep(240)
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=20).read())
        except Exception:
            pass


async def main():
    if not TOKEN or not ACCOUNT_ID:
        raise RuntimeError("METAAPI_TOKEN et METAAPI_ACCOUNT_ID sont requis.")
    asyncio.create_task(health_server())
    ka = asyncio.create_task(keepalive())
    api = MetaApi(TOKEN, {"region": REGION})
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    if account.state != "DEPLOYED":
        await account.deploy()
    await account.wait_connected()
    log(f"Compte connecté. {SYMBOL} {TIMEFRAME} | {YEARS} an(s) | session {SESSION} | spread {SPREAD}")

    try:
        df = await fetch_history(account, SYMBOL, TIMEFRAME, YEARS)
        if df is None:
            print(f"[{SYMBOL}] Aucune bougie reçue (historique indisponible ?).", flush=True)
        else:
            report(SYMBOL, df)
    except Exception as e:
        print(f"[{SYMBOL}] Erreur : {e}", flush=True)

    ka.cancel()
    print("\nTERMINE. Aucun ordre n'a été passé. Vous pouvez arrêter ce service.", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
