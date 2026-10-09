"""ARKAS ORB XAUUSD M5 — V8 backtest only.
No order-placement API is used. Simulations are candle-based, not tick-accurate.
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
SPREAD = float(os.getenv("BT_SPREAD", "0.30"))
RISK_PCT = float(os.getenv("BT_RISK", "1.0"))
TIMEFRAME = "5m"
INITIAL_CAPITAL = float(os.getenv("BT_CAPITAL", "10000"))


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


async def fetch_history(account, symbol, timeframe, years):
    """Download candles backwards; deduplicate and drop the last possibly-open candle."""
    target = datetime.now(timezone.utc) - timedelta(days=365.25 * years)
    rows, start, prev_oldest = [], None, None
    while True:
        batch = await account.get_historical_candles(
            symbol=symbol, timeframe=timeframe, start_time=start, limit=1000
        )
        if not batch:
            break
        batch = sorted(batch, key=lambda r: r["time"])
        rows = batch + rows
        oldest = batch[0]["time"]
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        if len(rows) % 5000 < 1000:
            log(f"[{symbol}] {len(rows)} bougies; plus ancienne : {oldest:%Y-%m-%d}")
        if oldest <= target or (prev_oldest is not None and oldest >= prev_oldest) or len(rows) > 120000:
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
    # Avoid keeping the most recent candle, which may not be closed yet.
    return df.iloc[:-1].reset_index(drop=True)


def make_params(**over):
    p = argparse.Namespace(
        spread=SPREAD, slippage=0.05, risk=RISK_PCT, rr=2.0, atr=14,
        sess_start=8, sess_end=16, orb_minutes=30,
        min_range_atr=0.3, max_range_atr=3.0,
        sl_mode="mid", max_hold=96, max_consec=2,
        max_positions=3, max_total_risk_pct=3.0,
        allow_same_dir=True, min_price_gap_atr=0.5,
        max_spread_ratio=0.15, capital=INITIAL_CAPITAL,
        long_only=False, exclude_days=None,
        gap_slip_tp=0.0, gap_slip_sl=0.0,
    )
    for k, v in over.items():
        setattr(p, k, v)
    return p


def run_orb(df, p, start_idx=0):
    """Candle-level ORB simulation. A new position is not stopped/targeted on its entry bar."""
    d = df.reset_index(drop=True).copy()
    if len(d) < max(30, p.atr + 2):
        return pd.DataFrame(), pd.Series(dtype=float), {}, pd.DataFrame()

    o, h, l, c = (d[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    n = len(d)
    prev_close = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])
    atr = pd.Series(tr).rolling(p.atr, min_periods=p.atr).mean().to_numpy()
    times = d["time"].to_numpy()
    hours = d["time"].dt.hour.to_numpy()
    minutes = d["time"].dt.minute.to_numpy()
    day_values = d["time"].dt.normalize().to_numpy()
    weekdays = d["time"].dt.weekday.to_numpy()
    half_spread = max(float(p.spread), 0.0) / 2.0
    slip = max(float(p.slippage), 0.0)
    equity = float(p.capital)
    positions, trades = [], []
    curve = np.full(n, np.nan)
    daily_rows = []
    excluded = set()
    if p.exclude_days:
        excluded = {np.datetime64(pd.Timestamp(x).normalize(), "D") for x in p.exclude_days}

    cur_day = None
    day_open_equity = equity
    day_halted = False
    halt_reason = None
    consec_losses = 0
    orb_high = orb_low = None
    orb_locked = False
    orb_window_seen = False
    info = {
        "orb_days": 0, "orb_skipped_range": 0, "orb_skipped_spread": 0,
        "longs": 0, "shorts": 0, "days_consec_hit": 0,
        "signals_rejected_max_pos": 0, "signals_rejected_max_risk": 0,
        "signals_rejected_same_dir": 0, "fills_stop_gap": 0,
        "fills_tp_gap": 0, "signals_rejected_entry_bar_ambiguity": 0,
    }

    def open_risk_pct():
        # Risk is expressed as a fraction of current initial capital for a conservative cap.
        return sum(pos["risk_money"] for pos in positions) / p.capital * 100.0

    def close_position(pos, i, raw_fill, reason, gap_kind=None):
        nonlocal equity, consec_losses, day_halted, halt_reason
        if pos not in positions:
            return
        positions.remove(pos)
        dr = pos["dir"]
        fill_raw = float(raw_fill)
        # For stop gaps, use the observed opening price when it is worse than the stop.
        # gap_slip_sl is an additional stress penalty measured in fractions of stop distance.
        if gap_kind == "sl":
            if dr == 1:
                fill_raw = min(fill_raw, float(o[i]))
                fill_raw -= p.gap_slip_sl * pos["dist"]
            else:
                fill_raw = max(fill_raw, float(o[i]))
                fill_raw += p.gap_slip_sl * pos["dist"]
            info["fills_stop_gap"] += 1
        elif gap_kind == "tp":
            # TP gaps are not assumed to receive a better-than-limit fill; apply optional stress.
            fill_raw -= dr * p.gap_slip_tp * pos["dist"]
            info["fills_tp_gap"] += 1
        fill = fill_raw - dr * (half_spread + slip)
        pnl = (fill - pos["entry"]) * dr * pos["size"]
        equity += pnl
        trades.append({
            "entry_time": pd.Timestamp(pos["entry_time"]),
            "exit_time": pd.Timestamp(times[i]),
            "dir": "LONG" if dr == 1 else "SHORT", "entry": pos["entry"],
            "exit": fill, "stop": pos["stop"], "tp": pos["tp"],
            "pnl": pnl, "R": pnl / pos["risk_money"] if pos["risk_money"] else 0.0,
            "cost_R": (p.spread + 2 * slip) / pos["dist"] if pos["dist"] else np.nan,
            "bars": i - pos["entry_i"] + 1, "reason": reason,
            "is_gap": gap_kind is not None,
        })
        consec_losses = consec_losses + 1 if pnl <= 0 else 0
        if consec_losses >= p.max_consec and not day_halted:
            day_halted = True
            halt_reason = f"{p.max_consec} pertes consécutives"
            info["days_consec_hit"] += 1

    for i in range(n):
        day_key = np.datetime64(pd.Timestamp(day_values[i]).normalize(), "D")
        if day_values[i] != cur_day:
            if cur_day is not None:
                daily_rows.append({
                    "date": pd.Timestamp(cur_day),
                    "ret_pct": (equity - day_open_equity) / p.capital * 100.0,
                    "halt_reason": halt_reason,
                })
            cur_day = day_values[i]
            day_open_equity = equity
            day_halted = day_key in excluded
            halt_reason = "jour exclu" if day_halted else None
            consec_losses = 0
            orb_high = orb_low = None
            orb_locked = False
            orb_window_seen = False

        # ORB window is defined in the candle timestamps' timezone (MetaApi server data).
        if weekdays[i] < 5 and not orb_locked:
            minute_of_day = int(hours[i]) * 60 + int(minutes[i])
            orb_start = int(p.sess_start) * 60
            orb_end = orb_start + int(p.orb_minutes)
            if orb_start <= minute_of_day < orb_end:
                orb_window_seen = True
                orb_high = h[i] if orb_high is None else max(orb_high, h[i])
                orb_low = l[i] if orb_low is None else min(orb_low, l[i])
            elif minute_of_day >= orb_end and orb_window_seen and orb_high is not None:
                orb_locked = True
                info["orb_days"] += 1

        # Manage only positions that existed before this candle. If SL and TP both touch
        # during the same candle, choose the stop first (conservative OHLC assumption).
        for pos in list(positions):
            if i <= pos["entry_i"]:
                continue
            dr = pos["dir"]
            if dr == 1:
                if o[i] <= pos["stop"]:
                    close_position(pos, i, min(pos["stop"], o[i]), "stop (gap)", "sl")
                elif o[i] >= pos["tp"]:
                    close_position(pos, i, pos["tp"], "objectif (gap)", "tp")
                elif l[i] <= pos["stop"]:
                    close_position(pos, i, pos["stop"], "stop")
                elif h[i] >= pos["tp"]:
                    close_position(pos, i, pos["tp"], "objectif")
            else:
                if o[i] >= pos["stop"]:
                    close_position(pos, i, max(pos["stop"], o[i]), "stop (gap)", "sl")
                elif o[i] <= pos["tp"]:
                    close_position(pos, i, pos["tp"], "objectif (gap)", "tp")
                elif h[i] >= pos["stop"]:
                    close_position(pos, i, pos["stop"], "stop")
                elif l[i] <= pos["tp"]:
                    close_position(pos, i, pos["tp"], "objectif")

            if pos in positions:
                if i - pos["entry_i"] + 1 >= p.max_hold:
                    close_position(pos, i, c[i], "temps")
                elif i + 1 >= n or not (
                    p.sess_start <= hours[i + 1] < p.sess_end and weekdays[i + 1] < 5
                ):
                    close_position(pos, i, c[i], "fin de session")

        in_session = p.sess_start <= hours[i] < p.sess_end and weekdays[i] < 5
        if (not day_halted and orb_locked and orb_high is not None and in_session
                and i >= start_idx and i > 0 and np.isfinite(atr[i])):
            rng = orb_high - orb_low
            A = atr[i]
            valid_range = rng > 0 and p.min_range_atr * A <= rng <= p.max_range_atr * A
            if valid_range:
                if p.sl_mode == "mid":
                    sl_long = sl_short = (orb_high + orb_low) / 2.0
                else:
                    sl_long, sl_short = orb_low, orb_high
                # Breakout detected from the completed candle; use breakout level unless
                # the candle opened beyond it, in which case use the opening price.
                candidates = []
                if h[i] >= orb_high:
                    candidates.append((1, max(orb_high, o[i]), sl_long))
                if l[i] <= orb_low and not p.long_only:
                    candidates.append((-1, min(orb_low, o[i]), sl_short))
                # If both boundaries are crossed in one M5 candle, path is unknowable:
                # reject both signals rather than choose a favorable direction.
                if h[i] >= orb_high and l[i] <= orb_low:
                    info["signals_rejected_entry_bar_ambiguity"] += len(candidates)
                    candidates = []
                for dr, trigger_px, sl_px in candidates:
                    if len(positions) >= p.max_positions:
                        info["signals_rejected_max_pos"] += 1
                        continue
                    dist = abs(trigger_px - sl_px)
                    if dist <= 0 or not np.isfinite(dist):
                        continue
                    if p.spread > p.max_spread_ratio * dist:
                        info["orb_skipped_spread"] += 1
                        continue
                    same_dir = [q for q in positions if q["dir"] == dr]
                    if same_dir:
                        if not p.allow_same_dir or any(
                            abs(trigger_px - q["entry"]) < p.min_price_gap_atr * A for q in same_dir
                        ):
                            info["signals_rejected_same_dir"] += 1
                            continue
                    risk_money = max(equity, 0.0) * p.risk / 100.0
                    if risk_money <= 0:
                        continue
                    if open_risk_pct() + risk_money / p.capital * 100.0 > p.max_total_risk_pct:
                        info["signals_rejected_max_risk"] += 1
                        continue
                    entry = trigger_px + dr * (half_spread + slip)
                    stop = entry - dr * dist
                    tp = entry + dr * dist * p.rr
                    positions.append({
                        "dir": dr, "entry": entry, "stop": stop, "tp": tp,
                        "dist": dist, "size": risk_money / dist,
                        "risk_money": risk_money, "entry_time": times[i], "entry_i": i,
                    })
                    info["longs" if dr == 1 else "shorts"] += 1
            else:
                info["orb_skipped_range"] += 1

        open_pnl = sum((c[i] - pos["entry"]) * pos["dir"] * pos["size"] for pos in positions)
        curve[i] = equity + open_pnl

    # Close any remaining positions at the final close, including normal exit costs.
    while positions:
        close_position(positions[0], n - 1, c[-1], "fin des données")
    curve[-1] = equity
    if cur_day is not None:
        daily_rows.append({
            "date": pd.Timestamp(cur_day),
            "ret_pct": (equity - day_open_equity) / p.capital * 100.0,
            "halt_reason": halt_reason,
        })
    trade_df = pd.DataFrame(trades)
    curve_series = pd.Series(curve, index=pd.to_datetime(d["time"])).ffill()
    return trade_df, curve_series, info, pd.DataFrame(daily_rows)


def metrics(trades, curve, capital, start_time=None):
    if start_time is not None and len(curve):
        curve = curve[curve.index >= start_time]
    out = {"n": len(trades)}
    if len(curve) < 2:
        return out
    base = float(curve.iloc[0]) if start_time is not None else float(capital)
    out["ret"] = (float(curve.iloc[-1]) / base - 1.0) * 100.0 if base else np.nan
    out["dd"] = ((curve / curve.cummax()) - 1.0).min() * 100.0
    out["days"] = max((curve.index[-1] - curve.index[0]).days, 1)
    if len(trades):
        r = trades["R"].replace([np.inf, -np.inf], np.nan).dropna()
        gp = r[r > 0].sum()
        gl = -r[r <= 0].sum()
        out["win"] = (r > 0).mean() * 100.0 if len(r) else 0.0
        out["exp"] = r.mean() if len(r) else np.nan
        out["exp_gross"] = (r + trades.loc[r.index, "cost_R"]).mean() if len(r) else np.nan
        out["cost"] = trades["cost_R"].mean()
        out["pf"] = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0.0)
        streak = best = 0
        for value in r.to_numpy():
            streak = streak + 1 if value <= 0 else 0
            best = max(best, streak)
        out["streak"] = best
        out["bars"] = trades["bars"].mean()
        out["reasons"] = trades["reason"].value_counts().to_dict()
    return out


def concentration_stats(trades, daily):
    out = {}
    if trades is not None and len(trades):
        r = trades["R"].sort_values(ascending=False)
        total = r.sum()
        for k in (1, 5, 10):
            out[f"top{k}_share"] = r.iloc[:k].sum() / total * 100.0 if total > 0 else np.nan
        out["top1_R"] = r.iloc[0]
    if daily is not None and len(daily):
        daily = daily.copy()
        daily["ret_pct"] = daily["ret_pct"].fillna(0.0)
        total_ret = daily["ret_pct"].sum()
        out["best_day"] = daily["ret_pct"].max()
        out["best_day_share"] = daily["ret_pct"].max() / total_ret * 100.0 if abs(total_ret) > 1e-9 else np.nan
        out["best_day_date"] = daily.loc[daily["ret_pct"].idxmax(), "date"]
    return out


def show(title, m, info, daily, trades, bh=None, show_concentration=True):
    print(f"\n=== {title} ===")
    if not m.get("n"):
        print("Aucun trade.")
        return
    print(f"Jours             : {m.get('days', 0)}")
    print(f"Trades            : {m['n']} ({m['n'] / max(m.get('days', 1), 1):.2f}/jour")
    print(f"Longs/shorts      : {info.get('longs', 0)}/{info.get('shorts', 0)}")
    print(f"Réussite          : {m.get('win', float('nan')):.1f} %")
    print(f"Espérance nette   : {m.get('exp', float('nan')):+.3f} R/trade")
    print(f"Avant coûts       : {m.get('exp_gross', float('nan')):+.3f} R/trade")
    print(f"Profit factor     : {m.get('pf', float('nan')):.2f}")
    print(f"Rendement         : {m.get('ret', float('nan')):+.1f} %")
    print(f"Drawdown max      : {m.get('dd', float('nan')):.1f} %")
    print(f"Pires pertes suite: {m.get('streak', 0)}")
    print(f"Fills gap SL/TP   : {info.get('fills_stop_gap', 0)}/{info.get('fills_tp_gap', 0)}")
    print(f"Sorties           : {m.get('reasons', {})}")
    if bh is not None:
        print(f"Buy & hold (réf.) : {bh:+.1f} %")
    if show_concentration:
        cs = concentration_stats(trades, daily)
        print("--- CONCENTRATION ---")
        print(f"Top 1 trade  : {cs.get('top1_share', np.nan):.1f} % du PnL en R")
        print(f"Top 5 trades : {cs.get('top5_share', np.nan):.1f} % du PnL en R")
        print(f"Top 10 trades: {cs.get('top10_share', np.nan):.1f} % du PnL en R")
        print(f"Meilleur jour: {cs.get('best_day', 0):+.2f} %; part du PnL journalier: {cs.get('best_day_share', np.nan):.1f} %")


def run_scenario(df, title, gap_tp, gap_sl):
    p = make_params(gap_slip_tp=gap_tp, gap_slip_sl=gap_sl)
    trades, curve, info, daily = run_orb(df, p)
    m = metrics(trades, curve, p.capital)
    return p, trades, curve, info, daily, m


def report(symbol, df, oos=0.30):
    p = make_params()
    print("\n" + "#" * 64)
    print(f"# ARKAS ORB V8 | {symbol} M5 | {len(df)} bougies")
    print(f"# {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d %H:%M}")
    print(f"# ORB {p.orb_minutes} min | RR {p.rr} | session {p.sess_start}-{p.sess_end} | spread {p.spread} | risque {p.risk}%")
    print("# ATTENTION: simulation OHLC, sizing théorique; aucun ordre réel.")
    print("#" * 64)
    if len(df) < 5000:
        print("Historique insuffisant (< 5000 bougies).")
        return
    if not 0.1 <= oos <= 0.5:
        raise ValueError("La proportion OOS doit être entre 0.10 et 0.50.")

    bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1.0) * 100.0
    print("\nTEST A — slippage TP gap, SL gap = 0")
    print(f"{'TP slip':>8} | {'Exp R':>8} | {'Rend.%':>8} | {'DD%':>7} | {'Trades':>7} | {'Best day%':>10} | {'Top5%':>7}")
    for gs in (0.0, 0.25, 0.5, 1.0):
        _, tr, cv, inf, daily, m = run_scenario(df, "A", gs, 0.0)
        cs = concentration_stats(tr, daily)
        if m.get("n"):
            print(f"{gs:8.2f} | {m.get('exp', np.nan):+8.3f} | {m.get('ret', np.nan):+8.1f} | {m.get('dd', np.nan):7.1f} | {m['n']:7d} | {cs.get('best_day', 0):+10.2f} | {cs.get('top5_share', np.nan):7.1f}")
        else:
            print(f"{gs:8.2f} | aucun trade")

    print("\nTEST B — slippage SL gap, TP gap fixé à 0.5")
    print(f"{'SL slip':>8} | {'Exp R':>8} | {'Rend.%':>8} | {'DD%':>7} | {'Trades':>7}")
    for gs in (0.0, 0.25, 0.5, 1.0):
        _, tr, cv, inf, daily, m = run_scenario(df, "B", 0.5, gs)
        if m.get("n"):
            print(f"{gs:8.2f} | {m.get('exp', np.nan):+8.3f} | {m.get('ret', np.nan):+8.1f} | {m.get('dd', np.nan):7.1f} | {m['n']:7d}")
        else:
            print(f"{gs:8.2f} | aucun trade")

    print("\nTEST C — scénario pessimiste (TP 0.5, SL 0.5)")
    _, tr, cv, inf, daily, m = run_scenario(df, "C", 0.5, 0.5)
    show("PESSIMISTE — période complète", m, inf, daily, tr, bh)
    cs = concentration_stats(tr, daily)
    best_day = cs.get("best_day_date")
    if best_day is not None and pd.notna(best_day):
        excluded_day = pd.Timestamp(best_day).normalize()
        _, tr2, cv2, inf2, daily2, m2 = run_scenario(
            df, "C sans meilleur jour", 0.5, 0.5
        )
        # Re-run explicitly excluding the best day.
        p2 = make_params(gap_slip_tp=0.5, gap_slip_sl=0.5, exclude_days=[excluded_day])
        tr2, cv2, inf2, daily2 = run_orb(df, p2)
        m2 = metrics(tr2, cv2, p2.capital)
        show(f"PESSIMISTE sans le meilleur jour ({excluded_day:%Y-%m-%d})", m2, inf2, daily2, tr2, bh, False)

    print("\nTEST D — très pessimiste (TP 1.0, SL 1.0)")
    _, trd, cvd, infd, dld, md = run_scenario(df, "D", 1.0, 1.0)
    show("TRÈS PESSIMISTE — période complète", md, infd, dld, trd, bh, False)

    # Chronological out-of-sample validation: parameters are unchanged, later segment untouched.
    split = int(len(df) * (1.0 - oos))
    train_df = df.iloc[:split].reset_index(drop=True)
    test_df = df.iloc[split:].reset_index(drop=True)
    print(f"\nTEST E — validation chronologique OOS ({100*oos:.0f}% final, {len(test_df)} bougies)")
    for label, part in (("TRAIN", train_df), ("OOS", test_df)):
        _, trv, cvv, infv, dlv, mv = run_scenario(part, label, 0.5, 0.5)
        if mv.get("n"):
            print(f"{label:5s}: trades={mv['n']:5d} | exp={mv.get('exp', np.nan):+.3f} R | PF={mv.get('pf', np.nan):.2f} | rendement={mv.get('ret', np.nan):+.1f}% | DD={mv.get('dd', np.nan):.1f}%")
        else:
            print(f"{label:5s}: aucun trade exploitable")
    print("\nNOTE: l'OOS chronologique est une première vérification, pas une preuve statistique ni une garantie de performance réelle.")


async def health_server():
    port = int(os.getenv("PORT", "10000"))
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"ARKAS ORB V8 backtest service OK - no live orders"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
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
        raise RuntimeError("Variables METAAPI_TOKEN et METAAPI_ACCOUNT_ID requises.")
    server_task = asyncio.create_task(health_server())
    keepalive_task = asyncio.create_task(keepalive())
    api = MetaApi(TOKEN, {"region": REGION})
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    if account.state != "DEPLOYED":
        await account.deploy()
    await account.wait_connected()
    log(f"Compte MetaApi connecté. Lecture historique seulement: {SYMBOL} {TIMEFRAME} | {YEARS} an(s)")
    try:
        df = await fetch_history(account, SYMBOL, TIMEFRAME, YEARS)
        if df is None or len(df) == 0:
            print(f"[{SYMBOL}] Aucune bougie reçue.", flush=True)
        else:
            report(SYMBOL, df, oos=float(os.getenv("BT_OOS", "0.30")))
    except Exception as exc:
        log(f"Erreur backtest: {type(exc).__name__}: {exc}")
    finally:
        keepalive_task.cancel()
        # Keep health endpoint alive for hosted services; no trading loop.
        print("\nTERMINÉ. Aucun ordre n'a été passé.", flush=True)
        await server_task


if __name__ == "__main__":
    asyncio.run(main())
