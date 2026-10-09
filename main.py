"""ARKAS MOMENTUM H1 — V9 backtest only.
No order-placement API is used. Simulations are candle-based, not tick-accurate.
Stratégie : momentum de tendance H1 sur indices (NAS100, US500, DAX...).
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
SYMBOL = os.getenv("BT_SYMBOL = US Tech 100")      # <-- US Tech 00 par défaut
YEARS = float(os.getenv("BT_YEARS", "1.0"))     # 1 an pour avoir assez de bougies H1
SPREAD = float(os.getenv("BT_SPREAD", "1.0"))   # US Tech 100 : ~1 point de spread
RISK_PCT = float(os.getenv("BT_RISK", "1.0"))
TIMEFRAME = "1h"                                 # <-- H1
INITIAL_CAPITAL = float(os.getenv("BT_CAPITAL", "10000"))


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------------------
# Historique
# ------------------------------------------------------------------------------
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
        if oldest <= target or (prev_oldest is not None and oldest >= prev_oldest) or len(rows) > 30000:
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


# ------------------------------------------------------------------------------
# Paramètres
# ------------------------------------------------------------------------------
def make_params(**over):
    p = argparse.Namespace(
        spread=SPREAD, slippage=0.5, risk=RISK_PCT, rr=2.5, atr=14,
        ema_fast=20, ema_slow=50,          # biais H1
        lookback_bars=12,                  # range de référence (12 bougies H1 = 12h)
        min_range_atr=0.5, max_range_atr=5.0,
        sl_pad_atr=0.3,                    # marge au-delà de la bougie de cassure
        sl_min_atr=1.0, sl_max_atr=3.0,    # bornes du SL
        max_hold=24,                       # 24 bougies H1 = 24h
        max_consec=2,
        max_positions=2,
        max_total_risk_pct=2.0,
        allow_same_dir=False,              # une position par sens
        max_spread_ratio=0.15,
        capital=INITIAL_CAPITAL,
        long_only=False,
        exclude_days=None,
        gap_slip_tp=0.25,                  # NAS100 : gap plus rare mais possible
        gap_slip_sl=0.25,
    )
    for k, v in over.items():
        setattr(p, k, v)
    return p


# ------------------------------------------------------------------------------
# Moteur momentum H1
# ------------------------------------------------------------------------------
def run_momentum(df, p, start_idx=0):
    d = df.reset_index(drop=True).copy()
    if len(d) < max(60, p.atr + 2):
        return pd.DataFrame(), pd.Series(dtype=float), {}, pd.DataFrame()

    o, h, l, c = (d[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    n = len(d)
    prev_close = np.r_[np.nan, c[:-1]]
    tr = np.maximum.reduce([h - l, np.abs(h - prev_close), np.abs(l - prev_close)])
    atr = pd.Series(tr).rolling(p.atr, min_periods=p.atr).mean().to_numpy()
    ema_f = pd.Series(c).ewm(span=p.ema_fast, adjust=False).mean().to_numpy()
    ema_s = pd.Series(c).ewm(span=p.ema_slow, adjust=False).mean().to_numpy()
    times = d["time"].to_numpy()
    hours = d["time"].dt.hour.to_numpy()
    weekdays = d["time"].dt.weekday.to_numpy()
    day_values = d["time"].dt.normalize().to_numpy()
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

    info = {
        "signals_long": 0, "signals_short": 0,
        "rejected_range": 0, "rejected_spread": 0,
        "rejected_max_pos": 0, "rejected_max_risk": 0,
        "rejected_same_dir": 0, "fills_stop_gap": 0, "fills_tp_gap": 0,
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
            info["days_consec_hit"] = info.get("days_consec_hit", 0) + 1

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

        # ---- Signal momentum ----
        in_session = weekdays[i] < 5
        if (not day_halted and in_session and i >= max(start_idx, p.lookback_bars)
                and i > 0 and np.isfinite(atr[i])):
            A = atr[i]
            rng_hi = h[i - p.lookback_bars:i].max()
            rng_lo = l[i - p.lookback_bars:i].min()
            rng = rng_hi - rng_lo
            valid_range = rng > 0 and p.min_range_atr * A <= rng <= p.max_range_atr * A
            if not valid_range:
                info["rejected_range"] += 1
            else:
                bull_bias = ema_f[i] > ema_s[i]
                bear_bias = ema_f[i] < ema_s[i]

                candidates = []
                # LONG : biais haussier + clôture au-dessus du range
                if bull_bias and c[i] > rng_hi:
                    candidates.append((1, c[i], l[i] - p.sl_pad_atr * A))
                # SHORT : biais baissier + clôture sous le range
                if bear_bias and c[i] < rng_lo and not p.long_only:
                    candidates.append((-1, c[i], h[i] + p.sl_pad_atr * A))

                for dr, entry_px, sl_px in candidates:
                    if len(positions) >= p.max_positions:
                        info["rejected_max_pos"] += 1
                        continue
                    dist = abs(entry_px - sl_px)
                    # Bornes ATR
                    dist = min(max(dist, p.sl_min_atr * A), p.sl_max_atr * A)
                    if dist <= 0 or not np.isfinite(dist):
                        continue
                    if p.spread > p.max_spread_ratio * dist:
                        info["rejected_spread"] += 1
                        continue
                    same_dir = [q for q in positions if q["dir"] == dr]
                    if same_dir:
                        info["rejected_same_dir"] += 1
                        continue
                    risk_money = max(equity, 0.0) * p.risk / 100.0
                    if risk_money <= 0:
                        continue
                    if open_risk_pct() + risk_money / p.capital * 100.0 > p.max_total_risk_pct:
                        info["rejected_max_risk"] += 1
                        continue
                    entry = entry_px + dr * (half_spread + slip)
                    stop = entry - dr * dist
                    tp = entry + dr * dist * p.rr
                    positions.append({
                        "dir": dr, "entry": entry, "stop": stop, "tp": tp,
                        "dist": dist, "size": risk_money / dist,
                        "risk_money": risk_money, "entry_time": times[i], "entry_i": i,
                    })
                    if dr == 1:
                        info["signals_long"] += 1
                    else:
                        info["signals_short"] += 1

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


# ------------------------------------------------------------------------------
# Métriques / rapport
# ------------------------------------------------------------------------------
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
    print(f"Longs/shorts      : {info.get('signals_long', 0)}/{info.get('signals_short', 0)}")
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
    print(f"Rejets (même sens): {info.get('rejected_same_dir', 0)}")
    print(f"Sorties           : {m.get('reasons', {})}")
    if bh is not None:
        print(f"Buy & hold (réf.) : {bh:+.1f} %")
    if show_concentration:
        cs = concentration_stats(trades, daily)
        print("--- CONCENTRATION ---")
        print(f"Top 1 trade  : {cs.get('top1_share', np.nan):.1f} % du PnL en R")
        print(f"Top 5 trades : {cs.get('top5_share', np.nan):.1f} % du PnL en R")
        print(f"Top 10 trades: {cs.get('top10_share', np.nan):.1f} % du PnL en R")
        print(f"Meilleur jour: {cs.get('best_day', 0):+.2f} %; part: {cs.get('best_day_share', np.nan):.1f} %")


def report(symbol, df, oos=0.30):
    p = make_params()
    print("\n" + "#" * 64)
    print(f"# ARKAS MOMENTUM V9 | {symbol} {TIMEFRAME} | {len(df)} bougies")
    print(f"# {df['time'].iloc[0]:%Y-%m-%d} -> {df['time'].iloc[-1]:%Y-%m-%d %H:%M}")
    print(f"# EMA {p.ema_fast}/{p.ema_slow} | range {p.lookback_bars} bars | RR {p.rr} | "
          f"spread {p.spread} | risque {p.risk}%")
    print(f"# ATTENTION: simulation OHLC, aucun ordre réel.")
    print("#" * 64)
    if len(df) < 2000:
        print("Historique insuffisant (< 2000 bougies H1).")
        return
    if not 0.1 <= oos <= 0.5:
        raise ValueError("La proportion OOS doit être entre 0.10 et 0.50.")

    bh = (df["close"].iloc[-1] / df["close"].iloc[0] - 1.0) * 100.0

    # ===== Run principal =====
    tr, cv, inf, dl = run_momentum(df, p)
    m = metrics(tr, cv, p.capital)
    show("PERIODE COMPLETE", m, inf, dl, tr, bh)

    # ===== Run sans le meilleur jour =====
    cs = concentration_stats(tr, dl)
    best_day = cs.get("best_day_date")
    if best_day is not None and pd.notna(best_day):
        excluded_day = pd.Timestamp(best_day).normalize()
        p2 = make_params(exclude_days=[excluded_day])
        tr2, cv2, inf2, dl2 = run_momentum(df, p2)
        m2 = metrics(tr2, cv2, p2.capital)
        show(f"SANS LE MEILLEUR JOUR ({excluded_day:%Y-%m-%d})", m2, inf2, dl2, tr2, bh, False)

    # ===== Sensibilité spread =====
    print("\n=== SENSIBILITE SPREAD (net R | rend.% | DD% | n) ===")
    for sp in (0.5, 1.0, 2.0, 4.0):
        tr_s, cv_s, _, _ = run_momentum(df, make_params(spread=sp))
        m_s = metrics(tr_s, cv_s, p.capital)
        if m_s.get("n"):
            print(f"spread {sp:.1f} : {m_s.get('exp', np.nan):+.3f}R | "
                  f"{m_s.get('ret', np.nan):+.1f}% | DD {m_s.get('dd', np.nan):.1f}% | n={m_s['n']}")
        else:
            print(f"spread {sp:.1f} : aucun trade")

    # ===== Robustesse paramètres =====
    print("\n=== ROBUSTESSE : RR (net R | rend.% | n) ===")
    for rr in (1.5, 2.0, 2.5, 3.0, 4.0):
        tr_r, cv_r, _, _ = run_momentum(df, make_params(rr=rr))
        m_r = metrics(tr_r, cv_r, p.capital)
        if m_r.get("n"):
            print(f"RR {rr} : {m_r.get('exp', np.nan):+.3f}R | "
                  f"{m_r.get('ret', np.nan):+.1f}% | n={m_r['n']}")

    print("\n=== ROBUSTESSE : lookback range (net R | rend.% | n) ===")
    for lb in (6, 12, 18, 24):
        tr_l, cv_l, _, _ = run_momentum(df, make_params(lookback_bars=lb))
        m_l = metrics(tr_l, cv_l, p.capital)
        if m_l.get("n"):
            print(f"LB {lb} : {m_l.get('exp', np.nan):+.3f}R | "
                  f"{m_l.get('ret', np.nan):+.1f}% | n={m_l['n']}")

    print("\n=== ROBUSTESSE : EMA (net R | rend.% | n) ===")
    for ef, es in ((10, 30), (20, 50), (30, 100)):
        tr_e, cv_e, _, _ = run_momentum(df, make_params(ema_fast=ef, ema_slow=es))
        m_e = metrics(tr_e, cv_e, p.capital)
        if m_e.get("n"):
            print(f"EMA {ef}/{es} : {m_e.get('exp', np.nan):+.3f}R | "
                  f"{m_e.get('ret', np.nan):+.1f}% | n={m_e['n']}")

    # ===== OOS chronologique =====
    split = int(len(df) * (1.0 - oos))
    train_df = df.iloc[:split].reset_index(drop=True)
    test_df = df.iloc[split:].reset_index(drop=True)
    print(f"\n=== VALIDATION OOS CHRONOLOGIQUE ({100*oos:.0f}% final) ===")
    for label, part in (("TRAIN", train_df), ("OOS", test_df)):
        tr_v, cv_v, inf_v, dl_v = run_momentum(part, p)
        m_v = metrics(tr_v, cv_v, p.capital)
        if m_v.get("n"):
            print(f"{label:5s}: trades={m_v['n']:4d} | exp={m_v.get('exp', np.nan):+.3f}R | "
                  f"PF={m_v.get('pf', np.nan):.2f} | ret={m_v.get('ret', np.nan):+.1f}% | "
                  f"DD={m_v.get('dd', np.nan):.1f}%")
        else:
            print(f"{label:5s}: aucun trade exploitable")

    print("\nLecture : viser espérance > +0,15 R sur les deux segments "
          "ET stabilité sur les paramètres. Un edge momentum H1 doit apparaître sur train ET OOS.")


# ------------------------------------------------------------------------------
# Service & main
# ------------------------------------------------------------------------------
async def health_server():
    port = int(os.getenv("PORT", "10000"))
    async def handle(reader, writer):
        try:
            await reader.read(1024)
            body = b"ARKAS MOMENTUM V9 backtest service OK - no live orders"
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
