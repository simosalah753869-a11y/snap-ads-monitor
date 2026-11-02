import csv
import hashlib
import hmac
import io
import json
import os
import re
import socket
import sys
import time
import urllib.request
import uuid
import zlib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import tiktok_monitor as tm

SA_TZ = timezone(timedelta(hours=3))
LEDGER_FILE = BASE_DIR / "tiktok_master_actions_ledger.json"
REPORT_FILE = BASE_DIR / "tiktok_master_latest_report.md"

# ==============================================================================
# STRICT USER & SAFETY THRESHOLDS FOR TIKTOK HOURLY SALES MASTER
# 1) Hard Campaign Stop-Loss ("اهم شي حملة في تيكتوك صرفت 20$ بدون بيع يطفي او سعر البيعة 20$ بطفي"):
#    - Campaign Spend >= $20.00 with 0 sales -> Pause Campaign immediately
#    - Campaign CPA >= $20.00 -> Pause Campaign immediately
# 2) Micro-Stop-Loss for Experimental [EXP-LEADS] / [EXP-] Campaigns ("بدون ما يخسرني فلوسي"):
#    - Experimental Video Spend >= $1.00 & CPC > $0.25 (or 0 clicks) with 0 sales -> Pause Video
#    - Experimental Video Spend >= $3.50 with 0 sales -> Pause Video
#    - Experimental Campaign Spend >= $8.50 with 0 sales -> Pause Campaign early (never burn $20!)
#    - Experimental Campaign CPA >= $9.50 after >= $9.50 spend -> Pause Campaign early
# 3) Proven Winning Videos ONLY ("اي تجريب يعمله ما يعمله على فيديوهاتي الجديدة ولاكن يعمله على فيديوهاتي الرابحة"):
#    - Every experiment MUST use 3 to 5 proven winning videos with real historical sales (0 new/untested videos).
# 4) Active Sales Boosting in Existing Campaigns ("يعمل على زيادة عدد المبيعات في الحملات"):
#    - Revive multi-sale 3D winning campaigns (3D sales >= 3, 3D CPA <= $7.00, 1D spend < $4.00) & their winning videos
#      that were choked right after midnight while keeping 0-sale sibling videos paused.
#    - Progressive +30% budget scaling on converting campaigns (1D sales >= 1 @ CPA <= $5.50 or >= 2 @ CPA <= $6.80).
#    - Prune 0-sale sibling videos (1D spend >= $2.50 with 0 sales) inside converting campaigns to focus 100% budget on winners.
# ==============================================================================
HARD_CAMPAIGN_NO_SALE_LIMIT = 20.00
HARD_CAMPAIGN_CPA_LIMIT = 20.00
EXP_CAMPAIGN_NO_SALE_MICRO_LIMIT = 8.50
EXP_CAMPAIGN_MAX_CPA_MICRO_LIMIT = 9.50
EXP_VIDEO_NO_SALE_MICRO_LIMIT = 3.50
ACCOUNT_BLEED_SPEND_THRESHOLD = 20.00
ACCOUNT_BLEED_MAX_CPA = 20.00
VIDEO_CPC_MIN_SPEND = 1.00
VIDEO_MAX_CPC = 0.25
VIDEO_MAX_CPC_WITH_SALES = 0.35
SIBLING_PRUNE_MIN_SPEND = 2.50
MAX_ACTIVE_EXPERIMENTS_PER_ACCOUNT = 0


def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        try:
            data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
            data.setdefault("cycles", [])
            data.setdefault("actions", [])
            data.setdefault("failed_experiments", [])
            data.setdefault("scaled_campaigns", {})
            data.setdefault("revived_campaigns", {})
            return data
        except Exception:
            pass
    return {
        "cycles": [],
        "actions": [],
        "failed_experiments": [],
        "scaled_campaigns": {},
        "revived_campaigns": {},
    }


def save_ledger(data: dict):
    LEDGER_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_sheet_tiktok_sales() -> dict:
    """Fetches Lightfunnels Google Sheet orders for Today, Yesterday, 3D, and 7D."""
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
        return {"1d": {}, "yest": {}, "3d": {}, "7d": {}}

    rows = list(csv.reader(io.StringIO(csv_text)))
    now_utc2 = datetime.now(timezone(timedelta(hours=2)))
    today_s = now_utc2.strftime("%Y-%m-%d")
    yest_s = (now_utc2 - timedelta(days=1)).strftime("%Y-%m-%d")
    d3_set = {(now_utc2 - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)}
    d7_set = {(now_utc2 - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)}

    last_time_by_utm = {}
    seen_keys = set()
    s1, sy, s3, s7 = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)

    for r in rows[1:]:
        if len(r) < 3:
            continue
        prod_raw = (r[0] or "").strip().lower()
        if any(g in prod_raw for g in ("باور بانك صغير للطوارئ", "سوار رجالي", "سماعات", "هدية", "مجانا", "مجاناً")):
            continue
        utm_raw = (r[1] or "").strip()
        date_raw = (r[2] or "").strip()
        if not utm_raw or not date_raw:
            continue
        d_str = date_raw.split(" ")[0]
        if d_str not in d7_set:
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

        s7[utm] += 1
        if d_str in d3_set:
            s3[utm] += 1
        if d_str == today_s:
            s1[utm] += 1
        elif d_str == yest_s:
            sy[utm] += 1

    return {"1d": dict(s1), "yest": dict(sy), "3d": dict(s3), "7d": dict(s7)}


def extract_winning_assets_from_campaigns(session, headers, aadvid: str, donor_specs: list[tuple[str, str]]) -> tuple[tuple, dict, dict, dict, list[dict]]:
    """
    Pulls exact image_list items for proven winning videos from donor campaigns in the same account.
    donor_specs = [(campaign_id, exact_or_prefix_video_name), ...]
    Returns (base_ids, base_camp_form, base_ad_form, base_creative_form, selected_image_items).
    """
    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/creation/1nn/create/campaign?aadvid={aadvid}"

    by_cid = defaultdict(list)
    if aadvid in DONOR_TEMPLATES:
        by_cid[str(DONOR_TEMPLATES[aadvid])].append("__BASE_TEMPLATE__")
    for cid, vname in donor_specs:
        by_cid[str(cid)].append(vname)

    base_c_f, base_a_f, base_cr_f = None, None, None
    base_ids = (None, None, None)
    collected_imgs = []
    seen_vids = set()

    for cid, want_names in by_cid.items():
        try:
            rc = session.post(
                f"https://ads.tiktok.com/api/v4/i18n/creation/campaign_snap/copy/?aadvid={aadvid}",
                headers=h,
                json={
                    "campaign_id": str(cid),
                    "name": "_tmp_winner_extract",
                    "with_sketch": False,
                    "with_ad": True,
                    "with_creative": True,
                    "risk_info": {},
                },
                timeout=20,
            ).json().get("data") or {}
            c_s = (rc.get("new_campaign_snap_info_item") or {}).get("campaign_snap_id")
            a_s_list = rc.get("new_ad_snap_info_item_list") or []
            a_s = a_s_list[0].get("ad_snap_id") if a_s_list else None
            cr_vals = list((rc.get("new_ad_and_creative_snap_info_item_map") or {}).values())
            cr_s = (
                (cr_vals[0][0].get("creative_snap_id") if isinstance(cr_vals[0][0], dict) else str(cr_vals[0][0]))
                if cr_vals and cr_vals[0]
                else None
            )
            if not (c_s and a_s and cr_s):
                continue
            rd = session.post(
                f"https://ads.tiktok.com/api/v4/i18n/creation/snap/detail/?aadvid={aadvid}",
                headers=h,
                json={"campaign_snap_ids": [c_s], "ad_snap_ids": [a_s], "creative_snap_ids": [cr_s]},
                timeout=20,
            ).json().get("data") or {}
            c_f = (rd.get("campaign_snap_map") or {}).get(c_s) or {}
            a_f = (rd.get("ad_snap_map") or {}).get(a_s) or {}
            cr_f = (rd.get("asset_group_creative_snap_map") or {}).get(cr_s) or {}

            if base_c_f is None and c_f and a_f and cr_f:
                base_c_f, base_a_f, base_cr_f = c_f, a_f, cr_f
                base_ids = (c_s, a_s, cr_s)

            for im in cr_f.get("image_list") or []:
                mn = (im.get("material_name") or (im.get("video_info") or {}).get("file_name") or "").strip()
                vid = (im.get("video_info") or {}).get("video_id") or mn
                if vid in seen_vids:
                    continue
                for w in want_names:
                    if w.startswith("__"):
                        continue
                    w_clean = w.lower().replace(".mp4", "").replace(".mov", "")
                    mn_clean = mn.lower().replace(".mp4", "").replace(".mov", "")
                    if mn == w or mn_clean.startswith(w_clean) or w_clean in mn_clean:
                        collected_imgs.append(im)
                        seen_vids.add(vid)
                        break
        except Exception as e:
            print(f"  [WARN] extract_winning_assets_from_campaigns ({cid}): {e}")

    # Fallback: Look up any remaining requested videos directly in the account's Material Library
    all_wanted = [vname for _, vname in donor_specs if not vname.startswith("__")]
    found_names_lower = {
        ((im.get("material_name") or (im.get("video_info") or {}).get("file_name") or "").strip().lower().replace(".mp4", ""))
        for im in collected_imgs
    }
    missing_wanted = [
        w for w in all_wanted
        if not any(w.lower().replace(".mp4", "") in fn or fn.startswith(w.lower().replace(".mp4", "")) for fn in found_names_lower)
    ]
    if missing_wanted and base_cr_f is not None:
        try:
            lib_vids = []
            for pg in range(1, 5):
                rl = session.get(
                    f"https://ads.tiktok.com/api/v2/i18n/material/video/list/?aadvid={aadvid}&page={pg}&limit=100",
                    headers=h,
                    timeout=15,
                ).json()
                vlist = (rl.get("data") or {}).get("video_list") or []
                lib_vids.extend(vlist)
                if len(vlist) < 100:
                    break
            id_type = base_cr_f.get("identity_type") or 5
            id_id = base_cr_f.get("identity_id") or "d83712c2-82df-5ba7-87fe-06331d89448c"
            id_bc = base_cr_f.get("identity_bc_id") or "7631763526647595025"
            for w in missing_wanted:
                w_clean = w.lower().replace(".mp4", "").replace(".mov", "")
                for v in lib_vids:
                    v_id = v.get("vid")
                    if not v_id or v_id in seen_vids:
                        continue
                    v_nm = (v.get("name") or v.get("file_name") or "").strip()
                    v_clean = v_nm.lower().replace(".mp4", "").replace(".mov", "")
                    if v_nm == w or v_clean.startswith(w_clean) or w_clean in v_clean:
                        seen_vids.add(v_id)
                        mat_nm = v_nm if v_nm.endswith(".mp4") else f"{v_nm}.mp4"
                        collected_imgs.append({
                            "image_mode": 15,
                            "image_info": [{
                                "web_uri": v.get("cover_uri") or v.get("poster_url") or FALLBACK_COVER,
                                "url": "",
                                "file_name": "",
                                "width": 720,
                                "height": 1280,
                            }],
                            "video_info": {
                                "video_id": v_id,
                                "file_name": mat_nm,
                                "mid": "0",
                            },
                            "material_name": mat_nm,
                            "identity_type": id_type,
                            "identity_id": id_id,
                            "agr_material_type": 0,
                            "identity_bc_id": id_bc,
                            "media_tag": 0,
                            "item_source": 3,
                            "media_recall_source": 0,
                        })
                        break
        except Exception as e_lib:
            print(f"  [WARN] Material library fallback error ({aadvid}): {e_lib}")

    return base_ids, base_c_f, base_a_f, base_cr_f, collected_imgs


def launch_leads_winner_experiment(
    session,
    headers,
    aadvid: str,
    acc_name: str,
    new_camp_name: str,
    donor_specs: list[tuple[str, str]],
    cpa_bid: str = "6.3",
    budget: str = "50",
    deep_funnel: bool = False,
    tiktok_only_placement: bool = True,
    use_leads_objective: bool = True,
    scheduled_start_time: str | None = None,
) -> tuple[bool, str, list[str]]:
    """
    Launches a strictly controlled experiment campaign (Target Cost smart_bid_type=0 ONLY)
    using ONLY 3 to 5 proven winning videos, and polls async_creation/detail to verify the new campaign_id.
    """
    base_ids, c_f, a_f, cr_f, imgs = extract_winning_assets_from_campaigns(
        session,
        headers,
        aadvid,
        ([(DONOR_TEMPLATES[aadvid], "__BASE_CONV_TEMPLATE__")] if (not use_leads_objective and aadvid in DONOR_TEMPLATES) else []) + list(donor_specs),
    )
    c_s, a_s, cr_s = base_ids
    if not (c_s and a_s and cr_s and c_f and a_f and cr_f):
        return False, "Missing base snap templates", []

    # Strictly enforce 3 to 5 proven winning videos per campaign
    imgs = imgs[:5]
    if len(imgs) < 3:
        return False, f"Only {len(imgs)} winning videos found (< 3 minimum required)", []

    selected_names = [
        (im.get("material_name") or (im.get("video_info") or {}).get("file_name") or "WinnerVideo")
        for im in imgs
    ]

    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/creation/1nn/create/campaign?aadvid={aadvid}"

    # 1. Configure Campaign Form: Target Cost (0), Controlled Budget
    c_f["campaign_name"] = new_camp_name
    if use_leads_objective:
        c_f["objective_type"] = 8
    c_f["budget"] = str(budget)
    c_f["cpa_bid"] = str(cpa_bid)
    c_f["campaign_bid_strategy_selection"] = 0
    r_c = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/creation/campaign_snap/save/?aadvid={aadvid}",
        headers=h,
        json={
            "campaign_sketch_form_data": c_f,
            "clear_lower_level_sketches": False,
            "with_sketch": False,
            "is_skip_check_fields": True,
            "risk_info": {},
        },
        timeout=20,
    ).json()
    if r_c.get("code") != 0:
        return False, f"campaign_snap/save failed: {r_c}", selected_names

    # 2. Configure AdGroup Form: Target Cost (smart_bid_type=0)
    now_utc2_exp = datetime.now(timezone.utc) + timedelta(hours=2)
    min_start_exp = (now_utc2_exp + timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:00")
    if scheduled_start_time and scheduled_start_time > min_start_exp:
        target_start_exp = scheduled_start_time
    elif now_utc2_exp.hour >= 17:  # >= 18:00 KSA (17:00 UTC+2) -> pre-schedule for tomorrow 06:00 AM KSA (05:00 UTC+2) so review finishes overnight
        target_start_exp = (now_utc2_exp + timedelta(days=1)).strftime("%Y-%m-%d 05:00:00")
    else:
        sched_6am_exp = now_utc2_exp.strftime("%Y-%m-%d 05:00:00")
        target_start_exp = sched_6am_exp if sched_6am_exp > min_start_exp else min_start_exp
    a_f["ad_name"] = f"{new_camp_name} - AdGroup"
    a_f["start_time"] = target_start_exp
    a_f["custom_tz_type"] = 0
    a_f.pop("custom_tz_id", None)
    if use_leads_objective:
        a_f["external_action"] = 2
        a_f["optimize_goal"] = 131
        a_f["promotion_target_type"] = 2
        a_f["external_type"] = 601
        if not deep_funnel or not a_f.get("lead_quality_ext"):
            a_f["deep_funnel_toggle"] = 3
            a_f.pop("lead_quality_ext", None)
    a_f["smart_bid_type"] = 0
    a_f["cpa_bid"] = str(cpa_bid)
    a_f["flow_control_mode"] = 1
    a_f["cpa_skip_first_phrase"] = 1
    a_f["is_comment_public_disable"] = 1
    if tiktok_only_placement:
        a_f["inventory_flow_type"] = 1
        a_f["inventory_flow"] = [3000]
    else:
        a_f["inventory_flow_type"] = 0
        a_f["inventory_flow"] = [3000, 9000, 1000]

    r_a = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/creation/ad_snap/save/?aadvid={aadvid}",
        headers=h,
        json={
            "ad_sketch_form_data": a_f,
            "campaign_id": "",
            "campaign_sketch_id": "",
            "campaign_snap_id": c_s,
            "clear_lower_level_sketches": False,
            "is_skip_check_fields": True,
            "spc_upgrade_mode": 1,
            "with_sketch": False,
            "risk_info": {},
        },
        timeout=20,
    ).json()
    if r_a.get("code") != 0:
        return False, f"ad_snap/save failed: {r_a}", selected_names

    # 3. Configure Creative Form: Attach ONLY the 3-5 proven winning videos & unify identity across all items
    id_type = cr_f.get("identity_type") or 5
    id_id = cr_f.get("identity_id") or "d83712c2-82df-5ba7-87fe-06331d89448c"
    id_bc = cr_f.get("identity_bc_id") or "7631763526647595025"
    if aadvid in ("7631763556322148369", "7631765139798081554", "7631765070816509959") or id_id == "089bde1c-687c-5b2b-9668-d1e410519145":
        id_type = 5
        id_id = "d83712c2-82df-5ba7-87fe-06331d89448c"
        id_bc = "7631763526647595025"
    cr_f["identity_type"] = id_type
    cr_f["identity_id"] = id_id
    cr_f["identity_bc_id"] = id_bc
    for im in imgs:
        im["identity_type"] = id_type
        im["identity_id"] = id_id
        im["identity_bc_id"] = id_bc
        if isinstance(im.get("video_info"), dict) and im["video_info"].get("mid") not in (None, "0", 0):
            im["video_info"]["mid"] = "0"
    cr_f["creative_name"] = f"{new_camp_name} - Creative"
    cr_f["image_list"] = imgs
    is_car_exp = "car" in new_camp_name.lower() or "وسادة" in new_camp_name.lower()
    cr_f["title_list"] = [dict(t) for t in (TT_CAR_TITLES_ZERO_GIFT if is_car_exp else TT_PB_TITLES_ZERO_GIFT)]
    cr_f["external_url"] = TT_CAR_URL if is_car_exp else TT_PB_URL
    cr_f["auto_pull_by_destination_toggle"] = 0
    cr_f["auto_pull_by_aigc_toggle"] = 0
    cr_f["aigc_approval_auto_pull_toggle"] = 0
    cr_f["carousel_automation_toggle"] = 0
    r_cr = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/creation/creative_snap/save/?aadvid={aadvid}",
        headers=h,
        json={
            "asset_group_sketch_form_data_list": [cr_f],
            "ad_id": "",
            "ad_sketch_id": "",
            "ad_snap_id": a_s,
            "campaign_id": "",
            "campaign_sketch_id": "",
            "campaign_snap_id": c_s,
            "with_sketch": False,
            "risk_info": {},
        },
        timeout=20,
    ).json()
    if r_cr.get("code") != 0:
        return False, f"creative_snap/save failed: {r_cr}", selected_names

    # 4. Publish Campaign & Poll async_creation/detail to verify real campaign_id
    pub_h = dict(h)
    pub_h["x-ttam-deduptoken"] = str(uuid.uuid4())
    r_pub = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/creation/async_creation/create_by_snap/?aadvid={aadvid}",
        headers=pub_h,
        json={
            "campaign_id": "",
            "campaign_snap_id": c_s,
            "ad_and_creative_snap_info_list": [
                {
                    "ad_id": "",
                    "ad_snap_id": a_s,
                    "ad_sketch_id": "",
                    "creative_snap_info_list": [
                        {
                            "creative_id": "",
                            "creative_snap_id": cr_s,
                            "creative_sketch_id": "",
                            "need_publish": True,
                        }
                    ],
                    "need_publish": True,
                }
            ],
            "coming_source_type": 0,
            "sketch_publish_source": 0,
            "campaign_sketch_id": "",
            "is_partial_publish": False,
            "create_cta_id": True,
            "risk_info": {},
        },
        timeout=25,
    ).json()
    if r_pub.get("code") != 0:
        return False, f"create_by_snap failed: {r_pub}", selected_names

    async_id = (r_pub.get("data") or {}).get("async_request_id")
    cid_new = None
    last_res_obj = {}
    if async_id:
        for _ in range(18):
            time.sleep(1.5)
            try:
                r_det = session.get(
                    f"https://ads.tiktok.com/api/v4/i18n/creation/async_creation/detail/?aadvid={aadvid}&async_request_id={async_id}",
                    headers=h,
                    timeout=15,
                ).json()
                d = r_det.get("data") or {}
                if d.get("status") in (1, 2, 3):
                    last_res_obj = d.get("result") or {}
                    cid_new = last_res_obj.get("campaign_id") or (last_res_obj.get("success_campaign_ids") or [None])[0]
                    break
            except Exception:
                pass

    if cid_new:
        return True, str(cid_new), selected_names
    return False, f"async_creation did not return campaign_id: {last_res_obj}", selected_names


