import json
import os
import socket
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
COOKIES_FILE = BASE_DIR / "tiktok_cookies.json"
HISTORY_FILE = BASE_DIR / "paused_history.json"

ACCOUNTS = [
    ("1035 Susan LLC0309", "7615303174180814849", -4),
    ("CN-Manzilk", "7462914566645694480", 1),
    ("Otyss - Ma - 138", "7649010597308301332", 3),
    ("ROELLINGER RECYCLING", "7631763556322148369", 2),
    ("ROELLINGER RECYCLING1", "7631765139798081554", 2),
    ("ROELLINGER RECYCLING2", "7631765070816509959", 2),
]

# Rules configured per user request:
# Campaign Level:
# 1. Campaign spend >= $18.00 in 1 day (Today) or 2 days (Yesterday + Today) with 0 conversions -> Pause Campaign
# 2. Campaign CPA (cost per sale) >= $21.00 in 1 day (Today) or 2 days (Yesterday + Today) -> Pause Campaign
# Video Level (Smart+):
# 1. Video spend > $6.00 in 1 day (Today) with 0 conversions -> Pause Video
# 2. Video spend > $6.00 in 2 days (Yesterday + Today) with 0 conversions -> Pause Video
# 3. Video spend >= $1.00 and CPC > $0.35 (or 0 clicks) -> Pause Video
#    * EXCEPTION: Never pause for high CPC if the video itself has conversions at a low CPA (< $13.50)
#      OR if the parent campaign is a low-CPA winning campaign (Campaign CPA <= $10.00, e.g. Hanan + Ola).
MAX_CAMPAIGN_NO_SALE_SPEND = 18.00
MAX_CAMPAIGN_CPA = 21.00
MAX_NO_SALE_SPEND = 6.00
MAX_VIDEO_CPA = 13.50
LOW_CAMPAIGN_CPA_EXEMPTION = 10.00
MIN_SPEND_FOR_CPC = 1.00
MAX_CPC = 0.35


ORIGIN_MATERIAL_TYPES = [
    "no_post_video",
    "post_video",
    "no_post_carousel",
    "post_carousel",
    "catalog_manual_video",
    "catalog_tpl_carousel",
    "catalog_tpl_video",
    "no_post_single_image",
    "catalog_tpl_multi_show",
]


def load_cookies():
    env_cookies = os.environ.get("TIKTOK_COOKIES_JSON", "").strip()
    if env_cookies:
        return json.loads(env_cookies)
    if COOKIES_FILE.exists():
        return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    raise RuntimeError("No TikTok cookies found in TIKTOK_COOKIES_JSON or tiktok_cookies.json")


def load_history():
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("adsquads", {})
    data.setdefault("ads", {})
    data.setdefault("tiktok_campaigns", {})
    data.setdefault("tiktok_videos", {})
    data.setdefault("tiktok_ads", {})
    return data


def save_history(data):
    HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_session(cookies_raw):
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)

    csrf = ""
    for c in cookies_raw:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain", ".tiktok.com"))
        if c["name"] == "csrftoken":
            csrf = c["value"]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://ads.tiktok.com",
        "x-csrftoken": csrf,
    }
    return s, headers


def parse_draft_ids(raw_val):
    cleaned = str(raw_val or "").strip().strip("[]")
    if not cleaned:
        return []
    return [x.strip().strip("\"'") for x in cleaned.split(",") if x.strip().strip("\"'")]


def fetch_campaigns(session, headers, aadvid, st_date, et_date):
    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
    body = {
        "common_req": {
            "compare_et": "",
            "compare_st": "",
            "dimensions": ["campaign_id"],
            "filters": [
                {"field": "campaign_status", "in_field_values": ["delete"], "filter_type": 10},
                {"field": "campaign_system_origin", "in_field_values": ["100000"], "filter_type": 0},
            ],
            "metrics": [
                "stat_cost",
                "cpc",
                "cpm",
                "show_cnt",
                "click_cnt",
                "ctr",
                "time_attr_convert_cnt",
                "time_attr_conversion_cost",
            ],
            "sort_stat": "stat_cost",
            "sort_order": 1,
            "st": st_date,
            "et": et_date,
            "lifetime": 0,
            "page": 1,
            "page_size": 100,
        },
        "extra": {
            "ab_primary_status": "v2",
            "get_compare_diff": "0",
            "get_compare_rate": "0",
            "get_compare_value": "0",
            "list_version": "v2",
            "region_schedule": "1",
        },
    }
    r = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/statistics/op/campaign/list/?aadvid={aadvid}&req_src=bidding",
        headers=h,
        json=body,
        timeout=20,
    ).json()
    return r.get("data", {}).get("table", []) or []


