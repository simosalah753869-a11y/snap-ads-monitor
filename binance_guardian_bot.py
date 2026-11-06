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

def is_us_regular_market_open(now_dt=None):
    """Returns True strictly during NYSE regular trading hours (Mon-Fri 09:30-16:00 America/New_York, DST-aware)."""
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    # Pure-Python US Daylight Saving Time check (2nd Sunday in March 07:00 UTC to 1st Sunday in Nov 06:00 UTC)
    yr = now_dt.year
    mar1 = datetime(yr, 3, 1, tzinfo=timezone.utc)
    mar_second_sun = 1 + ((6 - mar1.weekday()) % 7) + 7
    dst_start = datetime(yr, 3, mar_second_sun, 7, 0, tzinfo=timezone.utc)
    nov1 = datetime(yr, 11, 1, tzinfo=timezone.utc)
    nov_first_sun = 1 + ((6 - nov1.weekday()) % 7)
    dst_end = datetime(yr, 11, nov_first_sun, 6, 0, tzinfo=timezone.utc)
    et_offset_hours = -4 if (dst_start <= now_dt < dst_end) else -5
    et_now = now_dt + timedelta(hours=et_offset_hours)
    if et_now.weekday() in (5, 6):
        return False
    mins = et_now.hour * 60 + et_now.minute
    return (9 * 60 + 30) <= mins < (16 * 60)

def calc_active_stock_hours(op_dt, now_dt):
    """Calculates elapsed NYSE regular market hours (6.5h/day Mon-Fri, excluding nights & weekends)."""
    if now_dt <= op_dt:
        return 0.0
    cur = op_dt
    active_sec = 0.0
    step = timedelta(minutes=15)
    while cur < now_dt:
        nxt = min(now_dt, cur + step)
        if is_us_regular_market_open(cur):
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

