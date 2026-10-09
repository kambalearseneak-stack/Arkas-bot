"""
Backtest Donchian (suivi de tendance) 100 % MetaApi — aucun fichier CSV, aucun PC.

Le script télécharge l'historique via MetaApi, rejoue la stratégie et AFFICHE le
rapport dans les logs. Il ne passe AUCUN ordre.

Variables d'environnement :
  METAAPI_TOKEN, METAAPI_ACCOUNT_ID, METAAPI_REGION (comme pour le bot)
  BT_SYMBOLS   (défaut "XAUUSD,BTCUSD")
  BT_TIMEFRAME (défaut "4h" ; mettre "1d" pour du journalier)
  BT_YEARS     (défaut 8 : profondeur d'historique demandée)

Disponibilité et profondeur de l'historique : elles dépendent de votre offre MetaApi
et du broker. Si peu de bougies sont reçues, le rapport l'indique.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd


# ------------------------------------------------------------------------------
# Chargement
# ------------------------------------------------------------------------------
def load_csv(path):
    df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [str(c).strip().strip("<>").lower() for c in df.columns]

    if "datetime" in df.columns:
        ts = df["datetime"]
    elif "date" in df.columns and "time" in df.columns:
        ts = df["date"].astype(str) + " " + df["time"].astype(str)
    elif "time" in df.columns:
        ts = df["time"]
    elif "date" in df.columns:
        ts = df["date"]
    elif "timestamp" in df.columns:
        ts = df["timestamp"]
    else:
        sys.exit("Colonne de date/heure introuvable (time, date, datetime, timestamp).")

    if pd.api.types.is_numeric_dtype(ts):
        unit = "ms" if ts.iloc[0] > 1e11 else "s"
        df["time"] = pd.to_datetime(ts, unit=unit)
    else:
        df["time"] = pd.to_datetime(ts, errors="coerce")

    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            sys.exit(f"Colonne manquante : {col}")
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["time", "open", "high", "low", "close"])
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    return df[["time", "open", "high", "low", "close"]]


def add_indicators(df, n_entry, n_exit, ema_p, atr_p):
    d = df.copy()
    pc = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(atr_p).mean()
    d["ema"] = d["close"].ewm(span=ema_p, adjust=False).mean()
    d["don_hi"] = d["high"].shift(1).rolling(n_entry).max()
    d["don_lo"] = d["low"].shift(1).rolling(n_entry).min()
    d["exit_lo"] = d["low"].shift(1).rolling(n_exit).min()
    d["exit_hi"] = d["high"].shift(1).rolling(n_exit).max()
    return d


# ------------------------------------------------------------------------------
# Moteur de backtest
# ------------------------------------------------------------------------------
def run_backtest(df, p, start_idx=0):
    d = add_indicators(df, p.n_entry, p.n_exit, p.ema, p.atr)
    t = d["time"].values
    o, h, l, c = (d[k].values for k in ("open", "high", "low", "close"))
    atr, ema = d["atr"].values, d["ema"].values
    dhi, dlo = d["don_hi"].values, d["don_lo"].values
    xlo, xhi = d["exit_lo"].values, d["exit_hi"].values

    n = len(d)
    half = p.spread / 2.0
    equity = p.capital
    pos = None
    pending = None
    trades = []
    curve = np.full(n, np.nan)

    def close_trade(i, fill_raw, reason):
        nonlocal equity, pos
        dr = pos["dir"]
        fill = fill_raw - dr * (half + p.slippage)          # sortie dégradée
        pnl = (fill - pos["entry"]) * dr * pos["size"]
        equity += pnl
        trades.append({
            "entry_time": pos["entry_time"], "exit_time": t[i],
            "dir": "LONG" if dr == 1 else "SHORT",
            "entry": pos["entry"], "exit": fill, "size": pos["size"],
            "pnl": pnl, "R": pnl / pos["risk_money"] if pos["risk_money"] > 0 else 0.0,
            "bars": i - pos["entry_i"], "reason": reason,
        })
        pos = None

    for i in range(n):
        # 1) Exécution de l'entrée en attente à l'ouverture
        if pending is not None and pos is None:
            dr, atr_sig = pending
            pending = None
            entry = o[i] + dr * (half + p.slippage)           # entrée dégradée
            dist = p.atr_mult * atr_sig
            if dist > 0 and equity > 0:
                risk_money = equity * p.risk / 100.0
                pos = {"dir": dr, "entry": entry, "stop": entry - dr * dist,
                       "size": risk_money / dist, "risk_money": risk_money,
                       "entry_time": t[i], "entry_i": i}

        # 2) Gestion de la position (trailing + stop), y compris sur la bougie d'entrée
        if pos is not None:
            dr = pos["dir"]
            if dr == 1:
                if not np.isnan(xlo[i]):
                    pos["stop"] = max(pos["stop"], xlo[i])
                if o[i] <= pos["stop"]:
                    close_trade(i, o[i], "stop (gap)")
                elif l[i] <= pos["stop"]:
                    close_trade(i, pos["stop"], "stop")
            else:
                if not np.isnan(xhi[i]):
                    pos["stop"] = min(pos["stop"], xhi[i])
                if o[i] >= pos["stop"]:
                    close_trade(i, o[i], "stop (gap)")
                elif h[i] >= pos["stop"]:
                    close_trade(i, pos["stop"], "stop")

        # 3) Signal à la clôture
        if pos is None and pending is None and i >= start_idx and i + 1 < n:
            if not (np.isnan(atr[i]) or np.isnan(dhi[i]) or np.isnan(dlo[i])):
                if c[i] > dhi[i] and c[i] > ema[i]:
                    pending = (1, atr[i])
                elif (not p.long_only) and c[i] < dlo[i] and c[i] < ema[i]:
                    pending = (-1, atr[i])

        # 4) Equity mark-to-market
        if pos is not None:
            curve[i] = equity + (c[i] - pos["entry"]) * pos["dir"] * pos["size"]
        else:
            curve[i] = equity

    if pos is not None:
        close_trade(n - 1, c[-1], "fin des données")
        curve[-1] = equity

    tr_df = pd.DataFrame(trades)
    curve = pd.Series(curve, index=d["time"]).ffill()
    return tr_df, curve


# ------------------------------------------------------------------------------
# Métriques
# ------------------------------------------------------------------------------
def max_losing_streak(r):
    best = cur = 0
    for x in r:
        cur = cur + 1 if x <= 0 else 0
        best = max(best, cur)
    return best


def compute_metrics(trades, curve, capital, start_time=None):
    m = {}
    if start_time is not None:
        curve = curve[curve.index >= start_time]
    m["trades"] = len(trades)
    if len(curve) < 2:
        return m
    end_eq = curve.iloc[-1]
    base = curve.iloc[0] if start_time is not None else capital
    m["return_pct"] = (end_eq / base - 1) * 100
    years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
    m["cagr_pct"] = ((end_eq / base) ** (1 / years) - 1) * 100 if end_eq > 0 and base > 0 else -100.0
    m["max_dd_pct"] = ((curve / curve.cummax()) - 1).min() * 100
    if len(trades):
        r = trades["R"]
        wins, losses = r[r > 0], r[r <= 0]
        m["win_rate_pct"] = len(wins) / len(r) * 100
        m["avg_win_R"] = wins.mean() if len(wins) else 0.0
        m["avg_loss_R"] = losses.mean() if len(losses) else 0.0
        m["expectancy_R"] = r.mean()
        gp, gl = trades.loc[trades["pnl"] > 0, "pnl"].sum(), -trades.loc[trades["pnl"] <= 0, "pnl"].sum()
        m["profit_factor"] = gp / gl if gl > 0 else float("inf")
        m["max_losing_streak"] = max_losing_streak(r.values)
        m["avg_bars"] = trades["bars"].mean()
    m["years"] = years
    return m


def print_metrics(title, m, bh=None):
    print(f"\n=== {title} ===")
    if m.get("trades", 0) == 0:
        print("Aucun trade.")
        return
    print(f"Période              : {m['years']:.1f} ans")
    print(f"Trades               : {m['trades']}  ({m['trades'] / m['years']:.1f}/an)")
    print(f"Taux de réussite     : {m['win_rate_pct']:.1f} %")
    print(f"Gain moyen / perte   : {m['avg_win_R']:+.2f} R / {m['avg_loss_R']:+.2f} R")
    print(f"Espérance            : {m['expectancy_R']:+.3f} R par trade")
    print(f"Profit factor        : {m['profit_factor']:.2f}")
    print(f"Rendement total      : {m['return_pct']:+.1f} %   (CAGR {m['cagr_pct']:+.1f} %)")
    print(f"Drawdown max         : {m['max_dd_pct']:.1f} %")
    print(f"Plus longue série -  : {m['max_losing_streak']} pertes d'affilée")
    print(f"Durée moyenne trade  : {m['avg_bars']:.1f} bougies")
    if bh is not None:
        print(f"Buy & hold (réf.)    : {bh:+.1f} %")


def buy_hold(df, start_time=None):
    d = df if start_time is None else df[df["time"] >= start_time]
    if len(d) < 2:
        return None
    return (d["close"].iloc[-1] / d["close"].iloc[0] - 1) * 100



# ------------------------------------------------------------------------------
# Partie MetaApi : téléchargement de l'historique + rapport dans les logs
# ------------------------------------------------------------------------------
from metaapi_cloud_sdk import MetaApi  # noqa: E402

TOKEN = os.getenv("METAAPI_TOKEN")
ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")
REGION = os.getenv("METAAPI_REGION", "london")
SYMBOLS = [s.strip() for s in os.getenv("BT_SYMBOLS", "XAUUSD,BTCUSD").split(",") if s.strip()]
TIMEFRAME = os.getenv("BT_TIMEFRAME", "4h")
YEARS = float(os.getenv("BT_YEARS", "8"))

# Spread aller-retour en unités de prix : ADAPTEZ-LES au spread réel de votre broker
SPREAD = {"XAUUSD": 0.30, "BTCUSD": 20.0, "USDJPY": 0.02, "GBPJPY": 0.04}
LONG_ONLY = {"BTCUSD": True}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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
        log(f"[{symbol}] {len(rows)} bougies, la plus ancienne : {oldest:%Y-%m-%d}")
        if oldest <= target or (prev_oldest is not None and oldest >= prev_oldest) or len(rows) > 60000:
            break
        prev_oldest = oldest
        start = oldest - timedelta(seconds=1)
        await asyncio.sleep(0.3)
    if len(rows) < 3:
        return None
    df = pd.DataFrame([{"time": r["time"], "open": r["open"], "high": r["high"],
                        "low": r["low"], "close": r["close"]} for r in rows])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    return df.iloc[:-1].reset_index(drop=True)          # retire la bougie en formation


def make_params(symbol, **over):
    p = argparse.Namespace(n_entry=20, n_exit=10, ema=100, atr=14, atr_mult=2.0, risk=0.5,
                           capital=10000.0, spread=SPREAD.get(symbol, 0.0), slippage=0.0,
                           long_only=LONG_ONLY.get(symbol, False))
    for k, v in over.items():
        setattr(p, k, v)
    return p


def report(symbol, df, oos=0.3):
    p = make_params(symbol)
    print("\n" + "#" * 60, flush=True)
    print(f"# {symbol} {TIMEFRAME} | {len(df)} bougies | {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d}", flush=True)
    print(f"# spread={p.spread} long_only={p.long_only}", flush=True)
    print("#" * 60, flush=True)
    if len(df) < max(p.n_entry, p.ema) + 200:
        print("Historique insuffisant pour conclure (moins de ~300 bougies).", flush=True)
        return
    split = int(len(df) * (1 - oos))
    split_time = df["time"].iloc[split]
    tr_is, cv_is = run_backtest(df.iloc[:split].reset_index(drop=True), p)
    print_metrics("ECHANTILLON (réglages)", compute_metrics(tr_is, cv_is, p.capital), buy_hold(df.iloc[:split]))
    tr_o, cv_o = run_backtest(df, p, start_idx=split)
    print_metrics("HORS-ECHANTILLON", compute_metrics(tr_o, cv_o, p.capital, split_time), buy_hold(df, split_time))

    print("\n=== ROBUSTESSE : espérance R | rendement % | DD max % | nb trades ===", flush=True)
    exits = (5, 10, 20)
    print("entree\\sortie " + "".join(f"{x:>28}" for x in exits), flush=True)
    for ne in (10, 20, 55):
        row = f"{ne:>12} "
        for nx in exits:
            tr, cv = run_backtest(df, make_params(symbol, n_entry=ne, n_exit=nx))
            m = compute_metrics(tr, cv, p.capital)
            cell = (f"{m['expectancy_R']:+.2f}R {m['return_pct']:+.0f}% {m['max_dd_pct']:.0f}% n={m['trades']}"
                    if m.get("trades") else "-")
            row += f"{cell:>28}"
        print(row, flush=True)


async def health_server():
    port = int(os.getenv("PORT", 10000))

    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"Backtest OK"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    async with server:
        await server.serve_forever()


async def main():
    if not TOKEN or not ACCOUNT_ID:
        raise RuntimeError("METAAPI_TOKEN et METAAPI_ACCOUNT_ID sont requis.")
    asyncio.create_task(health_server())
    api = MetaApi(TOKEN, {"region": REGION})
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    if account.state != "DEPLOYED":
        await account.deploy()
    await account.wait_connected()
    log(f"Compte connecté. Symboles : {SYMBOLS} | {TIMEFRAME} | {YEARS} ans")

    for symbol in SYMBOLS:
        try:
            df = await fetch_history(account, symbol, TIMEFRAME, YEARS)
            if df is None:
                print(f"[{symbol}] Aucune bougie reçue (historique indisponible ?).", flush=True)
                continue
            report(symbol, df)
        except Exception as e:
            print(f"[{symbol}] Erreur : {e}", flush=True)

    print("\nTERMINE. Aucun ordre n'a été passé. Vous pouvez arrêter ce service.", flush=True)
    while True:                       # garde le service en vie pour pouvoir lire les logs
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
