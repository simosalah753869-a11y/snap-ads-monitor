"""
24/7 Facebook Ads Automated Monitor & Stop-Loss Engine (GitHub Actions + Local Daemon)
Monitors all 4 Facebook Ad Accounts via US Residential ISP Proxy Tunnel + Graph API v21.0:
  - act_1164514299337955 (DarAlRimal_2611)
  - act_4504761856414520 (DarAlRimal_1208)
  - act_1135591735634568 (DarAlRimal_BM2_2)
  - act_2031165734260288 (Isabel Van Der Sluis - USA)

Rules Enforced Every 20 Minutes (Updated 2026-11-05 per User Directive):
  1. Ad / Video Level Rules (Evaluated on Video Cumulative Stats):
     - Single Video Cumulative No-Sale Stop-Loss ($9.00): Any Ad / Video whose cumulative spend reaches >= $9.00 with 0 sales -> Pause Ad immediately.
     - Single Video Cumulative High CPA Cut ($8.00): Any Ad / Video with cumulative Sales >= 1 and cumulative CPA >= $8.00 (Saudi) / >= $12.00 (Gulf) -> Pause Ad immediately.
  2. Campaign Level Rules:
     - Banned Products (kashaf / warmer / maybach): Pause immediately.
     - Dead Campaign (0 active ads): Pause immediately.
     - Campaign No-Sale Stop-Loss ($20.00): Any Campaign that spends >= $20.00 (1D / 3D / cumulative) with 0 sales -> Pause Campaign immediately.
     - Campaign High CPA Cut ($20.00): Any Campaign with Sales >= 1 and CPA >= $20.00 (1D / 3D / cumulative) -> Pause Campaign immediately.
"""
import csv
import io
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import facebook_ads_controller as fb

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
HISTORY_FILE = BASE_DIR / "paused_history.json"
SNAPSHOT_FILE = BASE_DIR / "fb_live_spend_snapshot.json"
SA_TZ = timezone(timedelta(hours=3))

# Core Facebook Stop-Loss Thresholds (Updated 2026-11-06 per User Directive:
# - Video: Cumulative CPA >= $8.00 | Cumulative No-Sale >= $9.00 | High CPC Cut (> $0.27 after $1.00 with 0 sales)
# - Campaign: Spend >= $15.00-$20.00 with 0 sales OR CPA >= $20.00; never pause a new campaign early at < $15.00 spend)
AD_CPC_MIN_SPEND = 1.00              # فحص سعر النقرة بعد صرف $1.00 بدون بيع
AD_MAX_CPC_NO_SALE = 0.27            # إيقاف الفيديو عند ارتفاع CPC > $0.27 بعد $1.00 بدون مبيعة (زي ما نعمل كل مرة)
AD_NO_SALE_CUMULATIVE_MAX = 9.00     # أي فيديو يوصل التراكمي الخاص به >= $9.00 بدون مبيعة يطفيه فوراً
CAMP_MIN_SPEND_BEFORE_PAUSE = 15.00  # عدم إيقاف أي حملة جديدة مبكراً قبل أن تصرف على الأقل $15.00 بدون مبيعة
CAMP_NO_SALE_CUMULATIVE_MAX = 20.00  # أي حملة تصرف >= $20.00 (أو >= $15.00 للحملات الجديدة بدون إعلانات) بدون مبيعات يطفيها فوراً

AD_MAX_CPA_SA = 8.00                 # أي فيديو يوصل التراكمي الخاص به سعر البيعة >= $8.00 في السعودية يطفيه فوراً
AD_MAX_CPA_GULF = 12.00              # سقف سعر البيعة التراكمي للفيديو في الخليج
CAMP_CORE_MAX_CPA = 20.00            # أي حملة سعر البيعة فيها >= $20.00 يطفيها فوراً

BANNED_KEYWORDS = ("kashaf", "kahsaf", "كشاف", "warmer", "مسخن", "maybach", "مايباخ")
GULF_KEYWORDS = (
    "oman", "qatar", "qat", "bahrain", "kuwait", "kuwiat", "kwt", "uae", "emarat", "gcc", "gulf",
    "عمان", "قطر", "البحرين", "بحرين", "كويت", "الكويت", "الامارات", "الإمارات", "خليج",
)


def load_paused_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_paused_history(history: dict):
    try:
        HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except Exception as e:
        print(f"⚠️ تعذر حفظ paused_history.json: {e}")


