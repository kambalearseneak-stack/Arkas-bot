"""
Backtest ORB XAUUSD (M5) — 100 % MetaApi — rapport dans les logs, AUCUN ordre passé.

Stratégie : ORB (Opening Range Breakout) — VERSION MULTI-POSITIONS
  - Range d'ouverture sur N minutes au début d'une session
  - Achat si cassure haut, vente si cassure bas
  - SL = milieu du range (ou côté opposé) ; TP = RR x range
  - Filtre volatilité : range ∈ [min_range_atr ; max_range_atr] x ATR
  - PLUSIEURS positions simultanées possibles, sous plafond de risque

Garde-fous :
  - max_positions (défaut 3)
  - max_total_risk_pct (défaut 3.0 % du capital)
  - max_consec pertes (défaut 2) -> arrêt du jour
  - PAS de seuil en % de capital

Env :
  BT_SYMBOL, BT_YEARS, BT_SPREAD, BT_RISK
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
        # ORB
        sess_start=8, sess_end=16,
        orb_minutes=30,
        min_range_atr=0.3, max_range_atr=3.0,
        sl_mode="mid",
        # Gestion
        max_hold=96,
        max_consec=2,
        # MULTI-POSITIONS
        max_positions=3,             # nombre max de positions simultanées
        max_total_risk_pct=3.0,      # risque cumulé max en % du capital
        allow_same_dir=True,         # autoriser 2 longs simultanés ?
        min_price_gap_atr=0.5,       # écart min entre 2 entrées même sens (en ATR)
        # Exécution
        max_spread_ratio=0.15,
        capital=INITIAL_CAPITAL, long_only=False,
    )
    for k, v in over.items():
        setattr(p, k, v)
    return p


# ------------------------------------------------------------------------------
# Moteur ORB multi-positions
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
    positions = []           # liste de positions ouvertes
    trades = []
    curve = np.full(n, np.nan)
    daily_pnl = []

    # État journalier
    cur_day = None
    day_open_equity = equity
    day_halted = False
    day_halt_reason = None
    consec = 0
    # ORB
    orb_high = None
    orb_low = None
    orb_locked = False

    info = {"orb_days": 0, "orb_skipped_range": 0, "orb_skipped_spread": 0,
            "longs": 0, "shorts": 0, "days_consec_hit": 0,
            "signals_rejected_max_pos": 0, "signals_rejected_max_risk": 0,
            "signals_rejected_same_dir": 0}

    def current_risk_pct():
        """Risque cumulé en % du capital initial."""
        return sum(pos["risk_money"] for pos in positions) / INITIAL_CAPITAL * 100.0

    def close_position(idx, i, raw, reason):
        """Ferme la position positions[idx]."""
        nonlocal equity, consec, day_halted, day_halt_reason
        pos = positions.pop(idx)
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
        if consec >= p.max_consec and not day_halted:
            day_halted = True
            day_halt_reason = "%d pertes consécutives" % p.max_consec
            info["days_consec_hit"] += 1

    for i in range(n):
        # ------------------------------------------------------------------
        # Nouveau jour -> reset
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # Construction du range d'ouverture
        # ------------------------------------------------------------------
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

        # ------------------------------------------------------------------
        # Gestion de TOUTES les positions ouvertes (SL/TP/timeout)
        # ------------------------------------------------------------------
        still_open = []
        for pos in positions:
            closed = False
            if pos["dir"] == 1:
                if o[i] <= pos["stop"]:
                    close_position(positions.index(pos), i, o[i], "stop (gap)")
                    closed = True
                elif o[i] >= pos["tp"]:
                    close_position(positions.index(pos), i, o[i], "objectif (gap)")
                    closed = True
                elif l[i] <= pos["stop"]:
                    close_position(positions.index(pos), i, pos["stop"], "stop")
                    closed = True
                elif h[i] >= pos["tp"]:
                    close_position(positions.index(pos), i, pos["tp"], "objectif")
                    closed = True
            else:
                if o[i] >= pos["stop"]:
                    close_position(positions.index(pos), i, o[i], "stop (gap)")
                    closed = True
                elif o[i] <= pos["tp"]:
                    close_position(positions.index(pos), i, o[i], "objectif (gap)")
                    closed = True
                elif h[i] >= pos["stop"]:
                    close_position(positions.index(pos), i, pos["stop"], "stop")
                    closed = True
                elif l[i] <= pos["tp"]:
                    close_position(positions.index(pos), i, pos["tp"], "objectif")
                    closed = True
            if not closed:
                # timeout
                if i - pos["entry_i"] + 1 >= p.max_hold:
                    close_position(positions.index(pos), i, c[i], "temps")
                # fin de session
                elif i + 1 >= n or not (
                        (hours[i + 1] >= p.sess_start) and (hours[i + 1] < p.sess_end)
                        and weekday[i + 1] < 5):
                    close_position(positions.index(pos), i, c[i], "fin de session")

        # ------------------------------------------------------------------
        # Signaux : on peut ouvrir PLUSIEURS positions dans la même bougie
        # ------------------------------------------------------------------
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
                        # ---- plafond du nombre de positions
                        if len(positions) >= p.max_positions:
                            info["signals_rejected_max_pos"] += 1
                            continue
                        dist = abs(entry_px - sl_px)
                        if dist <= 0:
                            continue
                        # ---- filtre spread
                        if p.spread > p.max_spread_ratio * dist:
                            info["orb_skipped_spread"] += 1
                            continue
                        # ---- filtre même sens : empilement interdit
                        same_dir = [q for q in positions if q["dir"] == dr]
                        if same_dir:
                            if not p.allow_same_dir:
                                info["signals_rejected_same_dir"] += 1
                                continue
                            # vérifie l'écart de prix minimum
                            too_close = any(
                                abs(entry_px - q["entry"]) < p.min_price_gap_atr * A
                                for q in same_dir
                            )
                            if too_close:
                                info["signals_rejected_same_dir"] += 1
                                continue
                        # ---- plafond du risque cumulé
                        risk_money = equity * p.risk / 100.0
                        new_total = current_risk_pct() + (risk_money / INITIAL_CAPITAL * 100.0)
                        if new_total > p.max_total_risk_pct:
                            info["signals_rejected_max_risk"] += 1
                            continue
                        # ---- OUVRIR LA POSITION
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

        # ------------------------------------------------------------------
        # Equity curve : on valorise toutes les positions ouvertes
        # ------------------------------------------------------------------
        open_pnl = sum((c[i] - pos["entry"]) * pos["dir"] * pos["size"] for pos in positions)
        curve[i] = equity + open_pnl

    # Fermeture forcée en fin de données
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
# Métriques et rapport (identiques)
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
    print(f"Jours ORB valides : {info.get('orb_days', 0)}", flush=True)
    print(f"Rejets (range ATR): {info.get('orb_skipped_range', 0)}", flush=True)
    print(f"Rejets (spread)   : {info.get('orb_skipped_spread', 0)}", flush=True)
    print(f"Rejets (max pos)  : {info.get('signals_rejected_max_pos', 0)}", flush=True)
    print(f"Rejets (max risk) : {info.get('signals_rejected_max_risk', 0)}", flush=True)
    print(f"Rejets (même sens): {info.get('signals_rejected_same_dir', 0)}", flush=True)
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

    if daily is not None and len(daily):
        d = daily.copy()
        d["ret_pct"] = d["ret_pct"].fillna(0.0)
        win_days = (d["ret_pct"] > 0).sum()
        loss_days = (d["ret_pct"] < 0).sum()
        flat_days = (d["ret_pct"] == 0).sum()
        print("\n--- STATS JOURNALIÈRES (% sur capital initial) ---", flush=True)
        print(f"Jours tradés      : {len(d)}", flush=True)
        print(f"  gagnants        : {win_days} ({win_days/len(d)*100:.1f}%)", flush=True)
        print(f"  perdants        : {loss_days} ({loss_days/len(d)*100:.1f}%)", flush=True)
        print(f"  plats           : {flat_days}", flush=True)
        print(f"Meilleur jour     : {d['ret_pct'].max():+.2f} %", flush=True)
        print(f"Pire jour         : {d['ret_pct'].min():+.2f} %", flush=True)
        print(f"Jour moyen        : {d['ret_pct'].mean():+.3f} %", flush=True)
        print(f"Jours arrêtés (consec) : {info.get('days_consec_hit', 0)}", flush=True)


def report(symbol, df, oos=0.3):
    p = make_params()
    print("\n" + "#" * 44, flush=True)
    print(f"# ORB MULTI-POS {symbol} M5 | {len(df)} bougies", flush=True)
    print(f"# {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d %H:%M}", flush=True)
    print(f"# Session {p.sess_start}h-{p.sess_end}h | ORB {p.orb_minutes} min | "
          f"RR {p.rr} | spread {p.spread} | risque/trade {p.risk}%", flush=True)
    print(f"# Max positions : {p.max_positions} | Risque total max : {p.max_total_risk_pct}%", flush=True)
    print(f"# Même sens autorisé : {p.allow_same_dir} | gap min : {p.min_price_gap_atr} ATR", flush=True)
    print("#" * 44, flush=True)
    if len(df) < 5000:
        print("Historique insuffisant (< 5000 bougies).", flush=True)
        return

    split = int(len(df) * (1 - oos))
    split_time = df["time"].iloc[split]

    tr, cv, inf, dl = run_orb(df.iloc[:split], p)
    bh = (df["close"].iloc[split - 1] / df["close"].iloc[0] - 1) * 100
    show("ECHANTILLON (70 %)", metrics(tr, cv, p.capital), inf, dl, bh)

    tr, cv, inf, dl = run_orb(df, p, start_idx=split)
    bh = (df["close"].iloc[-1] / df["close"].iloc[split] - 1) * 100
    show("HORS-ECHANTILLON (30 %)", metrics(tr, cv, p.capital, split_time), inf, dl, bh)

    print("\n=== SENSIBILITE AU SPREAD (net R | rend. | DD | n) ===", flush=True)
    for sp in (0.15, 0.30, 0.50, 0.80):
        tr, cv, _, _ = run_orb(df, make_params(spread=sp))
        m = metrics(tr, cv, p.capital)
        line = (f"{m['exp']:+.2f}R {m['ret']:+.1f}% {m['dd']:.0f}% n={m['n']}"
                if m.get("n") else "aucun trade")
        print(f"spread {sp:.2f} : {line}", flush=True)

    print("\n=== ROBUSTESSE : max_positions (net R | rend. | DD | n) ===", flush=True)
    for mp in (1, 2, 3, 5, 10):
        tr, cv, _, _ = run_orb(df, make_params(max_positions=mp))
        m = metrics(tr, cv, p.capital)
        line = f"{m['exp']:+.2f}R {m['ret']:+.1f}% DD{m['dd']:.0f}% n={m['n']}" if m.get("n") else "-"
        print(f"max_pos {mp} : {line}", flush=True)

    print("\n=== ROBUSTESSE : risque total max (net R | rend. | DD | n) ===", flush=True)
    for rp in (1.0, 2.0, 3.0, 5.0, 10.0):
        tr, cv, _, _ = run_orb(df, make_params(max_total_risk_pct=rp))
        m = metrics(tr, cv, p.capital)
        line = f"{m['exp']:+.2f}R {m['ret']:+.1f}% DD{m['dd']:.0f}% n={m['n']}" if m.get("n") else "-"
        print(f"risque total {rp}% : {line}", flush=True)

    print("\n=== ROBUSTESSE : durée ORB (net R | rend. | n) ===", flush=True)
    for om in (15, 30, 45, 60):
        tr, cv, _, _ = run_orb(df, make_params(orb_minutes=om))
        m = metrics(tr, cv, p.capital)
        line = f"{m['exp']:+.2f}R {m['ret']:+.1f}% n={m['n']}" if m.get("n") else "-"
        print(f"ORB {om} min : {line}", flush=True)

    print("\n=== ROBUSTESSE : RR (net R | rend. | n) ===", flush=True)
    for rr in (1.0, 1.5, 2.0, 2.5, 3.0):
        tr, cv, _, _ = run_orb(df, make_params(rr=rr))
        m = metrics(tr, cv, p.capital)
        line = f"{m['exp']:+.2f}R {m['ret']:+.1f}% n={m['n']}" if m.get("n") else "-"
        print(f"RR {rr} : {line}", flush=True)

    print("\n=== ROBUSTESSE : SL mode (net R | rend. | n) ===", flush=True)
    for slm in ("mid", "opposite"):
        tr, cv, _, _ = run_orb(df, make_params(sl_mode=slm))
        m = metrics(tr, cv, p.capital)
        line = f"{m['exp']:+.2f}R {m['ret']:+.1f}% n={m['n']}" if m.get("n") else "-"
        print(f"SL {slm} : {line}", flush=True)

    print("\n=== ROBUSTESSE : session (net R | rend. | n) ===", flush=True)
    for ss, se in ((8, 11), (13, 16), (8, 16), (14, 20)):
        tr, cv, _, _ = run_orb(df, make_params(sess_start=ss, sess_end=se))
        m = metrics(tr, cv, p.capital)
        line = f"{m['exp']:+.2f}R {m['ret']:+.1f}% n={m['n']}" if m.get("n") else "-"
        print(f"session {ss}h-{se}h : {line}", flush=True)

    print("\nLecture : l'espérance NETTE doit rester > 0 partout. "
          "Surveiller le DD quand max_positions augmente.", flush=True)


# ------------------------------------------------------------------------------
# Service & main
# ------------------------------------------------------------------------------
async def health_server():
    port = int(os.getenv("PORT", 10000))

    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"Backtest ORB MP OK"
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
        f"spread {SPREAD} | risque {RISK_PCT}%")

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