def pause_campaign(session, headers, aadvid, campaign_id):
    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
    payload = {
        "campaign_list": json.dumps([str(campaign_id)]),
        "operation": "disable",
        "allow_partial_success": True,
        "ad_channel": 1,
    }
    r = session.post(
        f"https://ads.tiktok.com/api/v2/i18n/overture/campaign/update_status/?aadvid={aadvid}&req_src=bidding",
        headers=h,
        json=payload,
        timeout=20,
    ).json()
    ok = r.get("code") == 0
    return ok, json.dumps(r, ensure_ascii=False)


def evaluate_campaign_rules(spend_1d, conv_1d, spend_2d, conv_2d):
    reasons = []
    # Rule 1: Campaign Spend >= $18 today with 0 conversions
    if spend_1d >= MAX_CAMPAIGN_NO_SALE_SPEND and conv_1d == 0:
        reasons.append(f"Campaign Today Spend ${spend_1d:.2f} >= ${MAX_CAMPAIGN_NO_SALE_SPEND:.2f} with 0 sales")
    # Rule 2: Campaign Spend >= $18 over 2 days (Yesterday + Today) with 0 conversions
    if spend_2d >= MAX_CAMPAIGN_NO_SALE_SPEND and conv_2d == 0:
        reasons.append(f"Campaign 2-Day Spend ${spend_2d:.2f} >= ${MAX_CAMPAIGN_NO_SALE_SPEND:.2f} with 0 sales")
    # Rule 3: Campaign Today CPA >= $21 (with >= 1 conversion)
    if conv_1d > 0 and (spend_1d / conv_1d) >= MAX_CAMPAIGN_CPA:
        reasons.append(
            f"Campaign Today CPA ${(spend_1d / conv_1d):.2f} >= ${MAX_CAMPAIGN_CPA:.2f} "
            f"(Spend=${spend_1d:.2f}, Sales={conv_1d})"
        )
    # Rule 4: Campaign 2-Day CPA >= $21 (with >= 1 conversion)
    if conv_2d > 0 and (spend_2d / conv_2d) >= MAX_CAMPAIGN_CPA:
        reasons.append(
            f"Campaign 2-Day CPA ${(spend_2d / conv_2d):.2f} >= ${MAX_CAMPAIGN_CPA:.2f} "
            f"(Spend=${spend_2d:.2f}, Sales={conv_2d})"
        )
    return reasons


def fetch_ads(session, headers, aadvid, st_date, et_date):
    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/manage/creative?aadvid={aadvid}"
    body = {
        "common_req": {
            "compare_et": "",
            "compare_st": "",
            "dimensions": ["creative_id"],
            "filters": [
                {"field": "creative_status", "in_field_values": ["delete"], "filter_type": 10},
                {"field": "campaign_system_origin", "in_field_values": ["100000"], "filter_type": 0},
            ],
            "metrics": [
                "stat_cost",
                "cpc",
                "cpm",
                "show_cnt",
                "click_cnt",
                "ctr",
                "time_attr_convert_cnt",
                "time_attr_conversion_cost",
            ],
            "sort_stat": "stat_cost",
            "sort_order": 1,
            "st": st_date,
            "et": et_date,
            "lifetime": 0,
            "page": 1,
            "page_size": 100,
        },
        "extra": {
            "ab_primary_status": "v2",
            "get_compare_diff": "0",
            "get_compare_rate": "0",
            "get_compare_value": "0",
            "list_version": "v2",
            "region_schedule": "1",
        },
    }
    r = session.post(
        f"https://ads.tiktok.com/api/v4/i18n/statistics/op/ad/list/?aadvid={aadvid}&req_src=bidding",
        headers=h,
        json=body,
        timeout=20,
    ).json()
    return r.get("data", {}).get("table", []) or []


