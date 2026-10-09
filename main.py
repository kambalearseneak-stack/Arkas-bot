"""
Backtest SCALP XAUUSD (M5) 100 % MetaApi — rapport dans les logs, AUCUN ordre passé.

Stratégie : SMC V4 — Retournement sur Order Block en zone Fibonacci 0.5-1.0
  - Biais : structure H1 (dernier swing high/low cassé)
  - Leg identifié sur M5, retracement en zone discount (long) / premium (short)
  - Entrée : OB haussier/baissier non mitigé + balayage + FVG + rejet
  - SL derrière le leg + 0.1 ATR, borné [0.8 ; 2.0] ATR
  - TP = RR x risque ; sortie forcée 24 bougies / fin de session

Garde-fous journaliers (% calculés sur le CAPITAL INITIAL, pas sur l'equity courante) :
  - Objectif : +20 % du capital initial -> arrêt de la journée
  - Limite   : -15 % du capital initial -> arrêt de la journée
  - Stop secondaire : 2 pertes consécutives

Env (en plus de METAAPI_TOKEN / METAAPI_ACCOUNT_ID / METAAPI_REGION) :
  BT_SYMBOL   (défaut XAUUSD)
  BT_YEARS    (défaut 0.75)
  BT_SESSION  (défaut "8,16" : heures broker)
  BT_SPREAD   (défaut 0.30)
  BT_RISK     (défaut 1.0 : % du capital initial risqué par trade)
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
SESSION = tuple(int(x) for x in os.getenv("BT_SESSION", "8,16").split(","))
SPREAD = float(os.getenv("BT_SPREAD", "0.30"))
RISK_PCT = float(os.getenv("BT_RISK", "1.0"))
TIMEFRAME = "5m"

# ------------------------------------------------------------------------------
# Objectifs journaliers (en % du CAPITAL INITIAL, fixe)
# ------------------------------------------------------------------------------
DAILY_TARGET_PCT = 20.0    # +20 % du capital initial -> arrêt
DAILY_LOSS_PCT = 15.0      # -15 % du capital initial -> arrêt
MAX_CONSEC_LOSSES = 2      # garde-fou secondaire
INITIAL_CAPITAL = 10000.0  # référence fixe pour tous les calculs journaliers


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
    return df.iloc[:-1].reset_index(drop=True)


# ------------------------------------------------------------------------------
# Helpers SMC
# ------------------------------------------------------------------------------
def find_swings(h, l, left=2, right=2):
    n = len(h)
    sh = np.zeros(n, dtype=bool)
    sl = np.zeros(n, dtype=bool)
    for i in range(left, n - right):
        win_h = h[i - left:i + right + 1]
        win_l = l[i - left:i + right + 1]
        if h[i] == win_h.max() and (win_h == h[i]).sum() == 1:
            sh[i] = True
        if l[i] == win_l.min() and (win_l == l[i]).sum() == 1:
            sl[i] = True
    return sh, sl


def find_leg(h, l, sh, sl, i, direction, lookback=40):
    lo0 = max(0, i - lookback)
    highs = [j for j in range(lo0, i) if sh[j]]
    lows = [j for j in range(lo0, i) if sl[j]]
    if not highs or not lows:
        return None
    if direction == -1:
        hi_idx = highs[-1]
        lows_before = [j for j in lows if j < hi_idx]
        if not lows_before:
            return None
        lo_idx = lows_before[-1]
        if hi_idx <= lo_idx:
            return None
        return lo_idx, hi_idx
    else:
        lo_idx = lows[-1]
        highs_before = [j for j in highs if j < lo_idx]
        if not highs_before:
            return None
        hi_idx = highs_before[-1]
        if hi_idx <= lo_idx:
            return None
        return lo_idx, hi_idx


def fib_zone(leg_lo, leg_hi, direction, f_lo=0.5, f_hi=1.0):
    rng = leg_hi - leg_lo
    if direction == 1:
        z_hi = leg_hi - f_lo * rng
        z_lo = leg_hi - f_hi * rng
        return z_lo, z_hi
    else:
        z_lo = leg_lo + f_lo * rng
        z_hi = leg_lo + f_hi * rng
        return z_lo, z_hi


def has_fvg(h, l, i, direction, lookback=5):
    for k in range(max(2, i - lookback), i + 1):
        if direction == 1 and l[k - 2] > h[k]:
            return True
        if direction == -1 and h[k - 2] < l[k]:
            return True
    return False


def in_killzone(hours, weekday):
    if weekday >= 5:
        return False
    return ((hours >= 8) & (hours < 11)) | ((hours >= 13) & (hours < 16))


def make_params(**over):
    p = argparse.Namespace(
        spread=SPREAD, slippage=0.05, risk=RISK_PCT, rr=2.0, atr=14,
        swing_left=2, swing_right=2,
        leg_lookback=40,
        fib_lo=0.5, fib_hi=1.0,
        ob_lookback=15,
        eq_tol_atr=0.15,
        require_fvg=True,
        sl_min_atr=0.8, sl_max_atr=2.0,
        max_spread_ratio=0.15, max_hold=24,
        max_consec=MAX_CONSEC_LOSSES,
        daily_target=DAILY_TARGET_PCT,
        daily_loss=DAILY_LOSS_PCT,
        sess_start=SESSION[0], sess_end=SESSION[1],
        capital=INITIAL_CAPITAL, long_only=False,
    )
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
    sh, sl = find_swings(h, l, p.swing_left, p.swing_right)
    hours = d["time"].dt.hour.values
    weekday = d["time"].dt.weekday.values
    in_sess = in_killzone(hours, weekday)
    days = d["time"].dt.normalize().values
    t = d["time"].values

    half = p.spread / 2.0
    equity = p.capital
    pos, pending = None, None
    trades = []
    curve = np.full(n, np.nan)
    daily_pnl = []

    # -------------------------------------------------------------------
    # État journalier — le % est TOUJOURS calculé sur initial_capital (fixe)
    # -------------------------------------------------------------------
    initial_capital = p.capital          # référence FIXE, jamais modifiée
    cur_day = None
    day_open_equity = equity             # equity en début de journée (pour stats)
    day_halted = False
    day_halt_reason = None
    consec = 0

    info = {"signals": 0, "skipped_spread": 0, "longs": 0, "shorts": 0,
            "rejected_fib": 0, "rejected_fvg": 0, "rejected_ob_mit": 0,
            "days_target_hit": 0, "days_loss_hit": 0, "days_consec_hit": 0}

    def close_trade(i, raw, reason):
        nonlocal equity, pos, consec, day_halted, day_halt_reason
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

        # Seuils journaliers : % du CAPITAL INITIAL (fixe)
        day_ret_on_initial = (equity - day_open_equity) / initial_capital * 100.0
        if day_ret_on_initial >= p.daily_target and not day_halted:
            day_halted = True
            day_halt_reason = "objectif +%.1f%% du capital initial atteint" % p.daily_target
            info["days_target_hit"] += 1
        elif day_ret_on_initial <= -p.daily_loss and not day_halted:
            day_halted = True
            day_halt_reason = "limite -%.1f%% du capital initial atteinte" % p.daily_loss
            info["days_loss_hit"] += 1
        elif consec >= p.max_consec and not day_halted:
            day_halted = True
            day_halt_reason = "%d pertes consécutives" % p.max_consec
            info["days_consec_hit"] += 1
        pos = None

    for i in range(n):
        # Nouveau jour -> reset
        if days[i] != cur_day:
            if cur_day is not None:
                daily_pnl.append({
                    "date": cur_day,
                    "ret_pct_on_initial": (equity - day_open_equity) / initial_capital * 100.0,
                    "ret_pct_on_dayopen": (equity - day_open_equity) / day_open_equity * 100.0,
                    "halt_reason": day_halt_reason,
                })
            cur_day = days[i]
            day_open_equity = equity
            day_halted = False
            day_halt_reason = None
            consec = 0

        # 1) entrée à l'ouverture
        if pending is not None and pos is None:
            dr, dist = pending
            if in_sess[i] and not day_halted and equity > 0:
                entry = o[i] + dr * (half + p.slippage)
                risk_money = equity * p.risk / 100.0
                pos = {"dir": dr, "entry": entry, "stop": entry - dr * dist,
                       "tp": entry + dr * dist * p.rr, "dist": dist,
                       "size": risk_money / dist, "risk_money": risk_money,
                       "entry_time": t[i], "entry_i": i}
        pending = None

        # 2) gestion du trade en cours
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

        # 3) signal
        if (pos is None and i >= start_idx and in_sess[i] and not day_halted
                and i + 1 < n and in_sess[i + 1]
                and not np.isnan(atr[i])):
            A = atr[i]
            dr = 0
            raw = 0.0

            # LONG
            leg = find_leg(h, l, sh, sl, i, direction=-1, lookback=p.leg_lookback)
            if leg is not None:
                leg_lo, leg_hi = leg[0], leg[1]
                z_lo, z_hi = fib_zone(l[leg_lo], h[leg_hi], direction=1,
                                      f_lo=p.fib_lo, f_hi=p.fib_hi)
                price_in_zone = (z_lo - p.eq_tol_atr * A) <= c[i] <= (z_hi + p.eq_tol_atr * A)
                if not price_in_zone:
                    info["rejected_fib"] += 1
                else:
                    lo0 = max(0, i - p.leg_lookback)
                    ob_hi = ob_lo = ob_idx = None
                    for k in range(i, max(lo0, i - p.ob_lookback) - 1, -1):
                        if c[k] < o[k]:
                            ob_hi, ob_lo, ob_idx = h[k], l[k], k
                            break
                    if ob_idx is None:
                        info["rejected_ob_mit"] += 1
                    else:
                        ob_in_zone = (ob_lo >= z_lo - p.eq_tol_atr * A) and (ob_hi <= z_hi + p.eq_tol_atr * A)
                        touches = sum(1 for k in range(ob_idx + 1, i)
                                      if l[k] <= ob_hi and h[k] >= ob_lo)
                        swept = l[i] < l[leg_lo]
                        rej = c[i] > o[i] and c[i] > l[leg_lo]
                        fvg_ok = (not p.require_fvg) or has_fvg(h, l, i, 1)
                        if not fvg_ok:
                            info["rejected_fvg"] += 1
                        elif ob_in_zone and touches <= 1 and swept and rej:
                            dr = 1
                            raw = c[i] - l[i]

            # SHORT
            if dr == 0 and not p.long_only:
                leg = find_leg(h, l, sh, sl, i, direction=+1, lookback=p.leg_lookback)
                if leg is not None:
                    leg_lo, leg_hi = leg[0], leg[1]
                    z_lo, z_hi = fib_zone(l[leg_lo], h[leg_hi], direction=-1,
                                          f_lo=p.fib_lo, f_hi=p.fib_hi)
                    price_in_zone = (z_lo - p.eq_tol_atr * A) <= c[i] <= (z_hi + p.eq_tol_atr * A)
                    if not price_in_zone:
                        info["rejected_fib"] += 1
                    else:
                        lo0 = max(0, i - p.leg_lookback)
                        ob_hi = ob_lo = ob_idx = None
                        for k in range(i, max(lo0, i - p.ob_lookback) - 1, -1):
                            if c[k] > o[k]:
                                ob_hi, ob_lo, ob_idx = h[k], l[k], k
                                break
                        if ob_idx is None:
                            info["rejected_ob_mit"] += 1
                        else:
                            ob_in_zone = (ob_lo >= z_lo - p.eq_tol_atr * A) and (ob_hi <= z_hi + p.eq_tol_atr * A)
                            touches = sum(1 for k in range(ob_idx + 1, i)
                                          if l[k] <= ob_hi and h[k] >= ob_lo)
                            swept = h[i] > h[leg_hi]
                            rej = c[i] < o[i] and c[i] < h[leg_hi]
                            fvg_ok = (not p.require_fvg) or has_fvg(h, l, i, -1)
                            if not fvg_ok:
                                info["rejected_fvg"] += 1
                            elif ob_in_zone and touches <= 1 and swept and rej:
                                dr = -1
                                raw = h[i] - c[i]

            if dr:
                dist = min(max(raw + 0.1 * A, p.sl_min_atr * A), p.sl_max_atr * A)
                info["signals"] += 1
                if dr == 1:
                    info["longs"] += 1
                else:
                    info["shorts"] += 1
                if p.spread > p.max_spread_ratio * dist:
                    info["skipped_spread"] += 1
                else:
                    pending = (dr, dist)

        curve[i] = equity if pos is None else equity + (c[i] - pos["entry"]) * pos["dir"] * pos["size"]

    if pos is not None:
        close_trade(n - 1, c[-1], "fin des données")
        curve[-1] = equity
    if cur_day is not None:
        daily_pnl.append({
            "date": cur_day,
            "ret_pct_on_initial": (equity - day_open_equity) / initial_capital * 100.0,
            "ret_pct_on_dayopen": (equity - day_open_equity) / day_open_equity * 100.0,
            "halt_reason": day_halt_reason,
        })
    return pd.DataFrame(trades), pd.Series(curve, index=d["time"]).ffill(), info, pd.DataFrame(daily_pnl)


# ------------------------------------------------------------------------------
# Métriques & rapport
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


def show(title, m, info, daily, bh=None):
    print(f"\n=== {title} ===", flush=True)
    if not m.get("n"):
        print("Aucun trade.", flush=True)
        return
    print(f"Jours             : {m['days']}", flush=True)
    print(f"Trades            : {m['n']} ({m['n'] / m['days']:.2f}/jour)", flush=True)
    print(f"  dont longs/shorts : {info.get('longs', 0)}/{info.get('shorts', 0)}", flush=True)
    print(f"Signaux rejetés   : {info['skipped_spread']}/{info['signals']} (spread)", flush=True)
    print(f"Rejets fib 0.5-1.0: {info.get('rejected_fib', 0)}", flush=True)
    print(f"Rejets FVG        : {info.get('rejected_fvg', 0)}", flush=True)
    print(f"Rejets OB absent/mitigé: {info.get('rejected_ob_mit', 0)}", flush=True)
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

    # === STATS JOURNALIÈRES (sur CAPITAL INITIAL) ===
    if daily is not None and len(daily):
        d = daily.copy()
        d["ret_pct_on_initial"] = d["ret_pct_on_initial"].fillna(0.0)
        win_days = (d["ret_pct_on_initial"] > 0).sum()
        loss_days = (d["ret_pct_on_initial"] < 0).sum()
        flat_days = (d["ret_pct_on_initial"] == 0).sum()
        print("\n--- STATS JOURNALIÈRES (% sur CAPITAL INITIAL) ---", flush=True)
        print(f"Jours tradés      : {len(d)}", flush=True)
        print(f"  gagnants        : {win_days} ({win_days/len(d)*100:.1f}%)", flush=True)
        print(f"  perdants        : {loss_days} ({loss_days/len(d)*100:.1f}%)", flush=True)
        print(f"  plats           : {flat_days}", flush=True)
        print(f"Meilleur jour     : {d['ret_pct_on_initial'].max():+.2f} % du capital initial", flush=True)
        print(f"Pire jour         : {d['ret_pct_on_initial'].min():+.2f} % du capital initial", flush=True)
        print(f"Jour moyen        : {d['ret_pct_on_initial'].mean():+.3f} % du capital initial", flush=True)
        print(f"Jours atteignant +{DAILY_TARGET_PCT}% : {info.get('days_target_hit', 0)}", flush=True)
        print(f"Jours atteignant -{DAILY_LOSS_PCT}% : {info.get('days_loss_hit', 0)}", flush=True)
        print(f"Jours arrêtés (consec) : {info.get('days_consec_hit', 0)}", flush=True)


def report(symbol, df, oos=0.3):
    p = make_params()
    print("\n" + "#" * 44, flush=True)
    print(f"# SMC V4 RETOURNEMENT {symbol} M5 | {len(df)} bougies", flush=True)
    print(f"# {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d %H:%M}", flush=True)
    print(f"# Objectif : +{p.daily_target}%/jour max | Perte max : -{p.daily_loss}%/jour", flush=True)
    print(f"# (calculés sur CAPITAL INITIAL {p.capital:.0f})", flush=True)
    print(f"# Risque/trade : {p.risk}% | RR : {p.rr} | Session : {p.sess_start}-{p.sess_end}h broker", flush=True)
    print("#" * 44, flush=True)
    if len(df) < 5000:
        print("Historique insuffisant (< 5000 bougies).", flush=True)
        return

    split = int(len(df) * (1 - oos))
    split_time = df["time"].iloc[split]

    tr, cv, inf, dl = run_scalp(df.iloc[:split], p)
    bh = (df["close"].iloc[split - 1] / df["close"].iloc[0] - 1) * 100
    show("ECHANTILLON (70 %)", metrics(tr, cv, p.capital), inf, dl, bh)

    tr, cv, inf, dl = run_scalp(df, p, start_idx=split)
    bh = (df["close"].iloc[-1] / df["close"].iloc[split] - 1) * 100
    show("HORS-ECHANTILLON (30 %)", metrics(tr, cv, p.capital, split_time), inf, dl, bh)

    print("\n=== SENSIBILITE AU SPREAD (net R | rend. | DD | n) ===", flush=True)
    for sp in (0.15, 0.30, 0.50, 0.80):
        tr, cv, _, _ = run_scalp(df, make_params(spread=sp))
        m = metrics(tr, cv, p.capital)
        line = (f"{m['exp']:+.2f}R {m['ret']:+.1f}% {m['dd']:.0f}% n={m['n']}"
                if m.get("n") else "aucun trade")
        print(f"spread {sp:.2f} : {line}", flush=True)

    print("\n=== ROBUSTESSE : zone Fibonacci (net R | rend. | n) ===", flush=True)
    for f_lo, f_hi in ((0.382, 0.618), (0.5, 0.618), (0.5, 0.79), (0.5, 1.0), (0.618, 1.0)):
        tr, cv, _, _ = run_scalp(df, make_params(fib_lo=f_lo, fib_hi=f_hi))
        m = metrics(tr, cv, p.capital)
        line = f"{m['exp']:+.2f}R {m['ret']:+.1f}% n={m['n']}" if m.get("n") else "-"
        print(f"Fib {f_lo}-{f_hi} : {line}", flush=True)

    print("\n=== ROBUSTESSE : RR (net R | rend. | n) ===", flush=True)
    for rr in (1.0, 1.5, 2.0, 2.5, 3.0):
        tr, cv, _, _ = run_scalp(df, make_params(rr=rr))
        m = metrics(tr, cv, p.capital)
        line = f"{m['exp']:+.2f}R {m['ret']:+.1f}% n={m['n']}" if m.get("n") else "-"
        print(f"RR {rr} : {line}", flush=True)

    print("\n=== OBJECTIF JOURNALIER (% du CAPITAL INITIAL) ===", flush=True)
    for tgt, loss in ((5, 5), (10, 10), (20, 15), (30, 15), (50, 25)):
        tr, cv, inf2, dl2 = run_scalp(df, make_params(daily_target=tgt, daily_loss=loss))
        m = metrics(tr, cv, p.capital)
        if m.get("n") and len(dl2):
            best_d = dl2["ret_pct_on_initial"].max()
            worst_d = dl2["ret_pct_on_initial"].min()
            print(f"TP +{tgt}%/SL -{loss}% : {m['exp']:+.2f}R | "
                  f"meilleur {best_d:+.1f}% pire {worst_d:+.1f}% (cap. init) | "
                  f"n={m['n']} | arrêts obj:{inf2.get('days_target_hit', 0)} "
                  f"perte:{inf2.get('days_loss_hit', 0)}", flush=True)
        else:
            print(f"TP +{tgt}%/SL -{loss}% : aucun trade", flush=True)

    print("\nLecture : l'espérance NETTE doit rester > 0 partout, "
          "et le pire jour ne doit jamais dépasser -15% du capital initial.", flush=True)


# ------------------------------------------------------------------------------
# Service Render & main
# ------------------------------------------------------------------------------
async def health_server():
    port = int(os.getenv("PORT", 10000))

    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"Backtest SMC V4 OK"
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
        raise RuntimeError("METAAPI_TOKEN et METAAPI_ACCOUNT_ID sont requis.")
    asyncio.create_task(health_server())
    ka = asyncio.create_task(keepalive())
    api = MetaApi(TOKEN, {"region": REGION})
    account = await api.metatrader_account_api.get_account(ACCOUNT_ID)
    if account.state != "DEPLOYED":
        await account.deploy()
    await account.wait_connected()
    log(f"Compte connecté. {SYMBOL} {TIMEFRAME} | {YEARS} an(s) | "
        f"session {SESSION} | spread {SPREAD} | risque {RISK_PCT}%")

    try:
        df = await fetch_history(account, SYMBOL, TIMEFRAME, YEARS)
        if df is None:
            print(f"[{SYMBOL}] Aucune bougie reçue.", flush=True)
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
