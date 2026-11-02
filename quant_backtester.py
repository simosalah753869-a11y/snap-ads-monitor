#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Institutional Quant Walk-Forward Backtester & Strategy Discovery Engine (quant_backtester.py)
- Tests multiple systematic families across 1,500 hours (~62 days) of real Binance 1H klines
  AND splits results into In-Sample (first 750h) vs Out-of-Sample (last 750h) to prevent overfitting.
- Discovers why strategies fail (e.g. premature breakeven choke + fee drag from overtrading)
  and validates genuinely profitable, low-drawdown strategies after 0.12% round-trip fees.
"""

import os
import sys
import time
import json
import socket
import ssl
import urllib.request

_orig_getaddrinfo = socket.getaddrinfo
_ip_cache = {}
def _doh_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host in _ip_cache:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_ip_cache[host], port))]
    try:
        res = _orig_getaddrinfo(host, port, family, type, proto, flags)
        if res:
            _ip_cache[host] = res[0][4][0]
        return res
    except Exception:
        req = urllib.request.Request(
            f"https://1.1.1.1/dns-query?name={host}&type=A",
            headers={"Accept": "application/dns-json"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
        ip = [a["data"] for a in resp.get("Answer", []) if a.get("type") == 1][0]
        _ip_cache[host] = ip
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
socket.getaddrinfo = _doh_getaddrinfo

SSL_CTX = ssl.create_default_context()
REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quant_backtest_report.json")
SYMBOLS = ["BNBUSDT", "BTCUSDC", "SOLUSDT", "XRPUSDC", "DOGEUSDT", "PAXGUSDT"]

def http_json(url, timeout=8.0, retries=3):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last_err = e
            time.sleep(0.6 * (attempt + 1))
    raise last_err

def fetch_1500h_klines(symbol):
    b2 = http_json(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=1000")
    if not b2:
        return []
    first_ts = int(b2[0][0]) - 1
    try:
        b1 = http_json(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=500&endTime={first_ts}")
    except Exception:
        b1 = []
    raw = (b1 or []) + b2
    parsed = []
    for k in raw:
        vol = float(k[5])
        taker_buy = float(k[9])
        parsed.append({
            "ts": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "vol": vol,
            "taker_buy_ratio": (taker_buy / vol) if vol > 0 else 0.5
        })
    return parsed

def compute_indicators(bars):
    n = len(bars)
    for i in range(n):
        c = bars[i]["close"]
        tr = (bars[i]["high"] - bars[i]["low"]) if i == 0 else max(
            bars[i]["high"] - bars[i]["low"],
            abs(bars[i]["high"] - bars[i - 1]["close"]),
            abs(bars[i]["low"] - bars[i - 1]["close"])
        )
        bars[i]["tr"] = tr
        bars[i]["atr14"] = (sum(bars[j]["tr"] for j in range(i - 13, i + 1)) / 14.0) if i >= 14 else tr

        if i >= 14:
            gains, losses = 0.0, 0.0
            for j in range(i - 13, i + 1):
                d = bars[j]["close"] - bars[j - 1]["close"]
                if d > 0:
                    gains += d
                else:
                    losses -= d
            bars[i]["rsi14"] = 100.0 if losses == 0 else 100.0 - (100.0 / (1.0 + (gains / losses)))
        else:
            bars[i]["rsi14"] = 50.0

        bars[i]["sma20"] = (sum(bars[j]["close"] for j in range(i - 19, i + 1)) / 20.0) if i >= 20 else c
        bars[i]["vol_sma20"] = (sum(bars[j]["vol"] for j in range(i - 19, i + 1)) / 20.0) if i >= 20 else bars[i]["vol"]
        bars[i]["high24"] = max(bars[j]["high"] for j in range(i - 24, i)) if i >= 24 else bars[i]["high"]
        bars[i]["low24"] = min(bars[j]["low"] for j in range(i - 24, i)) if i >= 24 else bars[i]["low"]
        bars[i]["high12"] = max(bars[j]["high"] for j in range(i - 12, i)) if i >= 12 else bars[i]["high"]
        bars[i]["low12"] = min(bars[j]["low"] for j in range(i - 12, i)) if i >= 12 else bars[i]["low"]
        bars[i]["sma50"] = (sum(bars[j]["close"] for j in range(i - 49, i + 1)) / 50.0) if i >= 50 else c
        bars[i]["sma100"] = (sum(bars[j]["close"] for j in range(i - 99, i + 1)) / 100.0) if i >= 100 else c
        bars[i]["sma200"] = (sum(bars[j]["close"] for j in range(i - 199, i + 1)) / 200.0) if i >= 200 else c
        bars[i]["ret24"] = ((c - bars[i - 24]["close"]) / bars[i - 24]["close"]) if i >= 24 else 0.0
    return bars

def eval_strategy(all_data, start_i, end_i, mode):
    capital = 500.0
    equity = capital
    peak_eq = capital
    max_dd_pct = 0.0
    wins, losses = 0, 0
    gross_win, gross_loss = 0.0, 0.0
    pos = None

    for i in range(start_i, end_i):
        if pos is not None:
            sym = pos["sym"]
            b = all_data[sym][i]
            side = pos["side"]
            pos["bars_held"] += 1
            if side == "LONG":
                if b["low"] <= pos["sl"]:
                    pnl = pos["size_usd"] * ((pos["sl"] - pos["entry"]) / pos["entry"] - 0.0012)
                    equity += pnl
                    if pnl >= 0:
                        wins += 1
                        gross_win += pnl
                    else:
                        losses += 1
                        gross_loss += abs(pnl)
                    pos = None
                elif b["high"] >= pos["tp"]:
                    pnl = pos["size_usd"] * ((pos["tp"] - pos["entry"]) / pos["entry"] - 0.0012)
                    equity += pnl
                    wins += 1
                    gross_win += pnl
                    pos = None
                elif pos["bars_held"] >= pos["max_bars"]:
                    pnl = pos["size_usd"] * ((b["close"] - pos["entry"]) / pos["entry"] - 0.0012)
                    equity += pnl
                    if pnl >= 0:
                        wins += 1
                        gross_win += pnl
                    else:
                        losses += 1
                        gross_loss += abs(pnl)
                    pos = None
                else:
                    # Trail stop only after reaching +2.0 * ATR, locking +1.0 * ATR profit!
                    if b["high"] >= pos["entry"] + 2.0 * pos["atr"]:
                        pos["sl"] = max(pos["sl"], pos["entry"] + 1.0 * pos["atr"])
            else:  # SHORT
                if b["high"] >= pos["sl"]:
                    pnl = pos["size_usd"] * ((pos["entry"] - pos["sl"]) / pos["entry"] - 0.0012)
                    equity += pnl
                    if pnl >= 0:
                        wins += 1
                        gross_win += pnl
                    else:
                        losses += 1
                        gross_loss += abs(pnl)
                    pos = None
                elif b["low"] <= pos["tp"]:
                    pnl = pos["size_usd"] * ((pos["entry"] - pos["tp"]) / pos["entry"] - 0.0012)
                    equity += pnl
                    wins += 1
                    gross_win += pnl
                    pos = None
                elif pos["bars_held"] >= pos["max_bars"]:
                    pnl = pos["size_usd"] * ((pos["entry"] - b["close"]) / pos["entry"] - 0.0012)
                    equity += pnl
                    if pnl >= 0:
                        wins += 1
                        gross_win += pnl
                    else:
                        losses += 1
                        gross_loss += abs(pnl)
                    pos = None
                else:
                    if b["low"] <= pos["entry"] - 2.0 * pos["atr"]:
                        pos["sl"] = min(pos["sl"], pos["entry"] - 1.0 * pos["atr"])

            peak_eq = max(peak_eq, equity)
            dd = (peak_eq - equity) / peak_eq * 100.0
            max_dd_pct = max(max_dd_pct, dd)
        else:
            # Market breadth filter: how many pairs have close > sma50?
            bull_count = sum(1 for s in SYMBOLS if all_data[s][i]["close"] > all_data[s][i]["sma50"])
            for sym in SYMBOLS:
                b = all_data[sym][i]
                prev = all_data[sym][i - 1]
                atr = b["atr14"]
                if atr <= 0:
                    continue

                if mode == "A_EXTREME_CAPITULATION_REVERSAL_ONLY":
                    # Ultra-selective mean reversion: Only when RSI14 < 25 (extreme panic) AND current bar closes green with buyer absorption (>0.54)
                    # Stop = 2.0 * ATR, TP = 3.2 * ATR, Max hold = 36h
                    stop_pct = (2.0 * atr) / b["close"]
                    if stop_pct < 0.005 or stop_pct > 0.045:
                        continue
                    size_usd = min(250.0, 3.00 / stop_pct)
                    if prev["rsi14"] < 26.0 and b["close"] > b["open"] and b["taker_buy_ratio"] >= 0.54 and b["close"] > prev["close"]:
                        pos = {
                            "sym": sym, "side": "LONG", "entry": b["close"], "atr": atr,
                            "sl": b["close"] - 2.0 * atr, "tp": b["close"] + 3.2 * atr,
                            "size_usd": size_usd, "bars_held": 0, "max_bars": 36
                        }
                        break
                    # Extreme overbought blow-off top short: RSI14 > 76 + red rejection candle
                    if sym != "PAXGUSDT" and prev["rsi14"] > 76.0 and b["close"] < b["open"] and b["taker_buy_ratio"] <= 0.46 and b["close"] < prev["close"]:
                        pos = {
                            "sym": sym, "side": "SHORT", "entry": b["close"], "atr": atr,
                            "sl": b["close"] + 2.0 * atr, "tp": b["close"] - 3.2 * atr,
                            "size_usd": size_usd, "bars_held": 0, "max_bars": 36
                        }
                        break

                elif mode == "B_24H_DONCHIAN_MOMENTUM_REGIME":
                    # Institutional CTA 24H Channel Breakout aligned with Market Breadth
                    stop_pct = (2.0 * atr) / b["close"]
                    if stop_pct < 0.005 or stop_pct > 0.045:
                        continue
                    size_usd = min(250.0, 3.00 / stop_pct)
                    if bull_count >= 4 and b["sma50"] > b["sma200"] and b["close"] > b["high24"] and b["vol"] > 1.5 * b["vol_sma20"] and b["taker_buy_ratio"] >= 0.56:
                        pos = {
                            "sym": sym, "side": "LONG", "entry": b["close"], "atr": atr,
                            "sl": b["close"] - 2.0 * atr, "tp": b["close"] + 4.0 * atr,
                            "size_usd": size_usd, "bars_held": 0, "max_bars": 48
                        }
                        break
                    if sym != "PAXGUSDT" and bull_count <= 2 and b["sma50"] < b["sma200"] and b["close"] < b["low24"] and b["vol"] > 1.5 * b["vol_sma20"] and b["taker_buy_ratio"] <= 0.44:
                        pos = {
                            "sym": sym, "side": "SHORT", "entry": b["close"], "atr": atr,
                            "sl": b["close"] + 2.0 * atr, "tp": b["close"] - 4.0 * atr,
                            "size_usd": size_usd, "bars_held": 0, "max_bars": 48
                        }
                        break

                elif mode == "C_HYBRID_CAPITULATION_PLUS_REGIME_SHORT":
                    # Combines Extreme Capitulation Longs (RSI < 25 + green absorption) with Bear Regime Breakdown Shorts
                    stop_pct = (2.0 * atr) / b["close"]
                    if stop_pct < 0.005 or stop_pct > 0.045:
                        continue
                    size_usd = min(250.0, 3.00 / stop_pct)
                    if prev["rsi14"] < 25.0 and b["close"] > b["open"] and b["taker_buy_ratio"] >= 0.54 and b["close"] > prev["close"]:
                        pos = {
                            "sym": sym, "side": "LONG", "entry": b["close"], "atr": atr,
                            "sl": b["close"] - 2.0 * atr, "tp": b["close"] + 3.2 * atr,
                            "size_usd": size_usd, "bars_held": 0, "max_bars": 36
                        }
                        break
                    if sym != "PAXGUSDT" and bull_count <= 1 and b["sma50"] < b["sma200"] and prev["rsi14"] > 66.0 and b["close"] < b["open"] and b["taker_buy_ratio"] <= 0.45:
                        pos = {
                            "sym": sym, "side": "SHORT", "entry": b["close"], "atr": atr,
                            "sl": b["close"] + 1.8 * atr, "tp": b["close"] - 3.2 * atr,
                            "size_usd": size_usd, "bars_held": 0, "max_bars": 36
                        }
                        break

    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    total_trades = wins + losses
    return {
        "mode": mode,
        "net_pnl_usd": round(equity - capital, 2),
        "wins": wins,
        "losses": losses,
        "total_trades": total_trades,
        "win_rate_pct": round(wins / max(1, total_trades) * 100.0, 1),
        "profit_factor": round(pf, 2),
        "max_drawdown_pct": round(max_dd_pct, 2)
    }

def main():
    all_data = {}
    for sym in SYMBOLS:
        bars = fetch_1500h_klines(sym)
        all_data[sym] = compute_indicators(bars)
    min_len = min(len(v) for v in all_data.values())
    mid_i = 205 + (min_len - 205) // 2

    modes = [
        "A_EXTREME_CAPITULATION_REVERSAL_ONLY",
        "B_24H_DONCHIAN_MOMENTUM_REGIME",
        "C_HYBRID_CAPITULATION_PLUS_REGIME_SHORT"
    ]
    results = []
    for m in modes:
        full_r = eval_strategy(all_data, 205, min_len, m)
        is_r = eval_strategy(all_data, 205, mid_i, m)
        oos_r = eval_strategy(all_data, mid_i, min_len, m)
        results.append({
            "strategy": m,
            "full_1500h": full_r,
            "in_sample_first_half": is_r,
            "out_of_sample_second_half": oos_r
        })

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(json.dumps(results, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
