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
_STATIC_DNS_FALLBACK = {
    "adsapi.snapchat.com": "34.110.187.193",
    "accounts.snapchat.com": "34.149.46.130",
    "docs.google.com": "142.251.27.100",
    "api.github.com": "140.82.121.5",
    "github.com": "140.82.121.4",
}


def _doh_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if not host or str(host).replace(".", "").isdigit() or ":" in str(host):
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    if host in _dns_cache:
        return _orig_getaddrinfo(_dns_cache[host], port, socket.AF_INET, type, proto, flags)
    try:
        res = _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
        if res:
            ip = res[0][4][0]
            if ip and not str(host).replace(".", "").isdigit():
                _dns_cache[host] = ip
            return res
    except Exception:
        pass
    if host not in _dns_cache:
        try:
            req = urllib.request.Request(
                f"https://1.1.1.1/dns-query?name={host}&type=A",
                headers={"accept": "application/dns-json"},
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                r = json.loads(resp.read().decode("utf-8"))
            ips = [a["data"] for a in r.get("Answer", []) if a.get("type") == 1]
            if ips:
                _dns_cache[host] = ips[0]
        except Exception:
            pass
    if host not in _dns_cache and host in _STATIC_DNS_FALLBACK:
        _dns_cache[host] = _STATIC_DNS_FALLBACK[host]
    if host in _dns_cache:
        return _orig_getaddrinfo(_dns_cache[host], port, socket.AF_INET, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


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
    # SunBrowser is launched with --window-position=-3200,-3200 at startup; never call SetWindowPos or Browser.setWindowBounds which deadlocks SunBrowser's UI thread.
    return


def is_sheet_gift_row(prod_name: str) -> bool:
    p = (prod_name or "").strip().lower()
    return any(g in p for g in ("باور بانك صغير للطوارئ", "سوار رجالي", "سماعات", "هدية", "مجانا", "مجاناً"))


def classify_product_and_country(text_prod: str, text_country: str = "") -> tuple:
    t = f"{text_prod} {text_country}".lower()
    if "باور بانك صغير للطوارئ" in t:
        p_cat = "car"
    elif any(k in t for k in ("aroura", "arcura", "15047", "أرورا", "ارورا", "مصفف", "ستيلر", "styler")):
        p_cat = "aroura"
    elif any(k in t for k in ("doorbell", "10165", "جرس", "انتركم")):
        p_cat = "doorbell"
    elif any(k in t for k in ("22454", "window", "ممسحة الزجاج", "مكنسة الزجاج")):
        p_cat = "winvac"
    elif any(k in t for k in ("washer", "hp909", "hp-909", "غسالة", "غساله")):
        p_cat = "washer"
    elif any(k in t for k in ("bakhoor", "bw20", "bw-20", "مبخرة", "مبخره", "بخور")):
        p_cat = "bakhoor"
    elif any(k in t for k in ("r8019", "r.8019", "8019", "raf ", "cooker", "موقد", "سيراميك")):
        p_cat = "cooker"
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
        # Run standard snap_monitor on each account unless --post-monitor is passed (when snap_monitor just ran)
        if "--post-monitor" not in sys.argv:
            sm.run_automation(token, acc_id, dry_run=False)

        # Fetch fresh post-action snapshot to verify 1000%
        camps = sm.api_get_paginated(f"/adaccounts/{acc_id}/campaigns?limit=200", token, "campaigns", "campaign")
        active_camps = [c for c in camps if c.get("status") == "ACTIVE"]

        c_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=0)}
        c_2d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=1)}
        c_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=2)}
        c_7d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=6)}
        sq_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=0)}
        sq_2d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=1)}
        sq_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=2)}
        sq_7d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=6)}

        ad_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "ad", days_back=0)}
        ad_2d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "ad", days_back=1)}
        ad_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "ad", days_back=2)}
        ad_7d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "ad", days_back=6)}

        for c in active_camps:
            cid = c["id"]
            cname = c.get("name", "")
            if (cid in getattr(sm, "ALLOWED_GULF_CAMPAIGN_IDS", set())) or sm.is_gulf_034_campaign(cname):
                continue
            s1 = c_1d.get(cid, {"spend": 0.0, "purchases": 0, "cpa": None})
            s2 = c_2d.get(cid, {"spend": 0.0, "purchases": 0, "cpa": None})
            s3 = c_3d.get(cid, {"spend": 0.0, "purchases": 0, "cpa": None})
            s7 = c_7d.get(cid, {"spend": 0.0, "purchases": 0, "cpa": None})

            # Fetch active squads and check active Copy squads + active ads inside squads that spent in 7D
            if max(s1["spend"], s2["spend"], s3["spend"], s7["spend"]) <= 0.0:
                continue
            c_squads = {sq["id"]: sq for sq in sm.api_get_paginated(f"/campaigns/{cid}/adsquads?limit=1000", token, "adsquads", "adsquad", max_pages=5)}

            # أمر المستخدم الصارم (2026-11-01): أي حملة إعلانية ماشية في طريق غلط وصرفت >= $20.00 بدون مبيعة (أو CPA >= $20.00) يقتلها البوت فوراً دون تساهل! (مع عدم لمس حملات التيست)
            if not sm.is_cross_monitored_product_campaign(cname) and not sm.is_dedicated_snap_test_campaign(cid, cname):
                sh1_c = (sheet_sales.get("1d") or {}).get(cid.lower(), 0)
                sh2_c = (sheet_sales.get("2d") or {}).get(cid.lower(), 0)
                sh3_c = (sheet_sales.get("3d") or {}).get(cid.lower(), 0)
                sh7_c = (sheet_sales.get("7d") or {}).get(cid.lower(), 0)
                act_sq_ids_now = [sq_x["id"] for sq_x in c_squads.values() if sq_x.get("status") == "ACTIVE"]
                sp1_act = round(sum(sq_1d.get(sid_x, {}).get("spend", 0.0) for sid_x in act_sq_ids_now), 2)
                sp2_act = round(sum(sq_2d.get(sid_x, {}).get("spend", 0.0) for sid_x in act_sq_ids_now), 2)
                sp3_act = round(sum(sq_3d.get(sid_x, {}).get("spend", 0.0) for sid_x in act_sq_ids_now), 2)
                sp1_chk = max(float(s1.get("spend", 0.0)), sp1_act)
                sp2_chk = max(float(s2.get("spend", 0.0)), sp2_act)
                sp3_chk = max(float(s3.get("spend", 0.0)), sp3_act)
                pu1_chk = max(int(s1.get("purchases", 0)), int(sh1_c))
                pu2_chk = max(int(s2.get("purchases", 0)), int(sh2_c))
                pu3_chk = max(int(s3.get("purchases", 0)), int(sh3_c))
                cpa1_chk = round(sp1_chk / pu1_chk, 2) if pu1_chk > 0 else None
                cpa2_chk = round(sp2_chk / pu2_chk, 2) if pu2_chk > 0 else None
                cpa3_chk = round(sp3_chk / pu3_chk, 2) if pu3_chk > 0 else None
                camp_kill_20 = None
                if sp1_chk >= 20.00 and pu1_chk == 0:
                    camp_kill_20 = f"1D Campaign Spend ${sp1_chk:.2f} >= $20.00 with 0 sales"
                elif sp2_chk >= 20.00 and pu2_chk == 0:
                    camp_kill_20 = f"2D Campaign Spend ${sp2_chk:.2f} >= $20.00 with 0 sales"
                elif sp3_chk >= 20.00 and pu3_chk == 0:
                    camp_kill_20 = f"3D Campaign Spend ${sp3_chk:.2f} >= $20.00 with 0 sales"
                elif sp1_chk >= 20.00 and pu1_chk > 0 and cpa1_chk is not None and cpa1_chk >= 20.00:
                    camp_kill_20 = f"1D Campaign CPA ${cpa1_chk:.2f} >= $20.00 (${sp1_chk:.2f}/{pu1_chk}p)"
                elif sp3_chk >= 20.00 and pu3_chk > 0 and cpa3_chk is not None and cpa3_chk >= 20.00 and not (pu1_chk > 0 and cpa1_chk is not None and cpa1_chk <= 7.00):
                    camp_kill_20 = f"3D Campaign CPA ${cpa3_chk:.2f} >= $20.00 (${sp3_chk:.2f}/{pu3_chk}p)"
                if camp_kill_20:
                    c_off = dict(c)
                    c_off["status"] = "PAUSED"
                    sm.api_put(f"/adaccounts/{acc_id}/campaigns", token, {"campaigns": [c_off]})
                    extra_paused.append(f"🛑 سناب شات (قاعدة $20 الصارمة): إيقاف الحملة كاملة `{cname}` — {camp_kill_20}")
                    print(f"  🛑 [Campaign $20 Kill] '{cname}' ({cid}) | {camp_kill_20}", flush=True)
                    continue

            # بأمر المستخدم (2026-10-30): أي أدست تصرف 4$ فأكثر بدون بيعة تطفى ولو كان سعر البيعة فيها 7$ فأكثر أيضاً تطفى
            for sq_obj in c_squads.values():
                if sq_obj.get("status") != "ACTIVE":
                    continue
                sq_id_chk = sq_obj["id"]
                sq_name_chk = sq_obj.get("name", "")
                if sm.is_cross_monitored_product_campaign(cname, sq_name_chk):
                    continue
                is_gulf_sq = (cid in getattr(sm, "ALLOWED_GULF_CAMPAIGN_IDS", set())) or sm.is_gulf_034_campaign(cname, sq_name_chk, sq_obj)
                is_ded_test_sq = sm.is_dedicated_snap_test_campaign(cid, cname, sq_name_chk)
                # أمر المستخدم الصارم (2026-11-01):
                # - الأتمتة لا تقترب من البيد نهائياً (لا يطلع ولا ينزل) حتى يأمر المستخدم شخصياً!
                # - حملات 'تيست' المخصصة لا يطفي فيها البوت الحملة ولا الأدست.
                # أمر المستخدم: إذا كانت الحملة 'تيست' أو الأدست بدون كوبي (فيديوهات جديدة سعودية)، لا نطفئ الأدست نفسها بل نفلتر الفيديوهات بداخلها فردياً في القسم التالي!
                if not is_gulf_sq and (is_ded_test_sq or sm.is_non_copy_test_adsquad(sq_name_chk, cname)):
                    continue
                if is_gulf_sq:
                    sq_max_cpa, sq_nosale_lim, _ = sm.get_gulf_simulation_limits(cname, sq_name_chk, sq_obj, cid)
                else:
                    sq_max_cpa = 6.99
                    sq_nosale_lim = 4.00
                sq1 = sq_1d.get(sq_id_chk, {"spend": 0.0, "purchases": 0, "cpa": None})
                sq2 = sq_2d.get(sq_id_chk, {"spend": 0.0, "purchases": 0, "cpa": None})
                sq3 = sq_3d.get(sq_id_chk, {"spend": 0.0, "purchases": 0, "cpa": None})
                sq_good_1d = sq1["purchases"] >= 1 and sq1["cpa"] is not None and sq1["cpa"] <= sq_max_cpa
                sq_good_3d = sq3["purchases"] >= 1 and sq3["cpa"] is not None and sq3["cpa"] <= sq_max_cpa and not (sq1["spend"] >= sq_nosale_lim and sq1["purchases"] == 0)
                sq_kill = None
                if sq1["spend"] >= sq_nosale_lim and sq1["purchases"] == 0:
                    sq_kill = f"1D AdSquad Spend ${sq1['spend']:.2f} >= ${sq_nosale_lim:.2f} with 0 sales"
                elif sq1["purchases"] > 0 and sq1["cpa"] is not None and sq1["cpa"] > sq_max_cpa:
                    sq_kill = f"1D AdSquad CPA ${sq1['cpa']:.2f} >= $7.00 (${sq1['spend']:.2f}/{sq1['purchases']}p)"
                elif sq2["spend"] >= sq_nosale_lim and sq2["purchases"] == 0 and not sq_good_3d:
                    sq_kill = f"2D AdSquad Spend ${sq2['spend']:.2f} >= ${sq_nosale_lim:.2f} with 0 sales"
                elif sq2["purchases"] > 0 and sq2["cpa"] is not None and sq2["cpa"] > sq_max_cpa and not sq_good_1d:
                    sq_kill = f"2D AdSquad CPA ${sq2['cpa']:.2f} >= $7.00 (${sq2['spend']:.2f}/{sq2['purchases']}p)"
                elif sq3["spend"] >= sq_nosale_lim and sq3["purchases"] == 0 and not sq_good_1d:
                    sq_kill = f"3D AdSquad Spend ${sq3['spend']:.2f} >= ${sq_nosale_lim:.2f} with 0 sales"
                elif sq3["purchases"] > 0 and sq3["cpa"] is not None and sq3["cpa"] > sq_max_cpa and not sq_good_1d:
                    sq_kill = f"3D AdSquad CPA ${sq3['cpa']:.2f} >= $7.00 (${sq3['spend']:.2f}/{sq3['purchases']}p)"
                if sq_kill:
                    sq_obj["status"] = "PAUSED"
                    sm.api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": [sq_obj]})
                    history.setdefault("adsquads", {})[sq_id_chk] = {
                        "name": sq_name_chk,
                        "campaign": cname,
                        "reason": f"[MultiWindow AdSquad Dissector] {sq_kill}",
                        "paused_at": now_str,
                    }
                    sm.save_paused_history(history)
                    extra_paused.append(f"🛑 سناب شات: إيقاف الأدست `{sq_name_chk}` في `{cname}` — {sq_kill}")
                    print(f"  🛑 [Extra Dissector Squad Pause] '{sq_name_chk}' in '{cname}' | {sq_kill}", flush=True)

            act_sq_ids = {sq["id"] for sq in c_squads.values() if sq.get("status") == "ACTIVE" and sq_7d.get(sq["id"], {}).get("spend", 0.0) > 0.0}
            c_all_ads = sm.api_get_paginated(f"/campaigns/{cid}/ads?limit=1000", token, "ads", "ad", max_pages=3) if act_sq_ids else []
            act_ads = [a for a in c_all_ads if a.get("status") == "ACTIVE" and a.get("ad_squad_id") in act_sq_ids]

            for a in act_ads:
                aid = a["id"]
                aname = a.get("name", "")
                sq = c_squads.get(a.get("ad_squad_id", ""), {})
                sname = sq.get("name", "")
                if sm.is_cross_monitored_product_campaign(cname, sname):
                    continue
                is_gulf = (cid in getattr(sm, "ALLOWED_GULF_CAMPAIGN_IDS", set())) or sm.is_gulf_034_campaign(cname, sname, sq)
                is_new_test_snap = (not is_gulf) and (sm.is_dedicated_snap_test_campaign(cid, cname, sname) or sm.is_non_copy_test_adsquad(sname, cname))
                if is_gulf:
                    max_cpa, no_sale_lim, _ = sm.get_gulf_simulation_limits(cname, sname, sq, cid)
                else:
                    max_cpa = 6.99
                    no_sale_lim = 4.00

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

                # Check if this active ad violates $6.99 CPA or 0-sale limit ($4.00) or CPC > $0.27 after $1.00 for Test
                has_good_1d = a1["purchases"] >= 1 and a1["cpa"] is not None and a1["cpa"] <= max_cpa
                has_good_3d = (
                    a3["purchases"] >= 1
                    and a3["cpa"] is not None
                    and a3["cpa"] <= max_cpa
                    and not (a1["spend"] >= no_sale_lim and a1["purchases"] == 0)
                    and not (a1["purchases"] > 0 and a1["cpa"] is not None and a1["cpa"] > max_cpa)
                )
                kill_r = None
                if is_new_test_snap and a1["purchases"] == 0 and a1["spend"] >= 1.00 and (a1.get("swipes", 0) == 0 or a1.get("cpc", 0.0) > 0.27):
                    kill_r = f"[Test Video] 1D Spend ${a1['spend']:.2f} (>= $1.00) & CPC ${a1.get('cpc', 0.0):.3f} > $0.27 (0 sales)"
                elif is_new_test_snap and a3["purchases"] == 0 and a3["spend"] >= 1.00 and (a3.get("swipes", 0) == 0 or a3.get("cpc", 0.0) > 0.27):
                    kill_r = f"[Test Video] 3D Spend ${a3['spend']:.2f} (>= $1.00) & CPC ${a3.get('cpc', 0.0):.3f} > $0.27 (0 sales)"
                elif a1["spend"] >= no_sale_lim and a1["purchases"] == 0:
                    kill_r = f"1D Spend ${a1['spend']:.2f} >= ${no_sale_lim:.2f} with 0 sales"
                elif a1["purchases"] > 0 and a1["cpa"] is not None and a1["cpa"] > max_cpa:
                    kill_r = f"1D CPA ${a1['cpa']:.2f} > ${max_cpa:.2f} (${a1['spend']:.2f}/{a1['purchases']}p)"
                elif a2["spend"] >= no_sale_lim and a2["purchases"] == 0 and not has_good_3d:
                    kill_r = f"2D Spend ${a2['spend']:.2f} >= ${no_sale_lim:.2f} with 0 sales"
                elif a2["purchases"] > 0 and a2["cpa"] is not None and a2["cpa"] > max_cpa and not has_good_1d:
                    kill_r = f"2D CPA ${a2['cpa']:.2f} > ${max_cpa:.2f} (${a2['spend']:.2f}/{a2['purchases']}p)"
                elif a3["spend"] >= no_sale_lim and a3["purchases"] == 0:
                    kill_r = f"3D Spend ${a3['spend']:.2f} >= ${no_sale_lim:.2f} with 0 sales"
                elif a3["purchases"] > 0 and a3["cpa"] is not None and a3["cpa"] > max_cpa and not has_good_1d:
                    kill_r = f"3D CPA ${a3['cpa']:.2f} > ${max_cpa:.2f} (${a3['spend']:.2f}/{a3['purchases']}p)"
                elif a7["spend"] >= no_sale_lim and a7["purchases"] == 0 and not has_good_3d:
                    kill_r = f"7D Spend ${a7['spend']:.2f} >= ${no_sale_lim:.2f} with 0 sales"
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

    if "--post-monitor" not in sys.argv:
        tm.main()
    return {"status": "completed"}