DONOR_TEMPLATES = {
    "7631763556322148369": "1880201842305441",  # ROELLINGER RECYCLING
    "7631765139798081554": "1879845869407457",  # ROELLINGER RECYCLING1
    "7631765070816509959": "1879846150126786",  # ROELLINGER RECYCLING2
}

FALLBACK_COVER = "tos-alisg-p-0051c001-sg/oYSBxFBNQDqb0UDUdgm4A9SxEffBBKBElcI5nA"
TT_PB_URL = "https://www.daralrimal.shop/rluajrRjM/split?utm_source=tiktok&utm_id=__CAMPAIGN_ID__&utm_campaign=__CAMPAIGN_NAME__&utm_medium=paid"
TT_PB_TITLES_ZERO_GIFT = [
    {"title": "الطلب مرتفع والكمية محدودة جداً", "creative_auto_add_toggle": 1, "ad_text_source": 3},
    {"title": "الطلب مرتفع والكمية محدودة جداً - اطلبه الحين قبل نفاد المخزون", "creative_auto_add_toggle": 1, "ad_text_source": 3},
    {"title": "وداعاً لزحمة الأسلاك وانقطاع الشحن بالطلعات والسفر 👌 اطلبه الحين مع توصيل مجاني والدفع عند الاستلام", "creative_auto_add_toggle": 1, "ad_text_source": 3},
    {"title": "الباور بانك الأكثر مبيعاً في المتجر مع توصيل مجاني وضمان استرجاع", "creative_auto_add_toggle": 1, "ad_text_source": 3},
    {"title": "خلي جوالك دايماً مشحون في كل مكان ⚡ شحن سريع 20000 ملي أمبير وأسلاك مدمجة وحجم مربع يدخل الجيب", "creative_auto_add_toggle": 1, "ad_text_source": 3},
]
TT_CAR_URL = "https://www.daralrimal.org/tvfAkFHdl/split?utm_source=tiktok&utm_id=__CAMPAIGN_ID__&utm_campaign=__CAMPAIGN_NAME__&utm_medium=paid"
TT_CAR_TITLES_ZERO_GIFT = [
    {"title": "الطلب مرتفع والكمية محدودة جداً", "creative_auto_add_toggle": 1, "ad_text_source": 3},
    {"title": "الطلب مرتفع والكمية محدودة جداً - اطلبها الحين قبل نفاد المخزون", "creative_auto_add_toggle": 1, "ad_text_source": 3},
    {"title": "وداعاً لتعب الكوع والكتف بالزحمة والسفر 👌 وسادة ميموري فوم أصلية مع توصيل مجاني والدفع عند الاستلام", "creative_auto_add_toggle": 1, "ad_text_source": 3},
    {"title": "وسادة تكاية السيارة الأكثر مبيعاً في المتجر مع توصيل مجاني وضمان استرجاع 30 يوم", "creative_auto_add_toggle": 1, "ad_text_source": 3},
    {"title": "ركبها بـ 10 ثواني بدون أدوات واستمتع براحة فندقية وعلبة مناديل وجيبين جانبية للجوال", "creative_auto_add_toggle": 1, "ad_text_source": 3},
]


def _sign_aws4_vod(method, path, params, headers_to_sign, body_bytes, ak, sk, session_token, region="us-east-1", service="vod"):
    import urllib.parse
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    req_headers = dict(headers_to_sign)
    req_headers["X-Amz-Date"] = amz_date
    req_headers["x-amz-security-token"] = session_token
    body_hash = hashlib.sha256(body_bytes or b"").hexdigest()
    if body_bytes:
        req_headers["X-Amz-Content-Sha256"] = body_hash

    sorted_keys = sorted(params.keys())
    canonical_qs = "&".join(
        f"{urllib.parse.quote(str(k), safe='-_.~')}={urllib.parse.quote(str(params[k]), safe='-_.~')}"
        for k in sorted_keys
    )
    unsignable = {"authorization", "content-type", "content-length", "user-agent", "presigned-expires", "expect", "x-amzn-trace-id"}
    signable = {}
    for k, v in req_headers.items():
        kl = k.lower()
        if kl.startswith("x-amz-") or kl not in unsignable:
            signable[kl] = " ".join(str(v).strip().split())
    sorted_h_keys = sorted(signable.keys())
    canonical_headers = "\n".join(f"{k}:{signable[k]}" for k in sorted_h_keys) + "\n"
    signed_headers = ";".join(sorted_h_keys)

    canonical_request = "\n".join([method.upper(), path, canonical_qs, canonical_headers, signed_headers, body_hash])
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    def _hmac(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k_signing = _hmac(_hmac(_hmac(_hmac(("AWS4" + sk).encode("utf-8"), date_stamp), region), service), "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    req_headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={ak}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"
    )
    return req_headers, canonical_qs


def upload_videos_to_tiktok_account(session, headers, aadvid: str, acc_name: str, video_files: list[tuple[Path, str]]) -> list[dict]:
    """Uploads 3 to 5 MP4 files to TikTok Ads Creative Library via v4 get_video_upload_token + vod-upload-sg.tiktok.com."""
    import random
    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/creation/1nn/create/campaign?aadvid={aadvid}"

    rt = session.get(
        f"https://ads.tiktok.com/api/v4/i18n/creation/material/video/get_video_upload_token/?aadvid={aadvid}",
        headers=h,
        timeout=15,
    )
    sts = json.loads(rt.json()["data"]["token"])
    ak = sts.get("AccessKeyID") or sts.get("AccessKeyId")
    sk = sts["SecretAccessKey"]
    st = sts["SessionToken"]

    r_lib_list = session.get(
        f"https://ads.tiktok.com/api/v2/i18n/material/video/list/?aadvid={aadvid}&page=1&limit=100",
        headers=h,
        timeout=20,
    ).json()
    existing_by_name = {}
    for v in r_lib_list.get("data", {}).get("video_list", []):
        for k in (v.get("name"), v.get("file_name"), f"{v.get('name')}.mp4"):
            if k and v.get("vid"):
                existing_by_name[k] = v

    uploaded = []
    for idx, (local_path, mat_name) in enumerate(video_files, 1):
        if mat_name in existing_by_name and existing_by_name[mat_name].get("vid"):
            v = existing_by_name[mat_name]
            print(f"  ✔️ [{acc_name} {idx:02d}/{len(video_files):02d}] Already in library: {mat_name} (vid={v['vid']})", flush=True)
            uploaded.append({
                "idx": idx,
                "vid": v["vid"],
                "name": mat_name,
                "cover_uri": v.get("cover_uri") or v.get("poster_url") or FALLBACK_COVER,
                "width": 720,
                "height": 1280,
                "mid": str(v.get("video_mid") or v.get("mid") or "0"),
            })
            continue

        file_bytes = local_path.read_bytes()
        crc_val = f"{zlib.crc32(file_bytes) & 0xffffffff:08x}"
        for attempt in range(4):
            try:
                print(f"  📤 [{acc_name} {idx:02d}/{len(video_files):02d}] Uploading {mat_name} ({len(file_bytes) // (1024*1024)} MB)...", flush=True)
                params = {
                    "Action": "ApplyUploadInner",
                    "Version": "2020-11-19",
                    "SpaceName": "ad_site",
                    "FileType": "video",
                    "IsInner": "1",
                    "FileSize": str(len(file_bytes)),
                    "s": f"n5pb31{idx}r{random.randint(1000,9999)}",
                }
                sig_headers, qs = _sign_aws4_vod("GET", "/", params, {}, b"", ak, sk, st)
                r_apply = requests.get(f"https://vod-upload-sg.tiktok.com/?{qs}", headers=sig_headers, timeout=25).json()
                node = r_apply["Result"]["InnerUploadAddress"]["UploadNodes"][0]
                store_info = node["StoreInfos"][0]
                vid = node["Vid"]

                r_tos = requests.post(
                    f"https://{node['UploadHost']}/upload/v1/{store_info['StoreUri']}",
                    headers={
                        "Authorization": store_info["Auth"],
                        "Content-CRC32": crc_val,
                        "Content-Type": "application/octet-stream",
                    },
                    data=file_bytes,
                    timeout=180,
                )
                r_tos.raise_for_status()

                commit_params = {"Action": "CommitUploadInner", "Version": "2020-11-19", "SpaceName": "ad_site"}
                commit_body = json.dumps({
                    "SessionKey": node["SessionKey"],
                    "Functions": [
                        {"name": "Snapshot", "input": {"SnapshotTime": 0}},
                        {"name": "StartWorkflow", "input": {"TemplateId": "c3841b3122fd460db2bc99a6ec131cb8"}},
                    ],
                }).encode("utf-8")
                c_headers, c_qs = _sign_aws4_vod("POST", "/", commit_params, {"Content-Type": "application/json"}, commit_body, ak, sk, st)
                r_commit = requests.post(f"https://vod-upload-sg.tiktok.com/?{c_qs}", headers=c_headers, data=commit_body, timeout=25).json()
                res0 = r_commit["Result"]["Results"][0]
                poster_uri = res0.get("PosterUri") or FALLBACK_COVER

                r_lib = session.post(
                    f"https://ads.tiktok.com/mi/api/v3/i18n/material/video/create/?aadvid={aadvid}",
                    headers=h,
                    json={"vid": vid, "name": mat_name, "status": 0, "originalVid": vid},
                    timeout=20,
                ).json()
                mid = str(r_lib.get("data", {}).get("video_info", {}).get("video_mid") or "0")
                print(f"  ✅ [{acc_name} {idx:02d}/{len(video_files):02d}] Uploaded {mat_name} -> vid={vid}, mid={mid}", flush=True)
                uploaded.append({
                    "idx": idx,
                    "vid": vid,
                    "name": mat_name,
                    "cover_uri": poster_uri,
                    "width": 720,
                    "height": 1280,
                    "mid": mid,
                })
                break
            except Exception as e:
                print(f"  ⚠️ [{acc_name} {idx:02d}/{len(video_files):02d}] Retry {attempt+1}/4 after error: {e}", flush=True)
                time.sleep(2.5)
        else:
            raise RuntimeError(f"Failed to upload TikTok video {mat_name} on {acc_name}")

    time.sleep(2.5)
    r_lib2 = session.get(
        f"https://ads.tiktok.com/api/v2/i18n/material/video/list/?aadvid={aadvid}&page=1&limit=100",
        headers=h,
        timeout=20,
    ).json()
    by_vid = {v.get("vid"): v for v in r_lib2.get("data", {}).get("video_list", []) if v.get("vid")}
    for u in uploaded:
        v_fresh = by_vid.get(u["vid"], {})
        if v_fresh.get("cover_uri"):
            u["cover_uri"] = v_fresh["cover_uri"]
        if v_fresh.get("video_mid") or v_fresh.get("mid"):
            u["mid"] = str(v_fresh.get("video_mid") or v_fresh.get("mid"))
    return uploaded


def launch_new_video_batch_campaign(
    session,
    headers,
    aadvid: str,
    acc_name: str,
    new_camp_name: str,
    video_files: list[tuple[Path, str]],
    cpa_bid: str = "6.5",
    budget: str = "60",
    scheduled_start_time: str = "2026-10-31 05:00:00",
    tiktok_only_placement: bool = True,
    product_type: str = "pb",
    use_leads_objective: bool = False,
) -> tuple[bool, str, list[str]]:
    """
    Uploads 3 to 5 new videos via VOD and creates a scheduled Smart+ Website Conversions campaign
    using the exact v4 campaign_snap/copy -> create_by_snap architecture of the Hourly TikTok Automation:
    - 3 to 5 videos per campaign
    - Target Cost (smart_bid_type=0, cpa_bid=$6.50, budget=$60)
    - TikTok In-Feed Only placement isolation (inventory_flow_type=1, inventory_flow=[3000])
    - Public comments disabled (is_comment_public_disable=1)
    - All AIGC/auto-pull toggles disabled (0)
    - Zero gift mention in titles (TT_PB_TITLES_ZERO_GIFT)
    - Scheduled for tomorrow 06:00 AM KSA ("2026-10-31 05:00:00" UTC+2)
    """
    import copy
    if not (3 <= len(video_files) <= 5):
        return False, f"Must have 3 to 5 videos (got {len(video_files)})", []

    donor_camp_id = DONOR_TEMPLATES.get(aadvid)
    if not donor_camp_id:
        return False, f"No donor template for {aadvid}", []

    # Verify campaign doesn't already exist active/scheduled in account
    try:
        today_d = datetime.now().strftime("%Y-%m-%d")
        for c_existing in tm.fetch_campaigns(session, headers, aadvid, today_d, today_d):
            if (c_existing.get("campaign_name") or "").strip() == new_camp_name.strip():
                if str(c_existing.get("campaign_opt_status", c_existing.get("opt_status", "1"))) == "0":
                    return True, str(c_existing.get("campaign_id")), [u for _, u in video_files]
    except Exception:
        pass

    uploaded_5 = upload_videos_to_tiktok_account(session, headers, aadvid, acc_name, video_files)
    uploaded_names = [u["name"] for u in uploaded_5]

    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/creation/1nn/create/campaign?aadvid={aadvid}"

    rc_resp = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/creation/campaign_snap/copy/?aadvid={aadvid}",
        headers=h,
        json={
            "campaign_id": donor_camp_id,
            "name": new_camp_name,
            "with_sketch": False,
            "with_ad": True,
            "with_creative": True,
            "risk_info": {},
        },
        timeout=20,
    ).json()
    rc = rc_resp.get("data") or {}
    if not rc:
        return False, f"campaign_snap/copy failed: {rc_resp}", uploaded_names

    c_snap_id = rc["new_campaign_snap_info_item"]["campaign_snap_id"]
    a_snap_id = rc["new_ad_snap_info_item_list"][0]["ad_snap_id"]
    cr_list = list(rc["new_ad_and_creative_snap_info_item_map"].values())[0]
    cr_snap_id = cr_list[0]["creative_snap_id"] if isinstance(cr_list[0], dict) else str(cr_list[0])

    rd = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/creation/snap/detail/?aadvid={aadvid}",
        headers=h,
        json={"campaign_snap_ids": [c_snap_id], "ad_snap_ids": [a_snap_id], "creative_snap_ids": [cr_snap_id]},
        timeout=20,
    ).json().get("data") or {}

    c_form = rd["campaign_snap_map"][c_snap_id]
    a_form = rd["ad_snap_map"][a_snap_id]
    cr_form = rd["asset_group_creative_snap_map"][cr_snap_id]

    id_type = cr_form.get("identity_type") or 5
    id_id = cr_form.get("identity_id") or "d83712c2-82df-5ba7-87fe-06331d89448c"
    id_bc = cr_form.get("identity_bc_id") or "7631763526647595025"
    if aadvid in ("7631763556322148369", "7631765139798081554", "7631765070816509959") or id_id == "089bde1c-687c-5b2b-9668-d1e410519145":
        id_type = 5
        id_id = "d83712c2-82df-5ba7-87fe-06331d89448c"
        id_bc = "7631763526647595025"

    img_list = []
    for u in uploaded_5:
        img_list.append({
            "image_mode": 15,
            "image_info": [{
                "web_uri": u["cover_uri"] or FALLBACK_COVER,
                "url": "",
                "file_name": "",
                "width": 720,
                "height": 1280,
            }],
            "video_info": {
                "video_id": u["vid"],
                "file_name": u["name"],
                "mid": "0",
            },
            "material_name": u["name"],
            "identity_type": id_type,
            "identity_id": id_id,
            "agr_material_type": 0,
            "identity_bc_id": id_bc,
            "media_tag": 0,
            "item_source": 3,
            "media_recall_source": 0,
        })

    c_form["campaign_name"] = new_camp_name
    if use_leads_objective:
        c_form["objective_type"] = 8
    c_form["budget"] = str(budget)
    c_form["cpa_bid"] = str(cpa_bid)
    c_form["campaign_bid_strategy_selection"] = 0
    r_c = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/creation/campaign_snap/save/?aadvid={aadvid}",
        headers=h,
        json={
            "campaign_sketch_form_data": c_form,
            "clear_lower_level_sketches": False,
            "with_sketch": False,
            "is_skip_check_fields": True,
            "risk_info": {},
        },
        timeout=20,
    ).json()
    if r_c.get("code") != 0:
        return False, f"campaign_snap/save failed: {r_c}", uploaded_names

    now_utc2 = datetime.now(timezone.utc) + timedelta(hours=2)
    min_start = (now_utc2 + timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:00")
    a_form["ad_name"] = f"{new_camp_name} - AdSet 1"
    a_form["start_time"] = scheduled_start_time if scheduled_start_time > min_start else min_start
    a_form["custom_tz_type"] = 0
    a_form.pop("custom_tz_id", None)
    if use_leads_objective:
        a_form["external_action"] = 2
        a_form["optimize_goal"] = 131
        a_form["promotion_target_type"] = 2
        a_form["external_type"] = 601
        a_form["deep_funnel_toggle"] = 3
        a_form.pop("lead_quality_ext", None)
    if tiktok_only_placement:
        a_form["inventory_flow_type"] = 1
        a_form["inventory_flow"] = [3000]  # TikTok In-Feed Only
    else:
        a_form["inventory_flow_type"] = 0
        a_form["inventory_flow"] = [3000, 9000, 1000]  # Automatic Placements (TikTok + Global App Bundle + Pangle)
    a_form["is_comment_public_disable"] = 1
    a_form["smart_bid_type"] = 0  # Target Cost (Cost Cap)
    a_form["cpa_bid"] = str(cpa_bid)
    a_form["flow_control_mode"] = 1
    a_form["cpa_skip_first_phrase"] = 1

    r_a = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/creation/ad_snap/save/?aadvid={aadvid}",
        headers=h,
        json={
            "ad_sketch_form_data": a_form,
            "campaign_id": "",
            "campaign_sketch_id": "",
            "campaign_snap_id": c_snap_id,
            "clear_lower_level_sketches": False,
            "is_skip_check_fields": True,
            "spc_upgrade_mode": 1,
            "with_sketch": False,
            "risk_info": {},
        },
        timeout=20,
    ).json()
    if r_a.get("code") != 0:
        return False, f"ad_snap/save failed: {r_a}", uploaded_names

    cr_form["creative_name"] = f"{new_camp_name} - Creative"
    cr_form["identity_type"] = id_type
    cr_form["identity_id"] = id_id
    cr_form["identity_bc_id"] = id_bc
    cr_form["image_list"] = copy.deepcopy(img_list)
    cr_form["title_list"] = copy.deepcopy(TT_CAR_TITLES_ZERO_GIFT if product_type == "car" else TT_PB_TITLES_ZERO_GIFT)
    cr_form["external_url"] = TT_CAR_URL if product_type == "car" else TT_PB_URL
    cr_form["auto_pull_by_destination_toggle"] = 0
    cr_form["auto_pull_by_aigc_toggle"] = 0
    cr_form["aigc_approval_auto_pull_toggle"] = 0
    cr_form["carousel_automation_toggle"] = 0

    r_cr = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/creation/creative_snap/save/?aadvid={aadvid}",
        headers=h,
        json={
            "asset_group_sketch_form_data_list": [cr_form],
            "ad_id": "",
            "ad_sketch_id": "",
            "ad_snap_id": a_snap_id,
            "campaign_id": "",
            "campaign_sketch_id": "",
            "campaign_snap_id": c_snap_id,
            "with_sketch": False,
            "risk_info": {},
        },
        timeout=20,
    ).json()
    if r_cr.get("code") != 0:
        return False, f"creative_snap/save failed: {r_cr}", uploaded_names

    pub_headers = dict(h)
    pub_headers["x-ttam-deduptoken"] = str(uuid.uuid4())
    r_pub = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/creation/async_creation/create_by_snap/?aadvid={aadvid}",
        headers=pub_headers,
        json={
            "campaign_id": "",
            "campaign_snap_id": c_snap_id,
            "ad_and_creative_snap_info_list": [{
                "ad_id": "",
                "ad_snap_id": a_snap_id,
                "ad_sketch_id": "",
                "creative_snap_info_list": [{
                    "creative_id": "",
                    "creative_snap_id": cr_snap_id,
                    "creative_sketch_id": "",
                    "need_publish": True,
                }],
                "need_publish": True,
            }],
            "coming_source_type": 0,
            "sketch_publish_source": 0,
            "campaign_sketch_id": "",
            "is_partial_publish": False,
            "create_cta_id": True,
            "risk_info": {},
        },
        timeout=25,
    ).json()
    if r_pub.get("code") != 0:
        return False, f"create_by_snap failed: {r_pub}", uploaded_names

    async_id = (r_pub.get("data") or {}).get("async_request_id")
    cid_new = None
    last_res_obj = {}
    if async_id:
        for _ in range(25):
            time.sleep(1.5)
            try:
                r_det = session.get(
                    f"https://ads.tiktok.com/api/v4/i18n/creation/async_creation/detail/?aadvid={aadvid}&async_request_id={async_id}",
                    headers=h,
                    timeout=15,
                ).json()
                d = r_det.get("data") or {}
                if d.get("status") in (1, 2, 3):
                    last_res_obj = d.get("result") or {}
                    cid_new = last_res_obj.get("campaign_id") or (last_res_obj.get("success_campaign_ids") or [None])[0]
                    break
            except Exception:
                pass

    if cid_new:
        return True, str(cid_new), uploaded_names
    return False, f"async_creation did not return campaign_id: {last_res_obj}", uploaded_names


