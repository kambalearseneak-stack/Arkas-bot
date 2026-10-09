"""ARKAS TREND DONCHIAN — BACKTEST MOIS PAR MOIS — aucun ordre réel.
Teste la stratégie sur CHAQUE mois séparément sur 3 ans.
36 mois = 36 tests indépendants = vraie distribution des performances.
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
YEARS = float(os.getenv("BT_YEARS", "3.0"))
RISK_PCT = float(os.getenv("BT_RISK", "1.0"))
TIMEFRAME = os.getenv("BT_TF", "1h")
INITIAL_CAPITAL = float(os.getenv("BT_CAPITAL", "10000"))

SPREAD_OVERRIDES = {
    "XAUUSD": 0.30,
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


async def fetch_history(account, symbol, timeframe, years):
    target = datetime.now(timezone.utc) - timedelta(days=365.25 * years)
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
        if len(rows) % 5000 < 1000:
            log(f"[{symbol}] {len(rows)} bougies; plus ancienne : {oldest:%Y-%m-%d}")
        if oldest <= target or (prev_oldest is not None and oldest >= prev_oldest) or len(rows) > 40000:
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
    """Version avec paramètres réduits si historique court (pour les mois)."""
    d = df.reset_index(drop=True).copy()

    # Réduction adaptative pour tenir dans 1 mois
    ep = min(p.entry_period, max(5, len(d) // 30))
    xp = min(p.exit_period, max(3, len(d) // 60))
    ef = min(p.ema_filter, max(50, len(d) // 4))
    ap = min(p.atr_period, max(5, len(d) // 20))

    if len(d) < max(50, ep + 10):
        return pd.DataFrame(), pd.Series(dtype=float), {}

    o, h, l, c = (d[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    n = len(d)
    prev_close = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])
    atr = pd.Series(tr).rolling(ap, min_periods=ap).mean().to_numpy()
    atr_avg = pd.Series(atr).rolling(min(50, n // 2), min_periods=5).mean().to_numpy()
    ema_long = pd.Series(c).ewm(span=ef, adjust=False).mean().to_numpy()

    dc_hi_entry = pd.Series(h).rolling(ep).max().shift(1).to_numpy()
    dc_lo_entry = pd.Series(l).rolling(ep).min().shift(1).to_numpy()
    dc_hi_exit = pd.Series(h).rolling(xp).max().shift(1).to_numpy()
    dc_lo_exit = pd.Series(l).rolling(xp).min().shift(1).to_numpy()

    times = d["time"].to_numpy()
    weekdays = d["time"].dt.weekday.to_numpy()

    spread = get_spread(symbol)
    half_spread = spread / 2.0

    equity = float(p.capital)
    positions, trades = [], []
    info = {"signals_long": 0, "signals_short": 0}

    def close_position(pos, i, raw_fill, reason):
        nonlocal equity
        if pos not in positions:
            return
        positions.remove(pos)
        dr = pos["dir"]
        fill_raw = float(raw_fill)
        if reason.startswith("trail"):
            if dr == 1:
                fill_raw = min(fill_raw, float(o[i]))
            else:
                fill_raw = max(fill_raw, float(o[i]))
        fill = fill_raw - dr * half_spread
        pnl = (fill - pos["entry"]) * dr * pos["size"]
        equity += pnl
        R = pnl / pos["risk_money"] if pos["risk_money"] else 0.0
        trades.append({
            "entry_time": pd.Timestamp(pos["entry_time"]),
            "exit_time": pd.Timestamp(times[i]),
            "dir": "LONG" if dr == 1 else "SHORT",
            "entry": pos["entry"], "exit": fill,
            "pnl": pnl, "R": R,
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
                else:
                    new_stop = dc_hi_exit[i]
                    if np.isfinite(new_stop) and new_stop < pos["stop"]:
                        pos["stop"] = new_stop

            if dr == 1:
                if o[i] <= pos["stop"]:
                    close_position(pos, i, min(pos["stop"], o[i]), "trail(gap)")
                elif l[i] <= pos["stop"]:
                    close_position(pos, i, pos["stop"], "trail")
            else:
                if o[i] >= pos["stop"]:
                    close_position(pos, i, max(pos["stop"], o[i]), "trail(gap)")
                elif h[i] >= pos["stop"]:
                    close_position(pos, i, pos["stop"], "trail")

            if pos in positions:
                if i - pos["entry_i"] + 1 >= p.max_hold:
                    close_position(pos, i, c[i], "temps")

        in_session = weekdays[i] < 5
        can_trade = (in_session and i >= ep + 5 and i > 0
                     and np.isfinite(atr[i]) and np.isfinite(dc_hi_entry[i])
                     and np.isfinite(dc_lo_entry[i])
                     and len(positions) < p.max_positions)
        if can_trade:
            A = atr[i]
            atr_ok = (not np.isfinite(atr_avg[i])) or (A >= p.min_atr_ratio * atr_avg[i])
            if atr_ok:
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
                    entry = c[i] + dr * half_spread
                    dist = abs(entry - sl_px)
                    if dist <= 0:
                        continue
                    if spread > p.max_spread_ratio * dist:
                        continue
                    risk_money = max(equity, 0.0) * p.risk / 100.0
                    if risk_money <= 0:
                        continue
                    positions.append({
                        "dir": dr, "entry": entry,
                        "stop": sl_px, "stop_initial": sl_px,
                        "dist": dist, "size": risk_money / dist,
                        "risk_money": risk_money,
                        "entry_time": times[i], "entry_i": i,
                    })
                    if dr == 1:
                        info["signals_long"] += 1
                    else:
                        info["signals_short"] += 1
                    break

    while positions:
        close_position(positions[0], n - 1, c[-1], "fin")
    return pd.DataFrame(trades), pd.Series(equity, index=[0]), info


def monthly_report(symbol, df):
    p = make_params()
    print("\n" + "#" * 70)
    print(f"# BACKTEST MOIS PAR MOIS | {symbol} {TIMEFRAME} | {len(df)} bougies")
    print(f"# {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d}")
    print(f"# DC20 / Exit10 / EMA200 / max_hold=48h")
    print(f"# ⚠️ Ne trade qu'un mois isolé à la fois (pas de continuité de capital)")
    print("#" * 70)

    df = df.copy()
    df["year"] = df["time"].dt.year
    df["month"] = df["time"].dt.month

    months = sorted(df.groupby(["year", "month"]).groups.keys())
    results = []

    print(f"\n{'Mois':>8} | {'Trades':>6} | {'Win%':>5} | {'Exp R':>7} | "
          f"{'Ret%':>7} | {'PF':>5} | {'DD%':>6} | {'Durée':>7}")
    print("-" * 80)

    for (year, month) in months:
        sub = df[(df["year"] == year) & (df["month"] == month)].reset_index(drop=True)
        if len(sub) < 50:
            continue
        tr, _, info = run_donchian(sub, p, symbol=symbol)
        if not len(tr):
            print(f"{year}-{month:02d} | {0:>6} |     - |       - |       - |     - |      - |      -")
            results.append({"year": year, "month": month, "n": 0, "exp": 0.0, "ret": 0.0,
                            "pf": 0, "dd": 0, "win": 0, "bars": 0})
            continue

        # Calcul du rendement du mois (sur capital fixe 10000, pas compounding)
        total_pnl = tr["pnl"].sum()
        ret_pct = total_pnl / p.capital * 100.0
        # Equity du mois pour DD
        eq = p.capital + tr["pnl"].cumsum()
        dd = ((eq / eq.cummax()) - 1.0).min() * 100.0
        win = (tr["R"] > 0).mean() * 100.0
        exp = tr["R"].mean()
        gp = tr.loc[tr["R"] > 0, "R"].sum()
        gl = -tr.loc[tr["R"] <= 0, "R"].sum()
        pf = gp / gl if gl > 0 else float("inf")
        bars = tr["bars"].mean()

        print(f"{year}-{month:02d} | {len(tr):>6} | {win:>4.1f}% | {exp:>+7.3f} | "
              f"{ret_pct:>+7.2f} | {pf:>5.2f} | {dd:>6.2f} | {bars:>6.1f}h")

        results.append({"year": year, "month": month, "n": len(tr), "exp": exp,
                        "ret": ret_pct, "pf": pf, "dd": dd, "win": win, "bars": bars})

    # ============================================================
    # SYNTHESE
    # ============================================================
    print("\n" + "=" * 70)
    print("SYNTHESE")
    print("=" * 70)

    r_df = pd.DataFrame(results)
    active_months = r_df[r_df["n"] > 0]

    print(f"Mois testés         : {len(r_df)}")
    print(f"Mois avec trades    : {len(active_months)}")
    print(f"Total trades        : {active_months['n'].sum()}")

    if len(active_months) > 0:
        win_months = (active_months["ret"] > 0).sum()
        lose_months = (active_months["ret"] < 0).sum()
        flat_months = (active_months["ret"] == 0).sum()

        print(f"\nMois gagnants       : {win_months} ({win_months/len(active_months)*100:.1f}%)")
        print(f"Mois perdants       : {lose_months} ({lose_months/len(active_months)*100:.1f}%)")
        print(f"Mois plats          : {flat_months}")

        print(f"\nMeilleur mois       : {active_months['ret'].max():+.2f} %")
        print(f"Pire mois           : {active_months['ret'].min():+.2f} %")
        print(f"Mois médian         : {active_months['ret'].median():+.2f} %")
        print(f"Mois moyen          : {active_months['ret'].mean():+.2f} %")

        print(f"\nEspérance moyenne   : {active_months['exp'].mean():+.3f} R")
        print(f"Espérance médiane   : {active_months['exp'].median():+.3f} R")
        print(f"Trades par mois moy : {active_months['n'].mean():.1f}")

        print(f"\n--- DISTRIBUTION DES RENDEMENTS MENSUELS ---")
        bins = [-100, -5, -3, -1, 0, 1, 3, 5, 100]
        labels = ["< -5%", "-5/-3", "-3/-1", "-1/0", "0/+1", "+1/+3", "+3/+5", "> +5%"]
        for i in range(len(bins) - 1):
            count = ((active_months["ret"] >= bins[i]) &
                     (active_months["ret"] < bins[i+1])).sum()
            bar = "█" * count
            print(f"  {labels[i]:>8} : {count:>3} {bar}")

        # Conclusion
        print(f"\n--- INTERPRETATION ---")
        if win_months / len(active_months) > 0.5:
            print(f"✅ {win_months/len(active_months)*100:.0f}% de mois gagnants → edge stable")
        elif win_months / len(active_months) > 0.4:
            print(f"⚠️ {win_months/len(active_months)*100:.0f}% de mois gagnants → acceptable pour trend following")
        else:
            print(f"❌ {win_months/len(active_months)*100:.0f}% de mois gagnants → edge fragile")

        if active_months["ret"].mean() > 0:
            print(f"✅ Rendement mensuel moyen positif (+{active_months['ret'].mean():.2f}%)")
        else:
            print(f"❌ Rendement mensuel moyen négatif ({active_months['ret'].mean():.2f}%)")

        # Ratio de Sharpe simplifié
        if active_months["ret"].std() > 0:
            monthly_sharpe = active_months["ret"].mean() / active_months["ret"].std()
            annual_sharpe = monthly_sharpe * np.sqrt(12)
            print(f"\nSharpe mensuel      : {monthly_sharpe:.2f}")
            print(f"Sharpe annualisé    : {annual_sharpe:.2f}")
            if annual_sharpe > 1.0:
                print("✅ Sharpe > 1.0 → stratégie exploitable")
            elif annual_sharpe > 0.5:
                print("⚠️ Sharpe 0.5-1.0 → acceptable avec prudence")
            else:
                print("❌ Sharpe < 0.5 → risque élevé pour le rendement")


async def health_server():
    port = int(os.getenv("PORT", "10000"))
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"ARKAS monthly backtest OK - no live orders"
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
    log(f"Compte connecté. {SYMBOL} {TIMEFRAME} | {YEARS} an(s)")
    try:
        df = await fetch_history(account, SYMBOL, TIMEFRAME, YEARS)
        if df is None or len(df) == 0:
            print(f"[{SYMBOL}] Aucune bougie reçue.", flush=True)
        else:
            monthly_report(SYMBOL, df)
    except Exception as exc:
        log(f"Erreur: {type(exc).__name__}: {exc}")
    finally:
        keepalive_task.cancel()
        print("\nTERMINÉ. Aucun ordre n'a été passé.", flush=True)
        await server_task


if __name__ == "__main__":
    asyncio.run(main())
