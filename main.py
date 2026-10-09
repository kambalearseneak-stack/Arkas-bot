"""ARKAS TREND DONCHIAN — BACKTEST 1 MOIS — aucun ordre réel.
Trend following Donchian sur XAUUSD H1 sur les 30 DERNIERS JOURS uniquement.
⚠️ ATTENTION : sur 1 mois, le nombre de trades est trop faible pour conclure.
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
MONTHS = float(os.getenv("BT_MONTHS", "1"))
RISK_PCT = float(os.getenv("BT_RISK", "1.0"))
TIMEFRAME = os.getenv("BT_TF", "1h")
INITIAL_CAPITAL = float(os.getenv("BT_CAPITAL", "10000"))

SPREAD_OVERRIDES = {
    "XAUUSD": 0.30,
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


async def fetch_history_months(account, symbol, timeframe, months):
    """Récupère les bougies sur N mois glissants uniquement."""
    target = datetime.now(timezone.utc) - timedelta(days=30.44 * months)
    rows, start, prev_oldest = [], None, None
    while True:
        try:
            batch = await account.get_historical_candles(
                symbol=symbol, timeframe=timeframe, start_time=start, limit=1000
            )
        except Exception as e:
            log(f"[{symbol}] fetch error: {type(e).__name__}: {e}")
            return None
        if not batch:
            break
        batch = sorted(batch, key=lambda r: r["time"])
        rows = batch + rows
        oldest = batch[0]["time"]
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        log(f"[{symbol}] {len(rows)} bougies; plus ancienne : {oldest:%Y-%m-%d}")
        if oldest <= target or (prev_oldest is not None and oldest >= prev_oldest):
            break
        prev_oldest = oldest
        start = oldest - timedelta(seconds=1)
        await asyncio.sleep(0.2)

    if len(rows) < 3:
        return None
    df = pd.DataFrame([
        {"time": r["time"], "open": r["open"], "high": r["high"],
         "low": r["low"], "close": r["close"]}
        for r in rows
    ])
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = (df.dropna(subset=["time", "open", "high", "low", "close"])
            .sort_values("time").drop_duplicates("time").reset_index(drop=True))
    # Ne garder que les N derniers mois (filtre final)
    if len(df):
        cutoff = df["time"].max() - timedelta(days=30.44 * months)
        df = df[df["time"] >= cutoff].reset_index(drop=True)
    return df.iloc[:-1].reset_index(drop=True)


def get_spread(symbol):
    return SPREAD_OVERRIDES.get(symbol.upper(), 0.30)


def make_params(**over):
    p = argparse.Namespace(
        slippage=0.0, risk=RISK_PCT,
        entry_period=20,
        exit_period=10,
        ema_filter=200,
        use_ema_filter=True,
        min_atr_ratio=0.4,
        atr_period=20,
        max_hold=48,
        max_positions=2,
        max_total_risk_pct=2.0,
        max_spread_ratio=0.05,
        capital=INITIAL_CAPITAL,
        long_only=False, short_only=False,
        gap_slip_sl=0.3, gap_slip_tp=0.3,
        use_trailing=True,
    )
    for k, v in over.items():
        setattr(p, k, v)
    return p


def run_donchian(df, p, symbol="SYM"):
    d = df.reset_index(drop=True).copy()
    # 🔴 ATTENTION : sur 1 mois, l'EMA 200 + DC 20 ne sera pas calculable
    # On réduit automatiquement les paramètres si trop de bougies manquantes
    min_required = p.entry_period + p.ema_filter + 10
    if len(d) < min_required:
        # Réduction d'urgence : EMA à 50, DC à 5
        p.entry_period = min(p.entry_period, 5)
        p.exit_period = min(p.exit_period, 3)
        p.ema_filter = min(p.ema_filter, 50)
        p.atr_period = min(p.atr_period, 10)
        log(f"[{symbol}] Historique court ({len(d)} bougies) → paramètres réduits : "
            f"DC={p.entry_period}, Exit={p.exit_period}, EMA={p.ema_filter}")

    if len(d) < max(50, p.entry_period + p.ema_filter + 5):
        log(f"[{symbol}] Pas assez de bougies pour trader ({len(d)})")
        return pd.DataFrame(), pd.Series(dtype=float), {}, pd.DataFrame()

    o, h, l, c = (d[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    n = len(d)
    prev_close = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])
    atr = pd.Series(tr).rolling(p.atr_period, min_periods=p.atr_period).mean().to_numpy()
    atr_avg = pd.Series(atr).rolling(min(50, n//2), min_periods=5).mean().to_numpy()
    ema_long = pd.Series(c).ewm(span=p.ema_filter, adjust=False).mean().to_numpy()

    dc_hi_entry = pd.Series(h).rolling(p.entry_period).max().shift(1).to_numpy()
    dc_lo_entry = pd.Series(l).rolling(p.entry_period).min().shift(1).to_numpy()
    dc_hi_exit = pd.Series(h).rolling(p.exit_period).max().shift(1).to_numpy()
    dc_lo_exit = pd.Series(l).rolling(p.exit_period).min().shift(1).to_numpy()

    times = d["time"].to_numpy()
    weekdays = d["time"].dt.weekday.to_numpy()
    day_values = d["time"].dt.normalize().to_numpy()

    spread = get_spread(symbol)
    half_spread = spread / 2.0
    slip = max(float(p.slippage), 0.0)

    equity = float(p.capital)
    positions, trades = [], []
    curve = np.full(n, np.nan)

    info = {
        "signals_long": 0, "signals_short": 0,
        "rejected_ema": 0, "rejected_atr": 0,
        "rejected_spread": 0, "rejected_max_pos": 0, "rejected_max_risk": 0,
        "trail_moves": 0, "exit_by_trail": 0, "exit_by_timeout": 0,
    }

    def open_risk_pct():
        return sum(pos["risk_money"] for pos in positions) / p.capital * 100.0

    def close_position(pos, i, raw_fill, reason, gap_kind=None):
        nonlocal equity, info
        if pos not in positions:
            return
        positions.remove(pos)
        dr = pos["dir"]
        fill_raw = float(raw_fill)
        if gap_kind == "sl":
            if dr == 1:
                fill_raw = min(fill_raw, float(o[i]))
                fill_raw -= p.gap_slip_sl * pos["dist"]
            else:
                fill_raw = max(fill_raw, float(o[i]))
                fill_raw += p.gap_slip_sl * pos["dist"]
        fill = fill_raw - dr * (half_spread + slip)
        pnl = (fill - pos["entry"]) * dr * pos["size"]
        equity += pnl
        R = pnl / pos["risk_money"] if pos["risk_money"] else 0.0
        trades.append({
            "symbol": symbol,
            "entry_time": pd.Timestamp(pos["entry_time"]),
            "exit_time": pd.Timestamp(times[i]),
            "dir": "LONG" if dr == 1 else "SHORT",
            "entry": pos["entry"], "exit": fill,
            "pnl": pnl, "R": R,
            "cost_R": (spread + 2 * slip) / pos["dist"] if pos["dist"] else np.nan,
            "bars": i - pos["entry_i"] + 1, "reason": reason,
        })

    for i in range(n):
        for pos in list(positions):
            if i <= pos["entry_i"]:
                continue
            dr = pos["dir"]
            if p.use_trailing:
                if dr == 1:
                    new_stop = dc_lo_exit[i]
                    if np.isfinite(new_stop) and new_stop > pos["stop"]:
                        pos["stop"] = new_stop
                        info["trail_moves"] += 1
                else:
                    new_stop = dc_hi_exit[i]
                    if np.isfinite(new_stop) and new_stop < pos["stop"]:
                        pos["stop"] = new_stop
                        info["trail_moves"] += 1

            if dr == 1:
                if o[i] <= pos["stop"]:
                    close_position(pos, i, min(pos["stop"], o[i]), "trail (gap)", "sl")
                    info["exit_by_trail"] += 1
                elif l[i] <= pos["stop"]:
                    close_position(pos, i, pos["stop"], "trail")
                    info["exit_by_trail"] += 1
            else:
                if o[i] >= pos["stop"]:
                    close_position(pos, i, max(pos["stop"], o[i]), "trail (gap)", "sl")
                    info["exit_by_trail"] += 1
                elif h[i] >= pos["stop"]:
                    close_position(pos, i, pos["stop"], "trail")
                    info["exit_by_trail"] += 1

            if pos in positions:
                if i - pos["entry_i"] + 1 >= p.max_hold:
                    close_position(pos, i, c[i], "temps")
                    info["exit_by_timeout"] += 1

        in_session = weekdays[i] < 5
        can_trade = (in_session and i >= p.entry_period + 5
                     and i > 0 and np.isfinite(atr[i]) and np.isfinite(dc_hi_entry[i])
                     and np.isfinite(dc_lo_entry[i])
                     and len(positions) < p.max_positions)
        if can_trade:
            A = atr[i]
            atr_ok = (not np.isfinite(atr_avg[i])) or (A >= p.min_atr_ratio * atr_avg[i])
            if not atr_ok:
                info["rejected_atr"] += 1
            else:
                ema_ok_long = (not p.use_ema_filter) or (c[i] > ema_long[i])
                ema_ok_short = (not p.use_ema_filter) or (c[i] < ema_long[i])
                bull_break = c[i] > dc_hi_entry[i]
                bear_break = c[i] < dc_lo_entry[i]

                candidates = []
                if bull_break and ema_ok_long and not p.short_only:
                    candidates.append((1, dc_hi_entry[i]))
                if bear_break and ema_ok_short and not p.long_only:
                    candidates.append((-1, dc_lo_entry[i]))

                for dr, trigger_px in candidates:
                    sl_px = dc_lo_exit[i] if dr == 1 else dc_hi_exit[i]
                    if not np.isfinite(sl_px):
                        continue
                    entry = c[i] + dr * (half_spread + slip)
                    dist = abs(entry - sl_px)
                    if dist <= 0:
                        continue
                    if spread > p.max_spread_ratio * dist:
                        info["rejected_spread"] += 1
                        continue
                    risk_money = max(equity, 0.0) * p.risk / 100.0
                    if risk_money <= 0:
                        continue
                    positions.append({
                        "dir": dr, "entry": entry,
                        "stop": sl_px, "stop_initial": sl_px,
                        "dist": dist,
                        "size": risk_money / dist,
                        "risk_money": risk_money,
                        "entry_time": times[i], "entry_i": i,
                    })
                    if dr == 1:
                        info["signals_long"] += 1
                    else:
                        info["signals_short"] += 1
                    break

        open_pnl = sum((c[i] - pos["entry"]) * pos["dir"] * pos["size"] for pos in positions)
        curve[i] = equity + open_pnl

    while positions:
        close_position(positions[0], n - 1, c[-1], "fin des données")
    curve[-1] = equity
    trade_df = pd.DataFrame(trades)
    curve_series = pd.Series(curve, index=pd.to_datetime(d["time"])).ffill()
    return trade_df, curve_series, info, pd.DataFrame()


def metrics(trades, capital):
    out = {"n": len(trades)}
    if not len(trades):
        return out
    t = trades.copy()
    t["exit_time"] = pd.to_datetime(t["exit_time"])
    t = t.sort_values("exit_time")
    t["equity"] = capital + t["pnl"].cumsum()
    t["peak"] = t["equity"].cummax()
    t["dd"] = (t["equity"] / t["peak"] - 1.0) * 100.0
    out["ret"] = (t["equity"].iloc[-1] / capital - 1.0) * 100.0
    out["dd"] = t["dd"].min()
    out["days"] = max((t["exit_time"].iloc[-1] - t["exit_time"].iloc[0]).days, 1)
    r = t["R"].replace([np.inf, -np.inf], np.nan).dropna()
    gp = r[r > 0].sum()
    gl = -r[r <= 0].sum()
    out["win"] = (r > 0).mean() * 100.0
    out["exp"] = r.mean()
    out["exp_gross"] = (r + t.loc[r.index, "cost_R"]).mean()
    out["cost"] = t["cost_R"].mean()
    out["pf"] = gp / gl if gl > 0 else float("inf")
    out["bars"] = t["bars"].mean()
    out["reasons"] = t["reason"].value_counts().to_dict()
    return out


def report(symbol, df):
    p = make_params()
    print("\n" + "#" * 64)
    print(f"# ARKAS DONCHIAN — BACKTEST {MONTHS} MOIS | {symbol} {TIMEFRAME}")
    print(f"# {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d} | {len(df)} bougies")
    print(f"# DC{p.entry_period} | Exit{p.exit_period} | EMA{p.ema_filter} | "
          f"max_hold={p.max_hold}h | risque={p.risk}%")
    print(f"# ⚠️ ATTENTION : sur {MONTHS} mois, le nombre de trades est trop faible")
    print(f"#    pour conclure quoi que ce soit. Ce test ne vaut que comme")
    print(f"#    vérification de fonctionnement, pas comme validation.")
    print("#" * 64)

    if len(df) < 50:
        print(f"Historique très insuffisant ({len(df)} bougies).")
        return

    tr, cv, inf, dl = run_donchian(df, p, symbol=symbol)
    m = metrics(tr, p.capital)

    print(f"\n=== RESULTATS ({MONTHS} MOIS) ===")
    if not m.get("n"):
        print("Aucun trade généré sur cette période.")
        print(f"  rejets ATR   : {inf.get('rejected_atr', 0)}")
        print(f"  rejets spread: {inf.get('rejected_spread', 0)}")
        print("\n→ C'est NORMAL : sur 1 mois, il n'y a pas toujours de signal Donchian.")
        print("→ Ce résultat ne remet PAS en cause la stratégie (validée sur 3 ans).")
        return

    print(f"Jours             : {m.get('days', 0)}")
    print(f"Trades            : {m['n']}")
    print(f"Longs/shorts      : {inf.get('signals_long', 0)}/{inf.get('signals_short', 0)}")
    print(f"Réussite          : {m.get('win', float('nan')):.1f} %")
    print(f"Espérance nette   : {m.get('exp', float('nan')):+.3f} R/trade")
    print(f"Coût moyen        : {m.get('cost', float('nan')):.3f} R/trade")
    print(f"Profit factor     : {m.get('pf', float('nan')):.2f}")
    print(f"Rendement         : {m.get('ret', float('nan')):+.1f} %")
    print(f"Drawdown max      : {m.get('dd', float('nan')):.1f} %")
    print(f"Durée moyenne     : {m.get('bars', 0):.1f} heures")
    print(f"Sorties           : {m.get('reasons', {})}")

    print(f"\n--- DÉTAIL DES TRADES ---")
    for _, trade in tr.iterrows():
        print(f"  {trade['entry_time']:%Y-%m-%d %H:%M} {trade['dir']:5s} "
              f"entry={trade['entry']:.2f} exit={trade['exit']:.2f} "
              f"R={trade['R']:+.2f} ({trade['reason']})")

    print(f"\n⚠️ RAPPEL : sur {m['n']} trades et {m.get('days', 0)} jours, "
          f"les statistiques ne sont PAS fiables.")
    print(f"→ Pour une évaluation valable, il faut au minimum 30 trades et 6 mois.")
    print(f"→ La stratégie a été validée sur 3 ans (voir V14-MTF).")


async def health_server():
    port = int(os.getenv("PORT", "10000"))
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"ARKAS 1-month backtest OK - no live orders"
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


async def main():
    if not TOKEN or not ACCOUNT_ID:
        raise RuntimeError("METAAPI_TOKEN et METAAPI_ACCOUNT_ID requis.")
    server_task = asyncio.create_task(health_server())
    keepalive_task = asyncio.create_task(keepalive())
    api = MetaApi(TOKEN, {"region": REGION})
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    if account.state != "DEPLOYED":
        await account.deploy()
    await account.wait_connected()
    log(f"Compte connecté. {SYMBOL} {TIMEFRAME} | {MONTHS} mois")

    try:
        df = await fetch_history_months(account, SYMBOL, TIMEFRAME, MONTHS)
        if df is None or len(df) == 0:
            print(f"[{SYMBOL}] Aucune bougie reçue.", flush=True)
        else:
            report(SYMBOL, df)
    except Exception as exc:
        log(f"Erreur backtest: {type(exc).__name__}: {exc}")
    finally:
        keepalive_task.cancel()
        print("\nTERMINÉ. Aucun ordre n'a été passé.", flush=True)
        await server_task


if __name__ == "__main__":
    asyncio.run(main())
