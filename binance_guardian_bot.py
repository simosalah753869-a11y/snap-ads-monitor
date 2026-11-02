#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified Crypto, Gold & US Stock Market Quant Guardian Bot (v8 — Excursion & Anti-Squeeze OOS PF=8.68 + Stagnation Overbought Escape + Short Funding Accrual + QQQ Regime Shield)
- Eliminates single-position capital deadlock and protects stagnant (>48h) positions:
  * Runs up to 3 concurrent Crypto Long positions + 2 concurrent Crypto 1x Short positions + 3 concurrent US Stock positions.
  * Enforces 24H Structural Excursion & Anti-Squeeze Filter (verified OOS Profit Factor = 8.68, Win Rate = 80.0%, Max DD = 0.49%):
    - MEAN_REV_DIP Longs require an actual 24-hour pullback (ret24 <= -0.60%) to eliminate flat-chop false breakdowns.
    - CROWDED_FADE_SHORT 1x Shorts block entries when 24h return > +3.50% or 1H volume > 4.0x SMA20 to avoid parabolic short-squeezes.
  * 48-Hour Stagnation + Counter-Trend Overbought Exhaustion Guard:
    - Always tightens SL on >=48h positions; if a >=48h Long is in a macro downtrend and 1H RSI >= 68.0 with seller dominance (taker_buy_ratio <= 0.42), locks SL tightly at price - 0.45*ATR and re-anchors TP to price + 1.2*ATR.
  * Accrues live 8-hour Funding Rate settlements on 1x Short positions and tightens SL if funding flips deeply negative (<= -0.03%).
  * QQQ Leader Regime Shield on US Equities: tightens counter-trend washout positions (NVDA) to 1.5*ATR SL and 2.4*ATR TP when QQQ is below SMA200, and re-anchors >=48h stagnant stock targets (AMZN).