def fetch_expanded_materials(session, headers, aadvid, creative_ids, st_date, et_date):
    if not creative_ids:
        return []
    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/manage/creative?aadvid={aadvid}"
    all_rows = []
    page = 1
    while True:
        body = {
            "common_req": {
                "dimensions": ["main_entity_id", "main_entity_type", "creative_id", "ad_id", "campaign_id"],
                "filters": [
                    {
                        "field": "origin_material_type",
                        "filter_type": 0,
                        "in_field_values": ORIGIN_MATERIAL_TYPES,
                    },
                    {
                        "field": "is_del",
                        "filter_type": 0,
                        "in_field_values": ["0", "1"],
                    },
                    {
                        "field": "creative_id",
                        "filter_type": 0,
                        "in_field_values": [str(cid) for cid in creative_ids],
                    },
                ],
                "metrics": [
                    "stat_cost",
                    "cpc",
                    "cpm",
                    "show_cnt",
                    "click_cnt",
                    "ctr",
                    "time_attr_convert_cnt",
                    "time_attr_conversion_cost",
                    "main_entity_name",
                    "ad_material_draft_id",
                    "material_primary_status",
                    "material_second_status_list",
                    "transitional_id",
                    "mix_material_aco_opt_status",
                    "mix_material_item_source",
                    "mix_material_carousel_id",
                ],
                "sort_stat": "stat_cost",
                "sort_order": 1,
                "st": st_date,
                "et": et_date,
                "lifetime": 0,
                "compare_st": "",
                "compare_et": "",
                "page": page,
                "page_size": 100,
                "extra": {},
            },
            "extra": {
                "ab_primary_status": "v2",
                "get_compare_diff": "0",
                "get_compare_rate": "0",
                "get_compare_value": "0",
                "list_version": "v2",
                "region_schedule": "1",
            },
            "report_id": "mix_material",
        }
        r = session.post(
            f"https://ads.tiktok.com/api/v4/i18n/statistics/op/expand/material/list/?aadvid={aadvid}&req_src=bidding",
            headers=h,
            json=body,
            timeout=20,
        ).json()
        table = r.get("data", {}).get("table", []) or []
        all_rows.extend(table)
        pag = r.get("data", {}).get("pagination", {}) or {}
        if page >= int(pag.get("page_count") or 1) or not table:
            break
        page += 1
    return all_rows


def pause_smart_plus_video(session, headers, aadvid, ad_id, row):
    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/manage/creative?aadvid={aadvid}"
    entity_type = row.get("main_entity_type") or ""
    item_source = str(row.get("mix_material_item_source") or "")
    transitional_id = str(row.get("transitional_id") or "")
    draft_ids = parse_draft_ids(row.get("ad_material_draft_id"))

    material_list = []
    carousel_id_list = []
    if entity_type == "no_post_carousel" or (entity_type == "post_carousel" and item_source == "3"):
        if transitional_id:
            carousel_id_list.append(transitional_id)
            material_list.append(transitional_id)
    else:
        material_list.extend(draft_ids)

    if not material_list:
        return False, "Empty material_list"

    payload = {
        "ad_id": str(ad_id),
        "material_list": [str(m) for m in material_list],
        "carousel_id_list": [str(c) for c in carousel_id_list],
        "operation": "disable",
        "ad_channel": 1,
        "risk_info": {},
    }
    r = session.post(
        f"https://ads.tiktok.com/api/v3/i18n/overture/procedural_material/update_status/?aadvid={aadvid}&req_src=bidding",
        headers=h,
        json=payload,
        timeout=20,
    ).json()
    ok = r.get("code") == 0
    return ok, json.dumps(r, ensure_ascii=False)