def update_campaign_budget_in_place(session, headers, aadvid: str, camp_id: str, fallback_name: str, new_budget: int) -> tuple[bool, float]:
    """Updates a TikTok campaign daily budget in place via /api/v3/i18n/perf/campaign/update/."""
    try:
        h_b = dict(headers)
        h_b["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
        det_r = session.get(
            f"https://ads.tiktok.com/api/v3/i18n/perf/campaign/detail/?aadvid={aadvid}&campaign_id={camp_id}",
            headers=h_b,
            timeout=15,
        ).json().get("data") or {}
        if not det_r:
            return False, 0.0
        old_bdg = float(det_r.get("budget") or 50.0)
        up_payload = {
            **det_r,
            "campaign_id": str(camp_id),
            "campaign_name": det_r.get("name") or fallback_name,
            "budget": float(new_budget),
        }
        rb_resp = session.post(
            f"https://ads.tiktok.com/api/v3/i18n/perf/campaign/update/?aadvid={aadvid}",
            headers=h_b,
            json=up_payload,
            timeout=15,
        )
        if rb_resp.text.strip() and rb_resp.json().get("code") == 0:
            return True, old_bdg
    except Exception:
        pass
    return False, 0.0


def main():
    now_sa = datetime.now(SA_TZ)
    print(f"======================================================================", flush=True)
    print(f"🎯 [TikTok Hourly Autonomous Sales Master] الدورة الحالية: {now_sa:%Y-%m-%d %H:%M} (GMT+3)", flush=True)
    print(f"📌 مبدأ التشغيل: زيادة عدد المبيعات في الحملات الفائزة + ابتكار وتجريب طرق بيع جديدة كل ساعة بدون خسارة رأس المال", flush=True)
    print(f"======================================================================", flush=True)

    # Step 1: Run standard tiktok_monitor first so video-level CPC ($1.00 / $0.25) and duplicated video rules run
    try:
        tm.main()
    except Exception as e_tm:
        print(f"⚠️ تنبيه أثناء تشغيل tiktok_monitor الأساسي: {e_tm}", flush=True)

    cookies_raw = tm.load_cookies()
    session, headers = tm.build_session(cookies_raw)
    history = tm.load_history()
    ledger = load_ledger()
    sheet_sales = fetch_sheet_tiktok_sales()

    account_snapshots = []
    campaign_rows = []
    ledger_followups = []
    new_actions_taken = []
    scaling_opportunities_evaluated = []

    today_s = now_sa.strftime("%Y-%m-%d")
    is_scaling_window = (6 <= now_sa.hour < 17)

    live_camp_metrics = {}
    active_exp_per_account = defaultdict(int)

    for acc_name, aadvid, utc_offset in tm.ACCOUNTS:
        now_acc = datetime.now(timezone(timedelta(hours=utc_offset)))
        t_str = now_acc.strftime("%Y-%m-%d")
        y_str = (now_acc - timedelta(days=1)).strftime("%Y-%m-%d")
        two_str = (now_acc - timedelta(days=2)).strftime("%Y-%m-%d")
        six_str = (now_acc - timedelta(days=6)).strftime("%Y-%m-%d")
        fourteen_str = (now_acc - timedelta(days=14)).strftime("%Y-%m-%d")

        c_1d = {str(c.get("campaign_id")): c for c in tm.fetch_campaigns(session, headers, aadvid, t_str, t_str) if c.get("campaign_id")}
        c_yest = {str(c.get("campaign_id")): c for c in tm.fetch_campaigns(session, headers, aadvid, y_str, y_str) if c.get("campaign_id")}
        c_2d = {str(c.get("campaign_id")): c for c in tm.fetch_campaigns(session, headers, aadvid, y_str, t_str) if c.get("campaign_id")}
        c_3d = {str(c.get("campaign_id")): c for c in tm.fetch_campaigns(session, headers, aadvid, two_str, t_str) if c.get("campaign_id")}
        c_7d = {str(c.get("campaign_id")): c for c in tm.fetch_campaigns(session, headers, aadvid, six_str, t_str) if c.get("campaign_id")}
        c_14d = {str(c.get("campaign_id")): c for c in tm.fetch_campaigns(session, headers, aadvid, fourteen_str, t_str) if c.get("campaign_id")}

        ads_3d = tm.fetch_ads(session, headers, aadvid, two_str, t_str)
        cids_by_camp = defaultdict(list)
        for a in ads_3d:
            if a.get("campaign_id") and a.get("creative_id"):
                cids_by_camp[str(a.get("campaign_id"))].append(str(a.get("creative_id")))

        acc_sp1, acc_cv1 = 0.0, 0
        acc_spy, acc_cvy = 0.0, 0
        acc_sp3, acc_cv3 = 0.0, 0
        acc_sp7, acc_cv7 = 0.0, 0
        acc_sp14, acc_cv14 = 0.0, 0

        saudi_only_cids = [
            cid_s for cid_s in c_14d
            if not tm.is_gulf_campaign(((c_14d.get(cid_s) or {}).get("campaign_name") or ""))
        ]
        pre_acc_sp1 = sum(float(((c_1d.get(cid) or {}).get("row_data") or {}).get("stat_cost") or 0.0) for cid in saudi_only_cids)
        pre_acc_cv1 = sum(int(float(((c_1d.get(cid) or {}).get("row_data") or {}).get("time_attr_convert_cnt") or 0)) for cid in saudi_only_cids)
        pre_acc_cpa1 = (pre_acc_sp1 / pre_acc_cv1) if pre_acc_cv1 > 0 else 999.0
        pre_acc_sp3 = sum(float(((c_3d.get(cid) or {}).get("row_data") or {}).get("stat_cost") or 0.0) for cid in saudi_only_cids)
        pre_acc_cv3 = sum(int(float(((c_3d.get(cid) or {}).get("row_data") or {}).get("time_attr_convert_cnt") or 0)) for cid in saudi_only_cids)
        pre_acc_cpa3 = (pre_acc_sp3 / pre_acc_cv3) if pre_acc_cv3 > 0 else 999.0
        account_is_bleeding = (
            (pre_acc_sp1 >= ACCOUNT_BLEED_SPEND_THRESHOLD and pre_acc_cpa1 > ACCOUNT_BLEED_MAX_CPA)
            or (pre_acc_sp3 >= 18.00 and pre_acc_cpa3 > ACCOUNT_BLEED_MAX_CPA and pre_acc_cpa1 > 7.50)
        )

        for cid, c14_obj in c_14d.items():
            rd1 = (c_1d.get(cid) or {}).get("row_data") or {}
            rdy = (c_yest.get(cid) or {}).get("row_data") or {}
            rd2 = (c_2d.get(cid) or {}).get("row_data") or {}
            rd3 = (c_3d.get(cid) or {}).get("row_data") or {}
            rd7 = (c_7d.get(cid) or {}).get("row_data") or {}
            rd14 = c14_obj.get("row_data") or {}

            sp1 = float(rd1.get("stat_cost") or 0.0)
            cv1 = int(float(rd1.get("time_attr_convert_cnt") or 0))
            spy = float(rdy.get("stat_cost") or 0.0)
            cvy = int(float(rdy.get("time_attr_convert_cnt") or 0))
            sp2 = float(rd2.get("stat_cost") or 0.0)
            cv2 = int(float(rd2.get("time_attr_convert_cnt") or 0))
            sp3 = float(rd3.get("stat_cost") or 0.0)
            cv3 = int(float(rd3.get("time_attr_convert_cnt") or 0))
            sp7 = float(rd7.get("stat_cost") or 0.0)
            cv7 = int(float(rd7.get("time_attr_convert_cnt") or 0))
            sp14 = float(rd14.get("stat_cost") or 0.0)
            cv14 = int(float(rd14.get("time_attr_convert_cnt") or 0))

            acc_sp1 += sp1
            acc_cv1 += cv1
            acc_spy += spy
            acc_cvy += cvy
            acc_sp3 += sp3
            acc_cv3 += cv3
            acc_sp7 += sp7
            acc_cv7 += cv7
            acc_sp14 += sp14
            acc_cv14 += cv14

            cname = (c14_obj.get("campaign_name") or "").strip()
            is_active = c14_obj.get("primary_status") not in ("disable", "delete") and str(c14_obj.get("opt_status")) == "0"
            bdg = float(rd1.get("budget") or c14_obj.get("budget") or 0.0)
            cpc1 = float(rd1.get("cpc") or 0.0)
            clk1 = int(float(rd1.get("click_cnt") or 0))

            live_camp_metrics[f"{aadvid}:{cid}"] = {
                "acc_name": acc_name,
                "aadvid": aadvid,
                "campaign_id": cid,
                "campaign_name": cname,
                "is_active": is_active,
                "budget": bdg,
                "sp1": sp1, "cv1": cv1, "cpc1": cpc1, "clk1": clk1,
                "spy": spy, "cvy": cvy,
                "sp2": sp2, "cv2": cv2,
                "sp3": sp3, "cv3": cv3,
                "sp7": sp7, "cv7": cv7,
                "sp14": sp14, "cv14": cv14,
            }
            live_camp_metrics[f"{aadvid}:NAME:{cname}"] = live_camp_metrics[f"{aadvid}:{cid}"]

            # حملات الخليج (الكويت، قطر، عمان) مفصولة 100% وتخضع حصرياً لـ gulf_2h_profit_and_ads_master.py
            if tm.is_gulf_campaign(cname):
                if is_active or sp1 > 0 or spy > 0 or sp3 > 0:
                    campaign_rows.append(live_camp_metrics[f"{aadvid}:{cid}"])
                continue

            is_exp = "[exp-leads]" in cname.lower() or "[exp-" in cname.lower() or "exp leads" in cname.lower()

            if is_active:
                # 1) Clean up redundant $0-spend duplicate campaigns created earlier so they don't split auction power
                if cid in ("1880388640995346", "1880390201069217") and sp1 < 0.20 and cv1 == 0:
                    ok_p, _ = tm.pause_campaign(session, headers, aadvid, cid, operation="disable")
                    if ok_p:
                        is_active = False
                        live_camp_metrics[f"{aadvid}:{cid}"]["is_active"] = False
                        msg = f"إيقاف الحملة المكررة الصفرية '{cname}' ({cid}) في {acc_name} لمنع تشتيت المزاد عن الحملة الأساسية الرابحة."
                        new_actions_taken.append(msg)
                        print(f"  🧹 [CLEANUP] {msg}", flush=True)

            # ==================================================================
            # SALES BOOSTING METHOD A1: Auto-Revive Choked Multi-Sale 3D Winners
            # ("يعمل على زيادة عدد المبيعات في الحملات")
            # If a proven core campaign has >= 3 sales in 3D at CPA <= $7.00, was paused right after midnight
            # with Today spend < $4.00, revive its profitable winning videos and re-enable the campaign!
            # ==================================================================
            cpa3_val = (sp3 / cv3) if cv3 > 0 else None
            revive_key = f"{today_s}:{aadvid}:{cid}"
            if (
                not is_active
                and not is_exp
                and is_scaling_window
                and not tm.extract_new_product_key_tt(cname)
                and not tm.is_cross_monitored_product_campaign(cname)
                and not tm.is_banned_pillow_tiktok(cname)
                and "kashaf" not in cname.lower()
                and "كشاف" not in cname
                and cv3 >= 3
                and cpa3_val is not None
                and cpa3_val <= 7.00
                and sp1 < 1.00
                and revive_key not in ledger["revived_campaigns"]
            ):
                cr_ids_rev = cids_by_camp.get(cid, [])
                if cr_ids_rev:
                    try:
                        mats_1d_r = {
                            f"{m.get('ad_id')}:{m.get('ad_material_draft_id')}:{m.get('main_entity_id')}": m
                            for m in tm.fetch_expanded_materials(session, headers, aadvid, cr_ids_rev, t_str, t_str)
                        }
                        mats_3d_r = tm.fetch_expanded_materials(session, headers, aadvid, cr_ids_rev, two_str, t_str)
                        revived_vids = []
                        for mr3 in mats_3d_r:
                            k_r = f"{mr3.get('ad_id')}:{mr3.get('ad_material_draft_id')}:{mr3.get('main_entity_id')}"
                            mr1 = mats_1d_r.get(k_r, {})
                            v_sp1 = float(mr1.get("stat_cost") or 0.0)
                            v_cv1 = int(float(mr1.get("time_attr_convert_cnt") or 0))
                            v_cl1 = int(float(mr1.get("click_cnt") or 0))
                            v_cpc1 = (v_sp1 / v_cl1) if v_cl1 > 0 else float(mr1.get("cpc") or 0.0)
                            v_sp3 = float(mr3.get("stat_cost") or 0.0)
                            v_cv3 = int(float(mr3.get("time_attr_convert_cnt") or 0))
                            v_cl3 = int(float(mr3.get("click_cnt") or 0))
                            v_cpc3 = (v_sp3 / v_cl3) if v_cl3 > 0 else float(mr3.get("cpc") or 0.0)
                            v_cpa3 = (v_sp3 / v_cv3) if v_cv3 > 0 else 999.0
                            v_good_cpc_1d_35 = (v_sp1 >= 1.00 and v_cl1 > 0 and round(v_cpc1, 2) <= VIDEO_MAX_CPC_WITH_SALES)
                            v_exceeds_35_cpc = (
                                (v_cv1 == 0 and v_sp1 >= 1.00 and (v_cl1 == 0 or round(v_cpc1, 2) > VIDEO_MAX_CPC))
                                or (v_sp1 >= 1.00 and (v_cl1 == 0 or round(v_cpc1, 2) > VIDEO_MAX_CPC_WITH_SALES))
                                or (not v_good_cpc_1d_35 and v_sp3 >= 1.00 and (v_cl3 == 0 or round(v_cpc3, 2) > VIDEO_MAX_CPC_WITH_SALES))
                            )
                            v_opt = str(mr3.get("mix_material_aco_opt_status") or mr1.get("mix_material_aco_opt_status") or "0")
                            v_nm = mr3.get("main_entity_name") or "Video"
                            ad_id_r = str(mr3.get("ad_id") or "")
                            # Re-enable ONLY proven multi-sale or low-CPA winning videos whose CPC does NOT exceed $0.25 (0 sales Today) / $0.35 (with sales)
                            if not v_exceeds_35_cpc and ((v_cv3 >= 2 and v_cpa3 <= 7.00 and v_sp1 < 1.00) or (v_cv3 == 1 and v_cpa3 <= 4.50 and v_sp1 < 1.00)):
                                if v_opt != "0":
                                    ok_vr, _ = tm.pause_smart_plus_video(session, headers, aadvid, ad_id_r, mr3, operation="enable")
                                    if ok_vr:
                                        revived_vids.append(f"{v_nm} (3D: {v_cv3} sales @ ${v_cpa3:.2f}, CPC <= $0.35)")
                                else:
                                    revived_vids.append(f"{v_nm} (3D: {v_cv3} sales @ ${v_cpa3:.2f}, CPC <= $0.35)")
                            elif (v_exceeds_35_cpc or (v_cv3 == 0 and v_sp3 >= 1.00)) and v_opt == "0":
                                # Ensure 0-sale or CPC > $0.35 videos stay disabled so 100% of budget goes to clean winners
                                tm.pause_smart_plus_video(session, headers, aadvid, ad_id_r, mr3, operation="disable")
                        if revived_vids:
                            ok_cr, _ = tm.pause_campaign(session, headers, aadvid, cid, operation="enable")
                            if ok_cr:
                                is_active = True
                                live_camp_metrics[f"{aadvid}:{cid}"]["is_active"] = True
                                ledger["revived_campaigns"][revive_key] = {
                                    "campaign_name": cname,
                                    "revived_at": now_sa.strftime("%Y-%m-%d %H:%M"),
                                    "videos": revived_vids,
                                    "sp3": sp3,
                                    "cv3": cv3,
                                }
                                msg = (
                                    f"♻️🔥 [زيادة مبيعات الحملات — إحياء حملة فائزة] إعادة تفعيل '{cname}' ({cid}) في {acc_name} "
                                    f"(محققة {cv3} مبيعات في 3 أيام بـ CPA=${cpa3_val:.2f}) مع حصر الصرف في الفيديوهات الرابحة فقط: {revived_vids}"
                                )
                                new_actions_taken.append(msg)
                                print(f"  {msg}", flush=True)
                                ledger["actions"].append({
                                    "timestamp": now_sa.strftime("%Y-%m-%d %H:%M"),
                                    "experiment_key": f"REVIVE_{cid}_{today_s}",
                                    "is_experiment": False,
                                    "aadvid": aadvid,
                                    "acc_name": acc_name,
                                    "campaign_id": cid,
                                    "campaign_name": cname,
                                    "action_type": f"إعادة تفعيل حملة فائزة ({cv3} مبيعات @ ${cpa3_val:.2f}) وحصر الميزانية في الفيديوهات الرابحة",
                                    "videos": revived_vids,
                                    "base_sp3": sp3,
                                    "base_cv3": cv3,
                                    "status": "OPEN",
                                })
                    except Exception as e_rev:
                        print(f"  [WARN] Failed reviving 3D winner {cname}: {e_rev}", flush=True)

            # تم تعطيل إعادة التشغيل التلقائي لحملات 31-10 الموقوفة حتى لا تعود الحملات الخاسرة للنزيف:
            is_car_cushion_camp = any(k in cname.lower() for k in ("car", "armrest", "cushion", "pillow", "وساد", "تكاي"))
            if is_active:
                cn_low_chk = cname.lower()
                if (("kashaf" in cn_low_chk or "كشاف" in cn_low_chk) and not tm.is_gulf_campaign(cname)) or "pillow" in cn_low_chk or "وسادة نوم" in cn_low_chk:
                    ok_pk, _ = tm.pause_campaign(session, headers, aadvid, cid, operation="disable")
                    if ok_pk:
                        is_active = False
                        live_camp_metrics[f"{aadvid}:{cid}"]["is_active"] = False
                        new_actions_taken.append(f"🛑 إيقاف فوري لحملة منتج متوقف/محظور '{cname}' ({cid}) في {acc_name}")

            if is_active:
                if bdg > 50.01:
                    ok_cap24, old_b24 = update_campaign_budget_in_place(session, headers, aadvid, cid, cname, 50)
                    if ok_cap24:
                        bdg = 50.0
                        live_camp_metrics[f"{aadvid}:{cid}"]["budget"] = 50.0
                        new_actions_taken.append(f"🛡️ [تثبيت سقف تيك توك $50] تخفيض ميزانية '{cname}' ({cid}) في {acc_name} من ${old_b24:.0f} إلى $50")
                if is_exp:
                    active_exp_per_account[aadvid] += 1

                # 2) Enforce Hard $20 Campaign Stop-Loss ($25/$27 for Taager test products) & $8.50 Micro-Stop-Loss for [EXP-LEADS]
                cpa1 = (sp1 / cv1) if cv1 > 0 else None
                cpa2 = (sp2 / cv2) if cv2 > 0 else None
                cpa3 = (sp3 / cv3) if cv3 > 0 else None
                t_key_m = tm.extract_taager_product_key_tt(cname)
                hard_no_sale_m = 25.00 if t_key_m else HARD_CAMPAIGN_NO_SALE_LIMIT
                hard_cpa_m = 27.00 if t_key_m else HARD_CAMPAIGN_CPA_LIMIT

                pause_reason = None
                if t_key_m and t_key_m in (history.get("taager_failed_products") or {}):
                    pause_reason = f"[قاعدة تيست تاجر: المنتج فاشل '{t_key_m}'] تجاوز إجمالي صرف المنتج $25.00 بدون بيع أو $27.00 CPA عبر المنصات"
                elif account_is_bleeding and not t_key_m and cv1 == 0 and (sp1 >= 1.50 or sp3 >= 2.50):
                    pause_reason = f"[قاطع نزيف الحساب الكلي] الحساب خاسر (إجمالي اليوم ${pre_acc_sp1:.2f} / {pre_acc_cv1} مبيعات) والحملة صرفت ${sp1:.2f} اليوم بـ 0 مبيعات"
                elif is_exp:
                    if sp1 >= EXP_CAMPAIGN_NO_SALE_MICRO_LIMIT and cv1 == 0:
                        pause_reason = f"[فرملة تجربة المصغرة] صرف اليوم ${sp1:.2f} >= ${EXP_CAMPAIGN_NO_SALE_MICRO_LIMIT:.2f} بـ 0 مبيعات"
                    elif sp3 >= EXP_CAMPAIGN_NO_SALE_MICRO_LIMIT and cv3 == 0:
                        pause_reason = f"[فرملة تجربة المصغرة] صرف 3D ${sp3:.2f} >= ${EXP_CAMPAIGN_NO_SALE_MICRO_LIMIT:.2f} بـ 0 مبيعات"
                    elif cv1 > 0 and sp1 >= EXP_CAMPAIGN_MAX_CPA_MICRO_LIMIT and cpa1 >= EXP_CAMPAIGN_MAX_CPA_MICRO_LIMIT:
                        pause_reason = f"[فرملة تجربة المصغرة] CPA اليوم ${cpa1:.2f} >= ${EXP_CAMPAIGN_MAX_CPA_MICRO_LIMIT:.2f}"

                if not pause_reason:
                    if (sp1 > 25.00 if t_key_m else sp1 >= hard_no_sale_m) and cv1 == 0:
                        pause_reason = f"[قاطع النزيف الصارم] صرف اليوم ${sp1:.2f} {'>' if t_key_m else '>='} ${hard_no_sale_m:.2f} بدون بيع"
                    elif (sp2 > 25.00 if t_key_m else sp2 >= hard_no_sale_m) and cv2 == 0:
                        pause_reason = f"[قاطع النزيف الصارم] صرف يومين ${sp2:.2f} {'>' if t_key_m else '>='} ${hard_no_sale_m:.2f} بدون بيع"
                    elif (sp3 > 25.00 if t_key_m else sp3 >= hard_no_sale_m) and cv3 == 0:
                        pause_reason = f"[قاطع النزيف الصارم] صرف 3 أيام ${sp3:.2f} {'>' if t_key_m else '>='} ${hard_no_sale_m:.2f} بدون بيع"
                    elif (sp7 > 25.00 if t_key_m else sp7 >= hard_no_sale_m) and cv7 == 0:
                        pause_reason = f"[قاطع النزيف الصارم] صرف 7 أيام ${sp7:.2f} {'>' if t_key_m else '>='} ${hard_no_sale_m:.2f} بدون بيع"
                    elif cpa1 is not None and cpa1 >= hard_cpa_m:
                        pause_reason = f"[قاطع النزيف الصارم] سعر البيعة اليوم CPA=${cpa1:.2f} >= ${hard_cpa_m:.2f}"
                    elif cpa2 is not None and cpa2 >= hard_cpa_m and (cpa1 is None or cpa1 >= hard_cpa_m):
                        pause_reason = f"[قاطع النزيف الصارم] سعر البيعة ليومين CPA=${cpa2:.2f} >= ${hard_cpa_m:.2f}"
                    elif cpa3 is not None and cpa3 >= hard_cpa_m and (cpa1 is None or cpa1 >= hard_cpa_m):
                        pause_reason = f"[قاطع النزيف الصارم] سعر البيعة لـ 3 أيام CPA=${cpa3:.2f} >= ${hard_cpa_m:.2f}"

                if pause_reason:
                    ok_p, _ = tm.pause_campaign(session, headers, aadvid, cid, operation="disable")
                    if ok_p:
                        is_active = False
                        live_camp_metrics[f"{aadvid}:{cid}"]["is_active"] = False
                        if is_exp:
                            active_exp_per_account[aadvid] = max(0, active_exp_per_account[aadvid] - 1)
                        msg = f"🛑 إيقاف فوري للحملة '{cname}' ({cid}) في {acc_name} — {pause_reason}"
                        new_actions_taken.append(msg)
                        print(f"  {msg}", flush=True)

            # ==================================================================
            # SALES BOOSTING METHOD A2 & A3 ON ACTIVE CONVERTING CAMPAIGNS:
            # - A2: Progressive +30% Budget Scaling when campaign achieves profitable sales today
            # - A3: Intra-campaign pruning of 0-sale sibling videos (spend >= $2.50) so 100% of budget flows to the winning video(s)
            # ==================================================================
            if is_active and is_scaling_window:
                cpa1 = (sp1 / cv1) if cv1 > 0 else None
                cpa3 = (sp3 / cv3) if cv3 > 0 else None
                qualifies_for_scale = (
                    (cv1 >= 2 and cpa1 is not None and cpa1 <= 6.80 and (cpa3 is None or cpa3 <= 7.80))
                    or (cv1 == 1 and cpa1 is not None and cpa1 <= 6.20 and (cpa3 is None or cpa3 <= 7.00))
                )
                scale_rec_key = f"{today_s}:{aadvid}:{cid}"
                prev_scale = ledger["scaled_campaigns"].get(scale_rec_key) or ledger["scaled_campaigns"].get(f"{aadvid}:{cid}") or {}
                hist_tt_all = tm.load_history()
                hist_tt_scale = (hist_tt_all.get("proactive_scaled_tiktok") or {}).get(scale_rec_key) or {}
                last_scaled_cv1 = max(int(prev_scale.get("cv1") or prev_scale.get("conv_today") or 0), int(hist_tt_scale.get("conv_today") or 0))
                last_scaled_bdg = max(float(prev_scale.get("new_budget") or 0.0), float(hist_tt_scale.get("new_budget") or 0.0))
                cur_bdg_est = int(round(bdg if bdg >= 20 else 50))
                if last_scaled_bdg > cur_bdg_est:
                    cur_bdg_est = int(round(last_scaled_bdg))
                other_auto_wait_needed = False
                for _sc_dt_raw in (prev_scale.get("scaled_at"), hist_tt_scale.get("scaled_at")):
                    if _sc_dt_raw:
                        try:
                            _s_clean = str(_sc_dt_raw).replace("T", " ")[:16]
                            _dt_obj = datetime.strptime(_s_clean, "%Y-%m-%d %H:%M").replace(tzinfo=SA_TZ)
                            if (now_sa - _dt_obj).total_seconds() < 2700:
                                other_auto_wait_needed = True
                        except Exception:
                            pass
                high_budget_utilization_trigger = (
                    cv1 >= 2 and cpa1 is not None and cpa1 <= 5.50 and sp1 >= (0.45 * cur_bdg_est)
                )
                should_scale_now = (
                    qualifies_for_scale
                    and not other_auto_wait_needed
                    and (cv1 > last_scaled_cv1 or high_budget_utilization_trigger)
                )
                # أمر المستخدم الصارم (2026-11-02): سقف البدجت في تيك توك $50.00 (تخفيض أي حملة فوق $50 إلى $50 ومنع الرفع فوق $50)
                if bdg > 50.01:
                    ok_cap50, old_b50 = update_campaign_budget_in_place(session, headers, aadvid, cid, cname, 50)
                    if ok_cap50:
                        bdg = 50.0
                        live_camp_metrics[f"{aadvid}:{cid}"]["budget"] = 50.0
                        new_actions_taken.append(f"🛡️ [تثبيت سقف تيك توك $50] تخفيض ميزانية '{cname}' ({cid}) في {acc_name} من ${old_b50:.0f} إلى $50")
                if False and should_scale_now:
                    max_bdg_cap = 50
                    if cur_bdg_est < max_bdg_cap:
                        target_bdg = min(max_bdg_cap, int(round(cur_bdg_est * 1.30)))
                        ok_sc, old_bdg_real = update_campaign_budget_in_place(session, headers, aadvid, cid, cname, target_bdg)
                        if ok_sc:
                            live_camp_metrics[f"{aadvid}:{cid}"]["budget"] = float(target_bdg)
                            scale_entry = {
                                "campaign_name": cname,
                                "old_budget": old_bdg_real,
                                "new_budget": target_bdg,
                                "cv1": cv1,
                                "conv_today": cv1,
                                "cpa1": round(cpa1, 2),
                                "cpa_today": round(cpa1, 2),
                                "scaled_at": now_sa.strftime("%Y-%m-%d %H:%M"),
                            }
                            ledger["scaled_campaigns"][scale_rec_key] = scale_entry
                            ledger["scaled_campaigns"][f"{aadvid}:{cid}"] = scale_entry
                            hist_tt_all.setdefault("proactive_scaled_tiktok", {})[scale_rec_key] = scale_entry
                            tm.save_history(hist_tt_all)
                            msg_sc = (
                                f"📈 [زيادة مبيعات الحملات — رفع ميزانية +30%] ترقية ميزانية الحملة الرابحة '{cname}' ({cid}) في {acc_name} "
                                f"من ${old_bdg_real:.0f} إلى ${target_bdg} بعد تحقيق {cv1} مبيعات اليوم بـ CPA=${cpa1:.2f}!"
                            )
                            new_actions_taken.append(msg_sc)
                            scaling_opportunities_evaluated.append(
                                f"✅ **{acc_name} — `{cname}` (`{cid}`):** تم التقاط فرصة سكيل ورفع الميزانية فوراً **+30% (`${old_bdg_real:.0f} ➔ ${target_bdg}`)** (اليوم: `{cv1}` مبيعات بـ `CPA=${cpa1:.2f}` وصرف `${sp1:.2f}`)."
                            )
                            print(f"  {msg_sc}", flush=True)
                            ledger["actions"].append({
                                "timestamp": now_sa.strftime("%Y-%m-%d %H:%M"),
                                "experiment_key": f"SCALE_{cid}_{today_s}_CV{cv1}_B{target_bdg}",
                                "is_experiment": is_exp,
                                "aadvid": aadvid,
                                "acc_name": acc_name,
                                "campaign_id": cid,
                                "campaign_name": cname,
                                "action_type": f"رفع ميزانية الحملة الرابحة +30% (${old_bdg_real:.0f} -> ${target_bdg})",
                                "videos": [],
                                "base_sp3": sp3,
                                "base_cv3": cv3,
                                "status": "OPEN",
                            })
                elif cv1 >= 1:
                    if not qualifies_for_scale:
                        scaling_opportunities_evaluated.append(
                            f"⏸️ **{acc_name} — `{cname}` (`{cid}`):** لم يُرفع السكيل (اليوم `{cv1}` مبيعات بـ `CPA=${cpa1:.2f}` يتجاوز سقف السكيل الآمن)."
                        )
                    elif other_auto_wait_needed:
                        scaling_opportunities_evaluated.append(
                            f"⏳ **{acc_name} — `{cname}` (`{cid}`):** تم رفع ميزانيتها مؤخراً إلى `${cur_bdg_est}` (اليوم `{cv1}` مبيعات بـ `CPA=${cpa1:.2f}` وصرف `${sp1:.2f}`) — بانتظار مبيعة جديدة أو استهلاك 45% من الميزانية الجديدة."
                        )
                    else:
                        scaling_opportunities_evaluated.append(
                            f"⏳ **{acc_name} — `{cname}` (`{cid}`):** الميزانية الحالية `${cur_bdg_est}` كافية حالياً (`${sp1:.2f}` صرف / `{cv1}` مبيعات بـ `CPA=${cpa1:.2f}`) وتمت ترقيتها عند نفس عدد المبيعات (`{last_scaled_cv1}`)."
                        )

                # Method A3 (Runs 24/7 Every Hour): Microscopic Human-Eye Inspection of Inner Videos in EVERY Active Campaign:
                # 1) Detect Zombie Campaigns (0 active videos inside an active campaign) and pause the campaign immediately.
                # 2) Detect duplicate SCALE/COPY campaigns with CPA > $7.50 (after >= $5 spend) or stagnant 0-spend old Gulf campaigns and pause them.
                # 3) Prune any individual video that spent >= $1.00 with 0 sales and CPC > $0.25 (or 0 clicks).
                # 4) Prune 0-sale sibling videos (spend >= $1.50 Today or >= $2.30 in 3D) inside converting campaigns so 100% of budget flows to winning videos.
            if is_active:
                cpa1_cur = (sp1 / cv1) if cv1 > 0 else None
                if (" scale " in f" {cname.lower()} " or cname.lower().endswith(" scale")) and (
                    (sp1 >= 1.00 and cv1 == 0 and cpc1 > 0.25)
                    or (sp1 >= 5.00 and (cv1 == 0 or (cpa1_cur is not None and cpa1_cur > 7.50)))
                ):
                    ok_sc_cut, _ = tm.pause_campaign(session, headers, aadvid, cid, operation="disable")
                    if ok_sc_cut:
                        is_active = False
                        live_camp_metrics[f"{aadvid}:{cid}"]["is_active"] = False
                        msg_sc_cut = (
                            f"🛑 [تدقيق العين البشرية الدقيق] إيقاف حملة التوسيع المكررة الخاسرة '{cname}' ({cid}) في {acc_name} "
                            f"(صرف اليوم ${sp1:.2f} / {cv1} مبيعات، CPC=${cpc1:.2f}، CPA=${cpa1_cur if cpa1_cur else 0:.2f}) لمنع سحب المزاد من الحملة الأساسية الرابحة."
                        )
                        new_actions_taken.append(msg_sc_cut)
                        print(f"  {msg_sc_cut}", flush=True)
                elif ("20-10" in cname or "19-10" in cname) and not tm.is_gulf_campaign(cname) and sp14 <= 0.0 and cv14 == 0:
                    ok_old_cut, _ = tm.pause_campaign(session, headers, aadvid, cid, operation="disable")
                    if ok_old_cut:
                        is_active = False
                        live_camp_metrics[f"{aadvid}:{cid}"]["is_active"] = False
                        msg_old_cut = f"🧹 [تدقيق العين البشرية الدقيق] إيقاف الحملة القديمة الراكدة '{cname}' ({cid}) في {acc_name} ($0.00 صرف خلال 14 يوماً)."
                        new_actions_taken.append(msg_old_cut)
                        print(f"  {msg_old_cut}", flush=True)

            if is_active and cids_by_camp.get(cid):
                try:
                    cr_ids_p = cids_by_camp[cid]
                    mats_1d_p = {
                        f"{m.get('ad_id')}:{m.get('ad_material_draft_id')}:{m.get('main_entity_id')}": m
                        for m in tm.fetch_expanded_materials(session, headers, aadvid, cr_ids_p, t_str, t_str)
                    }
                    mats_3d_p = tm.fetch_expanded_materials(session, headers, aadvid, cr_ids_p, two_str, t_str)
                    active_vids_total = 0
                    active_converting_vids = 0
                    for mp3 in mats_3d_p:
                        kp = f"{mp3.get('ad_id')}:{mp3.get('ad_material_draft_id')}:{mp3.get('main_entity_id')}"
                        mp1 = mats_1d_p.get(kp, {})
                        v_opt_p = str(mp3.get("mix_material_aco_opt_status") or mp1.get("mix_material_aco_opt_status") or "0")
                        v_cv_p = int(float(mp3.get("time_attr_convert_cnt") or 0)) + int(float(mp1.get("time_attr_convert_cnt") or 0))
                        if v_opt_p == "0":
                            active_vids_total += 1
                            if v_cv_p > 0:
                                active_converting_vids += 1

                    # If this is a future-scheduled campaign ($0.00 spend in 14D) and Phase 2 disabled a $0.00-spend proven winner leaving < 3 active videos, re-enable it!
                    if sp14 <= 0.0 and mats_3d_p and active_vids_total < 3:
                        for mp3 in mats_3d_p:
                            kp = f"{mp3.get('ad_id')}:{mp3.get('ad_material_draft_id')}:{mp3.get('main_entity_id')}"
                            mp1 = mats_1d_p.get(kp, {})
                            v_opt_p = str(mp3.get("mix_material_aco_opt_status") or mp1.get("mix_material_aco_opt_status") or "0")
                            v_sp3_p = float(mp3.get("stat_cost") or 0.0)
                            v_nm_p = (mp3.get("main_entity_name") or "").strip()
                            v_key_p = tm.canonical_video_name(v_nm_p, str(mp3.get("main_entity_id") or ""))
                            if v_opt_p != "0" and v_sp3_p <= 0.0 and v_key_p not in history.get("tiktok_disabled_dup_videos", {}):
                                ok_reen, _ = tm.pause_smart_plus_video(session, headers, aadvid, str(mp3.get("ad_id")), mp3, operation="enable")
                                if ok_reen:
                                    active_vids_total += 1
                                    msg_reen = f"♻️ [حماية حملة الغد المجدولة] إعادة تفعيل الفيديو الرابح '{v_nm_p}' ($0.00 صرف) داخل '{cname}' لضمان دخول مزاد 06:00 ص بـ {active_vids_total} فيديوهات نشطة."
                                    new_actions_taken.append(msg_reen)
                                    print(f"  {msg_reen}", flush=True)

                    if mats_3d_p and active_vids_total == 0:
                        ok_zc, _ = tm.pause_campaign(session, headers, aadvid, cid, operation="disable")
                        if ok_zc:
                            is_active = False
                            live_camp_metrics[f"{aadvid}:{cid}"]["is_active"] = False
                            msg_zc = f"🧟 [تدقيق العين البشرية الدقيق] إيقاف حملة ميتة '{cname}' ({cid}) في {acc_name} لأن جميع الفيديوهات بداخلها مطفأة (0 فيديوهات نشطة)."
                            new_actions_taken.append(msg_zc)
                            print(f"  {msg_zc}", flush=True)

                    if is_active and active_vids_total >= 1:
                        for mp3 in mats_3d_p:
                            if active_vids_total <= 0:
                                break
                            kp = f"{mp3.get('ad_id')}:{mp3.get('ad_material_draft_id')}:{mp3.get('main_entity_id')}"
                            mp1 = mats_1d_p.get(kp, {})
                            v_opt_p = str(mp3.get("mix_material_aco_opt_status") or mp1.get("mix_material_aco_opt_status") or "0")
                            if v_opt_p != "0":
                                continue
                            v_sp1_p = float(mp1.get("stat_cost") or 0.0)
                            v_cv1_p = int(float(mp1.get("time_attr_convert_cnt") or 0))
                            v_cl1_p = int(float(mp1.get("click_cnt") or 0))
                            v_cpc1_p = (v_sp1_p / v_cl1_p) if v_cl1_p > 0 else float(mp1.get("cpc") or 0.0)
                            v_sp3_p = float(mp3.get("stat_cost") or 0.0)
                            v_cv3_p = int(float(mp3.get("time_attr_convert_cnt") or 0))
                            v_nm_p = mp3.get("main_entity_name") or "Video"

                            is_bad_cpc_1d = (v_cv1_p == 0 and v_sp1_p >= 1.00 and (v_cl1_p == 0 or round(v_cpc1_p, 2) > VIDEO_MAX_CPC))
                            is_sibling_bleeder = (
                                active_converting_vids >= 1
                                and active_vids_total >= 2
                                and v_cv1_p == 0
                                and v_cv3_p == 0
                                and (v_sp1_p >= 1.45 or v_sp3_p >= 2.25)
                            )
                            if is_bad_cpc_1d or is_sibling_bleeder:
                                ok_pr, _ = tm.pause_smart_plus_video(session, headers, aadvid, str(mp3.get("ad_id")), mp3, operation="disable")
                                if ok_pr:
                                    active_vids_total -= 1
                                    why_pr = f"1D Spend=${v_sp1_p:.2f}, 0 Sales, CPC=${v_cpc1_p:.2f} > $0.25" if is_bad_cpc_1d else f"صرف ${v_sp3_p:.2f} بـ 0 مبيعات بجانب فيديو فائز"
                                    msg_pr = (
                                        f"✂️ [تدقيق العين البشرية للفيديوهات] إيقاف الفيديو الخاسر '{v_nm_p}' "
                                        f"({why_pr}) داخل الحملة '{cname}' لحصر 100% من الصرف في الفيديوهات الرابحة."
                                    )
                                    new_actions_taken.append(msg_pr)
                                    print(f"  {msg_pr}", flush=True)

                        if active_vids_total == 0:
                            ok_zc2, _ = tm.pause_campaign(session, headers, aadvid, cid, operation="disable")
                            if ok_zc2:
                                is_active = False
                                live_camp_metrics[f"{aadvid}:{cid}"]["is_active"] = False
                                msg_zc2 = f"🛑 [تدقيق العين البشرية الدقيق] إيقاف الحملة '{cname}' ({cid}) في {acc_name} بعد تصفية جميع الفيديوهات الخاسرة بداخلها (0 فيديوهات متبقية)."
                                new_actions_taken.append(msg_zc2)
                                print(f"  {msg_zc2}", flush=True)
                except Exception:
                    pass

            if is_active or sp1 > 0 or spy > 0 or sp3 > 0:
                campaign_rows.append(live_camp_metrics[f"{aadvid}:{cid}"])

        account_snapshots.append({
            "acc_name": acc_name,
            "aadvid": aadvid,
            "sp1": round(acc_sp1, 2), "cv1": acc_cv1, "cpa1": round(acc_sp1 / acc_cv1, 2) if acc_cv1 > 0 else None,
            "spy": round(acc_spy, 2), "cvy": acc_cvy, "cpay": round(acc_spy / acc_cvy, 2) if acc_cvy > 0 else None,
            "sp3": round(acc_sp3, 2), "cv3": acc_cv3, "cpa3": round(acc_sp3 / acc_cv3, 2) if acc_cv3 > 0 else None,
            "sp7": round(acc_sp7, 2), "cv7": acc_cv7, "cpa7": round(acc_sp7 / acc_cv7, 2) if acc_cv7 > 0 else None,
            "sp14": round(acc_sp14, 2), "cv14": acc_cv14, "cpa14": round(acc_sp14 / acc_cv14, 2) if acc_cv14 > 0 else None,
        })

    # ==========================================================================
    # STEP 2: AUDIT PREVIOUS HOURLY ACTIONS IN LEDGER
    # ==========================================================================
    t_str_audit = now_sa.strftime("%Y-%m-%d")
    two_str_audit = (now_sa - timedelta(days=2)).strftime("%Y-%m-%d")
    for act in ledger.get("actions", []):
        if act.get("status") not in ("OPEN", "ACTIVE_TEST"):
            continue
        aadvid = act.get("aadvid", "")
        cid = act.get("campaign_id", "")
        cname = act.get("campaign_name", "")
        m = live_camp_metrics.get(f"{aadvid}:{cid}") or live_camp_metrics.get(f"{aadvid}:NAME:{cname}")
        if not m:
            ledger_followups.append(f"⏳ متابعة حركة '{cname}' ({act.get('action_type')}): بانتظار ظهور بيانات الصرف في الساعة القادمة.")
            continue

        if not act.get("campaign_id") and m.get("campaign_id"):
            act["campaign_id"] = m["campaign_id"]

        delta_sp = round(max(0.0, m["sp3"] - float(act.get("base_sp3") or 0.0)), 2)
        delta_cv = max(0, m["cv3"] - int(act.get("base_cv3") or 0))
        delta_cpa = round(delta_sp / delta_cv, 2) if delta_cv > 0 else None

        # Micro-check individual videos inside active [EXP-] campaigns
        if m["is_active"] and ("[exp-leads]" in cname.lower() or "[exp-" in cname.lower() or act.get("is_experiment")):
            try:
                ads_e = tm.fetch_ads(session, headers, aadvid, t_str_audit, t_str_audit)
                cids_e = [str(a.get("creative_id")) for a in ads_e if str(a.get("campaign_id")) == m["campaign_id"] and a.get("creative_id")]
                if cids_e:
                    mats_e = tm.fetch_expanded_materials(session, headers, aadvid, cids_e, two_str_audit, t_str_audit)
                    for me in mats_e:
                        if str(me.get("mix_material_aco_opt_status") or "0") != "0" or str(me.get("material_primary_status") or "") in ("disable", "delete"):
                            continue
                        v_sp = float(me.get("stat_cost") or 0.0)
                        v_cv = int(float(me.get("time_attr_convert_cnt") or 0))
                        v_clk = int(float(me.get("click_cnt") or 0))
                        v_cpc = float(me.get("cpc") or 0.0)
                        v_nm = me.get("main_entity_name") or "Video"
                        v_kill = None
                        if v_cv == 0 and v_sp >= VIDEO_CPC_MIN_SPEND and (v_clk == 0 or v_cpc > VIDEO_MAX_CPC):
                            v_kill = f"صرف ${v_sp:.2f} بـ CPC=${v_cpc:.2f} (> $0.25) بدون مبيعات"
                        elif v_cv > 0 and v_sp >= VIDEO_CPC_MIN_SPEND and (v_clk == 0 or v_cpc > VIDEO_MAX_CPC_WITH_SALES):
                            v_kill = f"صرف ${v_sp:.2f} بـ CPC=${v_cpc:.2f} (> $0.35 سقف النقرة مع وجود مبيعات)"
                        elif v_cv == 0 and v_sp >= EXP_VIDEO_NO_SALE_MICRO_LIMIT:
                            v_kill = f"صرف تجريبي ${v_sp:.2f} >= ${EXP_VIDEO_NO_SALE_MICRO_LIMIT:.2f} بدون مبيعات"
                        if v_kill:
                            ok_vk, _ = tm.pause_smart_plus_video(session, headers, aadvid, str(me.get("ad_id")), me, operation="disable")
                            if ok_vk:
                                new_actions_taken.append(f"🛡️ فرملة فيديو داخل التجربة '{v_nm}' في '{cname}' ({v_kill})")
            except Exception:
                pass

        if not m["is_active"] and delta_cv == 0:
            act["status"] = "FAILED_AND_CUT"
            act["closed_at"] = now_sa.isoformat()
            ledger.setdefault("failed_experiments", []).append(act.get("experiment_key", cname))
            ledger_followups.append(
                f"❌ تقييم خطوة المراجعة السابقة: '{cname}' صرفت +${delta_sp:.2f} بـ 0 مبيعات وتم إيقافها مبكراً لحماية رأس المال."
            )
        elif delta_cv >= 1 and delta_cpa is not None and delta_cpa <= 6.80:
            if delta_cv >= 2:
                act["status"] = "SUCCEEDED"
            ledger_followups.append(
                f"✅ تقييم خطوة المراجعة السابقة: حركة '{cname}' نجحت وحققت +{delta_cv} مبيعات جديدة بصرف +${delta_sp:.2f} (CPA=${delta_cpa:.2f})!"
            )
        else:
            ledger_followups.append(
                f"⏳ تقييم خطوة المراجعة السابقة: '{cname}' ({act.get('action_type')}) — الصرف منذ الإجراء +${delta_sp:.2f} | المبيعات الجديدة: +{delta_cv} (إجمالي اليوم: ${m['sp1']:.2f} / {m['cv1']} مبيعات)."
            )

    # ==========================================================================
    # STEP 3: MULTI-METHOD SALES EXPERIMENTATION ENGINE (ON 3-5 PROVEN WINNING VIDEOS ONLY)
    # ("بالضافة الى التجريب طرق لزيادة البيع" + "اي تجريب يعمله ما يعمله على فيديوهاتي الجديدة ولاكن يعمله على فيديوهاتي الرابحة")
    # Methods tested across accounts:
    # - Method 1: Website Leads (objective_type=8, external_action=2) + TikTok In-Feed Only [3000] on 4-5 Proven Power Bank Winners
    # - Method 2: Website Leads (objective_type=8) + TikTok In-Feed Only [3000] on Today's Hot Flow-Hook Winners + Multi-Sale Superstar
    # - Method 3: Website Leads (objective_type=8) + TikTok In-Feed Only [3000] on Proven Car Armrest Cushion Winners
    # ==========================================================================
    existing_exp_keys = {a.get("experiment_key") for a in ledger.get("actions", [])} | set(ledger.get("failed_experiments", []))
    date_tag = f"{today_s[8:10]}-{today_s[5:7]}"

    EXPERIMENT_BLUEPRINTS = [
        # Blueprint 1: Account 2 (ROELLINGER RECYCLING1 - 7631765139798081554)
        # Website Leads + TikTok In-Feed Only [3000] combining Account 2's Proven Power Bank Winners + Today's Winning Flow Hooks
        {
            "key": "ROEL1_LEADS_FEED_5_PB_WINNERS_V1",
            "aadvid": "7631765139798081554",
            "acc_name": "ROELLINGER RECYCLING1",
            "camp_name": f"[EXP-LEADS] power bank 5 Proven Winners Feed Leads {date_tag}",
            "method_label": "طريقة البيع (1): هدف Website Leads + عزل موضع TikTok In-Feed Only [3000] لـ 5 فيديوهات باور بانك رابحة (Target Cost $6.40)",
            "cpa_bid": "6.4",
            "budget": "50",
            "tiktok_only_placement": True,
            "use_leads_objective": True,
            "donors": [
                ("1879845883154146", "23-10_voltrx_ym49_s07.mp4"),          # 4 sales @ $1.80 CPA in ROELLINGER1
                ("1879845883154146", "23-10_voltrx_ym49_s06.mp4"),          # Proven winner in ROELLINGER1
                ("1880204726651266", "1yr_pb_tt_باور بانك(3)_103s.mp4"),    # 1 sale @ $2.38 CPA in ROELLINGER1 (103 historical sales)
                ("1880427747180001", "30-10_PB_FlowHook_Ad_03.mp4"),        # 2 sales today @ $1.67 CPA
                ("1880427747180001", "30-10_PB_FlowHook_Ad_04.mp4"),        # 3 sales today across accounts
            ],
        },
        # Blueprint 2: Account 3 (ROELLINGER RECYCLING2 - 7631765070816509959)
        # Website Leads + TikTok In-Feed Only [3000] combining the 27-Sale Superstar + Today's 3 Converting Flow Hook Winners
        {
            "key": "ROEL2_LEADS_FEED_SUPER_PB_FLOW_V2",
            "aadvid": "7631765070816509959",
            "acc_name": "ROELLINGER RECYCLING2",
            "camp_name": f"[EXP-LEADS] power bank Super Winners Feed Leads {date_tag}",
            "method_label": "طريقة البيع (2): دمج الفيديو الأسطوري (27 بيعة) مع فيديوهات Flow Hooks الفائزة اليوم في حملة Website Leads + TikTok Feed Only [3000] (Target Cost $6.40)",
            "cpa_bid": "6.4",
            "budget": "50",
            "tiktok_only_placement": True,
            "use_leads_objective": True,
            "donors": [
                ("1880299140950417", "26-10_Voltrx_Full_Hook_Ad_01_v3fast.mp4"),  # 27 sales @ $6.86 CPA in ROELLINGER2
                ("1880427764550018", "30-10_PB_FlowHook_Ad_04.mp4"),              # 2 sales today @ $1.72 CPA in ROELLINGER2
                ("1880427764550018", "30-10_PB_FlowHook_Ad_03.mp4"),              # 2 sales today @ $1.67 CPA
                ("1880427764550018", "30-10_PB_FlowHook_Ad_02.mp4"),              # 1 sale today @ $5.22 CPA
            ],
        },
        # Blueprint 3: Account 2 (ROELLINGER RECYCLING1 - 7631765139798081554)
        # Replicate Account 1's Superstar Formula (19 sales @ $5.80 CPA): Website Leads + TikTok In-Feed Only [3000] on 5 Proven Car Armrest Winners
        {
            "key": "ROEL1_LEADS_FEED_CAR_WINNERS_V1",
            "aadvid": "7631765139798081554",
            "acc_name": "ROELLINGER RECYCLING1",
            "camp_name": f"[EXP-LEADS] car armrest 5 Proven Winners Feed Leads {date_tag}",
            "method_label": "طريقة البيع (3): استنساخ الخلطة الرابحة (Website Leads + TikTok In-Feed Only [3000] + Target Cost $6.30) لـ 5 فيديوهات رابحة لوسادة السيارة في الحساب الثاني",
            "cpa_bid": "6.3",
            "budget": "50",
            "tiktok_only_placement": True,
            "use_leads_objective": True,
            "donors": [
                ("1880201944782978", "27-10_Fast_Car_Armrest_Approved_Ad_06.mp4"),  # 2 sales @ $1.21 CPA in ROELLINGER1
                ("1879741540536498", "21-10_hanan_20-10_1.mp4"),                    # 3 sales in ROELLINGER0, CPC $0.19 in ROELLINGER1
                ("1879741540536498", "22-10_flow_hook5_travel.mp4"),                # Proven Flow Hook winner, CPC $0.12
                ("1880201944782978", "27-10_Fast_Car_Armrest_Approved_Ad_09.mp4"),  # Clean low CPC $0.08
                ("1880201934167762", "27-10_Fast_Car_Armrest_Approved_Ad_04.mp4"),  # Clean low CPC $0.14
            ],
        },
        # Blueprint 4: Account 1 (ROELLINGER RECYCLING - 7631763556322148369)
        # Self-Learned CPC-Choke Migration: Migrate Account 1's Top 5 Multi-Sale Power Bank Winners (9 sales @ $4.30, 3 sales @ $4.90, 4 sales @ $2.85)
        # that were choked by CPC > $0.35 in standard Website Conversions into the low-CPC Website Leads + TikTok In-Feed Only [3000] auction!
        {
            "key": "ROEL0_LEADS_FEED_5_PB_SUPER_WINNERS_V1",
            "aadvid": "7631763556322148369",
            "acc_name": "ROELLINGER RECYCLING",
            "camp_name": f"[EXP-LEADS] power bank 5 Super Winners Feed Leads {date_tag}",
            "method_label": "طريقة البيع (4 - تعلم ذاتي): نقل أقوى 5 فيديوهات باور بانك بيّاعة (9 مبيعات بـ $4.30 و4 مبيعات بـ $2.85) من مزاد Conversions المرتفع النقرة إلى مزاد Website Leads + TikTok In-Feed Only [3000] منخفض النقرة (Target Cost $6.30)",
            "cpa_bid": "6.3",
            "budget": "50",
            "tiktok_only_placement": True,
            "use_leads_objective": True,
            "donors": [
                ("1880297411745937", "26-10_Voltrx_Seedance_Ad_14.mp4"),    # 9 sales @ $4.30 CPA in ROELLINGER0
                ("1880427722388082", "30-10_PB_FlowHook_Ad_03.mp4"),        # 3 sales @ $4.90 CPA in ROELLINGER0
                ("1880201842305441", "27-10_PB_Ola22_02.mp4"),              # 4 sales @ $2.85 CPA in ROELLINGER0
                ("1879845571604673", "23-10_voltrx_ym49_s12.mp4"),          # 4 sales in ROELLINGER0
                ("1880297411745937", "26-10_Voltrx_Seedance_Ad_10.mp4"),    # 4 sales in ROELLINGER0
            ],
        },
        # Blueprint 5: Account 3 (ROELLINGER RECYCLING2 - 7631765070816509959)
        # Replicate Account 1's 19-Sale Car Armrest Superstar Formula (Website Leads + TikTok In-Feed Only [3000]) into Account 3
        {
            "key": "ROEL2_LEADS_FEED_CAR_WINNERS_V1",
            "aadvid": "7631765070816509959",
            "acc_name": "ROELLINGER RECYCLING2",
            "camp_name": f"[EXP-LEADS] car armrest 5 Elite Winners Feed Leads {date_tag}",
            "method_label": "طريقة البيع (5 - تعميم عبر الحسابات): تعميم خلطة وسادة السيارة الفائزة (19 بيعة بـ $5.80 عبر Website Leads + TikTok In-Feed Only [3000]) في الحساب الثالث لـ 5 فيديوهات رابحة (Target Cost $6.30)",
            "cpa_bid": "6.3",
            "budget": "50",
            "tiktok_only_placement": True,
            "use_leads_objective": True,
            "donors": [
                ("1879741630263761", "21-10_hanan_20-10_1.mp4"),                    # Top Car Armrest winner
                ("1879741630263761", "22-10_flow_hook5_travel.mp4"),                # Proven Flow Hook winner
                ("1879741630263761", "21-10_hanan_20-10_3.mp4"),                    # Proven Car Armrest winner
                ("1880201973438514", "27-10_Fast_Car_Armrest_Approved_Ad_06.mp4"),  # Proven Fast Car Armrest winner
                ("1880201960208641", "27-10_Fast_Car_Armrest_Approved_Ad_04.mp4"),  # Proven Fast Car Armrest winner
            ],
        },
        # Blueprint 6: Account 1 (ROELLINGER RECYCLING - 7631763556322148369)
        # Clean 31-10 Renewal of Account 1's 26-Sale Superstar Formula ($159.15 / 26 sales @ $6.12 CPA)
        # replacing 15-10_video_4.mp4 (which burned $6.07 with 0 sales today) with 21-10_hanan_20-10_3.mp4
        {
            "key": "ROEL0_LEADS_FEED_CAR_ELITE_WINNERS_31_10",
            "aadvid": "7631763556322148369",
            "acc_name": "ROELLINGER RECYCLING",
            "camp_name": f"[EXP-LEADS] car armrest 5 Elite Winners Feed Leads {date_tag}",
            "method_label": "طريقة البيع (6 - تجديد مزاد الصباح النظيف): إعادة إطلاق خلطة وسادة السيارة الأسطورية (26 بيعة بـ $6.12 CPA) في الحساب الأول لمزاد 31-10 بعد استبدال الفيديو النازف اليوم (15-10_video_4) بـ 5 فيديوهات رابحة صافية (Website Leads + TikTok In-Feed Only [3000] + Target Cost $6.20)",
            "cpa_bid": "6.2",
            "budget": "50",
            "tiktok_only_placement": True,
            "use_leads_objective": True,
            "donors": [
                ("1880472559482321", "22-10_flow_hook3_shop.mp4"),                  # 11 sales @ $3.78 CPA in ROELLINGER0
                ("1880472559482321", "21-10_hanan_20-10_1.mp4"),                    # 11 sales @ $6.73 CPA in ROELLINGER0
                ("1880472559482321", "22-10_flow_hook5_travel.mp4"),                # Proven Flow Hook winner
                ("1880472559482321", "20-10_car_hook_ad_2_final_v3.mp4"),           # Proven Car Hook winner
                ("1879741471299601", "21-10_hanan_20-10_3.mp4"),                    # Proven Car Armrest winner
            ],
        },
        # Blueprint 7 (Auto Placement CPC-Choke Rescue): Account 1 Car Armrest — Website Leads + Auto Placement [3000, 9000, 1000]
        {
            "key": f"ROEL0_CAR_LEADS_AUTO_PLACEMENT_{date_tag}",
            "aadvid": "7631763556322148369",
            "acc_name": "ROELLINGER RECYCLING",
            "camp_name": f"[EXP-LEADS] car armrest 4 Winners Leads AutoPlacement {date_tag}",
            "method_label": "طريقة البيع (7 - إنقاذ CPC بالمواضع التلقائية Leads Auto Placement): وضع الفيديوهات الـ 4 البيّاعة (22-10_flow_hook3_shop بـ $1.76 CPA و 21-10_hanan_20-10_1 بـ $2.81 CPA) في حملة Website Leads + Auto Placement لخفض الـ CPM والـ CPC تحت $0.25",
            "cpa_bid": "6.2",
            "budget": "50",
            "tiktok_only_placement": False,
            "use_leads_objective": True,
            "donors": [
                ("1880472559482321", "22-10_flow_hook3_shop.mp4"),
                ("1880472559482321", "21-10_hanan_20-10_1.mp4"),
                ("1880472559482321", "21-10_hanan_20-10_5.mp4"),
                ("1880472559482321", "20-10_car_hook_ad_2_final_v3.mp4"),
            ],
        },
        # Blueprint 8 (Auto Placement CPC-Choke Rescue): Account 1 Car Armrest — Website Conversions + Auto Placement [3000, 9000, 1000]
        {
            "key": f"ROEL0_CAR_CONV_AUTO_PLACEMENT_{date_tag}",
            "aadvid": "7631763556322148369",
            "acc_name": "ROELLINGER RECYCLING",
            "camp_name": f"[EXP-LEADS] car armrest 4 Winners Conv AutoPlacement {date_tag}",
            "method_label": "طريقة البيع (8 - إنقاذ CPC بالمواضع التلقائية Conversions Auto Placement): وضع نفس الفيديوهات الـ 4 البيّاعة في حملة Website Conversions + Auto Placement لخفض الـ CPC تحت $0.25",
            "cpa_bid": "6.2",
            "budget": "50",
            "tiktok_only_placement": False,
            "use_leads_objective": False,
            "donors": [
                ("1880472559482321", "22-10_flow_hook3_shop.mp4"),
                ("1880472559482321", "21-10_hanan_20-10_1.mp4"),
                ("1880472559482321", "21-10_hanan_20-10_5.mp4"),
                ("1880472559482321", "20-10_car_hook_ad_2_final_v3.mp4"),
            ],
        },
        # Blueprint 9 (Auto Placement CPC-Choke Rescue): Account 1 Power Bank — Website Leads + Auto Placement
        {
            "key": f"ROEL0_PB_LEADS_AUTO_PLACEMENT_{date_tag}",
            "aadvid": "7631763556322148369",
            "acc_name": "ROELLINGER RECYCLING",
            "camp_name": f"[EXP-LEADS] power bank 5 CPC-Winners Leads AutoPlacement {date_tag}",
            "method_label": "طريقة البيع (9 - إنقاذ CPC الباور بانك بـ Leads Auto Placement): نقل أقوى 5 فيديوهات باور بانك بيّاعة توقفت بسبب CPC > $0.35 إلى Website Leads + Auto Placement",
            "cpa_bid": "6.3",
            "budget": "50",
            "tiktok_only_placement": False,
            "use_leads_objective": True,
            "donors": [
                ("1880297411745937", "26-10_Voltrx_Seedance_Ad_14.mp4"),
                ("1880201842305441", "27-10_PB_Ola22_02.mp4"),
                ("1880427722388082", "30-10_PB_FlowHook_Ad_03.mp4"),
                ("1880103841395026", "26-10_Fast_Voltrx_Approved_Ad_03.mp4"),
                ("1880201832311538", "27-10_PB_Hanan24_01.mp4"),
            ],
        },
        # Blueprint 10 (Auto Placement CPC-Choke Rescue): Account 1 Power Bank — Website Conversions + Auto Placement
        {
            "key": f"ROEL0_PB_CONV_AUTO_PLACEMENT_{date_tag}",
            "aadvid": "7631763556322148369",
            "acc_name": "ROELLINGER RECYCLING",
            "camp_name": f"[EXP-LEADS] power bank 5 CPC-Winners Conv AutoPlacement {date_tag}",
            "method_label": "طريقة البيع (10 - إنقاذ CPC الباور بانك بـ Conversions Auto Placement): نقل نفس الـ 5 فيديوهات باور بانك البيّاعة إلى Website Conversions + Auto Placement",
            "cpa_bid": "6.3",
            "budget": "50",
            "tiktok_only_placement": False,
            "use_leads_objective": False,
            "donors": [
                ("1880297411745937", "26-10_Voltrx_Seedance_Ad_14.mp4"),
                ("1880201842305441", "27-10_PB_Ola22_02.mp4"),
                ("1880427722388082", "30-10_PB_FlowHook_Ad_03.mp4"),
                ("1880103841395026", "26-10_Fast_Voltrx_Approved_Ad_03.mp4"),
                ("1880201832311538", "27-10_PB_Hanan24_01.mp4"),
            ],
        },
        # Blueprint 11: Account 3 Power Bank — Website Leads + Auto Placement
        {
            "key": f"ROEL2_PB_LEADS_AUTO_PLACEMENT_{date_tag}",
            "aadvid": "7631765070816509959",
            "acc_name": "ROELLINGER RECYCLING2",
            "camp_name": f"[EXP-LEADS] power bank 4 CPC-Winners Leads AutoPlacement {date_tag}",
            "method_label": "طريقة البيع (11 - إنقاذ CPC الحساب الثالث بـ Leads Auto Placement): وضع الفيديو الأسطوري (32 بيعة) و3 فيديوهات بيّاعة توقفت بسبب CPC > $0.35 في Website Leads + Auto Placement",
            "cpa_bid": "6.3",
            "budget": "50",
            "tiktok_only_placement": False,
            "use_leads_objective": True,
            "donors": [
                ("1880299140950417", "26-10_Voltrx_Full_Hook_Ad_01_v3fast.mp4"),
                ("1880427764550018", "30-10_PB_FlowHook_Ad_04.mp4"),
                ("1880472096597409", "30-10_PB_FlowHook_Ad_02.mp4"),
                ("1880468185645138", "30-10_PB_New5_Ad_03.mp4"),
            ],
        },
        # Blueprint 12: Account 3 Car Armrest — Website Leads + Auto Placement
        {
            "key": f"ROEL2_CAR_LEADS_AUTO_PLACEMENT_{date_tag}",
            "aadvid": "7631765070816509959",
            "acc_name": "ROELLINGER RECYCLING2",
            "camp_name": f"[EXP-LEADS] car armrest 4 Winners Leads AutoPlacement {date_tag}",
            "method_label": "طريقة البيع (12 - وسادة السيارة Leads Auto Placement في الحساب الثالث): وضع 4 فيديوهات وسادة سيارة بيّاعة في Website Leads + Auto Placement لخفض الـ CPC",
            "cpa_bid": "6.2",
            "budget": "50",
            "tiktok_only_placement": False,
            "use_leads_objective": True,
            "donors": [
                ("1880515858857985", "22-10_flow_hook5_travel.mp4"),
                ("1880515858857985", "21-10_hanan_20-10_1.mp4"),
                ("1880516660641873", "31-10_Car_SnapGads_New_02.mp4"),
                ("1880548063042625", "31-10_Car_Recycled_Ad_03.mp4"),
            ],
        },
        # Blueprint 13: Account 2 Power Bank — Website Leads + Auto Placement
        {
            "key": f"ROEL1_PB_LEADS_AUTO_PLACEMENT_{date_tag}",
            "aadvid": "7631765139798081554",
            "acc_name": "ROELLINGER RECYCLING1",
            "camp_name": f"[EXP-LEADS] power bank 5 Winners Leads AutoPlacement {date_tag}",
            "method_label": "طريقة البيع (13 - الباور بانك Leads Auto Placement في الحساب الثاني): وضع 5 فيديوهات باور بانك رابحة في Website Leads + Auto Placement لخفض الـ CPC وتحفيز الصرف",
            "cpa_bid": "6.4",
            "budget": "50",
            "tiktok_only_placement": False,
            "use_leads_objective": True,
            "donors": [
                ("1879845883154146", "23-10_voltrx_ym49_s07.mp4"),
                ("1879845883154146", "23-10_voltrx_ym49_s06.mp4"),
                ("1880204726651266", "1yr_pb_tt_باور بانك(3)_103s.mp4"),
                ("1880427747180001", "30-10_PB_FlowHook_Ad_03.mp4"),
                ("1880427747180001", "30-10_PB_FlowHook_Ad_04.mp4"),
            ],
        },
        # Blueprint 14: Account 2 Car Armrest — Website Leads + Auto Placement
        {
            "key": f"ROEL1_CAR_LEADS_AUTO_PLACEMENT_{date_tag}",
            "aadvid": "7631765139798081554",
            "acc_name": "ROELLINGER RECYCLING1",
            "camp_name": f"[EXP-LEADS] car armrest 5 Winners Leads AutoPlacement {date_tag}",
            "method_label": "طريقة البيع (14 - وسادة السيارة Leads Auto Placement في الحساب الثاني): وضع 5 فيديوهات وسادة سيارة رابحة في Website Leads + Auto Placement لخفض الـ CPC",
            "cpa_bid": "6.3",
            "budget": "50",
            "tiktok_only_placement": False,
            "use_leads_objective": True,
            "donors": [
                ("1880201944782978", "27-10_Fast_Car_Armrest_Approved_Ad_06.mp4"),
                ("1879741540536498", "21-10_hanan_20-10_1.mp4"),
                ("1879741540536498", "22-10_flow_hook5_travel.mp4"),
                ("1880201944782978", "27-10_Fast_Car_Armrest_Approved_Ad_09.mp4"),
                ("1880201934167762", "27-10_Fast_Car_Armrest_Approved_Ad_04.mp4"),
            ],
        },
    ]

    # Dynamic Hourly Scanner: Automatically find any proven converting videos (Sales >= 1, CPA <= $7.00)
    # that were paused (opt_status=1) due to CPC > $0.35 in In-Feed Only campaigns, top up to 3-5 videos
    # with top converting winners of the same product if needed (Rule 14), and append dynamic
    # Leads Auto Placement + Conversions Auto Placement rescue blueprints for them!
    for acc_name_dyn, aadvid_dyn, tz_dyn in tm.ACCOUNTS[3:]:
        try:
            now_dyn = datetime.now(timezone(timedelta(hours=tz_dyn)))
            t_dyn = now_dyn.strftime("%Y-%m-%d")
            six_dyn = (now_dyn - timedelta(days=6)).strftime("%Y-%m-%d")
            camps_dyn = {str(c.get("campaign_id")): (c.get("campaign_name") or "") for c in tm.fetch_campaigns(session, headers, aadvid_dyn, six_dyn, t_dyn) if c.get("campaign_id")}
            ads_dyn = tm.fetch_ads(session, headers, aadvid_dyn, six_dyn, t_dyn)
            cids_dyn = [str(a.get("creative_id")) for a in ads_dyn if a.get("creative_id") and float((a.get("row_data") or {}).get("stat_cost") or 0) > 0]
            mats_dyn = tm.fetch_expanded_materials(session, headers, aadvid_dyn, cids_dyn[:60], six_dyn, t_dyn) if cids_dyn else []
            choked_pb_donors, choked_car_donors = [], []
            active_pb_winners, active_car_winners = [], []
            seen_pb_v, seen_car_v = set(), set()
            sorted_mats_dyn = sorted(
                mats_dyn,
                key=lambda x: (
                    int(float((x.get("row_data") or x).get("time_attr_convert_cnt") or 0)),
                    -float((x.get("row_data") or x).get("stat_cost") or 0.0),
                ),
                reverse=True,
            )
            for md in sorted_mats_dyn:
                rdd = md.get("row_data") or md
                cv_d = int(float(rdd.get("time_attr_convert_cnt") or 0))
                sp_d = float(rdd.get("stat_cost") or 0.0)
                cl_d = int(float(rdd.get("click_cnt") or rdd.get("clicks") or 0))
                cpc_d = (sp_d / cl_d) if cl_d > 0 else 0.0
                opt_d = str(md.get("mix_material_aco_opt_status") or "0")
                cid_d = str(md.get("campaign_id") or "")
                cname_d = camps_dyn.get(cid_d, "").lower()
                vname_d = (md.get("main_entity_name") or "").strip()
                if cv_d >= 1 and (sp_d / cv_d) <= 7.00 and vname_d and "autoplacement" not in cname_d:
                    is_car_d = any(k in f"{cname_d} {vname_d.lower()}" for k in ("car", "armrest", "cushion", "وساد", "تكاي"))
                    if opt_d != "0" and cpc_d > 0.35:
                        if is_car_d and vname_d not in seen_car_v:
                            seen_car_v.add(vname_d)
                            choked_car_donors.append((cid_d, vname_d))
                        elif not is_car_d and vname_d not in seen_pb_v:
                            seen_pb_v.add(vname_d)
                            choked_pb_donors.append((cid_d, vname_d))
                    elif opt_d == "0":
                        if is_car_d:
                            active_car_winners.append((cid_d, vname_d))
                        else:
                            active_pb_winners.append((cid_d, vname_d))
            for prod_tag, prod_lbl, d_list, act_win_list in [
                ("PB", "power bank", choked_pb_donors, active_pb_winners),
                ("CAR", "car armrest", choked_car_donors, active_car_winners),
            ]:
                if len(d_list) >= 1:
                    combined_donors = list(d_list)
                    seen_names = {vn for _, vn in combined_donors}
                    for ac_cid, ac_vn in act_win_list:
                        if len(combined_donors) >= 5:
                            break
                        if ac_vn not in seen_names:
                            seen_names.add(ac_vn)
                            combined_donors.append((ac_cid, ac_vn))
                    if len(combined_donors) >= 3:
                        d5 = combined_donors[:5]
                        choked_sig = "_".join(sorted(vn[-8:-4] for _, vn in d_list[:3]))
                        EXPERIMENT_BLUEPRINTS.append({
                            "key": f"DYN_{aadvid_dyn[-4:]}_{prod_tag}_LEADS_AUTO_{date_tag}_{choked_sig}",
                            "is_cpc_rescue": True,
                            "aadvid": aadvid_dyn,
                            "acc_name": acc_name_dyn,
                            "camp_name": f"[EXP-LEADS] {prod_lbl} {len(d5)} CPC-Rescued Leads AutoPlacement {date_tag}",
                            "method_label": f"إنقاذ تلقائي للفيديوهات البيّاعة الموقوفة بسبب الـ CPC > $0.35 عبر Website Leads + Auto Placement ({len(d5)} فيديوهات)",
                            "cpa_bid": "6.3",
                            "budget": "50",
                            "tiktok_only_placement": False,
                            "use_leads_objective": True,
                            "donors": d5,
                        })
                        EXPERIMENT_BLUEPRINTS.append({
                            "key": f"DYN_{aadvid_dyn[-4:]}_{prod_tag}_CONV_AUTO_{date_tag}_{choked_sig}",
                            "is_cpc_rescue": True,
                            "aadvid": aadvid_dyn,
                            "acc_name": acc_name_dyn,
                            "camp_name": f"[EXP-LEADS] {prod_lbl} {len(d5)} CPC-Rescued Conv AutoPlacement {date_tag}",
                            "method_label": f"إنقاذ تلقائي للفيديوهات البيّاعة الموقوفة بسبب الـ CPC > $0.35 عبر Website Conversions + Auto Placement ({len(d5)} فيديوهات)",
                            "cpa_bid": "6.3",
                            "budget": "50",
                            "tiktok_only_placement": False,
                            "use_leads_objective": False,
                            "donors": d5,
                        })
        except Exception as e_dyn:
            print(f"  [WARN] Dynamic AutoPlacement scanner ({acc_name_dyn}): {e_dyn}", flush=True)

    # Evening Next-Day Pre-Scheduled Plan Builder (18:00–23:59 KSA):
    # Creates tomorrow's test & super-winner combo campaigns in the evening scheduled for tomorrow 05:00:00 UTC+2 (06:00 AM KSA)
    # so TikTok ad review ('In Review' -> 'Approved') finishes overnight before the 6:00 AM KSA auction!
    if now_sa.hour >= 18:
        tmr_sa = now_sa + timedelta(days=1)
        tmr_tag = tmr_sa.strftime("%d-%m")
        tmr_sched_utc2 = tmr_sa.strftime("%Y-%m-%d 05:00:00")
        for acc_name_tmr, aadvid_tmr, tz_tmr in tm.ACCOUNTS[3:]:
            try:
                now_t = datetime.now(timezone(timedelta(hours=tz_tmr)))
                t_d = now_t.strftime("%Y-%m-%d")
                camps_t = {str(c.get("campaign_id")): (c.get("campaign_name") or "") for c in tm.fetch_campaigns(session, headers, aadvid_tmr, t_d, t_d) if c.get("campaign_id")}
                ads_t = tm.fetch_ads(session, headers, aadvid_tmr, t_d, t_d)
                cids_t = [str(a.get("creative_id")) for a in ads_t if a.get("creative_id") and float((a.get("row_data") or {}).get("stat_cost") or 0) > 0]
                mats_t = tm.fetch_expanded_materials(session, headers, aadvid_tmr, cids_t[:60], t_d, t_d) if cids_t else []
                win_pb_tmr, win_car_tmr = [], []
                seen_pb_t, seen_car_t = set(), set()
                for mt in sorted(mats_t, key=lambda x: int(float((x.get("row_data") or x).get("time_attr_convert_cnt") or 0)), reverse=True):
                    rdt = mt.get("row_data") or mt
                    cv_t = int(float(rdt.get("time_attr_convert_cnt") or 0))
                    sp_t = float(rdt.get("stat_cost") or 0.0)
                    cid_t = str(mt.get("campaign_id") or "")
                    cname_t = camps_t.get(cid_t, "").lower()
                    vname_t = (mt.get("main_entity_name") or "").strip()
                    if cv_t >= 1 and (sp_t / cv_t) <= 7.80 and vname_t:
                        is_car_t = any(k in f"{cname_t} {vname_t.lower()}" for k in ("car", "armrest", "cushion", "وساد", "تكاي"))
                        if is_car_t and vname_t not in seen_car_t:
                            seen_car_t.add(vname_t)
                            win_car_tmr.append((cid_t, vname_t))
                        elif not is_car_t and vname_t not in seen_pb_t:
                            seen_pb_t.add(vname_t)
                            win_pb_tmr.append((cid_t, vname_t))
                for p_tag, p_lbl, p_donors in [("PB", "power bank", win_pb_tmr), ("CAR", "car armrest", win_car_tmr)]:
                    if len(p_donors) >= 3:
                        d_top = p_donors[:5]
                        EXPERIMENT_BLUEPRINTS.append({
                            "key": f"TOMORROW_{tmr_tag}_{p_tag}_SUPERCOMBO_{aadvid_tmr[-4:]}",
                            "aadvid": aadvid_tmr,
                            "acc_name": acc_name_tmr,
                            "camp_name": f"[EXP-LEADS] {p_lbl} {len(d_top)} SuperCombo AutoPlc {tmr_tag}",
                            "method_label": f"خطة الغد المسبقة ({tmr_tag} الساعة 06:00 ص لتجاوز تأخر مراجعة تيك توك ليلاً): تجميع أقوى {len(d_top)} فيديوهات فائزة اليوم في Website Leads + Auto Placement",
                            "cpa_bid": "6.3" if p_tag == "PB" else "6.2",
                            "budget": "50" if p_tag == "PB" else "55",
                            "tiktok_only_placement": False,
                            "use_leads_objective": True,
                            "scheduled_start_time": tmr_sched_utc2,
                            "donors": d_top,
                        })
            except Exception as e_tmr:
                print(f"  [WARN] Evening Tomorrow Plan Scanner ({acc_name_tmr}): {e_tmr}", flush=True)

    launched_this_cycle_per_acc = defaultdict(int)
    for bp in EXPERIMENT_BLUEPRINTS:
        b_key = bp["key"]
        b_aadvid = bp["aadvid"]
        b_acc = bp["acc_name"]
        if b_key in existing_exp_keys:
            continue
        max_allowed_for_bp = 2 if bp.get("is_cpc_rescue") else MAX_ACTIVE_EXPERIMENTS_PER_ACCOUNT
        if active_exp_per_account[b_aadvid] >= max_allowed_for_bp:
            continue
        if launched_this_cycle_per_acc[b_aadvid] >= 2:
            continue

        ok_exp, msg_exp, vids_exp = launch_leads_winner_experiment(
            session,
            headers,
            b_aadvid,
            b_acc,
            bp["camp_name"],
            bp["donors"],
            cpa_bid=bp["cpa_bid"],
            budget=bp["budget"],
            deep_funnel=False,
            tiktok_only_placement=bp["tiktok_only_placement"],
            use_leads_objective=bp["use_leads_objective"],
            scheduled_start_time=bp.get("scheduled_start_time"),
        )
        if ok_exp:
            active_exp_per_account[b_aadvid] += 1
            launched_this_cycle_per_acc[b_aadvid] += 1
            existing_exp_keys.add(b_key)
            act_msg = (
                f"🚀 [تجريب طريقة بيع جديدة] إطلاق '{bp['camp_name']}' (ID={msg_exp}) في {b_acc} — "
                f"{bp['method_label']} — الفيديوهات الفائزة المختارة ({len(vids_exp)}): {vids_exp}"
            )
            new_actions_taken.append(act_msg)
            print(f"  {act_msg}", flush=True)
            campaign_rows.append({
                "acc_name": b_acc,
                "aadvid": b_aadvid,
                "campaign_id": msg_exp,
                "campaign_name": bp["camp_name"],
                "is_active": True,
                "budget": float(bp["budget"]),
                "sp1": 0.0, "cv1": 0, "cpc1": 0.0, "clk1": 0,
                "spy": 0.0, "cvy": 0, "sp2": 0.0, "cv2": 0,
                "sp3": 0.0, "cv3": 0, "sp7": 0.0, "cv7": 0, "sp14": 0.0, "cv14": 0,
            })
            ledger["actions"].append({
                "timestamp": now_sa.strftime("%Y-%m-%d %H:%M"),
                "experiment_key": b_key,
                "is_experiment": True,
                "aadvid": b_aadvid,
                "acc_name": b_acc,
                "campaign_id": msg_exp,
                "campaign_name": bp["camp_name"],
                "action_type": bp["method_label"],
                "videos": vids_exp,
                "base_sp3": 0.0,
                "base_cv3": 0,
                "status": "OPEN",
            })
        else:
            print(f"  [WARN] Experiment {b_key} launch skipped/failed: {msg_exp}", flush=True)

    # ==========================================================================
    # STEP 3B: SCHEDULED NEW VIDEO BATCH & CROSS-PLATFORM WINNER TRANSFER LAUNCHER
    # 1) 5 Recycled Zero-Gift Power Bank Ads for 31-10
    # 2) Cross-Platform Transfer 1 (Snapchat/Google Ads #1 Superstar PB_v10_VoltrxH7 -> 5 Zero-Gift Hook Variations)
    #    - PB_v10_VoltrxH7 generated 28 sales @ $5.81 CPA on Snapchat and 10 real Sheet orders (phk7) on Google Ads,
    #      and its 5 hook variations were completely missing on TikTok!
    # 3) Cross-Platform Transfer 2 (Snapchat/Google Ads 4 New Zero-Gift Car Armrest Cushion Ads 30-10)
    #    - Launched on Snapchat & Google Ads only and missing across all 3 TikTok accounts!
    # ==========================================================================
    RECYCLED_PB_ADS_DIR = Path(r"C:\Users\salah\Desktop\PowerBank_5_Recycled_Ads_And_Hooks_Final\Ads")
    RECYCLED_PB_VIDEOS_31_10 = [
        (RECYCLED_PB_ADS_DIR / "Ad_01_Recycled_Cashier_ApplePay.mp4", "31-10_PB_Recycled_Ad_01.mp4"),
        (RECYCLED_PB_ADS_DIR / "Ad_02_Recycled_BestEver_Review.mp4", "31-10_PB_Recycled_Ad_02.mp4"),
        (RECYCLED_PB_ADS_DIR / "Ad_03_Recycled_4Times_FakeChargers.mp4", "31-10_PB_Recycled_Ad_03.mp4"),
        (RECYCLED_PB_ADS_DIR / "Ad_04_Recycled_ThrewOldInDrawer.mp4", "31-10_PB_Recycled_Ad_04.mp4"),
        (RECYCLED_PB_ADS_DIR / "Ad_05_Recycled_BusySockets_5Percent.mp4", "31-10_PB_Recycled_Ad_05.mp4"),
    ]
    CROSS_PLATFORM_PB_H7_DIR = BASE_DIR / "pb_h7_hooks_5_uploads_30_10"
    CROSS_PLATFORM_PB_H7_VIDEOS = [
        (CROSS_PLATFORM_PB_H7_DIR / "h7varone.mp4", "31-10_PB_H7_SnapGads_Winner_01.mp4"),
        (CROSS_PLATFORM_PB_H7_DIR / "h7vartwo.mp4", "31-10_PB_H7_SnapGads_Winner_02.mp4"),
        (CROSS_PLATFORM_PB_H7_DIR / "h7varthree.mp4", "31-10_PB_H7_SnapGads_Winner_03.mp4"),
        (CROSS_PLATFORM_PB_H7_DIR / "h7varfour.mp4", "31-10_PB_H7_SnapGads_Winner_04.mp4"),
        (CROSS_PLATFORM_PB_H7_DIR / "h7varfive.mp4", "31-10_PB_H7_SnapGads_Winner_05.mp4"),
    ]
    CROSS_PLATFORM_CAR_DIR = BASE_DIR / "car_new4_ads_uploads_30_10"
    CROSS_PLATFORM_CAR_VIDEOS = [
        (CROSS_PLATFORM_CAR_DIR / "car30newone.mp4", "31-10_Car_SnapGads_New_01.mp4"),
        (CROSS_PLATFORM_CAR_DIR / "car30newtwo.mp4", "31-10_Car_SnapGads_New_02.mp4"),
        (CROSS_PLATFORM_CAR_DIR / "car30newthree.mp4", "31-10_Car_SnapGads_New_03.mp4"),
        (CROSS_PLATFORM_CAR_DIR / "car30newfour.mp4", "31-10_Car_SnapGads_New_04.mp4"),
    ]

    NEW_BATCH_LAUNCH_BLUEPRINTS = [
        {
            "key": "LAUNCH_31_10_PB_RECYCLED_5_ADS_ROEL0",
            "aadvid": "7631763556322148369",
            "acc_name": "ROELLINGER RECYCLING",
            "camp_name": "power bank 31-10 Recycled 5 Ads (5 Ads)",
            "videos": RECYCLED_PB_VIDEOS_31_10,
            "product_type": "pb",
            "use_leads_objective": False,
            "cpa_bid": "6.5",
            "budget": "50",
            "scheduled_start_time": "2026-10-31 05:00:00",
            "action_label": "إطلاق وجدولة دفعة الـ 5 فيديوهات الجديدة المعاد تدويرها بدون هدية (Smart+ Conversions + TikTok Feed Only [3000] + Target Cost $6.50)",
        },
        {
            "key": "LAUNCH_31_10_PB_RECYCLED_5_ADS_ROEL1",
            "aadvid": "7631765139798081554",
            "acc_name": "ROELLINGER RECYCLING1",
            "camp_name": "power bank 31-10 Recycled 5 Ads (5 Ads)",
            "videos": RECYCLED_PB_VIDEOS_31_10,
            "product_type": "pb",
            "use_leads_objective": False,
            "cpa_bid": "6.5",
            "budget": "50",
            "scheduled_start_time": "2026-10-31 05:00:00",
            "action_label": "إطلاق وجدولة دفعة الـ 5 فيديوهات الجديدة المعاد تدويرها بدون هدية (Smart+ Conversions + TikTok Feed Only [3000] + Target Cost $6.50)",
        },
        {
            "key": "LAUNCH_31_10_PB_RECYCLED_5_ADS_ROEL2",
            "aadvid": "7631765070816509959",
            "acc_name": "ROELLINGER RECYCLING2",
            "camp_name": "power bank 31-10 Recycled 5 Ads (5 Ads)",
            "videos": RECYCLED_PB_VIDEOS_31_10,
            "product_type": "pb",
            "use_leads_objective": False,
            "cpa_bid": "6.5",
            "budget": "50",
            "scheduled_start_time": "2026-10-31 05:00:00",
            "action_label": "إطلاق وجدولة دفعة الـ 5 فيديوهات الجديدة المعاد تدويرها بدون هدية (Smart+ Conversions + TikTok Feed Only [3000] + Target Cost $6.50)",
        },
        # Cross-Platform Transfer 1: PB_v10_VoltrxH7 (28 sales on Snap @ $5.81, 10 orders on GAds) -> 5 Hook Variations on all 3 TikTok accounts
        {
            "key": "CROSS_PLATFORM_PB_H7_5_HOOKS_ROEL0",
            "aadvid": "7631763556322148369",
            "acc_name": "ROELLINGER RECYCLING",
            "camp_name": "[EXP-LEADS] power bank 5 H7 CrossPlatform 31-10",
            "videos": CROSS_PLATFORM_PB_H7_VIDEOS,
            "product_type": "pb",
            "use_leads_objective": True,
            "cpa_bid": "6.4",
            "budget": "50",
            "scheduled_start_time": "2026-10-31 05:00:00",
            "action_label": "نقل عبر المنصات (Cross-Platform): رفع وتشغيل الـ 5 نسخ بدون هدية للفيديو الأسطوري PB_v10_VoltrxH7 (28 بيعة بـ $5.81 في سناب و10 طلبات في جوجل أدز والغائب عن تيك توك) بخلطة Website Leads + TikTok In-Feed Only [3000] (Target Cost $6.40)",
        },
        {
            "key": "CROSS_PLATFORM_PB_H7_5_HOOKS_ROEL1",
            "aadvid": "7631765139798081554",
            "acc_name": "ROELLINGER RECYCLING1",
            "camp_name": "[EXP-LEADS] power bank 5 H7 CrossPlatform 31-10",
            "videos": CROSS_PLATFORM_PB_H7_VIDEOS,
            "product_type": "pb",
            "use_leads_objective": True,
            "cpa_bid": "6.4",
            "budget": "50",
            "scheduled_start_time": "2026-10-31 05:00:00",
            "action_label": "نقل عبر المنصات (Cross-Platform): رفع وتشغيل الـ 5 نسخ بدون هدية للفيديو الأسطوري PB_v10_VoltrxH7 (28 بيعة بـ $5.81 في سناب و10 طلبات في جوجل أدز والغائب عن تيك توك) بخلطة Website Leads + TikTok In-Feed Only [3000] (Target Cost $6.40)",
        },
        {
            "key": "CROSS_PLATFORM_PB_H7_5_HOOKS_ROEL2",
            "aadvid": "7631765070816509959",
            "acc_name": "ROELLINGER RECYCLING2",
            "camp_name": "[EXP-LEADS] power bank 5 H7 CrossPlatform 31-10",
            "videos": CROSS_PLATFORM_PB_H7_VIDEOS,
            "product_type": "pb",
            "use_leads_objective": True,
            "cpa_bid": "6.4",
            "budget": "50",
            "scheduled_start_time": "2026-10-31 05:00:00",
            "action_label": "نقل عبر المنصات (Cross-Platform): رفع وتشغيل الـ 5 نسخ بدون هدية للفيديو الأسطوري PB_v10_VoltrxH7 (28 بيعة بـ $5.81 في سناب و10 طلبات في جوجل أدز والغائب عن تيك توك) بخلطة Website Leads + TikTok In-Feed Only [3000] (Target Cost $6.40)",
        },
        # Cross-Platform Transfer 2: 4 New Zero-Gift Car Armrest Ads (launched on Snap & GAds on 30-10, missing on TikTok) -> all 3 TikTok accounts
        {
            "key": "CROSS_PLATFORM_CAR_4_NEW_ROEL0",
            "aadvid": "7631763556322148369",
            "acc_name": "ROELLINGER RECYCLING",
            "camp_name": "[EXP-LEADS] car armrest 4 CrossPlatform 31-10",
            "videos": CROSS_PLATFORM_CAR_VIDEOS,
            "product_type": "car",
            "use_leads_objective": True,
            "cpa_bid": "6.3",
            "budget": "50",
            "scheduled_start_time": "2026-10-31 05:00:00",
            "action_label": "نقل عبر المنصات (Cross-Platform): رفع وتشغيل الـ 4 فيديوهات الجديدة لوسادة السيارة (المطلقة في سناب وجوجل أدز فقط والغائبة عن تيك توك) بخلطة Website Leads + TikTok In-Feed Only [3000] (Target Cost $6.30)",
        },
        {
            "key": "CROSS_PLATFORM_CAR_4_NEW_ROEL1",
            "aadvid": "7631765139798081554",
            "acc_name": "ROELLINGER RECYCLING1",
            "camp_name": "[EXP-LEADS] car armrest 4 CrossPlatform 31-10",
            "videos": CROSS_PLATFORM_CAR_VIDEOS,
            "product_type": "car",
            "use_leads_objective": True,
            "cpa_bid": "6.3",
            "budget": "50",
            "scheduled_start_time": "2026-10-31 05:00:00",
            "action_label": "نقل عبر المنصات (Cross-Platform): رفع وتشغيل الـ 4 فيديوهات الجديدة لوسادة السيارة (المطلقة في سناب وجوجل أدز فقط والغائبة عن تيك توك) بخلطة Website Leads + TikTok In-Feed Only [3000] (Target Cost $6.30)",
        },
        {
            "key": "CROSS_PLATFORM_CAR_4_NEW_ROEL2",
            "aadvid": "7631765070816509959",
            "acc_name": "ROELLINGER RECYCLING2",
            "camp_name": "[EXP-LEADS] car armrest 4 CrossPlatform 31-10",
            "videos": CROSS_PLATFORM_CAR_VIDEOS,
            "product_type": "car",
            "use_leads_objective": True,
            "cpa_bid": "6.3",
            "budget": "50",
            "scheduled_start_time": "2026-10-31 05:00:00",
            "action_label": "نقل عبر المنصات (Cross-Platform): رفع وتشغيل الـ 4 فيديوهات الجديدة لوسادة السيارة (المطلقة في سناب وجوجل أدز فقط والغائبة عن تيك توك) بخلطة Website Leads + TikTok In-Feed Only [3000] (Target Cost $6.30)",
        },
    ]

    for nbp in []:  # Disabled automatic mass batch launching without explicit human verification
        n_key = nbp["key"]
        if n_key in existing_exp_keys:
            continue
        ok_nb, msg_nb, vids_nb = launch_new_video_batch_campaign(
            session,
            headers,
            nbp["aadvid"],
            nbp["acc_name"],
            nbp["camp_name"],
            nbp["videos"],
            cpa_bid=nbp["cpa_bid"],
            budget=nbp["budget"],
            scheduled_start_time=nbp["scheduled_start_time"],
            tiktok_only_placement=True,
            product_type=nbp.get("product_type", "pb"),
            use_leads_objective=nbp.get("use_leads_objective", False),
        )
        if ok_nb:
            existing_exp_keys.add(n_key)
            history.setdefault("user_protected_tiktok_campaigns", {})[f"{nbp['aadvid']}:{msg_nb}"] = {
                "account_name": nbp["acc_name"],
                "aadvid": nbp["aadvid"],
                "campaign_id": str(msg_nb),
                "campaign_name": nbp["camp_name"],
                "protected_at": now_sa.isoformat(),
                "reason": nbp.get("action_label") or "Launched via Hourly TikTok Sales Master",
            }
            tm.save_history(history)
            obj_lbl = "Website Leads [obj=8]" if nbp.get("use_leads_objective") else "Smart+ Conversions"
            act_nb_msg = (
                f"🚀 [نقل وإطلاق عبر المنصات] جدولة وإطلاق '{nbp['camp_name']}' (ID={msg_nb}) في {nbp['acc_name']} "
                f"({obj_lbl} | Target Cost ${nbp['cpa_bid']} | Budget ${nbp['budget']} | TikTok In-Feed Only [3000] | "
                f"0% ذكر هدية | مجدولة للساعة 06:00 ص بتوقيت السعودية) — الفيديوهات ({len(vids_nb)}): {vids_nb}"
            )
            new_actions_taken.append(act_nb_msg)
            print(f"  {act_nb_msg}", flush=True)
            campaign_rows.append({
                "acc_name": nbp["acc_name"],
                "aadvid": nbp["aadvid"],
                "campaign_id": str(msg_nb),
                "campaign_name": nbp["camp_name"],
                "is_active": True,
                "budget": float(nbp["budget"]),
                "sp1": 0.0, "cv1": 0, "cpc1": 0.0, "clk1": 0,
                "spy": 0.0, "cvy": 0, "sp2": 0.0, "cv2": 0,
                "sp3": 0.0, "cv3": 0, "sp7": 0.0, "cv7": 0, "sp14": 0.0, "cv14": 0,
            })
            ledger["actions"].append({
                "timestamp": now_sa.strftime("%Y-%m-%d %H:%M"),
                "experiment_key": n_key,
                "is_experiment": bool(nbp.get("use_leads_objective")),
                "aadvid": nbp["aadvid"],
                "acc_name": nbp["acc_name"],
                "campaign_id": str(msg_nb),
                "campaign_name": nbp["camp_name"],
                "action_type": nbp.get("action_label") or "إطلاق دفعة فيديوهات عبر المنصات",
                "videos": vids_nb,
                "base_sp3": 0.0,
                "base_cv3": 0,
                "status": "OPEN",
            })
        else:
            print(f"  [WARN] New Batch {n_key} launch failed: {msg_nb}", flush=True)

    # ==========================================================================
    # STEP 3C: HISTORICAL REGIME & STRUCTURE COMPARATIVE ANALYSIS ENGINE
    # Compares profitable periods vs. weak periods (1D vs 3D vs 7D) across all tried campaign structures
    # to extract the #1 winning formula and steer upcoming campaigns automatically.
    # ==========================================================================
    tt_structure_buckets = {
        "Website Leads [obj=8] + Auto Placement [3000,9000,1000] + 3–5 فيديوهات رابحة": {
            "sp1": 0.0, "cv1": 0, "sp3": 0.0, "cv3": 0, "sp7": 0.0, "cv7": 0,
            "decision": "✅ التركيبة رقم #1 اليوم لإنقاذ الـ CPC (< $0.22) وتوليد مبيعات فورية (SuperCombo / CPC-Rescued)",
        },
        "Website Leads [obj=8] + TikTok In-Feed Only [3000] + فيديوهات عابرة للمنصات": {
            "sp1": 0.0, "cv1": 0, "sp3": 0.0, "cv3": 0, "sp7": 0.0, "cv7": 0,
            "decision": "✅ التركيبة رقم #1 في حجم المبيعات لـ Cross-Platform Winners (مثل H7 صاحبة 43 بيعة بـ $5.25)",
        },
        "Smart+ Website Conversions + 3–4 فيديوهات رابحة معاد تدويرها (30d Batch)": {
            "sp1": 0.0, "cv1": 0, "sp3": 0.0, "cv3": 0, "sp7": 0.0, "cv7": 0,
            "decision": "✅ ناجحة عند حصر الحملة في 3–4 فيديوهات مجربة وتصفية الفيديوهات الخاسرة عند $1.00 و CPC > $0.25",
        },
        "حملات التوسيع المكررة (SCALE / Copy) أو تجميع > 6 فيديوهات خام": {
            "sp1": 0.0, "cv1": 0, "sp3": 0.0, "cv3": 0, "sp7": 0.0, "cv7": 0,
            "decision": "🚫 سبب الفترات الخاسرة (يرفع CPC فوق $0.35 ويسحب المزاد من الحملة الأم — ممنوع نهائياً)",
        },
    }
    for cr in campaign_rows:
        cn_l = (cr.get("campaign_name") or "").lower()
        if " scale" in cn_l or "copy" in cn_l:
            b_k = "حملات التوسيع المكررة (SCALE / Copy) أو تجميع > 6 فيديوهات خام"
        elif "[exp-leads]" in cn_l and ("autoplc" in cn_l or "autoplacement" in cn_l):
            b_k = "Website Leads [obj=8] + Auto Placement [3000,9000,1000] + 3–5 فيديوهات رابحة"
        elif "[exp-leads]" in cn_l:
            b_k = "Website Leads [obj=8] + TikTok In-Feed Only [3000] + فيديوهات عابرة للمنصات"
        else:
            b_k = "Smart+ Website Conversions + 3–4 فيديوهات رابحة معاد تدويرها (30d Batch)"
        tt_structure_buckets[b_k]["sp1"] = round(tt_structure_buckets[b_k]["sp1"] + float(cr.get("sp1") or 0.0), 2)
        tt_structure_buckets[b_k]["cv1"] += int(cr.get("cv1") or 0)
        tt_structure_buckets[b_k]["sp3"] = round(tt_structure_buckets[b_k]["sp3"] + float(cr.get("sp3") or 0.0), 2)
        tt_structure_buckets[b_k]["cv3"] += int(cr.get("cv3") or 0)
        tt_structure_buckets[b_k]["sp7"] = round(tt_structure_buckets[b_k]["sp7"] + float(cr.get("sp7") or 0.0), 2)
        tt_structure_buckets[b_k]["cv7"] += int(cr.get("cv7") or 0)

    ledger["latest_winning_structure_analysis"] = tt_structure_buckets
    ledger.setdefault("cycles", []).append({
        "timestamp": now_sa.strftime("%Y-%m-%d %H:%M"),
        "actions_count": len(new_actions_taken),
        "followups_count": len(ledger_followups),
    })
    ledger["cycles"] = ledger["cycles"][-72:]
    save_ledger(ledger)

    # ==========================================================================
    # STEP 4: WRITE DETAILED ARABIC REPORT (MAX 5 COLUMNS PER TABLE)
    # ==========================================================================
    lines = [
        f"# 🎯 تقرير أتمتة تيك توك المستقلة (كل ساعة — Hourly Sales Master) — {now_sa:%Y-%m-%d %H:%M} (توقيت السعودية)",
        "",
        "## 1. مراجعة وتقييم خطوات الساعات الماضية (`Hourly Ledger Audit`)",
    ]
    if ledger_followups:
        for lf in ledger_followups:
            lines.append(f"- {lf}")
    else:
        lines.append("- هذه أول دورة تأسيسية للسجل الجديد؛ تم توثيق الخطوات المنفذة الآن ليتم تقييمها بالأرقام في الساعة القادمة.")

    lines.extend([
        "",
        "## 2. إجراءات زيادة عدد المبيعات والتجارب المنفذة في هذه الساعة (`Sales Scaling & Experimentation Actions`)",
    ])
    if new_actions_taken:
        for na in new_actions_taken:
            lines.append(f"- {na}")
    else:
        lines.append("- لم يتم إطلاق تجارب جديدة في هذه الساعة لمنع تشتيت الميزانية؛ راجع جدول إجمالي الحسابات والحملات أدناه لتقييم الربحية الحقيقية بعين المراقب البشري.")

    lines.extend([
        "",
        "## 2-B. فحص فرص السكيل الإلزامي في هذه الدورة (`Mandatory Hourly Scaling Opportunities Audit`)",
    ])
    if scaling_opportunities_evaluated:
        for soe in scaling_opportunities_evaluated:
            lines.append(f"- {soe}")
    else:
        lines.append("- تم فحص جميع الحملات النشطة ولم تتوفر فرصة سكيل جديدة في هذه الساعة (إما لعدم دخول مبيعات جديدة بعد آخر ترقية أو لأننا خارج نافذة السكيل النهارية 06:00–17:00).")

    lines.extend([
        "",
        "## 2-C. تحليل الفترات الرابحة مقابل الفترات الضعيفة وأفضل تركيبة للحملات القادمة (`Regime & Structure Analysis — 5 أعمدة`)",
        "| التركيبة / الهيكلة المجربة | اليوم (`1D`) | آخر 3 أيام (`3D`) | آخر 7 أيام (`7D`) | القرار الاستراتيجي للحملات القادمة |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    for st_name, st_v in tt_structure_buckets.items():
        c1_b = f"${st_v['sp1']/st_v['cv1']:.2f}" if st_v["cv1"] > 0 else "—"
        c3_b = f"${st_v['sp3']/st_v['cv3']:.2f}" if st_v["cv3"] > 0 else "—"
        c7_b = f"${st_v['sp7']/st_v['cv7']:.2f}" if st_v["cv7"] > 0 else "—"
        lines.append(
            f"| **{st_name}** | `${st_v['sp1']:.2f}` / **{st_v['cv1']}** (`{c1_b}`) | "
            f"`${st_v['sp3']:.2f}` / **{st_v['cv3']}** (`{c3_b}`) | "
            f"`${st_v['sp7']:.2f}` / **{st_v['cv7']}** (`{c7_b}`) | {st_v['decision']} |"
        )

    lines.extend([
        "",
        "## 3. مقارنة أداء حسابات تيك توك الـ 6 (`حد أقصى 5 أعمدة`)",
        "| الحساب | اليوم (`1D`) | أمس (`Yesterday`) | آخر 3 أيام (`3D`) | آخر 7 أيام (`7D`) |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    for s in account_snapshots:
        c1_s = f"${s['cpa1']:.2f}" if s["cpa1"] is not None else "—"
        cy_s = f"${s['cpay']:.2f}" if s["cpay"] is not None else "—"
        c3_s = f"${s['cpa3']:.2f}" if s["cpa3"] is not None else "—"
        c7_s = f"${s['cpa7']:.2f}" if s["cpa7"] is not None else "—"
        lines.append(
            f"| **{s['acc_name']}** | `${s['sp1']:.2f}` / **{s['cv1']}** (`{c1_s}`) | "
            f"`${s['spy']:.2f}` / **{s['cvy']}** (`{cy_s}`) | "
            f"`${s['sp3']:.2f}` / **{s['cv3']}** (`{c3_s}`) | "
            f"`${s['sp7']:.2f}` / **{s['cv7']}** (`{c7_s}`) |"
        )

    lines.extend([
        "",
        "## 4. تفصيل الحملات النشطة وذات الصرف في آخر 3 أيام (`حد أقصى 5 أعمدة`)",
        "| الحساب والحملة | الحالة والميزانية | اليوم (`1D`) | أمس (`Yesterday`) | آخر 3 أيام (`3D`) |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    for cr in sorted(campaign_rows, key=lambda x: (not x["is_active"], -x["sp1"], -x["sp3"])):
        st_lbl = "🟢 نشطة" if cr["is_active"] else "⏸️ موقوفة"
        cpa1_s = f"${cr['sp1']/cr['cv1']:.2f}" if cr["cv1"] > 0 else "—"
        cpay_s = f"${cr['spy']/cr['cvy']:.2f}" if cr["cvy"] > 0 else "—"
        cpa3_s = f"${cr['sp3']/cr['cv3']:.2f}" if cr["cv3"] > 0 else "—"
        lines.append(
            f"| **{cr['acc_name']}** — `{cr['campaign_name']}` | {st_lbl} (`${cr['budget']:.0f}`) | "
            f"`${cr['sp1']:.2f}` / **{cr['cv1']}** (`{cpa1_s}`) | "
            f"`${cr['spy']:.2f}` / **{cr['cvy']}** (`{cpay_s}`) | "
            f"`${cr['sp3']:.2f}` / **{cr['cv3']}** (`{cpa3_s}`) |"
        )

    report_txt = "\n".join(lines) + "\n"
    REPORT_FILE.write_text(report_txt, encoding="utf-8")
    print("\n" + report_txt, flush=True)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        try:
            from github_deploy import sync_state_and_rules_to_github
            sync_state_and_rules_to_github("tiktok_2h_sales_master")
        except Exception as _sync_err:
            print(f"ℹ️ ملاحظة مزامنة GitHub من tiktok_2h_sales_master: {_sync_err}", flush=True)



    # --- Auto-run Sales Stall Detector & Immediate Recovery + Historical Master Advisor ---
    if os.environ.get("GITHUB_ACTIONS") != "true":
        try:
            import adaptive_bid_sweetspot_tuner
            adaptive_bid_sweetspot_tuner.tune_tiktok_converting_campaigns(dry_run=False)
            import sales_stall_recovery_engine
            sales_stall_recovery_engine.run_stall_detector_and_recovery(dry_run=False)
        except Exception as _st_err:
            print(f"ℹ️ ملاحظة محرك استعادة المبيعات اللحظي: {_st_err}")
        try:
            import historical_and_consultation_master_advisor
            historical_and_consultation_master_advisor.main()
            import subprocess as _sp_exp
            _sp_exp.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve().parent / 'proactive_domain_expert_advisor.py'), '--domain', 'tiktok'], check=False)
        except Exception as _adv_err:
            print(f"ℹ️ ملاحظة المستشار التاريخي والتنفيذي: {_adv_err}")


if __name__ == "__main__":
    main()