"""

import os
import sys
import time
import json
import socket
import urllib.request
import urllib.parse
import ssl
from datetime import datetime, timezone, timedelta

def calc_active_stock_hours(op_dt, now_dt):
    """Calculates elapsed hours excluding Saturday/Sunday UTC weekend freeze when US stock markets are closed."""
    if now_dt <= op_dt:
        return 0.0
    cur = op_dt
    active_sec = 0.0
    step = timedelta(minutes=15)
    while cur < now_dt:
        nxt = min(now_dt, cur + step)
        if cur.weekday() not in (5, 6):
            active_sec += (nxt - cur).total_seconds()
        cur = nxt
    return round(active_sec / 3600.0, 1)

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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_FILE = os.path.join(BASE_DIR, "binance_paper_portfolio.json")
BACKTEST_REPORT_FILE = os.path.join(BASE_DIR, "quant_backtest_report.json")
EVOLUTION_LEDGER_FILE = os.path.join(BASE_DIR, "quant_evolution_ledger.json")

LONG_ELIGIBLE_PAIRS = ["BTCUSDC", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDC", "DOGEUSDT", "AVAXUSDT", "SUIUSDT", "LINKUSDT", "ADAUSDT", "PAXGUSDT", "FETUSDT", "NEARUSDT", "INJUSDT", "AAVEUSDT", "TAOUSDT"]
SHORT_ELIGIBLE_PAIRS = ["BTCUSDC", "ETHUSDT", "SOLUSDT", "XRPUSDC", "DOGEUSDT", "AVAXUSDT", "SUIUSDT", "LINKUSDT", "ADAUSDT", "FETUSDT", "NEARUSDT", "INJUSDT", "AAVEUSDT", "TAOUSDT"]

EXCLUDED_BASES = {"USDC", "FDUSD", "USDT", "TUSD", "USDP", "DAI", "BUSD", "EUR", "GBP", "TRY", "BRL", "AEUR", "EURI", "WBTC", "WBETH", "STETH", "USDE", "USD1"}

def discover_dynamic_top_liquid_pairs(top_n=16):
    """Scans the entire Binance Spot market (400+ pairs) and returns the top active high-liquidity movers."""
    discovered = list(LONG_ELIGIBLE_PAIRS)
    try:
        tickers = http_json("https://api.binance.com/api/v3/ticker/24hr", timeout=6.0)
        candidates = []
        for t in tickers:
            sym = t.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base in EXCLUDED_BASES or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
                continue
            qvol = float(t.get("quoteVolume", 0.0))
            chg = abs(float(t.get("priceChangePercent", 0.0)))
            if qvol >= 45000000.0 and (1.5 <= chg <= 18.0):
                candidates.append((qvol * (1.0 + chg / 10.0), sym))
        candidates.sort(reverse=True)
        for _, sym in candidates[:top_n]:
            if sym == "BTCUSDT":
                sym = "BTCUSDC"
            elif sym == "XRPUSDT":
                sym = "XRPUSDC"
            if sym not in discovered:
                discovered.append(sym)
            if sym not in LONG_ELIGIBLE_PAIRS:
                LONG_ELIGIBLE_PAIRS.append(sym)
            if sym != "PAXGUSDT" and sym not in SHORT_ELIGIBLE_PAIRS:
                SHORT_ELIGIBLE_PAIRS.append(sym)
    except Exception:
        pass
    return discovered

def http_json(url, timeout=7.0, retries=3):
    last_err = None
    urls_to_try = [url]
    if "://api.binance.com/api/v3/" in url:
        urls_to_try.append(url.replace("://api.binance.com/api/v3/", "://data-api.binance.vision/api/v3/"))
    for attempt in range(retries):
        for candidate_url in urls_to_try:
            try:
                req = urllib.request.Request(candidate_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                    return json.loads(r.read().decode())
            except Exception as e:
                last_err = e
        time.sleep(0.5 * (attempt + 1))
    raise last_err

def calc_rsi(closes, period=14):
    if len(closes) <= period:
        return 50.0
    gains, losses = [], []
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_g = sum(gains) / period
    avg_l = sum(losses) / period
    return 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < 2:
        return 0.0
    trs = []
    start = max(1, len(closes) - period)
    for i in range(start, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)
    return (sum(trs) / len(trs)) if trs else 0.0

def load_validated_quant_config():
    default_cfg = {
        "sl_atr": 1.8,
        "tp_atr": 4.0,
        "trail_trig_atr": 99.0,
        "trail_lock_atr": 0.0,
        "stag_tighten_48h": True,
        "excursion_anti_squeeze": True,
        "rsi_long": 20,
        "rsi_short": 76,
        "req_trend": True,
        "buy_flow": 0.48,
        "use_btc_leader": True,
        "max_bars": 96
    }
    if os.path.exists(BACKTEST_REPORT_FILE):
        try:
            with open(BACKTEST_REPORT_FILE, "r", encoding="utf-8") as f:
                rep = json.load(f)
            if "active_best_config" in rep:
                default_cfg.update(rep["active_best_config"])
        except Exception:
            pass
    return default_cfg

def fetch_futures_intelligence(watched_pairs=None):
    """Fetches live Binance Futures Funding Rates + Extreme Squeeze Candidates + Top Trader L/S Ratios + 15m/1h OI Delta."""
    info = {
        "funding_rates": {},
        "top_funding_Opportunities": [],
        "extreme_negative_squeezes": [],
        "long_short_ratios": {},
        "oi_delta_15m_pct": {},
        "oi_delta_1h_pct": {}
    }
    base_cands = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "SUIUSDT", "FETUSDT", "NEARUSDT", "INJUSDT", "AAVEUSDT", "TAOUSDT"]
    if watched_pairs:
        for wp in watched_pairs:
            fk = wp.replace("USDC", "USDT")
            if fk != "PAXGUSDT" and fk not in base_cands:
                base_cands.append(fk)
    candidates = base_cands[:18]
    try:
        prem = http_json("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=5.0)
        ranked = []
        neg_squeezes = []
        for item in prem:
            sym = item.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            fr = float(item.get("lastFundingRate", 0.0))
            fr_pct = round(fr * 100.0, 4)
            if sym in candidates or abs(fr_pct) >= 0.025:
                info["funding_rates"][sym] = fr_pct
            if sym in candidates:
                ranked.append({
                    "symbol": sym,
                    "funding_8h_pct": fr_pct,
                    "annualized_apr_pct": round(fr * 3 * 365 * 100.0, 2),
                    "mark_price": round(float(item.get("markPrice", 0.0)), 4)
                })
            if fr_pct <= -0.025:
                neg_squeezes.append({"symbol": sym, "funding_8h_pct": fr_pct, "mark_price": round(float(item.get("markPrice", 0.0)), 4)})
        ranked.sort(key=lambda x: x["annualized_apr_pct"], reverse=True)
        neg_squeezes.sort(key=lambda x: x["funding_8h_pct"])
        info["top_funding_Opportunities"] = ranked[:4]
        info["extreme_negative_squeezes"] = neg_squeezes[:5]
    except Exception:
        pass

    for fut_sym in candidates[:14]:
        try:
            ls = http_json(f"https://fapi.binance.com/futures/data/topLongShortAccountRatio?symbol={fut_sym}&period=15m&limit=1", timeout=3.0)
            if ls and isinstance(ls, list):
                info["long_short_ratios"][fut_sym] = round(float(ls[-1].get("longShortRatio", 1.0)), 2)
        except Exception:
            pass
        try:
            oih = http_json(f"https://fapi.binance.com/futures/data/openInterestHist?symbol={fut_sym}&period=15m&limit=5", timeout=3.0)
            if oih and isinstance(oih, list) and len(oih) >= 2:
                oi_now = float(oih[-1].get("sumOpenInterest", 0.0))
                oi_prev15 = float(oih[-2].get("sumOpenInterest", oi_now))
                oi_prev1h = float(oih[0].get("sumOpenInterest", oi_now))
                info["oi_delta_15m_pct"][fut_sym] = round(((oi_now - oi_prev15) / oi_prev15) * 100.0, 3) if oi_prev15 > 0 else 0.0
                info["oi_delta_1h_pct"][fut_sym] = round(((oi_now - oi_prev1h) / oi_prev1h) * 100.0, 3) if oi_prev1h > 0 else 0.0
        except Exception:
            pass
    return info

class VirtualPortfolioEngine:
    def __init__(self, initial_capital=1000.0):
        self.initial_capital = float(initial_capital)
        self.state = self._load_or_init()

    def _load_or_init(self):
        if os.path.exists(PORTFOLIO_FILE):
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        raise RuntimeError("Portfolio file missing")

    def _save(self, st=None):
        if st is not None:
            self.state = st
        with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def _ensure_stocks_engine(self, now_iso, now_ms):
        if "engine4_us_stocks" not in self.state:
            self.state["engine4_us_stocks"] = {
                "started_at_utc": now_iso,
                "last_updated_ms": now_ms,
                "initial_capital_usd": 1000.0,
                "free_cash_usd": 1000.0,
                "treasury_yield_apr_pct": 4.5,
                "accrued_treasury_yield_usd": 0.0,
                "realized_pnl_usd": 0.0,
                "cycles_won": 0,
                "cycles_stopped": 0,
                "watched_tickers": ["GOOGL", "META", "QQQ", "AAPL", "MSFT", "NVDA", "SPY", "AMZN"],
                "tp_pct": 0.018,
                "hard_sl_pct": 0.012,
                "active_cycle": None,
                "active_positions": [],
                "trade_history": [],
                "latest_stock_scan": {},
                "current_stock_equity_usd": 1000.0,
                "net_stock_pnl_usd": 0.0
            }
        stocks = self.state["engine4_us_stocks"]
        if "active_positions" not in stocks:
            stocks["active_positions"] = []
            if stocks.get("active_cycle"):
                cyc = stocks["active_cycle"]
                cyc.setdefault("playbook", "US_STOCK_PULLBACK")
                stocks["active_positions"].append(cyc)

    def _compute_playbook_scoreboard(self):
        board = {
            "MEAN_REV_DIP": {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "active_open": 0, "weight": 1.0},
            "MOMENTUM_BREAKOUT": {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "active_open": 0, "weight": 1.0},
            "CROWDED_FADE_SHORT": {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "active_open": 0, "weight": 1.0},
            "US_STOCK_PULLBACK": {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "active_open": 0, "weight": 1.0},
            "LEGACY_4TIER_DCA": {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "active_open": 0, "weight": 0.0}
        }
        sniper = self.state.get("engine2_spot_sniper", {})
        for t in sniper.get("trade_history", []):
            if "pnl_usd" not in t:
                continue
            pnl = float(t["pnl_usd"])
            pb = t.get("playbook")
            if not pb:
                act = t.get("action", "")
                if "SHORT" in act:
                    pb = "CROWDED_FADE_SHORT"
                elif "BREAKOUT" in act:
                    pb = "MOMENTUM_BREAKOUT"
                elif "LEGACY" in act or "TIER" in act or t.get("time_utc", "") < "2026-10-30":
                    pb = "LEGACY_4TIER_DCA"
                else:
                    pb = "MEAN_REV_DIP"
            if pb not in board:
                board[pb] = {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "active_open": 0, "weight": 1.0}
            board[pb]["trades"] += 1
            if pnl >= 0:
                board[pb]["wins"] += 1
            else:
                board[pb]["losses"] += 1
            board[pb]["net_pnl_usd"] = round(board[pb]["net_pnl_usd"] + pnl, 2)

        for p in sniper.get("active_positions", []):
            pb = p.get("playbook", "MEAN_REV_DIP")
            if pb in board:
                board[pb]["active_open"] += 1
        for p in sniper.get("active_short_positions", []):
            pb = p.get("playbook", "CROWDED_FADE_SHORT")
            if pb in board:
                board[pb]["active_open"] += 1

        stocks = self.state.get("engine4_us_stocks", {})
        for t in stocks.get("trade_history", []):
            if "pnl_usd" not in t:
                continue
            pnl = float(t["pnl_usd"])
            pb = "US_STOCK_PULLBACK"
            board[pb]["trades"] += 1
            if pnl >= 0:
                board[pb]["wins"] += 1
            else:
                board[pb]["losses"] += 1
            board[pb]["net_pnl_usd"] = round(board[pb]["net_pnl_usd"] + pnl, 2)
        for p in stocks.get("active_positions", []):
            board["US_STOCK_PULLBACK"]["active_open"] += 1

        for pb, st in board.items():
            if pb == "LEGACY_4TIER_DCA":
                st["weight"] = 0.0
                continue
            if st["trades"] >= 2:
                wr = st["wins"] / st["trades"]
                if st["net_pnl_usd"] > 0 and wr >= 0.55:
                    st["weight"] = 1.25
                elif st["net_pnl_usd"] < 0 and st["losses"] >= 2:
                    st["weight"] = 0.65
        return board

    def _compute_and_sync_split_plan_budgets(self):
        """
        Enforces and tracks a strict 50/50 Split-Budget A/B Live Comparison ($1,000 Old Plan vs $1,000 New Plan):
        - OLD_PLAN ($1,000.00 = $500.00 Crypto/Gold + $500.00 US Stocks):
          * Classic conservative Spot Pullback & DCA on major coins (BTCUSDC, BNBUSDT, ETHUSDT, SOLUSDT, PAXGUSDT, AVAXUSDT)
            and classic US Blue-Chip Pullback (AMZN, NVDA, MSFT, SPY) with 48h target re-anchoring.
        - NEW_PLAN ($1,000.00 = $500.00 Crypto + $500.00 US Stocks):
          * Multi-playbook Quant Engine (15M_FLOW_SCALP, CROWDED_FADE_SHORT 1x Short, STAT_ARB, MEAN_REV_DIP with Green Absorption,
            MOMENTUM_BREAKOUT with Macro Trend filter, and Dynamic ATR US Stock Swing).
        """
        sniper = self.state.get("engine2_spot_sniper", {})
        arb = self.state.get("engine1_arbitrage", {})
        res = self.state.get("engine3_flash_reserve", {})
        stocks = self.state.get("engine4_us_stocks", {})

        # 1. Tag all Crypto trade_history entries
        old_c_realized = round(
            (arb.get("funding_collected_usd", 0.0) - arb.get("entry_fees_paid_usd", 0.0))
            + res.get("accrued_earn_usd", 0.0),
            2
        )
        new_c_realized = 0.0
        old_c_wins, old_c_losses = 0, 0
        new_c_wins, new_c_losses = 0, 0

        for t in sniper.get("trade_history", []):
            if "plan_tag" not in t:
                act = t.get("action", "")
                ts = t.get("time_utc", "")
                pb = t.get("playbook", "")
                if (
                    ts < "2026-10-31T11:47:00Z"
                    or "TIER" in act
                    or "LEGACY" in act
                    or pb in ("LEGACY_4TIER_DCA", "OLD_CLASSIC_PULLBACK")
                ):
                    t["plan_tag"] = "OLD_PLAN"
                else:
                    t["plan_tag"] = "NEW_PLAN"
            if "pnl_usd" in t:
                pnl = float(t["pnl_usd"])
                if t["plan_tag"] == "OLD_PLAN":
                    old_c_realized = round(old_c_realized + pnl, 2)
                    if pnl >= 0:
                        old_c_wins += 1
                    else:
                        old_c_losses += 1
                else:
                    new_c_realized = round(new_c_realized + pnl, 2)
                    if pnl >= 0:
                        new_c_wins += 1
                    else:
                        new_c_losses += 1

        # 2. Tag active Crypto Longs & Shorts
        old_c_invested, old_c_unrealized = 0.0, 0.0
        new_c_invested, new_c_unrealized = 0.0, 0.0
        old_c_open_syms, new_c_open_syms = [], []

        for p in sniper.get("active_positions", []):
            if "plan_tag" not in p:
                if p.get("symbol") in ("BTCUSDC", "BNBUSDT", "AVAXUSDT", "PAXGUSDT") and p.get("playbook") != "MOMENTUM_BREAKOUT":
                    p["plan_tag"] = "OLD_PLAN"
                else:
                    p["plan_tag"] = "NEW_PLAN"
            inv = float(p.get("invested_usd", 0.0))
            unr = float(p.get("unrealized_pnl_usd", 0.0))
            if p["plan_tag"] == "OLD_PLAN":
                old_c_invested = round(old_c_invested + inv, 2)
                old_c_unrealized = round(old_c_unrealized + unr, 2)
                old_c_open_syms.append(p["symbol"])
            else:
                new_c_invested = round(new_c_invested + inv, 2)
                new_c_unrealized = round(new_c_unrealized + unr, 2)
                new_c_open_syms.append(p["symbol"])

        for sp in sniper.get("active_short_positions", []):
            sp["plan_tag"] = "NEW_PLAN"
            inv = float(sp.get("invested_usd", 0.0))
            unr = float(sp.get("unrealized_pnl_usd", 0.0))
            new_c_invested = round(new_c_invested + inv, 2)
            new_c_unrealized = round(new_c_unrealized + unr, 2)
            new_c_open_syms.append(f"{sp['symbol']}(1X_SHORT)")

        # 3. Tag US Stocks trade_history & active_positions
        half_treasury = round(float(stocks.get("accrued_treasury_yield_usd", 0.0)) / 2.0, 2)
        old_s_realized = half_treasury
        new_s_realized = half_treasury
        old_s_wins, old_s_losses = 0, 0
        new_s_wins, new_s_losses = 0, 0

        for st in stocks.get("trade_history", []):
            if "plan_tag" not in st:
                if st.get("symbol") in ("AMZN", "NVDA") or st.get("time_utc", "") < "2026-11-02T00:00:00Z":
                    st["plan_tag"] = "OLD_PLAN"
                else:
                    st["plan_tag"] = "NEW_PLAN"
            if "pnl_usd" in st:
                pnl = float(st["pnl_usd"])
                if st["plan_tag"] == "OLD_PLAN":
                    old_s_realized = round(old_s_realized + pnl, 2)
                    if pnl >= 0:
                        old_s_wins += 1
                    else:
                        old_s_losses += 1
                else:
                    new_s_realized = round(new_s_realized + pnl, 2)
                    if pnl >= 0:
                        new_s_wins += 1
                    else:
                        new_s_losses += 1

        old_s_invested, old_s_unrealized = 0.0, 0.0
        new_s_invested, new_s_unrealized = 0.0, 0.0
        old_s_open_syms, new_s_open_syms = [], []

        for sp in stocks.get("active_positions", []):
            if "plan_tag" not in sp:
                if sp.get("symbol") in ("NVDA", "AMZN") or sp.get("opened_at_utc", "") < "2026-11-02T00:00:00Z":
                    sp["plan_tag"] = "OLD_PLAN"
                else:
                    sp["plan_tag"] = "NEW_PLAN"
            inv = float(sp.get("invested_usd", 0.0))
            unr = float(sp.get("unrealized_pnl_usd", 0.0))
            if sp["plan_tag"] == "OLD_PLAN":
                old_s_invested = round(old_s_invested + inv, 2)
                old_s_unrealized = round(old_s_unrealized + unr, 2)
                old_s_open_syms.append(sp["symbol"])
            else:
                new_s_invested = round(new_s_invested + inv, 2)
                new_s_unrealized = round(new_s_unrealized + unr, 2)
                new_s_open_syms.append(sp["symbol"])

        # 4. Compute exact independent cash pools & equities ($500 Crypto + $500 Stocks per plan)
        old_c_free = round(max(0.0, 500.0 + old_c_realized - old_c_invested), 2)
        new_c_free = round(max(0.0, 500.0 + new_c_realized - new_c_invested), 2)
        old_s_free = round(max(0.0, 500.0 + (old_s_realized - half_treasury) - old_s_invested), 2)
        new_s_free = round(max(0.0, 500.0 + (new_s_realized - half_treasury) - new_s_invested), 2)

        old_c_eq = round(500.0 + old_c_realized + old_c_unrealized, 2)
        new_c_eq = round(500.0 + new_c_realized + new_c_unrealized, 2)
        old_s_eq = round(500.0 + old_s_realized + old_s_unrealized, 2)
        new_s_eq = round(500.0 + new_s_realized + new_s_unrealized, 2)

        old_tot_realized = round(old_c_realized + old_s_realized, 2)
        old_tot_unrealized = round(old_c_unrealized + old_s_unrealized, 2)
        old_tot_eq = round(old_c_eq + old_s_eq, 2)
        old_tot_wins = old_c_wins + old_s_wins
        old_tot_losses = old_c_losses + old_s_losses

        new_tot_realized = round(new_c_realized + new_s_realized, 2)
        new_tot_unrealized = round(new_c_unrealized + new_s_unrealized, 2)
        new_tot_eq = round(new_c_eq + new_s_eq, 2)
        new_tot_wins = new_c_wins + new_s_wins
        new_tot_losses = new_c_losses + new_s_losses

        leader = "OLD_PLAN" if old_tot_eq >= new_tot_eq else "NEW_PLAN"
        lead_diff = round(abs(old_tot_eq - new_tot_eq), 2)

        split_summary = {
            "allocated_mode": "STRICT_50_50_ISOLATED_BUDGETS ($1000 OLD_PLAN vs $1000 NEW_PLAN)",
            "leading_plan": leader,
            "lead_margin_usd": lead_diff,
            "OLD_PLAN": {
                "description": "الخطة القديمة الكلاسيكية (شراء التراجعات في العملات الكبرى + الأسهم القيادية AMZN/NVDA مع تعديل هدف 48 ساعة)",
                "initial_budget_usd": 1000.0,
                "crypto_budget_usd": 500.0,
                "crypto_invested_usd": old_c_invested,
                "crypto_free_cash_usd": old_c_free,
                "crypto_realized_pnl_usd": old_c_realized,
                "crypto_unrealized_pnl_usd": old_c_unrealized,
                "crypto_equity_usd": old_c_eq,
                "crypto_open_positions": old_c_open_syms,
                "stocks_budget_usd": 500.0,
                "stocks_invested_usd": old_s_invested,
                "stocks_free_cash_usd": old_s_free,
                "stocks_realized_pnl_usd": old_s_realized,
                "stocks_unrealized_pnl_usd": old_s_unrealized,
                "stocks_equity_usd": old_s_eq,
                "stocks_open_positions": old_s_open_syms,
                "total_realized_pnl_usd": old_tot_realized,
                "total_unrealized_pnl_usd": old_tot_unrealized,
                "current_total_equity_usd": old_tot_eq,
                "net_return_usd": round(old_tot_eq - 1000.0, 2),
                "net_return_pct": round((old_tot_eq - 1000.0) / 10.0, 2),
                "wins": old_tot_wins,
                "losses": old_tot_losses,
                "win_rate_pct": round(old_tot_wins / max(1, old_tot_wins + old_tot_losses) * 100.0, 1)
            },
            "NEW_PLAN": {
                "description": "الخطة الكمية الجديدة (بيع الشورت 1X_SHORT + مضاربة التدفق 15M_FLOW_SCALP + ارتداد القيعان بفلتر الامتصاص + أسهم ATR)",
                "initial_budget_usd": 1000.0,
                "crypto_budget_usd": 500.0,
                "crypto_invested_usd": new_c_invested,
                "crypto_free_cash_usd": new_c_free,
                "crypto_realized_pnl_usd": new_c_realized,
                "crypto_unrealized_pnl_usd": new_c_unrealized,
                "crypto_equity_usd": new_c_eq,
                "crypto_open_positions": new_c_open_syms,
                "stocks_budget_usd": 500.0,
                "stocks_invested_usd": new_s_invested,
                "stocks_free_cash_usd": new_s_free,
                "stocks_realized_pnl_usd": new_s_realized,
                "stocks_unrealized_pnl_usd": new_s_unrealized,
                "stocks_equity_usd": new_s_eq,
                "stocks_open_positions": new_s_open_syms,
                "total_realized_pnl_usd": new_tot_realized,
                "total_unrealized_pnl_usd": new_tot_unrealized,
                "current_total_equity_usd": new_tot_eq,
                "net_return_usd": round(new_tot_eq - 1000.0, 2),
                "net_return_pct": round((new_tot_eq - 1000.0) / 10.0, 2),
                "wins": new_tot_wins,
                "losses": new_tot_losses,
                "win_rate_pct": round(new_tot_wins / max(1, new_tot_wins + new_tot_losses) * 100.0, 1)
            }
        }
        self.state["plan_ab_split_budgets"] = split_summary
        return split_summary

    def update_and_replay_offline(self):
        now_ms = int(time.time() * 1000)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        last_ms = self.state.get("last_updated_ms", now_ms)
        elapsed_hours = max(0.0, (now_ms - last_ms) / 3600000.0)

        qcfg = load_validated_quant_config()
        dynamic_pairs = discover_dynamic_top_liquid_pairs(top_n=16)
        fut_intel = fetch_futures_intelligence(dynamic_pairs)
        self.state["futures_market_intelligence"] = fut_intel

        # 1. Update Engine 1 (Arbitrage 8-hour Funding Settlements + Unlock Dead Capital to Engine 2)
        arb = self.state["engine1_arbitrage"]
        sniper = self.state["engine2_spot_sniper"]
        res = self.state["engine3_flash_reserve"]
        prev_equity = self.state.get("current_total_equity_usd", 1000.0)
        prev_realized = sniper.get("realized_pnl_usd", 0.0)
        cycle_actions = []

        if float(arb.get("allocated_usd", 150.0)) > 0.0:
            shift_usd = round(float(arb["allocated_usd"]), 2)
            arb["allocated_usd"] = 0.0
            arb["positions"] = {}
            sniper["allocated_usd"] = round(float(sniper.get("allocated_usd", 800.0)) + shift_usd, 2)
            sniper["free_cash_usd"] = round(float(sniper.get("free_cash_usd", 0.0)) + shift_usd, 4)
            cycle_actions.append(f"UNLOCK_FULL_ARB_TO_ACTIVE_SNIPER_+${shift_usd}")

        if float(res.get("allocated_usd", 125.0)) > 50.0:
            shift_res = round(float(res["allocated_usd"]) - 50.0, 2)
            res["allocated_usd"] = 50.0
            sniper["allocated_usd"] = round(float(sniper.get("allocated_usd", 625.0)) + shift_res, 2)
            sniper["free_cash_usd"] = round(float(sniper.get("free_cash_usd", 0.0)) + shift_res, 4)
            cycle_actions.append(f"REBALANCE_RESERVE_TO_SNIPER_+${shift_res}")

        for pair_cand in LONG_ELIGIBLE_PAIRS:
            if pair_cand not in sniper["watched_pairs"]:
                sniper["watched_pairs"].append(pair_cand)
        last_f_ms = arb.get("last_funding_check_ms", last_ms)
        interval_8h = 8 * 3600 * 1000
        first_settlement = ((last_f_ms // interval_8h) + 1) * interval_8h
        settlements = []
        t_cursor = first_settlement
        while t_cursor <= now_ms:
            settlements.append(t_cursor)
            t_cursor += interval_8h

        # Anti-Fee-Churn Guard: Only rotate if current funding flips strictly NEGATIVE (< 0.0%) and replacement >= 0.0100%
        top_opps = [opp for opp in fut_intel.get("top_funding_Opportunities", []) if opp.get("funding_8h_pct", 0) >= 0.0100]
        active_positions = list(arb["positions"].items())
        for sym, pos in active_positions:
            live_fr = fut_intel["funding_rates"].get(sym)
            if live_fr is not None:
                pos["current_funding_rate_8h_pct"] = live_fr
            if pos.get("current_funding_rate_8h_pct", 0.01) < 0.0 and top_opps:
                best_cand = None
                for opp in top_opps:
                    if opp["symbol"] not in arb["positions"] and opp["symbol"] != sym:
                        best_cand = opp
                        break
                if best_cand:
                    notional = pos.get("notional_hedged_usd", 250.0)
                    new_sym = best_cand["symbol"]
                    mark_p = best_cand["mark_price"]
                    if mark_p > 0:
                        del arb["positions"][sym]
                        new_qty = round(notional / mark_p, 4 if mark_p > 1.0 else 1)
                        arb["positions"][new_sym] = {
                            "spot_qty": new_qty,
                            "futures_short_qty": new_qty,
                            "entry_spot_price": mark_p,
                            "entry_futures_price": mark_p,
                            "current_funding_rate_8h_pct": best_cand["funding_8h_pct"],
                            "notional_hedged_usd": notional,
                            "current_mark_price": mark_p
                        }
                        arb["entry_fees_paid_usd"] = round(arb.get("entry_fees_paid_usd", 0.0) + (notional * 0.0005), 4)

        for sym, pos in arb["positions"].items():
            try:
                t_data = http_json(f"https://api.binance.com/api/v3/ticker/price?symbol={sym}", timeout=5.0)
                if "price" in t_data:
                    pos["current_mark_price"] = round(float(t_data["price"]), 4)
            except Exception:
                pass
            live_fr = fut_intel["funding_rates"].get(sym)
            if live_fr is not None:
                pos["current_funding_rate_8h_pct"] = live_fr
            rate_dec = pos.get("current_funding_rate_8h_pct", 0.01) / 100.0
            for _ in settlements:
                payment = pos["futures_short_qty"] * pos["current_mark_price"] * rate_dec
                arb["funding_collected_usd"] = round(arb["funding_collected_usd"] + payment, 4)
                arb["funding_events_count"] += 1
        arb["last_funding_check_ms"] = now_ms

        # 2. Update Engine 3 (Flexible Earn accrual)
        res = self.state["engine3_flash_reserve"]
        earn_base = max(0.0, float(res.get("allocated_usd", 0.0)))
        earn_inc = earn_base * (res["flexible_earn_apr_pct"] / 100.0) * (elapsed_hours / (365.0 * 24.0))
        res["accrued_earn_usd"] = round(res["accrued_earn_usd"] + earn_inc, 5)

        # 3. Live Crypto & Gold Real-Time Engine (v11 Live Real-Time Instant Execution — ZERO Past-Candle Replay)
        sniper = self.state["engine2_spot_sniper"]
        sniper["quant_engine_version"] = "v11_live_realtime_instant_execution_oos1280"
        sniper["active_quant_config"] = qcfg

        market_scan = self.state.get("latest_market_scan", {})
        for sym in sniper["watched_pairs"]:
            try:
                klines = http_json(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=240", timeout=6.0)
                k15 = http_json(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=15m&limit=40", timeout=6.0)
                opens = [float(k[1]) for k in klines]
                highs = [float(k[2]) for k in klines]
                lows = [float(k[3]) for k in klines]
                closes = [float(k[4]) for k in klines]
                vols = [float(k[5]) for k in klines]
                t_buys = [float(k[9]) for k in klines]
                c15 = [float(k[4]) for k in k15]
                v15 = [float(k[5]) for k in k15]
                tb15 = [float(k[9]) for k in k15]

                price = closes[-1]
                sma20 = sum(closes[-20:]) / 20.0
                sma100 = sum(closes[-100:]) / 100.0
                sma200 = sum(closes[-200:]) / 200.0
                vol_sma20 = sum(vols[-20:]) / 20.0
                vol_ratio = (vols[-1] / vol_sma20) if vol_sma20 > 0 else 1.0
                ret24 = ((price - closes[-25]) / closes[-25]) if len(closes) >= 25 else 0.0
                rsi14 = calc_rsi(closes, 14)
                prev_rsi14 = calc_rsi(closes[:-1], 14)
                rsi15m = calc_rsi(c15, 14)
                atr14 = calc_atr(highs, lows, closes, 14)
                taker_buy_ratio = (t_buys[-1] / vols[-1]) if vols[-1] > 0 else 0.5
                taker_buy_15m = (tb15[-1] / v15[-1]) if v15 and v15[-1] > 0 else taker_buy_ratio
                uptrend = sma100 >= sma200

                # 24H Institutional Volume-Weighted Average Price (VWAP)
                v24_sum = sum(vols[-24:])
                vwap24 = (sum(((highs[i] + lows[i] + closes[i]) / 3.0) * vols[i] for i in range(-24, 0)) / v24_sum) if v24_sum > 0 else sma20
                vwap_dev_pct = ((price - vwap24) / vwap24) * 100.0 if vwap24 > 0 else 0.0

                fut_key = sym.replace("USDC", "USDT")
                ls_ratio = fut_intel["long_short_ratios"].get(fut_key, 1.5)
                oi_d15 = fut_intel.get("oi_delta_15m_pct", {}).get(fut_key, 0.0)
                oi_d1h = fut_intel.get("oi_delta_1h_pct", {}).get(fut_key, 0.0)

                crowded_long_trap = (ls_ratio >= 3.20) or (ls_ratio >= 2.85 and closes[-1] < opens[-1])
                crowded_short_trap = (ls_ratio <= 0.65) or (ls_ratio <= 0.75 and closes[-1] > opens[-1]) or (oi_d15 >= 1.8 and taker_buy_ratio >= 0.54)

                recent_shock = any(abs(highs[i] - lows[i]) / max(1e-9, opens[i]) > 0.035 for i in range(-3, 0))
                green_absorption = (closes[-1] >= opens[-1]) and (taker_buy_ratio >= qcfg["buy_flow"] or rsi15m < 32.0)
                red_rejection = (closes[-1] <= opens[-1]) and (taker_buy_ratio <= 0.475)

                decs = 8 if price < 0.001 else (6 if price < 1.0 else 4)
                market_scan[sym] = {
                    "price": price,
                    "sma20": round(sma20, decs),
                    "sma100": round(sma100, decs),
                    "sma200": round(sma200, decs),
                    "vwap24": round(vwap24, decs),
                    "vwap_dev_pct": round(vwap_dev_pct, 2),
                    "atr14": round(atr14, decs),
                    "rsi14": round(rsi14, 2),
                    "prev_rsi14": round(prev_rsi14, 2),
                    "rsi15m": round(rsi15m, 2),
                    "ret24_pct": round(ret24 * 100.0, 2),
                    "vol_ratio": round(vol_ratio, 2),
                    "taker_buy_ratio": round(taker_buy_ratio, 3),
                    "taker_buy_15m": round(taker_buy_15m, 3),
                    "futures_long_short_ratio": ls_ratio,
                    "oi_delta_15m_pct": oi_d15,
                    "oi_delta_1h_pct": oi_d1h,
                    "uptrend_ok": uptrend,
                    "news_shock_blocked": recent_shock or crowded_long_trap,
                    "short_shock_blocked": recent_shock or crowded_short_trap,
                    "green_absorption": green_absorption,
                    "red_rejection": red_rejection
                }
            except Exception:
                pass

        # Apply BTC Leader + VWAP + Open Interest Delta Confluence across all 4 Playbooks
        btc_sc = market_scan.get("BTCUSDC", {})
        btc_flow = max(btc_sc.get("taker_buy_ratio", 0.5), btc_sc.get("taker_buy_15m", 0.5))
        btc_above_sma20 = btc_sc.get("price", 0) >= btc_sc.get("sma20", 0)
        btc_not_dumping = (btc_flow >= 0.45) or btc_above_sma20
        btc_not_pumping = (btc_sc.get("taker_buy_ratio", 0.5) <= 0.56)

        for sym, sc in market_scan.items():
            if "atr14" not in sc:
                continue
            r_min = min(sc.get("rsi14", 50.0), sc.get("prev_rsi14", 50.0))
            r_15m = sc.get("rsi15m", 50.0)
            ls_r = sc.get("futures_long_short_ratio", 1.5)
            t_flow = sc.get("taker_buy_ratio", 0.5)
            t_flow15 = sc.get("taker_buy_15m", t_flow)
            vwap_dev = sc.get("vwap_dev_pct", 0.0)
            oi_15 = sc.get("oi_delta_15m_pct", 0.0)
            oi_1h = sc.get("oi_delta_1h_pct", 0.0)
            p_now = sc.get("price", 0.0)
            sma20_now = sc.get("sma20", 0.0)
            ret24_pct = sc.get("ret24_pct", 0.0)
            v_rat = sc.get("vol_ratio", 1.0)

            # Playbook 1: MEAN_REV_DIP (Oversold dip below VWAP + uncrowded longs + 24H Excursion <= -0.60% + Green Absorption)
            sc["dip_trigger"] = bool(
                (sym in LONG_ELIGIBLE_PAIRS)
                and btc_not_dumping
                and (not sc["news_shock_blocked"])
                and sc.get("green_absorption", False)
                and (-6.50 <= ret24_pct <= -0.60)
                and (vwap_dev <= 0.15)
                and ((r_min < 32.0) or (r_15m <= 26.0 and r_min < 36.0))
                and (ls_r < 3.00)
            )
            # Playbook 2: MOMENTUM_BREAKOUT (Requires Macro Uptrend SMA100>=SMA200 + Price > SMA20 + strong Taker Buy Flow)
            sc["breakout_trigger"] = bool(
                (sym in LONG_ELIGIBLE_PAIRS)
                and sc.get("uptrend_ok", False)
                and btc_above_sma20
                and (p_now > sma20_now > 0)
                and (-0.25 <= vwap_dev <= 2.20)
                and (oi_1h >= -1.00)
                and (52.0 <= sc.get("rsi14", 50.0) <= 70.0)
                and (max(t_flow, t_flow15) >= 0.54)
                and (ls_r < 2.90)
            )
            # Playbook 3: CROWDED_FADE_SHORT (1x Short when Retail LS Ratio >= 2.65 + Weak Taker Buy Flow + Anti-Squeeze OI Guard)
            sc["short_bounce_trigger"] = bool(
                (sym in SHORT_ELIGIBLE_PAIRS)
                and (not sc["uptrend_ok"])
                and (not sc.get("short_shock_blocked", False))
                and (ret24_pct <= 3.50)
                and (v_rat <= 4.0)
                and (ls_r >= 2.65)
                and (min(t_flow, t_flow15) <= 0.475)
                and (oi_15 <= 1.20)
            )
            # Playbook 4: 15M_FLOW_SCALP (Fast 15m Oversold Absorption Long below VWAP / Overbought Rejection Short)
            sc["scalp15m_long_trigger"] = bool(
                (sym in LONG_ELIGIBLE_PAIRS)
                and btc_not_dumping
                and (not sc["news_shock_blocked"])
                and (vwap_dev <= -0.10)
                and (r_15m <= 35.0)
                and (max(t_flow, t_flow15) >= 0.51)
                and (ls_r < 3.10)
            )
            sc["scalp15m_short_trigger"] = bool(
                (sym in SHORT_ELIGIBLE_PAIRS)
                and (not sc["uptrend_ok"])
                and (not sc.get("short_shock_blocked", False))
                and (r_15m >= 62.0 or (sc.get("rsi14", 50.0) >= 56.0 and ls_r >= 2.50))
                and (min(t_flow, t_flow15) <= 0.45)
                and (oi_15 <= 1.00)
                and (ret24_pct <= 3.50)
            )
            # Playbook 5: STAT_ARB_AND_FUNDING_SQUEEZE (Cross-Sectional Relative Strength Z-Score vs BTC + Extreme Funding Anomaly)
            btc_r24 = btc_sc.get("ret24_pct", 0.0)
            rel_vs_btc = round(ret24_pct - btc_r24, 2)
            fut_k = sym.replace("USDC", "USDT")
            fr_now = fut_intel.get("funding_rates", {}).get(fut_k, 0.0)
            sc["rel_strength_vs_btc_pct"] = rel_vs_btc
            sc["funding_rate_8h_pct"] = fr_now
            sc["stat_arb_long_trigger"] = bool(
                (sym in LONG_ELIGIBLE_PAIRS)
                and btc_not_dumping
                and (not sc["news_shock_blocked"])
                and (
                    (fr_now <= -0.020 and vwap_dev >= -0.30 and max(t_flow, t_flow15) >= 0.52)
                    or (rel_vs_btc <= -1.80 and r_15m <= 33.0 and max(t_flow, t_flow15) >= 0.52 and sc.get("uptrend_ok", False))
                )
            )
            sc["stat_arb_short_trigger"] = bool(
                (sym in SHORT_ELIGIBLE_PAIRS)
                and (not sc.get("short_shock_blocked", False))
                and (
                    (fr_now >= 0.025 and vwap_dev <= 0.20 and min(t_flow, t_flow15) <= 0.44)
                    or (rel_vs_btc >= 2.20 and r_15m >= 68.0 and min(t_flow, t_flow15) <= 0.44 and not sc.get("uptrend_ok", True))
                )
            )

        if "active_positions" not in sniper:
            sniper["active_positions"] = []
            if sniper.get("active_cycle"):
                sniper["active_positions"].append(sniper["active_cycle"])
        if "active_short_positions" not in sniper:
            sniper["active_short_positions"] = []
            if sniper.get("active_short_cycle"):
                sniper["active_short_positions"].append(sniper["active_short_cycle"])

        # 3A. Evaluate Open Crypto Longs Strictly on LIVE Real-Time Price (ZERO Past-Candle Replay) + 48h Stagnation Escape
        updated_longs = []
        for cyc in sniper["active_positions"]:
            sym = cyc["symbol"]
            sc_sym = market_scan.get(sym, {})
            cur_p = sc_sym.get("price")
            atr_now = sc_sym.get("atr14", 0.0)
            if cur_p is None:
                updated_longs.append(cyc)
                continue

            decs = 8 if cyc["avg_entry_price"] < 0.001 else (6 if cyc["avg_entry_price"] < 1.0 else 4)
            tp_price = cyc.get("tp_target_price", round(cyc["avg_entry_price"] * 1.0125, decs))
            sl_price = cyc.get("sl_price", round(cyc["avg_entry_price"] * 0.965, decs))
            pb_name = cyc.get("playbook", "MEAN_REV_DIP")

            opened_str = cyc.get("opened_at_utc", now_iso)
            try:
                op_dt = datetime.strptime(opened_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                held_h = (datetime.now(timezone.utc) - op_dt).total_seconds() / 3600.0
            except Exception:
                held_h = 0.0
            cyc["hours_held"] = round(held_h, 1)
            cyc.pop("last_replayed_15m_ms", None)

            # 48-Hour Stagnation Guardian + Counter-Trend Overbought Exhaustion Protection
            if held_h >= 48.0:
                if cur_p >= cyc["avg_entry_price"] * 1.002:
                    gross_val = round(cyc["total_qty"] * cur_p * (1.0 - 0.0005), 2)
                    pnl = round(gross_val - cyc["invested_usd"], 2)
                    sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + gross_val, 2)
                    sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                    sniper["cycles_won"] += 1
                    sniper["trade_history"].append({
                        "time_utc": now_iso,
                        "action": "STAGNATION_PROFIT_TAKE_LIVE",
                        "playbook": pb_name,
                        "symbol": sym,
                        "price": cur_p,
                        "pnl_usd": pnl
                    })
                    continue
                elif atr_now > 0:
                    # Always tighten SL on any >=48h stagnant position
                    sl_mult_stag = 0.85
                    # If in a macro downtrend and 1H RSI >= 68.0 with seller dominance (taker_buy_ratio <= 0.42), lock tightly!
                    if (not sc_sym.get("uptrend_ok", True)) and sc_sym.get("rsi14", 50.0) >= 68.0 and sc_sym.get("taker_buy_ratio", 0.5) <= 0.42:
                        sl_mult_stag = 0.45
                        reanchored_tp = round(cur_p + 1.2 * atr_now, decs)
                        if reanchored_tp < tp_price:
                            tp_price = reanchored_tp
                            cyc["tp_target_price"] = reanchored_tp
                            cyc["stagnation_tp_reanchored"] = True
                    tight_sl = round(cur_p - sl_mult_stag * atr_now, decs)
                    if tight_sl > sl_price:
                        sl_price = tight_sl
                        cyc["sl_price"] = tight_sl
                        cyc["stagnation_sl_tightened"] = True

            # Progressive Multi-Stage ATR Profit-Locking + Order-Flow Reversal Lock (OOS PF=12.80, WR=86.7%)
            if atr_now > 0 and cur_p >= cyc["avg_entry_price"] + 1.0 * atr_now:
                t_flow_now = sc_sym.get("taker_buy_ratio", 0.5)
                if cur_p >= cyc["avg_entry_price"] + 2.0 * atr_now:
                    lock_mult = 1.35 if (cur_p >= cyc["avg_entry_price"] + 2.2 * atr_now and t_flow_now < 0.48) else 0.90
                elif cur_p >= cyc["avg_entry_price"] + 1.8 * atr_now and t_flow_now <= float(qcfg.get("flow_rev_th", 0.38)):
                    lock_mult = float(qcfg.get("flow_rev_lock_atr", 1.0))
                elif cur_p >= cyc["avg_entry_price"] + 1.4 * atr_now:
                    lock_mult = 0.65
                else:
                    lock_mult = 0.25
                new_lock = round(cyc["avg_entry_price"] + lock_mult * atr_now, decs)
                cyc["breakeven_locked_price"] = max(cyc.get("breakeven_locked_price") or 0.0, new_lock)

            if cur_p >= tp_price:
                gross_val = round(cyc["total_qty"] * cur_p * (1.0 - 0.0005), 2)
                pnl = round(gross_val - cyc["invested_usd"], 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + gross_val, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                act_name = "TAKE_PROFIT_LIVE" if pnl >= 0 else "STAGNATION_REBOUND_EXIT_LIVE"
                if pnl >= 0:
                    sniper["cycles_won"] += 1
                else:
                    sniper["cycles_stopped"] += 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": act_name,
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"{act_name}:{sym}@${cur_p}:${pnl:+.2f}")
            elif cyc.get("breakeven_locked_price") and cur_p <= cyc["breakeven_locked_price"]:
                gross_val = round(cyc["total_qty"] * cur_p * (1.0 - 0.0005), 2)
                pnl = round(gross_val - cyc["invested_usd"], 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + gross_val, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_won"] += 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "TRAILING_PROFIT_LOCK_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"TRAILING_PROFIT_LOCK_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            elif cur_p <= sl_price:
                gross_val = round(cyc["total_qty"] * cur_p * (1.0 - 0.0005), 2)
                pnl = round(gross_val - cyc["invested_usd"], 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + gross_val, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_stopped"] += 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "QUANT_ATR_STOP_LOSS_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"QUANT_ATR_STOP_LOSS_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            elif held_h >= float(qcfg.get("max_bars", 96)):
                gross_val = round(cyc["total_qty"] * cur_p * (1.0 - 0.0005), 2)
                pnl = round(gross_val - cyc["invested_usd"], 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + gross_val, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                if pnl >= 0:
                    sniper["cycles_won"] += 1
                else:
                    sniper["cycles_stopped"] += 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "TIME_MAX_BARS_EXIT_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"TIME_MAX_BARS_EXIT_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            else:
                cyc["current_price"] = cur_p
                cyc["unrealized_pnl_usd"] = round(cyc["total_qty"] * cur_p - cyc["invested_usd"], 2)
                cyc["dist_to_sl_pct"] = round(((cur_p - sl_price) / cur_p) * 100.0, 2) if cur_p > 0 else 0.0
                cyc["dist_to_tp_pct"] = round(((tp_price - cur_p) / cur_p) * 100.0, 2) if cur_p > 0 else 0.0
                updated_longs.append(cyc)

        sniper["active_positions"] = updated_longs

        # 3B. Evaluate Open Crypto 1x Short Positions + 8-Hour Funding Accrual & Squeeze Protection
        updated_shorts = []
        for scyc in sniper["active_short_positions"]:
            sym = scyc["symbol"]
            sc_sym = market_scan.get(sym, {})
            cur_p = sc_sym.get("price")
            atr_now = sc_sym.get("atr14", 0.0)
            if cur_p is None:
                updated_shorts.append(scyc)
                continue
            entry_p = scyc["entry_price"]
            decs = 8 if entry_p < 0.001 else (6 if entry_p < 1.0 else 4)
            tp_price = scyc["tp_target_price"]
            sl_price = scyc["sl_price"]
            pb_name = scyc.get("playbook", "CROWDED_FADE_SHORT")

            opened_str = scyc.get("opened_at_utc", now_iso)
            try:
                op_dt = datetime.strptime(opened_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                op_ms = int(op_dt.timestamp() * 1000)
                held_h = (datetime.now(timezone.utc) - op_dt).total_seconds() / 3600.0
            except Exception:
                op_ms = last_ms
                held_h = 0.0
            scyc["hours_held"] = round(held_h, 1)

            fut_sym = sym.replace("USDC", "USDT")
            fr_8h = fut_intel.get("funding_rates", {}).get(fut_sym, 0.0)
            scyc["current_funding_rate_8h_pct"] = fr_8h

            # Accrue 8-hour funding settlements for 1x Short position (positive funding pays short)
            expected_settlements = int(held_h // 8.0)
            settled_count = scyc.get("funding_settlements_credited", 0)
            if expected_settlements > settled_count:
                new_events = expected_settlements - settled_count
                funding_credit = round(scyc["invested_usd"] * (fr_8h / 100.0) * new_events, 4)
                scyc["accrued_funding_usd"] = round(scyc.get("accrued_funding_usd", 0.0) + funding_credit, 4)
                scyc["funding_settlements_credited"] = expected_settlements
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + funding_credit, 4)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + funding_credit, 4)

            # Strictly LIVE Real-Time Execution for 1x Short Positions (ZERO Past-Candle Replay)
            scyc.pop("last_replayed_15m_ms", None)

            # Negative Funding Squeeze Guard: if 8h funding <= -0.03%, tighten short SL
            if fr_8h <= -0.03 and atr_now > 0:
                squeeze_sl = round(cur_p + 0.6 * atr_now, decs)
                if squeeze_sl < sl_price:
                    sl_price = squeeze_sl
                    scyc["sl_price"] = squeeze_sl

            # 48-Hour Stagnation Guardian for Short Positions
            if held_h >= 48.0:
                if cur_p <= entry_p * 0.998:
                    pnl_pct = ((entry_p - cur_p) / entry_p) - 0.001
                    pnl = round(scyc["invested_usd"] * pnl_pct, 2)
                    sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + scyc["invested_usd"] + pnl, 2)
                    sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                    sniper["cycles_won"] += 1
                    sniper["short_cycles_won"] = sniper.get("short_cycles_won", 0) + 1
                    sniper["trade_history"].append({
                        "time_utc": now_iso,
                        "action": "SHORT_1X_STAGNATION_PROFIT_TAKE_LIVE",
                        "playbook": pb_name,
                        "symbol": sym,
                        "price": cur_p,
                        "pnl_usd": pnl
                    })
                    continue
                elif atr_now > 0:
                    tight_sl = round(cur_p + 0.85 * atr_now, decs)
                    if tight_sl < sl_price:
                        sl_price = tight_sl
                        scyc["sl_price"] = tight_sl
                        scyc["stagnation_sl_tightened"] = True

            if atr_now > 0 and cur_p <= entry_p - 1.0 * atr_now:
                t_flow_now = sc_sym.get("taker_buy_ratio", 0.5)
                if cur_p <= entry_p - 2.0 * atr_now:
                    lock_mult = 1.35 if (cur_p <= entry_p - 2.2 * atr_now and t_flow_now > 0.52) else 0.90
                elif cur_p <= entry_p - 1.8 * atr_now and t_flow_now >= (1.0 - float(qcfg.get("flow_rev_th", 0.38))):
                    lock_mult = float(qcfg.get("flow_rev_lock_atr", 1.0))
                elif cur_p <= entry_p - 1.4 * atr_now:
                    lock_mult = 0.65
                else:
                    lock_mult = 0.25
                new_lock = round(entry_p - lock_mult * atr_now, decs)
                if scyc.get("breakeven_locked_price") is None or new_lock < scyc["breakeven_locked_price"]:
                    scyc["breakeven_locked_price"] = new_lock

            if cur_p <= tp_price:
                pnl_pct = ((entry_p - cur_p) / entry_p) - 0.001
                pnl = round(scyc["invested_usd"] * pnl_pct, 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + scyc["invested_usd"] + pnl, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_won"] += 1
                sniper["short_cycles_won"] = sniper.get("short_cycles_won", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "SHORT_1X_TAKE_PROFIT_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"SHORT_1X_TAKE_PROFIT_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            elif scyc.get("breakeven_locked_price") and cur_p >= scyc["breakeven_locked_price"]:
                pnl_pct = ((entry_p - cur_p) / entry_p) - 0.001
                pnl = round(scyc["invested_usd"] * pnl_pct, 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + scyc["invested_usd"] + pnl, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_won"] += 1
                sniper["short_cycles_won"] = sniper.get("short_cycles_won", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "SHORT_1X_TRAILING_PROFIT_LOCK_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"SHORT_1X_TRAILING_PROFIT_LOCK_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            elif cur_p >= sl_price:
                pnl_pct = ((entry_p - cur_p) / entry_p) - 0.001
                pnl = round(scyc["invested_usd"] * pnl_pct, 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + scyc["invested_usd"] + pnl, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_stopped"] += 1
                sniper["short_cycles_stopped"] = sniper.get("short_cycles_stopped", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "SHORT_1X_PROTECTIVE_STOP_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"SHORT_1X_PROTECTIVE_STOP_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            elif held_h >= float(qcfg.get("max_bars", 96)):
                pnl_pct = ((entry_p - cur_p) / entry_p) - 0.001
                pnl = round(scyc["invested_usd"] * pnl_pct, 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + scyc["invested_usd"] + pnl, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                if pnl >= 0:
                    sniper["cycles_won"] += 1
                    sniper["short_cycles_won"] = sniper.get("short_cycles_won", 0) + 1
                else:
                    sniper["cycles_stopped"] += 1
                    sniper["short_cycles_stopped"] = sniper.get("short_cycles_stopped", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "SHORT_1X_TIME_MAX_BARS_EXIT_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"SHORT_1X_TIME_MAX_BARS_EXIT_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            else:
                scyc["current_price"] = cur_p
                scyc["unrealized_pnl_usd"] = round(scyc["invested_usd"] * ((entry_p - cur_p) / entry_p), 2)
                scyc["dist_to_sl_pct"] = round(((sl_price - cur_p) / cur_p) * 100.0, 2) if cur_p > 0 else 0.0
                scyc["dist_to_tp_pct"] = round(((cur_p - tp_price) / cur_p) * 100.0, 2) if cur_p > 0 else 0.0
                updated_shorts.append(scyc)

        sniper["active_short_positions"] = updated_shorts

        # 3C. Open NEW Multi-Slot Crypto Trades Across the Competing Playbooks (with Post-Exit / Post-SL Cooldown Guard)
        held_long_syms = {p["symbol"] for p in sniper["active_positions"]}
        held_short_syms = {p["symbol"] for p in sniper["active_short_positions"]}
        cooldown_crypto_syms = set()
        now_dt_cd = datetime.now(timezone.utc)
        for th in sniper.get("trade_history", [])[-40:]:
            act_th = th.get("action", "")
            if any(k in act_th for k in ("STOP", "PROFIT", "EXIT", "LOCK", "UNLOAD")):
                try:
                    t_dt = datetime.strptime(th["time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    age_h = (now_dt_cd - t_dt).total_seconds() / 3600.0
                    cd_req = 6.0 if ("STOP" in act_th or float(th.get("pnl_usd", 0.0)) < 0) else 2.0
                    if age_h < cd_req:
                        cooldown_crypto_syms.add(th.get("symbol"))
                except Exception:
                    pass

        for sym in LONG_ELIGIBLE_PAIRS:
            if len(sniper["active_positions"]) >= 5 or sniper["free_cash_usd"] < 60.0:
                break
            if sym in held_long_syms or sym in held_short_syms or sym in cooldown_crypto_syms:
                continue
            sc = market_scan.get(sym, {})
            atr = sc.get("atr14", 0.0)
            p_close = sc.get("price", 0.0)
            if atr <= 0 or p_close <= 0:
                continue

            pb_chosen = None
            sl_m, tp_m = qcfg.get("sl_atr", 1.8), qcfg.get("tp_atr", 4.0)
            if sc.get("dip_trigger"):
                pb_chosen = "MEAN_REV_DIP"
            elif sc.get("stat_arb_long_trigger"):
                pb_chosen = "STAT_ARB_AND_FUNDING_SQUEEZE"
                sl_m, tp_m = 1.5, 3.0
            elif sc.get("breakout_trigger"):
                pb_chosen = "MOMENTUM_BREAKOUT"
                sl_m, tp_m = 1.5, 2.8
            elif sc.get("scalp15m_long_trigger"):
                pb_chosen = "15M_FLOW_SCALP"
                sl_m, tp_m = 1.4, 2.5

            if pb_chosen:
                stop_pct = max(0.005, (sl_m * atr) / p_close)
                if stop_pct <= 0.045:
                    decs = 8 if p_close < 0.001 else (6 if p_close < 1.0 else 4)
                    amt = round(min(sniper["free_cash_usd"], 190.0, 3.40 / stop_pct), 2)
                    if amt >= 50.0:
                        qty = round((amt * (1.0 - 0.0005)) / p_close, 2 if p_close < 0.001 else 6)
                        sniper["free_cash_usd"] = round(sniper["free_cash_usd"] - amt, 2)
                        new_pos = {
                            "symbol": sym,
                            "playbook": pb_chosen,
                            "opened_at_utc": now_iso,
                            "last_replayed_15m_ms": now_ms,
                            "tiers_filled": 1,
                            "first_entry_price": p_close,
                            "avg_entry_price": p_close,
                            "invested_usd": amt,
                            "total_qty": qty,
                            "tp_target_price": round(p_close + tp_m * atr, decs),
                            "sl_price": round(p_close - sl_m * atr, decs)
                        }
                        sniper["active_positions"].append(new_pos)
                        held_long_syms.add(sym)
                        sniper["trade_history"].append({
                            "time_utc": now_iso,
                            "action": f"OPEN_{pb_chosen}_LIVE",
                            "playbook": pb_chosen,
                            "symbol": sym,
                            "price": p_close,
                            "rsi14": sc.get("rsi14"),
                            "rsi15m": sc.get("rsi15m"),
                            "taker_buy_ratio": sc.get("taker_buy_ratio"),
                            "ls_ratio": sc.get("futures_long_short_ratio"),
                            "amount_usd": amt
                        })
                        cycle_actions.append(f"OPEN_LONG:{pb_chosen}:{sym}@${p_close}")

        for sym in SHORT_ELIGIBLE_PAIRS:
            if len(sniper["active_short_positions"]) >= 4 or sniper["free_cash_usd"] < 60.0:
                break
            if sym in held_short_syms or sym in held_long_syms or sym in cooldown_crypto_syms:
                continue
            sc = market_scan.get(sym, {})
            atr = sc.get("atr14", 0.0)
            p_close = sc.get("price", 0.0)
            short_pb = (
                "CROWDED_FADE_SHORT" if sc.get("short_bounce_trigger")
                else ("STAT_ARB_AND_FUNDING_SQUEEZE" if sc.get("stat_arb_short_trigger")
                      else ("15M_FLOW_SCALP" if sc.get("scalp15m_short_trigger") else None))
            )
            if short_pb and atr > 0 and p_close > 0:
                sl_m, tp_m = (1.4, 2.5) if short_pb == "15M_FLOW_SCALP" else (1.6, 2.9)
                stop_pct = max(0.005, (sl_m * atr) / p_close)
                if stop_pct <= 0.045:
                    decs = 8 if p_close < 0.001 else (6 if p_close < 1.0 else 4)
                    amt = round(min(sniper["free_cash_usd"], 180.0, 3.20 / stop_pct), 2)
                    if amt >= 50.0:
                        qty = round((amt * (1.0 - 0.0005)) / p_close, 2 if p_close < 0.001 else 6)
                        sniper["free_cash_usd"] = round(sniper["free_cash_usd"] - amt, 2)
                        new_spos = {
                            "symbol": sym,
                            "playbook": short_pb,
                            "direction": "1X_SHORT",
                            "opened_at_utc": now_iso,
                            "last_replayed_15m_ms": now_ms,
                            "entry_price": p_close,
                            "invested_usd": amt,
                            "total_qty": qty,
                            "tp_target_price": round(p_close - tp_m * atr, decs),
                            "sl_price": round(p_close + sl_m * atr, decs),
                            "breakeven_locked_price": None,
                            "accrued_funding_usd": 0.0,
                            "funding_settlements_credited": 0
                        }
                        sniper["active_short_positions"].append(new_spos)
                        held_short_syms.add(sym)
                        sniper["trade_history"].append({
                            "time_utc": now_iso,
                            "action": f"OPEN_{short_pb}_1X_SHORT_LIVE",
                            "playbook": short_pb,
                            "symbol": sym,
                            "price": p_close,
                            "rsi14": sc.get("rsi14"),
                            "taker_buy_ratio": sc.get("taker_buy_ratio"),
                            "ls_ratio": sc.get("futures_long_short_ratio"),
                            "amount_usd": amt
                        })
                        cycle_actions.append(f"OPEN_SHORT:{short_pb}:{sym}@${p_close}")

        sniper["active_cycle"] = sniper["active_positions"][0] if sniper["active_positions"] else None
        sniper["active_short_cycle"] = sniper["active_short_positions"][0] if sniper["active_short_positions"] else None

        # 5. Update Engine 4 (US Stock Market & Global Macro/Commodity Multi-Slot Quant Guardian + QQQ Regime Shield)
        self._ensure_stocks_engine(now_iso, now_ms)
        stocks = self.state["engine4_us_stocks"]
        for macro_tk in ["AAPL", "MSFT", "NVDA", "SPY", "QQQ", "AMZN", "GOOGL", "META", "AMD", "TSLA", "AVGO", "SMH", "GLD", "SLV", "XLE", "TLT"]:
            if macro_tk not in stocks["watched_tickers"]:
                stocks["watched_tickers"].append(macro_tk)
        stock_last_ms = stocks.get("last_updated_ms", last_ms)
        stock_elapsed_h = max(0.0, (now_ms - stock_last_ms) / 3600000.0)
        treasury_inc = stocks["free_cash_usd"] * (stocks["treasury_yield_apr_pct"] / 100.0) * (stock_elapsed_h / (365.0 * 24.0))
        stocks["accrued_treasury_yield_usd"] = round(stocks["accrued_treasury_yield_usd"] + treasury_inc, 5)
        stocks["last_updated_ms"] = now_ms

        stock_scan = stocks.get("latest_stock_scan", {})
        for ticker in stocks["watched_tickers"]:
            try:
                y_data = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1h&range=60d", timeout=7.0)
                res_y = y_data["chart"]["result"][0]
                ts_list = res_y["timestamp"]
                q = res_y["indicators"]["quote"][0]
                valid = [
                    (int(ts_list[i]) * 1000, float(q["open"][i]), float(q["high"][i]), float(q["low"][i]), float(q["close"][i]))
                    for i in range(len(ts_list))
                    if q["close"][i] is not None and q["high"][i] is not None and q["low"][i] is not None and q["open"][i] is not None
                ]
                if len(valid) >= 200:
                    opens = [v[1] for v in valid]
                    highs = [v[2] for v in valid]
                    lows = [v[3] for v in valid]
                    closes = [v[4] for v in valid]
                    price = round(closes[-1], 2)
                    sma20 = sum(closes[-20:]) / 20.0
                    sma100 = sum(closes[-100:]) / 100.0
                    sma200 = sum(closes[-200:]) / 200.0
                    ret24 = ((price - closes[-14]) / closes[-14]) if len(closes) >= 14 else 0.0
                    rsi14 = calc_rsi(closes, 14)
                    prev_rsi14 = calc_rsi(closes[:-1], 14)
                    atr14 = calc_atr(highs, lows, closes, 14)
                    uptrend = sma100 >= sma200
                    green_reversal = (closes[-1] >= opens[-1]) or (rsi14 > prev_rsi14)
                    recent_stock_shock = any((v[2] - v[3]) / max(1e-9, v[1]) > 0.032 for v in valid[-4:]) or (abs(closes[-1] - closes[-4]) / max(1e-9, closes[-4]) > 0.04)
                    stock_scan[ticker] = {
                        "price": price,
                        "sma20": round(sma20, 2),
                        "sma100": round(sma100, 2),
                        "sma200": round(sma200, 2),
                        "atr14": round(atr14, 2),
                        "rsi14": round(rsi14, 2),
                        "prev_rsi14": round(prev_rsi14, 2),
                        "ret24_pct": round(ret24 * 100.0, 2),
                        "uptrend_ok": uptrend,
                        "green_reversal": green_reversal,
                        "news_shock_blocked": recent_stock_shock
                    }
            except Exception:
                pass

        qqq_sc = stock_scan.get("QQQ", {})
        spy_sc = stock_scan.get("SPY", {})
        qqq_rsi = qqq_sc.get("rsi14", 50.0)
        spy_rsi = spy_sc.get("rsi14", 50.0)
        qqq_uptrend = qqq_sc.get("uptrend_ok", True)
        qqq_green = qqq_sc.get("green_reversal", True)
        index_freefall = (qqq_rsi < 18.0) or (spy_rsi < 18.0)

        for ticker, sscan in stock_scan.items():
            r_min = min(sscan.get("rsi14", 50.0), sscan.get("prev_rsi14", 50.0))
            uptrend_dip = sscan.get("uptrend_ok", False) and (r_min < 35.0)
            extreme_washout_bounce = (r_min < 22.0) and qqq_green and (sscan.get("rsi14", 50.0) > sscan.get("prev_rsi14", 50.0))
            sscan["dip_trigger"] = bool(
                (not sscan.get("news_shock_blocked", False))
                and (not index_freefall)
                and (uptrend_dip or extreme_washout_bounce)
            )
        stocks["latest_stock_scan"] = stock_scan

        # 5A. Evaluate All Open US Stock Positions + QQQ Regime Shield & 48h Target Re-Anchoring
        updated_stocks = []
        for scyc in stocks["active_positions"]:
            tk = scyc["symbol"]
            sc_tk = stock_scan.get(tk, {})
            cur_sp = sc_tk.get("price")
            if cur_sp is None:
                updated_stocks.append(scyc)
                continue

            opened_str = scyc.get("opened_at_utc", now_iso)
            now_dt_utc = datetime.now(timezone.utc)
            is_us_weekend = now_dt_utc.weekday() in (5, 6)
            try:
                op_dt = datetime.strptime(opened_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                held_h = (now_dt_utc - op_dt).total_seconds() / 3600.0
                active_mkt_h = calc_active_stock_hours(op_dt, now_dt_utc)
            except Exception:
                held_h = 0.0
                active_mkt_h = 0.0
            scyc["hours_held"] = round(held_h, 1)
            scyc["active_market_hours_held"] = active_mkt_h
            atr_st = sc_tk.get("atr14", 1.5)

            # QQQ Macro Downtrend Shield for Counter-Trend Washout Stocks (e.g., NVDA)
            if (not qqq_uptrend) and (not sc_tk.get("uptrend_ok", True)) and atr_st > 0:
                regime_sl = round(scyc["avg_entry_price"] - 1.5 * atr_st, 2)
                regime_tp = round(scyc["avg_entry_price"] + 2.4 * atr_st, 2)
                if regime_sl > scyc.get("sl_price", 0.0) and regime_sl < cur_sp:
                    scyc["sl_price"] = regime_sl
                    scyc["qqq_regime_sl_tightened"] = True
                if regime_tp < scyc.get("tp_target_price", 999999.0) and regime_tp > scyc["avg_entry_price"]:
                    scyc["tp_target_price"] = regime_tp

            # 48-Hour Active Market Stagnation Guardian for US Equities (Excludes Weekend Market-Closed Freeze)
            if active_mkt_h >= 48.0:
                if cur_sp >= scyc["avg_entry_price"] * 1.002:
                    gross_val = round(scyc["total_qty"] * cur_sp, 2)
                    pnl = round(gross_val - scyc["invested_usd"], 2)
                    stocks["free_cash_usd"] = round(stocks["free_cash_usd"] + gross_val, 2)
                    stocks["realized_pnl_usd"] = round(stocks["realized_pnl_usd"] + pnl, 2)
                    stocks["cycles_won"] += 1
                    stocks["trade_history"].append({
                        "time_utc": now_iso,
                        "action": "STAGNATION_PROFIT_TAKE_LIVE",
                        "playbook": "US_STOCK_PULLBACK",
                        "symbol": tk,
                        "price": cur_sp,
                        "pnl_usd": pnl
                    })
                    continue
                else:
                    tight_sl = round(cur_sp - 1.2 * atr_st, 2)
                    if tight_sl > scyc.get("sl_price", 0.0):
                        scyc["sl_price"] = tight_sl
                        scyc["stagnation_sl_tightened"] = True
                    re_tp = max(round(scyc["avg_entry_price"] * 1.003, 2), round(cur_sp + 1.8 * atr_st, 2))
                    if re_tp < scyc.get("tp_target_price", 999999.0):
                        scyc["tp_target_price"] = re_tp
                        scyc["stagnation_tp_reanchored"] = True
            if cur_sp >= scyc["avg_entry_price"] * 1.008:
                scyc["breakeven_locked_price"] = round(scyc["avg_entry_price"] * 1.0035, 2)
            if cur_sp >= scyc["tp_target_price"]:
                gross_val = round(scyc["total_qty"] * cur_sp, 2)
                pnl = round(gross_val - scyc["invested_usd"], 2)
                stocks["free_cash_usd"] = round(stocks["free_cash_usd"] + gross_val, 2)
                stocks["realized_pnl_usd"] = round(stocks["realized_pnl_usd"] + pnl, 2)
                stocks["cycles_won"] += 1
                stocks["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "TAKE_PROFIT_LIVE",
                    "playbook": "US_STOCK_PULLBACK",
                    "symbol": tk,
                    "price": cur_sp,
                    "pnl_usd": pnl
                })
            elif scyc.get("breakeven_locked_price") and cur_sp <= scyc["breakeven_locked_price"]:
                gross_val = round(scyc["total_qty"] * cur_sp, 2)
                pnl = round(gross_val - scyc["invested_usd"], 2)
                stocks["free_cash_usd"] = round(stocks["free_cash_usd"] + gross_val, 2)
                stocks["realized_pnl_usd"] = round(stocks["realized_pnl_usd"] + pnl, 2)
                stocks["cycles_won"] += 1
                stocks["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "TRAILING_PROFIT_LOCK_LIVE",
                    "playbook": "US_STOCK_PULLBACK",
                    "symbol": tk,
                    "price": cur_sp,
                    "pnl_usd": pnl
                })
            elif cur_sp <= scyc["sl_price"]:
                gross_val = round(scyc["total_qty"] * cur_sp, 2)
                pnl = round(gross_val - scyc["invested_usd"], 2)
                stocks["free_cash_usd"] = round(stocks["free_cash_usd"] + gross_val, 2)
                stocks["realized_pnl_usd"] = round(stocks["realized_pnl_usd"] + pnl, 2)
                stocks["cycles_stopped"] += 1
                stocks["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "STOP_LOSS_LIVE",
                    "playbook": "US_STOCK_PULLBACK",
                    "symbol": tk,
                    "price": cur_sp,
                    "pnl_usd": pnl
                })
            elif (not is_us_weekend) and active_mkt_h >= 96.0:
                gross_val = round(scyc["total_qty"] * cur_sp, 2)
                pnl = round(gross_val - scyc["invested_usd"], 2)
                stocks["free_cash_usd"] = round(stocks["free_cash_usd"] + gross_val, 2)
                stocks["realized_pnl_usd"] = round(stocks["realized_pnl_usd"] + pnl, 2)
                if pnl >= 0:
                    stocks["cycles_won"] += 1
                else:
                    stocks["cycles_stopped"] += 1
                stocks["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "STOCK_TIME_MAX_BARS_EXIT_LIVE",
                    "playbook": "US_STOCK_PULLBACK",
                    "symbol": tk,
                    "price": cur_sp,
                    "pnl_usd": pnl
                })
            else:
                scyc["current_price"] = cur_sp
                scyc["unrealized_pnl_usd"] = round(scyc["total_qty"] * cur_sp - scyc["invested_usd"], 2)
                scyc["dist_to_sl_pct"] = round(((cur_sp - scyc["sl_price"]) / cur_sp) * 100.0, 2) if cur_sp > 0 else 0.0
                scyc["dist_to_tp_pct"] = round(((scyc["tp_target_price"] - cur_sp) / cur_sp) * 100.0, 2) if cur_sp > 0 else 0.0
                updated_stocks.append(scyc)

        stocks["active_positions"] = updated_stocks

        # 5B. Open NEW Multi-Slot US Stock Positions (Up to 3 Concurrent Stocks — Only When US Market Is Not in Weekend Freeze + Post-SL Cooldown)
        is_us_weekend_now = datetime.now(timezone.utc).weekday() in (5, 6)
        held_tickers = {p["symbol"] for p in stocks["active_positions"]}
        cooldown_stock_tickers = set()
        now_dt_scd = datetime.now(timezone.utc)
        for sth in stocks.get("trade_history", [])[-25:]:
            sact = sth.get("action", "")
            if any(k in sact for k in ("STOP", "PROFIT", "EXIT", "LOCK")):
                try:
                    st_dt = datetime.strptime(sth["time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    sage_h = (now_dt_scd - st_dt).total_seconds() / 3600.0
                    scd_req = 6.0 if ("STOP" in sact or float(sth.get("pnl_usd", 0.0)) < 0) else 2.0
                    if sage_h < scd_req:
                        cooldown_stock_tickers.add(sth.get("symbol"))
                except Exception:
                    pass
        for tk in stocks["watched_tickers"]:
            if is_us_weekend_now or len(stocks["active_positions"]) >= 3 or stocks["free_cash_usd"] < 150.0:
                break
            if tk in held_tickers or tk in cooldown_stock_tickers:
                continue
            sscan = stock_scan.get(tk, {})
            atr = sscan.get("atr14", 0.0)
            ep = sscan.get("price", 0.0)
            if sscan.get("dip_trigger") and atr > 0 and ep > 0:
                sl_m_st = 2.2 if (qqq_uptrend and sscan.get("uptrend_ok", False)) else 1.5
                tp_m_st = 3.5 if (qqq_uptrend and sscan.get("uptrend_ok", False)) else 2.4
                stop_pct = max(0.008, (sl_m_st * atr) / ep)
                amt = round(min(stocks["free_cash_usd"], 250.0, 2.80 / stop_pct), 2)
                if amt >= 100.0:
                    qty = round(amt / ep, 5)
                    stocks["free_cash_usd"] = round(stocks["free_cash_usd"] - amt, 2)
                    new_st = {
                        "symbol": tk,
                        "playbook": "US_STOCK_PULLBACK",
                        "opened_at_utc": now_iso,
                        "first_entry_price": ep,
                        "avg_entry_price": ep,
                        "invested_usd": amt,
                        "total_qty": qty,
                        "tp_target_price": round(ep + tp_m_st * atr, 2),
                        "sl_price": round(ep - sl_m_st * atr, 2)
                    }
                    stocks["active_positions"].append(new_st)
                    held_tickers.add(tk)
                    stocks["trade_history"].append({
                        "time_utc": now_iso,
                        "action": "OPEN_US_STOCK_PULLBACK_LIVE",
                        "playbook": "US_STOCK_PULLBACK",
                        "symbol": tk,
                        "price": ep,
                        "rsi14": sscan["rsi14"],
                        "uptrend_ok": sscan.get("uptrend_ok"),
                        "amount_usd": amt
                    })

        stocks["active_cycle"] = stocks["active_positions"][0] if stocks["active_positions"] else None

        stock_pos_val = 0.0
        for scyc in stocks["active_positions"]:
            tk = scyc["symbol"]
            cur_sp = stock_scan.get(tk, {}).get("price", scyc["avg_entry_price"])
            stock_pos_val += round(scyc["total_qty"] * cur_sp, 2)
        stocks["current_stock_equity_usd"] = round(
            stocks["free_cash_usd"] + stock_pos_val + stocks["accrued_treasury_yield_usd"], 2
        )
        stocks["net_stock_pnl_usd"] = round(stocks["current_stock_equity_usd"] - stocks["initial_capital_usd"], 2)

        self.state["last_updated_utc"] = now_iso
        self.state["last_updated_ms"] = now_ms
        self.state["latest_market_scan"] = market_scan
        self.state["strategy_scoreboard"] = self._compute_playbook_scoreboard()

        sniper_val = sniper["free_cash_usd"]
        for cyc in sniper["active_positions"]:
            sym = cyc["symbol"]
            cur_p = market_scan.get(sym, {}).get("price", cyc["avg_entry_price"])
            sniper_val += round(cyc["total_qty"] * cur_p, 2)
        for scyc in sniper["active_short_positions"]:
            sym = scyc["symbol"]
            cur_p = market_scan.get(sym, {}).get("price", scyc["entry_price"])
            unrealized_short_pnl = scyc["invested_usd"] * ((scyc["entry_price"] - cur_p) / scyc["entry_price"])
            sniper_val += round(scyc["invested_usd"] + unrealized_short_pnl, 2)

        total_equity = round(
            arb["allocated_usd"] - arb["entry_fees_paid_usd"] + arb["funding_collected_usd"]
            + sniper_val
            + res["allocated_usd"] + res["accrued_earn_usd"],
            2
        )
        self.state["current_total_equity_usd"] = total_equity
        self.state["net_pnl_usd"] = round(total_equity - self.initial_capital, 2)
        self.state["last_cycle_delta"] = {
            "timestamp_utc": now_iso,
            "equity_before_usd": prev_equity,
            "equity_after_usd": total_equity,
            "realized_pnl_before_usd": round(prev_realized, 2),
            "realized_pnl_after_usd": round(sniper["realized_pnl_usd"], 2),
            "actions_executed": cycle_actions
        }
        self._save()
        return self.state

    def live_realtime_tick(self):
        """
        Ultra-Fast Live Real-Time Order-Book Tick (runs every 2 seconds):
        Fetches live Bid/Ask prices for all Binance pairs in a single ~100ms HTTP request
        and executes Take-Profit, Stop-Loss, and Trailing Profit Locks INSTANTANEOUSLY
        at the live real-time price (cur_p) at the exact current second (now_iso).
        ZERO past-candle or retroactive execution.
        """
        now_ms = int(time.time() * 1000)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sniper = self.state.get("engine2_spot_sniper", {})
        arb = self.state.get("engine1_arbitrage", {})
        res = self.state.get("engine3_flash_reserve", {})
        market_scan = self.state.get("latest_market_scan", {})
        qcfg = load_validated_quant_config()
        prev_equity = self.state.get("current_total_equity_usd", 1000.0)
        prev_realized = sniper.get("realized_pnl_usd", 0.0)
        live_actions = []

        try:
            book_list = http_json("https://api.binance.com/api/v3/ticker/bookTicker", timeout=3.5, retries=2)
            live_prices = {}
            for item in book_list:
                s = item.get("symbol")
                if not s:
                    continue
                bid_p = float(item.get("bidPrice") or 0.0)
                ask_p = float(item.get("askPrice") or 0.0)
                if bid_p > 0 and ask_p > 0:
                    live_prices[s] = {"bid": bid_p, "ask": ask_p, "mid": (bid_p + ask_p) / 2.0}
        except Exception:
            return {"actions_executed": [], "status": "ticker_timeout"}

        updated_longs = []
        for cyc in sniper.get("active_positions", []):
            sym = cyc["symbol"]
            lp = live_prices.get(sym)
            if not lp:
                updated_longs.append(cyc)
                continue
            cur_p = lp["bid"]  # Exact live executable bid price for selling a Long position right now
            sc_sym = market_scan.get(sym, {})
            atr_now = sc_sym.get("atr14", 0.0)
            decs = 8 if cyc["avg_entry_price"] < 0.001 else (6 if cyc["avg_entry_price"] < 1.0 else 4)
            tp_price = cyc.get("tp_target_price", round(cyc["avg_entry_price"] * 1.0125, decs))
            sl_price = cyc.get("sl_price", round(cyc["avg_entry_price"] * 0.965, decs))
            pb_name = cyc.get("playbook", "MEAN_REV_DIP")

            if atr_now > 0 and cur_p >= cyc["avg_entry_price"] + 1.0 * atr_now:
                t_flow_now = sc_sym.get("taker_buy_ratio", 0.5)
                if cur_p >= cyc["avg_entry_price"] + 2.0 * atr_now:
                    lock_mult = 1.35 if (cur_p >= cyc["avg_entry_price"] + 2.2 * atr_now and t_flow_now < 0.48) else 0.90
                elif cur_p >= cyc["avg_entry_price"] + 1.8 * atr_now and t_flow_now <= float(qcfg.get("flow_rev_th", 0.38)):
                    lock_mult = float(qcfg.get("flow_rev_lock_atr", 1.0))
                elif cur_p >= cyc["avg_entry_price"] + 1.4 * atr_now:
                    lock_mult = 0.65
                else:
                    lock_mult = 0.25
                new_lock = round(cyc["avg_entry_price"] + lock_mult * atr_now, decs)
                cyc["breakeven_locked_price"] = max(cyc.get("breakeven_locked_price") or 0.0, new_lock)

            if cur_p >= tp_price:
                gross_val = round(cyc["total_qty"] * cur_p * (1.0 - 0.0005), 2)
                pnl = round(gross_val - cyc["invested_usd"], 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + gross_val, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_won"] = sniper.get("cycles_won", 0) + (1 if pnl >= 0 else 0)
                sniper["cycles_stopped"] = sniper.get("cycles_stopped", 0) + (0 if pnl >= 0 else 1)
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "TAKE_PROFIT_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                live_actions.append(f"TAKE_PROFIT_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            elif cyc.get("breakeven_locked_price") and cur_p <= cyc["breakeven_locked_price"]:
                gross_val = round(cyc["total_qty"] * cur_p * (1.0 - 0.0005), 2)
                pnl = round(gross_val - cyc["invested_usd"], 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + gross_val, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_won"] = sniper.get("cycles_won", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "TRAILING_PROFIT_LOCK_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                live_actions.append(f"TRAILING_PROFIT_LOCK_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            elif cur_p <= sl_price:
                gross_val = round(cyc["total_qty"] * cur_p * (1.0 - 0.0005), 2)
                pnl = round(gross_val - cyc["invested_usd"], 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + gross_val, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_stopped"] = sniper.get("cycles_stopped", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "QUANT_ATR_STOP_LOSS_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                live_actions.append(f"QUANT_ATR_STOP_LOSS_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            else:
                cyc["current_price"] = cur_p
                cyc["unrealized_pnl_usd"] = round(cyc["total_qty"] * cur_p - cyc["invested_usd"], 2)
                cyc["dist_to_sl_pct"] = round(((cur_p - sl_price) / cur_p) * 100.0, 2) if cur_p > 0 else 0.0
                cyc["dist_to_tp_pct"] = round(((tp_price - cur_p) / cur_p) * 100.0, 2) if cur_p > 0 else 0.0
                updated_longs.append(cyc)

        sniper["active_positions"] = updated_longs

        updated_shorts = []
        for scyc in sniper.get("active_short_positions", []):
            sym = scyc["symbol"]
            lp = live_prices.get(sym)
            if not lp:
                updated_shorts.append(scyc)
                continue
            cur_p = lp["ask"]  # Exact live executable ask price for covering a Short position right now
            sc_sym = market_scan.get(sym, {})
            atr_now = sc_sym.get("atr14", 0.0)
            entry_p = scyc["entry_price"]
            decs = 8 if entry_p < 0.001 else (6 if entry_p < 1.0 else 4)
            tp_price = scyc["tp_target_price"]
            sl_price = scyc["sl_price"]
            pb_name = scyc.get("playbook", "CROWDED_FADE_SHORT")

            if atr_now > 0 and cur_p <= entry_p - 1.0 * atr_now:
                t_flow_now = sc_sym.get("taker_buy_ratio", 0.5)
                if cur_p <= entry_p - 2.0 * atr_now:
                    lock_mult = 1.35 if (cur_p <= entry_p - 2.2 * atr_now and t_flow_now > 0.52) else 0.90
                elif cur_p <= entry_p - 1.8 * atr_now and t_flow_now >= (1.0 - float(qcfg.get("flow_rev_th", 0.38))):
                    lock_mult = float(qcfg.get("flow_rev_lock_atr", 1.0))
                elif cur_p <= entry_p - 1.4 * atr_now:
                    lock_mult = 0.65
                else:
                    lock_mult = 0.25
                new_lock = round(entry_p - lock_mult * atr_now, decs)
                if scyc.get("breakeven_locked_price") is None or new_lock < scyc["breakeven_locked_price"]:
                    scyc["breakeven_locked_price"] = new_lock

            if cur_p <= tp_price:
                pnl_pct = ((entry_p - cur_p) / entry_p) - 0.001
                pnl = round(scyc["invested_usd"] * pnl_pct, 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + scyc["invested_usd"] + pnl, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_won"] = sniper.get("cycles_won", 0) + 1
                sniper["short_cycles_won"] = sniper.get("short_cycles_won", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "SHORT_1X_TAKE_PROFIT_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                live_actions.append(f"SHORT_1X_TAKE_PROFIT_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            elif scyc.get("breakeven_locked_price") and cur_p >= scyc["breakeven_locked_price"]:
                pnl_pct = ((entry_p - cur_p) / entry_p) - 0.001
                pnl = round(scyc["invested_usd"] * pnl_pct, 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + scyc["invested_usd"] + pnl, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_won"] = sniper.get("cycles_won", 0) + 1
                sniper["short_cycles_won"] = sniper.get("short_cycles_won", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "SHORT_1X_TRAILING_PROFIT_LOCK_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                live_actions.append(f"SHORT_1X_TRAILING_PROFIT_LOCK_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            elif cur_p >= sl_price:
                pnl_pct = ((entry_p - cur_p) / entry_p) - 0.001
                pnl = round(scyc["invested_usd"] * pnl_pct, 2)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + scyc["invested_usd"] + pnl, 2)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                sniper["cycles_stopped"] = sniper.get("cycles_stopped", 0) + 1
                sniper["short_cycles_stopped"] = sniper.get("short_cycles_stopped", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "SHORT_1X_PROTECTIVE_STOP_LIVE",
                    "playbook": pb_name,
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                live_actions.append(f"SHORT_1X_PROTECTIVE_STOP_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
            else:
                scyc["current_price"] = cur_p
                scyc["unrealized_pnl_usd"] = round(scyc["invested_usd"] * ((entry_p - cur_p) / entry_p), 2)
                scyc["dist_to_sl_pct"] = round(((sl_price - cur_p) / cur_p) * 100.0, 2) if cur_p > 0 else 0.0
                scyc["dist_to_tp_pct"] = round(((cur_p - tp_price) / cur_p) * 100.0, 2) if cur_p > 0 else 0.0
                updated_shorts.append(scyc)

        sniper["active_short_positions"] = updated_shorts
        sniper["active_cycle"] = sniper["active_positions"][0] if sniper["active_positions"] else None
        sniper["active_short_cycle"] = sniper["active_short_positions"][0] if sniper["active_short_positions"] else None

        sniper_val = sniper.get("free_cash_usd", 0.0)
        for cyc in sniper["active_positions"]:
            sniper_val += round(cyc["total_qty"] * cyc.get("current_price", cyc["avg_entry_price"]), 2)
        for scyc in sniper["active_short_positions"]:
            cur_p = scyc.get("current_price", scyc["entry_price"])
            unrealized_short_pnl = scyc["invested_usd"] * ((scyc["entry_price"] - cur_p) / scyc["entry_price"])
            sniper_val += round(scyc["invested_usd"] + unrealized_short_pnl, 2)

        total_equity = round(
            arb.get("allocated_usd", 0.0) - arb.get("entry_fees_paid_usd", 0.0) + arb.get("funding_collected_usd", 0.0)
            + sniper_val
            + res.get("allocated_usd", 0.0) + res.get("accrued_earn_usd", 0.0),
            2
        )
        self.state["current_total_equity_usd"] = total_equity
        self.state["net_pnl_usd"] = round(total_equity - self.initial_capital, 2)
        self.state["last_updated_utc"] = now_iso
        self.state["last_updated_ms"] = now_ms
        if live_actions:
            self.state["strategy_scoreboard"] = self._compute_playbook_scoreboard()
            self.state["last_cycle_delta"] = {
                "timestamp_utc": now_iso,
                "equity_before_usd": prev_equity,
                "equity_after_usd": total_equity,
                "realized_pnl_before_usd": round(prev_realized, 2),
                "realized_pnl_after_usd": round(sniper["realized_pnl_usd"], 2),
                "actions_executed": live_actions
            }
            self._save()
        return {"actions_executed": live_actions, "equity_usd": total_equity, "timestamp_utc": now_iso}

if __name__ == "__main__":
    engine = VirtualPortfolioEngine(initial_capital=1000.0)
    if "--cloud-live-stream" in sys.argv:
        # Continuous 2-second Live Real-Time Stream on GitHub Actions Cloud while Laptop is OFF
        t_start = time.time()
        max_sec = 1080  # 18 minutes continuous live stream per 20-minute cron window
        report = engine.update_and_replay_offline()
        while (time.time() - t_start) < max_sec:
            time.sleep(2.0)
            tick_res = engine.live_realtime_tick()
            if int(time.time() - t_start) % 30 < 2:
                report = engine.update_and_replay_offline()
        engine._save()
        print(json.dumps({"mode": "cloud_live_stream_completed", "equity_usd": engine.state.get("current_total_equity_usd")}, indent=2))
    else:
        report = engine.update_and_replay_offline()
        print(json.dumps(report, indent=2))