def evaluate_video_rules(spend_1d, conv_1d, cpc_1d, clicks_1d, spend_2d, conv_2d, cpc_2d, clicks_2d, camp_low_cpa=False):
    reasons = []
    has_profitable_2d = conv_2d > 0 and (spend_2d / conv_2d) < MAX_VIDEO_CPA
    has_profitable_1d = conv_1d > 0 and (spend_1d / conv_1d) < MAX_VIDEO_CPA
    has_profitable_sales = has_profitable_2d or has_profitable_1d

    # Rule 1: Spend > $6 today with 0 conversions (unless 2-day CPA is already profitable)
    if spend_1d > MAX_NO_SALE_SPEND and conv_1d == 0 and not has_profitable_2d:
        reasons.append(f"Today Spend ${spend_1d:.2f} > ${MAX_NO_SALE_SPEND:.2f} with 0 sales")
    # Rule 2: Spend > $6 over 2 days (Yesterday + Today) with 0 conversions
    if spend_2d > MAX_NO_SALE_SPEND and conv_2d == 0:
        reasons.append(f"2-Day Spend ${spend_2d:.2f} > ${MAX_NO_SALE_SPEND:.2f} with 0 sales")
    # Rule 3: Video Today CPA >= $13.50 (unless 2-day CPA is profitable)
    if conv_1d > 0 and (spend_1d / conv_1d) >= MAX_VIDEO_CPA and not has_profitable_2d:
        reasons.append(f"Today Video CPA ${(spend_1d / conv_1d):.2f} >= ${MAX_VIDEO_CPA:.2f} (Spend=${spend_1d:.2f}, Sales={conv_1d})")
    # Rule 4: Video 2-Day CPA >= $13.50 (with >= 1 conversion)
    if conv_2d > 0 and (spend_2d / conv_2d) >= MAX_VIDEO_CPA:
        reasons.append(f"2-Day Video CPA ${(spend_2d / conv_2d):.2f} >= ${MAX_VIDEO_CPA:.2f} (Spend=${spend_2d:.2f}, Sales={conv_2d})")

    # Rule 5 & 6: CPC Cut (EXEMPT if video has profitable sales OR parent campaign has low CPA <= $10.00)
    if not has_profitable_sales and not camp_low_cpa:
        if spend_1d >= MIN_SPEND_FOR_CPC and (cpc_1d > MAX_CPC or clicks_1d == 0):
            reasons.append(f"Today Spend ${spend_1d:.2f} >= ${MIN_SPEND_FOR_CPC:.2f} & CPC ${cpc_1d:.2f} > ${MAX_CPC:.2f}")
        elif spend_2d >= MIN_SPEND_FOR_CPC and (cpc_2d > MAX_CPC or clicks_2d == 0):
            reasons.append(f"2-Day Spend ${spend_2d:.2f} >= ${MIN_SPEND_FOR_CPC:.2f} & CPC ${cpc_2d:.2f} > ${MAX_CPC:.2f}")
    return reasons



