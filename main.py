"""ARKAS RANGE POC V13 — backtest only. AUCUN ordre réel.
Multi-symboles : AUDNZD, EURGBP, XAUUSD.
SL derrière le dernier swing (liquidité), TP = 1.5 x SL, BE à +1R, trailing 0.5 ATR.
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
SYMBOLS = [s.strip() for s in os.getenv("BT_SYMBOLS", "AUDNZD,EURGBP,XAUUSD").split(",") if s.strip()]
YEARS = float(os.getenv("BT_YEARS", "1.0"))
SPREAD_OVERRIDES = {
    "AUDNZD": 0.0002,
    "EURGBP": 0.0002,
    "XAUUSD": 0.30,
    "EURCHF": 0.0002,
    "NZDUSD": 0.0002,
    "USDCAD": 0.0002,
}
DEFAULT_SPREAD = float(os.getenv("BT_SPREAD", "0.0002"))
RISK_PCT = float(os.getenv("BT_RISK", "1.0"))
TIMEFRAME = os.getenv("BT_TF", "15m")
INITIAL_CAPITAL = float(os.getenv("BT_CAPITAL", "10000"))
MAX_TRADES_PER_DAY = int(os.getenv("BT_MAX_TRADES_DAY", "3"))


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
    return df.iloc[:-1].reset_index(drop=True)


def get_spread(symbol):
    return SPREAD_OVERRIDES.get(symbol.upper(), DEFAULT_SPREAD)


def make_params(**over):
    p = argparse.Namespace(
        spread=DEFAULT_SPREAD, slippage=0.0, risk=RISK_PCT,
        lookback_bars=96,
        min_range_atr=1.0, max_range_atr=8.0,
        poc_ratio=0.5,
        poc_zone_atr=0.25,
        break_timeout_bars=16,
        # SL/TP
        swing_lookback=20,         # fenêtre pour trouver le dernier swing low/high
        sl_pad_atr=0.3,            # marge au-delà du swing
        sl_min_atr=1.0, sl_max_atr=4.0,
        tp_R=1.5,                  # TP = 1.5 x distance SL
        # Trailing / BE
        be_trigger_R=1.0,
        trail_atr=0.5,
        trail_start_R=1.0,
        # Gestion
        max_hold=48,
        max_positions=3,
        max_trades_per_day=MAX_TRADES_PER_DAY,
        max_total_risk_pct=3.0,
        max_spread_ratio=0.15,
        atr=14,
        capital=INITIAL_CAPITAL,
        long_only=False, short_only=False,
        exclude_days=None,
        gap_slip_tp=0.25, gap_slip_sl=0.25,
    )
    for k, v in over.items():
        setattr(p, k, v)
    return p


def find_recent_swing(h, l, i, direction, lookback):
    """
    direction=1  -> cherche le dernier swing LOW (bas) sur [i-lookback : i]
    direction=-1 -> cherche le dernier swing HIGH (haut) sur [i-lookback : i]
    """
    lo0 = max(0, i - lookback)
    if lo0 >= i:
        return None
    if direction == 1:
        # Swing low local
        seg = l[lo0:i]
        if len(seg) == 0:
            return None
        idx = int(np.argmin(seg)) + lo0
        return float(l[idx]), idx
    else:
        seg = h[lo0:i]
        if len(seg) == 0:
            return None
        idx = int(np.argmax(seg)) + lo0
        return float(h[idx]), idx


def run_symbol(df, p, symbol="SYM", global_state=None, start_idx=0):
    d = df.reset_index(drop=True).copy()
    if len(d) < max(120, p.atr + 2):
        return pd.DataFrame(), pd.Series(dtype=float), {}, pd.DataFrame()

    o, h, l, c = (d[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    n = len(d)
    prev_close = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])
    atr = pd.Series(tr).rolling(p.atr, min_periods=p.atr).mean().to_numpy()
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
    watch = None

    excluded = set()
    if p.exclude_days:
        excluded = {np.datetime64(pd.Timestamp(x).normalize(), "D") for x in p.exclude_days}

    if global_state is None:
        global_state = {"trades_today": {}, "positions_total": 0}

    cur_day = None
    day_open_equity = equity

    info = {
        "signals_long": 0, "signals_short": 0,
        "breaks_detected": 0, "breaks_timeout": 0,
        "returns_to_zone": 0,
        "rejected_range": 0, "rejected_spread": 0,
        "rejected_max_pos": 0, "rejected_max_risk": 0,
        "rejected_max_trades_day": 0, "rejected_global_pos": 0,
        "fills_stop_gap": 0, "fills_tp_gap": 0,
        "be_moves": 0, "trail_moves": 0,
    }

    def open_risk_pct():
        return sum(pos["risk_money"] for pos in positions) / p.capital * 100.0

    def close_position(pos, i, raw_fill, reason, gap_kind=None):
        nonlocal equity
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
        elif gap_kind == "tp":
            fill_raw -= dr * p.gap_slip_tp * pos["dist"]
            info["fills_tp_gap"] += 1
        fill = fill_raw - dr * (half_spread + slip)
        pnl = (fill - pos["entry"]) * dr * pos["size"]
        equity += pnl
        trades.append({
            "symbol": symbol,
            "entry_time": pd.Timestamp(pos["entry_time"]),
            "exit_time": pd.Timestamp(times[i]),
            "dir": "LONG" if dr == 1 else "SHORT",
            "entry": pos["entry"], "exit": fill,
            "stop_initial": pos["stop_initial"], "stop_final": pos["stop"],
            "tp": pos["tp"],
            "pnl": pnl, "R": pnl / pos["risk_money"] if pos["risk_money"] else 0.0,
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

        # ---- Gestion positions : BE, trailing, SL/TP ----
        for pos in list(positions):
            if i <= pos["entry_i"]:
                continue
            dr = pos["dir"]
            cur_price = c[i]
            move_R = (cur_price - pos["entry"]) * dr / pos["dist"]

            # BE
            if move_R >= p.be_trigger_R and not pos["be_done"]:
                pos["stop"] = pos["entry"]
                pos["be_done"] = True
                info["be_moves"] += 1

            # Trailing
            if move_R >= p.trail_start_R:
                new_stop = cur_price - dr * p.trail_atr * atr[i]
                if dr == 1 and new_stop > pos["stop"]:
                    pos["stop"] = new_stop
                    info["trail_moves"] += 1
                elif dr == -1 and new_stop < pos["stop"]:
                    pos["stop"] = new_stop
                    info["trail_moves"] += 1

            if dr == 1:
                if o[i] <= pos["stop"]:
                    reason = "BE/trail (gap)" if pos["be_done"] else "stop (gap)"
                    close_position(pos, i, min(pos["stop"], o[i]), reason, "sl")
                elif o[i] >= pos["tp"]:
                    close_position(pos, i, pos["tp"], "objectif (gap)", "tp")
                elif l[i] <= pos["stop"]:
                    reason = "BE/trail" if pos["be_done"] else "stop"
                    close_position(pos, i, pos["stop"], reason)
                elif h[i] >= pos["tp"]:
                    close_position(pos, i, pos["tp"], "objectif")
            else:
                if o[i] >= pos["stop"]:
                    reason = "BE/trail (gap)" if pos["be_done"] else "stop (gap)"
                    close_position(pos, i, max(pos["stop"], o[i]), reason, "sl")
                elif o[i] <= pos["tp"]:
                    close_position(pos, i, pos["tp"], "objectif (gap)", "tp")
                elif h[i] >= pos["stop"]:
                    reason = "BE/trail" if pos["be_done"] else "stop"
                    close_position(pos, i, pos["stop"], reason)
                elif l[i] <= pos["tp"]:
                    close_position(pos, i, pos["tp"], "objectif")

            if pos in positions:
                if i - pos["entry_i"] + 1 >= p.max_hold:
                    close_position(pos, i, c[i], "temps")
                elif i + 1 >= n or weekdays[i + 1] >= 5:
                    close_position(pos, i, c[i], "fin de semaine")

        # ---- Surveillance retour zone POC ----
        if watch is not None and len(positions) < p.max_positions:
            if i - watch["placed_i"] >= p.break_timeout_bars:
                info["breaks_timeout"] += 1
                watch = None
            else:
                dr = watch["dir"]
                zone_lo, zone_hi = watch["zone_lo"], watch["zone_hi"]
                price_in_zone = (l[i] <= zone_hi) and (h[i] >= zone_lo)
                if price_in_zone:
                    info["returns_to_zone"] += 1

                    # ---- SL : derrière le dernier swing (liquidité) ----
                    swing = find_recent_swing(h, l, i, direction=dr, lookback=p.swing_lookback)
                    if swing is None:
                        watch = None
                        continue
                    swing_price = swing[0]
                    # SL sous le swing pour LONG, au-dessus pour SHORT
                    if dr == 1:
                        sl_px_raw = swing_price - p.sl_pad_atr * atr[i]
                    else:
                        sl_px_raw = swing_price + p.sl_pad_atr * atr[i]

                    # Entrée au close actuel
                    entry = c[i] + dr * (half_spread + slip)
                    dist = abs(entry - sl_px_raw)
                    # Bornes ATR
                    A = atr[i]
                    dist = min(max(dist, p.sl_min_atr * A), p.sl_max_atr * A)
                    if dist <= 0:
                        watch = None
                        continue
                    sl_px = entry - dr * dist

                    # Filtre spread
                    if spread > p.max_spread_ratio * dist:
                        info["rejected_spread"] += 1
                        watch = None
                        continue

                    risk_money = max(equity, 0.0) * p.risk / 100.0
                    if risk_money <= 0:
                        watch = None
                        continue
                    if open_risk_pct() + risk_money / p.capital * 100.0 > p.max_total_risk_pct:
                        info["rejected_max_risk"] += 1
                        watch = None
                        continue
                    if global_state["positions_total"] >= p.max_positions:
                        info["rejected_global_pos"] += 1
                        watch = None
                        continue
                    if global_state["trades_today"].get(cur_day, 0) >= p.max_trades_per_day:
                        info["rejected_max_trades_day"] += 1
                        watch = None
                        continue

                    # ---- TP = 1.5 x distance SL ----
                    tp_px = entry + dr * dist * p.tp_R

                    positions.append({
                        "dir": dr, "entry": entry,
                        "stop": sl_px, "stop_initial": sl_px,
                        "tp": tp_px, "dist": dist,
                        "size": risk_money / dist,
                        "risk_money": risk_money,
                        "entry_time": times[i], "entry_i": i,
                        "be_done": False,
                    })
                    global_state["positions_total"] += 1
                    global_state["trades_today"][cur_day] = global_state["trades_today"].get(cur_day, 0) + 1
                    if dr == 1:
                        info["signals_long"] += 1
                    else:
                        info["signals_short"] += 1
                    watch = None

        # ---- Détection cassure ----
        in_session = weekdays[i] < 5
        can_watch = (in_session and i >= max(start_idx, p.lookback_bars)
                     and i > 0 and np.isfinite(atr[i]) and watch is None
                     and len(positions) < p.max_positions)
        if can_watch:
            A = atr[i]
            rng_hi = h[i - p.lookback_bars:i].max()
            rng_lo = l[i - p.lookback_bars:i].min()
            rng = rng_hi - rng_lo
            valid_range = rng > 0 and p.min_range_atr * A <= rng <= p.max_range_atr * A
            if not valid_range:
                info["rejected_range"] += 1
            else:
                poc = rng_lo + p.poc_ratio * rng
                zone_half = p.poc_zone_atr * A
                zone_lo = poc - zone_half
                zone_hi = poc + zone_half
                bull_break = c[i] > rng_hi
                bear_break = c[i] < rng_lo

                if bear_break and not p.short_only:
                    watch = {"dir": 1, "poc": poc, "zone_lo": zone_lo, "zone_hi": zone_hi,
                             "rng_hi": rng_hi, "rng_lo": rng_lo, "placed_i": i}
                    info["breaks_detected"] += 1
                elif bull_break and not p.long_only:
                    watch = {"dir": -1, "poc": poc, "zone_lo": zone_lo, "zone_hi": zone_hi,
                             "rng_hi": rng_hi, "rng_lo": rng_lo, "placed_i": i}
                    info["breaks_detected"] += 1

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
        print(f"  breaks détectés : {info.get('breaks_detected', 0)}")
        print(f"  retours zone    : {info.get('returns_to_zone', 0)}")
        return
    print(f"Jours             : {m.get('days', 0)}")
    print(f"Trades            : {m['n']} ({m['n'] / max(m.get('days', 1), 1):.2f}/jour)")
    print(f"Longs/shorts      : {info.get('signals_long', 0)}/{info.get('signals_short', 0)}")
    print(f"  breaks détectés : {info.get('breaks_detected', 0)}")
    print(f"  timeouts        : {info.get('breaks_timeout', 0)}")
    print(f"  retours zone    : {info.get('returns_to_zone', 0)}")
    print(f"  rejets max_pos  : {info.get('rejected_global_pos', 0)}")
    print(f"  rejets max_jour : {info.get('rejected_max_trades_day', 0)}")
    print(f"  BE moves        : {info.get('be_moves', 0)}")
    print(f"  trailing moves  : {info.get('trail_moves', 0)}")
    print(f"Réussite          : {m.get('win', float('nan')):.1f} %")
    print(f"Espérance nette   : {m.get('exp', float('nan')):+.3f} R/trade")
    print(f"Avant coûts       : {m.get('exp_gross', float('nan')):+.3f} R/trade")
    print(f"Profit factor     : {m.get('pf', float('nan')):.2f}")
    print(f"Rendement         : {m.get('ret', float('nan')):+.1f} %")
    print(f"Drawdown max      : {m.get('dd', float('nan')):.1f} %")
    print(f"Pires pertes suite: {m.get('streak', 0)}")
    print(f"Sorties           : {m.get('reasons', {})}")
    if len(trades):
        print("--- PAR SYMBOLE ---")
        for sym, g in trades.groupby("symbol"):
            r = g["R"]
            print(f"  {sym:8s}: n={len(g):3d} | win={((r>0).mean()*100):5.1f}% | "
                  f"exp={r.mean():+.3f}R | pnl={g['pnl'].sum():+.2f}")


def report(symbols, dfs, oos=0.30):
    p = make_params()
    print("\n" + "#" * 64)
    print(f"# ARKAS RANGE POC V13 | symboles : {symbols} | {TIMEFRAME}")
    print(f"# SL derrière dernier swing | TP {p.tp_R}R | BE {p.be_trigger_R}R | "
          f"trail {p.trail_atr}ATR | max {p.max_positions} pos | {p.max_trades_per_day} trades/j")
    print(f"# AUCUN ordre réel. Simulation OHLC.")
    print("#" * 64)

    global_state = {"trades_today": {}, "positions_total": 0}
    results = []
    for s in symbols:
        df = dfs.get(s)
        if df is None or not len(df):
            continue
        tr, cv, inf, dl = run_symbol(df, p, symbol=s, global_state=global_state)
        results.append((s, tr, cv, inf, dl))

    trades, info = merge_results(results)
    m = metrics_multi(trades, p.capital)
    show_multi("PERIODE COMPLETE (MULTI)", m, info, trades)

    if m.get("n"):
        # Sans meilleur jour
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

    # Sensibilité spread
    print("\n=== SENSIBILITE SPREAD (net R | rend.% | DD% | n) ===")
    for mult in (0.5, 1.0, 2.0, 3.0):
        gs = {"trades_today": {}, "positions_total": 0}
        r2 = []
        for s in symbols:
            df = dfs.get(s)
            if df is None or not len(df):
                continue
            # On overrid le spread par symbole x mult
            sp_base = get_spread(s)
            old = SPREAD_OVERRIDES.get(s.upper(), DEFAULT_SPREAD)
            SPREAD_OVERRIDES[s.upper()] = sp_base * mult
            tr, cv, inf, dl = run_symbol(df, make_params(), symbol=s, global_state=gs)
            SPREAD_OVERRIDES[s.upper()] = old
            r2.append((s, tr, cv, inf, dl))
        t2, _ = merge_results(r2)
        m2 = metrics_multi(t2, p.capital)
        if m2.get("n"):
            print(f"spread x{mult} : {m2.get('exp', np.nan):+.3f}R | "
                  f"{m2.get('ret', np.nan):+.1f}% | DD {m2.get('dd', np.nan):.1f}% | n={m2['n']}")

    # Robustesse TP
    print("\n=== ROBUSTESSE : TP (R multiples) (net R | rend.% | n) ===")
    for tpr in (1.0, 1.5, 2.0, 3.0):
        gs = {"trades_today": {}, "positions_total": 0}
        r2 = []
        for s in symbols:
            df = dfs.get(s)
            if df is None or not len(df):
                continue
            tr, cv, inf, dl = run_symbol(df, make_params(tp_R=tpr), symbol=s, global_state=gs)
            r2.append((s, tr, cv, inf, dl))
        t2, _ = merge_results(r2)
        m2 = metrics_multi(t2, p.capital)
        if m2.get("n"):
            print(f"TP {tpr}R : {m2.get('exp', np.nan):+.3f}R | "
                  f"{m2.get('ret', np.nan):+.1f}% | n={m2['n']}")

    # Robustesse BE
    print("\n=== ROBUSTESSE : BE trigger (net R | rend.% | n) ===")
    for be in (0.5, 1.0, 1.5, 2.0):
        gs = {"trades_today": {}, "positions_total": 0}
        r2 = []
        for s in symbols:
            df = dfs.get(s)
            if df is None or not len(df):
                continue
            tr, cv, inf, dl = run_symbol(df, make_params(be_trigger_R=be), symbol=s, global_state=gs)
            r2.append((s, tr, cv, inf, dl))
        t2, _ = merge_results(r2)
        m2 = metrics_multi(t2, p.capital)
        if m2.get("n"):
            print(f"BE {be}R : {m2.get('exp', np.nan):+.3f}R | "
                  f"{m2.get('ret', np.nan):+.1f}% | n={m2['n']}")

    # Robustesse swing_lookback
    print("\n=== ROBUSTESSE : swing lookback (net R | rend.% | n) ===")
    for sw in (10, 20, 40):
        gs = {"trades_today": {}, "positions_total": 0}
        r2 = []
        for s in symbols:
            df = dfs.get(s)
            if df is None or not len(df):
                continue
            tr, cv, inf, dl = run_symbol(df, make_params(swing_lookback=sw), symbol=s, global_state=gs)
            r2.append((s, tr, cv, inf, dl))
        t2, _ = merge_results(r2)
        m2 = metrics_multi(t2, p.capital)
        if m2.get("n"):
            print(f"swing {sw} : {m2.get('exp', np.nan):+.3f}R | "
                  f"{m2.get('ret', np.nan):+.1f}% | n={m2['n']}")

    print("\nLecture : viser espérance > +0,10 R, BE moves > 15% des trades, "
          "stabilité sur les paramètres.", flush=True)


async def health_server():
    port = int(os.getenv("PORT", "10000"))
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"ARKAS V13 backtest OK - no live orders"
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
            if df is not None and len(df) > 100:
                dfs[s] = df
                log(f"[{s}] {len(df)} bougies chargées")
            else:
                log(f"[{s}] données insuffisantes")
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
