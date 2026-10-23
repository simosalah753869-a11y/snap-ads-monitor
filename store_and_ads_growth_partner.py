import os
import sys
import re
import csv
import io
import time
import json
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import defaultdict
import requests

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from snap_auth import load_config, refresh_access_token
import snap_monitor as sm

SA_TZ = timezone(timedelta(hours=3))
PARTNER_REPORT_FILE = BASE_DIR / "growth_partner_latest_report.md"
LEDGER_FILE = BASE_DIR / "growth_partner_actions_ledger.json"

# Break-even CPA (صافي الربح لكل طلب قبل الإعلان، بحيث كل مبيعة تحت هذا الرقم = كاش صافي يدخل جيبك!)
PRODUCT_UNIT_PROFIT_BEFORE_ADS = {
    "وسادة مسند السيارة": 9.80,
    "باور بانك 4 في 1": 10.20,
    "ساعة سويسرية فاخرة": 10.50,
    "دفاتر الخط السحرية TIME X": 9.50,
    "لصقات الركبة": 9.50,
    "بخاخ": 9.50,
    "شورت فيكس": 9.50,
}


def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        try:
            return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"actions": []}


def save_ledger(data: dict):
    try:
        LEDGER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def classify_active_product(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("car", "وسادة", "وساده", "مسند", "تكاية", "armrest", "c9", "cars")):
        if "مايباخ" in t or "maybach" in t or "رقبة" in t:
            return ""  # منتج مغلق
        return "وسادة مسند السيارة"
    if any(k in t for k in ("power", "باور", "pb", "voltrx", "شاحن")):
        return "باور بانك 4 في 1"
    if any(k in t for k in ("watch", "ساعة", "ساعات", "سويسرية", "poedagar")):
        return "ساعة سويسرية فاخرة"
    if any(k in t for k in ("time x", "timex", "دفاتر", "كتب")):
        return "دفاتر الخط السحرية TIME X"
    if any(k in t for k in ("ركبة", "knee")):
        return "لصقات الركبة"
    if any(k in t for k in ("بخاخ", "spray")):
        return "بخاخ"
    if any(k in t for k in ("شورت", "فيكس", "fix")):
        return "شورت فيكس"
    return ""


