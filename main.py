"""ARKAS RANGE POC — V10 backtest only.
No order-placement API is used. Simulations are candle-based, not tick-accurate.

Stratégie : Range Fade avec ordre limite au POC (Point of Control = milieu du range).
  1. Identifier le range sur les N dernières bougies (Range High / Range Low).
  2. POC = milieu du range (ratio 0.5 par défaut).
  3. Quand le prix CASSE le range, placer un ordre LIMITE au POC dans le sens opposé.
  4. Attendre le retour du prix au POC (validité X bougies).
  5. SL = au-delà du bord cassé + marge ATR. TP = bord opposé du range.
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
YEARS = float(os.getenv("BT_YEARS", "1.0"))
SPREAD = float(os.getenv("BT_SPREAD", "0.30"))
RISK_PCT = float(os.getenv("BT_RISK", "1.0"))
TIMEFRAME = os.getenv("BT_TF", "1h")
INITIAL_CAPITAL = float(os.getenv("BT_CAPITAL", "10000"))


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


async def fetch_history(account, symbol, timeframe, years):
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
        if len(rows) % 2000 < 1000:
            log(f"[{symbol}] {len(rows)} bougies; plus ancienne : {oldest:%Y-%m-%d}")
        if oldest <= target or (prev_oldest is not None and oldest >= prev_oldest) or len(rows) > 60000:
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


def make_params(**over):
    p = argparse.Namespace(
        spread=SPREAD, slippage=0.05, risk=RISK_PCT,
        lookback_bars=24,
        min_range_atr=1.0, max_range_atr=8.0,
        poc_ratio=0.5,
        limit_valid_bars=12,
        sl_pad_atr=0.5,
        sl_min_atr=1.0, sl_max_atr=4.0,
        tp_mode="opposite",
        rr=2.0,
        max_hold=48,
        max_consec=2,
        max_positions=1,
        max_total_risk_pct=2.0,
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


def run_range_poc(df, p, start_idx=0):
    d = df.reset_index(drop=True).copy()
    if len(d) < max(60, p.atr + 2):
        return pd.DataFrame(), pd.Series(dtype=float), {}, pd.DataFrame()

    o, h, l, c = (d[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    n = len(d)
    prev_close = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])
    atr = pd.Series(tr).rolling(p.atr, min_periods=p.atr).mean().to_numpy()
    times = d["time"].to_numpy()
    weekdays = d["time"].dt.weekday.to_numpy()
    day_values = d["time"].dt.normalize().to_numpy()
    half_spread = max(float(p.spread), 0.0) / 2.0
    slip = max(float(p.slippage), 0.0)

    equity = float(p.capital)
    positions, pending_orders, trades = [], [], []
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

    info = {
        "signals_long": 0, "signals_short": 0,
        "rejected_range": 0, "rejected_spread": 0,
        "rejected_max_pos": 0, "rejected_max_risk": 0,
        "orders_placed": 0, "orders_filled": 0,
        "orders_expired": 0, "orders_cancelled": 0,
        "fills_stop_gap": 0, "fills_tp_gap": 0,
        "days_consec_hit": 0,
    }

    def open_risk_pct():
        return sum(pos["risk_money"] for pos in positions) / p.capital * 100.0

    def close_position(pos, i, raw_fill, reason, gap_kind=None):
        nonlocal equity, consec_losses, day_halted, halt_reason
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
            info["fills_stop_gap"] += 1
        elif gap_kind == "tp":
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

        # ---- Gestion des positions ouvertes ----
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
                elif i + 1 >= n or weekdays[i + 1] >= 5:
                    close_position(pos, i, c[i], "fin de semaine")

        # ---- Gestion des ordres limites en attente ----
        still_pending = []
        for order in pending_orders:
            if i - order["placed_i"] >= p.limit_valid_bars:
                info["orders_expired"] += 1
                continue
            if len(positions) >= p.max_positions:
                info["orders_cancelled"] += 1
                continue
            dr = order["dir"]
            poc = order["poc"]
            filled = False
            if dr == 1:
                if l[i] <= poc:
                    fill_price = min(poc, o[i])
                    entry = fill_price + dr * (half_spread + slip)
                    dist = order["dist"]
                    if p.spread > p.max_spread_ratio * dist:
                        info["rejected_spread"] += 1
                        continue
                    risk_money = max(equity, 0.0) * p.risk / 100.0
                    if risk_money <= 0:
                        continue
                    if open_risk_pct() + risk_money / p.capital * 100.0 > p.max_total_risk_pct:
                        info["rejected_max_risk"] += 1
                        continue
                    positions.append({
                        "dir": dr, "entry": entry,
                        "stop": order["stop"], "tp": order["tp"],
                        "dist": dist, "size": risk_money / dist,
                        "risk_money": risk_money,
                        "entry_time": times[i], "entry_i": i,
                    })
                    info["orders_filled"] += 1
                    info["signals_long"] += 1
                    filled = True
            else:
                if h[i] >= poc:
                    fill_price = max(poc, o[i])
                    entry = fill_price + dr * (half_spread + slip)
                    dist = order["dist"]
                    if p.spread > p.max_spread_ratio * dist:
                        info["rejected_spread"] += 1
                        continue
                    risk_money = max(equity, 0.0) * p.risk / 100.0
                    if risk_money <= 0:
                        continue
                    if open_risk_pct() + risk_money / p.capital * 100.0 > p.max_total_risk_pct:
                        info["rejected_max_risk"] += 1
                        continue
                    positions.append({
                        "dir": dr, "entry": entry,
                        "stop": order["stop"], "tp": order["tp"],
                        "dist": dist, "size": risk_money / dist,
                        "risk_money": risk_money,
                        "entry_time": times[i], "entry_i": i,
                    })
                    info["orders_filled"] += 1
                    info["signals_short"] += 1
                    filled = True
            if not filled:
                still_pending.append(order)
        pending_orders = still_pending

        # ---- Détection breakout pour placer un ordre limite au POC ----
        in_session = weekdays[i] < 5
        can_place = (not day_halted and in_session
                     and i >= max(start_idx, p.lookback_bars)
                     and i > 0 and np.isfinite(atr[i])
                     and len(pending_orders) == 0
                     and len(positions) < p.max_positions)
        if can_place:
            A = atr[i]
            rng_hi = h[i - p.lookback_bars:i].max()
            rng_lo = l[i - p.lookback_bars:i].min()
            rng = rng_hi - rng_lo
            valid_range = rng > 0 and p.min_range_atr * A <= rng <= p.max_range_atr * A
            if not valid_range:
                info["rejected_range"] += 1
            else:
                poc = rng_lo + p.poc_ratio * rng
                bull_break = c[i] > rng_hi
                bear_break = c[i] < rng_lo

                if bear_break and not p.short_only:
                    sl_dist = rng_lo - p.sl_pad_atr * A
                    sl_dist = min(max(abs(poc - sl_dist), p.sl_min_atr * A), p.sl_max_atr * A)
                    sl_px = poc - sl_dist
                    tp_px = rng_hi if p.tp_mode == "opposite" else poc + p.rr * sl_dist
                    pending_orders.append({
                        "dir": 1, "poc": poc, "dist": sl_dist,
                        "stop": sl_px, "tp": tp_px,
                        "placed_i": i, "rng_hi": rng_hi, "rng_lo": rng_lo,
                    })
                    info["orders_placed"] += 1

                if bull_break and not p.long_only:
                    sl_dist = rng_hi + p.sl_pad_atr * A
                    sl_dist = min(max(abs(sl_dist - poc), p.sl_min_atr * A), p.sl_max_atr * A)
                    sl_px = poc + sl_dist
                    tp_px = rng_lo if p.tp_mode == "opposite" else poc - p.rr * sl_dist
                    pending_orders.append({
                        "dir": -1, "poc": poc, "dist": sl_dist,
                        "stop": sl_px, "tp": tp_px,
                        "placed_i": i, "rng_hi": rng_hi, "rng_lo": rng_lo,
                    })
                    info["orders_placed"] += 1

        open_pnl = sum((c[i] - pos["entry"]) * pos["dir"] * pos["size"] for pos in positions)
        curve[i] = equity + open_pnl

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
    print(f"Trades            : {m['n']} ({m['n'] / max(m.get('days', 1), 1):.2f}/jour)")
    print(f"Longs/shorts      : {info.get('signals_long', 0)}/{info.get('signals_short', 0)}")
    print(f"Ordres placés     : {info.get('orders_placed', 0)}")
    print(f"  remplis         : {info.get('orders_filled', 0)}")
    print(f"  expirés         : {info.get('orders_expired', 0)}")
    print(f"  annulés         : {info.get('orders_cancelled', 0)}")
    print(f"Réussite          : {m.get('win', float('nan')):.1f} %")
    print(f"Espérance nette   : {m.get('exp', float('nan')):+.3f} R/trade")
    print(f"Avant coûts       : {m.get('exp_gross', float('nan')):+.3f} R/trade")
    print(f"Profit factor     : {m.get('pf', float('nan')):.2f}")
    print(f"Rendement         : {m.get('ret', float('nan')):+.1f} %")
    print(f"Drawdown max      : {m.get('dd', float('nan')):.1f} %")
    print(f"Pires pertes suite: {m.get('streak', 0)}")
    print(f"Fills gap SL/TP   : {info.get('fills_stop_gap', 0)}/{info.get('fills_tp_gap', 0)}")
    print(f"Rejets (range)    : {info.get('rejected_range', 0)}")
    print(f"Rejets (spread)   : {info.get('rejected_spread', 0)}")
    print(f"Sorties           : {m.get('reasons', {})}")
    if bh is not None:
        print(f"Buy & hold (réf.) : {bh:+.1f} %")
    if show_concentration:
        cs = concentration_stats(trades, daily)
        print("--- CONCENTRATION ---")
        print(f"Top 1 trade  : {cs.get('top1_share', np.nan):.1f} % du PnL")
        print(f"Top 5 trades : {cs.get('top5_share', np.nan):.1f} % du PnL")
        print(f"Top 10 trades: {cs.get('top10_share', np.nan):.1f} % du PnL")
        print(f"Meilleur jour: {cs.get('best_day', 0):+.2f} %; part: {cs.get('best_day_share', np.nan):.1f} %")


def report(symbol, df, oos=0.30):
    p = make_params()
    print("\n" + "#" * 64)
    print(f"# ARKAS RANGE POC V10 | {symbol} {TIMEFRAME} | {len(df)} bougies")
    print(f"# {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d %H:%M}")
    print(f"# Range {p.lookback_bars} bars | POC {p.poc_ratio} | "
          f"ordre valide {p.limit_valid_bars} bars | SL pad {p.sl_pad_atr}ATR | "
          f"TP {p.tp_mode} | spread {p.spread} | risque {p.risk}%")
    print("# ATTENTION: simulation OHLC, aucun ordre réel.")
    print("#" * 64)
    if len(df) < 2000:
        print("Historique insuffisant (< 2000 bougies).")
        return

    bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1.0) * 100.0

    tr, cv, inf, dl = run_range_poc(df, p)
    m = metrics(tr, cv, p.capital)
    show("PERIODE COMPLETE", m, inf, dl, tr, bh)

    cs = concentration_stats(tr, dl)
    best_day = cs.get("best_day_date")
    if best_day is not None and pd.notna(best_day):
        excluded_day = pd.Timestamp(best_day).normalize()
        p2 = make_params(exclude_days=[excluded_day])
        tr2, cv2, inf2, dl2 = run_range_poc(df, p2)
        m2 = metrics(tr2, cv2, p2.capital)
        show(f"SANS LE MEILLEUR JOUR ({excluded_day:%Y-%m-%d})", m2, inf2, dl2, tr2, bh, False)

    print("\n=== SENSIBILITE SPREAD (net R | rend.% | DD% | n) ===")
    for sp in (0.10, 0.30, 0.50, 1.0):
        tr_s, cv_s, _, _ = run_range_poc(df, make_params(spread=sp))
        m_s = metrics(tr_s, cv_s, p.capital)
        if m_s.get("n"):
            print(f"spread {sp:.2f} : {m_s.get('exp', np.nan):+.3f}R | "
                  f"{m_s.get('ret', np.nan):+.1f}% | DD {m_s.get('dd', np.nan):.1f}% | n={m_s['n']}")
        else:
            print(f"spread {sp:.2f} : aucun trade")

    print("\n=== ROBUSTESSE : lookback range (net R | rend.% | n) ===")
    for lb in (12, 24, 48, 96):
        tr_l, cv_l, _, _ = run_range_poc(df, make_params(lookback_bars=lb))
        m_l = metrics(tr_l, cv_l, p.capital)
        if m_l.get("n"):
            print(f"LB {lb} : {m_l.get('exp', np.nan):+.3f}R | "
                  f"{m_l.get('ret', np.nan):+.1f}% | n={m_l['n']}")

    print("\n=== ROBUSTESSE : POC ratio (net R | rend.% | n) ===")
    for pr in (0.382, 0.5, 0.618, 0.75):
        tr_p, cv_p, _, _ = run_range_poc(df, make_params(poc_ratio=pr))
        m_p = metrics(tr_p, cv_p, p.capital)
        if m_p.get("n"):
            print(f"POC {pr} : {m_p.get('exp', np.nan):+.3f}R | "
                  f"{m_p.get('ret', np.nan):+.1f}% | n={m_p['n']}")

    print("\n=== ROBUSTESSE : TP mode (net R | rend.% | n) ===")
    for mode in ("opposite", "rr"):
        for rr in ([None] if mode == "opposite" else [1.5, 2.0, 3.0]):
            kw = {"tp_mode": mode}
            if rr is not None:
                kw["rr"] = rr
            tr_t, cv_t, _, _ = run_range_poc(df, make_params(**kw))
            m_t = metrics(tr_t, cv_t, p.capital)
            if m_t.get("n"):
                label = f"{mode}" + (f" RR{rr}" if rr else "")
                print(f"{label} : {m_t.get('exp', np.nan):+.3f}R | "
                      f"{m_t.get('ret', np.nan):+.1f}% | n={m_t['n']}")

    print("\n=== ROBUSTESSE : validité ordre limite (net R | rend.% | n) ===")
    for vb in (6, 12, 24, 48):
        tr_v, cv_v, _, _ = run_range_poc(df, make_params(limit_valid_bars=vb))
        m_v = metrics(tr_v, cv_v, p.capital)
        if m_v.get("n"):
            print(f"valid {vb} bars : {m_v.get('exp', np.nan):+.3f}R | "
                  f"{m_v.get('ret', np.nan):+.1f}% | n={m_v['n']}")

    split = int(len(df) * (1.0 - oos))
    train_df = df.iloc[:split].reset_index(drop=True)
    test_df = df.iloc[split:].reset_index(drop=True)
    print(f"\n=== VALIDATION OOS CHRONOLOGIQUE ({100*oos:.0f}% final) ===")
    for label, part in (("TRAIN", train_df), ("OOS", test_df)):
        tr_v, cv_v, inf_v, dl_v = run_range_poc(part, p)
        m_v = metrics(tr_v, cv_v, p.capital)
        if m_v.get("n"):
            print(f"{label:5s}: trades={m_v['n']:4d} | exp={m_v.get('exp', np.nan):+.3f}R | "
                  f"PF={m_v.get('pf', np.nan):.2f} | ret={m_v.get('ret', np.nan):+.1f}% | "
                  f"DD={m_v.get('dd', np.nan):.1f}%")
        else:
            print(f"{label:5s}: aucun trade exploitable")

    print("\nLecture : viser espérance > +0,10 R sur les DEUX segments ET stabilité sur les paramètres.")


async def health_server():
    port = int(os.getenv("PORT", "10000"))
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"ARKAS RANGE POC V10 backtest service OK - no live orders"
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
        print("\nTERMINÉ. Aucun ordre n'a été passé.", flush=True)
        await server_task


if __name__ == "__main__":
    asyncio.run(main())