# ==============================================================================
# PART 3: GOOGLE ADS 1000% CAREFUL MULTI-WINDOW (1D / 2D / 3D / 7D) AUDITOR
# ==============================================================================
MID = 100


def send_cdp(ws, method, params=None):
    global MID
    if method == "Page.navigate" and isinstance(params, dict) and params.get("url"):
        nav_url = params["url"]
        MID += 1
        my_id = MID
        ws.send(json.dumps({
            "id": my_id,
            "method": "Runtime.evaluate",
            "params": {"expression": f"window.location.href = {json.dumps(nav_url)}; true;", "returnByValue": True},
        }))
        try:
            while True:
                raw = ws.recv()
                msg = json.loads(raw)
                if msg.get("id") == my_id:
                    return msg.get("result", {}), msg.get("error")
        except Exception:
            return {}, None
    if method.startswith("Input."):
        return {}, None
    MID += 1
    my_id = MID
    ws.send(json.dumps({"id": my_id, "method": method, "params": params or {}}))
    old_to = ws.gettimeout()
    ws.settimeout(12.0)
    try:
        while True:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == my_id:
                return msg.get("result", {}), msg.get("error")
    except Exception as e:
        return {}, str(e)
    finally:
        try:
            ws.settimeout(old_to)
        except Exception:
            pass


def eval_js(ws, expr, await_promise=False):
    res, _ = send_cdp(ws, "Runtime.evaluate", {"expression": expr, "awaitPromise": await_promise, "returnByValue": True})
    return (res or {}).get("result", {}).get("value")


