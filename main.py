"""
Backtest ORB XAUUSD M5 — V7 — 100 % MetaApi — AUCUN ordre passé.

V7 : Slippage réaliste sur les fills gap.
  - TP gap : tu obtiens tp - gap_slip_tp * dist (au lieu du TP plein)
  - SL gap : tu obtiens sl - gap_slip_sl * dist (au lieu du SL plein)
  - Test automatique sur 3 niveaux de slippage gap (faible / moyen / fort)

Tout le reste (ORB multi-pos, fills réalistes V6, concentration, run sans meilleur jour)
est conservé.
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

INITIAL_CAPITAL = 10000.0


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
# Paramètres
# ------------------------------------------------------------------------------
def make_params(**over):
    p = argparse.Namespace(
        spread=SPREAD, slippage=0.05, risk=RISK_PCT, rr=2.0, atr=14,
        sess_start=8, sess_end=16,
        orb_minutes=30,
        min_range_atr=0.3, max_range_atr=3.0,
        sl_mode="mid",
        max_hold=96,
        max_consec=2,
        max_positions=3,
        max_total_risk_pct=3.0,
        allow_same_dir=True,
        min_price_gap_atr=0.5,
        max_spread_ratio=0.15,
        capital=INITIAL_CAPITAL, long_only=False,
        exclude_days=None,
        # --- V7 : slippage sur fills gap ---
        gap_slip_tp=0.0,   # fraction de dist perdue sur un TP gap
        gap_slip_sl=0.0,   # fraction de dist perdue EN PLUS sur un SL gap
    )
    for k, v in over.items():
        setattr(p, k, v)
    return p


# ------------------------------------------------------------------------------
# Moteur ORB V7
# ------------------------------------------------------------------------------
def run_orb(df, p, start_idx=0):
    d = df.reset_index(drop=True)
    o, h, l, c = (d[k].values.astype(float) for k in ("open", "high", "low", "close"))
    n = len(d)
    pc = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - pc), np.abs(l - pc)])
    atr = pd.Series(tr).rolling(p.atr).mean().values
    t = d["time"].values
    hours = d["time"].dt.hour.values
    minutes = d["time"].dt.minute.values
    days = d["time"].dt.normalize().values
    weekday = d["time"].dt.weekday.values

    half = p.spread / 2.0
    equity = p.capital
    positions = []
    trades = []
    curve = np.full(n, np.nan)
    daily_pnl = []

    excluded_set = set()
    if p.exclude_days:
        excluded_set = set(np.datetime64(x, "D") for x in p.exclude_days)

    cur_day = None
    day_open_equity = equity
    day_halted = False
    day_halt_reason = None
    consec = 0
    orb_high = None
    orb_low = None
    orb_locked = False

    info = {"orb_days": 0, "orb_skipped_range": 0, "orb_skipped_spread": 0,
            "longs": 0, "shorts": 0, "days_consec_hit": 0,
            "signals_rejected_max_pos": 0, "signals_rejected_max_risk": 0,
            "signals_rejected_same_dir": 0,
            "fills_stop_gap": 0, "fills_tp_gap": 0}

    def current_risk_pct():
        return sum(pos["risk_money"] for pos in positions) / INITIAL_CAPITAL * 100.0

    def close_position(idx, i, raw, reason, is_gap=False, gap_kind=None, pos_ref=None):
        """
        gap_kind : None | "tp" | "sl"
        Applique le slippage de gap sur le prix de fill si demandé.
        """
        nonlocal equity, consec, day_halted, day_halt_reason
        pos = positions.pop(idx)
        dr = pos["dir"]
        # Application du slippage gap : on dégrade le fill au détriment du trader
        if is_gap and gap_kind == "tp" and p.gap_slip_tp > 0:
            raw = raw - dr * p.gap_slip_tp * pos["dist"]
        if is_gap and gap_kind == "sl" and p.gap_slip_sl > 0:
            raw = raw - dr * p.gap_slip_sl * pos["dist"]
        fill = raw - dr * (half + p.slippage)
        pnl = (fill - pos["entry"]) * dr * pos["size"]
        equity += pnl
        trades.append({"entry_time": pos["entry_time"], "exit_time": t[i],
                       "dir": "LONG" if dr == 1 else "SHORT", "pnl": pnl,
                       "R": pnl / pos["risk_money"],
                       "cost_R": (p.spread + 2 * p.slippage) / pos["dist"],
                       "bars": i - pos["entry_i"] + 1, "reason": reason,
                       "is_gap": is_gap})
        consec = consec + 1 if pnl <= 0 else 0
        if consec >= p.max_consec and not day_halted:
            day_halted = True
            day_halt_reason = "%d pertes consécutives" % p.max_consec
            info["days_consec_hit"] += 1

    for i in range(n):
        day_key = np.datetime64(days[i], "D")

        if days[i] != cur_day:
            if cur_day is not None:
                daily_pnl.append({
                    "date": cur_day,
                    "ret_pct": (equity - day_open_equity) / INITIAL_CAPITAL * 100.0,
                    "halt_reason": day_halt_reason,
                })
            cur_day = days[i]
            day_open_equity = equity
            day_halted = False
            day_halt_reason = None
            consec = 0
            orb_high = None
            orb_low = None
            orb_locked = False
            if day_key in excluded_set:
                day_halted = True
                day_halt_reason = "jour exclu"

        # Construction du range
        if weekday[i] < 5 and not orb_locked:
            h_ok = hours[i] >= p.sess_start
            total_min = p.sess_start * 60 + p.orb_minutes
            end_h = total_min // 60
            end_m = total_min % 60
            in_orb_window = (hours[i] < end_h) or (hours[i] == end_h and minutes[i] < end_m)
            if h_ok and in_orb_window:
                if orb_high is None:
                    orb_high = h[i]
                    orb_low = l[i]
                else:
                    orb_high = max(orb_high, h[i])
                    orb_low = min(orb_low, l[i])
            elif h_ok and not in_orb_window and orb_high is not None:
                orb_locked = True
                info["orb_days"] += 1

        # Gestion des positions avec fills V7
        for pos in list(positions):
            if pos not in positions:
                continue
            closed = False
            idx = positions.index(pos)

            if pos["dir"] == 1:
                if o[i] <= pos["stop"]:
                    close_position(idx, i, pos["stop"], "stop (gap)",
                                   is_gap=True, gap_kind="sl")
                    info["fills_stop_gap"] += 1
                    closed = True
                elif o[i] >= pos["tp"]:
                    close_position(idx, i, pos["tp"], "objectif (gap)",
                                   is_gap=True, gap_kind="tp")
                    info["fills_tp_gap"] += 1
                    closed = True
                elif l[i] <= pos["stop"]:
                    close_position(idx, i, pos["stop"], "stop")
                    closed = True
                elif h[i] >= pos["tp"]:
                    close_position(idx, i, pos["tp"], "objectif")
                    closed = True
            else:
                if o[i] >= pos["stop"]:
                    close_position(idx, i, pos["stop"], "stop (gap)",
                                   is_gap=True, gap_kind="sl")
                    info["fills_stop_gap"] += 1
                    closed = True
                elif o[i] <= pos["tp"]:
                    close_position(idx, i, pos["tp"], "objectif (gap)",
                                   is_gap=True, gap_kind="tp")
                    info["fills_tp_gap"] += 1
                    closed = True
                elif h[i] >= pos["stop"]:
                    close_position(idx, i, pos["stop"], "stop")
                    closed = True
                elif l[i] <= pos["tp"]:
                    close_position(idx, i, pos["tp"], "objectif")
                    closed = True

            if not closed and pos in positions:
                if i - pos["entry_i"] + 1 >= p.max_hold:
                    close_position(positions.index(pos), i, c[i], "temps")
                elif i + 1 >= n or not (
                        (hours[i + 1] >= p.sess_start) and (hours[i + 1] < p.sess_end)
                        and weekday[i + 1] < 5):
                    close_position(positions.index(pos), i, c[i], "fin de session")

        # Signaux
        in_session = (hours[i] >= p.sess_start) and (hours[i] < p.sess_end) and weekday[i] < 5
        if (not day_halted and orb_locked and orb_high is not None
                and in_session and i >= start_idx):
            rng = orb_high - orb_low
            if rng > 0 and not np.isnan(atr[i]):
                A = atr[i]
                valid_range = (p.min_range_atr * A) <= rng <= (p.max_range_atr * A)
                if valid_range:
                    if p.sl_mode == "mid":
                        mid = (orb_high + orb_low) / 2.0
                        sl_long = mid
                        sl_short = mid
                    else:
                        sl_long = orb_low
                        sl_short = orb_high
                    long_break = h[i] >= orb_high
                    short_break = l[i] <= orb_low
                    candidates = []
                    if long_break and not p.long_only:
                        candidates.append((1, orb_high, sl_long))
                    if short_break and not p.long_only:
                        candidates.append((-1, orb_low, sl_short))
                    for dr, entry_px, sl_px in candidates:
                        if len(positions) >= p.max_positions:
                            info["signals_rejected_max_pos"] += 1
                            continue
                        dist = abs(entry_px - sl_px)
                        if dist <= 0:
                            continue
                        if p.spread > p.max_spread_ratio * dist:
                            info["orb_skipped_spread"] += 1
                            continue
                        same_dir = [q for q in positions if q["dir"] == dr]
                        if same_dir:
                            if not p.allow_same_dir:
                                info["signals_rejected_same_dir"] += 1
                                continue
                            too_close = any(
                                abs(entry_px - q["entry"]) < p.min_price_gap_atr * A
                                for q in same_dir
                            )
                            if too_close:
                                info["signals_rejected_same_dir"] += 1
                                continue
                        risk_money = equity * p.risk / 100.0
                        new_total = current_risk_pct() + (risk_money / INITIAL_CAPITAL * 100.0)
                        if new_total > p.max_total_risk_pct:
                            info["signals_rejected_max_risk"] += 1
                            continue
                        entry = entry_px + dr * (half + p.slippage)
                        pos = {"dir": dr, "entry": entry,
                               "stop": entry - dr * dist,
                               "tp": entry + dr * dist * p.rr,
                               "dist": dist,
                               "size": risk_money / dist,
                               "risk_money": risk_money,
                               "entry_time": t[i], "entry_i": i}
                        positions.append(pos)
                        if dr == 1:
                            info["longs"] += 1
                        else:
                            info["shorts"] += 1
                else:
                    info["orb_skipped_range"] += 1

        open_pnl = sum((c[i] - pos["entry"]) * pos["dir"] * pos["size"] for pos in positions)
        curve[i] = equity + open_pnl

    while positions:
        close_position(0, n - 1, c[-1], "fin des données")
    curve[-1] = equity

    if cur_day is not None:
        daily_pnl.append({
            "date": cur_day,
            "ret_pct": (equity - day_open_equity) / INITIAL_CAPITAL * 100.0,
            "halt_reason": day_halt_reason,
        })
    return pd.DataFrame(trades), pd.Series(curve, index=d["time"]).ffill(), info, pd.DataFrame(daily_pnl)


# ------------------------------------------------------------------------------
# Métriques / rapport
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


def concentration_stats(trades, daily):
    out = {}
    if not len(trades):
        return out
    r = trades["R"].sort_values(ascending=False)
    total_r = r.sum()
    if total_r > 0:
        out["top1_share"] = r.iloc[0] / total_r * 100
        out["top5_share"] = r.iloc[:5].sum() / total_r * 100
        out["top10_share"] = r.iloc[:10].sum() / total_r * 100
    else:
        out["top1_share"] = out["top5_share"] = out["top10_share"] = float("nan")
    out["top1_R"] = r.iloc[0] if len(r) else 0
    if daily is not None and len(daily):
        d = daily.copy()
        d["ret_pct"] = d["ret_pct"].fillna(0.0)
        total_ret = d["ret_pct"].sum()
        if abs(total_ret) > 1e-9:
            out["best_day_share"] = d["ret_pct"].max() / total_ret * 100
        else:
            out["best_day_share"] = float("nan")
        out["best_day"] = d["ret_pct"].max()
        out["best_day_date"] = d.loc[d["ret_pct"].idxmax(), "date"] if len(d) else None
    return out


def show(title, m, info, daily, trades, bh=None, show_concentration=True):
    print(f"\n=== {title} ===", flush=True)
    if not m.get("n"):
        print("Aucun trade.", flush=True)
        return
    print(f"Jours             : {m['days']}", flush=True)
    print(f"Trades            : {m['n']} ({m['n'] / m['days']:.2f}/jour)", flush=True)
    print(f"  dont longs/shorts : {info.get('longs', 0)}/{info.get('shorts', 0)}", flush=True)
    print(f"Réussite          : {m['win']:.1f} %", flush=True)
    print(f"Espérance NETTE   : {m['exp']:+.3f} R/trade", flush=True)
    print(f"Avant coûts       : {m['exp_gross']:+.3f} R/trade", flush=True)
    print(f"Profit factor     : {m['pf']:.2f}", flush=True)
    print(f"Rendement         : {m['ret']:+.1f} %", flush=True)
    print(f"Drawdown max      : {m['dd']:.1f} %", flush=True)
    print(f"Pires pertes suite: {m['streak']}", flush=True)
    print(f"Fills gap SL/TP   : {info.get('fills_stop_gap', 0)}/{info.get('fills_tp_gap', 0)}", flush=True)
    print(f"Sorties           : {m['reasons']}", flush=True)
    if bh is not None:
        print(f"Buy & hold (réf.) : {bh:+.1f} %", flush=True)
    if show_concentration:
        cs = concentration_stats(trades, daily)
        print("\n--- CONCENTRATION ---", flush=True)
        print(f"Top 1 trade  : {cs.get('top1_share', 0):.1f} % du PnL", flush=True)
        print(f"Top 5 trades : {cs.get('top5_share', 0):.1f} % du PnL", flush=True)
        print(f"Top 10 trades: {cs.get('top10_share', 0):.1f} % du PnL", flush=True)
        print(f"Meilleur jour : {cs.get('best_day', 0):+.2f} % -> "
              f"{cs.get('best_day_share', 0):.1f} % du PnL total", flush=True)


def report(symbol, df, oos=0.3):
    p = make_params()
    print("\n" + "#" * 44, flush=True)
    print(f"# ORB V7 SLIPPAGE GAP {symbol} M5 | {len(df)} bougies", flush=True)
    print(f"# {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d %H:%M}", flush=True)
    print(f"# Base : ORB {p.orb_minutes}min | RR {p.rr} | session {p.sess_start}-{p.sess_end}h | "
          f"spread {p.spread} | risque {p.risk}%/trade", flush=True)
    print("#" * 44, flush=True)
    if len(df) < 5000:
        print("Historique insuffisant.", flush=True)
        return

    bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100

    # ============================================================
    # COMPARAISON : 4 niveaux de slippage gap TP (SL slippage = 0)
    # ============================================================
    print("\n" + "=" * 60, flush=True)
    print("TEST A : Impact du slippage TP gap (SL gap slippage = 0)", flush=True)
    print("=" * 60, flush=True)
    print(f"{'gap_slip_tp':>12} | {'esp.R':>7} | {'rend.%':>8} | {'DD%':>6} | "
          f"{'n':>5} | {'meil.jour%':>10} | {'top5%':>6}", flush=True)
    print("-" * 75, flush=True)
    for gs in (0.0, 0.25, 0.5, 1.0):
        tr, cv, inf, dl = run_orb(df, make_params(gap_slip_tp=gs))
        m = metrics(tr, cv, p.capital)
        cs = concentration_stats(tr, dl)
        if m.get("n"):
            line = (f"{gs:>12.2f} | {m['exp']:>+7.3f} | {m['ret']:>+8.1f} | "
                    f"{m['dd']:>6.1f} | {m['n']:>5} | "
                    f"{cs.get('best_day', 0):>+10.2f} | {cs.get('top5_share', 0):>6.1f}")
        else:
            line = f"{gs:>12.2f} | aucun trade"
        print(line, flush=True)

    # ============================================================
    # COMPARAISON : 4 niveaux de slippage SL gap (TP slippage = 0.5)
    # ============================================================
    print("\n" + "=" * 60, flush=True)
    print("TEST B : Impact du slippage SL gap (TP gap slippage fixé à 0.5)", flush=True)
    print("=" * 60, flush=True)
    print(f"{'gap_slip_sl':>12} | {'esp.R':>7} | {'rend.%':>8} | {'DD%':>6} | {'n':>5}", flush=True)
    print("-" * 55, flush=True)
    for gsl in (0.0, 0.25, 0.5, 1.0):
        tr, cv, inf, dl = run_orb(df, make_params(gap_slip_tp=0.5, gap_slip_sl=gsl))
        m = metrics(tr, cv, p.capital)
        if m.get("n"):
            line = (f"{gsl:>12.2f} | {m['exp']:>+7.3f} | {m['ret']:>+8.1f} | "
                    f"{m['dd']:>6.1f} | {m['n']:>5}")
        else:
            line = f"{gsl:>12.2f} | aucun trade"
        print(line, flush=True)

    # ============================================================
    # COMBINAISON : cas réaliste "pessimiste"
    # ============================================================
    print("\n" + "=" * 60, flush=True)
    print("TEST C : Scénario réaliste pessimiste (TP slip 0.5, SL slip 0.5)", flush=True)
    print("=" * 60, flush=True)
    tr_pe, cv_pe, inf_pe, dl_pe = run_orb(df, make_params(gap_slip_tp=0.5, gap_slip_sl=0.5))
    show("REALISTE PESSIMISTE", metrics(tr_pe, cv_pe, p.capital), inf_pe, dl_pe, tr_pe, bh)

    # Run sans le meilleur jour sur le scénario pessimiste
    cs = concentration_stats(tr_pe, dl_pe)
    best_day = cs.get("best_day_date")
    if best_day is not None:
        bd = np.datetime64(pd.Timestamp(best_day).normalize().date(), "D")
        tr_pe2, cv_pe2, inf_pe2, dl_pe2 = run_orb(
            df, make_params(gap_slip_tp=0.5, gap_slip_sl=0.5, exclude_days=[bd]))
        show(f"REALISTE PESSIMISTE sans le meilleur jour ({pd.Timestamp(best_day):%Y-%m-%d})",
             metrics(tr_pe2, cv_pe2, p.capital), inf_pe2, dl_pe2, tr_pe2, bh,
             show_concentration=False)

    # ============================================================
    # COMBINAISON : cas "très pessimiste"
    # ============================================================
    print("\n" + "=" * 60, flush=True)
    print("TEST D : Scénario très pessimiste (TP slip 1.0, SL slip 1.0)", flush=True)
    print("=" * 60, flush=True)
    tr_tp, cv_tp, inf_tp, dl_tp = run_orb(df, make_params(gap_slip_tp=1.0, gap_slip_sl=1.0))
    show("TRES PESSIMISTE", metrics(tr_tp, cv_tp, p.capital), inf_tp, dl_tp, tr_tp, bh,
         show_concentration=False)

    print("\nLecture : si l'espérance reste > +0,3 R même en scénario très pessimiste, "
          "l'edge est réel. Sinon, c'est un artefact de fills.", flush=True)


# ------------------------------------------------------------------------------
# Service & main
# ------------------------------------------------------------------------------
async def health_server():
    port = int(os.getenv("PORT", 10000))

    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"Backtest ORB V7 OK"
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
    log(f"Compte connecté. {SYMBOL} {TIMEFRAME} | {YEARS} an(s)")

    try:
        df = await fetch_history(account, SYMBOL, TIMEFRAME, YEARS)
        if df is None:
            print(f"[{SYMBOL}] Aucune bougie reçue.", flush=True)
        else:
            report(SYMBOL, df)
    except Exception as e:
        print(f"[{SYMBOL}] Erreur : {e}", flush=True)

    ka.cancel()
    print("\nTERMINE. Aucun ordre n'a été passé.", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