def fetch_sheet_sales() -> tuple[dict, dict, dict]:
    """Reads Lightfunnels Google Sheet orders for All-Time Cumulative, Last 3 Days, and Today with 60s deduplication."""
    csv_url = "https://docs.google.com/spreadsheets/d/1AaGXjCDbrD7tLEpYaJf5cncgZ3HW5IP3J8TIVAcn0pE/gviz/tq?tqx=out:csv"
    csv_text = ""
    for _ in range(3):
        try:
            req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                csv_text = resp.read().decode("utf-8", errors="replace")
            if csv_text:
                break
        except Exception:
            time.sleep(1.2)
    if not csv_text:
        return {}, {}, {}

    rows = list(csv.reader(io.StringIO(csv_text)))
    now = datetime.now(SA_TZ)
    today_str = now.strftime("%Y-%m-%d")
    valid_3d = {
        today_str,
        (now - timedelta(days=1)).strftime("%Y-%m-%d"),
        (now - timedelta(days=2)).strftime("%Y-%m-%d"),
    }

    last_time_by_utm = {}
    seen_keys = set()
    sales_all = {}
    sales_3d = {}
    sales_today = {}

    for r in rows[1:]:
        if len(r) < 3:
            continue
        prod_raw = (r[0] or "").strip().lower()
        if any(g in prod_raw for g in ("باور بانك صغير للطوارئ", "سوار رجالي", "سماعات", "هدية", "مجانا", "مجاناً")):
            continue
        utm_raw = (r[1] or "").strip().lower()
        date_raw = (r[2] or "").strip()
        if not utm_raw or not date_raw:
            continue
        date_str = date_raw.split(" ")[0]
        try:
            dt = datetime.strptime(date_raw[:19], "%Y-%m-%d %H:%M:%S")
            t_ms = dt.timestamp() * 1000.0
        except Exception:
            t_ms = 0.0
        ukey = f"{utm_raw}|{date_raw}"
        if ukey in seen_keys:
            continue
        if t_ms > 0 and utm_raw in last_time_by_utm and abs(t_ms - last_time_by_utm[utm_raw]) <= 60000:
            seen_keys.add(ukey)
            continue
        seen_keys.add(ukey)
        if t_ms > 0:
            last_time_by_utm[utm_raw] = t_ms
        sales_all[utm_raw] = sales_all.get(utm_raw, 0) + 1
        if date_str in valid_3d:
            sales_3d[utm_raw] = sales_3d.get(utm_raw, 0) + 1
        if date_str == today_str:
            sales_today[utm_raw] = sales_today.get(utm_raw, 0) + 1

    return sales_all, sales_3d, sales_today


def parse_insight_node(ins_block: dict) -> dict:
    """Extracts spend, impressions, clicks, link_clicks, cpc, link_cpc, ctr, purchases from a Graph API insights object."""
    data_list = (ins_block or {}).get("data") or []
    if not data_list:
        return {
            "spend": 0.0,
            "impressions": 0,
            "reach": 0,
            "clicks": 0,
            "link_clicks": 0,
            "cpc": 0.0,
            "link_cpc": 0.0,
            "ctr": 0.0,
            "purchases": 0,
            "initiate_checkouts": 0,
            "landing_page_views": 0,
            "cpa": None,
        }
    row = data_list[0]
    spend = float(row.get("spend") or 0.0)
    impressions = int(float(row.get("impressions") or 0))
    reach = int(float(row.get("reach") or 0))
    clicks = int(float(row.get("clicks") or 0))
    cpc = float(row.get("cpc") or (spend / clicks if clicks > 0 else 0.0))
    ctr = float(row.get("ctr") or 0.0)

    actions = row.get("actions") or []
    act_map = {}
    for a in actions:
        atype = a.get("action_type")
        val = int(float(a.get("value") or 0))
        if atype:
            act_map[atype] = val

    purchases = max(
        act_map.get("purchase", 0),
        act_map.get("offsite_conversion.fb_pixel_purchase", 0),
        act_map.get("omni_purchase", 0),
        act_map.get("onsite_web_purchase", 0),
    )
    initiate_checkouts = max(
        act_map.get("initiate_checkout", 0),
        act_map.get("offsite_conversion.fb_pixel_initiate_checkout", 0),
        act_map.get("omni_initiated_checkout", 0),
    )
    landing_page_views = max(
        act_map.get("landing_page_view", 0),
        act_map.get("omni_landing_page_view", 0),
    )
    link_clicks = act_map.get("link_click", clicks)
    link_cpc = round(spend / link_clicks, 4) if link_clicks > 0 else (spend if spend > 0 else 0.0)
    cpa = round(spend / purchases, 2) if purchases > 0 else None

    return {
        "spend": round(spend, 2),
        "impressions": impressions,
        "reach": reach,
        "clicks": clicks,
        "link_clicks": link_clicks,
        "cpc": round(cpc, 4),
        "link_cpc": link_cpc,
        "ctr": round(ctr, 2),
        "purchases": purchases,
        "initiate_checkouts": initiate_checkouts,
        "landing_page_views": landing_page_views,
        "cpa": cpa,
    }