def cdp_click(ws, x, y):
    eval_js(
        ws,
        f"""(() => {{
            const el = document.elementFromPoint({int(x)}, {int(y)});
            if (el) {{
                el.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, cancelable: true, clientX: {int(x)}, clientY: {int(y)}}}));
                el.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true, cancelable: true, clientX: {int(x)}, clientY: {int(y)}}}));
                if (typeof el.click === 'function') el.click();
            }}
        }})()""",
    )


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
    for attempt in range(3):
        cur_dt = eval_js(
            ws,
            "(() => { const d = document.querySelector('material-date-range-picker, .date-range-picker'); return d ? (d.innerText || '').replace(/\\s+/g, ' ').trim() : ''; })()",
        ) or ""
        if cur_dt.startswith(option_text):
            return f"SELECTED:{option_text}"

        send_cdp(ws, "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
        send_cdp(ws, "Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
        eval_js(ws, "document.body.click(); window.scrollTo(0, 0); document.querySelectorAll('.particle-table-canvas, ess-table-canvas, .ess-table-canvas, [role=\"grid\"]').forEach(s => s.scrollTop = 0);")
        time.sleep(0.35)

        js_open = """
        (() => {
            const icons = Array.from(document.querySelectorAll('material-date-range-picker material-icon, .date-range-picker material-icon'));
            const arrow = icons.find(i => (i.innerText || '').trim() === 'arrow_drop_down');
            const btn = arrow ? (arrow.closest('[role="button"], material-button, .dropdown-button') || arrow) : document.querySelector('material-date-range-picker .dropdown-button, material-date-range-picker');
            if (!btn) return null;
            const r = btn.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return null;
            return {x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
        })()
        """
        box = None
        for _ in range(10):
            box = eval_js(ws, js_open)
            if box:
                break
            time.sleep(0.6)
        if not box:
            return "NO_DATE_PICKER"

        # Open datepicker via real CDP mouse click; DO NOT call keep_adspower_offscreen while popup is open!
        cdp_click(ws, box["x"], box["y"])
        time.sleep(0.95)

        pk = {"found": False}
        for scroll_pos in (0, 180):
            if scroll_pos > 0:
                eval_js(ws, f"Array.from(document.querySelectorAll('.preset-list, .presets, material-list')).forEach(lst => lst.scrollTop = {scroll_pos});")
                time.sleep(0.15)
            js_pick = f"""
            (() => {{
                const want = {json.dumps(option_text)};
                const items = Array.from(document.querySelectorAll('material-select-item')).filter(el => {{
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }});
                for (const itemEl of items) {{
                    const t = (itemEl.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (t === want || t.startsWith(want + ' ')) {{
                        const r = itemEl.getBoundingClientRect();
                        return {{found: true, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2), label: t}};
                    }}
                }}
                return {{found: false}};
            }})()
            """
            pk = eval_js(ws, js_pick) or {}
            if pk.get("found"):
                break

        if pk.get("found") and pk.get("x") and pk.get("y"):
            cdp_click(ws, pk["x"], pk["y"])
            wait_for_table_change(ws, old_sig, timeout=7.5)
            time.sleep(0.4)
            keep_adspower_offscreen(port)
            new_dt = eval_js(
                ws,
                "(() => { const d = document.querySelector('material-date-range-picker, .date-range-picker'); return d ? (d.innerText || '').replace(/\\s+/g, ' ').trim() : ''; })()",
            ) or ""
            if new_dt.startswith(option_text):
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
        time.sleep(0.3)
        js = """
        (() => {
            const icons = Array.from(document.querySelectorAll('material-date-range-picker material-icon, .date-range-picker material-icon, material-date-range-picker i'));
            const prev = icons.find(el => (el.innerText || '').trim() === 'chevron_left');
            const btn = prev ? (prev.closest('material-button, button, [role="button"]') || prev) : document.querySelector('material-date-range-picker material-button.prev, material-date-range-picker [aria-label="Previous"]');
            if (!btn) return {found: false};
            const r = btn.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
                return {found: true, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
            }
            return {found: false};
        })()
        """
        r = eval_js(ws, js) or {}
        if not r.get("found") or not r.get("x") or not r.get("y"):
            return False
        # Click ONCE via CDP (never mix btn.click() + cdp_click() which jumps 2 days!)
        cdp_click(ws, r["x"], r["y"])
        wait_for_table_change(ws, old_sig, timeout=7.5)
        time.sleep(0.4)
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
            const firstBtn = document.querySelector('[aria-label="Go to the first page"], [aria-label="First page"]');
            if (firstBtn && !firstBtn.disabled && firstBtn.getAttribute('aria-disabled') !== 'true') firstBtn.click();
            const costHdr = document.querySelector('[role="columnheader"][essfield="stats.cost"], .ess-cell-header[essfield="stats.cost"]');
            if (costHdr && (costHdr.getAttribute('aria-sort') || '') !== 'descending') {
                const clickable = costHdr.querySelector('.header-cell-label, .sortable, span') || costHdr;
                clickable.click();
            }
        })()""",
    )
    time.sleep(1.5)
    eval_js(
        ws,
        """(() => {
            const costHdr = document.querySelector('[role="columnheader"][essfield="stats.cost"], .ess-cell-header[essfield="stats.cost"]');
            if (costHdr && (costHdr.getAttribute('aria-sort') || '') === 'ascending') {
                const clickable = costHdr.querySelector('.header-cell-label, .sortable, span') || costHdr;
                clickable.click();
            }
        })()""",
    )
    time.sleep(0.9)
    seen = set()
    rows_out = []
    for _page in range(4):
        eval_js(ws, "window.scrollTo(0, 0); document.querySelectorAll('*').forEach(el => { if (el.scrollTop > 0) el.scrollTop = 0; });")
        time.sleep(0.2)
        no_new_count = 0
        for _step in range(24):
            batch_res = eval_js(
                ws,
                """(() => {
                    const out = [];
                    const rows = Array.from(document.querySelectorAll('.particle-table-row, ess-table-row'));
                    for (const r of rows) {
                        const stCell = r.querySelector('[essfield="status"]');
                        const stImg = stCell ? stCell.querySelector('[role="img"]') : null;
                        let toggle = stImg ? (stImg.getAttribute('aria-label') || '') : '';
                        let camp = ((r.querySelector('[essfield="campaign_name"] a, [essfield="campaign_name"], [essfield="name"] a, [essfield="name"]') || {}).innerText || '').trim();
                        if (camp.includes('\\n')) camp = camp.split('\\n')[0].trim();
                        if (camp.endsWith(' settings')) camp = camp.replace(/\\s*settings$/, '').trim();
                        const ag = ((r.querySelector('[essfield="ad_group_name"], [essfield="ad_group"]') || {}).innerText || '').trim();
                        const ad = ((r.querySelector('.ad-name') || {}).innerText || '').trim();
                        const costTxt = ((r.querySelector('[essfield="stats.cost"]') || {}).innerText || '').trim();
                        const clicksTxt = ((r.querySelector('[essfield="stats.clicks"], [essfield="stats.interactions"]') || {}).innerText || '').trim();
                        const imprTxt = ((r.querySelector('[essfield="stats.impressions"]') || {}).innerText || '').trim();
                        const cpcTxt = ((r.querySelector('[essfield="stats.cost_per_click"], [essfield="stats.average_cpc"]') || {}).innerText || '').trim();
                        const convTxt = ((r.querySelector('[essfield="stats.conversions"], [essfield="conversions"]') || {}).innerText || '').trim();
                        let txt = (r.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (txt.includes('View asset details')) {
                            txt = txt.split('View asset details')[1].trim();
                        }
                        if (toggle === 'Enabled' && /Campaign is paused|Ad group is paused|Campaign ended/i.test(txt)) {
                            toggle = 'Paused (Parent)';
                        }
                        if (txt.length > 5) {
                            const isZero = (!costTxt || /(?:^|\\$)0\\.00$/.test(costTxt)) && /\\b0\\s+0\\s+(?:—|0\\.00%)\\s+(?:US)?\\$0\\.00/.test(txt);
                            if (toggle === 'Enabled' || !isZero || txt.includes('Total:')) {
                                out.push({toggle, camp, ag, ad, costTxt, clicksTxt, imprTxt, cpcTxt, convTxt, text: txt.slice(0, 320)});
                            }
                        }
                    }
                    window.scrollBy(0, 550);
                    document.querySelectorAll('.particle-table-canvas, ess-table-canvas, [role="grid"], .aw-page-content, main, .content-container').forEach(el => {
                        if (el.scrollHeight > el.clientHeight) el.scrollTop += 550;
                    });
                    return {out};
                })()""",
            ) or {}
            batch = batch_res.get("out") or []
            added_this_step = 0
            for item in batch:
                key = f"{item.get('toggle')}|{item.get('camp')}|{item.get('ag')}|{item.get('ad')}|{item.get('text', '')[:140]}"
                if key not in seen:
                    seen.add(key)
                    rows_out.append(item)
                    added_this_step += 1
            time.sleep(0.18)
            if added_this_step == 0:
                no_new_count += 1
                if no_new_count >= 4:
                    break
            else:
                no_new_count = 0
        eval_js(ws, "const s = document.querySelector('.particle-table-canvas, ess-table-canvas, [role=\"grid\"]'); if (s) s.scrollTop = 0; else window.scrollTo(0, 0);")
        has_next = eval_js(
            ws,
            """(() => {
                const nextBtn = Array.from(document.querySelectorAll('[aria-label="Go to the next page"]')).find(el => !el.closest('material-date-range-picker, .date-range-picker'));
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
    for _ in range(4):
        went_prev = eval_js(
            ws,
            """(() => {
                const firstBtn = Array.from(document.querySelectorAll('[aria-label="Go to the first page"], [aria-label="Go to the previous page"]')).find(el => !el.closest('material-date-range-picker, .date-range-picker'));
                if (firstBtn && !firstBtn.disabled && firstBtn.getAttribute('aria-disabled') !== 'true') { firstBtn.click(); return true; }
                return false;
            })()""",
        )
        if not went_prev:
            break
        time.sleep(0.8)
    date_txt = eval_js(
        ws,
        "(() => { const d = document.querySelector('material-date-range-picker, .date-range-picker'); return d ? (d.innerText || '').replace(/\\s+/g, ' ').trim() : ''; })()",
    ) or ""
    return {"dateText": date_txt, "rows": rows_out}


def parse_google_ad_metrics(row_obj: dict) -> dict:
    """
    Carefully parses a Google Ads /aw/ads row using explicit [essfield] attributes first,
    with fallback to row text regex.
    """
    cost_txt = row_obj.get("costTxt") or ""
    if cost_txt:
        m_cost = re.search(r"([\d,]+\.\d{2})", cost_txt)
        cost = float(m_cost.group(1).replace(",", "")) if m_cost else 0.0
        clicks_txt = (row_obj.get("clicksTxt") or "").replace(",", "").strip()
        clicks = int(clicks_txt) if clicks_txt.isdigit() else 0
        impr_txt = (row_obj.get("imprTxt") or "").replace(",", "").strip()
        impr = int(impr_txt) if impr_txt.isdigit() else 0
        cpc_txt = row_obj.get("cpcTxt") or ""
        m_cpc = re.search(r"([\d,]+\.\d{2})", cpc_txt)
        cpc = float(m_cpc.group(1).replace(",", "")) if m_cpc else (round(cost / clicks, 2) if clicks > 0 else 0.0)
        conv_txt = row_obj.get("convTxt") or ""
        m_conv = re.search(r"([\d,]+(?:\.\d+)?)", conv_txt)
        conv = float(m_conv.group(1).replace(",", "")) if m_conv else 0.0
        return {"clicks": clicks, "impr": impr, "cpc": cpc, "spend": cost, "ui_conv": conv}

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
    for _ in range(4):
        went_prev = eval_js(
            ws,
            """(() => {
                const firstBtn = Array.from(document.querySelectorAll('[aria-label="Go to the first page"], [aria-label="Go to the previous page"]')).find(el => !el.closest('material-date-range-picker, .date-range-picker'));
                if (firstBtn && !firstBtn.disabled && firstBtn.getAttribute('aria-disabled') !== 'true') { firstBtn.click(); return true; }
                return false;
            })()""",
        )
        if not went_prev:
            break
        time.sleep(0.8)

    for _page_idx in range(4):
        eval_js(ws, "const s = document.querySelector('.particle-table-canvas, ess-table-canvas, [role=\"grid\"]'); if (s) s.scrollTop = 0; else window.scrollTo(0, 0);")
        time.sleep(0.15)
        for _step in range(18):
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
                        const btn = stCell.querySelector('.ess-edit-icon, [role="button"], aw-status, [role="img"]') || stCell;
                        if (btn) {{
                            const br = btn.getBoundingClientRect();
                            btn.click();
                            return {{res: 'OPENED', cur, bx: Math.round(br.left + br.width/2), by: Math.round(br.top + br.height/2)}};
                        }}
                        return {{res: 'NO_BTN', cur}};
                    }}
                }}
                const scroller = document.querySelector('.particle-table-canvas, ess-table-canvas, [role="grid"]');
                let atBottom = false;
                if (scroller) {{
                    const prev = scroller.scrollTop;
                    scroller.scrollTop += 650;
                    atBottom = (scroller.scrollTop === prev) || (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 10);
                }}
                return {{res: 'NOT_FOUND', atBottom}};
            }})()
            """
            info = eval_js(ws, js_find) or {}
            if info.get("res") == "NOT_FOUND":
                if info.get("atBottom"):
                    break
                eval_js(ws, "window.scrollBy(0, 650); document.querySelectorAll('.particle-table-canvas, ess-table-canvas, [role=\"grid\"]').forEach(e => { if (e.scrollHeight > e.clientHeight) e.scrollTop += 650; });")
                time.sleep(0.14)
                continue
            if info.get("res", "").startswith("ALREADY_") or info.get("res") == "NO_BTN":
                return info["res"]
            if info.get("res") == "OPENED":
                time.sleep(0.6)
                # DO NOT call keep_adspower_offscreen(port) while status dropdown popup is open!
                js_menu = f"""
                (() => {{
                    const items = Array.from(document.querySelectorAll('material-select-item, material-select-dropdown-item, [role="menuitem"]:not(a), [role="option"]:not(a)')).filter(el => {{
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }});
                    for (const item of items) {{
                        const t = (item.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (t === {json.dumps(desired_status)} || t.endsWith(' ' + {json.dumps(desired_status)})) {{
                            const r = item.getBoundingClientRect();
                            item.click();
                            return {{found: true, t, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}};
                        }}
                    }}
                    return {{found: false}};
                }})()
                """
                minfo = eval_js(ws, js_menu) or {}
                if not minfo.get("found") and info.get("bx") and info.get("by"):
                    cdp_click(ws, info["bx"], info["by"])
                    time.sleep(0.75)
                    minfo = eval_js(ws, js_menu) or {}
                if minfo.get("found"):
                    time.sleep(1.2)
                    cur_after = eval_js(
                        ws,
                        f"""(() => {{
                            const rows = Array.from(document.querySelectorAll('.particle-table-row, ess-table-row'));
                            for (const r of rows) {{
                                const c = ((r.querySelector('[essfield="campaign_name"]') || {{}}).innerText || '').trim();
                                const ag = ((r.querySelector('[essfield="ad_group_name"], [essfield="ad_group"]') || {{}}).innerText || '').trim();
                                const n = ((r.querySelector('.ad-name') || {{}}).innerText || '').trim();
                                if (c === {json.dumps(camp_name)} && n === {json.dumps(ad_name)} && (!{json.dumps(ag_name or "")} || ag === {json.dumps(ag_name or "")})) {{
                                    const img = r.querySelector('[essfield="status"] [role="img"]');
                                    return img ? (img.getAttribute('aria-label') || '') : '';
                                }}
                            }}
                            return '';
                        }})()""",
                    ) or ""
                    if cur_after != expected_label and minfo.get("x") and minfo.get("y"):
                        cdp_click(ws, minfo["x"], minfo["y"])
                        time.sleep(1.2)
                    keep_adspower_offscreen(port)
                    return f"{desired_status.upper()}_SUCCESS (was {info.get('cur')})"
                return "MENU_NOT_FOUND"
        eval_js(ws, "const s = document.querySelector('.particle-table-canvas, ess-table-canvas, [role=\"grid\"]'); if (s) s.scrollTop = 0; else window.scrollTo(0, 0);")
        has_next = eval_js(
            ws,
            """(() => {
                const nextBtn = Array.from(document.querySelectorAll('[aria-label="Go to the next page"]')).find(el => !el.closest('material-date-range-picker, .date-range-picker'));
                if (nextBtn && !nextBtn.disabled && nextBtn.getAttribute('aria-disabled') !== 'true') { nextBtn.click(); return true; }
                return false;
            })()""",
        )
        if not has_next:
            break
        time.sleep(1.3)
        time.sleep(1.5)
    return "NOT_FOUND"


def set_google_campaign_status_carefully(ws, camp_name: str, desired_status: str = "Pause", port=None) -> str:
    expected_label = "Enabled" if desired_status == "Enable" else "Paused"
    eval_js(ws, "document.body.click()")
    time.sleep(0.2)
    eval_js(ws, "window.scrollTo(0, 0); document.querySelectorAll('.particle-table-canvas, ess-table-canvas, [role=\"grid\"]').forEach(s => s.scrollTop = 0);")
    time.sleep(0.2)
    for _step in range(18):
        js_find = f"""
        (() => {{
            const rows = Array.from(document.querySelectorAll('.particle-table-row, ess-table-row, [role="row"]'));
            for (const r of rows) {{
                const cnEl = r.querySelector('[essfield="campaign_name"] a, [essfield="campaign_name"], [essfield="name"] a, [essfield="name"]');
                let c = (cnEl ? cnEl.innerText : '').trim();
                if (c.includes('\\n')) c = c.split('\\n')[0].trim();
                if (c.endsWith(' settings')) c = c.replace(/\\s*settings$/, '').trim();
                if (c === {json.dumps(camp_name)}) {{
                    r.scrollIntoView({{block: 'center'}});
                    const stCell = r.querySelector('[essfield="status"]');
                    if (!stCell) continue;
                    const img = stCell.querySelector('[role="img"]');
                    const cur = img ? (img.getAttribute('aria-label') || '') : '';
                    if (cur === {json.dumps(expected_label)}) return {{res: 'ALREADY_' + cur}};
                    stCell.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
                    stCell.dispatchEvent(new MouseEvent('mouseover', {{bubbles: true}}));
                    r.dispatchEvent(new MouseEvent('mouseenter', {{bubbles: true}}));
                    const btn = stCell.querySelector('.ess-edit-icon, [role="button"], aw-status, [role="img"]') || stCell;
                    if (btn) {{
                        const br = btn.getBoundingClientRect();
                        btn.click();
                        return {{res: 'OPENED', cur, bx: Math.round(br.left + br.width/2), by: Math.round(br.top + br.height/2)}};
                    }}
                    return {{res: 'NO_BTN', cur}};
                }}
            }}
            const scroller = document.querySelector('.particle-table-canvas, ess-table-canvas, [role="grid"]');
            let atBottom = false;
            if (scroller) {{
                const prev = scroller.scrollTop;
                scroller.scrollTop += 600;
                atBottom = (scroller.scrollTop === prev) || (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 10);
            }}
            return {{res: 'SCROLLING', atBottom}};
        }})()
        """
        info = eval_js(ws, js_find) or {}
        res_code = info.get("res", "")
        if res_code.startswith("ALREADY_") or res_code == "NO_BTN":
            return res_code
        if res_code == "OPENED":
            time.sleep(0.55)
            js_menu = f"""
            (() => {{
                const items = Array.from(document.querySelectorAll('material-select-item, material-list-item, [role="menuitem"], [role="option"]')).filter(el => {{
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }});
                for (const it of items) {{
                    const t = (it.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (t === {json.dumps(desired_status)} || t === {json.dumps(expected_label)}) {{
                        const r = it.getBoundingClientRect();
                        it.click();
                        return {{found: true, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}};
                    }}
                }}
                return {{found: false}};
            }})()
            """
            minfo = eval_js(ws, js_menu) or {}
            if not minfo.get("found") and info.get("bx") and info.get("by"):
                cdp_click(ws, info["bx"], info["by"])
                time.sleep(0.55)
                minfo = eval_js(ws, js_menu) or {}
            if minfo.get("found"):
                time.sleep(1.2)
                cur_after_c = eval_js(
                    ws,
                    f"""(() => {{
                        const rows = Array.from(document.querySelectorAll('.particle-table-row, ess-table-row, [role="row"]'));
                        for (const r of rows) {{
                            const cnEl = r.querySelector('[essfield="campaign_name"] a, [essfield="campaign_name"], [essfield="name"] a, [essfield="name"]');
                            let c = (cnEl ? cnEl.innerText : '').trim().split('\\n')[0].replace(/\\s*settings$/, '').trim();
                            if (c === {json.dumps(camp_name)}) {{
                                const img = r.querySelector('[essfield="status"] [role="img"]');
                                return img ? (img.getAttribute('aria-label') || '') : '';
                            }}
                        }}
                        return '';
                    }})()""",
                ) or ""
                if cur_after_c != expected_label and minfo.get("x") and minfo.get("y"):
                    cdp_click(ws, minfo["x"], minfo["y"])
                    time.sleep(1.2)
                keep_adspower_offscreen(port)
                return f"{desired_status.upper()}_SUCCESS (was {info.get('cur')})"
            return "MENU_NOT_FOUND"
        if info.get("atBottom"):
            break
        eval_js(ws, "window.scrollBy(0, 600); document.querySelectorAll('.particle-table-canvas, ess-table-canvas, [role=\"grid\"]').forEach(e => { if (e.scrollHeight > e.clientHeight) e.scrollTop += 600; });")
        time.sleep(0.16)
    return "NOT_FOUND"


def audit_google_ads_multiwindow() -> dict:
    from run_all_monitors_5h import find_open_adspower_ports

    if os.path.exists(r"C:\Users\salah\.gemini\antigravity\scratch\pause_gads_bot.flag"):
        print("⏸️ [Google Ads Audit Paused] تم إيقاف بوت جوجل أدز مؤقتاً بطلب المستخدم لاستخدام المتصفح بحرية.", flush=True)
        return {"accounts": [], "actions": []}

    print("\n======================================================================")
    print("🌐 [3/3] فحص وتفصيص حسابات Google Ads عبر 4 نوافذ (1D / 2D / 3D / 7D) بحرص 1000% (بيكسل + شيت Lightfunnels)")
    print("======================================================================", flush=True)

    ports = dict(find_open_adspower_ports())
    if len([k for k in ports if "k1cekgik" in k or "k1dh2jyf" in k]) < 2:
        try:
            from gads_hourly_tcpa_bumper import ensure_adspower_profiles_open
            opened_p = ensure_adspower_profiles_open()
            for pk, pv in (opened_p or {}).items():
                if not any(pk in ek for ek in ports):
                    ports[pk] = pv
        except Exception:
            pass
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
            g_tabs = [t for t in tabs if t.get("type") == "page" and "ads.google.com" in t.get("url", "") and "/aw/ads" in t.get("url", "") and "/campaigns/new" not in t.get("url", "") and "/demandgen" not in t.get("url", "")]
            if not g_tabs:
                g_tabs = [t for t in tabs if t.get("type") == "page" and "ads.google.com" in t.get("url", "") and "/campaigns/new" not in t.get("url", "") and "/demandgen" not in t.get("url", "") and "/construction" not in t.get("url", "")]
            if not g_tabs:
                # If only a /campaigns/new tab exists, open a separate background /aw/ads tab instead of skipping the account audit!
                try:
                    ver_info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3).read().decode("utf-8"))
                    bws_new = websocket.create_connection(ver_info["webSocketDebuggerUrl"].replace("ws://localhost:", "ws://127.0.0.1:"), suppress_origin=True, timeout=5)
                    new_ads_url = f"https://ads.google.com/aw/ads?ocid={ocid}" if ocid else "https://ads.google.com/aw/ads"
                    bws_new.send(json.dumps({"id": 1, "method": "Target.createTarget", "params": {"url": new_ads_url}}))
                    bws_new.recv()
                    bws_new.close()
                    keep_adspower_offscreen(port)
                    time.sleep(5.5)
                    tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=4).read().decode("utf-8"))
                    g_tabs = [t for t in tabs if t.get("type") == "page" and "ads.google.com" in t.get("url", "") and "/aw/ads" in t.get("url", "")]
                except Exception:
                    pass
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
            send_cdp(ws, "Emulation.setDeviceMetricsOverride", {"width": 1600, "height": 4500, "deviceScaleFactor": 1, "mobile": False})
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
                # If stuck on Yesterday or earlier day, click chevron_right up to 15 times until it reaches Today
                for _ in range(15):
                    clicked_nxt = eval_js(ws, """(() => {
                        const icons = Array.from(document.querySelectorAll('material-date-range-picker material-icon, .date-range-picker material-icon'));
                        const nxt = icons.find(el => (el.innerText || '').trim() === 'chevron_right');
                        if (nxt) {
                            const b = nxt.closest('material-button, button, [role="button"]') || nxt;
                            if (!b.disabled && b.getAttribute('aria-disabled') !== 'true' && !b.classList.contains('is-disabled')) {
                                b.click();
                                return true;
                            }
                        }
                        return false;
                    })()""")
                    if not clicked_nxt:
                        break
                    time.sleep(1.8)
                    cur_chk = eval_js(ws, "(() => { const d = document.querySelector('material-date-range-picker, .date-range-picker'); return d ? (d.innerText || '').replace(/\\s+/g, ' ').trim() : ''; })()") or ""
                    if cur_chk.startswith("Today"):
                        break
                if not (eval_js(ws, "(() => { const d = document.querySelector('material-date-range-picker, .date-range-picker'); return d ? (d.innerText || '').replace(/\\s+/g, ' ').trim() : ''; })()") or "").startswith("Today"):
                    select_date_preset(ws, "Today", port=port)
                tbl_d0 = scrape_google_ads_table(ws)
                date_d0 = tbl_d0.get("dateText", "")
            is_at_latest_day = eval_js(ws, """(() => {
                const icons = Array.from(document.querySelectorAll('material-date-range-picker material-icon, .date-range-picker material-icon'));
                const nxt = icons.find(el => (el.innerText || '').trim() === 'chevron_right');
                if (!nxt) return false;
                const b = nxt.closest('material-button, button, [role="button"]') || nxt;
                return Boolean(b.disabled || b.getAttribute('aria-disabled') === 'true' || b.classList.contains('is-disabled'));
            })()""")
            today_verified = date_d0.startswith("Today") or (bool(is_at_latest_day) and "ago" not in date_d0.lower())

            # 2) Select Yesterday (d1) directly via preset (fallback to prev-day arrow if needed)
            res_yest = select_date_preset(ws, "Yesterday", port=port)
            if not str(res_yest).startswith("SELECTED:"):
                click_prev_day_js(ws, prev_date_text=date_d0)
            tbl_d1 = scrape_google_ads_table(ws)
            date_d1 = tbl_d1.get("dateText", "Yesterday")

            # 3) Click prev day once from Yesterday -> 2 Days Ago (d2)
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
            if date_d0.startswith("Today"):
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
            seen_slot_fps = set()

            def ingest_table(tbl, slot):
                dt_str = (tbl.get("dateText") or "").strip()
                rows_list = tbl.get("rows", []) or []
                row_fp = tuple(
                    (r.get("camp"), r.get("ag"), r.get("ad"), r.get("text"))
                    for r in rows_list
                    if r.get("ad") and "Total:" not in (r.get("text") or "")
                )[:8]
                if slot != "d0":
                    if dt_str and dt_str in seen_slot_dates:
                        return
                    if row_fp and row_fp in seen_slot_fps:
                        return
                if dt_str:
                    seen_slot_dates.add(dt_str)
                if row_fp:
                    seen_slot_fps.add(row_fp)
                for r in rows_list:
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
                    elif str(r.get("toggle", "")).startswith("Paused"):
                        entry["toggle"] = r.get("toggle")
                    m = parse_google_ad_metrics(r)
                    if m["spend"] >= entry[slot]["spend"]:
                        entry[slot] = m

            ingest_table(tbl_d0, "d0")
            ingest_table(tbl_d1, "d1")
            ingest_table(tbl_d2, "d2")
            ingest_table(tbl_l7, "l7")

            # =====================================================================
            # MANDATORY CAMPAIGN-LEVEL 3-DAY $20.00 NO-SALE & $21.00 CPA BRAKE (2026-10-30):
            # "بالنسبة لغوغل ادس ثبت القاعدة هاذي انا الحملة لو صرفت 20$ بدون مبيعات يطفيها بعد ما يرجع اخر 3 ايام طبعاً ولو سعر البيع 21$ سطفيها"
            # =====================================================================
            camp_agg = {}
            for (c_nm, ag_nm, ad_nm), inf_c in ads_map.items():
                d0_c, d1_c, d2_c, l7_c = inf_c["d0"], inf_c["d1"], inf_c["d2"], inf_c["l7"]
                sp1_c = round(d0_c["spend"], 2)
                sp2_c = round(d0_c["spend"] + d1_c["spend"], 2)
                sp3_c = round(d0_c["spend"] + d1_c["spend"] + d2_c["spend"], 2)
                sp7_c = round(max(sp3_c, d0_c["spend"] + l7_c["spend"]), 2)
                ui1_c = d0_c["ui_conv"]
                ui2_c = round(d0_c["ui_conv"] + d1_c["ui_conv"], 2)
                ui3_c = round(d0_c["ui_conv"] + d1_c["ui_conv"] + d2_c["ui_conv"], 2)
                ui7_c = round(max(ui3_c, d0_c["ui_conv"] + l7_c["ui_conv"]), 2)
                ad_k_c = ad_nm.strip().lower()
                c_pcat_c, c_ccat_c = classify_product_and_country(c_nm, c_nm)
                strict_k_c = f"{ad_k_c}|{c_pcat_c}|{c_ccat_c}"
                if c_pcat_c != "other":
                    sh1_c = sheet.get("1d_strict", {}).get(strict_k_c, 0)
                    sh2_c = sheet.get("2d_strict", {}).get(strict_k_c, 0)
                    sh3_c = sheet.get("3d_strict", {}).get(strict_k_c, 0)
                    sh7_c = sheet.get("7d_strict", {}).get(strict_k_c, 0)
                else:
                    sh1_c = sheet["1d"].get(ad_k_c, 0)
                    sh2_c = sheet["2d"].get(ad_k_c, 0)
                    sh3_c = sheet["3d"].get(ad_k_c, 0)
                    sh7_c = sheet["7d"].get(ad_k_c, 0)
                same_ad_c = [
                    k for k, v_k in ads_map.items()
                    if k[2].strip().lower() == ad_k_c
                    and (v_k["d0"]["spend"] + v_k["d1"]["spend"] + v_k["d2"]["spend"]) > 2.50
                ]
                if len(same_ad_c) > 1:
                    csp1 = sum(ads_map[k]["d0"]["spend"] for k in same_ad_c)
                    csp2 = sum(ads_map[k]["d0"]["spend"] + ads_map[k]["d1"]["spend"] for k in same_ad_c)
                    csp3 = sum(ads_map[k]["d0"]["spend"] + ads_map[k]["d1"]["spend"] + ads_map[k]["d2"]["spend"] for k in same_ad_c)
                    csp7 = sum(max(ads_map[k]["d0"]["spend"] + ads_map[k]["d1"]["spend"] + ads_map[k]["d2"]["spend"], ads_map[k]["l7"]["spend"] + ads_map[k]["d0"]["spend"]) for k in same_ad_c)
                    cui7 = sum(max(ads_map[k]["d0"].get("ui_conv", 0.0) + ads_map[k]["d1"].get("ui_conv", 0.0) + ads_map[k]["d2"].get("ui_conv", 0.0), ads_map[k]["l7"].get("ui_conv", 0.0) + ads_map[k]["d0"].get("ui_conv", 0.0)) for k in same_ad_c)
                    if ui7_c == 0 and cui7 > 0 and sp7_c >= 6.0:
                        ash1 = ash2 = ash3 = ash7 = 0.0
                    else:
                        ash1 = float(sh1_c) * (sp1_c / csp1) if csp1 > 0 else (float(sh1_c) / len(same_ad_c))
                        ash2 = float(sh2_c) * (sp2_c / csp2) if csp2 > 0 else (float(sh2_c) / len(same_ad_c))
                        ash3 = float(sh3_c) * (sp3_c / csp3) if csp3 > 0 else (float(sh3_c) / len(same_ad_c))
                        ash7 = float(sh7_c) * (sp7_c / csp7) if csp7 > 0 else (float(sh7_c) / len(same_ad_c))
                else:
                    ash1, ash2, ash3, ash7 = float(sh1_c), float(sh2_c), float(sh3_c), float(sh7_c)

                cb = camp_agg.setdefault(c_nm, {
                    "sp1": 0.0, "sp2": 0.0, "sp3": 0.0, "sp7": 0.0,
                    "ui1": 0.0, "ui2": 0.0, "ui3": 0.0, "ui7": 0.0,
                    "sh_by_ad": {},
                    "has_enabled": False,
                })
                if inf_c.get("toggle") == "Enabled":
                    cb["has_enabled"] = True
                cb["sp1"] = round(cb["sp1"] + sp1_c, 2)
                cb["sp2"] = round(cb["sp2"] + sp2_c, 2)
                cb["sp3"] = round(cb["sp3"] + sp3_c, 2)
                cb["sp7"] = round(cb["sp7"] + sp7_c, 2)
                cb["ui1"] = round(cb["ui1"] + ui1_c, 2)
                cb["ui2"] = round(cb["ui2"] + ui2_c, 2)
                cb["ui3"] = round(cb["ui3"] + ui3_c, 2)
                cb["ui7"] = round(cb["ui7"] + ui7_c, 2)
                if ad_k_c not in cb["sh_by_ad"]:
                    cb["sh_by_ad"][ad_k_c] = (ash1, ash2, ash3, ash7)

            violating_20dollar_camps = {}
            taager_gads_cache_path = BASE_DIR / "taager_gads_latest_stats.json"
            try:
                taager_gads_cache = json.loads(taager_gads_cache_path.read_text(encoding="utf-8")) if taager_gads_cache_path.exists() else {}
            except Exception:
                taager_gads_cache = {}
            taager_gads_cache.setdefault("campaigns", {})
            for c_nm, cb in camp_agg.items():
                sh1_tot = round(sum(v[0] for v in cb["sh_by_ad"].values()), 2)
                sh2_tot = round(sum(v[1] for v in cb["sh_by_ad"].values()), 2)
                sh3_tot = round(sum(v[2] for v in cb["sh_by_ad"].values()), 2)
                sh7_tot = round(sum(v[3] for v in cb["sh_by_ad"].values()), 2)
                eff1_tot = max(cb["ui1"], sh1_tot)
                eff2_tot = max(cb["ui2"], sh2_tot)
                eff3_tot = max(cb["ui3"], sh3_tot)
                eff7_tot = max(cb["ui7"], sh7_tot)
                cpa1_tot = round(cb["sp1"] / eff1_tot, 2) if eff1_tot > 0 else None
                cpa2_tot = round(cb["sp2"] / eff2_tot, 2) if eff2_tot > 0 else None
                cpa3_tot = round(cb["sp3"] / eff3_tot, 2) if eff3_tot > 0 else None
                cpa7_tot = round(cb["sp7"] / eff7_tot, 2) if eff7_tot > 0 else None
                prof_today = eff1_tot >= 1.0 and cpa1_tot is not None and cpa1_tot <= 7.00

                c_nm_low = c_nm.lower()
                is_taager_c = any(k in c_nm_low for k in ("taager", "تاجر", "washer", "hp909", "hp-909", "غسالة", "bakhoor", "bw20", "bw-20", "مبخرة", "r8019", "r.8019", "8019", "raf ", "cooker", "موقد"))
                taager_prod_key_c = None
                if is_taager_c:
                    if any(k in c_nm_low for k in ("washer", "hp909", "hp-909", "غسالة")):
                        taager_prod_key_c = "washer_hp909"
                    elif any(k in c_nm_low for k in ("bakhoor", "bw20", "bw-20", "مبخرة")):
                        taager_prod_key_c = "bakhoor_bw20"
                    elif any(k in c_nm_low for k in ("r8019", "r.8019", "8019", "raf", "cooker", "موقد")):
                        taager_prod_key_c = "cooker_raf8019"
                    else:
                        taager_prod_key_c = "taager_product"
                    taager_gads_cache["campaigns"][f"{ocid}:{c_nm}"] = {
                        "ocid": ocid,
                        "account_label": label,
                        "campaign_name": c_nm,
                        "product_key": taager_prod_key_c,
                        "has_enabled": bool(cb.get("has_enabled")),
                        "sp1": cb["sp1"], "sp2": cb["sp2"], "sp3": cb["sp3"], "sp7": cb["sp7"],
                        "ui1": cb["ui1"], "ui2": cb["ui2"], "ui3": cb["ui3"], "ui7": cb["ui7"],
                        "sh1": sh1_tot, "sh2": sh2_tot, "sh3": sh3_tot, "sh7": sh7_tot,
                        "eff1": eff1_tot, "eff2": eff2_tot, "eff3": eff3_tot, "eff7": eff7_tot,
                        "updated_at": datetime.now(SA_TZ).isoformat(),
                    }

                c_no_sale_lim = 25.00 if is_taager_c else 20.00
                c_cpa_lim = 27.00 if is_taager_c else 20.00

                # الفحص الإلزامي لآخر 3 أيام (3D) واليوم (1D) ويومين (2D) و7 أيام (7D):
                # - للحملات العادية: >= $20.00 بدون مبيعات أو CPA >= $20.00 -> إيقاف فوري للحملة كاملة
                # - لحملات تيست منتجات تاجر (كل منتج على حدة): > $25.00 بدون مبيعات أو CPA >= $27.00 -> إيقاف كل حملات المنتج في غوغل وتيك توك
                c_reason = None
                try:
                    ph_p = BASE_DIR / "paused_history.json"
                    if taager_prod_key_c and ph_p.exists():
                        ph_d = json.loads(ph_p.read_text(encoding="utf-8"))
                        if taager_prod_key_c in (ph_d.get("taager_failed_products") or {}):
                            c_reason = f"[قاعدة تيست تاجر: المنتج فاشل '{taager_prod_key_c}'] مجموع صرف المنتج تعدى $25.00 بدون بيع أو وصل CPA >= $27.00"
                except Exception:
                    pass
                if not c_reason and any(k in c_nm_low for k in ("31-10", "rescue 10 ads", "recycled 5ads")):
                    c_reason = "حملة 31-10 موقوفة بأمر المستخدم (زيارات رديئة رخيصة وCPA مرتفع)"
                elif not c_reason and (cb["sp1"] > 25.00 if is_taager_c else cb["sp1"] >= c_no_sale_lim) and eff1_tot == 0:
                    c_reason = f"الحملة صرفت اليوم (1D) ${cb['sp1']:.2f} ({'>' if is_taager_c else '>='} ${c_no_sale_lim:.2f}) بـ 0 مبيعات"
                elif not c_reason and (cb["sp2"] > 25.00 if is_taager_c else cb["sp2"] >= c_no_sale_lim) and eff2_tot == 0:
                    c_reason = f"الحملة صرفت في يومين (2D) ${cb['sp2']:.2f} ({'>' if is_taager_c else '>='} ${c_no_sale_lim:.2f}) بـ 0 مبيعات"
                elif not c_reason and (cb["sp3"] > 25.00 if is_taager_c else cb["sp3"] >= c_no_sale_lim) and eff3_tot == 0:
                    c_reason = f"الحملة صرفت في آخر 3 أيام (3D) ${cb['sp3']:.2f} ({'>' if is_taager_c else '>='} ${c_no_sale_lim:.2f}) بـ 0 مبيعات"
                elif not c_reason and (cb["sp7"] > 25.00 if is_taager_c else cb["sp7"] >= c_no_sale_lim) and eff7_tot == 0:
                    c_reason = f"الحملة صرفت في آخر 7 أيام (7D) ${cb['sp7']:.2f} ({'>' if is_taager_c else '>='} ${c_no_sale_lim:.2f}) بـ 0 مبيعات"
                elif not c_reason and cb["sp1"] >= c_cpa_lim and eff1_tot > 0 and cpa1_tot is not None and cpa1_tot >= c_cpa_lim:
                    c_reason = f"سعر المبيعة في الحملة اليوم (1D) CPA=${cpa1_tot:.2f} (>= ${c_cpa_lim:.2f}) [صرف ${cb['sp1']:.2f} / {eff1_tot:g} مبيعات]"
                elif not c_reason and cb["sp2"] >= c_cpa_lim and eff2_tot > 0 and cpa2_tot is not None and cpa2_tot >= c_cpa_lim and not prof_today:
                    c_reason = f"سعر المبيعة في الحملة ليومين (2D) CPA=${cpa2_tot:.2f} (>= ${c_cpa_lim:.2f}) [صرف ${cb['sp2']:.2f} / {eff2_tot:g} مبيعات]"
                elif not c_reason and cb["sp3"] >= c_cpa_lim and eff3_tot > 0 and cpa3_tot is not None and cpa3_tot >= c_cpa_lim and not prof_today:
                    c_reason = f"سعر المبيعة في الحملة لآخر 3 أيام (3D) CPA=${cpa3_tot:.2f} (>= ${c_cpa_lim:.2f}) [صرف ${cb['sp3']:.2f} / {eff3_tot:g} مبيعات]"
                if c_reason:
                    violating_20dollar_camps[c_nm] = c_reason
                    print(f"  🚨 [حملة مخالفة لقاعدة (${c_no_sale_lim:.0f} بدون بيع / ${c_cpa_lim:.0f} CPA)] '{c_nm}' -> {c_reason}", flush=True)
            try:
                taager_gads_cache_path.write_text(json.dumps(taager_gads_cache, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass

            # إيقاف الحملات المخالفة فوراً على مستوى الحملة (/aw/campaigns) أولاً قبل فحص الإعلانات الفردية لمنع أي سنت إضافي!
            active_violating_20dollar_camps = {
                c_v: r_v for c_v, r_v in violating_20dollar_camps.items()
                if camp_agg.get(c_v, {}).get("has_enabled") or camp_agg.get(c_v, {}).get("sp1", 0.0) > 0.0 or any(k in c_v.lower() for k in ("31-10", "rescue 10 ads", "recycled 5ads"))
            }
            if active_violating_20dollar_camps and today_verified:
                camps_nav_url = f"https://ads.google.com/aw/campaigns?ocid={ocid}" if ocid else "https://ads.google.com/aw/campaigns"
                send_cdp(ws, "Page.navigate", {"url": camps_nav_url})
                time.sleep(5.5)
                keep_adspower_offscreen(port)
                select_date_preset(ws, "Today", port=port)
                time.sleep(1.0)
                for c_viol, c_rsn in active_violating_20dollar_camps.items():
                    res_cp = set_google_campaign_status_carefully(ws, c_viol, "Pause", port=port)
                    if res_cp == "NOT_FOUND":
                        select_date_preset(ws, "Last 7 days", port=port)
                        time.sleep(1.0)
                        res_cp = set_google_campaign_status_carefully(ws, c_viol, "Pause", port=port)
                        select_date_preset(ws, "Today", port=port)
                        time.sleep(1.0)
                    print(f"  🛑 [إيقاف فوري للحملة أولاً على /aw/campaigns] '{c_viol}' -> {res_cp} ({c_rsn})", flush=True)
                    if "SUCCESS" in res_cp:
                        all_actions.append(f"🛑 Google Ads ({label}): إيقاف الحملة كاملة `{c_viol}` على `/aw/campaigns` — {c_rsn} ({res_cp})")
                select_date_preset(ws, "Today", port=port)
                nav_back_ads = f"https://ads.google.com/aw/ads?ocid={ocid}" if ocid else "https://ads.google.com/aw/ads"
                send_cdp(ws, "Page.navigate", {"url": nav_back_ads})
                time.sleep(4.5)
                keep_adspower_offscreen(port)

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

                # If an ad_name (e.g. c7, c9) is shared across multiple SPENDING campaigns (#2, #3, etc.),
                # apportion or zero out shared Sheet UTM sales when UI conversions show poor CPA,
                # so a losing campaign (like c7 at $54.95 / 2 UI conv = $27.49 CPA) NEVER hides behind shared UTM sales!
                if toggle != "Enabled" and max(sp_1d, sp_2d, sp_3d, sp_7d) < 0.50:
                    continue

                is_gulf_g = any(k in camp.lower() for k in ("عمان", "قطر", "كويت", "بحرين", "امارات", "oman", "qatar", "kuwait", "bahrain", "uae"))
                is_31_10_bad_camp = any(k in camp.lower() for k in ("31-10", "rescue 10 ads", "recycled 5ads"))
                # أمر المستخدم الصارم (2026-11-01): قاعدة غوغل أدز للفيديو تكون $6.00 بدون بيعة و $8.00 أقصى سعر بيعة (مش $4.00 - $6.00 لأن غوغل يتعلم، ولا يُرفع إلى غوغل إلا الفيديوهات الرابحة المجربة في سناب وتيك توك)
                max_cpa_g = 10.00 if is_gulf_g else 8.00  # $8.00 for Saudi campaigns, $10.00 for Gulf
                no_sale_g = 7.00 if is_gulf_g else 6.00   # $6.00 for Saudi campaigns, $7.00 for Gulf
                min_cpc_sp_g = 1.00
                max_cpc_lim_g = 0.35

                # If an ad_name (e.g. c7, c9, phk5) is shared across multiple SPENDING campaigns (3D spend > $2.50),
                # apportion Sheet UTM sales proportionally by spend across active campaigns (unless a campaign has 0 UI conv in 7D while others have UI conv > 0).
                same_ad_camps = [
                    k for k, v_k in ads_map.items()
                    if k[2].strip().lower() == ad_key
                    and (v_k["d0"]["spend"] + v_k["d1"]["spend"] + v_k["d2"]["spend"]) > 2.50
                ]
                is_shared_utm = len(same_ad_camps) > 1
                if is_shared_utm:
                    comb_sp_1d = sum(ads_map[k]["d0"]["spend"] for k in same_ad_camps)
                    comb_sp_2d = sum(ads_map[k]["d0"]["spend"] + ads_map[k]["d1"]["spend"] for k in same_ad_camps)
                    comb_sp_3d = sum(ads_map[k]["d0"]["spend"] + ads_map[k]["d1"]["spend"] + ads_map[k]["d2"]["spend"] for k in same_ad_camps)
                    comb_sp_7d = sum(max(ads_map[k]["d0"]["spend"] + ads_map[k]["d1"]["spend"] + ads_map[k]["d2"]["spend"], ads_map[k]["l7"]["spend"] + ads_map[k]["d0"]["spend"]) for k in same_ad_camps)
                    comb_ui_7d = sum(max(ads_map[k]["d0"].get("ui_conv", 0.0) + ads_map[k]["d1"].get("ui_conv", 0.0) + ads_map[k]["d2"].get("ui_conv", 0.0), ads_map[k]["l7"].get("ui_conv", 0.0) + ads_map[k]["d0"].get("ui_conv", 0.0)) for k in same_ad_camps)
                    if ui_7d == 0 and comb_ui_7d > 0 and sp_7d >= no_sale_g:
                        app_sh_1d = app_sh_2d = app_sh_3d = app_sh_7d = 0.0
                    else:
                        app_sh_1d = float(sh_1d) * (sp_1d / comb_sp_1d) if comb_sp_1d > 0 else (float(sh_1d) / len(same_ad_camps))
                        app_sh_2d = float(sh_2d) * (sp_2d / comb_sp_2d) if comb_sp_2d > 0 else (float(sh_2d) / len(same_ad_camps))
                        app_sh_3d = float(sh_3d) * (sp_3d / comb_sp_3d) if comb_sp_3d > 0 else (float(sh_3d) / len(same_ad_camps))
                        app_sh_7d = float(sh_7d) * (sp_7d / comb_sp_7d) if comb_sp_7d > 0 else (float(sh_7d) / len(same_ad_camps))
                else:
                    app_sh_1d, app_sh_2d, app_sh_3d, app_sh_7d = float(sh_1d), float(sh_2d), float(sh_3d), float(sh_7d)

                eff_1d = max(ui_1d, app_sh_1d)
                eff_2d = max(ui_2d, app_sh_2d)
                eff_3d = max(ui_3d, app_sh_3d)
                eff_7d = max(ui_7d, app_sh_7d)

                cpa_1d = round(sp_1d / eff_1d, 2) if eff_1d > 0 else None
                cpa_2d = round(sp_2d / eff_2d, 2) if eff_2d > 0 else None
                cpa_3d = round(sp_3d / eff_3d, 2) if eff_3d > 0 else None
                cpa_7d = round(sp_7d / eff_7d, 2) if eff_7d > 0 else None

                # 1000% CAREFUL EVALUATION FOR ENABLED & PAUSED WINNING ADS:
                decision = "⏸️ متوقف مسبقاً"
                pause_reason = None
                is_profitable_1d = eff_1d >= 1.0 and cpa_1d is not None and cpa_1d <= max_cpa_g
                is_profitable_2d = eff_2d >= 1.0 and cpa_2d is not None and cpa_2d <= max_cpa_g
                is_profitable_3d = eff_3d >= 1.0 and cpa_3d is not None and cpa_3d <= max_cpa_g

                if is_31_10_bad_camp:
                    pause_reason = "حملة 31-10 موقوفة بأمر المستخدم (زيارات رديئة رخيصة وCPA مرتفع)"
                elif camp in violating_20dollar_camps:
                    pause_reason = f"[قاعدة إيقاف الحملة عند $20.00] {violating_20dollar_camps[camp]}"
                elif eff_1d == 0 and ((d0["clicks"] >= 45 and sp_1d >= 1.80) or (sp_1d >= 2.20 and 0 < d0["cpc"] <= 0.06)):
                    pause_reason = f"زيارات رديئة رخيصة بدون مبيعات اليوم (نقرات={d0['clicks']} | CPC=${d0['cpc']:.2f} | صرف=${sp_1d:.2f} / 0 مبيعات)"
                elif sp_1d >= no_sale_g and eff_1d == 0:
                    pause_reason = f"صرف اليوم ${sp_1d:.2f} (>= ${no_sale_g:.2f}) بـ 0 مبيعات (في البيكسل والشيت)"
                elif eff_1d >= 1.0 and sp_1d >= max_cpa_g and cpa_1d is not None and cpa_1d > max_cpa_g:
                    pause_reason = f"CPA اليوم مرتفع ${cpa_1d:.2f} (> ${max_cpa_g:.2f}) [صرف ${sp_1d:.2f} / {eff_1d:.1f} مبيعات]"
                elif sp_2d >= no_sale_g and eff_2d == 0:
                    pause_reason = f"صرف يومين (2D) ${sp_2d:.2f} (>= ${no_sale_g:.2f}) بـ 0 مبيعات"
                elif eff_2d >= 1.0 and sp_2d >= max_cpa_g and cpa_2d is not None and cpa_2d > max_cpa_g:
                    pause_reason = f"CPA يومين (2D) مرتفع ${cpa_2d:.2f} (> ${max_cpa_g:.2f}) [صرف ${sp_2d:.2f} / {eff_2d:.1f} مبيعات]"
                elif sp_3d >= no_sale_g and eff_3d == 0:
                    pause_reason = f"صرف 3 أيام (3D) ${sp_3d:.2f} (>= ${no_sale_g:.2f}) بـ 0 مبيعات"
                elif eff_3d >= 1.0 and sp_3d >= max_cpa_g and cpa_3d is not None and cpa_3d > max_cpa_g:
                    pause_reason = f"CPA 3 أيام (3D) مرتفع ${cpa_3d:.2f} (> ${max_cpa_g:.2f}) [صرف ${sp_3d:.2f} / {eff_3d:.1f} مبيعات]"
                elif sp_7d >= no_sale_g and eff_7d == 0 and not (is_profitable_2d and is_profitable_3d):
                    pause_reason = f"صرف 7 أيام (7D) ${sp_7d:.2f} (>= ${no_sale_g:.2f}) بـ 0 مبيعات"
                elif eff_7d >= 1.0 and sp_7d >= max_cpa_g and cpa_7d is not None and cpa_7d > max_cpa_g:
                    pause_reason = f"CPA 7 أيام (7D) مرتفع ${cpa_7d:.2f} (> ${max_cpa_g:.2f}) [صرف ${sp_7d:.2f} / {eff_7d:.1f} مبيعات]"
                elif eff_1d == 0 and eff_2d == 0 and eff_3d == 0 and eff_7d == 0 and sp_1d >= min_cpc_sp_g and (d0["clicks"] == 0 or d0["cpc"] > max_cpc_lim_g):
                    pause_reason = f"CPC مرتفع اليوم (${d0['cpc']:.2f} > ${max_cpc_lim_g:.2f} / نقرات={d0['clicks']}) بعد صرف ${sp_1d:.2f} بـ 0 مبيعات"
                elif eff_1d == 0 and eff_2d == 0 and eff_3d == 0 and eff_7d == 0 and sp_2d >= min_cpc_sp_g and ((d0["clicks"] + d1["clicks"]) == 0 or (sp_2d / max(1, d0["clicks"] + d1["clicks"])) > max_cpc_lim_g):
                    pause_reason = f"CPC مرتفع في يومين ($2D=${sp_2d:.2f} / نقرات={d0['clicks']+d1['clicks']} > ${max_cpc_lim_g:.2f}) بـ 0 مبيعات"
                elif eff_1d == 0 and eff_2d == 0 and eff_3d == 0 and eff_7d == 0 and sp_3d >= min_cpc_sp_g and ((d0["clicks"] + d1["clicks"] + d2["clicks"]) == 0 or (sp_3d / max(1, d0["clicks"] + d1["clicks"] + d2["clicks"])) > max_cpc_lim_g):
                    pause_reason = f"CPC مرتفع في 3 أيام ($3D=${sp_3d:.2f} / نقرات={d0['clicks']+d1['clicks']+d2['clicks']} > ${max_cpc_lim_g:.2f}) بـ 0 مبيعات"
                elif ad_key == "ad 1" and sp_3d >= 1.00 and eff_3d == 0:
                    pause_reason = f"إعلان باسم عام (Ad 1) بدون كود UTM وصرف ${sp_3d:.2f} بـ 0 مبيعات"

                if toggle == "Enabled":
                    if pause_reason and not today_verified:
                        decision = f"⚠️ تم تخطي الإيقاف مؤقتاً لأن نافذة اليوم لم تتأكد ({date_d0[:25]})"
                        pause_reason = None
                    elif pause_reason:
                        res_p = set_google_ad_status_carefully(ws, camp, ad, "Pause", port=port, ag_name=ag)
                        if res_p == "NOT_FOUND":
                            select_date_preset(ws, "Yesterday", port=port)
                            res_p = set_google_ad_status_carefully(ws, camp, ad, "Pause", port=port, ag_name=ag)
                        if res_p == "NOT_FOUND":
                            select_date_preset(ws, "Last 7 days", port=port)
                            res_p = set_google_ad_status_carefully(ws, camp, ad, "Pause", port=port, ag_name=ag)
                        select_date_preset(ws, "Today", port=port)
                        decision = f"🛑 تم إيقافه فوراً ({res_p}) — {pause_reason}"
                        all_actions.append(f"🛑 Google Ads ({label}): إيقاف الإعلان `{ad}` في حملة `{camp}` ({ag}) — {pause_reason} ({res_p})")
                    else:
                        decision = "✅ رابح / آمن ومستمر"
                else:
                    is_active_modern_camp = any(tag in camp.lower() for tag in ("25-10", "26-10", "27-10", "28-10", "29-10", "30-10", "31-10", "01-11", "02-11", "manzilk"))
                    # Must be profitable in BOTH 2D and 3D (or 3D profitable with <no_sale_g spend in 2D) AND 7D CPA <= max_cpa_g, AND NOT in violating_20dollar_camps!
                    has_proven_winning_sales = (
                        pause_reason is None
                        and camp not in violating_20dollar_camps
                        and ad_key != "ad 1"
                        and is_profitable_3d
                        and (is_profitable_2d or sp_2d < no_sale_g)
                        and (cpa_7d is None or cpa_7d <= max_cpa_g)
                    )
                    if (
                        toggle == "Paused"
                        and is_active_modern_camp
                        and has_proven_winning_sales
                        and today_verified
                    ):
                        res_e = set_google_ad_status_carefully(ws, camp, ad, "Enable", port=port, ag_name=ag)
                        if res_e == "NOT_FOUND":
                            select_date_preset(ws, "Yesterday", port=port)
                            res_e = set_google_ad_status_carefully(ws, camp, ad, "Enable", port=port, ag_name=ag)
                        if res_e == "NOT_FOUND":
                            select_date_preset(ws, "Last 7 days", port=port)
                            res_e = set_google_ad_status_carefully(ws, camp, ad, "Enable", port=port, ag_name=ag)
                        select_date_preset(ws, "Today", port=port)
                        if "SUCCESS" in str(res_e):
                            best_cpa_str = f"2D CPA=${cpa_2d} | 3D CPA=${cpa_3d}" if cpa_2d else f"3D CPA=${cpa_3d}"
                            decision = f"🚀 تم إعادة تفعيله تلقائياً ({res_e}) — إعلان رابح ({best_cpa_str})"
                            all_actions.append(f"🚀 Google Ads ({label}): إعادة تفعيل الإعلان الرابح `{ad}` في حملة `{camp}` ({ag}) — {best_cpa_str} ({res_e})")

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

    lines.extend(["", "## 🌐 جدول تفصيص Google Ads عبر النوافذ (`1D / 3D` — حد أقصى 5 أعمدة)"])
    for acc in g_res.get("accounts", []):
        lines.append(f"### {acc['label']}")
        lines.append("| الحالة والحملة | الإعلان | اليوم (1D) | آخر 3 أيام (3D) | القرار |")
        lines.append("|---|---|---|---|---|")
        for r in acc.get("ads", []):
            lines.append(f"| `{r['toggle']}` — `{r['camp']}` | **`{r['ad']}`** | {r['1d']} | {r['3d']} | {r['decision']} |")
        lines.append("")

    if g_res.get("accounts") or not REPORT_FILE.exists():
        REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n✅ اكتمل الفحص الشامل وحُفظ التقرير في: {REPORT_FILE}", flush=True)
    else:
        print(f"\nℹ️ تم الاحتفاظ بآخر تقرير Google Ads كامل في: {REPORT_FILE} (أثناء تفعيل قفل المتصفح المؤقت).", flush=True)
    if combined_actions and os.environ.get("GITHUB_ACTIONS") != "true":
        try:
            from github_deploy import sync_state_and_rules_to_github
            sync_state_and_rules_to_github("deep_multiwindow_account_auditor")
        except Exception as _sync_err:
            print(f"ℹ️ ملاحظة مزامنة GitHub من deep_multiwindow_account_auditor: {_sync_err}", flush=True)


if __name__ == "__main__":
    main()
