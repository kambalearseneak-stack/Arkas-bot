"""ARKAS TREND DONCHIAN V14 — backtest only. AUCUN ordre réel.
Trend following par cassure de canal Donchian sur Daily.
Famille Turtle Traders — edge documenté depuis 1980.
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
SYMBOLS = [s.strip() for s in os.getenv("BT_SYMBOLS", "XAUUSD").split(",") if s.strip()]
YEARS = float(os.getenv("BT_YEARS", "3.0"))
RISK_PCT = float(os.getenv("BT_RISK", "1.0"))
TIMEFRAME = "1d"
INITIAL_CAPITAL = float(os.getenv("BT_CAPITAL", "10000"))
MAX_TRADES_PER_DAY = 5

SPREAD_OVERRIDES = {
    "XAUUSD": 0.30,
    "US500":  0.50,
    "USTEC":  1.0,
    "NAS100": 1.0,
    "EURUSD": 0.0001,
    "GBPUSD": 0.00015,
    "USDJPY": 0.01,
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
        if len(rows) % 500 < 100:
            log(f"[{symbol}] {len(rows)} bougies D1; plus ancienne : {oldest:%Y-%m-%d}")
        if oldest <= target or (prev_oldest is not None and oldest >= prev_oldest) or len(rows) > 5000:
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
    return SPREAD_OVERRIDES.get(symbol.upper(), 0.0002)


def make_params(**over):
    p = argparse.Namespace(
        slippage=0.0, risk=RISK_PCT,
        entry_period=20,
        exit_period=10,
        ema_filter=200,
        use_ema_filter=True,
        min_atr_ratio=0.5,
        atr_period=20,
        max_hold=200,
        max_positions=3,
        max_total_risk_pct=3.0,
        max_spread_ratio=0.05,
        capital=INITIAL_CAPITAL,
        long_only=False, short_only=False,
        exclude_days=None,
        gap_slip_sl=0.3, gap_slip_tp=0.3,
        use_trailing=True,
    )
    for k, v in over.items():
        setattr(p, k, v)
    return p


def run_donchian(df, p, symbol="SYM", global_state=None, start_idx=0):
    d = df.reset_index(drop=True).copy()
    if len(d) < max(250, p.entry_period + p.ema_filter + 10):
        return pd.DataFrame(), pd.Series(dtype=float), {}, pd.DataFrame()

    o, h, l, c = (d[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    n = len(d)
    prev_close = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])
    atr = pd.Series(tr).rolling(p.atr_period, min_periods=p.atr_period).mean().to_numpy()
    atr_avg = pd.Series(atr).rolling(100, min_periods=20).mean().to_numpy()
    ema_long = pd.Series(c).ewm(span=p.ema_filter, adjust=False).mean().to_numpy()

    # Canaux Donchian — décalés de 1 bougie
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
    daily_rows = []

    excluded = set()
    if p.exclude_days:
        excluded = {np.datetime64(pd.Timestamp(x).normalize(), "D") for x in p.exclude_days}

    if global_state is None:
        global_state = {"positions_total": 0, "trades_today": {}}

    cur_day = None
    day_open_equity = equity

    info = {
        "signals_long": 0, "signals_short": 0,
        "rejected_no_break": 0, "rejected_ema": 0, "rejected_atr": 0,
        "rejected_spread": 0, "rejected_max_pos": 0, "rejected_max_risk": 0,
        "trail_moves": 0, "exit_by_trail": 0, "exit_by_timeout": 0,
        "fills_stop_gap": 0,
    }

    def open_risk_pct():
        return sum(pos["risk_money"] for pos in positions) / p.capital * 100.0

    def close_position(pos, i, raw_fill, reason, gap_kind=None):
        nonlocal equity, info
        if pos not in positions:
            return
        positions.remove(pos)
        global_state["positions_total"] = max(0, global_state["positions_total"] - 1)
        dr = pos["dir"]
        fill_raw = float(raw_fill)
        if gap_kind == "sl":
            if dr == 1:
                fill_raw = min(fill_raw, float(o[i]))
                fill_raw -= p.gap_slip_sl * pos["dist"]
            else:
                fill_raw = max(fill_raw, float(o[i]))
                fill_raw += p.gap_slip_sl * pos["dist"]
            info["fills_stop_gap"] += 1
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
            "stop_initial": pos["stop_initial"], "stop_final": pos["stop"],
            "pnl": pnl, "R": R,
            "cost_R": (spread + 2 * slip) / pos["dist"] if pos["dist"] else np.nan,
            "bars": i - pos["entry_i"] + 1, "reason": reason,
            "is_gap": gap_kind is not None,
        })

    for i in range(n):
        day_key = np.datetime64(pd.Timestamp(day_values[i]).normalize(), "D")
        if day_values[i] != cur_day:
            if cur_day is not None:
                daily_rows.append({
                    "date": pd.Timestamp(cur_day),
                    "ret_pct": (equity - day_open_equity) / p.capital * 100.0,
                })
            cur_day = day_values[i]
            day_open_equity = equity

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
        can_trade = (in_session and i >= max(start_idx, p.entry_period + p.ema_filter + 5)
                     and i > 0 and np.isfinite(atr[i]) and np.isfinite(atr_avg[i])
                     and np.isfinite(dc_hi_entry[i]) and np.isfinite(dc_lo_entry[i])
                     and len(positions) < p.max_positions)
        if can_trade:
            A = atr[i]
            atr_ok = A >= p.min_atr_ratio * atr_avg[i]
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
                    if dr == 1:
                        sl_px = dc_lo_exit[i]
                    else:
                        sl_px = dc_hi_exit[i]
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
                    if open_risk_pct() + risk_money / p.capital * 100.0 > p.max_total_risk_pct:
                        info["rejected_max_risk"] += 1
                        continue
                    if global_state["positions_total"] >= p.max_positions:
                        info["rejected_max_pos"] += 1
                        continue
                    positions.append({
                        "dir": dr, "entry": entry,
                        "stop": sl_px, "stop_initial": sl_px,
                        "dist": dist,
                        "size": risk_money / dist,
                        "risk_money": risk_money,
                        "entry_time": times[i], "entry_i": i,
                    })
                    global_state["positions_total"] += 1
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
    if cur_day is not None:
        daily_rows.append({
            "date": pd.Timestamp(cur_day),
            "ret_pct": (equity - day_open_equity) / p.capital * 100.0,
        })
    trade_df = pd.DataFrame(trades)
    curve_series = pd.Series(curve, index=pd.to_datetime(d["time"])).ffill()
    return trade_df, curve_series, info, pd.DataFrame(daily_rows)


def merge_results(results):
    all_trades = []
    all_info = {}
    for sym, tr, cv, inf, dl in results:
        if tr is not None and len(tr):
            all_trades.append(tr)
        for k, v in inf.items():
            all_info[k] = all_info.get(k, 0) + v
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    return trades, all_info


def metrics_multi(trades, capital):
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
    streak = best = 0
    for value in r.to_numpy():
        streak = streak + 1 if value <= 0 else 0
        best = max(best, streak)
    out["streak"] = best
    out["bars"] = t["bars"].mean()
    out["reasons"] = t["reason"].value_counts().to_dict()
    return out


def show_multi(title, m, info, trades):
    print(f"\n=== {title} ===")
    if not m.get("n"):
        print("Aucun trade.")
        print(f"  signaux rejetés (ATR) : {info.get('rejected_atr', 0)}")
        return
    print(f"Jours             : {m.get('days', 0)}")
    print(f"Trades            : {m['n']} ({m['n'] / max(m.get('days', 1), 1) * 365:.1f}/an)")
    print(f"Longs/shorts      : {info.get('signals_long', 0)}/{info.get('signals_short', 0)}")
    print(f"  trailing moves  : {info.get('trail_moves', 0)}")
    print(f"  sorties trail   : {info.get('exit_by_trail', 0)}")
    print(f"  sorties temps   : {info.get('exit_by_timeout', 0)}")
    print(f"  rejets ATR      : {info.get('rejected_atr', 0)}")
    print(f"  rejets spread   : {info.get('rejected_spread', 0)}")
    print(f"Réussite          : {m.get('win', float('nan')):.1f} %")
    print(f"Espérance nette   : {m.get('exp', float('nan')):+.3f} R/trade")
    print(f"Profit factor     : {m.get('pf', float('nan')):.2f}")
    print(f"Rendement         : {m.get('ret', float('nan')):+.1f} %")
    print(f"Drawdown max      : {m.get('dd', float('nan')):.1f} %")
    print(f"Pires pertes suite: {m.get('streak', 0)}")
    print(f"Durée moyenne     : {m.get('bars', 0):.1f} bougies D1")
    print(f"Sorties           : {m.get('reasons', {})}")
    if len(trades):
        print("--- PAR SYMBOLE ---")
        for sym, g in trades.groupby("symbol"):
            r = g["R"]
            print(f"  {sym:8s}: n={len(g):3d} | win={((r>0).mean()*100):5.1f}% | "
                  f"exp={r.mean():+.3f}R | pnl={g['pnl'].sum():+.2f}")


def run_single_symbol(dfs, symbols, params_override, label, p_base):
    """Run un scénario avec des paramètres spécifiques et retourne (trades, metrics)."""
    gs = {"positions_total": 0, "trades_today": {}}
    results = []
    for s in symbols:
        df = dfs.get(s)
        if df is None or not len(df):
            continue
        p = make_params(**params_override) if params_override else p_base
        tr, cv, inf, dl = run_donchian(df, p, symbol=s, global_state=gs)
        results.append((s, tr, cv, inf, dl))
    t, _ = merge_results(results)
    m = metrics_multi(t, p_base.capital)
    return t, m


def report(symbols, dfs, oos=0.30):
    p = make_params()
    print("\n" + "#" * 64)
    print(f"# ARKAS TREND DONCHIAN V14 | symboles : {symbols} | {TIMEFRAME}")
    print(f"# Entry DC {p.entry_period} | Exit DC {p.exit_period} | "
          f"EMA {p.ema_filter} | ATR min {p.min_atr_ratio}x | risque {p.risk}%")
    print(f"# AUCUN ordre réel. Simulation OHLC D1.")
    print("#" * 64)

    # ---- Run principal ----
    global_state = {"positions_total": 0, "trades_today": {}}
    results = []
    for s in symbols:
        df = dfs.get(s)
        if df is None or not len(df):
            continue
        tr, cv, inf, dl = run_donchian(df, p, symbol=s, global_state=global_state)
        results.append((s, tr, cv, inf, dl))

    trades, info = merge_results(results)
    m = metrics_multi(trades, p.capital)
    show_multi("PERIODE COMPLETE (DEFAUT)", m, info, trades)

    if m.get("n"):
        t_d = trades.copy()
        t_d["exit_time"] = pd.to_datetime(t_d["exit_time"])
        t_d["day"] = t_d["exit_time"].dt.normalize()
        day_pnl = t_d.groupby("day")["pnl"].sum().sort_values(ascending=False)
        if len(day_pnl):
            best_day = day_pnl.index[0]
            t2 = t_d[t_d["day"] != best_day]
            if len(t2):
                m2 = metrics_multi(t2, p.capital)
                show_multi(f"SANS LE MEILLEUR JOUR ({best_day:%Y-%m-%d})", m2, info, t2)

    # ---- Robustesse entry_period ----
    print("\n=== ROBUSTESSE : entry_period (net R | rend.% | n) ===")
    for ep in (10, 20, 40, 60):
        t2, m2 = run_single_symbol(dfs, symbols, {"entry_period": ep}, f"DC{ep}", p)
        if m2.get("n"):
            print(f"DC {ep} : {m2.get('exp', np.nan):+.3f}R | "
                  f"{m2.get('ret', np.nan):+.1f}% | n={m2['n']}")

    # ---- Robustesse exit_period ----
    print("\n=== ROBUSTESSE : exit_period (net R | rend.% | n) ===")
    for xp in (5, 10, 20):
        t2, m2 = run_single_symbol(dfs, symbols, {"exit_period": xp}, f"Exit{xp}", p)
        if m2.get("n"):
            print(f"Exit DC {xp} : {m2.get('exp', np.nan):+.3f}R | "
                  f"{m2.get('ret', np.nan):+.1f}% | n={m2['n']}")

    # ---- Robustesse EMA ----
    print("\n=== ROBUSTESSE : EMA filter (net R | rend.% | n) ===")
    for ef in (100, 150, 200, 300):
        t2, m2 = run_single_symbol(dfs, symbols, {"ema_filter": ef}, f"EMA{ef}", p)
        if m2.get("n"):
            print(f"EMA {ef} : {m2.get('exp', np.nan):+.3f}R | "
                  f"{m2.get('ret', np.nan):+.1f}% | n={m2['n']}")

    # ---- Robustesse ATR ----
    print("\n=== ROBUSTESSE : filtre ATR (net R | rend.% | n) ===")
    for ar in (0.3, 0.5, 0.7, 1.0):
        t2, m2 = run_single_symbol(dfs, symbols, {"min_atr_ratio": ar}, f"ATR{ar}", p)
        if m2.get("n"):
            print(f"ATR min {ar}x : {m2.get('exp', np.nan):+.3f}R | "
                  f"{m2.get('ret', np.nan):+.1f}% | n={m2['n']}")

    # ---- Sans EMA ----
    print("\n=== SANS filtre EMA (net R | rend.% | n) ===")
    t2, m2 = run_single_symbol(dfs, symbols, {"use_ema_filter": False}, "NoEMA", p)
    if m2.get("n"):
        print(f"Sans EMA : {m2.get('exp', np.nan):+.3f}R | "
              f"{m2.get('ret', np.nan):+.1f}% | n={m2['n']}")

    # ---- Combinaison optimale (DC10 + Exit20 + EMA300) ----
    print("\n=== COMBINAISONS OPTIMALES ===")
    combos = [
        ({"entry_period": 10}, "DC10"),
        ({"entry_period": 10, "exit_period": 20}, "DC10+Exit20"),
        ({"entry_period": 10, "exit_period": 20, "ema_filter": 300}, "DC10+Exit20+EMA300"),
        ({"entry_period": 20, "exit_period": 20, "ema_filter": 300}, "DC20+Exit20+EMA300"),
        ({"entry_period": 10, "exit_period": 20, "ema_filter": 300, "min_atr_ratio": 0.3}, "DC10+Exit20+EMA300+ATR0.3"),
    ]
    for kw, label in combos:
        t2, m2 = run_single_symbol(dfs, symbols, kw, label, p)
        if m2.get("n"):
            print(f"{label:30s} : {m2.get('exp', np.nan):+.3f}R | "
                  f"{m2.get('ret', np.nan):+.1f}% | "
                  f"DD {m2.get('dd', np.nan):.1f}% | n={m2['n']}")

    print("\nLecture : viser espérance > +0,10 R, PF > 1, stabilité sur DC/EMA/ATR.",
          flush=True)


async def health_server():
    port = int(os.getenv("PORT", "10000"))
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"ARKAS TREND V14 backtest OK - no live orders"
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
    log(f"Compte connecté. Symboles : {SYMBOLS} | {TIMEFRAME} | {YEARS} an(s)")

    dfs = {}
    for s in SYMBOLS:
        try:
            df = await fetch_history(account, s, TIMEFRAME, YEARS)
            if df is not None and len(df) > 250:
                dfs[s] = df
                log(f"[{s}] {len(df)} bougies D1 chargées")
            else:
                log(f"[{s}] données insuffisantes ({len(df) if df is not None else 0} bougies)")
        except Exception as exc:
            log(f"[{s}] erreur: {type(exc).__name__}: {exc}")

    if dfs:
        report(list(dfs.keys()), dfs)
    else:
        print("Aucune donnée chargée.", flush=True)

    keepalive_task.cancel()
    print("\nTERMINÉ. Aucun ordre n'a été passé.", flush=True)
    await server_task


if __name__ == "__main__":
    asyncio.run(main())