def add_insight_nodes(a: dict, b: dict) -> dict:
    """Sums Today + Last_3D insight nodes so 3D includes Today (since Meta's last_3d excludes Today)."""
    spend = round(a["spend"] + b["spend"], 2)
    impressions = a["impressions"] + b["impressions"]
    reach = max(a["reach"], b["reach"])
    clicks = a["clicks"] + b["clicks"]
    link_clicks = a["link_clicks"] + b["link_clicks"]
    purchases = a["purchases"] + b["purchases"]
    initiate_checkouts = a["initiate_checkouts"] + b["initiate_checkouts"]
    landing_page_views = a["landing_page_views"] + b["landing_page_views"]
    cpc = round(spend / clicks, 4) if clicks > 0 else 0.0
    link_cpc = round(spend / link_clicks, 4) if link_clicks > 0 else (spend if spend > 0 else 0.0)
    ctr = round((clicks / impressions) * 100.0, 2) if impressions > 0 else 0.0
    cpa = round(spend / purchases, 2) if purchases > 0 else None
    return {
        "spend": spend,
        "impressions": impressions,
        "reach": reach,
        "clicks": clicks,
        "link_clicks": link_clicks,
        "cpc": cpc,
        "link_cpc": link_cpc,
        "ctr": ctr,
        "purchases": purchases,
        "initiate_checkouts": initiate_checkouts,
        "landing_page_views": landing_page_views,
        "cpa": cpa,
    }


def max_insight_nodes(a: dict, b: dict) -> dict:
    """Returns the cumulative maximum between two insight nodes (e.g., Meta 'maximum' vs 'today + last_3d')."""
    spend = round(max(a["spend"], b["spend"]), 2)
    impressions = max(a["impressions"], b["impressions"])
    reach = max(a["reach"], b["reach"])
    clicks = max(a["clicks"], b["clicks"])
    link_clicks = max(a["link_clicks"], b["link_clicks"])
    purchases = max(a["purchases"], b["purchases"])
    initiate_checkouts = max(a["initiate_checkouts"], b["initiate_checkouts"])
    landing_page_views = max(a["landing_page_views"], b["landing_page_views"])
    cpc = round(spend / clicks, 4) if clicks > 0 else 0.0
    link_cpc = round(spend / link_clicks, 4) if link_clicks > 0 else (spend if spend > 0 else 0.0)
    ctr = round((clicks / impressions) * 100.0, 2) if impressions > 0 else 0.0
    cpa = round(spend / purchases, 2) if purchases > 0 else None
    return {
        "spend": spend,
        "impressions": impressions,
        "reach": reach,
        "clicks": clicks,
        "link_clicks": link_clicks,
        "cpc": cpc,
        "link_cpc": link_cpc,
        "ctr": ctr,
        "purchases": purchases,
        "initiate_checkouts": initiate_checkouts,
        "landing_page_views": landing_page_views,
        "cpa": cpa,
    }


SHARED_CAMPAIGN_UTMS = {"fb_car_test_10usd", "fb_car_test", "facebook", "paid"}


def extract_ad_utm_keys(ad_obj: dict) -> list[str]:
    """Extracts ONLY unique ad/video-specific UTM identifiers (never shared campaign-wide UTMs), including normalized underscore variants (e.g. carmz07 <-> fb_usa_car_carmz_07)."""
    keys = set()
    ad_name = (ad_obj.get("name") or "").strip().lower()
    if ad_name:
        keys.add(ad_name)
        for token in re.split(r"[^a-z0-9_]+", ad_name):
            if len(token) >= 5 and token not in ("winner", "creative", "multi5h") and token not in SHARED_CAMPAIGN_UTMS:
                keys.add(token)
                # Match carmz07 <-> carmz_07 / fb_usa_car_carmz_07 / fb_pb_pbmz08
                m = re.match(r"^([a-z]+)_?(\d+)$", token)
                if m:
                    prefix, digits = m.group(1), m.group(2)
                    no_us = f"{prefix}{digits}"
                    with_us = f"{prefix}_{digits}"
                    keys.update({
                        no_us,
                        with_us,
                        f"fb_pb_{no_us}",
                        f"fb_pb_{with_us}",
                        f"fb_car_{no_us}",
                        f"fb_car_{with_us}",
                        f"fb_usa_pb_{no_us}",
                        f"fb_usa_pb_{with_us}",
                        f"fb_usa_car_{no_us}",
                        f"fb_usa_car_{with_us}",
                    })
    creative = ad_obj.get("creative") or {}
    oss = creative.get("object_story_spec") or {}
    vdata = oss.get("video_data") or {}
    cta = (vdata.get("call_to_action") or {}).get("value") or {}
    link = cta.get("link") or ""
    if link and "?" in link:
        qs = urllib.parse.parse_qs(link.split("?", 1)[1])
        for k in ("utm_content", "utm_campaign"):
            for v in qs.get(k, []):
                val = (v or "").strip().lower()
                if val and val not in SHARED_CAMPAIGN_UTMS:
                    keys.add(val)
    return list(keys)


def fb_post_status(token: str, object_id: str, new_status: str = "PAUSED") -> tuple[bool, dict]:
    for attempt in range(3):
        ssock = None
        try:
            ssock = fb.open_tunnel("graph.facebook.com")
            res = fb.graph_call(ssock, "POST", object_id, data={"status": new_status}, token=token)
            ssock.close()
            if res.get("success") or res.get("id"):
                return True, res
            return False, res
        except Exception as e:
            if ssock is not None:
                try:
                    ssock.close()
                except Exception:
                    pass
            if attempt == 2:
                return False, {"error": str(e)}
            time.sleep(1.5)
    return False, {}