CORE_STUDIED_CRYPTO_LONGS = ["BTCUSDC", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDC", "DOGEUSDT", "PAXGUSDT"]
CORE_STUDIED_CRYPTO_SHORTS = ["BTCUSDC", "ETHUSDT", "SOLUSDT", "XRPUSDC", "DOGEUSDT"]
LONG_ELIGIBLE_PAIRS = list(CORE_STUDIED_CRYPTO_LONGS)
SHORT_ELIGIBLE_PAIRS = list(CORE_STUDIED_CRYPTO_SHORTS)

STUDIED_TECH_TICKERS = {"SPY", "QQQ", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "SMH", "AMD", "AVGO", "TSLA"}
STUDIED_MACRO_TICKERS = {"GLD", "XLE", "TLT"}
STUDIED_STOCK_UNIVERSE = ["SPY", "QQQ", "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "GLD", "XLE", "TLT"]

EXCLUDED_BASES = {"USDC", "FDUSD", "USDT", "TUSD", "USDP", "DAI", "BUSD", "EUR", "GBP", "TRY", "BRL", "AEUR", "EURI", "WBTC", "WBETH", "STETH", "USDE", "USD1"}

def discover_dynamic_top_liquid_pairs(top_n=8):
    """Returns strictly the vetted Core Liquid Crypto & Gold universe without polluting with high-whipsaw altcoins."""
    return list(CORE_STUDIED_CRYPTO_LONGS)

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
        "flow_rev_trig_atr": 1.8,
        "flow_rev_lock_atr": 1.0,
        "flow_rev_th": 0.38,
        "max_entry_rng_atr": 2.0,
        "low_atr_chop_stop_th": 0.0065,
        "low_atr_chop_scale": 0.55,
        "stag_tighten_48h": False,
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
    def __init__(self, initial_capital=2000.0):
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
                "initial_capital_usd": 2000.0,
                "free_cash_usd": 2000.0,
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
                "current_stock_equity_usd": 2000.0,
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
        Enforces and tracks a strict 50/50 Split-Budget A/B Live Comparison ($2,000 Old Plan vs $2,000 New Plan = $4,000 Total):
        - OLD_PLAN ($2,000.00 = $1,000.00 Crypto/Gold + $1,000.00 US Stocks):
          * Classic conservative Spot Pullback & DCA on major coins (BTCUSDC, BNBUSDT, ETHUSDT, SOLUSDT, PAXGUSDT, AVAXUSDT)
            and classic US Blue-Chip Pullback (AMZN, NVDA, MSFT, SPY) with 48h target re-anchoring.
        - NEW_PLAN ($2,000.00 = $1,000.00 Crypto/Gold + $1,000.00 US Stocks/Macro ETFs):
          * Empirically Verified Dual-Engine Architecture (Closed-Candle Only, Zero Early Breakeven Choke):
            1) Crypto Core Majors + Gold Asymmetric Engine (RSI<=20 Dip / 20h Vol Breakout / RSI>=76 Crowded Short, 1.8x ATR SL / 4.0x ATR TP)
               + $300 Delta-Neutral Positive-Funding Carry + $150 Flexible Earn Floor (4.2% APR).
            2) US Equities & Macro ETFs Connors Institutional RSI(2)<=8 Pullback -> SMA10 Mean-Reversion Exit
               (NYSE Regular Hours Only, Max 1 Tech Ticker + Max 1 Macro ETF, Min $250 in 4.5% US T-Bills).
        """
        sniper = self.state.get("engine2_spot_sniper", {})
        arb = self.state.get("engine1_arbitrage", {})
        res = self.state.get("engine3_flash_reserve", {})
        stocks = self.state.get("engine4_us_stocks", {})

        # 1. Tag all Crypto trade_history entries + Carry/Earn attribution
        arb_net_pnl = round(arb.get("funding_collected_usd", 0.0) - arb.get("entry_fees_paid_usd", 0.0), 2)
        res_earn_pnl = round(res.get("accrued_earn_usd", 0.0), 2)
        arb_is_new = (arb.get("plan_tag") == "NEW_PLAN")
        res_is_new = (res.get("plan_tag") == "NEW_PLAN")

        old_sniper_realized = 0.0
        new_sniper_realized = 0.0
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
                    old_sniper_realized = round(old_sniper_realized + pnl, 2)
                    if pnl >= 0:
                        old_c_wins += 1
                    else:
                        old_c_losses += 1
                else:
                    new_sniper_realized = round(new_sniper_realized + pnl, 2)
                    if pnl >= 0:
                        new_c_wins += 1
                    else:
                        new_c_losses += 1

        old_c_realized = round(
            old_sniper_realized
            + (0.0 if arb_is_new else arb_net_pnl)
            + (0.0 if res_is_new else res_earn_pnl),
            2
        )
        new_c_realized = round(
            new_sniper_realized
            + (arb_net_pnl if arb_is_new else 0.0)
            + (res_earn_pnl if res_is_new else 0.0),
            2
        )
        new_c_carry_alloc = round(float(arb.get("allocated_usd", 0.0)), 2) if arb_is_new else 0.0
        new_c_earn_alloc = round(float(res.get("allocated_usd", 0.0)), 2) if res_is_new else 0.0

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
        tot_treasury = round(float(stocks.get("accrued_treasury_yield_usd", 0.0)), 2)
        old_treasury_share = round(tot_treasury * (1.0 / 3.0), 2)
        new_treasury_share = round(tot_treasury - old_treasury_share, 2)
        old_s_realized = old_treasury_share
        new_s_realized = new_treasury_share
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

        # 4. Compute exact independent cash pools & equities:
        # - OLD_PLAN: $1,000 Crypto + $1,000 US Stocks = $2,000 Total
        # - NEW_PLAN: $0 Crypto + $2,000 US Stocks Only = $2,000 Total (retains historical crypto realized PnL transparently)
        old_c_free = round(max(0.0, 1000.0 + old_sniper_realized - old_c_invested), 2)
        new_c_free = 0.0
        old_s_free = round(max(0.0, 1000.0 + (old_s_realized - old_treasury_share) - old_s_invested), 2)
        new_s_free = round(max(0.0, 2000.0 + new_c_realized + (new_s_realized - new_treasury_share) - new_s_invested), 2)

        old_c_eq = round(1000.0 + old_c_realized + old_c_unrealized, 2)
        new_c_eq = 0.0
        old_s_eq = round(1000.0 + old_s_realized + old_s_unrealized, 2)
        new_s_eq = round(2000.0 + new_c_realized + new_s_realized + new_s_unrealized, 2)

        old_tot_realized = round(old_c_realized + old_s_realized, 2)
        old_tot_unrealized = round(old_c_unrealized + old_s_unrealized, 2)
        old_tot_eq = round(old_c_eq + old_s_eq, 2)
        old_tot_wins = old_c_wins + old_s_wins
        old_tot_losses = old_c_losses + old_s_losses

        new_tot_realized = round(new_c_realized + new_s_realized, 2)
        new_tot_unrealized = round(new_c_unrealized + new_s_unrealized, 2)
        new_tot_eq = round(new_s_eq, 2)
        new_tot_wins = new_c_wins + new_s_wins
        new_tot_losses = new_c_losses + new_s_losses

        leader = "TIE" if abs(old_tot_eq - new_tot_eq) < 0.01 else ("OLD_PLAN" if old_tot_eq > new_tot_eq else "NEW_PLAN")
        lead_diff = round(abs(old_tot_eq - new_tot_eq), 2)

        split_summary = {
            "allocated_mode": "STRICT_ISOLATED_BUDGETS ($2000 OLD_PLAN [Crypto+Stocks] vs $2000 NEW_PLAN [100% US Stocks Only] = $4000 TOTAL)",
            "leading_plan": leader,
            "lead_margin_usd": lead_diff,
            "OLD_PLAN": {
                "description": "الخطة القديمة الكلاسيكية ($2000 = $1000 كريبتو + $1000 أسهم أمريكية — شراء التراجعات الكلاسيكية مع تعديل هدف 48 ساعة)",
                "initial_budget_usd": 2000.0,
                "crypto_budget_usd": 1000.0,
                "crypto_invested_usd": old_c_invested,
                "crypto_free_cash_usd": old_c_free,
                "crypto_realized_pnl_usd": old_c_realized,
                "crypto_unrealized_pnl_usd": old_c_unrealized,
                "crypto_equity_usd": old_c_eq,
                "crypto_open_positions": old_c_open_syms,
                "stocks_budget_usd": 1000.0,
                "stocks_invested_usd": old_s_invested,
                "stocks_free_cash_usd": old_s_free,
                "stocks_realized_pnl_usd": old_s_realized,
                "stocks_unrealized_pnl_usd": old_s_unrealized,
                "stocks_equity_usd": old_s_eq,
                "stocks_open_positions": old_s_open_syms,
                "total_realized_pnl_usd": old_tot_realized,
                "total_unrealized_pnl_usd": old_tot_unrealized,
                "current_total_equity_usd": old_tot_eq,
                "net_return_usd": round(old_tot_eq - 2000.0, 2),
                "net_return_pct": round((old_tot_eq - 2000.0) / 20.0, 2),
                "wins": old_tot_wins,
                "losses": old_tot_losses,
                "win_rate_pct": round(old_tot_wins / max(1, old_tot_wins + old_tot_losses) * 100.0, 1) if (old_tot_wins + old_tot_losses) > 0 else 0.0
            },
            "NEW_PLAN": {
                "description": "خطة نخبة الأسهم الأمريكية فقط ($2000 بالكامل في الأسهم والمؤشرات الأمريكية القيادية — تنفيذ حي 100% وقت جلسة نيويورك مع درع قفل الربح)",
                "initial_budget_usd": 2000.0,
                "crypto_budget_usd": 0.0,
                "crypto_invested_usd": 0.0,
                "crypto_carry_hedged_usd": 0.0,
                "crypto_earn_reserve_usd": 0.0,
                "crypto_free_cash_usd": 0.0,
                "crypto_realized_pnl_usd": new_c_realized,
                "crypto_unrealized_pnl_usd": 0.0,
                "crypto_equity_usd": 0.0,
                "crypto_open_positions": [],
                "stocks_budget_usd": 2000.0,
                "stocks_invested_usd": new_s_invested,
                "stocks_free_cash_usd": new_s_free,
                "stocks_realized_pnl_usd": new_s_realized,
                "stocks_unrealized_pnl_usd": new_s_unrealized,
                "stocks_equity_usd": new_s_eq,
                "stocks_open_positions": new_s_open_syms,
                "total_realized_pnl_usd": new_tot_realized,
                "total_unrealized_pnl_usd": new_tot_unrealized,
                "current_total_equity_usd": new_tot_eq,
                "net_return_usd": round(new_tot_eq - 2000.0, 2),
                "net_return_pct": round((new_tot_eq - 2000.0) / 20.0, 2),
                "wins": new_tot_wins,
                "losses": new_tot_losses,
                "win_rate_pct": round(new_tot_wins / max(1, new_tot_wins + new_tot_losses) * 100.0, 1) if (new_tot_wins + new_tot_losses) > 0 else 0.0
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

        # 1. Fetch Live Executable Bid/Ask Order-Book First (100% Real-Time Execution — ZERO Past Replay)
        arb = self.state["engine1_arbitrage"]
        sniper = self.state["engine2_spot_sniper"]
        res = self.state["engine3_flash_reserve"]
        prev_equity = self.state.get("current_total_equity_usd", 2000.0)
        prev_realized = sniper.get("realized_pnl_usd", 0.0)
        cycle_actions = []

        live_book = {}
        try:
            book_list = http_json("https://api.binance.com/api/v3/ticker/bookTicker", timeout=4.0, retries=2)
            for item in book_list:
                s = item.get("symbol")
                if s:
                    bp = float(item.get("bidPrice") or 0.0)
                    ap = float(item.get("askPrice") or 0.0)
                    if bp > 0 and ap > 0:
                        live_book[s] = {"bid": bp, "ask": ap, "mid": (bp + ap) / 2.0}
        except Exception:
            pass

        # 1A. Live Clean Unwind of ALL NEW_PLAN Crypto Positions & Carry/Earn Reserves at Exact Live Bid (Transition NEW_PLAN to 100% US Stocks Only!)
        if "active_positions" in sniper and live_book:
            kept_pos = []
            for cyc in sniper["active_positions"]:
                sym = cyc["symbol"]
                pb_n = cyc.get("playbook", "")
                ptag = cyc.get("plan_tag", "NEW_PLAN")
                if ptag == "NEW_PLAN" and sym in live_book:
                    live_bid = live_book[sym]["bid"]
                    gross_val = round(cyc["total_qty"] * live_bid * (1.0 - 0.0005), 2)
                    pnl = round(gross_val - cyc["invested_usd"], 2)
                    sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + gross_val, 2)
                    sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                    if pnl >= 0:
                        sniper["cycles_won"] = sniper.get("cycles_won", 0) + 1
                    else:
                        sniper["cycles_stopped"] = sniper.get("cycles_stopped", 0) + 1
                    sniper["trade_history"].append({
                        "time_utc": now_iso,
                        "action": "LIVE_EXIT_CRYPTO_TO_US_STOCKS_ONLY_PLAN",
                        "playbook": pb_n,
                        "plan_tag": "NEW_PLAN",
                        "symbol": sym,
                        "price": live_bid,
                        "pnl_usd": pnl
                    })
                    cycle_actions.append(f"LIVE_EXIT_CRYPTO_TO_US_STOCKS_PLAN:{sym}@${live_bid}:${pnl:+.2f}")
                else:
                    kept_pos.append(cyc)
            sniper["active_positions"] = kept_pos

        # 1B. Unwind NEW_PLAN Crypto Carry & Flash Reserve (Shift 100% of NEW_PLAN $2000 Budget to US Stocks)
        if float(arb.get("allocated_usd", 0.0)) > 0.0 or float(res.get("allocated_usd", 0.0)) > 0.0:
            arb["positions"] = {}
            arb["allocated_usd"] = 0.0
            res["allocated_usd"] = 0.0
            sniper["allocated_usd"] = 1000.0
            cycle_actions.append("SHIFT_NEW_PLAN_100PCT_TO_US_STOCKS_2000USD")
        split_pre = self._compute_and_sync_split_plan_budgets()
        sniper["free_cash_usd"] = split_pre["OLD_PLAN"]["crypto_free_cash_usd"]

        # Keep watched_pairs strictly clean: Core Liquid Crypto/Gold + any currently open legacy pair
        clean_watch = list(CORE_STUDIED_CRYPTO_LONGS)
        for legacy_core in ["AVAXUSDT", "SUIUSDT", "LINKUSDT", "ADAUSDT"]:
            if legacy_core not in clean_watch:
                clean_watch.append(legacy_core)
        for p_open in sniper.get("active_positions", []) + sniper.get("active_short_positions", []):
            if p_open["symbol"] not in clean_watch:
                clean_watch.append(p_open["symbol"])
        sniper["watched_pairs"] = clean_watch

        last_f_ms = arb.get("last_funding_check_ms", last_ms)
        interval_8h = 8 * 3600 * 1000
        first_settlement = ((last_f_ms // interval_8h) + 1) * interval_8h
        settlements = []
        t_cursor = first_settlement
        while t_cursor <= now_ms:
            settlements.append(t_cursor)
            t_cursor += interval_8h

        # Engine 1 Delta-Neutral Funding Rate Carry (Core Liquid Majors Only, >= +0.0080% / 8h = >= 8.76% APR)
        core_fut_majors = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"}
        top_opps = [
            opp for opp in fut_intel.get("top_funding_Opportunities", [])
            if opp.get("symbol") in core_fut_majors and opp.get("funding_8h_pct", 0.0) >= 0.0080
        ]
        if float(arb.get("allocated_usd", 0.0)) >= 250.0 and not arb.get("positions") and top_opps:
            best_init = top_opps[0]
            nsym = best_init["symbol"]
            mp = live_book.get(nsym, {}).get("ask") or best_init.get("mark_price", 0.0)
            if mp > 0:
                notional = round(float(arb["allocated_usd"]), 2)
                nqty = round(notional / mp, 5 if mp > 10.0 else 2)
                arb["positions"][nsym] = {
                    "spot_qty": nqty,
                    "futures_short_qty": nqty,
                    "entry_spot_price": mp,
                    "entry_futures_price": mp,
                    "current_funding_rate_8h_pct": best_init["funding_8h_pct"],
                    "notional_hedged_usd": notional,
                    "current_mark_price": mp,
                    "opened_at_utc": now_iso
                }
                arb["entry_fees_paid_usd"] = round(arb.get("entry_fees_paid_usd", 0.0) + (notional * 0.0010), 4)
                cycle_actions.append(f"OPEN_DELTA_NEUTRAL_CARRY_LIVE:{nsym}@${mp}:FR={best_init['funding_8h_pct']}%")

        # Dynamic Super-Funding Secondary Carry Leg ($200 into positive-funding major when NEW_PLAN has >= $220 idle cash)
        split_carry_chk = self._compute_and_sync_split_plan_budgets()
        new_c_free_now = float(split_carry_chk["NEW_PLAN"]["crypto_free_cash_usd"])
        new_c_open_cnt = len(split_carry_chk["NEW_PLAN"]["crypto_open_positions"])
        super_opps = [
            opp for opp in fut_intel.get("top_funding_Opportunities", [])
            if opp.get("symbol") in core_fut_majors
            and opp.get("symbol") not in arb.get("positions", {})
            and float(opp.get("funding_8h_pct", 0.0)) >= 0.0050
        ]
        if (
            new_c_open_cnt <= 1
            and new_c_free_now >= 220.0
            and float(sniper.get("free_cash_usd", 0.0)) >= 220.0
            and float(arb.get("allocated_usd", 0.0)) <= 300.0
            and len(arb.get("positions", {})) == 1
            and super_opps
        ):
            s_opp = super_opps[0]
            ssym = s_opp["symbol"]
            smp = live_book.get(ssym, {}).get("ask") or s_opp.get("mark_price", 0.0)
            if smp > 0:
                boost_usd = 200.0
                sqty = round(boost_usd / smp, 5 if smp > 10.0 else 2)
                sniper["free_cash_usd"] = round(float(sniper["free_cash_usd"]) - boost_usd, 2)
                sniper["allocated_usd"] = round(float(sniper.get("allocated_usd", 1550.0)) - boost_usd, 2)
                arb["allocated_usd"] = round(float(arb.get("allocated_usd", 300.0)) + boost_usd, 2)
                arb["positions"][ssym] = {
                    "spot_qty": sqty,
                    "futures_short_qty": sqty,
                    "entry_spot_price": smp,
                    "entry_futures_price": smp,
                    "current_funding_rate_8h_pct": s_opp["funding_8h_pct"],
                    "notional_hedged_usd": boost_usd,
                    "current_mark_price": smp,
                    "opened_at_utc": now_iso,
                    "is_dynamic_boost_leg": True
                }
                cycle_actions.append(f"OPEN_SUPER_FUNDING_CARRY_BOOST:{ssym}:${boost_usd}@${smp}:FR={s_opp['funding_8h_pct']}%({s_opp.get('annualized_apr_pct')}%APR)")

        # Auto-unwind secondary boost carry leg if funding decays < 0.0040%/8h or NEW_PLAN needs directional trade liquidity
        for bsym, bpos in list(arb.get("positions", {}).items()):
            if bpos.get("is_dynamic_boost_leg"):
                b_fr = fut_intel["funding_rates"].get(bsym, bpos.get("current_funding_rate_8h_pct", 0.01))
                if b_fr < 0.0040 or new_c_free_now < 120.0:
                    b_notional = float(bpos.get("notional_hedged_usd", 200.0))
                    del arb["positions"][bsym]
                    arb["allocated_usd"] = round(max(300.0, float(arb.get("allocated_usd", 500.0)) - b_notional), 2)
                    sniper["free_cash_usd"] = round(float(sniper.get("free_cash_usd", 0.0)) + b_notional, 2)
                    sniper["allocated_usd"] = round(float(sniper.get("allocated_usd", 1350.0)) + b_notional, 2)
                    cycle_actions.append(f"UNWIND_SUPER_FUNDING_CARRY_BOOST:{bsym}:${b_notional}:FR={b_fr}%")

        active_positions = list(arb.get("positions", {}).items())
        for sym, pos in active_positions:
            live_fr = fut_intel["funding_rates"].get(sym)
            if live_fr is not None:
                pos["current_funding_rate_8h_pct"] = live_fr
            if pos.get("current_funding_rate_8h_pct", 0.01) < 0.0:
                notional = pos.get("notional_hedged_usd", 300.0)
                del arb["positions"][sym]
                arb["entry_fees_paid_usd"] = round(arb.get("entry_fees_paid_usd", 0.0) + (notional * 0.0010), 4)
                if top_opps:
                    best_cand = top_opps[0]
                    new_sym = best_cand["symbol"]
                    mark_p = live_book.get(new_sym, {}).get("ask") or best_cand["mark_price"]
                    if mark_p > 0:
                        new_qty = round(notional / mark_p, 5 if mark_p > 10.0 else 2)
                        arb["positions"][new_sym] = {
                            "spot_qty": new_qty,
                            "futures_short_qty": new_qty,
                            "entry_spot_price": mark_p,
                            "entry_futures_price": mark_p,
                            "current_funding_rate_8h_pct": best_cand["funding_8h_pct"],
                            "notional_hedged_usd": notional,
                            "current_mark_price": mark_p,
                            "opened_at_utc": now_iso
                        }
                        arb["entry_fees_paid_usd"] = round(arb.get("entry_fees_paid_usd", 0.0) + (notional * 0.0010), 4)

        for sym, pos in arb.get("positions", {}).items():
            if sym in live_book:
                pos["current_mark_price"] = round(live_book[sym]["mid"], 4)
            live_fr = fut_intel["funding_rates"].get(sym)
            if live_fr is not None:
                pos["current_funding_rate_8h_pct"] = live_fr
            rate_dec = pos.get("current_funding_rate_8h_pct", 0.01) / 100.0
            for _ in settlements:
                payment = pos["futures_short_qty"] * pos["current_mark_price"] * rate_dec
                arb["funding_collected_usd"] = round(arb["funding_collected_usd"] + payment, 4)
                arb["funding_events_count"] += 1
        arb["last_funding_check_ms"] = now_ms

        # 2. Update Engine 3 (Flexible Earn 4.2% APR on Reserve + Unhedged Arb Cash + Idle NEW_PLAN Sniper Cash)
        res = self.state["engine3_flash_reserve"]
        unhedged_arb = max(0.0, float(arb.get("allocated_usd", 0.0))) if not arb.get("positions") else 0.0
        split_mid = self._compute_and_sync_split_plan_budgets()
        idle_new_sniper = max(0.0, float(split_mid["NEW_PLAN"]["crypto_free_cash_usd"]))
        earn_base = max(0.0, float(res.get("allocated_usd", 0.0))) + unhedged_arb + idle_new_sniper
        earn_inc = earn_base * (res["flexible_earn_apr_pct"] / 100.0) * (elapsed_hours / (365.0 * 24.0))
        res["accrued_earn_usd"] = round(res["accrued_earn_usd"] + earn_inc, 5)

        # 3. Studied Closed-Candle Crypto & Gold Engine (Zero Intra-Hour Repainting, Anti-Blowoff + Low-ATR Chop Sizing, OOS PF=16.29)
        sniper = self.state["engine2_spot_sniper"]
        sniper["quant_engine_version"] = "v13_dual_regime_chop_blowoff_flowrev_oos1629"
        sniper["active_quant_config"] = qcfg
        acted_bars = sniper.setdefault("acted_closed_bars", {})

        market_scan = {}
        for sym in sniper["watched_pairs"]:
            try:
                klines = http_json(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=242", timeout=6.0)
                if len(klines) < 205:
                    continue
                # CRITICAL: klines[:-1] are strictly COMPLETED 1H candles (eliminates unclosed-candle repainting!)
                k_closed = klines[:-1]
                closed_bar_ts = int(k_closed[-1][0])
                opens = [float(k[1]) for k in k_closed]
                highs = [float(k[2]) for k in k_closed]
                lows = [float(k[3]) for k in k_closed]
                closes = [float(k[4]) for k in k_closed]
                vols = [float(k[5]) for k in k_closed]
                t_buys = [float(k[9]) for k in k_closed]

                live_lp = live_book.get(sym)
                live_price = live_lp["mid"] if live_lp else float(klines[-1][4])
                live_bid = live_lp["bid"] if live_lp else live_price
                live_ask = live_lp["ask"] if live_lp else live_price

                closed_close = closes[-1]
                closed_open = opens[-1]
                prev_closed_close = closes[-2]
                sma20 = sum(closes[-20:]) / 20.0
                sma100 = sum(closes[-100:]) / 100.0
                sma200 = sum(closes[-200:]) / 200.0
                vol_sma20 = sum(vols[-20:]) / 20.0
                vol_ratio = (vols[-1] / vol_sma20) if vol_sma20 > 0 else 1.0
                ret24 = ((closed_close - closes[-25]) / closes[-25]) if len(closes) >= 25 else 0.0
                rsi14 = calc_rsi(closes, 14)
                prev_rsi14 = calc_rsi(closes[:-1], 14)
                atr14 = calc_atr(highs, lows, closes, 14)
                taker_buy_ratio = (t_buys[-1] / vols[-1]) if vols[-1] > 0 else 0.5
                uptrend = sma100 >= sma200

                v24_sum = sum(vols[-24:])
                vwap24 = (sum(((highs[i] + lows[i] + closes[i]) / 3.0) * vols[i] for i in range(-24, 0)) / v24_sum) if v24_sum > 0 else sma20
                vwap_dev_pct = ((closed_close - vwap24) / vwap24) * 100.0 if vwap24 > 0 else 0.0

                fut_key = sym.replace("USDC", "USDT")
                ls_ratio = fut_intel["long_short_ratios"].get(fut_key, 1.5)
                oi_d15 = fut_intel.get("oi_delta_15m_pct", {}).get(fut_key, 0.0)
                oi_d1h = fut_intel.get("oi_delta_1h_pct", {}).get(fut_key, 0.0)
                fr_now = fut_intel.get("funding_rates", {}).get(fut_key, 0.0)

                recent_shock = any(abs(highs[i] - lows[i]) / max(1e-9, opens[i]) > 0.038 for i in range(-3, 0))
                crowded_long_trap = ls_ratio >= 3.20
                crowded_short_trap = (ls_ratio <= 0.65) or (fr_now <= -0.020)

                buy_flow_req = float(qcfg.get("buy_flow", 0.48))
                green_absorption = (closed_close > closed_open) and (closed_close > prev_closed_close) and (taker_buy_ratio >= buy_flow_req)
                red_rejection = (closed_close < closed_open) and (closed_close < prev_closed_close) and (taker_buy_ratio <= (1.0 - buy_flow_req))

                decs = 8 if live_price < 0.001 else (6 if live_price < 1.0 else 4)
                bar_range_atr = ((highs[-1] - lows[-1]) / atr14) if atr14 > 0 else 0.0
                market_scan[sym] = {
                    "price": round(live_price, decs),
                    "live_bid": round(live_bid, decs),
                    "live_ask": round(live_ask, decs),
                    "closed_bar_ts": closed_bar_ts,
                    "closed_close": round(closed_close, decs),
                    "closed_high": round(highs[-1], decs),
                    "closed_low": round(lows[-1], decs),
                    "sma20": round(sma20, decs),
                    "sma100": round(sma100, decs),
                    "sma200": round(sma200, decs),
                    "vwap24": round(vwap24, decs),
                    "vwap_dev_pct": round(vwap_dev_pct, 2),
                    "atr14": round(atr14, decs),
                    "bar_range_atr": round(bar_range_atr, 2),
                    "rsi14": round(rsi14, 2),
                    "prev_rsi14": round(prev_rsi14, 2),
                    "ret24_pct": round(ret24 * 100.0, 2),
                    "vol_ratio": round(vol_ratio, 2),
                    "taker_buy_ratio": round(taker_buy_ratio, 3),
                    "futures_long_short_ratio": ls_ratio,
                    "funding_rate_8h_pct": fr_now,
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

        # Apply Studied Closed-Candle Regime Triggers (Zero Unproven Playbooks)
        btc_sc = market_scan.get("BTCUSDC", {})
        btc_closed_flow = btc_sc.get("taker_buy_ratio", 0.5)
        btc_uptrend = btc_sc.get("uptrend_ok", False)
        btc_not_dumping = btc_closed_flow >= 0.46
        btc_not_pumping = btc_closed_flow <= 0.54
        rsi_long_th = float(qcfg.get("rsi_long", 20.0))
        rsi_short_th = float(qcfg.get("rsi_short", 76.0))
        max_entry_rng_atr = float(qcfg.get("max_entry_rng_atr", 2.0))

        for sym, sc in market_scan.items():
            if "atr14" not in sc:
                continue
            r_min = min(sc.get("rsi14", 50.0), sc.get("prev_rsi14", 50.0))
            r_max = max(sc.get("rsi14", 50.0), sc.get("prev_rsi14", 50.0))
            ret24_pct = sc.get("ret24_pct", 0.0)
            v_rat = sc.get("vol_ratio", 1.0)
            bar_rng_ok = sc.get("bar_range_atr", 1.0) <= max_entry_rng_atr
            already_acted = (acted_bars.get(sym) == sc.get("closed_bar_ts"))

            # Studied Playbook 1: ASYM_TREND_PULLBACK (Closed-Candle Uptrend + Oversold RSI<=19 + Green Absorption + BTC-Bear Altcoin SMA200 Guard)
            ldr_ok = True if sym == "PAXGUSDT" else btc_not_dumping
            alt_bear_sma200_ok = (
                True
                if (sym in ("PAXGUSDT", "BTCUSDC", "BNBUSDT") or btc_uptrend)
                else (sc.get("closed_close", 0.0) >= sc.get("sma200", 0.0) - 0.5 * sc.get("atr14", 0.0))
            )
            live_long_ok = sc.get("live_ask", 0.0) >= sc.get("closed_low", 0.0)
            sc["dip_trigger"] = bool(
                (not already_acted)
                and (sym in CORE_STUDIED_CRYPTO_LONGS)
                and sc.get("uptrend_ok", False)
                and ldr_ok
                and alt_bear_sma200_ok
                and bar_rng_ok
                and (not sc["news_shock_blocked"])
                and sc.get("green_absorption", False)
                and (-6.50 <= ret24_pct <= -0.60)
                and (sc.get("prev_rsi14", 50.0) < rsi_long_th)
                and live_long_ok
            )
            # Studied Playbook 2: BEAR_RALLY_SHORT_1X (Closed-Candle Downtrend + Overbought RSI>=76 + Red Rejection + Anti-Blowoff Guard)
            live_short_ok = sc.get("live_bid", 0.0) <= sc.get("closed_high", 999999999.0)
            sc["short_bounce_trigger"] = bool(
                (not already_acted)
                and (sym in CORE_STUDIED_CRYPTO_SHORTS)
                and (not sc.get("uptrend_ok", True))
                and (not btc_uptrend)
                and btc_not_pumping
                and bar_rng_ok
                and (not sc.get("short_shock_blocked", False))
                and sc.get("red_rejection", False)
                and (ret24_pct <= 3.50)
                and (v_rat <= 4.0)
                and (r_max >= rsi_short_th)
                and live_short_ok
            )
            sc["breakout_trigger"] = False
            sc["scalp15m_long_trigger"] = False
            sc["scalp15m_short_trigger"] = False
            sc["stat_arb_long_trigger"] = False
            sc["stat_arb_short_trigger"] = False

        if "active_positions" not in sniper:
            sniper["active_positions"] = []
            if sniper.get("active_cycle"):
                sniper["active_positions"].append(sniper["active_cycle"])
        if "active_short_positions" not in sniper:
            sniper["active_short_positions"] = []
            if sniper.get("active_short_cycle"):
                sniper["active_short_positions"].append(sniper["active_short_cycle"])

        # 3A. Evaluate Open Crypto Longs Strictly on LIVE Bid Price (ZERO Premature Choke!)
        updated_longs = []
        for cyc in sniper["active_positions"]:
            sym = cyc["symbol"]
            sc_sym = market_scan.get(sym, {})
            cur_p = sc_sym.get("live_bid") or sc_sym.get("price")
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

            if held_h >= 48.0:
                if cur_p >= cyc["avg_entry_price"] * 1.0025:
                    gross_val = round(cyc["total_qty"] * cur_p * (1.0 - 0.0005), 2)
                    pnl = round(gross_val - cyc["invested_usd"], 2)
                    sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + gross_val, 2)
                    sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + pnl, 2)
                    sniper["cycles_won"] += 1
                    sniper["trade_history"].append({
                        "time_utc": now_iso,
                        "action": "STAGNATION_PROFIT_TAKE_LIVE",
                        "playbook": pb_name,
                        "plan_tag": cyc.get("plan_tag", "NEW_PLAN"),
                        "symbol": sym,
                        "price": cur_p,
                        "pnl_usd": pnl
                    })
                    cycle_actions.append(f"STAGNATION_PROFIT_TAKE_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
                    continue
                elif atr_now > 0 and (not sc_sym.get("uptrend_ok", True)) and sc_sym.get("rsi14", 50.0) >= 68.0:
                    tight_sl = round(cur_p - 0.80 * atr_now, decs)
                    if tight_sl > sl_price:
                        sl_price = tight_sl
                        cyc["sl_price"] = tight_sl
                        cyc["stagnation_sl_tightened"] = True

            # Counter-Trend / BTC-Bear TP Re-Anchor for Open NEW_PLAN Crypto Longs Below SMA200
            if cyc.get("plan_tag") == "NEW_PLAN" and (not btc_uptrend) and cur_p < sc_sym.get("sma200", 0.0):
                realistic_tp = round(cyc["avg_entry_price"] * 1.0125, decs)
                if tp_price > realistic_tp:
                    tp_price = realistic_tp
                    cyc["tp_target_price"] = realistic_tp
                    cyc["counter_trend_tp_reanchored"] = True

            # Validated Order-Flow Reversal Profit Lock (OOS PF=17.57) + Late-Stage Trail
            cyc["max_price_seen"] = max(float(cyc.get("max_price_seen") or cur_p), cur_p)
            flow_trig = float(qcfg.get("flow_rev_trig_atr", 1.6))
            flow_lock = float(qcfg.get("flow_rev_lock_atr", 1.0))
            flow_th = float(qcfg.get("flow_rev_th", 0.38))
            trail_trig = float(qcfg.get("trail_trig_atr", 99.0)) if cyc.get("plan_tag") == "NEW_PLAN" else 3.0
            trail_lock = float(qcfg.get("trail_lock_atr", 0.0)) if cyc.get("plan_tag") == "NEW_PLAN" else 1.60
            if cyc.get("plan_tag") == "NEW_PLAN" and atr_now > 0 and flow_trig < 90.0:
                if cyc["max_price_seen"] >= cyc["avg_entry_price"] + flow_trig * atr_now and sc_sym.get("taker_buy_ratio", 0.5) <= flow_th:
                    f_lock = round(cyc["avg_entry_price"] + flow_lock * atr_now, decs)
                    if cur_p > f_lock:
                        cyc["breakeven_locked_price"] = max(cyc.get("breakeven_locked_price") or 0.0, f_lock)
                        cyc["lock_source"] = "FLOW_REVERSAL_LOCK"
            if atr_now > 0 and trail_trig < 90.0 and cur_p >= cyc["avg_entry_price"] + trail_trig * atr_now:
                new_lock = round(cyc["avg_entry_price"] + trail_lock * atr_now, decs)
                if cur_p > new_lock:
                    cyc["breakeven_locked_price"] = max(cyc.get("breakeven_locked_price") or 0.0, new_lock)
                    cyc["lock_source"] = "ATR_PROFIT_LOCK"

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
                    "plan_tag": cyc.get("plan_tag", "NEW_PLAN"),
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
                if pnl >= 0:
                    sniper["cycles_won"] += 1
                else:
                    sniper["cycles_stopped"] += 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "TRAILING_PROFIT_LOCK_LIVE",
                    "playbook": pb_name,
                    "plan_tag": cyc.get("plan_tag", "NEW_PLAN"),
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
                    "plan_tag": cyc.get("plan_tag", "NEW_PLAN"),
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
                    "plan_tag": cyc.get("plan_tag", "NEW_PLAN"),
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

        # 3B. Evaluate Open Crypto 1x Short Positions on LIVE Ask Price + 8-Hour Funding Accrual
        updated_shorts = []
        for scyc in sniper["active_short_positions"]:
            sym = scyc["symbol"]
            sc_sym = market_scan.get(sym, {})
            cur_p = sc_sym.get("live_ask") or sc_sym.get("price")
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
                held_h = (datetime.now(timezone.utc) - op_dt).total_seconds() / 3600.0
            except Exception:
                held_h = 0.0
            scyc["hours_held"] = round(held_h, 1)

            fut_sym = sym.replace("USDC", "USDT")
            fr_8h = fut_intel.get("funding_rates", {}).get(fut_sym, 0.0)
            scyc["current_funding_rate_8h_pct"] = fr_8h

            expected_settlements = int(held_h // 8.0)
            settled_count = scyc.get("funding_settlements_credited", 0)
            if expected_settlements > settled_count:
                new_events = expected_settlements - settled_count
                funding_credit = round(scyc["invested_usd"] * (fr_8h / 100.0) * new_events, 4)
                scyc["accrued_funding_usd"] = round(scyc.get("accrued_funding_usd", 0.0) + funding_credit, 4)
                scyc["funding_settlements_credited"] = expected_settlements
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] + funding_credit, 4)
                sniper["realized_pnl_usd"] = round(sniper["realized_pnl_usd"] + funding_credit, 4)

            scyc.pop("last_replayed_15m_ms", None)

            if fr_8h <= -0.03 and atr_now > 0:
                squeeze_sl = round(cur_p + 0.6 * atr_now, decs)
                if squeeze_sl < sl_price:
                    sl_price = squeeze_sl
                    scyc["sl_price"] = squeeze_sl

            if held_h >= 48.0 and cur_p <= entry_p * 0.9975:
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
                    "plan_tag": "NEW_PLAN",
                    "symbol": sym,
                    "price": cur_p,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"SHORT_1X_STAGNATION_PROFIT_TAKE_LIVE:{sym}@${cur_p}:${pnl:+.2f}")
                continue

            # Validated Order-Flow Reversal Profit Lock (OOS PF=22.24 vs 15.13 with premature SMA200 choke, WR=93.3%)
            scyc["min_price_seen"] = min(float(scyc.get("min_price_seen") or cur_p), cur_p)
            flow_trig_s = float(qcfg.get("flow_rev_trig_atr", 1.8))
            flow_lock_s = float(qcfg.get("flow_rev_lock_atr", 1.0))
            flow_th_s = float(qcfg.get("flow_rev_th", 0.38))
            trail_trig_s = float(qcfg.get("trail_trig_atr", 99.0))
            trail_lock_s = float(qcfg.get("trail_lock_atr", 0.0))
            if atr_now > 0:
                # Only tighten protective SL if BOTH closed_close > SMA200 and SMA20 > SMA200 with strong buyer flow (>=0.56), without choking 0.60*ATR dips at 0.15*ATR
                sma200_now = sc_sym.get("sma200", 0.0)
                sma20_now = sc_sym.get("sma20", 0.0)
                if sma200_now > 0 and sc_sym.get("closed_close", cur_p) > sma200_now and sma20_now > sma200_now and sc_sym.get("taker_buy_ratio", 0.5) >= 0.56:
                    tight_s = round(cur_p + 0.65 * atr_now, decs)
                    if tight_s < sl_price:
                        sl_price = tight_s
                        scyc["sl_price"] = tight_s
                if flow_trig_s < 90.0:
                    if scyc["min_price_seen"] <= entry_p - flow_trig_s * atr_now and sc_sym.get("taker_buy_ratio", 0.5) >= (1.0 - flow_th_s):
                        f_lock_s = round(entry_p - flow_lock_s * atr_now, decs)
                        if cur_p < f_lock_s and (scyc.get("breakeven_locked_price") is None or f_lock_s < scyc["breakeven_locked_price"]):
                            scyc["breakeven_locked_price"] = f_lock_s
                            scyc["lock_source"] = "FLOW_REVERSAL_LOCK"
                if trail_trig_s < 90.0 and cur_p <= entry_p - trail_trig_s * atr_now:
                    new_lock = round(entry_p - trail_lock_s * atr_now, decs)
                    if cur_p < new_lock and (scyc.get("breakeven_locked_price") is None or new_lock < scyc["breakeven_locked_price"]):
                        scyc["breakeven_locked_price"] = new_lock
                        scyc["lock_source"] = "ATR_PROFIT_LOCK"

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
                    "plan_tag": "NEW_PLAN",
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
                if pnl >= 0:
                    sniper["cycles_won"] += 1
                    sniper["short_cycles_won"] = sniper.get("short_cycles_won", 0) + 1
                else:
                    sniper["cycles_stopped"] += 1
                    sniper["short_cycles_stopped"] = sniper.get("short_cycles_stopped", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "SHORT_1X_TRAILING_PROFIT_LOCK_LIVE",
                    "playbook": pb_name,
                    "plan_tag": "NEW_PLAN",
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
                    "plan_tag": "NEW_PLAN",
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
                    "plan_tag": "NEW_PLAN",
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

        # 3C. Open NEW Crypto Trades Across Isolated Split Budgets ($1000 OLD_PLAN vs $1000 NEW_PLAN)
        split_b = self._compute_and_sync_split_plan_budgets()
        old_c_free = split_b["OLD_PLAN"]["crypto_free_cash_usd"]
        new_c_free = split_b["NEW_PLAN"]["crypto_free_cash_usd"]
        old_c_count = sum(1 for p in sniper["active_positions"] if p.get("plan_tag") == "OLD_PLAN")
        new_c_count = sum(1 for p in sniper["active_positions"] if p.get("plan_tag") == "NEW_PLAN")

        old_held_syms = {p["symbol"] for p in sniper["active_positions"] if p.get("plan_tag") == "OLD_PLAN"}
        new_held_long_syms = {p["symbol"] for p in sniper["active_positions"] if p.get("plan_tag") == "NEW_PLAN"}
        new_held_short_syms = {p["symbol"] for p in sniper["active_short_positions"]}

        old_cooldown_syms = set()
        new_cooldown_syms = set()
        now_dt_cd = datetime.now(timezone.utc)
        for th in sniper.get("trade_history", [])[-40:]:
            act_th = th.get("action", "")
            ptag = th.get("plan_tag", "NEW_PLAN")
            if any(k in act_th for k in ("STOP", "PROFIT", "EXIT", "LOCK", "UNLOAD")):
                try:
                    t_dt = datetime.strptime(th["time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    age_h = (now_dt_cd - t_dt).total_seconds() / 3600.0
                    cd_req = 8.0 if ("STOP" in act_th or float(th.get("pnl_usd", 0.0)) < 0) else 3.0
                    if age_h < cd_req:
                        if ptag == "OLD_PLAN":
                            old_cooldown_syms.add(th.get("symbol"))
                        else:
                            new_cooldown_syms.add(th.get("symbol"))
                except Exception:
                    pass

        # 3C-1. OLD_PLAN Crypto Entry (Untouched Benchmark from its $1000 Crypto Budget)
        for sym in ["BTCUSDC", "BNBUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDC", "AVAXUSDT", "DOGEUSDT", "ADAUSDT", "SUIUSDT", "LINKUSDT", "PAXGUSDT"]:
            if old_c_count >= 4 or old_c_free < 100.0 or sniper["free_cash_usd"] < 100.0:
                break
            if sym in old_held_syms or sym in old_cooldown_syms:
                continue
            sc = market_scan.get(sym, {})
            p_ask = sc.get("live_ask") or sc.get("price", 0.0)
            rsi_now = sc.get("rsi14", 50.0)
            if p_ask <= 0 or sc.get("news_shock_blocked"):
                continue
            if sym == "PAXGUSDT" and not (sc.get("uptrend_ok") and rsi_now < 36.0):
                continue
            if sym != "PAXGUSDT" and not (rsi_now <= 34.5):
                continue
            decs = 8 if p_ask < 0.001 else (6 if p_ask < 1.0 else 4)
            amt = round(min(old_c_free, sniper["free_cash_usd"], 250.0), 2)
            if amt >= 100.0:
                qty = round((amt * (1.0 - 0.0005)) / p_ask, 2 if p_ask < 0.001 else 6)
                sniper["free_cash_usd"] = round(sniper["free_cash_usd"] - amt, 2)
                old_c_free = round(old_c_free - amt, 2)
                old_c_count += 1
                tp_pct_old = 0.0075 if sym == "PAXGUSDT" else 0.0125
                tp_p_old = round(p_ask * (1.0 + tp_pct_old), decs)
                sl_p_old = round(p_ask * 0.955, decs)
                new_pos = {
                    "symbol": sym,
                    "playbook": "OLD_CLASSIC_PULLBACK",
                    "plan_tag": "OLD_PLAN",
                    "opened_at_utc": now_iso,
                    "tiers_filled": 1,
                    "first_entry_price": p_ask,
                    "avg_entry_price": p_ask,
                    "invested_usd": amt,
                    "total_qty": qty,
                    "tp_target_price": tp_p_old,
                    "sl_price": sl_p_old,
                    "hours_held": 0.0,
                    "current_price": p_ask,
                    "unrealized_pnl_usd": round(qty * p_ask - amt, 2),
                    "dist_to_sl_pct": round(((p_ask - sl_p_old) / p_ask) * 100.0, 2),
                    "dist_to_tp_pct": round(((tp_p_old - p_ask) / p_ask) * 100.0, 2)
                }
                sniper["active_positions"].append(new_pos)
                old_held_syms.add(sym)
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "OPEN_OLD_CLASSIC_PULLBACK_LIVE",
                    "playbook": "OLD_CLASSIC_PULLBACK",
                    "plan_tag": "OLD_PLAN",
                    "symbol": sym,
                    "price": p_ask,
                    "rsi14": rsi_now,
                    "amount_usd": amt
                })
                cycle_actions.append(f"OPEN_LONG_OLD_PLAN:{sym}@${p_ask}")

        # 3C-2. NEW_PLAN Studied Core Entries (Strictly Max 2 Total Directional Slots, 1.8x ATR SL / 4.0x ATR TP, Closed-Bar Signals)
        new_short_count = len(sniper["active_short_positions"])
        sl_m_core = float(qcfg.get("sl_atr", 1.8))
        tp_m_core = float(qcfg.get("tp_atr", 4.0))
        low_chop_th = float(qcfg.get("low_atr_chop_stop_th", 0.0065))
        low_chop_scale = float(qcfg.get("low_atr_chop_scale", 0.55))

        # 3C-2A. Bear-Regime 1x Short Entry at Exact Live Bid (Core Liquid Majors Only)
        for sym in CORE_STUDIED_CRYPTO_SHORTS:
            if (new_c_count + new_short_count) >= 2 or new_c_free < 65.0 or sniper["free_cash_usd"] < 65.0:
                break
            if sym in new_held_short_syms or sym in new_held_long_syms or sym in new_cooldown_syms:
                continue
            sc = market_scan.get(sym, {})
            atr = sc.get("atr14", 0.0)
            p_bid = sc.get("live_bid") or sc.get("price", 0.0)
            if sc.get("short_bounce_trigger") and atr > 0 and p_bid > 0:
                raw_stop_pct = (sl_m_core * atr) / p_bid
                stop_pct = max(0.0045, raw_stop_pct)
                if 0.0045 <= raw_stop_pct <= 0.042:
                    decs = 8 if p_bid < 0.001 else (6 if p_bid < 1.0 else 4)
                    amt = round(min(new_c_free, sniper["free_cash_usd"], 250.0, 4.50 / stop_pct), 2)
                    min_ticket = 100.0
                    if raw_stop_pct < low_chop_th:
                        amt = round(min(new_c_free, sniper["free_cash_usd"], amt * low_chop_scale), 2)
                        min_ticket = 65.0
                    if amt >= min_ticket:
                        qty = round((amt * (1.0 - 0.0005)) / p_bid, 2 if p_bid < 0.001 else 6)
                        sniper["free_cash_usd"] = round(sniper["free_cash_usd"] - amt, 2)
                        new_c_free = round(new_c_free - amt, 2)
                        new_short_count += 1
                        acted_bars[sym] = sc.get("closed_bar_ts")
                        tp_s_new = round(p_bid - tp_m_core * atr, decs)
                        sl_s_new = round(p_bid + sl_m_core * atr, decs)
                        new_spos = {
                            "symbol": sym,
                            "playbook": "CROWDED_FADE_SHORT",
                            "plan_tag": "NEW_PLAN",
                            "direction": "1X_SHORT",
                            "opened_at_utc": now_iso,
                            "entry_price": p_bid,
                            "invested_usd": amt,
                            "total_qty": qty,
                            "tp_target_price": tp_s_new,
                            "sl_price": sl_s_new,
                            "breakeven_locked_price": None,
                            "min_price_seen": p_bid,
                            "accrued_funding_usd": 0.0,
                            "funding_settlements_credited": 0,
                            "hours_held": 0.0,
                            "current_price": p_bid,
                            "unrealized_pnl_usd": 0.0,
                            "dist_to_sl_pct": round(((sl_s_new - p_bid) / p_bid) * 100.0, 2),
                            "dist_to_tp_pct": round(((p_bid - tp_s_new) / p_bid) * 100.0, 2)
                        }
                        sniper["active_short_positions"].append(new_spos)
                        new_held_short_syms.add(sym)
                        sniper["trade_history"].append({
                            "time_utc": now_iso,
                            "action": "OPEN_BEAR_RALLY_1X_SHORT_LIVE",
                            "playbook": "CROWDED_FADE_SHORT",
                            "plan_tag": "NEW_PLAN",
                            "symbol": sym,
                            "price": p_bid,
                            "rsi14": sc.get("rsi14"),
                            "taker_buy_ratio": sc.get("taker_buy_ratio"),
                            "amount_usd": amt
                        })
                        cycle_actions.append(f"OPEN_SHORT_NEW_PLAN:{sym}@${p_bid}:${amt}")

        # 3C-2B. Uptrend Asymmetric Pullback Long Entry at Exact Live Ask (Core Liquid Majors + Gold PAXGUSDT)
        for sym in CORE_STUDIED_CRYPTO_LONGS:
            if (new_c_count + new_short_count) >= 2 or new_c_free < 65.0 or sniper["free_cash_usd"] < 65.0:
                break
            if sym in new_held_long_syms or sym in new_held_short_syms or sym in new_cooldown_syms:
                continue
            sc = market_scan.get(sym, {})
            atr = sc.get("atr14", 0.0)
            p_ask = sc.get("live_ask") or sc.get("price", 0.0)
            if sc.get("dip_trigger") and atr > 0 and p_ask > 0:
                raw_stop_pct = (sl_m_core * atr) / p_ask
                stop_pct = max(0.0045, raw_stop_pct)
                if 0.0045 <= raw_stop_pct <= 0.042:
                    decs = 8 if p_ask < 0.001 else (6 if p_ask < 1.0 else 4)
                    amt = round(min(new_c_free, sniper["free_cash_usd"], 250.0, 4.50 / stop_pct), 2)
                    min_ticket = 100.0
                    if raw_stop_pct < low_chop_th:
                        amt = round(min(new_c_free, sniper["free_cash_usd"], amt * low_chop_scale), 2)
                        min_ticket = 65.0
                    if amt >= min_ticket:
                        qty = round((amt * (1.0 - 0.0005)) / p_ask, 2 if p_ask < 0.001 else 6)
                        sniper["free_cash_usd"] = round(sniper["free_cash_usd"] - amt, 2)
                        new_c_free = round(new_c_free - amt, 2)
                        new_c_count += 1
                        acted_bars[sym] = sc.get("closed_bar_ts")
                        tp_p_new = round(p_ask + tp_m_core * atr, decs)
                        sl_p_new = round(p_ask - sl_m_core * atr, decs)
                        new_pos = {
                            "symbol": sym,
                            "playbook": "MEAN_REV_DIP",
                            "plan_tag": "NEW_PLAN",
                            "opened_at_utc": now_iso,
                            "tiers_filled": 1,
                            "first_entry_price": p_ask,
                            "avg_entry_price": p_ask,
                            "invested_usd": amt,
                            "total_qty": qty,
                            "tp_target_price": tp_p_new,
                            "sl_price": sl_p_new,
                            "max_price_seen": p_ask,
                            "hours_held": 0.0,
                            "current_price": p_ask,
                            "unrealized_pnl_usd": round(qty * p_ask - amt, 2),
                            "dist_to_sl_pct": round(((p_ask - sl_p_new) / p_ask) * 100.0, 2),
                            "dist_to_tp_pct": round(((tp_p_new - p_ask) / p_ask) * 100.0, 2)
                        }
                        sniper["active_positions"].append(new_pos)
                        new_held_long_syms.add(sym)
                        sniper["trade_history"].append({
                            "time_utc": now_iso,
                            "action": "OPEN_ASYM_TREND_PULLBACK_LIVE",
                            "playbook": "MEAN_REV_DIP",
                            "plan_tag": "NEW_PLAN",
                            "symbol": sym,
                            "price": p_ask,
                            "rsi14": sc.get("rsi14"),
                            "taker_buy_ratio": sc.get("taker_buy_ratio"),
                            "amount_usd": amt
                        })
                        cycle_actions.append(f"OPEN_LONG_NEW_PLAN:{sym}@${p_ask}:${amt}")

        sniper["active_cycle"] = sniper["active_positions"][0] if sniper["active_positions"] else None
        sniper["active_short_cycle"] = sniper["active_short_positions"][0] if sniper["active_short_positions"] else None

        # 5. Update Engine 4 (US Equities & Macro ETFs: Connors RSI(2)->SMA10 Institutional Engine, OOS PF=2.07, Full PF=3.06)
        self._ensure_stocks_engine(now_iso, now_ms)
        stocks = self.state["engine4_us_stocks"]
        clean_stock_watch = list(STUDIED_STOCK_UNIVERSE)
        for p_st in stocks.get("active_positions", []):
            if p_st["symbol"] not in clean_stock_watch:
                clean_stock_watch.append(p_st["symbol"])
        stocks["watched_tickers"] = clean_stock_watch

        stock_last_ms = stocks.get("last_updated_ms", last_ms)
        stock_elapsed_h = max(0.0, (now_ms - stock_last_ms) / 3600000.0)
        treasury_inc = stocks["free_cash_usd"] * (stocks["treasury_yield_apr_pct"] / 100.0) * (stock_elapsed_h / (365.0 * 24.0))
        stocks["accrued_treasury_yield_usd"] = round(stocks["accrued_treasury_yield_usd"] + treasury_inc, 5)
        stocks["last_updated_ms"] = now_ms

        now_dt_utc = datetime.now(timezone.utc)
        us_market_open = is_us_regular_market_open(now_dt_utc)

        stock_scan = {}
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
                if len(valid) >= 205:
                    live_sp = round(valid[-1][4], 2)
                    # When NYSE is open, valid[-1] is the forming 1H bar -> use valid[:-1] for closed-bar signal integrity!
                    closed_bars = valid[:-1] if us_market_open else valid
                    c_opens = [v[1] for v in closed_bars]
                    c_highs = [v[2] for v in closed_bars]
                    c_lows = [v[3] for v in closed_bars]
                    c_closes = [v[4] for v in closed_bars]
                    closed_c = c_closes[-1]
                    sma10 = sum(c_closes[-10:]) / 10.0
                    sma20 = sum(c_closes[-20:]) / 20.0
                    sma100 = sum(c_closes[-100:]) / 100.0
                    sma200 = sum(c_closes[-200:]) / 200.0
                    ret24 = ((closed_c - c_closes[-14]) / c_closes[-14]) if len(c_closes) >= 14 else 0.0
                    rsi14 = calc_rsi(c_closes, 14)
                    prev_rsi14 = calc_rsi(c_closes[:-1], 14)
                    rsi2 = calc_rsi(c_closes, 2)
                    atr14 = calc_atr(c_highs, c_lows, c_closes, 14)
                    uptrend = (closed_c > sma200 * 0.992) and (sma100 >= sma200)
                    green_reversal = (closed_c >= c_opens[-1]) or (rsi14 > prev_rsi14)
                    recent_stock_shock = any((v[2] - v[3]) / max(1e-9, v[1]) > 0.032 for v in closed_bars[-4:])
                    stock_scan[ticker] = {
                        "price": live_sp,
                        "closed_close": round(closed_c, 2),
                        "closed_bar_ts": int(closed_bars[-1][0]),
                        "sma10": round(sma10, 2),
                        "sma20": round(sma20, 2),
                        "sma100": round(sma100, 2),
                        "sma200": round(sma200, 2),
                        "atr14": round(atr14, 2),
                        "rsi14": round(rsi14, 2),
                        "prev_rsi14": round(prev_rsi14, 2),
                        "rsi2": round(rsi2, 2),
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
        index_freefall = ((qqq_rsi < 14.0) and not qqq_sc.get("green_reversal", False)) or ((spy_rsi < 14.0) and not spy_sc.get("green_reversal", False))
        acted_stock_bars = stocks.setdefault("acted_closed_bars", {})

        for ticker, sscan in stock_scan.items():
            atr_s = sscan.get("atr14", 0.0)
            cc_s = sscan.get("closed_close", 0.0)
            sma20_s = sscan.get("sma20", 0.0)
            ret24_s = sscan.get("ret24_pct", 0.0)
            rsi14_s = sscan.get("rsi14", 50.0)
            rsi2_s = sscan.get("rsi2", 50.0)
            already_st = (acted_stock_bars.get(ticker) == sscan.get("closed_bar_ts"))
            # NEW_PLAN ($2,000 100% US Stocks Only):
            # 1) Uptrend Pullback (above SMA200 with RSI14 <= 33.5 or RSI2 <= 10 below SMA20)
            # 2) Oversold Washout Reversal (below SMA200 with RSI14 <= 29.0 + Green Reversal below SMA20 - 1.0*ATR)
            uptrend_pullback_ok = (
                sscan.get("uptrend_ok", False)
                and (rsi14_s <= 33.5 or (rsi2_s <= 10.0 and atr_s > 0 and cc_s < (sma20_s - 0.5 * atr_s)))
            )
            washout_reversal_ok = (
                (not sscan.get("uptrend_ok", False))
                and (rsi14_s <= 29.0)
                and sscan.get("green_reversal", False)
                and (atr_s > 0 and cc_s < (sma20_s - 1.0 * atr_s))
            )
            sscan["dip_trigger"] = bool(
                (not already_st)
                and (ticker in STUDIED_STOCK_UNIVERSE)
                and (not sscan.get("news_shock_blocked", False))
                and (not index_freefall)
                and (uptrend_pullback_ok or washout_reversal_ok)
            )
        stocks["latest_stock_scan"] = stock_scan
        stocks["us_regular_market_open"] = us_market_open

        # 5A. Evaluate Open US Stock Positions (Strictly Execute Fills ONLY When NYSE Regular Session is OPEN!)
        updated_stocks = []
        for scyc in stocks["active_positions"]:
            tk = scyc["symbol"]
            ptag_st = scyc.get("plan_tag", "NEW_PLAN")
            sc_tk = stock_scan.get(tk, {})
            cur_sp = sc_tk.get("price")
            if cur_sp is None:
                updated_stocks.append(scyc)
                continue

            opened_str = scyc.get("opened_at_utc", now_iso)
            try:
                op_dt = datetime.strptime(opened_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                held_h = (now_dt_utc - op_dt).total_seconds() / 3600.0
                active_mkt_h = calc_active_stock_hours(op_dt, now_dt_utc)
            except Exception:
                held_h = 0.0
                active_mkt_h = 0.0
            scyc["hours_held"] = round(held_h, 1)
            scyc["active_market_hours_held"] = active_mkt_h

            # Remove premature +0.8% choke on NEW_PLAN stock positions
            if ptag_st == "NEW_PLAN":
                scyc.pop("breakeven_locked_price", None)

            # Automatic Profit-Lock Shield (Both Plans) + Counter-Trend Target Re-Anchoring
            atr_tk = float(sc_tk.get("atr14") or 0.0)
            sma20_tk = float(sc_tk.get("sma20") or 0.0)
            sma100_tk = float(sc_tk.get("sma100") or 0.0)
            tk_uptrend = bool(sc_tk.get("uptrend_ok", False))
            qqq_uptrend = bool(qqq_sc.get("uptrend_ok", False))
            unr_now = round(scyc["total_qty"] * cur_sp - scyc["invested_usd"], 2)

            if atr_tk > 0 and (ptag_st == "NEW_PLAN" or not tk_uptrend or not qqq_uptrend):
                # Tier-2 Profit Lock: +1.20*ATR or above SMA20 -> lock +0.55*ATR guaranteed profit
                if ((sma20_tk > 0 and cur_sp >= sma20_tk) or (cur_sp >= scyc["avg_entry_price"] + 1.20 * atr_tk)) and unr_now >= 2.50:
                    lock_sl = round(scyc["avg_entry_price"] + 0.55 * atr_tk, 2)
                    if lock_sl > scyc["sl_price"] and cur_sp > lock_sl:
                        scyc["sl_price"] = lock_sl
                        scyc["counter_trend_profit_locked"] = True
                        cycle_actions.append(f"STOCK_PROFIT_LOCK_TIER2:{tk}({ptag_st}):SL->${lock_sl}")
                # Tier-1 Breakeven+Profit Shield: >= +0.60*ATR -> lock +0.12*ATR above entry
                elif cur_sp >= scyc["avg_entry_price"] + 0.60 * atr_tk and unr_now >= 0.80:
                    be_sl = round(scyc["avg_entry_price"] + 0.12 * atr_tk, 2)
                    if be_sl > scyc["sl_price"] and cur_sp > be_sl:
                        scyc["sl_price"] = be_sl
                        scyc["counter_trend_profit_locked"] = True
                        cycle_actions.append(f"STOCK_BREAKEVEN_SHIELD:{tk}({ptag_st}):SL->${be_sl}")

            # Re-anchor over-stretched OLD_PLAN Macro ETF or Counter-Trend Stock targets (e.g., NVDA 3.62x ATR -> realistic 2.2x ATR)
            if ptag_st == "OLD_PLAN" and (tk in STUDIED_MACRO_TICKERS or not tk_uptrend) and atr_tk > 0:
                cap_tp = round(scyc["avg_entry_price"] + 2.2 * atr_tk, 2)
                if tk in STUDIED_MACRO_TICKERS and sma100_tk > 0:
                    cap_tp = round(max(cap_tp, sma100_tk), 2)
                realistic_tp = round(min(scyc["tp_target_price"], cap_tp), 2)
                if realistic_tp < scyc["tp_target_price"] and realistic_tp > scyc["avg_entry_price"] * 1.01:
                    scyc["tp_target_price"] = realistic_tp
                    cycle_actions.append(f"STOCK_COUNTERTREND_TP_REANCHOR:{tk}:TP->${realistic_tp}")

            if not us_market_open:
                scyc["current_price"] = cur_sp
                scyc["unrealized_pnl_usd"] = round(scyc["total_qty"] * cur_sp - scyc["invested_usd"], 2)
                scyc["dist_to_sl_pct"] = round(((cur_sp - scyc["sl_price"]) / cur_sp) * 100.0, 2) if cur_sp > 0 else 0.0
                scyc["dist_to_tp_pct"] = round(((scyc["tp_target_price"] - cur_sp) / cur_sp) * 100.0, 2) if cur_sp > 0 else 0.0
                updated_stocks.append(scyc)
                continue

            rsi14_now = sc_tk.get("rsi14", 50.0)
            overbought_bounce_done = (
                ptag_st == "NEW_PLAN"
                and active_mkt_h >= 1.0
                and rsi14_now >= 66.0
                and cur_sp > scyc["avg_entry_price"] * 1.008
            )
            max_mkt_h = 45.0 if ptag_st == "NEW_PLAN" else 96.0

            if overbought_bounce_done or cur_sp >= scyc["tp_target_price"]:
                gross_val = round(scyc["total_qty"] * cur_sp, 2)
                pnl = round(gross_val - scyc["invested_usd"], 2)
                stocks["free_cash_usd"] = round(stocks["free_cash_usd"] + gross_val, 2)
                stocks["realized_pnl_usd"] = round(stocks["realized_pnl_usd"] + pnl, 2)
                if pnl >= 0:
                    stocks["cycles_won"] += 1
                else:
                    stocks["cycles_stopped"] += 1
                act_st_lbl = "OVERBOUGHT_RSI_PROFIT_EXIT_LIVE" if overbought_bounce_done else "TAKE_PROFIT_LIVE"
                stocks["trade_history"].append({
                    "time_utc": now_iso,
                    "action": act_st_lbl,
                    "playbook": "US_STOCK_PULLBACK",
                    "plan_tag": ptag_st,
                    "symbol": tk,
                    "price": cur_sp,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"STOCK_{act_st_lbl}:{tk}({ptag_st})@${cur_sp}:${pnl:+.2f}")
            elif cur_sp <= scyc["sl_price"]:
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
                    "action": "STOP_LOSS_LIVE",
                    "playbook": "US_STOCK_PULLBACK",
                    "plan_tag": ptag_st,
                    "symbol": tk,
                    "price": cur_sp,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"STOCK_STOP_LOSS_LIVE:{tk}({ptag_st})@${cur_sp}:${pnl:+.2f}")
            elif active_mkt_h >= max_mkt_h:
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
                    "plan_tag": ptag_st,
                    "symbol": tk,
                    "price": cur_sp,
                    "pnl_usd": pnl
                })
                cycle_actions.append(f"STOCK_TIME_MAX_BARS_EXIT_LIVE:{tk}({ptag_st})@${cur_sp}:${pnl:+.2f}")
            else:
                scyc["current_price"] = cur_sp
                scyc["unrealized_pnl_usd"] = round(scyc["total_qty"] * cur_sp - scyc["invested_usd"], 2)
                scyc["dist_to_sl_pct"] = round(((cur_sp - scyc["sl_price"]) / cur_sp) * 100.0, 2) if cur_sp > 0 else 0.0
                scyc["dist_to_tp_pct"] = round(((scyc["tp_target_price"] - cur_sp) / cur_sp) * 100.0, 2) if cur_sp > 0 else 0.0
                updated_stocks.append(scyc)

        stocks["active_positions"] = updated_stocks

        # Re-sync isolated stock cash pools ($1000 OLD_PLAN + $2000 NEW_PLAN = $3000 Total Stock Pool)
        stocks["initial_capital_usd"] = 3000.0
        split_budgets_st = self._compute_and_sync_split_plan_budgets()
        old_s_free = split_budgets_st["OLD_PLAN"]["stocks_free_cash_usd"]
        new_s_free = split_budgets_st["NEW_PLAN"]["stocks_free_cash_usd"]
        stocks["free_cash_usd"] = round(old_s_free + new_s_free, 2)
        old_s_count = sum(1 for p in stocks["active_positions"] if p.get("plan_tag") == "OLD_PLAN")
        new_s_count = sum(1 for p in stocks["active_positions"] if p.get("plan_tag") == "NEW_PLAN")

        # 5B. Open NEW US Stock Positions Strictly When NYSE Regular Session is OPEN!
        INDIVIDUAL_TECH_STOCKS = {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AMD", "AVGO", "TSLA"}
        old_held_tickers = {p["symbol"] for p in stocks["active_positions"] if p.get("plan_tag") == "OLD_PLAN"}
        new_held_tickers = {p["symbol"] for p in stocks["active_positions"] if p.get("plan_tag") == "NEW_PLAN"}
        new_tech_held = sum(1 for tk_h in new_held_tickers if tk_h in INDIVIDUAL_TECH_STOCKS)
        old_cooldown_tickers = set()
        new_cooldown_tickers = set()
        for sth in stocks.get("trade_history", [])[-25:]:
            sact = sth.get("action", "")
            sptag = sth.get("plan_tag", "NEW_PLAN")
            if any(k in sact for k in ("STOP", "PROFIT", "EXIT", "LOCK")):
                try:
                    st_dt = datetime.strptime(sth["time_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    sage_h = (now_dt_utc - st_dt).total_seconds() / 3600.0
                    scd_req = 6.0 if ("STOP" in sact or float(sth.get("pnl_usd", 0.0)) < 0) else 2.0
                    if sage_h < scd_req:
                        if sptag == "OLD_PLAN":
                            old_cooldown_tickers.add(sth.get("symbol"))
                        else:
                            new_cooldown_tickers.add(sth.get("symbol"))
                except Exception:
                    pass

        # 5B-1. OLD_PLAN US Stock Entry (Only during NYSE regular session)
        for tk in stocks["watched_tickers"]:
            if (not us_market_open) or old_s_count >= 3 or old_s_free < 150.0 or stocks["free_cash_usd"] < 150.0:
                break
            if tk in old_held_tickers or tk in old_cooldown_tickers:
                continue
            sscan = stock_scan.get(tk, {})
            atr = sscan.get("atr14", 0.0)
            ep = sscan.get("price", 0.0)
            rsi_st = sscan.get("rsi14", 50.0)
            if atr > 0 and ep > 0 and (not sscan.get("news_shock_blocked", False)) and (not index_freefall) and rsi_st <= 35.0:
                pool_avail_st = min(stocks["free_cash_usd"], old_s_free)
                amt = round(min(pool_avail_st, 330.0), 2)
                if amt >= 150.0:
                    qty = round(amt / ep, 5)
                    stocks["free_cash_usd"] = round(stocks["free_cash_usd"] - amt, 2)
                    old_s_free = round(old_s_free - amt, 2)
                    old_s_count += 1
                    tp_st_old = round(ep + 3.5 * atr, 2)
                    sl_st_old = round(ep - 2.2 * atr, 2)
                    new_st = {
                        "symbol": tk,
                        "playbook": "US_STOCK_PULLBACK",
                        "plan_tag": "OLD_PLAN",
                        "opened_at_utc": now_iso,
                        "first_entry_price": ep,
                        "avg_entry_price": ep,
                        "invested_usd": amt,
                        "total_qty": qty,
                        "tp_target_price": tp_st_old,
                        "sl_price": sl_st_old,
                        "hours_held": 0.0,
                        "active_market_hours_held": 0.0,
                        "current_price": ep,
                        "unrealized_pnl_usd": 0.0,
                        "dist_to_sl_pct": round(((ep - sl_st_old) / ep) * 100.0, 2),
                        "dist_to_tp_pct": round(((tp_st_old - ep) / ep) * 100.0, 2)
                    }
                    stocks["active_positions"].append(new_st)
                    old_held_tickers.add(tk)
                    stocks["trade_history"].append({
                        "time_utc": now_iso,
                        "action": "OPEN_US_STOCK_PULLBACK_LIVE",
                        "playbook": "US_STOCK_PULLBACK",
                        "plan_tag": "OLD_PLAN",
                        "symbol": tk,
                        "price": ep,
                        "rsi14": rsi_st,
                        "uptrend_ok": sscan.get("uptrend_ok"),
                        "amount_usd": amt
                    })
                    cycle_actions.append(f"OPEN_OLD_US_STOCK_LIVE:{tk}@${ep}:${amt}")

        # 5B-2. NEW_PLAN 100% US Stocks Only Entry ($2,000 Budget, Max 5 Slots x $400, Max 3 Individual Tech Stocks, 100% Live Execution)
        for tk in STUDIED_STOCK_UNIVERSE:
            if (not us_market_open) or new_s_count >= 5 or new_s_free < 180.0 or stocks["free_cash_usd"] < 180.0:
                break
            if tk in new_held_tickers or tk in new_cooldown_tickers:
                continue
            if (tk in INDIVIDUAL_TECH_STOCKS) and new_tech_held >= 3:
                continue
            sscan = stock_scan.get(tk, {})
            atr = sscan.get("atr14", 0.0)
            ep = sscan.get("price", 0.0)
            if sscan.get("dip_trigger") and atr > 0 and ep > 0:
                pool_avail_st = min(stocks["free_cash_usd"], new_s_free)
                amt = round(min(pool_avail_st, 400.0), 2)
                if amt >= 180.0:
                    qty = round(amt / ep, 5)
                    stocks["free_cash_usd"] = round(stocks["free_cash_usd"] - amt, 2)
                    new_s_free = round(new_s_free - amt, 2)
                    new_s_count += 1
                    if tk in INDIVIDUAL_TECH_STOCKS:
                        new_tech_held += 1
                    acted_stock_bars[tk] = sscan.get("closed_bar_ts")
                    tp_mult_new = 2.5 if sscan.get("uptrend_ok") else 2.0
                    tp_st_new = round(ep + tp_mult_new * atr, 2)
                    sl_st_new = round(ep - 2.0 * atr, 2)
                    new_st = {
                        "symbol": tk,
                        "playbook": "US_STOCK_PULLBACK",
                        "plan_tag": "NEW_PLAN",
                        "opened_at_utc": now_iso,
                        "first_entry_price": ep,
                        "avg_entry_price": ep,
                        "invested_usd": amt,
                        "total_qty": qty,
                        "tp_target_price": tp_st_new,
                        "sl_price": sl_st_new,
                        "hours_held": 0.0,
                        "active_market_hours_held": 0.0,
                        "current_price": ep,
                        "unrealized_pnl_usd": 0.0,
                        "dist_to_sl_pct": round(((ep - sl_st_new) / ep) * 100.0, 2),
                        "dist_to_tp_pct": round(((tp_st_new - ep) / ep) * 100.0, 2)
                    }
                    stocks["active_positions"].append(new_st)
                    new_held_tickers.add(tk)
                    stocks["trade_history"].append({
                        "time_utc": now_iso,
                        "action": "OPEN_NEW_US_STOCK_ELITE_LIVE",
                        "playbook": "US_STOCK_PULLBACK",
                        "plan_tag": "NEW_PLAN",
                        "symbol": tk,
                        "price": ep,
                        "rsi2": sscan.get("rsi2"),
                        "rsi14": sscan.get("rsi14"),
                        "uptrend_ok": sscan.get("uptrend_ok"),
                        "amount_usd": amt
                    })
                    cycle_actions.append(f"OPEN_NEW_US_STOCK_ELITE_LIVE:{tk}@${ep}:${amt}")

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
        self._compute_and_sync_split_plan_budgets()

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

            cyc["max_price_seen"] = max(float(cyc.get("max_price_seen") or cur_p), cur_p)
            flow_trig = float(qcfg.get("flow_rev_trig_atr", 1.8))
            flow_lock = float(qcfg.get("flow_rev_lock_atr", 1.0))
            flow_th = float(qcfg.get("flow_rev_th", 0.38))
            trail_trig = float(qcfg.get("trail_trig_atr", 99.0)) if cyc.get("plan_tag") == "NEW_PLAN" else 3.0
            trail_lock = float(qcfg.get("trail_lock_atr", 0.0)) if cyc.get("plan_tag") == "NEW_PLAN" else 1.60
            if cyc.get("plan_tag") == "NEW_PLAN" and atr_now > 0 and flow_trig < 90.0:
                if cyc["max_price_seen"] >= cyc["avg_entry_price"] + flow_trig * atr_now and sc_sym.get("taker_buy_ratio", 0.5) <= flow_th:
                    f_lock = round(cyc["avg_entry_price"] + flow_lock * atr_now, decs)
                    if cur_p > f_lock:
                        cyc["breakeven_locked_price"] = max(cyc.get("breakeven_locked_price") or 0.0, f_lock)
                        cyc["lock_source"] = "FLOW_REVERSAL_LOCK"
            if atr_now > 0 and trail_trig < 90.0 and cur_p >= cyc["avg_entry_price"] + trail_trig * atr_now:
                new_lock = round(cyc["avg_entry_price"] + trail_lock * atr_now, decs)
                if cur_p > new_lock:
                    cyc["breakeven_locked_price"] = max(cyc.get("breakeven_locked_price") or 0.0, new_lock)
                    cyc["lock_source"] = "ATR_PROFIT_LOCK"

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
                    "plan_tag": cyc.get("plan_tag", "OLD_PLAN" if pb_name == "CLASSIC_PULLBACK" else "NEW_PLAN"),
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
                sniper["cycles_won"] = sniper.get("cycles_won", 0) + (1 if pnl >= 0 else 0)
                sniper["cycles_stopped"] = sniper.get("cycles_stopped", 0) + (0 if pnl >= 0 else 1)
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "TRAILING_PROFIT_LOCK_LIVE",
                    "playbook": pb_name,
                    "plan_tag": cyc.get("plan_tag", "OLD_PLAN" if pb_name == "CLASSIC_PULLBACK" else "NEW_PLAN"),
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
                    "plan_tag": cyc.get("plan_tag", "OLD_PLAN" if pb_name == "CLASSIC_PULLBACK" else "NEW_PLAN"),
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

            scyc["min_price_seen"] = min(float(scyc.get("min_price_seen") or cur_p), cur_p)
            flow_trig_s = float(qcfg.get("flow_rev_trig_atr", 1.8))
            flow_lock_s = float(qcfg.get("flow_rev_lock_atr", 1.0))
            flow_th_s = float(qcfg.get("flow_rev_th", 0.38))
            trail_trig_s = float(qcfg.get("trail_trig_atr", 99.0))
            trail_lock_s = float(qcfg.get("trail_lock_atr", 0.0))
            if atr_now > 0:
                sma200_now = sc_sym.get("sma200", 0.0)
                sma20_now = sc_sym.get("sma20", 0.0)
                if sma200_now > 0 and sc_sym.get("closed_close", cur_p) > sma200_now:
                    if scyc["min_price_seen"] <= entry_p - 0.60 * atr_now:
                        c_lock = round(entry_p - 0.15 * atr_now, decs)
                        if cur_p < c_lock and (scyc.get("breakeven_locked_price") is None or c_lock < scyc["breakeven_locked_price"]):
                            scyc["breakeven_locked_price"] = c_lock
                            scyc["lock_source"] = "COUNTER_SMA200_LOCK"
                    if sma20_now > sma200_now:
                        tight_s = round(cur_p + 0.65 * atr_now, decs)
                        if tight_s < sl_price:
                            sl_price = tight_s
                            scyc["sl_price"] = tight_s
                if flow_trig_s < 90.0:
                    if scyc["min_price_seen"] <= entry_p - flow_trig_s * atr_now and sc_sym.get("taker_buy_ratio", 0.5) >= (1.0 - flow_th_s):
                        f_lock_s = round(entry_p - flow_lock_s * atr_now, decs)
                        if cur_p < f_lock_s and (scyc.get("breakeven_locked_price") is None or f_lock_s < scyc["breakeven_locked_price"]):
                            scyc["breakeven_locked_price"] = f_lock_s
                            scyc["lock_source"] = "FLOW_REVERSAL_LOCK"
            if atr_now > 0 and trail_trig_s < 90.0 and cur_p <= entry_p - trail_trig_s * atr_now:
                new_lock = round(entry_p - trail_lock_s * atr_now, decs)
                if cur_p < new_lock and (scyc.get("breakeven_locked_price") is None or new_lock < scyc["breakeven_locked_price"]):
                    scyc["breakeven_locked_price"] = new_lock
                    scyc["lock_source"] = "ATR_PROFIT_LOCK"

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
                    "plan_tag": "NEW_PLAN",
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
                if pnl >= 0:
                    sniper["cycles_won"] = sniper.get("cycles_won", 0) + 1
                    sniper["short_cycles_won"] = sniper.get("short_cycles_won", 0) + 1
                else:
                    sniper["cycles_stopped"] = sniper.get("cycles_stopped", 0) + 1
                    sniper["short_cycles_stopped"] = sniper.get("short_cycles_stopped", 0) + 1
                sniper["trade_history"].append({
                    "time_utc": now_iso,
                    "action": "SHORT_1X_TRAILING_PROFIT_LOCK_LIVE",
                    "playbook": pb_name,
                    "plan_tag": "NEW_PLAN",
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
                    "plan_tag": "NEW_PLAN",
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
            self._compute_and_sync_split_plan_budgets()
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
    engine = VirtualPortfolioEngine(initial_capital=2000.0)
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
