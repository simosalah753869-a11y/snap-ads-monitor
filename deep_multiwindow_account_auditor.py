import csv
import ctypes
import glob
import io
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
import websocket

sys.stdout.reconfigure(encoding="utf-8")

_orig_getaddrinfo = socket.getaddrinfo
_dns_cache = {}


def _doh_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    except Exception:
        if host not in _dns_cache:
            try:
                req = urllib.request.Request(
                    f"https://1.1.1.1/dns-query?name={host}&type=A",
                    headers={"accept": "application/dns-json"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    r = json.loads(resp.read().decode("utf-8"))
                ips = [a["data"] for a in r.get("Answer", []) if a.get("type") == 1]
                if ips:
                    _dns_cache[host] = ips[0]
            except Exception:
                pass
        if host in _dns_cache:
            return _orig_getaddrinfo(_dns_cache[host], port, family, type, proto, flags)
        raise


socket.getaddrinfo = _doh_getaddrinfo

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

SA_TZ = timezone(timedelta(hours=3))
REPORT_FILE = BASE_DIR / "multiwindow_audit_latest_report.md"

# القواعد الصارمة المحدثة بأمر المستخدم: سعر البيعة الأقصى 7.00$ للسعودية (10.00$-12.00$ للخليج) عبر 4 نوافذ (1D, 2D, 3D, 7D)
MAX_CPA_SA = 7.00
MAX_CPA_GULF = 12.00
NO_SALE_AD_STOP_SA = 4.00
NO_SALE_AD_STOP_GULF = 8.50
NO_SALE_SQUAD_STOP_SA = 4.00
NO_SALE_SQUAD_STOP_GULF = 8.00


def keep_adspower_offscreen(port=None):
    if port:
        try:
            ver = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2.5).read().decode("utf-8"))
            b_ws_url = ver.get("webSocketDebuggerUrl", "").replace("ws://localhost:", "ws://127.0.0.1:")
            tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2.5).read().decode("utf-8"))
            g_tabs = [t for t in tabs if t.get("type") == "page" and "ads.google.com" in t.get("url", "")]
            if b_ws_url and g_tabs:
                bws = websocket.create_connection(b_ws_url, suppress_origin=True, timeout=4)
                bws.send(json.dumps({"id": 1, "method": "Browser.getWindowForTarget", "params": {"targetId": g_tabs[0]["id"]}}))
                rw = json.loads(bws.recv()).get("result", {})
                if "windowId" in rw:
                    bws.send(json.dumps({"id": 2, "method": "Browser.setWindowBounds", "params": {"windowId": rw["windowId"], "bounds": {"windowState": "normal"}}}))
                    bws.recv()
                    bws.send(json.dumps({"id": 3, "method": "Browser.setWindowBounds", "params": {"windowId": rw["windowId"], "bounds": {"left": -3200, "top": -3200, "width": 1440, "height": 900, "windowState": "normal"}}}))
                    bws.recv()
                bws.close()
        except Exception:
            pass
    try:
        user32 = ctypes.windll.user32

        def cb(hwnd, lParam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if any(k in buf.value for k in ("SunBrowser", "Google Ads", "AdsPower")):
                        user32.SetWindowPos(hwnd, 1, -3200, -3200, 1440, 900, 0x0010 | 0x0004)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    except Exception:
        pass


def is_sheet_gift_row(prod_name: str) -> bool:
    p = (prod_name or "").strip().lower()
    return any(g in p for g in ("باور بانك صغير للطوارئ", "سوار رجالي", "سماعات", "هدية", "مجانا", "مجاناً"))


def classify_product_and_country(text_prod: str, text_country: str = "") -> tuple:
    t = f"{text_prod} {text_country}".lower()
    if "باور بانك صغير للطوارئ" in t:
        p_cat = "car"
    elif any(k in t for k in ("watch", "ساع", "ساعه", "ساعات", "سوار رجالي")):
        p_cat = "watch"
    elif any(k in t for k in ("power", "باور", "شاحن", "voltrx", "سماعات")):
        p_cat = "power"
    elif any(k in t for k in ("kashaf", "كشاف")):
        p_cat = "kashaf"
    elif any(k in t for k in ("car", "armrest", "سيار", "وساد", "تكاي", "مسند")):
        p_cat = "car"
    else:
        p_cat = "other"

    c_raw = (text_country or text_prod).lower()
    if any(k in c_raw for k in ("oman", "عمان", "عُمان")) or c_raw.strip() == "om":
        c_cat = "om"
    elif any(k in c_raw for k in ("qatar", "قطر")) or c_raw.strip() == "qa":
        c_cat = "qa"
    elif any(k in c_raw for k in ("kuwait", "كويت")) or c_raw.strip() == "kw":
        c_cat = "kw"
    elif any(k in c_raw for k in ("uae", "امارات", "إمارات")) or c_raw.strip() == "ae":
        c_cat = "ae"
    elif any(k in c_raw for k in ("bahrain", "بحرين")) or c_raw.strip() == "bh":
        c_cat = "bh"
    else:
        c_cat = "sa"
    return p_cat, c_cat


def fetch_sheet_multiwindow_sales(ref_date_str: str = None) -> dict:
    """Fetches Lightfunnels Google Sheet orders and aggregates by UTM across 1D, 2D, 3D, and 7D."""
    csv_url = "https://docs.google.com/spreadsheets/d/1AaGXjCDbrD7tLEpYaJf5cncgZ3HW5IP3J8TIVAcn0pE/gviz/tq?tqx=out:csv"
    csv_text = ""
    for _ in range(3):
        try:
            r = requests.get(csv_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            if r.status_code == 200 and r.text:
                csv_text = r.text
                break
        except Exception:
            time.sleep(1.0)
    if not csv_text:
        req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
        csv_text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="replace")

    rows = list(csv.reader(io.StringIO(csv_text)))
    now_sa = datetime.now(SA_TZ)
    if ref_date_str:
        try:
            base_dt = datetime.strptime(ref_date_str, "%Y-%m-%d").replace(tzinfo=SA_TZ)
        except Exception:
            base_dt = now_sa
    else:
        base_dt = now_sa

    d_1d = {base_dt.strftime("%Y-%m-%d"), now_sa.strftime("%Y-%m-%d")}
    # حماية منتصف الليل (00:00 إلى 04:00 فجراً بتوقيت السعودية): شمل مبيعات يوم أمس في 1D لمنع أي فرق توقيت بين الشيت وحسابات جوجل أدز
    if now_sa.hour < 4:
        d_1d.add((now_sa - timedelta(days=1)).strftime("%Y-%m-%d"))
    d_2d = {(base_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(2)} | d_1d
    d_3d = {(base_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)} | d_1d
    d_7d = {(base_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)} | d_1d

    last_time_by_utm = {}
    seen_keys = set()
    s1 = defaultdict(int)
    s2 = defaultdict(int)
    s3 = defaultdict(int)
    s7 = defaultdict(int)
    s1_strict = defaultdict(int)
    s2_strict = defaultdict(int)
    s3_strict = defaultdict(int)
    s7_strict = defaultdict(int)
    by_day_utm = defaultdict(lambda: defaultdict(int))

    for r in rows[1:]:
        if len(r) < 3:
            continue
        prod_raw = (r[0] or "").strip()
        utm_raw = (r[1] or "").strip()
        date_raw = (r[2] or "").strip()
        country_raw = (r[3] or "").strip() if len(r) > 3 else ""
        if not utm_raw or not date_raw:
            continue
        # تجاهل سطر الهدية المرفقة مع الطلب حتى تُحسب الليد مع هديتها كطلب واحد فقط (ليد واحدة)
        if is_sheet_gift_row(prod_raw):
            continue
        d_str = date_raw.split(" ")[0]
        if d_str not in d_7d:
            continue
        utm = utm_raw.lower()
        try:
            dt = datetime.strptime(date_raw[:19], "%Y-%m-%d %H:%M:%S")
            t_ms = dt.timestamp() * 1000.0
        except Exception:
            t_ms = 0.0
        ukey = f"{utm}|{date_raw}"
        if ukey in seen_keys:
            continue
        if t_ms > 0 and utm in last_time_by_utm and abs(t_ms - last_time_by_utm[utm]) <= 60000:
            seen_keys.add(ukey)
            continue
        seen_keys.add(ukey)
        if t_ms > 0:
            last_time_by_utm[utm] = t_ms

        p_cat, c_cat = classify_product_and_country(prod_raw, country_raw)
        strict_key = f"{utm}|{p_cat}|{c_cat}"

        by_day_utm[d_str][utm] += 1
        if d_str in d_1d:
            s1[utm] += 1
            s1_strict[strict_key] += 1
        if d_str in d_2d:
            s2[utm] += 1
            s2_strict[strict_key] += 1
        if d_str in d_3d:
            s3[utm] += 1
            s3_strict[strict_key] += 1
        if d_str in d_7d:
            s7[utm] += 1
            s7_strict[strict_key] += 1

    return {
        "1d": dict(s1),
        "2d": dict(s2),
        "3d": dict(s3),
        "7d": dict(s7),
        "1d_strict": dict(s1_strict),
        "2d_strict": dict(s2_strict),
        "3d_strict": dict(s3_strict),
        "7d_strict": dict(s7_strict),
        "by_day": {k: dict(v) for k, v in by_day_utm.items()},
    }


# ==============================================================================
# PART 1 & 2: RUN SNAPCHAT & TIKTOK MONITORS + MULTI-WINDOW DISSECTION
# ==============================================================================
def audit_snapchat_multiwindow(sheet_sales: dict) -> dict:
    from snap_auth import load_config, refresh_access_token
    import snap_monitor as sm

    print("\n======================================================================")
    print("👻 [1/3] فحص وتفصيص جميع حسابات سناب شات والفيديوهات عبر 4 نوافذ (1D / 2D / 3D / 7D) بحد CPA <= $7.00")
    print("======================================================================", flush=True)

    token = refresh_access_token(load_config())
    account_ids = getattr(sm, "TARGET_ACCOUNT_IDS", [sm.TARGET_ACCOUNT_ID])
    snap_summary = []
    snap_videos_summary = []
    extra_paused = []
    history = sm.load_paused_history()
    now_str = datetime.now(SA_TZ).strftime("%Y-%m-%d %H:%M GMT+3")

    for acc_id in account_ids:
        # Run standard snap_monitor on each account (which uses 1_DAY attribution & $7.00 max CPA across 1D/2D/3D/7D)
        sm.run_automation(token, acc_id, dry_run=False)

        # Fetch fresh post-action snapshot to verify 1000%
        camps = sm.api_get_paginated(f"/adaccounts/{acc_id}/campaigns?limit=200", token, "campaigns", "campaign")
        active_camps = [c for c in camps if c.get("status") == "ACTIVE"]

        c_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=0)}
        c_2d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=1)}
        c_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=2)}
        c_7d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=6)}

        ad_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "ad", days_back=0)}
        ad_2d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "ad", days_back=1)}
        ad_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "ad", days_back=2)}
        ad_7d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "ad", days_back=6)}

        for c in active_camps:
            cid = c["id"]
            cname = c.get("name", "")
            s1 = c_1d.get(cid, {"spend": 0.0, "purchases": 0, "cpa": None})
            s2 = c_2d.get(cid, {"spend": 0.0, "purchases": 0, "cpa": None})
            s3 = c_3d.get(cid, {"spend": 0.0, "purchases": 0, "cpa": None})
            s7 = c_7d.get(cid, {"spend": 0.0, "purchases": 0, "cpa": None})

            # Double check all active ads inside this campaign
            c_ads = sm.api_get_paginated(f"/campaigns/{cid}/ads?limit=100", token, "ads", "ad", max_pages=60)
            c_squads = {sq["id"]: sq for sq in sm.api_get_paginated(f"/campaigns/{cid}/adsquads?limit=100", token, "adsquads", "adsquad", max_pages=50)}
            act_ads = [a for a in c_ads if a.get("status") == "ACTIVE" and c_squads.get(a.get("ad_squad_id", ""), {}).get("status") == "ACTIVE"]

            for a in act_ads:
                aid = a["id"]
                aname = a.get("name", "")
                sq = c_squads.get(a.get("ad_squad_id", ""), {})
                sname = sq.get("name", "")
                if sm.is_cross_monitored_product_campaign(cname, sname):
                    continue
                is_gulf = sm.is_gulf_034_campaign(cname, sname, sq)
                max_cpa = MAX_CPA_GULF if is_gulf else MAX_CPA_SA
                no_sale_lim = NO_SALE_AD_STOP_GULF if is_gulf else NO_SALE_AD_STOP_SA

                a1 = ad_1d.get(aid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
                a2 = ad_2d.get(aid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
                a3 = ad_3d.get(aid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
                a7 = ad_7d.get(aid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})

                if max(a1["spend"], a2["spend"], a3["spend"], a7["spend"]) > 0:
                    snap_videos_summary.append({
                        "camp": cname,
                        "squad": sname,
                        "ad": aname,
                        "1d": f"${a1['spend']:.2f}/{a1['purchases']}p",
                        "2d": f"${a2['spend']:.2f}/{a2['purchases']}p",
                        "3d": f"${a3['spend']:.2f}/{a3['purchases']}p",
                        "7d": f"${a7['spend']:.2f}/{a7['purchases']}p",
                    })
                else:
                    continue

                # Check if this active ad violates $7 CPA or 0-sale limit in 1D, 2D, 3D, or 7D
                has_good_1d = a1["purchases"] >= 1 and a1["cpa"] is not None and a1["cpa"] <= max_cpa
                has_good_3d = (
                    a3["purchases"] >= 1
                    and a3["cpa"] is not None
                    and a3["cpa"] <= max_cpa
                    and not (a1["spend"] >= no_sale_lim and a1["purchases"] == 0)
                    and not (a1["purchases"] > 0 and a1["cpa"] is not None and a1["cpa"] > max_cpa)
                )
                kill_r = None
                if a1["spend"] >= no_sale_lim and a1["purchases"] == 0:
                    kill_r = f"1D Spend ${a1['spend']:.2f} with 0 sales"
                elif a1["purchases"] > 0 and a1["cpa"] is not None and a1["cpa"] > max_cpa:
                    kill_r = f"1D CPA ${a1['cpa']:.2f} > ${max_cpa:.2f} (${a1['spend']:.2f}/{a1['purchases']}p)"
                elif a2["spend"] >= no_sale_lim and a2["purchases"] == 0 and not has_good_3d:
                    kill_r = f"2D Spend ${a2['spend']:.2f} with 0 sales"
                elif a2["purchases"] > 0 and a2["cpa"] is not None and a2["cpa"] > max_cpa and not has_good_1d:
                    kill_r = f"2D CPA ${a2['cpa']:.2f} > ${max_cpa:.2f} (${a2['spend']:.2f}/{a2['purchases']}p)"
                elif a3["spend"] >= no_sale_lim and a3["purchases"] == 0:
                    kill_r = f"3D Spend ${a3['spend']:.2f} with 0 sales"
                elif a3["purchases"] > 0 and a3["cpa"] is not None and a3["cpa"] > max_cpa and not has_good_1d:
                    kill_r = f"3D CPA ${a3['cpa']:.2f} > ${max_cpa:.2f} (${a3['spend']:.2f}/{a3['purchases']}p)"
                elif a7["spend"] >= no_sale_lim and a7["purchases"] == 0 and not has_good_3d:
                    kill_r = f"7D Spend ${a7['spend']:.2f} with 0 sales"
                elif a7["purchases"] > 0 and a7["cpa"] is not None and a7["cpa"] > max_cpa and not has_good_3d and not has_good_1d:
                    kill_r = f"7D CPA ${a7['cpa']:.2f} > ${max_cpa:.2f} (${a7['spend']:.2f}/{a7['purchases']}p)"

                if kill_r:
                    a["status"] = "PAUSED"
                    sm.api_put(f"/adsquads/{sq['id']}/ads", token, {"ads": [a]})
                    history["ads"][aid] = {
                        "name": aname,
                        "campaign": cname,
                        "adsquad": sname,
                        "reason": f"[MultiWindow Manual+Bot Dissector] {kill_r}",
                        "paused_at": now_str,
                    }
                    sm.save_paused_history(history)
                    extra_paused.append(f"🛑 سناب شات: إيقاف الإعلان `{aname}` في `{cname}` (`{sname}`) — {kill_r}")
                    print(f"  🛑 [Extra Dissector Pause] '{aname}' in '{cname}' | {kill_r}", flush=True)

            if max(s1["spend"], s2["spend"], s3["spend"], s7["spend"]) > 0 or act_ads:
                snap_summary.append({
                    "name": f"{cname} (Active Ads={len(act_ads)})",
                    "1d": f"${s1['spend']:.2f} / {s1['purchases']}p (CPA={('$'+str(s1['cpa'])) if s1['cpa'] else '—'})",
                    "2d": f"${s2['spend']:.2f} / {s2['purchases']}p (CPA={('$'+str(s2['cpa'])) if s2['cpa'] else '—'})",
                    "3d": f"${s3['spend']:.2f} / {s3['purchases']}p (CPA={('$'+str(s3['cpa'])) if s3['cpa'] else '—'})",
                    "7d": f"${s7['spend']:.2f} / {s7['purchases']}p (CPA={('$'+str(s7['cpa'])) if s7['cpa'] else '—'})",
                })

    return {"campaigns": snap_summary, "videos": snap_videos_summary, "extra_paused": extra_paused}


def audit_tiktok_multiwindow() -> dict:
    import tiktok_monitor as tm

    print("\n======================================================================")
    print("🎵 [2/3] فحص وتفصيص حسابات تيك توك الـ 6 عبر 4 نوافذ (1D / 2D / 3D / 7D) بحد CPA <= $7.00")
    print("======================================================================", flush=True)

    tm.main()
    return {"status": "completed"}


# ==============================================================================
# PART 3: GOOGLE ADS 1000% CAREFUL MULTI-WINDOW (1D / 2D / 3D / 7D) AUDITOR
# ==============================================================================
MID = 100


def send_cdp(ws, method, params=None):
    global MID
    MID += 1
    my_id = MID
    ws.send(json.dumps({"id": my_id, "method": method, "params": params or {}}))
    while True:
        raw = ws.recv()
        msg = json.loads(raw)
        if msg.get("id") == my_id:
            return msg.get("result", {}), msg.get("error")


def eval_js(ws, expr, await_promise=False):
    res, _ = send_cdp(ws, "Runtime.evaluate", {"expression": expr, "awaitPromise": await_promise, "returnByValue": True})
    return (res or {}).get("result", {}).get("value")


def cdp_click(ws, x, y):
    send_cdp(ws, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    send_cdp(ws, "Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    send_cdp(ws, "Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})


def get_total_row_sig(ws):
    js = """
    (() => {
        const rows = Array.from(document.querySelectorAll('.particle-table-row, ess-table-row'));
        const tot = rows.find(r => (r.innerText || '').includes('Total:'));
        if (tot) return (tot.innerText || '').replace(/\\s+/g, ' ').trim();
        const firstAd = rows.find(r => r.querySelector('.ad-name'));
        return firstAd ? (firstAd.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 180) : '';
    })()
    """
    return eval_js(ws, js) or ""


def wait_for_table_change(ws, old_sig, timeout=9.0):
    time.sleep(1.8)
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.65)
        cur_sig = get_total_row_sig(ws)
        if cur_sig and cur_sig != old_sig:
            time.sleep(1.2)
            return cur_sig
    time.sleep(1.5)
    return get_total_row_sig(ws)


def select_date_preset(ws, option_text, port=None):
    old_sig = get_total_row_sig(ws)
    send_cdp(ws, "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
    send_cdp(ws, "Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
    eval_js(ws, "document.body.click(); window.scrollTo(0, 0); document.querySelectorAll('.particle-table-canvas, ess-table-canvas, .ess-table-canvas, [role=\"grid\"]').forEach(s => s.scrollTop = 0);")
    time.sleep(0.35)
    js_open = """
    (() => {
        const icons = Array.from(document.querySelectorAll('material-date-range-picker material-icon, .date-range-picker material-icon'));
        const arrow = icons.find(i => (i.innerText || '').trim() === 'arrow_drop_down');
        const btn = arrow ? (arrow.closest('[role="button"], material-button, .dropdown-button') || arrow) : document.querySelector('material-date-range-picker .dropdown-button');
        if (!btn) return null;
        const r = btn.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return null;
        return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
    })()
    """
    box = None
    for _ in range(12):
        box = eval_js(ws, js_open)
        if box:
            break
        time.sleep(0.8)
    if not box:
        return "NO_DATE_PICKER"
    cdp_click(ws, box["x"], box["y"])
    time.sleep(1.0)
    keep_adspower_offscreen(port)
    js_pick = f"""
    (async () => {{
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        const lists = Array.from(document.querySelectorAll('.preset-list, .presets, material-list, [role="listbox"]'));
        for (const scrollPos of [0, 180, 360, 0]) {{
            for (const lst of lists) lst.scrollTop = scrollPos;
            await sleep(80);
            const items = Array.from(document.querySelectorAll('material-select-item, [role="option"], [role="menuitem"], .item-label, span'));
            for (const el of items) {{
                const t = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                if ((t === {json.dumps(option_text)} || t.startsWith({json.dumps(option_text)} + ' ')) && el.offsetParent !== null) {{
                    const itemEl = el.closest('material-select-item, [role="option"], [role="menuitem"]') || el;
                    itemEl.scrollIntoView({{block: 'center'}});
                    await sleep(50);
                    const r = itemEl.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) {{
                        return {{found: true, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}};
                    }}
                }}
            }}
        }}
        return {{found: false}};
    }})()
    """
    pk = eval_js(ws, js_pick, await_promise=True) or {}
    if pk.get("found"):
        time.sleep(0.15)
        cdp_click(ws, pk["x"], pk["y"])
        wait_for_table_change(ws, old_sig, timeout=7.5)
        keep_adspower_offscreen(port)
        return f"SELECTED:{option_text}"
    send_cdp(ws, "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
    send_cdp(ws, "Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
    return "NOT_FOUND"


def click_prev_day_js(ws, prev_date_text=""):
    for attempt in range(3):
        old_sig = get_total_row_sig(ws)
        send_cdp(ws, "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
        send_cdp(ws, "Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
        eval_js(ws, "document.body.click(); window.scrollTo(0, 0);")
        time.sleep(0.25)
        js = """
        (() => {
            const icons = Array.from(document.querySelectorAll('material-date-range-picker material-icon, .date-range-picker material-icon, material-date-range-picker i'));
            const prev = icons.find(el => (el.innerText || '').trim() === 'chevron_left');
            if (prev) {
                const btn = prev.closest('material-button, button, [role="button"]') || prev;
                btn.click();
                const r = btn.getBoundingClientRect();
                if (r.width > 0 && r.height > 0) {
                    return {found: true, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
                }
                return {found: true, clicked_js: true};
            }
            return {found: false};
        })()
        """
        r = eval_js(ws, js) or {}
        if not r.get("found"):
            return False
        if attempt > 0 and r.get("x") and r.get("y"):
            cdp_click(ws, r["x"], r["y"])
        wait_for_table_change(ws, old_sig, timeout=7.5)
        cur_dt = eval_js(
            ws,
            "(() => { const d = document.querySelector('material-date-range-picker, .date-range-picker'); return d ? (d.innerText || '').replace(/\\s+/g, ' ').trim() : ''; })()",
        ) or ""
        if not prev_date_text or (cur_dt and cur_dt != prev_date_text):
            return True
    return False


def scrape_google_ads_table(ws) -> dict:
    """Scrapes all rows from Google Ads /aw/ads or /aw/campaigns table across pages with exact parsed metrics (Python-driven to prevent background tab timer throttling)."""
    eval_js(
        ws,
        """(() => {
            const firstBtn = document.querySelector('[aria-label="Go to the first page"]');
            if (firstBtn && !firstBtn.disabled && firstBtn.getAttribute('aria-disabled') !== 'true') firstBtn.click();
        })()""",
    )
    time.sleep(0.9)
    seen = set()
    rows_out = []
    for _page in range(4):
        eval_js(ws, "const s = document.querySelector('.particle-table-canvas, ess-table-canvas, [role=\"grid\"]'); if (s) s.scrollTop = 0; else window.scrollTo(0, 0);")
        time.sleep(0.15)
        for _step in range(18):
            batch = eval_js(
                ws,
                """(() => {
                    const out = [];
                    const rows = Array.from(document.querySelectorAll('.particle-table-row, ess-table-row'));
                    for (const r of rows) {
                        const stCell = r.querySelector('[essfield="status"]');
                        const stImg = stCell ? stCell.querySelector('[role="img"]') : null;
                        let toggle = stImg ? (stImg.getAttribute('aria-label') || '') : '';
                        const camp = ((r.querySelector('[essfield="campaign_name"]') || {}).innerText || '').trim();
                        const ag = ((r.querySelector('[essfield="ad_group_name"], [essfield="ad_group"]') || {}).innerText || '').trim();
                        const ad = ((r.querySelector('.ad-name') || {}).innerText || '').trim();
                        let txt = (r.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (txt.includes('View asset details')) {
                            txt = txt.split('View asset details')[1].trim();
                        }
                        if (toggle === 'Enabled' && /Campaign is paused|Ad group is paused|Campaign ended/i.test(txt)) {
                            toggle = 'Paused';
                        }
                        if (txt.length > 5) {
                            const isZero = /\\b0\\s+0\\s+(?:—|0\\.00%)\\s+(?:US)?\\$0\\.00/.test(txt);
                            if (toggle === 'Enabled' || !isZero || txt.includes('Total:')) {
                                out.push({toggle, camp, ag, ad, text: txt.slice(0, 320)});
                            }
                        }
                    }
                    const scroller = document.querySelector('.particle-table-canvas, ess-table-canvas, [role="grid"]');
                    if (scroller) scroller.scrollTop += 650;
                    else window.scrollBy(0, 650);
                    return out;
                })()""",
            ) or []
            for item in batch:
                key = f"{item.get('toggle')}|{item.get('camp')}|{item.get('ag')}|{item.get('ad')}|{item.get('text', '')[:140]}"
                if key not in seen:
                    seen.add(key)
                    rows_out.append(item)
            time.sleep(0.10)
        eval_js(ws, "const s = document.querySelector('.particle-table-canvas, ess-table-canvas, [role=\"grid\"]'); if (s) s.scrollTop = 0; else window.scrollTo(0, 0);")
        has_next = eval_js(
            ws,
            """(() => {
                const nextBtn = document.querySelector('[aria-label="Go to the next page"]');
                if (nextBtn && !nextBtn.disabled && nextBtn.getAttribute('aria-disabled') !== 'true') {
                    nextBtn.click();
                    return true;
                }
                return false;
            })()""",
        )
        if not has_next:
            break
        time.sleep(1.3)
    eval_js(
        ws,
        """(() => {
            const firstBtn = document.querySelector('[aria-label="Go to the first page"]');
            if (firstBtn && !firstBtn.disabled && firstBtn.getAttribute('aria-disabled') !== 'true') firstBtn.click();
        })()""",
    )
    time.sleep(0.8)
    date_txt = eval_js(
        ws,
        "(() => { const d = document.querySelector('material-date-range-picker, .date-range-picker'); return d ? (d.innerText || '').replace(/\\s+/g, ' ').trim() : ''; })()",
    ) or ""
    return {"dateText": date_txt, "rows": rows_out}


def parse_google_ad_metrics(row_obj: dict) -> dict:
    """
    Carefully parses a Google Ads /aw/ads row text:
    Example: 'Enabled c9 car lesds 14-10 Ad group 1 Eligible Demand Gen Video Ad 238 4,009 5.94% $0.08 $19.47 0.00% 3.00 $6.49'
    or with 0 clicks: 'Enabled pbw5 power bank WINNERS 20-10 Ad group 1 Eligible (Limited) Demand Gen Video Ad 0 118 0.00% — $1.51 0.00% 0.00 $0.00'
    """
    txt = row_obj.get("text", "")
    after_status = re.split(r"(?:Demand Gen )?video ad|Eligible(?:\s*\([^)]+\))?|Paused|Pending|Under review|Approved|Disapproved", txt, flags=re.IGNORECASE)
    tail = after_status[-1] if len(after_status) > 1 else txt

    # Match pattern: <clicks> <impr> <ctr>%_or_dash <cpc_or_dash> $<cost> <after_cost>
    m = re.search(
        r"(\d[\d,]*)\s+(\d[\d,]*)\s+(?:[\d.]+%|—)\s+(?:(?:US)?\$([\d,]+\.\d{2})|—)\s+(?:US)?\$([\d,]+\.\d{2})(.*)",
        tail,
    )
    if m:
        clicks = int(m.group(1).replace(",", ""))
        impr = int(m.group(2).replace(",", ""))
        cpc = float(m.group(3).replace(",", "")) if m.group(3) else 0.0
        cost = float(m.group(4).replace(",", ""))
        after_cost = m.group(5) or ""
        conv_m = re.search(r"(?<![$\d.])([\d,]+\.\d{2})(?!%|\d)", after_cost)
        conv = float(conv_m.group(1).replace(",", "")) if conv_m else 0.0
        return {"clicks": clicks, "impr": impr, "cpc": cpc, "spend": cost, "ui_conv": conv}

    # Fallback parser
    dollars = [float(x.replace(",", "")) for x in re.findall(r"(?:US)?\$([\d,]+\.\d{2})", tail)]
    has_dash_cpc = bool(re.search(r"(?:%|—)\s+—\s+(?:US)?\$", tail))
    if dollars:
        spend = dollars[0] if has_dash_cpc else (dollars[1] if len(dollars) > 1 else dollars[0])
        cpc = 0.0 if has_dash_cpc else (dollars[0] if len(dollars) > 1 else 0.0)
    else:
        spend, cpc = 0.0, 0.0
    est_clicks = max(1, int(round(spend / cpc))) if (cpc > 0 and spend > 0) else 0
    conv_m = re.search(r"%\s+([\d,]+\.\d{2})\s+(?:US)?\$[\d,]+\.\d{2}\s*$", tail)
    ui_conv = float(conv_m.group(1).replace(",", "")) if conv_m else 0.0
    return {"clicks": est_clicks, "impr": 0, "cpc": cpc, "spend": spend, "ui_conv": ui_conv}


def set_google_ad_status_carefully(ws, camp_name: str, ad_name: str, desired_status: str, port=None, ag_name: str = None) -> str:
    expected_label = "Enabled" if desired_status == "Enable" else "Paused"
    eval_js(ws, "document.body.click()")
    time.sleep(0.2)
    has_first = eval_js(
        ws,
        """(() => { const b = document.querySelector('[aria-label="Go to the first page"]'); if (b && !b.disabled && b.getAttribute('aria-disabled') !== 'true') { b.click(); return true; } return false; })()""",
    )
    if has_first:
        time.sleep(1.3)

    for _page_idx in range(4):
        for scroll_pos in range(0, 9500, 550):
            eval_js(
                ws,
                f"window.scrollTo(0, {scroll_pos}); document.querySelectorAll('.particle-table-canvas, ess-table-canvas, .ess-table-canvas, [role=\"grid\"]').forEach(s => s.scrollTop = {scroll_pos});",
            )
            time.sleep(0.18)
            js_find = f"""
            (() => {{
                const rows = Array.from(document.querySelectorAll('.particle-table-row, ess-table-row'));
                const wantAg = {json.dumps(ag_name or "")};
                for (const r of rows) {{
                    const c = ((r.querySelector('[essfield="campaign_name"]') || {{}}).innerText || '').trim();
                    const ag = ((r.querySelector('[essfield="ad_group_name"], [essfield="ad_group"]') || {{}}).innerText || '').trim();
                    const n = ((r.querySelector('.ad-name') || {{}}).innerText || '').trim();
                    if (c === {json.dumps(camp_name)} && n === {json.dumps(ad_name)} && (!wantAg || ag === wantAg)) {{
                        r.scrollIntoView({{block: 'center'}});
                        const stCell = r.querySelector('[essfield="status"]');
                        if (!stCell) continue;
                        const img = stCell.querySelector('[role="img"]');
                        const cur = img ? (img.getAttribute('aria-label') || '') : '';
                        if (cur === {json.dumps(expected_label)}) return {{res: 'ALREADY_' + cur}};
                        stCell.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
                        stCell.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}}));
                        r.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
                        const btn = stCell.querySelector('.ess-edit-icon, [role="button"]');
                        if (btn) {{
                            btn.click();
                            return {{res: 'OPENED', cur}};
                        }}
                        return {{res: 'NO_BTN', cur}};
                    }}
                }}
                return {{res: 'NOT_FOUND'}};
            }})()
            """
            info = eval_js(ws, js_find) or {}
            if info.get("res") == "NOT_FOUND":
                continue
            if info.get("res", "").startswith("ALREADY_"):
                return info["res"]
            if info.get("res") == "OPENED":
                time.sleep(0.75)
                keep_adspower_offscreen(port)
                js_menu = f"""
                (() => {{
                    const items = Array.from(document.querySelectorAll('material-select-item, [role="menuitem"], [role="option"]')).filter(el => el.offsetParent !== null);
                    for (const item of items) {{
                        const t = (item.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (t === {json.dumps(desired_status)} || t.endsWith(' ' + {json.dumps(desired_status)})) {{
                            const r = item.getBoundingClientRect();
                            return {{found: true, t, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}};
                        }}
                    }}
                    return {{found: false}};
                }})()
                """
                minfo = eval_js(ws, js_menu) or {}
                if minfo.get("found"):
                    cdp_click(ws, minfo["x"], minfo["y"])
                    time.sleep(1.5)
                    keep_adspower_offscreen(port)
                    return f"{desired_status.upper()}_SUCCESS (was {info.get('cur')})"
        has_next = eval_js(
            ws,
            """(() => { const b = document.querySelector('[aria-label="Go to the next page"]'); if (b && !b.disabled && b.getAttribute('aria-disabled') !== 'true') { b.click(); return true; } return false; })()""",
        )
        if not has_next:
            break
        time.sleep(1.5)
    return "NOT_FOUND"


def audit_google_ads_multiwindow() -> dict:
    from run_all_monitors_5h import find_open_adspower_ports

    print("\n======================================================================")
    print("🌐 [3/3] فحص وتفصيص حسابات Google Ads عبر 4 نوافذ (1D / 2D / 3D / 7D) بحرص 1000% (بيكسل + شيت Lightfunnels)")
    print("======================================================================", flush=True)

    ports = dict(find_open_adspower_ports())
    if not ports:
        print("⚠️ لا توجد منافذ AdsPower مفتوحة حالياً!", flush=True)
        return {"accounts": [], "actions": []}

    sheet = fetch_sheet_multiwindow_sales()
    print(f"📊 مبيعات شيت Lightfunnels الموثقة (1D اليوم): {json.dumps(sheet['1d'], ensure_ascii=False)}")
    print(f"📊 مبيعات شيت Lightfunnels الموثقة (2D يومين): {json.dumps(sheet['2d'], ensure_ascii=False)}")
    print(f"📊 مبيعات شيت Lightfunnels الموثقة (3D 3 أيام): {json.dumps(sheet['3d'], ensure_ascii=False)}")
    print(f"📊 مبيعات شيت Lightfunnels الموثقة (7D 7 أيام): {json.dumps(sheet['7d'], ensure_ascii=False)}", flush=True)

    accounts_cfg = []
    for prof, port in ports.items():
        if "k1cekgik" in prof:
            accounts_cfg.append(("Account 1: ads (843-693-9582)", prof, port, "8436939582"))
        elif "k1dh2jyf" in prof:
            accounts_cfg.append(("Account 2: 744-305-2213 (802-462-7197)", prof, port, "8024627197"))
        else:
            accounts_cfg.append((f"Profile {prof}", prof, port, ""))

    all_acc_reports = []
    all_actions = []

    for label, prof, port, ocid in sorted(accounts_cfg, key=lambda x: x[0]):
        keep_adspower_offscreen(port)
        print(f"\n----------------------------------------------------------------------")
        print(f"🔎 الدخول إلى {label} (Port={port}, ocid={ocid})")
        print(f"----------------------------------------------------------------------", flush=True)
        try:
            tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=4).read().decode("utf-8"))
            g_tabs = [t for t in tabs if t.get("type") == "page" and "ads.google.com" in t.get("url", "") and "/aw/ads" in t.get("url", "")]
            if not g_tabs:
                g_tabs = [t for t in tabs if t.get("type") == "page" and "ads.google.com" in t.get("url", "") and "/campaigns/new" not in t.get("url", "") and "/demandgen" not in t.get("url", "") and "/construction" not in t.get("url", "")]
            if not g_tabs:
                print(f"⚠️ لا توجد تبويبة Google Ads متاحة للمراقبة في {label}", flush=True)
                continue
            try:
                ws_test = websocket.create_connection(
                    g_tabs[0]["webSocketDebuggerUrl"].replace("ws://localhost:", "ws://127.0.0.1:"),
                    suppress_origin=True,
                    timeout=4,
                )
                ws_test.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "1+1", "returnByValue": True}}))
                ws_test.recv()
                ws_test.close()
            except Exception:
                ver_info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3).read().decode("utf-8"))
                bws_fix = websocket.create_connection(ver_info["webSocketDebuggerUrl"].replace("ws://localhost:", "ws://127.0.0.1:"), suppress_origin=True, timeout=5)
                bws_fix.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": g_tabs[0]["url"]}}))
                bws_fix.recv()
                bws_fix.send(json.dumps({"id": 2, "method": "Target.closeTarget", "params": {"targetId": g_tabs[0]["id"]}}))
                bws_fix.recv()
                bws_fix.close()
                keep_adspower_offscreen(port)
                time.sleep(5.5)
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=4).read().decode("utf-8"))
                g_tabs = [t for t in tabs if t.get("type") == "page" and "ads.google.com" in t.get("url", "") and "/aw/ads" in t.get("url", "")]
            ws = websocket.create_connection(
                g_tabs[0]["webSocketDebuggerUrl"].replace("ws://localhost:", "ws://127.0.0.1:"),
                suppress_origin=True,
                timeout=60,
            )
            send_cdp(ws, "Target.activateTarget", {"targetId": g_tabs[0]["id"]})
            send_cdp(ws, "Emulation.setFocusEmulationEnabled", {"enabled": True})
            keep_adspower_offscreen(port)

            # Navigate to /aw/ads
            cur_url = eval_js(ws, "location.href") or ""
            if "/aw/ads" not in cur_url:
                nav_url = f"https://ads.google.com/aw/ads?ocid={ocid}" if ocid else "https://ads.google.com/aw/ads"
                send_cdp(ws, "Page.navigate", {"url": nav_url})
                time.sleep(6.5)
                keep_adspower_offscreen(port)

            # 1) Select Today (d0) — with strict verification that dateText starts with 'Today'
            select_date_preset(ws, "Today", port=port)
            tbl_d0 = scrape_google_ads_table(ws)
            date_d0 = tbl_d0.get("dateText", "")
            if not date_d0.startswith("Today"):
                # If stuck on Yesterday or 2 days ago, click chevron_right until Today or retry select_date_preset
                for _ in range(3):
                    eval_js(ws, """(() => {
                        const icons = Array.from(document.querySelectorAll('material-date-range-picker material-icon, .date-range-picker material-icon'));
                        const nxt = icons.find(el => (el.innerText || '').trim() === 'chevron_right');
                        if (nxt) { const b = nxt.closest('material-button, button, [role="button"]') || nxt; if (!b.disabled && b.getAttribute('aria-disabled') !== 'true') b.click(); }
                    })()""")
                    time.sleep(2.2)
                    tbl_d0 = scrape_google_ads_table(ws)
                    date_d0 = tbl_d0.get("dateText", "")
                    if date_d0.startswith("Today"):
                        break
            today_verified = date_d0.startswith("Today")

            # 2) Click prev day -> Yesterday (d1)
            click_prev_day_js(ws, prev_date_text=date_d0)
            tbl_d1 = scrape_google_ads_table(ws)
            date_d1 = tbl_d1.get("dateText", "Yesterday")

            # 3) Click prev day -> 2 Days Ago (d2)
            click_prev_day_js(ws, prev_date_text=date_d1)
            tbl_d2 = scrape_google_ads_table(ws)
            date_d2 = tbl_d2.get("dateText", "2 Days Ago")

            # 4) Select Last 7 days (l7)
            select_date_preset(ws, "Last 7 days", port=port)
            tbl_l7 = scrape_google_ads_table(ws)
            date_l7 = tbl_l7.get("dateText", "Last 7 days")

            # Return cleanly to Today so UI stays on Today
            select_date_preset(ws, "Today", port=port)

            print(f"  📅 النوافذ المقروءة بنجاح: اليوم=({date_d0}) | أمس=({date_d1}) | قبل أمس=({date_d2}) | 7 أيام=({date_l7})", flush=True)

            # Parse the exact calendar date shown in Google Ads UI 'Today' (both 'Today Oct 23, 2026' and 'Today 23 Oct 2026')
            # so that between 00:00 and 02:00 Saudi time (before Google Ads account timezone rolls over),
            # Sheet 1D sales match the exact date displayed in Google Ads UI!
            m_ui_us = re.search(r"([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})", date_d0)
            m_ui_uk = re.search(r"(\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", date_d0)
            try:
                if m_ui_us:
                    ui_dt_parsed = datetime.strptime(m_ui_us.group(1), "%b %d, %Y")
                    sheet = fetch_sheet_multiwindow_sales(ui_dt_parsed.strftime("%Y-%m-%d"))
                elif m_ui_uk:
                    ui_dt_parsed = datetime.strptime(m_ui_uk.group(1), "%d %b %Y")
                    sheet = fetch_sheet_multiwindow_sales(ui_dt_parsed.strftime("%Y-%m-%d"))
            except Exception:
                pass

            # Aggregate per (camp, ag, ad) across d0, d1, d2, l7
            ads_map = {}
            seen_slot_dates = set()

            def ingest_table(tbl, slot):
                dt_str = (tbl.get("dateText") or "").strip()
                if slot != "d0" and dt_str and dt_str in seen_slot_dates:
                    return
                if dt_str:
                    seen_slot_dates.add(dt_str)
                for r in tbl.get("rows", []):
                    camp = (r.get("camp") or "").strip()
                    ag = (r.get("ag") or "").strip()
                    ad = (r.get("ad") or "").strip()
                    if not camp or not ad or "Total:" in r.get("text", ""):
                        continue
                    key = (camp, ag, ad)
                    entry = ads_map.setdefault(key, {
                        "camp": camp,
                        "ag": ag,
                        "ad": ad,
                        "toggle": r.get("toggle", ""),
                        "d0": {"spend": 0.0, "ui_conv": 0.0, "clicks": 0, "cpc": 0.0},
                        "d1": {"spend": 0.0, "ui_conv": 0.0, "clicks": 0, "cpc": 0.0},
                        "d2": {"spend": 0.0, "ui_conv": 0.0, "clicks": 0, "cpc": 0.0},
                        "l7": {"spend": 0.0, "ui_conv": 0.0, "clicks": 0, "cpc": 0.0},
                    })
                    if slot == "d0" and r.get("toggle"):
                        entry["toggle"] = r.get("toggle")
                    m = parse_google_ad_metrics(r)
                    if m["spend"] >= entry[slot]["spend"]:
                        entry[slot] = m

            ingest_table(tbl_d0, "d0")
            ingest_table(tbl_d1, "d1")
            ingest_table(tbl_d2, "d2")
            ingest_table(tbl_l7, "l7")

            acc_ads_report = []
            for (camp, ag, ad), info in sorted(ads_map.items(), key=lambda kv: (kv[1]["toggle"] != "Enabled", -kv[1]["d0"]["spend"], -kv[1]["l7"]["spend"])):
                toggle = info["toggle"]
                d0, d1, d2, l7 = info["d0"], info["d1"], info["d2"], info["l7"]

                sp_1d = round(d0["spend"], 2)
                sp_2d = round(d0["spend"] + d1["spend"], 2)
                sp_3d = round(d0["spend"] + d1["spend"] + d2["spend"], 2)
                sp_7d = round(max(sp_3d, d0["spend"] + l7["spend"]), 2)

                ui_1d = d0["ui_conv"]
                ui_2d = round(d0["ui_conv"] + d1["ui_conv"], 2)
                ui_3d = round(d0["ui_conv"] + d1["ui_conv"] + d2["ui_conv"], 2)
                ui_7d = round(max(ui_3d, d0["ui_conv"] + l7["ui_conv"]), 2)

                ad_key = ad.strip().lower()
                c_pcat, c_ccat = classify_product_and_country(camp, camp)
                strict_k = f"{ad_key}|{c_pcat}|{c_ccat}"
                if c_pcat != "other":
                    sh_1d = sheet.get("1d_strict", {}).get(strict_k, 0)
                    sh_2d = sheet.get("2d_strict", {}).get(strict_k, 0)
                    sh_3d = sheet.get("3d_strict", {}).get(strict_k, 0)
                    sh_7d = sheet.get("7d_strict", {}).get(strict_k, 0)
                else:
                    sh_1d = sheet["1d"].get(ad_key, 0)
                    sh_2d = sheet["2d"].get(ad_key, 0)
                    sh_3d = sheet["3d"].get(ad_key, 0)
                    sh_7d = sheet["7d"].get(ad_key, 0)

                # If this is a duplicate '#2' campaign sharing the same ad_name as the original campaign,
                # do not let the 0-UI-conversion duplicate claim the original campaign's Sheet sales!
                base_camp = camp.split("#")[0].strip()
                if "#" in camp and any(k[0] == base_camp and k[2] == ad for k in ads_map) and ui_7d == 0:
                    sh_1d = sh_2d = sh_3d = sh_7d = 0

                eff_1d = max(ui_1d, float(sh_1d))
                eff_2d = max(ui_2d, float(sh_2d))
                eff_3d = max(ui_3d, float(sh_3d))
                eff_7d = max(ui_7d, float(sh_7d))

                cpa_1d = round(sp_1d / eff_1d, 2) if eff_1d > 0 else None
                cpa_2d = round(sp_2d / eff_2d, 2) if eff_2d > 0 else None
                cpa_3d = round(sp_3d / eff_3d, 2) if eff_3d > 0 else None
                cpa_7d = round(sp_7d / eff_7d, 2) if eff_7d > 0 else None

                if toggle != "Enabled" and max(sp_1d, sp_2d, sp_3d, sp_7d) < 0.50:
                    continue

                is_gulf_g = any(k in camp.lower() for k in ("عمان", "قطر", "كويت", "بحرين", "امارات", "oman", "qatar", "kuwait", "bahrain", "uae"))
                max_cpa_g = 10.00 if is_gulf_g else MAX_CPA_SA  # $7.00 for Saudi, $10.00 for Gulf in Google Ads
                no_sale_g = 7.00 if is_gulf_g else 5.00

                # 1000% CAREFUL EVALUATION FOR ENABLED ADS ONLY:
                decision = "⏸️ متوقف مسبقاً"
                pause_reason = None
                if toggle == "Enabled":
                    is_profitable_1d = eff_1d >= 1.0 and cpa_1d is not None and cpa_1d <= max_cpa_g
                    is_profitable_3d = (
                        eff_3d >= 1.0
                        and cpa_3d is not None
                        and cpa_3d <= max_cpa_g
                        and not (sp_1d >= no_sale_g and eff_1d == 0)
                        and not (eff_1d >= 1.0 and cpa_1d is not None and cpa_1d > max_cpa_g)
                    )

                    if sp_1d >= no_sale_g and eff_1d == 0:
                        pause_reason = f"صرف اليوم ${sp_1d:.2f} (>= ${no_sale_g:.2f}) بـ 0 مبيعات (في البيكسل والشيت)"
                    elif eff_1d >= 1.0 and sp_1d >= max_cpa_g and cpa_1d is not None and cpa_1d > max_cpa_g:
                        pause_reason = f"CPA اليوم مرتفع ${cpa_1d:.2f} (> ${max_cpa_g:.2f}) [صرف ${sp_1d:.2f} / {eff_1d:.0f} مبيعات]"
                    elif sp_2d >= no_sale_g and eff_2d == 0:
                        pause_reason = f"صرف يومين (2D) ${sp_2d:.2f} (>= ${no_sale_g:.2f}) بـ 0 مبيعات"
                    elif eff_2d >= 1.0 and sp_2d >= max_cpa_g and cpa_2d is not None and cpa_2d > max_cpa_g and not is_profitable_1d:
                        pause_reason = f"CPA يومين (2D) مرتفع ${cpa_2d:.2f} (> ${max_cpa_g:.2f}) [صرف ${sp_2d:.2f} / {eff_2d:.0f} مبيعات]"
                    elif sp_3d >= no_sale_g and eff_3d == 0:
                        pause_reason = f"صرف 3 أيام (3D) ${sp_3d:.2f} (>= ${no_sale_g:.2f}) بـ 0 مبيعات"
                    elif eff_3d >= 1.0 and sp_3d >= max_cpa_g and cpa_3d is not None and cpa_3d > max_cpa_g and not is_profitable_1d:
                        pause_reason = f"CPA 3 أيام (3D) مرتفع ${cpa_3d:.2f} (> ${max_cpa_g:.2f}) [صرف ${sp_3d:.2f} / {eff_3d:.0f} مبيعات]"
                    elif sp_7d >= (no_sale_g + 1.50) and eff_7d == 0 and not is_profitable_3d:
                        pause_reason = f"صرف 7 أيام (7D) ${sp_7d:.2f} بـ 0 مبيعات"
                    elif eff_7d >= 1.0 and sp_7d >= (max_cpa_g + 2.0) and cpa_7d is not None and cpa_7d > max_cpa_g and not is_profitable_3d and not is_profitable_1d:
                        pause_reason = f"CPA 7 أيام (7D) مرتفع ${cpa_7d:.2f} (> ${max_cpa_g:.2f}) [صرف ${sp_7d:.2f} / {eff_7d:.0f} مبيعات]"
                    elif sp_1d >= 1.00 and eff_1d == 0 and (d0["clicks"] == 0 or d0["cpc"] > 0.35):
                        pause_reason = f"CPC مرتفع اليوم (${d0['cpc']:.2f} / نقرات={d0['clicks']}) بعد صرف ${sp_1d:.2f} بـ 0 مبيعات"
                    elif sp_2d >= 1.00 and eff_2d == 0 and ((d0["clicks"] + d1["clicks"]) == 0 or (sp_2d / max(1, d0["clicks"] + d1["clicks"])) > 0.35):
                        pause_reason = f"CPC مرتفع في يومين ($2D=${sp_2d:.2f} / نقرات={d0['clicks']+d1['clicks']}) بـ 0 مبيعات"
                    elif sp_3d >= 1.00 and eff_3d == 0 and ((d0["clicks"] + d1["clicks"] + d2["clicks"]) == 0 or (sp_3d / max(1, d0["clicks"] + d1["clicks"] + d2["clicks"])) > 0.35):
                        pause_reason = f"CPC مرتفع في 3 أيام ($3D=${sp_3d:.2f} / نقرات={d0['clicks']+d1['clicks']+d2['clicks']}) بـ 0 مبيعات"
                    elif ad_key == "ad 1" and sp_3d >= 1.00 and eff_3d == 0:
                        pause_reason = f"إعلان باسم عام (Ad 1) بدون كود UTM وصرف ${sp_3d:.2f} بـ 0 مبيعات"

                    if pause_reason and not today_verified:
                        decision = f"⚠️ تم تخطي الإيقاف مؤقتاً لأن نافذة اليوم لم تتأكد ({date_d0[:25]})"
                        pause_reason = None
                    if pause_reason:
                        res_p = set_google_ad_status_carefully(ws, camp, ad, "Pause", port=port, ag_name=ag)
                        if res_p == "NOT_FOUND":
                            click_prev_day_js(ws)
                            res_p = set_google_ad_status_carefully(ws, camp, ad, "Pause", port=port, ag_name=ag)
                        if res_p == "NOT_FOUND":
                            select_date_preset(ws, "Last 7 days", port=port)
                            res_p = set_google_ad_status_carefully(ws, camp, ad, "Pause", port=port, ag_name=ag)
                        select_date_preset(ws, "Today", port=port)
                        decision = f"🛑 تم إيقافه فوراً ({res_p}) — {pause_reason}"
                        all_actions.append(f"🛑 Google Ads ({label}): إيقاف الإعلان `{ad}` في حملة `{camp}` ({ag}) — {pause_reason} ({res_p})")
                    else:
                        decision = "✅ رابح / آمن ومستمر"

                row_line = (
                    f"  • [{toggle:7s}] Camp='{camp}' | AG='{ag}' | Ad='{ad}' || "
                    f"1D: ${sp_1d:.2f} (UI={ui_1d:.0f}, Sheet={sh_1d} -> CPA={('$'+str(cpa_1d)) if cpa_1d else '—'}) | "
                    f"2D: ${sp_2d:.2f} (UI={ui_2d:.0f}, Sheet={sh_2d} -> CPA={('$'+str(cpa_2d)) if cpa_2d else '—'}) | "
                    f"3D: ${sp_3d:.2f} (UI={ui_3d:.0f}, Sheet={sh_3d} -> CPA={('$'+str(cpa_3d)) if cpa_3d else '—'}) | "
                    f"7D: ${sp_7d:.2f} (UI={ui_7d:.0f}, Sheet={sh_7d} -> CPA={('$'+str(cpa_7d)) if cpa_7d else '—'}) ==> {decision}"
                )
                print(row_line, flush=True)
                acc_ads_report.append({
                    "camp": camp,
                    "ag": ag,
                    "ad": ad,
                    "toggle": toggle,
                    "1d": f"${sp_1d:.2f} ({eff_1d:.0f}p: UI={ui_1d:.0f}/Sh={sh_1d}, CPA={('$'+str(cpa_1d)) if cpa_1d else '—'})",
                    "2d": f"${sp_2d:.2f} ({eff_2d:.0f}p: UI={ui_2d:.0f}/Sh={sh_2d}, CPA={('$'+str(cpa_2d)) if cpa_2d else '—'})",
                    "3d": f"${sp_3d:.2f} ({eff_3d:.0f}p: UI={ui_3d:.0f}/Sh={sh_3d}, CPA={('$'+str(cpa_3d)) if cpa_3d else '—'})",
                    "7d": f"${sp_7d:.2f} ({eff_7d:.0f}p: UI={ui_7d:.0f}/Sh={sh_7d}, CPA={('$'+str(cpa_7d)) if cpa_7d else '—'})",
                    "decision": decision,
                })

            all_acc_reports.append({"label": label, "ads": acc_ads_report})
            ws.close()
            keep_adspower_offscreen(port)
        except Exception as e:
            print(f"⚠️ خطأ أثناء فحص {label}: {e}", flush=True)

    return {"accounts": all_acc_reports, "actions": all_actions}