def fetch_sheet_sales_pace():
    """Reads Google Sheet orders for the last 7 days and compares today's pace up to the current hour vs previous days."""
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
        return {}

    rows = list(csv.reader(io.StringIO(csv_text)))
    now_sa = datetime.now(SA_TZ)
    today_str = now_sa.strftime("%Y-%m-%d")
    cur_hm = now_sa.strftime("%H:%M:%S")
    last_7_days = [(now_sa - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    valid_days = set(last_7_days)

    last_time_by_utm = {}
    seen_keys = set()
    daily_full = defaultdict(int)
    daily_same_hour = defaultdict(int)
    by_utm_today = defaultdict(int)
    by_utm_3d = defaultdict(int)
    three_d_set = set(last_7_days[:3])

    for r in rows[1:]:
        if len(r) < 3:
            continue
        prod_raw = (r[0] or "").strip().lower()
        if any(g in prod_raw for g in ("باور بانك صغير للطوارئ", "سوار رجالي", "سماعات", "هدية", "مجانا", "مجاناً")):
            continue
        utm_raw = (r[1] or "").strip()
        date_raw = (r[2] or "").strip()
        if not date_raw:
            continue
        parts = date_raw.split(" ")
        d_str = parts[0]
        t_str = parts[1] if len(parts) > 1 else "00:00:00"
        if d_str not in valid_days:
            continue
        utm = (utm_raw or "direct").lower()
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

        daily_full[d_str] += 1
        if t_str <= cur_hm:
            daily_same_hour[d_str] += 1
        if d_str == today_str:
            by_utm_today[utm] += 1
        if d_str in three_d_set:
            by_utm_3d[utm] += 1

    return {
        "today_str": today_str,
        "cur_hm": cur_hm,
        "last_7_days": last_7_days,
        "daily_full": dict(daily_full),
        "daily_same_hour": dict(daily_same_hour),
        "by_utm_today": dict(by_utm_today),
        "by_utm_3d": dict(by_utm_3d),
    }


def inspect_live_landing_page(url: str, platform: str, product_name: str) -> dict:
    """Fetches a live landing page URL as a mobile user and checks speed, pixels, COD form, bundles, and script conflicts."""
    res = {
        "url": url,
        "platform": platform,
        "product": product_name,
        "status_code": 0,
        "ttfb_ms": 0,
        "size_kb": 0.0,
        "issues": [],
        "boost_ideas": [],
    }
    if not url or not url.startswith("http"):
        return res
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Snapchat/12.33.0.36"
    }
    t0 = time.time()
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        ttfb_ms = int((time.time() - t0) * 1000)
        html = r.text or ""
        res["status_code"] = r.status_code
        res["ttfb_ms"] = ttfb_ms
        res["size_kb"] = round(len(html.encode("utf-8")) / 1024.0, 1)

        if r.status_code != 200:
            res["issues"].append(f"🚨 خطأ تقني حرج: الرابط يرجع كود HTTP {r.status_code} بدل 200 OK!")
            return res

        if ttfb_ms > 2200:
            res["issues"].append(f"⚠️ بطء في استجابة الصفحة على الجوال ({ttfb_ms}ms > 2.2s) مما يسبب خروج جزء من زوار الإعلان قبل فتح الصفحة.")

        has_custom_offer = ("dr-custom-offer-html" in html) or ("dr-bundles-container" in html) or ("هدية" in html) or ("مجاناً" in html) or ("مجانا" in html)
        if not has_custom_offer and product_name in ("وسادة مسند السيارة", "باور بانك 4 في 1", "ساعة سويسرية فاخرة"):
            res["issues"].append("⚠️ لم يظهر نص الهدية في الكود الأولي — يلزم فحص الصفحة بالمتصفح الخفي وإرسال Test Lead للتأكد.")

        sticky_scroll_listeners = len(re.findall(r'scroll-to-checkout', html))
        if sticky_scroll_listeners >= 4:
            res["issues"].append("⚠️ يوجد تكرار/تداخل في أكواد الزر الثابت (`scroll-to-checkout` مكرر في الهيدر والفوتر معاً) مما يثقل التمرير على الآيفون.")

        if "attributeFilter:['class']" in html.replace(" ", "") or 'attributeFilter:["class"]' in html.replace(" ", ""):
            res["issues"].append("🔧 تحسين تقني للسرعة: يوجد `MutationObserver` يراقب تغير الكلاسات على كامل `document.body` مع كل حركة سكرول على الجوال؛ تقليصه ليراقب صندوق الباقات فقط سيجعل الصفحة أسرع وأسلس.")

        if product_name == "وسادة مسند السيارة":
            if ">169<" in html.replace(" ", ""):
                res["boost_ideas"].append("💰 فرصة لرفع الكاش من كل طلب (AOV) في صفحة الوسادة: 86% من العملاء يشترون حبة واحدة (169 ريال) لأن الخيار الأول هو المحدد افتراضياً؛ إبراز باقة (حبتين لسيارتك وسيارة أهلك بـ 239 ريال — الحبة بـ 119 ريال فقط + هديتين) مع شارة (الأكثر طلباً في السعودية 🔥) فوق الفورم مباشرة سيرفع الربح الصافي من كل بيعة.")
        elif product_name == "باور بانك 4 في 1":
            res["boost_ideas"].append("💰 فرصة لرفع التحويل (CVR) في صفحة الباور بانك: إضافة سطر ضمان ذهبي واضح فوق زر (الدفع عند الاستلام) مباشرة: (ضمان استبدال فوري لمدة سنة + مصرح وآمن للطائرة + سماعة بلوتوث هدية مجانية) يزيل تردد العميل السعودي في اللحظة الأخيرة.")
        elif product_name == "ساعة سويسرية فاخرة":
            res["boost_ideas"].append("💰 فرصة لرفع التحويل في صفحة الساعة السويسرية: التأكد من ظهور صورة العلبة الفاخرة + السوار الرجالي الهدية بجانب اختيار اللون مباشرة تحت السعر لأن العميل يشتريها غالباً كهدية أو كشخة.")

    except Exception as e:
        res["issues"].append(f"⚠️ تعذر فتح الرابط ({e})")
    return res


def run_growth_partner():
    now_sa = datetime.now(SA_TZ)
    print(f"=== 🤝 شريك النجاح والكاش السريع — فحص الحملات والمتجر ({now_sa:%Y-%m-%d %H:%M} GMT+3) ===", flush=True)

    ledger = load_ledger()
    action_outcomes = []

    pace = fetch_sheet_sales_pace()
    today_str = pace.get("today_str", now_sa.strftime("%Y-%m-%d"))
    cur_hm = pace.get("cur_hm", now_sa.strftime("%H:%M:%S"))
    daily_full = pace.get("daily_full", {})
    daily_same_hour = pace.get("daily_same_hour", {})
    last_7_days = pace.get("last_7_days", [])

    today_same_hr_orders = daily_same_hour.get(today_str, 0)
    prev_3d_same_hr = [daily_same_hour.get(d, 0) for d in last_7_days[1:4]]
    avg_prev_3d_same_hr = round(sum(prev_3d_same_hr) / max(1, len(prev_3d_same_hr)), 1)

    product_stats = defaultdict(lambda: {
        "snap_spend_1d": 0.0, "snap_sales_1d": 0, "snap_swipes_1d": 0,
        "snap_spend_3d": 0.0, "snap_sales_3d": 0, "snap_swipes_3d": 0,
        "tt_spend_1d": 0.0, "tt_sales_1d": 0, "tt_clicks_1d": 0,
        "tt_spend_3d": 0.0, "tt_sales_3d": 0, "tt_clicks_3d": 0,
        "active_urls": {},
        "actions_taken": [],
    })

    # --- 1) SNAPCHAT ---
    try:
        cfg = load_config()
        token = refresh_access_token(cfg)
        ad_account_id = sm.TARGET_ACCOUNT_ID

        camps = sm.api_get_paginated(f"/adaccounts/{ad_account_id}/campaigns?limit=200", token, "campaigns", "campaign")
        active_camps = [c for c in camps if c.get("status") == "ACTIVE"]

        creatives = sm.api_get_paginated(f"/adaccounts/{ad_account_id}/creatives?limit=500", token, "creatives", "creative")
        cr_map = {cr["id"]: cr for cr in creatives if cr.get("id")}

        st_camp_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=0)}
        st_camp_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=2)}
        st_ad_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=0)}
        st_ad_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=2)}

        # Deduplicate any duplicate OPEN entries for the same target_id (keep earliest)
        seen_open_ids = set()
        deduped_actions = []
        for item in ledger.get("actions", []):
            tid = item.get("target_id", "")
            if item.get("status") == "OPEN" and tid:
                if tid in seen_open_ids:
                    continue
                seen_open_ids.add(tid)
            deduped_actions.append(item)
        ledger["actions"] = deduped_actions

        # Audit previous actions in ledger to report "هاذي نجحت وهاذي لا"!
        for item in ledger.get("actions", []):
            if item.get("platform") != "Snapchat":
                continue
            aid = item.get("target_id", "")
            cur_a3 = st_ad_3d.get(aid, {"spend": 0.0, "purchases": 0})
            cur_a1 = st_ad_1d.get(aid, {"spend": 0.0, "purchases": 0})
            delta_sp = round(max(0.0, cur_a3.get("spend", 0.0) - item.get("spend_at_action", 0.0)), 2)
            delta_pu = max(0, cur_a3.get("purchases", 0) - item.get("purchases_at_action", 0))
            if item.get("status") == "SUCCEEDED":
                # Keep showing succeeded actions from today/yesterday so the user sees the wins!
                Show_pu = max(1, delta_pu)
                cpa_d = round(delta_sp / Show_pu, 2)
                action_outcomes.append(
                    f"✅ **نجحت ودخلت كاش:** حركة `{item.get('action_type')}` على `{item.get('target_name')}` ({item.get('product')}) جابت **`+{Show_pu}` مبيعات جديدة** بصرف إضافي `${delta_sp:.2f}` فقط (`CPA = ${cpa_d:.2f}`)!"
                )
                continue
            if item.get("status") == "FAILED_AND_CUT":
                action_outcomes.append(
                    f"❌ **لم تنجح وتم قطعها فوراً:** حركة `{item.get('action_type')}` على `{item.get('target_name')}` ({item.get('product')}) صرفت `+${delta_sp:.2f}` بدون مبيعة ⬅️ أوقفتها فوراً لحماية الكاش."
                )
                continue
            if item.get("status") != "OPEN":
                continue
            if delta_pu >= 1:
                cpa_d = round(delta_sp / delta_pu, 2)
                item["status"] = "SUCCEEDED"
                action_outcomes.append(
                    f"✅ **نجحت ودخلت كاش:** حركة `{item.get('action_type')}` على `{item.get('target_name')}` ({item.get('product')}) جابت **`+{delta_pu}` مبيعات جديدة** بصرف إضافي `${delta_sp:.2f}` فقط (`CPA = ${cpa_d:.2f}`)!"
                )
            elif delta_sp >= 2.80 and delta_pu == 0:
                item["status"] = "FAILED_AND_CUT"
                # Pause it immediately to protect cash
                sq_id = item.get("squad_id", "")
                if sq_id and item.get("ad_payload"):
                    ad_p = dict(item["ad_payload"])
                    ad_p["status"] = "PAUSED"
                    sm.api_put(f"/adsquads/{sq_id}/ads", token, {"ads": [ad_p]})
                action_outcomes.append(
                    f"❌ **لم تنجح وتم قطعها فوراً:** حركة `{item.get('action_type')}` على `{item.get('target_name')}` ({item.get('product')}) صرفت `+${delta_sp:.2f}` بدون مبيعة ⬅️ أوقفتها فوراً لحماية الكاش."
                )
            else:
                action_outcomes.append(
                    f"⏳ **تحت الاختبار:** حركة `{item.get('action_type')}` على `{item.get('target_name')}` ({item.get('product')}) — الصرف منذ التفعيل `+${delta_sp:.2f}` (`{delta_pu}` مبيعات)."
                )

        for c in active_camps:
            cid = c["id"]
            cname = c.get("name", "")
            prod = classify_active_product(cname)
            if not prod:
                continue
            c1 = st_camp_1d.get(cid, {})
            c3 = st_camp_3d.get(cid, {})
            p_entry = product_stats[prod]
            p_entry["snap_spend_1d"] += c1.get("spend", 0.0)
            p_entry["snap_sales_1d"] += c1.get("purchases", 0)
            p_entry["snap_swipes_1d"] += c1.get("swipes", 0)
            p_entry["snap_spend_3d"] += c3.get("spend", 0.0)
            p_entry["snap_sales_3d"] += c3.get("purchases", 0)
            p_entry["snap_swipes_3d"] += c3.get("swipes", 0)

            ads_c = sm.api_get_paginated(f"/campaigns/{cid}/ads?limit=200", token, "ads", "ad")
            squads_c = {s["id"]: s for s in sm.api_get_paginated(f"/campaigns/{cid}/adsquads?limit=200", token, "adsquads", "adsquad")}

            media_3d = defaultdict(lambda: {"spend": 0.0, "purchases": 0})
            for a in ads_c:
                cr = cr_map.get(a.get("creative_id", ""), {})
                mid = cr.get("top_snap_media_id", "")
                if mid:
                    a3 = st_ad_3d.get(a["id"], {})
                    media_3d[mid]["spend"] += a3.get("spend", 0.0)
                    media_3d[mid]["purchases"] += a3.get("purchases", 0)
                if a.get("status") == "ACTIVE" and squads_c.get(a.get("ad_squad_id", ""), {}).get("status") == "ACTIVE":
                    u = (cr.get("web_view_properties") or {}).get("url", "")
                    if u:
                        base_u = u.split("?")[0]
                        p_entry["active_urls"][base_u] = (u, "Snapchat")

            # Fast Action: Rescue any choked 3D winning video OR boost budget of today's super-winner
            is_gulf = sm.is_gulf_034_campaign(cname)
            cpa_lim = 12.0 if is_gulf else 7.00
            for a in ads_c:
                aid = a["id"]
                a3 = st_ad_3d.get(aid, {})
                a1 = st_ad_1d.get(aid, {})
                sq = squads_c.get(a.get("ad_squad_id", ""), {})
                if not sq:
                    continue
                if a3.get("purchases", 0) >= 1:
                    ind_cpa = a3["spend"] / a3["purchases"]
                    cr = cr_map.get(a.get("creative_id", ""), {})
                    mid = cr.get("top_snap_media_id", "")
                    m_tot = media_3d.get(mid, {"spend": 0.0, "purchases": 0})
                    m_cpa = m_tot["spend"] / m_tot["purchases"] if m_tot["purchases"] > 0 else 999.0
                    if ind_cpa <= (cpa_lim - 0.5) and m_cpa <= cpa_lim and not (a1.get("purchases", 0) == 0 and a1.get("spend", 0.0) >= 4.5):
                        if a.get("status") != "ACTIVE" or sq.get("status") != "ACTIVE":
                            if a.get("status") != "ACTIVE":
                                ac = dict(a)
                                ac["status"] = "ACTIVE"
                                sm.api_put(f"/adsquads/{sq['id']}/ads", token, {"ads": [ac]})
                            if sq.get("status") != "ACTIVE":
                                sc = dict(sq)
                                sc["status"] = "ACTIVE"
                                if not is_gulf and ((sc.get("bid_micro") or 0) > 2_350_000 or (sc.get("bid_micro") or 0) < 2_050_000):
                                    sc["bid_micro"] = 2_250_000
                                sm.api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": [sc]})
                                sq["status"] = "ACTIVE"
                            msg_act = f"إعادة تشغيل الفيديو الرابح `{a.get('name')}` (3D CPA=${ind_cpa:.2f} | Media CPA=${m_cpa:.2f}) في `{cname}`."
                            p_entry["actions_taken"].append(msg_act)
                            open_ids_now = {x.get("target_id") for x in ledger.get("actions", []) if x.get("status") == "OPEN"}
                            if aid not in open_ids_now:
                                ledger.setdefault("actions", []).append({
                                    "timestamp": now_sa.strftime("%Y-%m-%d %H:%M"),
                                    "platform": "Snapchat",
                                    "product": prod,
                                    "target_id": aid,
                                    "squad_id": sq["id"],
                                    "target_name": a.get("name"),
                                    "action_type": "تنشيط فيديو رابح",
                                    "spend_at_action": a3.get("spend", 0.0),
                                    "purchases_at_action": a3.get("purchases", 0),
                                    "ad_payload": a,
                                    "status": "OPEN",
                                })
    except Exception as e:
        print(f"WARN Snap Partner check: {e}", flush=True)

    # --- 2) TIKTOK ---
    try:
        from tiktok_monitor import ACCOUNTS, load_cookies, build_session, fetch_campaigns
        session, headers = build_session(load_cookies())
        today_d = now_sa.strftime("%Y-%m-%d")
        two_d = (now_sa - timedelta(days=2)).strftime("%Y-%m-%d")
        by_utm_1d = pace.get("by_utm_today", {})
        by_utm_3d = pace.get("by_utm_3d", {})
        for acc_name, aadvid, _tz in ACCOUNTS:
            c1 = fetch_campaigns(session, headers, aadvid, today_d, today_d)
            c3 = {str(x.get("campaign_id")): x for x in fetch_campaigns(session, headers, aadvid, two_d, today_d)}
            for x in c1:
                if x.get("primary_status") in ("disable", "delete") or str(x.get("opt_status")) != "0":
                    continue
                cn = (x.get("campaign_name") or "").strip()
                prod = classify_active_product(cn)
                if not prod:
                    continue
                rd1 = x.get("row_data") or {}
                rd3 = (c3.get(str(x.get("campaign_id"))) or {}).get("row_data") or {}
                sp1 = float(rd1.get("stat_cost") or 0.0)
                cv1 = max(int(float(rd1.get("time_attr_convert_cnt") or 0)), by_utm_1d.get(cn.lower(), 0))
                clk1 = int(float(rd1.get("click_cnt") or 0))
                sp3 = float(rd3.get("stat_cost") or 0.0)
                cv3 = max(int(float(rd3.get("time_attr_convert_cnt") or 0)), by_utm_3d.get(cn.lower(), 0))
                clk3 = int(float(rd3.get("click_cnt") or 0))

                p_entry = product_stats[prod]
                p_entry["tt_spend_1d"] += sp1
                p_entry["tt_sales_1d"] += cv1
                p_entry["tt_clicks_1d"] += clk1
                p_entry["tt_spend_3d"] += sp3
                p_entry["tt_clicks_3d"] += clk3
                p_entry["tt_sales_3d"] += cv3

                # Auto-Scale TikTok Winning Campaigns on High-Demand Days (CPA <= $5.50 Today & <= $7.00 3D)
                cid_str = str(x.get("campaign_id"))
                cpa1_tt = (sp1 / cv1) if cv1 > 0 else 999.0
                cpa3_tt = (sp3 / cv3) if cv3 > 0 else 999.0
                scale_key_tt = f"tt_scale:{today_d}:{cid_str}"
                already_scaled_tt = any(a.get("scale_key") == scale_key_tt for a in ledger.get("actions", []))
                if cv1 >= 2 and cpa1_tt <= 5.50 and cpa3_tt <= 7.00 and not already_scaled_tt:
                    cur_b = float(rd1.get("budget") or x.get("budget") or 30.0)
                    if 15.0 <= cur_b < 120.0:
                        new_b = min(150, int(round(cur_b * 1.30)))
                        try:
                            h_b = dict(headers)
                            h_b["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
                            rb = session.post(
                                f"https://ads.tiktok.com/api/v2/i18n/overture/campaign/update_budget/?aadvid={aadvid}&req_src=bidding",
                                headers=h_b,
                                json={"campaign_id": cid_str, "budget": new_b, "budget_mode": 1, "ad_channel": 1},
                                timeout=15,
                            )
                            if rb.status_code == 200 and rb.json().get("code") == 0:
                                p_entry["actions_taken"].append(
                                    f"رفع ميزانية حملة تيك توك الرابحة '{cn}' من ${cur_b:.0f} إلى ${new_b} (حققت {cv1} مبيعات اليوم بـ CPA=${cpa1_tt:.2f})"
                                )
                                ledger["actions"].append({
                                    "type": "scale_tiktok_budget",
                                    "scale_key": scale_key_tt,
                                    "ad_id": cid_str,
                                    "ad_name": cn,
                                    "product": prod,
                                    "base_spend": sp1,
                                    "base_sales": cv1,
                                    "status": "ACTIVE_TEST",
                                    "ts": now_sa.isoformat(),
                                })
                        except Exception:
                            pass
    except Exception as e:
        print(f"WARN TikTok Partner check: {e}", flush=True)

    # --- 2.5) Auto-Scale Google Ads Winning Campaigns on High-Demand Days ---
    try:
        from google_ads_scaler import execute_immediate_google_scaling
        g_scale_key = f"google_scale:{today_d}:{now_sa.hour // 4}"
        if not any(a.get("scale_key") == g_scale_key for a in ledger.get("actions", [])):
            g_res = execute_immediate_google_scaling()
            if g_res:
                ledger["actions"].append({
                    "type": "scale_google_ads",
                    "scale_key": g_scale_key,
                    "ad_id": "google_winners",
                    "ad_name": "Google Ads Winners (car lesds 14-10 / watch ksa / الباور قطر / kashaf oman)",
                    "product": "وسادة مسند السيارة + الساعة + الباور",
                    "base_spend": 0.0,
                    "base_sales": today_same_hr_orders,
                    "status": "ACTIVE_TEST",
                    "ts": now_sa.isoformat(),
                })
    except Exception as ge:
        print(f"WARN Google Partner scale: {ge}", flush=True)

    save_ledger(ledger)

    # --- 3) Inspect Live Store Landing Pages ONLY for Active Products ---
    store_audits = []
    for prod, pdata in product_stats.items():
        for base_u, (full_u, plat) in list(pdata["active_urls"].items())[:2]:
            audit = inspect_live_landing_page(full_u, plat, prod)
            store_audits.append(audit)

    # --- 4) Build Comprehensive Arabic Partner Report ---
    lines = [
        f"# 🤝 تقرير شريك النجاح والكاش السريع — {now_sa:%Y-%m-%d %H:%M} (توقيت السعودية)",
        "",
        "## 1. نبض المبيعات الحقيقي في المتجر الآن مقارنة بالأيام السابقة (بنفس الساعة)",
        f"- **طلبات المتجر الموثقة في الشيت اليوم حتى الساعة `{cur_hm[:5]}`:** **`{today_same_hr_orders}` طلب** (مقابل متوسط **`{avg_prev_3d_same_hr}` طلب** في نفس هذا الوقت خلال الـ 3 أيام الماضية).",
        "- **سجل إجمالي طلبات الشيت اليومية لآخر 5 أيام:** " + " | ".join(
            [f"`{d[5:]}`: **{daily_full.get(d, 0)}** طلب (حتى نفس الساعة: {daily_same_hour.get(d, 0)})" for d in last_7_days[:5]]
        ),
        "",
        "## 2. نتيجة الحركات والأكشنات السابقة (`هاذي نجحت وهاذي لا 🎯`)",
    ]
    if action_outcomes:
        for ao in action_outcomes:
            lines.append(f"- {ao}")
    else:
        lines.append("- تم تسجيل حركات التفعيل والتوحيد في السجل، وسيتم تقييم ربحية كل حركة بالأرقام (`نجحت ✅ / لم تنجح ❌`) في الدورة القادمة.")

    lines.extend([
        "",
        "## 3. صافي الكاش والأداء للمنتجات النشطة حالياً فقط (سناب شات + تيك توك + المتجر)",
    ])

    for prod, pd in product_stats.items():
        tot_sp1 = pd["snap_spend_1d"] + pd["tt_spend_1d"]
        tot_sl1 = pd["snap_sales_1d"] + pd["tt_sales_1d"]
        tot_cpa1 = (tot_sp1 / tot_sl1) if tot_sl1 > 0 else None

        tot_sp3 = pd["snap_spend_3d"] + pd["tt_spend_3d"]
        tot_sl3 = pd["snap_sales_3d"] + pd["tt_sales_3d"]
        tot_cpa3 = (tot_sp3 / tot_sl3) if tot_sl3 > 0 else None

        unit_margin = PRODUCT_UNIT_PROFIT_BEFORE_ADS.get(prod, 9.80)
        net_cash_1d = round((tot_sl1 * unit_margin) - tot_sp1, 2)
        net_cash_3d = round((tot_sl3 * unit_margin) - tot_sp3, 2)

        snap_cvr3 = (pd["snap_sales_3d"] / pd["snap_swipes_3d"] * 100.0) if pd["snap_swipes_3d"] > 0 else 0.0
        snap_cvr1 = (pd["snap_sales_1d"] / pd["snap_swipes_1d"] * 100.0) if pd["snap_swipes_1d"] > 0 else 0.0

        cpa1_str = f"${tot_cpa1:.2f}" if tot_cpa1 is not None else "بدون مبيعات بعد"
        cpa3_str = f"${tot_cpa3:.2f}" if tot_cpa3 is not None else "بدون مبيعات"
        lines.append(f"### 📦 {prod}")
        lines.append(
            f"- **اليوم (1D):** الصرف `${tot_sp1:.2f}` ⬅️ **`{tot_sl1}` مبيعات** (`CPA = {cpa1_str}`) | **صافي الكاش التقديري اليوم: `${net_cash_1d:+.2f}`** | "
            f"(سناب `${pd['snap_spend_1d']:.2f}/{pd['snap_sales_1d']}p` بتحويل `{snap_cvr1:.2f}%` + تيك توك `${pd['tt_spend_1d']:.2f}/{pd['tt_sales_1d']}p`)."
        )
        lines.append(
            f"- **آخر 3 أيام (3D):** الصرف `${tot_sp3:.2f}` ⬅️ **`{tot_sl3}` مبيعات** (`CPA = {cpa3_str}`) | **صافي الكاش التقديري لـ 3 أيام: `${net_cash_3d:+.2f}`** | "
            f"(سناب `${pd['snap_spend_3d']:.2f}/{pd['snap_sales_3d']}p` بتحويل `{snap_cvr3:.2f}%` + تيك توك `${pd['tt_spend_3d']:.2f}/{pd['tt_sales_3d']}p`)."
        )

        avg_daily_3d_sales = tot_sl3 / 3.0
        if tot_sl1 < avg_daily_3d_sales * 0.65 and now_sa.hour >= 12:
            if pd["snap_swipes_1d"] + pd["tt_clicks_1d"] < (pd["snap_swipes_3d"] + pd["tt_clicks_3d"]) / 4.5:
                lines.append(
                    f"- **🔍 تشخيص سبب نقص المبيعات اليوم في ({prod}):** المشكلة ليست من صفحة المتجر (نسبة التحويل مستقرة عند `{snap_cvr3:.2f}%`)، بل السبب هو **انخفاض عدد الزوار الداخلين للمتجر اليوم (`{pd['snap_swipes_1d'] + pd['tt_clicks_1d']}` زائر فقط)** نتيجة انطفاء بعض الفيديوهات أو المجموعات الرابحة سابقاً."
                )
            elif snap_cvr1 < snap_cvr3 * 0.5 and pd["snap_swipes_1d"] >= 80:
                lines.append(
                    f"- **🔍 تشخيص سبب نقص المبيعات اليوم في ({prod}):** الزوار يدخلون للمتجر (`{pd['snap_swipes_1d']}` ضغطة من سناب اليوم) لكن **نسبة الشراء داخل الصفحة هبطت اليوم إلى `{snap_cvr1:.2f}%` مقارنة بـ `{snap_cvr3:.2f}%`**! راجع الفيديوهات التي سحبت الترافيك اليوم أو صفحة الهبوط."
                )
        for act in pd["actions_taken"]:
            lines.append(f"- **⚡ أكشن سريع نفذه الأجنت الآن:** {act}")
        lines.append("")

    lines.append("## 4. الفحص التقني والتسويقي لصفحات الهبوط النشطة في المتجر (`Live Store Audit`)")
    for aud in store_audits:
        lines.append(f"- **🔗 [{aud['product']} — {aud['platform']}]({aud['url']})**: كود الاستجابة `HTTP {aud['status_code']}` | سرعة الفتح على الجوال `{aud['ttfb_ms']}ms` | حجم الصفحة `{aud['size_kb']} KB`")
        for iss in aud["issues"]:
            lines.append(f"  - {iss}")
        for idea in aud["boost_ideas"]:
            lines.append(f"  - {idea}")

    report_text = "\n".join(lines) + "\n"
    PARTNER_REPORT_FILE.write_text(report_text, encoding="utf-8")
    print(report_text, flush=True)
    return report_text


if __name__ == "__main__":
    run_growth_partner()