def main():
    dry_run = "--dry-run" in sys.argv
    now_sa = datetime.now(SA_TZ)
    now_str = now_sa.strftime("%Y-%m-%d %H:%M:%S GMT+3")
    print("======================================================================")
    print(f"📘 [Facebook Ads 24/7 Monitor] بدء فحص حسابات فيسبوك — {now_str}")
    print(
        f"⚙️ القواعد المفعلة: "
        f"الفيديو (CPC > ${AD_MAX_CPC_NO_SALE:.2f} بعد ${AD_CPC_MIN_SPEND:.2f} بصفر بيع | >= ${AD_NO_SALE_CUMULATIVE_MAX:.2f} بدون بيعة | CPA التراكمي >= ${AD_MAX_CPA_SA:.2f}) | "
        f"الحملة (الحملات الجديدة >= ${CAMP_MIN_SPEND_BEFORE_PAUSE:.2f} بدون بيع | >= ${CAMP_NO_SALE_CUMULATIVE_MAX:.2f} بدون مبيعات أو CPA >= ${CAMP_CORE_MAX_CPA:.2f})"
    )
    print("======================================================================")

    try:
        tok = fb.get_token()
        ssock = fb.open_tunnel("graph.facebook.com")
        me_check = fb.graph_call(ssock, "GET", "me", {"fields": "id,name"}, token=tok)
        ssock.close()
        if "error" in me_check:
            tok = fb.refresh_live_token()
    except Exception as e_auth:
        print(f"⚠️ تعذر الاتصال بـ Facebook Graph API عبر البروكسي: {e_auth}")
        return

    # Execute pending user budget/status commands automatically as soon as token is active
    pending_updates = [
        ("52542228452250", {"daily_budget": "8000", "name": "SA - Car Armrest Cushion - Test 3 Winners ($80)"}, "رفع بدجت حملة وسادة السيارة الهولندية إلى $80/يوم"),
        ("120247881183330658", {"daily_budget": "5000", "name": "SA - Power Bank 4in1 - Untested Batch 2 ($50)"}, "رفع بدجت حملة الباور بانك Untested Batch 2 إلى $50/يوم"),
        ("120247859287340658", {"status": "PAUSED"}, "إيقاف حملة SA - Power Bank 4in1 - USA 3 Winners ($30)"),
    ]
    for obj_id, payload, desc in pending_updates:
        try:
            _s = fb.open_tunnel("graph.facebook.com")
            _r = fb.graph_call(_s, "POST", obj_id, data=payload, token=tok)
            _s.close()
            if _r.get("success") or _r.get("id"):
                print(f"⚡ {desc} ({obj_id}): نجح ✅")
        except Exception:
            pass

    sales_all, sales_3d, sales_today = fetch_sheet_sales()
    history = load_paused_history()
    fb_ads_hist = history.setdefault("facebook_ads", {})
    fb_camps_hist = history.setdefault("facebook_campaigns", {})

    snapshot_out = {
        "timestamp": now_str,
        "total_spend_today": 0.0,
        "total_spend_maximum": 0.0,
        "total_purchases_today": 0,
        "total_purchases_maximum": 0,
        "accounts": {},
        "actions_taken": [],
    }

    for acc_key, acc_cfg in fb.AD_ACCOUNTS.items():
        aid = acc_cfg["id"]
        aname = acc_cfg["name"]
        try:
            def _fb_get_retry(path, params):
                for _att in range(4):
                    _s = None
                    try:
                        _s = fb.open_tunnel("graph.facebook.com")
                        _r = fb.graph_call(_s, "GET", path, params, token=tok)
                        _s.close()
                        return _r
                    except Exception:
                        if _s is not None:
                            try:
                                _s.close()
                            except Exception:
                                pass
                        time.sleep(1.2)
                return {}

            acc_info = _fb_get_retry(
                aid,
                {
                    "fields": "id,account_id,name,account_status,disable_reason,currency,timezone_name,amount_spent,spend_cap,balance"
                },
            )
            ins_1d_raw = _fb_get_retry(
                f"{aid}/insights",
                {
                    "date_preset": "today",
                    "fields": "spend,impressions,reach,clicks,cpc,cpm,ctr,actions,cost_per_action_type",
                },
            )
            ins_max_raw = _fb_get_retry(
                f"{aid}/insights",
                {
                    "date_preset": "maximum",
                    "fields": "spend,impressions,reach,clicks,cpc,cpm,ctr,actions,cost_per_action_type",
                },
            )
            camps_1d_raw = _fb_get_retry(
                f"{aid}/campaigns",
                {
                    "fields": (
                        "id,name,status,effective_status,configured_status,objective,daily_budget,budget_remaining,bid_strategy,"
                        "insights.date_preset(today){spend,impressions,reach,clicks,cpc,cpm,ctr,actions,cost_per_action_type},"
                        "adsets{id,name,status,effective_status,daily_budget,optimization_goal,"
                        "insights.date_preset(today){spend,impressions,clicks,cpc,cpm,ctr,actions,cost_per_action_type}},"
                        "ads{id,name,status,effective_status,configured_status,"
                        "creative{id,name,object_story_spec},"
                        "insights.date_preset(today){spend,impressions,reach,clicks,cpc,cpm,ctr,actions,cost_per_action_type}}"
                    ),
                    "limit": "50",
                },
            )
            camps_3d_raw = _fb_get_retry(
                f"{aid}/campaigns",
                {
                    "fields": (
                        "id,name,"
                        "insights.date_preset(last_3d){spend,impressions,reach,clicks,cpc,cpm,ctr,actions,cost_per_action_type},"
                        "ads{id,name,insights.date_preset(last_3d){spend,impressions,reach,clicks,cpc,cpm,ctr,actions,cost_per_action_type}}"
                    ),
                    "limit": "50",
                },
            )
            camps_max_raw = _fb_get_retry(
                f"{aid}/campaigns",
                {
                    "fields": (
                        "id,name,"
                        "insights.date_preset(maximum){spend,impressions,reach,clicks,cpc,cpm,ctr,actions,cost_per_action_type},"
                        "ads{id,name,insights.date_preset(maximum){spend,impressions,reach,clicks,cpc,cpm,ctr,actions,cost_per_action_type}}"
                    ),
                    "limit": "50",
                },
            )
        except Exception as e_acc:
            print(f"⚠️ خطأ أثناء فحص حساب فيسبوك {aname} ({aid}): {e_acc}")
            continue

        acc_1d = parse_insight_node(ins_1d_raw)
        acc_max = parse_insight_node(ins_max_raw)
        snapshot_out["total_spend_today"] = round(snapshot_out["total_spend_today"] + acc_1d["spend"], 2)
        snapshot_out["total_spend_maximum"] = round(snapshot_out["total_spend_maximum"] + acc_max["spend"], 2)
        snapshot_out["total_purchases_today"] += acc_1d["purchases"]
        snapshot_out["total_purchases_maximum"] += acc_max["purchases"]

        cpa_1d_str = f"${acc_1d['cpa']:.2f}" if acc_1d["cpa"] is not None else "—"
        print(
            f"\n📊 الحساب: {aname} ({aid}) | الحالة: {acc_info.get('account_status')} | "
            f"صرف اليوم: ${acc_1d['spend']:.2f} | مبيعات اليوم: {acc_1d['purchases']} (CPA: {cpa_1d_str}) | "
            f"التراكمي: ${acc_max['spend']:.2f} / {acc_max['purchases']} مبيعة | "
            f"الظهور: {acc_1d['impressions']} | النقرات: {acc_1d['clicks']} (Link: {acc_1d['link_clicks']}, CPC: ${acc_1d['cpc']:.3f})"
        )

        # Index 3D and Maximum stats by campaign_id and ad_id
        c3_map = {}
        ad3_map = {}
        for c3 in (camps_3d_raw or {}).get("data") or []:
            cid3 = str(c3.get("id") or "")
            c3_map[cid3] = parse_insight_node(c3.get("insights"))
            for a3 in ((c3.get("ads") or {}).get("data") or []):
                aid3 = str(a3.get("id") or "")
                ad3_map[aid3] = parse_insight_node(a3.get("insights"))

        cmax_map = {}
        admax_map = {}
        for cm in (camps_max_raw or {}).get("data") or []:
            cidm = str(cm.get("id") or "")
            cmax_map[cidm] = parse_insight_node(cm.get("insights"))
            for am in ((cm.get("ads") or {}).get("data") or []):
                aidm = str(am.get("id") or "")
                admax_map[aidm] = parse_insight_node(am.get("insights"))

        acc_camps_summary = []
        for camp in (camps_1d_raw or {}).get("data") or []:
            cid = str(camp.get("id") or "")
            cname = camp.get("name") or cid
            c_status = camp.get("status") or ""
            c_eff = camp.get("effective_status") or ""
            daily_budget = float(camp.get("daily_budget") or 0) / 100.0

            st1 = parse_insight_node(camp.get("insights"))
            st3_prev = c3_map.get(cid) or parse_insight_node({})
            st3 = add_insight_nodes(st1, st3_prev)
            st_max_raw = cmax_map.get(cid) or parse_insight_node({})
            st_cum = max_insight_nodes(st3, st_max_raw)

            cname_low = cname.lower()
            is_gulf = any(g in cname_low for g in GULF_KEYWORDS)

            cpa_st1_s = f"${st1['cpa']:.2f}" if st1["cpa"] else "—"
            cpa_cum_s = f"${st_cum['cpa']:.2f}" if st_cum["cpa"] else "—"
            print(
                f"  🎯 الحملة: {cname} ({cid}) [{c_eff} | Budget: ${daily_budget:.0f}/day] -> "
                f"اليوم: ${st1['spend']:.2f} / {st1['purchases']} مبيعة (CPA: {cpa_st1_s}) | "
                f"التراكمي: ${st_cum['spend']:.2f} / {st_cum['purchases']} مبيعة (CPA: {cpa_cum_s})"
            )

            # First evaluate ads so we also know if any ad had attributed sheet sales
            ads_list = (camp.get("ads") or {}).get("data") or []
            active_ads_count = sum(1 for a in ads_list if a.get("status") == "ACTIVE")
            ads_summary = []
            camp_sheet_sales_cum = 0
            camp_sheet_sales_1d = 0

            for ad in ads_list:
                ad_id = str(ad.get("id") or "")
                ad_name = ad.get("name") or ad_id
                ad_status = ad.get("status") or ""
                ad_eff = ad.get("effective_status") or ""

                ast1 = parse_insight_node(ad.get("insights"))
                ast3_prev = ad3_map.get(ad_id) or parse_insight_node({})
                ast3 = add_insight_nodes(ast1, ast3_prev)
                ast_max_raw = admax_map.get(ad_id) or parse_insight_node({})
                ast_cum = max_insight_nodes(ast3, ast_max_raw)

                # Match with Lightfunnels Google Sheet UTM only if ad has link clicks and unique ad UTM
                utm_keys = extract_ad_utm_keys(ad)
                sheet_cv1 = max([sales_today.get(k, 0) for k in utm_keys] or [0]) if (ast1["spend"] >= 0.30 and ast1["link_clicks"] >= 1) else 0
                sheet_cv3 = max([sales_3d.get(k, 0) for k in utm_keys] or [0]) if (ast3["spend"] >= 0.30 and ast3["link_clicks"] >= 1) else 0
                sheet_cv_all = max([sales_all.get(k, 0) for k in utm_keys] or [0]) if (ast_cum["spend"] >= 0.30 and ast_cum["link_clicks"] >= 1) else 0

                cv1 = max(ast1["purchases"], sheet_cv1)
                cv3 = max(ast3["purchases"], sheet_cv3)
                cv_cum = max(ast_cum["purchases"], cv3, sheet_cv_all)
                camp_sheet_sales_cum += cv_cum
                camp_sheet_sales_1d += cv1

                cpa1 = round(ast1["spend"] / cv1, 2) if cv1 > 0 else None
                cpa3 = round(ast3["spend"] / cv3, 2) if cv3 > 0 else None
                cpa_cum = round(ast_cum["spend"] / cv_cum, 2) if cv_cum > 0 else None

                max_cpa_allowed = AD_MAX_CPA_GULF if is_gulf else AD_MAX_CPA_SA

                print(
                    f"    🎬 إعلان: {ad_name} ({ad_id}) [{ad_eff}] -> "
                    f"اليوم: ${ast1['spend']:.2f}/{cv1} مبيعة | التراكمي: ${ast_cum['spend']:.2f}/{cv_cum} مبيعة (CPA: {f'${cpa_cum:.2f}' if cpa_cum else '—'}) | "
                    f"ظهور: {ast_cum['impressions']} | نقرات: {ast_cum['clicks']} (Link: {ast_cum['link_clicks']}, CPC: ${ast_cum['cpc']:.3f})"
                )

                ads_summary.append(
                    {
                        "id": ad_id,
                        "name": ad_name,
                        "status": ad_status,
                        "effective_status": ad_eff,
                        "spend_today": ast1["spend"],
                        "impressions_today": ast1["impressions"],
                        "clicks_today": ast1["clicks"],
                        "link_clicks_today": ast1["link_clicks"],
                        "cpc_today": ast1["cpc"],
                        "link_cpc_today": ast1["link_cpc"],
                        "purchases_today": cv1,
                        "cpa_today": cpa1,
                        "spend_3d": ast3["spend"],
                        "purchases_3d": cv3,
                        "cpa_3d": cpa3,
                        "spend_cumulative": ast_cum["spend"],
                        "purchases_cumulative": cv_cum,
                        "cpa_cumulative": cpa_cum,
                    }
                )

                if ad_status != "ACTIVE":
                    continue

                ad_pause_reason = None
                # Rule 1 (Video Rules: Mandatory High CPC > $0.27 after $1.00 with 0 sales | Cumulative CPA >= $8.00 | Cumulative Spend >= $9.00 with 0 sales):
                # 1-A: Cumulative CPA >= $8.00 (SA) / $12.00 (Gulf) when cumulative sales >= 1
                if cv_cum > 0 and cpa_cum is not None and cpa_cum >= max_cpa_allowed:
                    ad_pause_reason = (
                        f"[Facebook Video Rule: Cumulative CPA >= ${max_cpa_allowed:.2f}] سعر البيعة التراكمي للفيديو وصل ${cpa_cum:.2f} (>= ${max_cpa_allowed:.2f} بصرف ${ast_cum['spend']:.2f} / {cv_cum} مبيعة)"
                    )
                # 1-B: Cumulative Spend >= $9.00 with 0 sales
                elif ast_cum["spend"] >= AD_NO_SALE_CUMULATIVE_MAX and cv_cum == 0:
                    ad_pause_reason = (
                        f"[Facebook Video Rule: Cumulative >= ${AD_NO_SALE_CUMULATIVE_MAX:.2f} No-Sale Stop] الفيديو صرف ${ast_cum['spend']:.2f} تراكمي (>= ${AD_NO_SALE_CUMULATIVE_MAX:.2f}) بدون مبيعات (0 مبيعات)"
                    )
                # 1-C: Mandatory & Unconditional High CPC > $0.27 after $1.00 spend with 0 sales (ضروري بعد $1.00 بدون أي استثناء)
                elif cv_cum == 0 and ast_cum["spend"] >= AD_CPC_MIN_SPEND and (ast_cum["clicks"] == 0 or ast_cum["cpc"] > AD_MAX_CPC_NO_SALE):
                    ad_pause_reason = (
                        f"[Facebook Video Rule: CPC > ${AD_MAX_CPC_NO_SALE:.2f} after ${AD_CPC_MIN_SPEND:.2f}] صرف الفيديو ${ast_cum['spend']:.2f} بدون مبيعات | النقرات: {ast_cum['clicks']} | CPC = ${ast_cum['cpc']:.3f} (> ${AD_MAX_CPC_NO_SALE:.2f})"
                    )
                elif cv1 == 0 and ast1["spend"] >= AD_CPC_MIN_SPEND and (ast1["clicks"] == 0 or ast1["cpc"] > AD_MAX_CPC_NO_SALE):
                    ad_pause_reason = (
                        f"[Facebook Video Rule: Today CPC > ${AD_MAX_CPC_NO_SALE:.2f} after ${AD_CPC_MIN_SPEND:.2f}] صرف الفيديو اليوم ${ast1['spend']:.2f} بدون مبيعات | النقرات: {ast1['clicks']} | CPC = ${ast1['cpc']:.3f} (> ${AD_MAX_CPC_NO_SALE:.2f})"
                    )

                if ad_pause_reason:
                    if dry_run:
                        print(f"      🔍 [DRY-RUN] سيتم إيقاف إعلان فيسبوك '{ad_name}' ({ad_id}): {ad_pause_reason}")
                    else:
                        ok, resp = fb_post_status(tok, ad_id, "PAUSED")
                        act_txt = f"إيقاف إعلان فيسبوك '{ad_name}' ({ad_id}) في '{cname}': {ad_pause_reason} -> {'نجح ✅' if ok else f'فشل ⚠️ {resp}'}"
                        print(f"      🛑 {act_txt}")
                        snapshot_out["actions_taken"].append(act_txt)
                        if ok:
                            active_ads_count = max(0, active_ads_count - 1)
                            fb_ads_hist[ad_id] = {
                                "ad_id": ad_id,
                                "ad_name": ad_name,
                                "campaign_id": cid,
                                "campaign_name": cname,
                                "account_id": aid,
                                "spend_today": ast1["spend"],
                                "spend_cumulative": ast_cum["spend"],
                                "purchases_cumulative": cv_cum,
                                "reason": ad_pause_reason,
                                "paused_at": now_str,
                            }

            # Check Campaign-Level Stop-Loss if ACTIVE ($15.00-$20.00 No-Sale OR CPA >= $20.00)
            # Combine Meta Pixel purchases with verified unique per-ad Sheet sales (camp_sheet_sales_cum / camp_sheet_sales_1d)
            camp_total_purchases_cum = max(st_cum["purchases"], camp_sheet_sales_cum)
            camp_cpa_cum = round(st_cum["spend"] / camp_total_purchases_cum, 2) if camp_total_purchases_cum > 0 else None
            camp_total_purchases_1d = max(st1["purchases"], camp_sheet_sales_1d)
            camp_cpa_1d = round(st1["spend"] / camp_total_purchases_1d, 2) if camp_total_purchases_1d > 0 else None
            is_new_test_camp = any(k in cname_low for k in ("untested", "taager", "usa 3 winners", "batch"))
            eff_camp_no_sale_max = CAMP_MIN_SPEND_BEFORE_PAUSE if is_new_test_camp else CAMP_NO_SALE_CUMULATIVE_MAX

            camp_pause_reason = None
            if c_status == "ACTIVE":
                if any(b in cname_low for b in BANNED_KEYWORDS):
                    camp_pause_reason = f"حملة منتج محظور ({cname})"
                elif len(ads_list) > 0 and active_ads_count == 0:
                    camp_pause_reason = f"حملة ميتة بدون أي إعلانات نشطة (0/{len(ads_list)} إعلان نشط بعد إيقاف جميع الفيديوهات الخاسرة)"
                elif st_cum["spend"] >= eff_camp_no_sale_max and camp_total_purchases_cum == 0:
                    camp_pause_reason = (
                        f"[Campaign >= ${eff_camp_no_sale_max:.2f} No-Sale Stop] الحملة صرفت ${st_cum['spend']:.2f} تراكمي (>= ${eff_camp_no_sale_max:.2f}) بدون مبيعات (0 مبيعات)"
                    )
                elif st1["spend"] >= eff_camp_no_sale_max and camp_total_purchases_1d == 0:
                    camp_pause_reason = (
                        f"[Campaign Today >= ${eff_camp_no_sale_max:.2f} No-Sale Stop] الحملة صرفت اليوم ${st1['spend']:.2f} (>= ${eff_camp_no_sale_max:.2f}) بدون مبيعات اليوم (0 مبيعات)"
                    )
                elif camp_total_purchases_cum > 0 and camp_cpa_cum is not None and camp_cpa_cum >= CAMP_CORE_MAX_CPA:
                    camp_pause_reason = (
                        f"[Campaign CPA >= ${CAMP_CORE_MAX_CPA:.2f} Cut] سعر البيعة التراكمي للحملة وصل ${camp_cpa_cum:.2f} (>= ${CAMP_CORE_MAX_CPA:.2f} بصرف ${st_cum['spend']:.2f} / {camp_total_purchases_cum} مبيعة)"
                    )
                elif camp_total_purchases_1d > 0 and camp_cpa_1d is not None and camp_cpa_1d >= CAMP_CORE_MAX_CPA:
                    camp_pause_reason = (
                        f"[Campaign Today CPA >= ${CAMP_CORE_MAX_CPA:.2f} Cut] سعر البيعة اليوم للحملة وصل ${camp_cpa_1d:.2f} (>= ${CAMP_CORE_MAX_CPA:.2f} بصرف ${st1['spend']:.2f} / {camp_total_purchases_1d} مبيعة)"
                    )

            if camp_pause_reason:
                if dry_run:
                    print(f"    🔍 [DRY-RUN] سيتم إيقاف حملة فيسبوك '{cname}' ({cid}): {camp_pause_reason}")
                else:
                    ok, resp = fb_post_status(tok, cid, "PAUSED")
                    act_txt = f"إيقاف حملة فيسبوك '{cname}' ({cid}): {camp_pause_reason} -> {'نجح ✅' if ok else f'فشل ⚠️ {resp}'}"
                    print(f"    🛑 {act_txt}")
                    snapshot_out["actions_taken"].append(act_txt)
                    if ok:
                        fb_camps_hist[cid] = {
                            "campaign_id": cid,
                            "campaign_name": cname,
                            "account_id": aid,
                            "spend_today": st1["spend"],
                            "spend_cumulative": st_cum["spend"],
                            "purchases_cumulative": camp_total_purchases_cum,
                            "reason": camp_pause_reason,
                            "paused_at": now_str,
                        }

            acc_camps_summary.append(
                {
                    "id": cid,
                    "name": cname,
                    "status": c_status,
                    "effective_status": c_eff,
                    "daily_budget": daily_budget,
                    "spend_today": st1["spend"],
                    "impressions_today": st1["impressions"],
                    "clicks_today": st1["clicks"],
                    "link_clicks_today": st1["link_clicks"],
                    "cpc_today": st1["cpc"],
                    "link_cpc_today": st1["link_cpc"],
                    "purchases_today": st1["purchases"],
                    "cpa_today": st1["cpa"],
                    "spend_3d": st3["spend"],
                    "purchases_3d": st3["purchases"],
                    "cpa_3d": st3["cpa"],
                    "spend_cumulative": st_cum["spend"],
                    "purchases_cumulative": camp_total_purchases_cum,
                    "cpa_cumulative": camp_cpa_cum,
                    "ads": ads_summary,
                }
            )

        snapshot_out["accounts"][aid] = {
            "name": aname,
            "account_status": acc_info.get("account_status"),
            "currency": acc_info.get("currency", "USD"),
            "spend_today": acc_1d["spend"],
            "purchases_today": acc_1d["purchases"],
            "cpa_today": acc_1d["cpa"],
            "impressions_today": acc_1d["impressions"],
            "clicks_today": acc_1d["clicks"],
            "link_clicks_today": acc_1d["link_clicks"],
            "cpc_today": acc_1d["cpc"],
            "spend_maximum": acc_max["spend"],
            "purchases_maximum": acc_max["purchases"],
            "cpa_maximum": acc_max["cpa"],
            "campaigns": acc_camps_summary,
        }

    history["facebook_latest_summary"] = {
        "updated_at": now_str,
        "total_spend_today": snapshot_out["total_spend_today"],
        "total_purchases_today": snapshot_out["total_purchases_today"],
        "total_spend_maximum": snapshot_out["total_spend_maximum"],
        "total_purchases_maximum": snapshot_out["total_purchases_maximum"],
        "actions_taken": snapshot_out["actions_taken"],
    }
    save_paused_history(history)
    try:
        SNAPSHOT_FILE.write_text(json.dumps(snapshot_out, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    tot_sp = snapshot_out["total_spend_today"]
    tot_cv = snapshot_out["total_purchases_today"]
    tot_cpa = f"${tot_sp / tot_cv:.2f}" if tot_cv > 0 else "—"
    print("\n======================================================================")
    print(f"✅ [Facebook Monitor Summary] إجمالي صرف اليوم: ${tot_sp:.2f} | إجمالي المبيعات: {tot_cv} | متوسط CPA: {tot_cpa}")
    print("======================================================================")


if __name__ == "__main__":
    main()