def main():
    now_sa = datetime.now(SA_TZ)
    skip_snap_tt = "--skip-snap-tiktok" in sys.argv or "--google-only" in sys.argv
    print(f"🚀 بدء الفاحص والمفصّص الشامل متعدد النوافذ (1D / 2D / 3D / 7D) بحد أقصى CPA = $7.00 ({now_sa:%Y-%m-%d %H:%M} GMT+3)", flush=True)
    sheet = fetch_sheet_multiwindow_sales()
    if skip_snap_tt:
        snap_res = {"campaigns": [], "extra_paused": []}
        tt_res = {}
    else:
        try:
            snap_res = audit_snapchat_multiwindow(sheet)
        except Exception as e:
            print(f"⚠️ خطأ جزئي في فحص سناب شات: {e}", flush=True)
            snap_res = {"campaigns": [], "extra_paused": []}
        try:
            tt_res = audit_tiktok_multiwindow()
        except Exception as e:
            print(f"⚠️ خطأ جزئي في فحص تيك توك: {e}", flush=True)
            tt_res = {}
    try:
        g_res = audit_google_ads_multiwindow()
    except Exception as e:
        print(f"⚠️ خطأ جزئي في فحص Google Ads: {e}", flush=True)
        g_res = {"accounts": [], "actions": []}

    lines = [
        f"# 🛡️ تقرير المفصّص الشامل وقاطع النزيف عبر الـ 4 نوافذ (`1D / 2D / 3D / 7D` — سقف `CPA <= $7.00`) — {now_sa:%Y-%m-%d %H:%M} GMT+3",
        "",
        "## ⚡ الأكشنات الفورية المنفذة في هذه الدورة",
    ]
    combined_actions = snap_res.get("extra_paused", []) + g_res.get("actions", [])
    if combined_actions:
        for a in combined_actions:
            lines.append(f"- {a}")
    else:
        lines.append("- ✅ تم تنظيف وإيقاف جميع الإعلانات الخاسرة؛ كل ما يعمل الآن آمن أو مربح تحت سقف `$7.00`.")

    lines.extend(["", "## 🌐 جدول تفصيص Google Ads عبر الـ 4 نوافذ (`1D / 2D / 3D / 7D` — بيكسل + شيت)"])
    for acc in g_res.get("accounts", []):
        lines.append(f"### {acc['label']}")
        lines.append("| الحالة | الحملة | الإعلان | اليوم (1D) | يومين (2D) | 3 أيام (3D) | 7 أيام (7D) | القرار |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in acc.get("ads", []):
            lines.append(f"| `{r['toggle']}` | `{r['camp']}` | **`{r['ad']}`** | {r['1d']} | {r['2d']} | {r['3d']} | {r['7d']} | {r['decision']} |")
        lines.append("")

    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✅ اكتمل الفحص الشامل وحُفظ التقرير في: {REPORT_FILE}", flush=True)


if __name__ == "__main__":
    main()