def main():
    cookies_raw = load_cookies()
    history = load_history()
    session, headers = build_session(cookies_raw)

    total_campaigns_paused = 0
    total_paused = 0
    for acc_name, aadvid, utc_offset in ACCOUNTS:
        now_tz = datetime.now(timezone(timedelta(hours=utc_offset)))
        today_str = now_tz.strftime("%Y-%m-%d")
        yesterday_str = (now_tz - timedelta(days=1)).strftime("%Y-%m-%d")

        # 1. Campaign-Level Rule Check (Spend >= $18.00 with 0 sales)
        camps_1d = fetch_campaigns(session, headers, aadvid, today_str, today_str)
        camps_2d = fetch_campaigns(session, headers, aadvid, yesterday_str, today_str)
        camp_1d_map = {str(c.get("campaign_id")): c for c in camps_1d if c.get("campaign_id")}
        active_camps = [
            c for c in camps_2d
            if c.get("primary_status") not in ("disable", "delete")
            and str(c.get("opt_status")) == "0"
        ]

        ads_2d = fetch_ads(session, headers, aadvid, yesterday_str, today_str)
        active_ads = [
            a for a in ads_2d
            if a.get("primary_status") not in ("disable", "delete")
            and str(a.get("opt_status")) == "0"
        ]
        active_cids = [str(a.get("creative_id")) for a in active_ads if a.get("creative_id")]
        print(
            f"\n=== [{acc_name}] ({aadvid}) | Date: {yesterday_str} -> {today_str} | "
            f"Active Campaigns: {len(active_camps)} | Active Ads: {len(active_cids)} ==="
        )

        camp_low_cpa_map = {}
        for c2 in active_camps:
            camp_id = str(c2.get("campaign_id") or "")
            camp_name = c2.get("campaign_name") or "Campaign"
            c1 = camp_1d_map.get(camp_id, {})
            rd1 = c1.get("row_data") or {}
            rd2 = c2.get("row_data") or {}

            c_spend_1d = float(rd1.get("stat_cost") or 0.0)
            c_conv_1d = int(float(rd1.get("time_attr_convert_cnt") or 0))
            c_spend_2d = float(rd2.get("stat_cost") or 0.0)
            c_conv_2d = int(float(rd2.get("time_attr_convert_cnt") or 0))

            is_low_cpa_camp = (
                (c_conv_2d > 0 and (c_spend_2d / c_conv_2d) <= LOW_CAMPAIGN_CPA_EXEMPTION)
                or (c_conv_1d > 0 and (c_spend_1d / c_conv_1d) <= LOW_CAMPAIGN_CPA_EXEMPTION)
                or ("hanan" in camp_name.lower() and "ola" in camp_name.lower())
            )
            camp_low_cpa_map[camp_id] = is_low_cpa_camp

            if c_spend_2d > 0 or c_spend_1d > 0:
                print(
                    f"  * Campaign '{camp_name}' (ID={camp_id}, status={c2.get('primary_status')}, low_cpa_exempt={is_low_cpa_camp}) | "
                    f"Today: Spend=${c_spend_1d:.2f}, Conv={c_conv_1d} | "
                    f"2-Day: Spend=${c_spend_2d:.2f}, Conv={c_conv_2d}"
                )

            c_reasons = evaluate_campaign_rules(c_spend_1d, c_conv_1d, c_spend_2d, c_conv_2d)
            if c_reasons:
                c_hist_key = f"{aadvid}:{camp_id}"
                if c_hist_key in history["tiktok_campaigns"]:
                    print(
                        f"    [SKIP CAMPAIGN - SECOND CHANCE] Already paused once before "
                        f"({history['tiktok_campaigns'][c_hist_key].get('paused_at')}), user re-enabled it."
                    )
                    continue
                c_reason_str = " | ".join(c_reasons)
                print(f"    [PAUSING CAMPAIGN] Reason: {c_reason_str}")
                ok, resp_txt = pause_campaign(session, headers, aadvid, camp_id)
                if ok:
                    print(f"    [SUCCESS] Paused campaign {camp_name} ({camp_id})")
                    history["tiktok_campaigns"][c_hist_key] = {
                        "aadvid": aadvid,
                        "account_name": acc_name,
                        "campaign_id": camp_id,
                        "campaign_name": camp_name,
                        "reason": c_reason_str,
                        "spend_1d": c_spend_1d,
                        "conv_1d": c_conv_1d,
                        "spend_2d": c_spend_2d,
                        "conv_2d": c_conv_2d,
                        "paused_at": datetime.now(timezone.utc).isoformat(),
                    }
                    total_campaigns_paused += 1
                else:
                    print(f"    [ERROR] Failed to pause campaign {camp_id}: {resp_txt}")

        if not active_cids:
            continue

        ad_name_map = {
            str(a.get("creative_id")): f"{a.get('campaign_name', '')} / {a.get('creative_name', '')}"
            for a in active_ads
        }
        creative_to_camp_map = {
            str(a.get("creative_id")): str(a.get("campaign_id") or "")
            for a in active_ads
        }

        mats_1d = fetch_expanded_materials(session, headers, aadvid, active_cids, today_str, today_str)
        mats_2d = fetch_expanded_materials(session, headers, aadvid, active_cids, yesterday_str, today_str)

        # Index 1d stats by unique material key
        map_1d = {}
        for m in mats_1d:
            key = f"{m.get('ad_id')}:{m.get('ad_material_draft_id')}:{m.get('main_entity_id')}"
            map_1d[key] = m

        print(f"  Expanded Smart+ Videos found: {len(mats_2d)}")
        for m2 in mats_2d:
            key = f"{m2.get('ad_id')}:{m2.get('ad_material_draft_id')}:{m.get('main_entity_id')}"
            m1 = map_1d.get(key, {})

            opt_status = str(m2.get("mix_material_aco_opt_status") or m1.get("mix_material_aco_opt_status") or "0")
            prim_status = str(m2.get("material_primary_status") or m1.get("material_primary_status") or "")

            spend_1d = float(m1.get("stat_cost") or 0.0)
            cpc_1d = float(m1.get("cpc") or 0.0)
            clicks_1d = int(float(m1.get("click_cnt") or 0))
            conv_1d = int(float(m1.get("time_attr_convert_cnt") or 0))

            spend_2d = float(m2.get("stat_cost") or 0.0)
            cpc_2d = float(m2.get("cpc") or 0.0)
            clicks_2d = int(float(m2.get("click_cnt") or 0))
            conv_2d = int(float(m2.get("time_attr_convert_cnt") or 0))

            # Only log or act on videos that are enabled (opt_status == "0") and have spend > 0
            if opt_status != "0" or prim_status in ("disable", "delete"):
                continue
            if spend_2d <= 0 and spend_1d <= 0:
                continue

            vid_name = (m2.get("main_entity_name") or "Video")[:45]
            cid = str(m2.get("creative_id") or "")
            ad_id = str(m2.get("ad_id") or "")
            parent_camp_id = creative_to_camp_map.get(cid, "")
            camp_low_cpa = camp_low_cpa_map.get(parent_camp_id, False)
            draft_ids = parse_draft_ids(m2.get("ad_material_draft_id"))
            draft_str = ",".join(draft_ids)
            hist_key = f"{aadvid}:{ad_id}:{draft_str}"

            print(
                f"  - Video '{vid_name}' (draft={draft_str}, status={prim_status}, low_cpa_camp={camp_low_cpa}) | "
                f"Today: Spend=${spend_1d:.2f}, CPC=${cpc_1d:.2f}, Conv={conv_1d} | "
                f"2-Day: Spend=${spend_2d:.2f}, CPC=${cpc_2d:.2f}, Conv={conv_2d}"
            )

            reasons = evaluate_video_rules(
                spend_1d, conv_1d, cpc_1d, clicks_1d,
                spend_2d, conv_2d, cpc_2d, clicks_2d,
                camp_low_cpa=camp_low_cpa,
            )
            if reasons:
                if hist_key in history["tiktok_videos"]:
                    print(f"    [SKIP - SECOND CHANCE] Already paused once before ({history['tiktok_videos'][hist_key].get('paused_at')}), user re-enabled it.")
                    continue
                reason_str = " | ".join(reasons)
                print(f"    [PAUSING VIDEO] Reason: {reason_str}")
                ok, resp_txt = pause_smart_plus_video(session, headers, aadvid, ad_id, m2)
                if ok:
                    print(f"    [SUCCESS] Paused video {draft_str} in ad_id {ad_id}")
                    history["tiktok_videos"][hist_key] = {
                        "aadvid": aadvid,
                        "account_name": acc_name,
                        "ad_id": ad_id,
                        "creative_id": cid,
                        "campaign_ad_name": ad_name_map.get(cid, ""),
                        "video_name": m2.get("main_entity_name"),
                        "draft_ids": draft_ids,
                        "reason": reason_str,
                        "spend_1d": spend_1d,
                        "conv_1d": conv_1d,
                        "spend_2d": spend_2d,
                        "conv_2d": conv_2d,
                        "paused_at": datetime.now(timezone.utc).isoformat(),
                    }
                    total_paused += 1
                else:
                    print(f"    [ERROR] Failed to pause video {draft_str}: {resp_txt}")

    save_history(history)
    print(f"\nDone! Total TikTok Campaigns paused: {total_campaigns_paused} | Smart+ videos paused: {total_paused}")


if __name__ == "__main__":
    main()
