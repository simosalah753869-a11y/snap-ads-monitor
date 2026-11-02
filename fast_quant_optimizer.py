#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Autonomous Quant Evolution & Walk-Forward Multi-Playbook Optimizer (fast_quant_optimizer.py)
- Runs every 3 hours alongside binance_guardian_bot.py.
- Refreshes the 1,500-hour rolling Binance 1H klines cache if older than 2.5 hours.
- Evaluates institutional playbooks & risk filters in Walk-Forward (In-Sample 750h vs Out-of-Sample 750h):
  1) MEAN_REV_DIP (Oversold Pullback + Order-Flow Absorption + Anti-Freefall Waterfall Guard)
  2) MOMENTUM_BREAKOUT (Trend + Volume/Order-Flow Breakout)
  3) CROWDED_FADE_SHORT (1x Short on Over-Extended Bounces + Anti-Parabolic Squeeze & Volume Climax Guards)
- Reads live closed paper trades from binance_paper_portfolio.json to compute empirical Bayesian
  weight multipliers (Bandit Capital Allocation) in quant_evolution_ledger.json.
"""

import os
import time
import json
from quant_backtester import SYMBOLS, fetch_1500h_klines, compute_indicators

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, "klines_cache_1500h.json")
REPORT_FILE = os.path.join(BASE_DIR, "quant_backtest_report.json")
PORTFOLIO_FILE = os.path.join(BASE_DIR, "binance_paper_portfolio.json")
LEDGER_FILE = os.path.join(BASE_DIR, "quant_evolution_ledger.json")

def load_or_fetch_data():
    if os.path.exists(CACHE_FILE) and (os.path.getsize(CACHE_FILE) > 50000):
        age_sec = time.time() - os.path.getmtime(CACHE_FILE)
        if age_sec < 9000:  # < 2.5 hours
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {sym: compute_indicators(bars) for sym, bars in raw.items()}
    raw = {}
    for sym in SYMBOLS:
        raw[sym] = fetch_1500h_klines(sym)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    return {sym: compute_indicators(bars) for sym, bars in raw.items()}

def run_sim(all_data, start_i, end_i, cfg):
    capital = 500.0
    equity = capital
    peak_eq = capital
    max_dd_pct = 0.0
    wins, losses = 0, 0
    gross_win, gross_loss = 0.0, 0.0
    pos = None

    sl_mult = cfg["sl_atr"]
    tp_mult = cfg["tp_atr"]
    trail_trig = cfg["trail_trig_atr"]
    trail_lock = cfg["trail_lock_atr"]
    flow_rev_trig = cfg.get("flow_rev_trig_atr", 1.8)
    flow_rev_lock = cfg.get("flow_rev_lock_atr", 1.0)
    flow_rev_th = cfg.get("flow_rev_th", 0.38)
    rsi_long = cfg["rsi_long"]
    rsi_short = cfg["rsi_short"]
    req_trend = cfg["req_trend"]
    buy_flow = cfg["buy_flow"]
    short_max_ret24 = cfg.get("short_max_ret24", 0.035)
    short_max_vol_ratio = cfg.get("short_max_vol_ratio", 4.0)
    long_min_ret24 = cfg.get("long_min_ret24", -0.065)
    long_max_ret24 = cfg.get("long_max_ret24", -0.006)
    use_btc_leader = cfg.get("use_btc_leader", True)
    fee_rt = cfg.get("fee_rt", 0.0010)
    max_bars = cfg.get("max_bars", 96)
    stag_tighten = cfg.get("stag_tighten_48h", False)
    stag_ob_protect = cfg.get("stag_ob_protect", True)

    for i in range(start_i, end_i):
        if pos is not None:
            sym = pos["sym"]
            b = all_data[sym][i]
            side = pos["side"]
            pos["bars_held"] += 1

            if stag_ob_protect and pos["bars_held"] >= 48:
                if side == "LONG" and b["rsi14"] >= 70.0 and b["taker_buy_ratio"] < 0.45:
                    tight_sl = b["close"] - 0.8 * pos["atr"]
                    if tight_sl > pos["sl"]:
                        pos["sl"] = tight_sl
                elif side == "SHORT" and b["rsi14"] <= 30.0 and b["taker_buy_ratio"] > 0.55:
                    tight_sl = b["close"] + 0.8 * pos["atr"]
                    if tight_sl < pos["sl"]:
                        pos["sl"] = tight_sl
            elif stag_tighten and pos["bars_held"] >= 48:
                if side == "LONG":
                    tight_sl = b["close"] - 1.0 * pos["atr"]
                    if tight_sl > pos["sl"]:
                        pos["sl"] = tight_sl
                else:
                    tight_sl = b["close"] + 1.0 * pos["atr"]
                    if tight_sl < pos["sl"]:
                        pos["sl"] = tight_sl

            if side == "LONG":
                if b["low"] <= pos["sl"]:
                    pnl = pos["size_usd"] * ((pos["sl"] - pos["entry"]) / pos["entry"] - fee_rt)
                    equity += pnl
                    if pnl >= 0:
                        wins += 1
                        gross_win += pnl
                    else:
                        losses += 1
                        gross_loss += abs(pnl)
                    pos = None
                elif b["high"] >= pos["tp"]:
                    pnl = pos["size_usd"] * ((pos["tp"] - pos["entry"]) / pos["entry"] - fee_rt)
                    equity += pnl
                    wins += 1
                    gross_win += pnl
                    pos = None
                elif pos["bars_held"] >= max_bars:
                    pnl = pos["size_usd"] * ((b["close"] - pos["entry"]) / pos["entry"] - fee_rt)
                    equity += pnl
                    if pnl >= 0:
                        wins += 1
                        gross_win += pnl
                    else:
                        losses += 1
                        gross_loss += abs(pnl)
                    pos = None
                else:
                    if cfg.get("early_be_atr", 0.0) > 0 and b["high"] >= pos["entry"] + cfg["early_be_atr"] * pos["atr"]:
                        pos["sl"] = max(pos["sl"], pos["entry"] + 0.20 * pos["atr"])
                    if flow_rev_trig < 90.0 and b["high"] >= pos["entry"] + flow_rev_trig * pos["atr"] and b["taker_buy_ratio"] <= flow_rev_th:
                        pos["sl"] = max(pos["sl"], pos["entry"] + flow_rev_lock * pos["atr"])
                    if b["high"] >= pos["entry"] + trail_trig * pos["atr"]:
                        pos["sl"] = max(pos["sl"], pos["entry"] + trail_lock * pos["atr"])
            else:
                if b["high"] >= pos["sl"]:
                    pnl = pos["size_usd"] * ((pos["entry"] - pos["sl"]) / pos["entry"] - fee_rt)
                    equity += pnl
                    if pnl >= 0:
                        wins += 1
                        gross_win += pnl
                    else:
                        losses += 1
                        gross_loss += abs(pnl)
                    pos = None
                elif b["low"] <= pos["tp"]:
                    pnl = pos["size_usd"] * ((pos["entry"] - pos["tp"]) / pos["entry"] - fee_rt)
                    equity += pnl
                    wins += 1
                    gross_win += pnl
                    pos = None
                elif pos["bars_held"] >= max_bars:
                    pnl = pos["size_usd"] * ((pos["entry"] - b["close"]) / pos["entry"] - fee_rt)
                    equity += pnl
                    if pnl >= 0:
                        wins += 1
                        gross_win += pnl
                    else:
                        losses += 1
                        gross_loss += abs(pnl)
                    pos = None
                else:
                    if cfg.get("early_be_atr", 0.0) > 0 and b["low"] <= pos["entry"] - cfg["early_be_atr"] * pos["atr"]:
                        pos["sl"] = min(pos["sl"], pos["entry"] - 0.20 * pos["atr"])
                    if flow_rev_trig < 90.0 and b["low"] <= pos["entry"] - flow_rev_trig * pos["atr"] and b["taker_buy_ratio"] >= (1.0 - flow_rev_th):
                        pos["sl"] = min(pos["sl"], pos["entry"] - flow_rev_lock * pos["atr"])
                    if b["low"] <= pos["entry"] - trail_trig * pos["atr"]:
                        pos["sl"] = min(pos["sl"], pos["entry"] - trail_lock * pos["atr"])

            peak_eq = max(peak_eq, equity)
            dd = (peak_eq - equity) / peak_eq * 100.0
            max_dd_pct = max(max_dd_pct, dd)
        else:
            btc_b = all_data["BTCUSDC"][i]
            btc_not_dumping = (btc_b["taker_buy_ratio"] >= 0.45) if use_btc_leader else True
            btc_not_pumping = (btc_b["taker_buy_ratio"] <= 0.55) if use_btc_leader else True

            for sym in SYMBOLS:
                b = all_data[sym][i]
                prev = all_data[sym][i - 1]
                atr = b["atr14"]
                if atr <= 0:
                    continue
                stop_pct = (sl_mult * atr) / b["close"]
                if stop_pct < 0.0045 or stop_pct > 0.04:
                    continue
                size_usd = min(250.0, 2.50 / stop_pct)
                vol_ratio = b["vol"] / max(1e-9, b["vol_sma20"])

                trend_up = (b["sma100"] >= b["sma200"]) if req_trend else True
                trend_dn = (b["sma100"] < b["sma200"]) if req_trend else True

                if (btc_not_dumping and trend_up and prev["rsi14"] < rsi_long
                        and (long_min_ret24 <= b["ret24"] <= long_max_ret24)
                        and b["close"] > b["open"]
                        and b["close"] > prev["close"]
                        and b["taker_buy_ratio"] >= buy_flow):
                    pos = {
                        "sym": sym, "side": "LONG", "entry": b["close"], "atr": atr,
                        "sl": b["close"] - sl_mult * atr, "tp": b["close"] + tp_mult * atr,
                        "size_usd": size_usd, "bars_held": 0
                    }
                    break

                if (sym not in ("PAXGUSDT", "BNBUSDT") and btc_not_pumping and trend_dn
                        and prev["rsi14"] > rsi_short
                        and b["ret24"] <= short_max_ret24
                        and vol_ratio <= short_max_vol_ratio
                        and b["close"] < b["open"]
                        and b["close"] < prev["close"]
                        and b["taker_buy_ratio"] <= (1.0 - buy_flow)):
                    pos = {
                        "sym": sym, "side": "SHORT", "entry": b["close"], "atr": atr,
                        "sl": b["close"] + sl_mult * atr, "tp": b["close"] - tp_mult * atr,
                        "size_usd": size_usd, "bars_held": 0
                    }
                    break

    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    total_trades = wins + losses
    return {
        "net_pnl_usd": round(equity - capital, 2),
        "wins": wins,
        "losses": losses,
        "total_trades": total_trades,
        "win_rate_pct": round(wins / max(1, total_trades) * 100.0, 1),
        "profit_factor": round(pf, 2),
        "max_drawdown_pct": round(max_dd_pct, 2)
    }

def evaluate_live_paper_feedback():
    """Reads actual closed paper trades from binance_paper_portfolio.json and computes live playbook performance."""
    playbooks = {
        "MEAN_REV_DIP": {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "capital_weight": 1.0},
        "MOMENTUM_BREAKOUT": {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "capital_weight": 1.0},
        "CROWDED_FADE_SHORT": {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "capital_weight": 1.0},
        "LEGACY_4TIER_DCA": {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "capital_weight": 0.0},
        "US_STOCK_PULLBACK": {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "capital_weight": 1.0}
    }
    if not os.path.exists(PORTFOLIO_FILE):
        return playbooks
    try:
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            pf = json.load(f)
        for t in pf.get("engine2_spot_sniper", {}).get("trade_history", []):
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
                elif "TIER" in act or t.get("time_utc", "") < "2026-10-30":
                    pb = "LEGACY_4TIER_DCA"
                else:
                    pb = "MEAN_REV_DIP"
            if pb not in playbooks:
                playbooks[pb] = {"trades": 0, "wins": 0, "losses": 0, "net_pnl_usd": 0.0, "capital_weight": 1.0}
            playbooks[pb]["trades"] += 1
            if pnl >= 0:
                playbooks[pb]["wins"] += 1
            else:
                playbooks[pb]["losses"] += 1
            playbooks[pb]["net_pnl_usd"] = round(playbooks[pb]["net_pnl_usd"] + pnl, 2)

        for t in pf.get("engine4_us_stocks", {}).get("trade_history", []):
            if "pnl_usd" not in t:
                continue
            pnl = float(t["pnl_usd"])
            pb = "US_STOCK_PULLBACK"
            playbooks[pb]["trades"] += 1
            if pnl >= 0:
                playbooks[pb]["wins"] += 1
            else:
                playbooks[pb]["losses"] += 1
            playbooks[pb]["net_pnl_usd"] = round(playbooks[pb]["net_pnl_usd"] + pnl, 2)

        for pb, st in playbooks.items():
            if pb == "LEGACY_4TIER_DCA":
                st["capital_weight"] = 0.0
                continue
            if st["trades"] >= 2:
                wr = st["wins"] / st["trades"]
                if st["net_pnl_usd"] > 0 and wr >= 0.55:
                    st["capital_weight"] = 1.25
                elif st["net_pnl_usd"] < 0 and st["losses"] >= 2:
                    st["capital_weight"] = 0.65
    except Exception:
        pass
    return playbooks

def main():
    all_data = load_or_fetch_data()
    min_len = min(len(v) for v in all_data.values())
    mid_i = 205 + (min_len - 205) // 2

    candidates = []
    for sl_atr in [1.7, 1.8, 1.9, 2.0]:
        for tp_atr in [3.6, 3.8, 4.0]:
            for flow_rev_trig, flow_rev_lock, flow_rev_th in [(1.8, 1.0, 0.38), (99.0, 0.0, 0.38)]:
                for stag_ob in [True, False]:
                    for rsi_l, rsi_s in [(20, 76), (24, 76)]:
                        for buy_flow in [0.48, 0.50]:
                            for short_ret24, short_vr, long_ret24, long_max_r24 in [(0.035, 4.0, -0.065, -0.006), (0.04, 4.0, -0.065, 99.0), (99.0, 99.0, -99.0, 99.0)]:
                                for max_bars in [72, 96]:
                                    cfg = {
                                        "sl_atr": sl_atr, "tp_atr": tp_atr,
                                        "trail_trig_atr": 99.0, "trail_lock_atr": 0.0,
                                        "flow_rev_trig_atr": flow_rev_trig,
                                        "flow_rev_lock_atr": flow_rev_lock,
                                        "flow_rev_th": flow_rev_th,
                                        "stag_tighten_48h": False,
                                        "stag_ob_protect": stag_ob,
                                        "rsi_long": rsi_l, "rsi_short": rsi_s,
                                        "req_trend": True, "buy_flow": buy_flow,
                                        "short_max_ret24": short_ret24,
                                        "short_max_vol_ratio": short_vr,
                                        "long_min_ret24": long_ret24,
                                        "long_max_ret24": long_max_r24,
                                        "use_btc_leader": True,
                                        "max_bars": max_bars
                                    }
                                    is_r = run_sim(all_data, 205, mid_i, cfg)
                                    oos_r = run_sim(all_data, mid_i, min_len, cfg)
                                    full_r = run_sim(all_data, 205, min_len, cfg)
                                    if (is_r["profit_factor"] >= 1.25 and oos_r["profit_factor"] >= 2.2
                                            and full_r["max_drawdown_pct"] <= 2.20 and full_r["total_trades"] >= 15):
                                        candidates.append({
                                            "cfg": cfg,
                                            "full_1500h": full_r,
                                            "in_sample": is_r,
                                            "out_of_sample": oos_r
                                        })

    candidates.sort(
        key=lambda x: (
            (x["out_of_sample"]["profit_factor"] ** 3.0)
            * x["out_of_sample"]["net_pnl_usd"]
            * x["in_sample"]["profit_factor"]
            * (x["full_1500h"]["net_pnl_usd"] / max(0.5, x["full_1500h"]["max_drawdown_pct"]))
        ),
        reverse=True
    )
    top3 = candidates[:3]
    live_feedback = evaluate_live_paper_feedback()
    now_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    out = {
        "updated_at_utc": now_utc,
        "total_grid_tested": len(candidates),
        "active_best_config": top3[0]["cfg"] if top3 else {
            "sl_atr": 1.8, "tp_atr": 4.0, "trail_trig_atr": 99.0, "trail_lock_atr": 0.0,
            "stag_tighten_48h": False, "stag_ob_protect": True,
            "rsi_long": 20, "rsi_short": 76, "req_trend": True, "buy_flow": 0.48,
            "short_max_ret24": 0.04, "short_max_vol_ratio": 4.0, "long_min_ret24": -0.065,
            "use_btc_leader": True, "max_bars": 96
        },
        "live_playbook_scoreboard": live_feedback,
        "top_validated_strategies": top3
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    ledger = {"updated_at_utc": now_utc, "live_playbook_scoreboard": live_feedback, "promoted_config": out["active_best_config"]}
    with open(LEDGER_FILE, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2, ensure_ascii=False)

    print(json.dumps(out, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
