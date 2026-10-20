import json
import os
import re
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
# Video Level (Single AdSet / Smart+):
# 1. Video spend > $6.00 in 1 day (Today) with 0 conversions -> Pause Video
# 2. Video spend > $6.00 in 2 days (Yesterday + Today) with 0 conversions -> Pause Video
# 3. Video spend >= $1.00 and CPC > $0.35 (or 0 clicks) -> Pause Video
#    * EXCEPTION: If the video itself has conversions at a low CPA (< $10.00), do NOT pause it for CPC;
#      keep it running until its CPA reaches >= $10.00 (MAX_VIDEO_CPA), then pause it.
# Aggregated Duplicated Video Level (Across ALL Campaigns & ALL Accounts):
# 1. Duplicated video total spend >= $20.00 (1-day or 2-day) with 0 sales -> Pause video across ALL campaigns & accounts
# 2. Duplicated video total CPA >= $22.00 (1-day or 2-day, with >= 1 sale) -> Pause video across ALL campaigns & accounts
MAX_CAMPAIGN_NO_SALE_SPEND = 18.00
MAX_CAMPAIGN_CPA = 21.00
MAX_NO_SALE_SPEND = 6.00
MAX_VIDEO_CPA = 8.00
MIN_SPEND_FOR_CPC = 1.00
MAX_CPC = 0.35
MAX_DUP_VIDEO_NO_SALE_SPEND = 20.00
MAX_DUP_VIDEO_CPA = 22.00


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
        return json.loads(COOKIES_FILE.read_text(encoding="utf-8-sig"))
    raise RuntimeError("No TikTok cookies found in TIKTOK_COOKIES_JSON or tiktok_cookies.json")


def load_history():
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            data = {}
    else:
        data = {}
    data.setdefault("adsquads", {})
    data.setdefault("ads", {})
    data.setdefault("tiktok_campaigns", {})
    data.setdefault("tiktok_videos", {})
    data.setdefault("tiktok_ads", {})
    data.setdefault("tiktok_disabled_dup_videos", {})
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
            if "ads.tiktok.com" in c.get("domain", "") or not csrf:
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


GULF_KEYWORDS = (
    "oman", "qatar", "qat", "bahrain", "kuwait", "kuwiat", "kwt", "uae", "emarat",
    "عمان", "قطر", "البحرين", "بحرين", "كويت", "الكويت", "الامارات", "الإمارات",
)


def is_gulf_campaign(campaign_name: str) -> bool:
    cname = (campaign_name or "").lower()
    return any(k in cname for k in GULF_KEYWORDS)


def is_cross_monitored_product_campaign(campaign_name: str, video_name: str = "") -> bool:
    combined = f"{campaign_name} {video_name}".lower()
    return any(k in combined for k in ("مايباخ", "maybach", "time x", "timex", "تايم إكس", "دفاتر"))


def fetch_campaigns(session, headers, aadvid, st_date, et_date):
    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
    all_rows = []
    for page in range(1, 6):
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
                "page": page,
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
        try:
            r = session.post(
                f"https://ads.tiktok.com/api/v4/i18n/statistics/op/campaign/list/?aadvid={aadvid}&req_src=bidding",
                headers=h,
                json=body,
                timeout=20,
            ).json()
            rows = (r.get("data", {}) if isinstance(r, dict) else {}).get("table", []) or []
            all_rows.extend(rows)
            if len(rows) < 100:
                break
        except Exception as e:
            print(f"    [WARN] fetch_campaigns page {page} error: {e}")
            break
    return all_rows


def pause_campaign(session, headers, aadvid, campaign_id, operation="disable"):
    h = dict(headers)
    h["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
    payload = {
        "campaign_list": json.dumps([str(campaign_id)]),
        "operation": operation,
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
    all_rows = []
    for page in range(1, 6):
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
                "page": page,
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
        try:
            r = session.post(
                f"https://ads.tiktok.com/api/v4/i18n/statistics/op/ad/list/?aadvid={aadvid}&req_src=bidding",
                headers=h,
                json=body,
                timeout=20,
            ).json()
            rows = (r.get("data", {}) if isinstance(r, dict) else {}).get("table", []) or []
            all_rows.extend(rows)
            if len(rows) < 100:
                break
        except Exception as e:
            print(f"    [WARN] fetch_ads page {page} error: {e}")
            break
    return all_rows


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


def pause_smart_plus_video(session, headers, aadvid, ad_id, row, operation="disable"):
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
        "operation": operation,
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


def evaluate_video_rules(
    spend_1d, conv_1d, cpc_1d, clicks_1d,
    spend_2d, conv_2d, cpc_2d, clicks_2d,
    spend_3d=0.0, conv_3d=0, cpc_3d=0.0, clicks_3d=0,
    spend_7d=0.0, conv_7d=0,
):
    reasons = []
    has_profitable_3d = conv_3d > 0 and (spend_3d / conv_3d) <= MAX_VIDEO_CPA
    has_profitable_2d = conv_2d > 0 and (spend_2d / conv_2d) <= MAX_VIDEO_CPA
    has_profitable_1d = conv_1d > 0 and (spend_1d / conv_1d) <= MAX_VIDEO_CPA
    has_profitable_sales = has_profitable_3d or has_profitable_2d or has_profitable_1d

    # Rule 1: Spend > $6 with 0 conversions (1D, 2D, 3D, or 7D accumulated across days) unless 3D CPA is profitable <= $8.00
    if spend_7d > MAX_NO_SALE_SPEND and conv_7d == 0:
        reasons.append(f"7-Day Accumulated Spend ${spend_7d:.2f} > ${MAX_NO_SALE_SPEND:.2f} with 0 sales")
    elif spend_3d > MAX_NO_SALE_SPEND and conv_3d == 0:
        reasons.append(f"3-Day Spend ${spend_3d:.2f} > ${MAX_NO_SALE_SPEND:.2f} with 0 sales")
    elif spend_2d > MAX_NO_SALE_SPEND and conv_2d == 0 and not has_profitable_3d:
        reasons.append(f"2-Day Spend ${spend_2d:.2f} > ${MAX_NO_SALE_SPEND:.2f} with 0 sales")
    elif spend_1d > MAX_NO_SALE_SPEND and conv_1d == 0 and not (has_profitable_2d or has_profitable_3d):
        reasons.append(f"Today Spend ${spend_1d:.2f} > ${MAX_NO_SALE_SPEND:.2f} with 0 sales")

    # Rule 2: Video CPA > $8.00 over 3 days / 7 days (or 2D/1D if 3D is not profitable)
    if conv_3d > 0 and (spend_3d / conv_3d) > MAX_VIDEO_CPA:
        reasons.append(f"3-Day Video CPA ${(spend_3d / conv_3d):.2f} > ${MAX_VIDEO_CPA:.2f} (Spend=${spend_3d:.2f}, Sales={conv_3d})")
    elif conv_7d > 0 and (spend_7d / conv_7d) > MAX_VIDEO_CPA and not has_profitable_3d:
        reasons.append(f"7-Day Video CPA ${(spend_7d / conv_7d):.2f} > ${MAX_VIDEO_CPA:.2f} (Spend=${spend_7d:.2f}, Sales={conv_7d})")
    elif conv_2d > 0 and (spend_2d / conv_2d) > MAX_VIDEO_CPA and not has_profitable_3d:
        reasons.append(f"2-Day Video CPA ${(spend_2d / conv_2d):.2f} > ${MAX_VIDEO_CPA:.2f} (Spend=${spend_2d:.2f}, Sales={conv_2d})")
    elif conv_1d > 0 and (spend_1d / conv_1d) > MAX_VIDEO_CPA and not (has_profitable_2d or has_profitable_3d):
        reasons.append(f"Today Video CPA ${(spend_1d / conv_1d):.2f} > ${MAX_VIDEO_CPA:.2f} (Spend=${spend_1d:.2f}, Sales={conv_1d})")

    # Rule 3: CPC Cut (EXEMPT ONLY if this specific video has sales with CPA <= $8.00)
    if not has_profitable_sales:
        if spend_1d >= MIN_SPEND_FOR_CPC and (cpc_1d > MAX_CPC or clicks_1d == 0):
            reasons.append(f"Today Spend ${spend_1d:.2f} >= ${MIN_SPEND_FOR_CPC:.2f} & CPC ${cpc_1d:.2f} > ${MAX_CPC:.2f}")
        elif spend_2d >= MIN_SPEND_FOR_CPC and (cpc_2d > MAX_CPC or clicks_2d == 0):
            reasons.append(f"2-Day Spend ${spend_2d:.2f} >= ${MIN_SPEND_FOR_CPC:.2f} & CPC ${cpc_2d:.2f} > ${MAX_CPC:.2f}")
        elif spend_3d >= MIN_SPEND_FOR_CPC and (cpc_3d > MAX_CPC or clicks_3d == 0):
            reasons.append(f"3-Day Spend ${spend_3d:.2f} >= ${MIN_SPEND_FOR_CPC:.2f} & CPC ${cpc_3d:.2f} > ${MAX_CPC:.2f}")
    return reasons


def canonical_video_name(raw_name: str, fallback_id: str = "") -> str:
    name = (raw_name or "").strip()
    if not name:
        return f"id:{fallback_id}"
    # Strip extension (.mp4, .mov, etc.) and optional trailing timestamp
    stem = re.sub(r"\.(mp4|mov|m4v|avi|webm)(_\d+)?$", "", name, flags=re.IGNORECASE)
    # Strip TikTok auto-appended 8-char random hash suffix (e.g. _17CKxSRy, _BNo9p4lP)
    stem = re.sub(r"_[A-Za-z0-9]{8}$", "", stem)
    # Strip leading "Copy of "
    stem = re.sub(r"^copy\s+of\s+", "", stem, flags=re.IGNORECASE)
    return stem.strip().lower()


def evaluate_dup_video_rules(
    spend_1d: float, conv_1d: int,
    spend_2d: float, conv_2d: int,
    spend_3d: float = 0.0, conv_3d: int = 0,
    spend_7d: float = 0.0, conv_7d: int = 0,
) -> list[str]:
    reasons = []
    # Rule 1: Aggregated Today Spend >= $20.00 across all copies with 0 conversions
    if spend_1d >= MAX_DUP_VIDEO_NO_SALE_SPEND and conv_1d == 0:
        reasons.append(
            f"[Global Dup Video Stop-Loss 1D] Total Today Spend ${spend_1d:.2f} >= ${MAX_DUP_VIDEO_NO_SALE_SPEND:.2f} with 0 sales"
        )
    # Rule 2: Aggregated 2-Day Spend >= $20.00 across all copies with 0 conversions
    if spend_2d >= MAX_DUP_VIDEO_NO_SALE_SPEND and conv_2d == 0:
        reasons.append(
            f"[Global Dup Video Stop-Loss 2D] Total 2-Day Spend ${spend_2d:.2f} >= ${MAX_DUP_VIDEO_NO_SALE_SPEND:.2f} with 0 sales"
        )
    # Rule 2B: Aggregated 3-Day or 7-Day Spend >= $12.00 across all copies with 0 conversions
    if spend_3d >= 12.0 and conv_3d == 0:
        reasons.append(
            f"[Global Dup Video Stop-Loss 3D] Total 3-Day Spend ${spend_3d:.2f} >= $12.00 with 0 sales"
        )
    elif spend_7d >= 12.0 and conv_7d == 0:
        reasons.append(
            f"[Global Dup Video Stop-Loss 7D] Total 7-Day Spend ${spend_7d:.2f} >= $12.00 with 0 sales"
        )
    # Rule 3: Aggregated Today CPA >= $22.00 across all copies (with >= 1 conversion)
    if conv_1d > 0 and (spend_1d / conv_1d) >= MAX_DUP_VIDEO_CPA:
        reasons.append(
            f"[Global Dup Video High CPA 1D] Total Today CPA ${(spend_1d / conv_1d):.2f} >= ${MAX_DUP_VIDEO_CPA:.2f} "
            f"(Total Spend=${spend_1d:.2f}, Total Sales={conv_1d})"
        )
    # Rule 4: Aggregated 2-Day / 3-Day / 7-Day CPA >= $18.00-$22.00 across all copies
    if conv_2d > 0 and (spend_2d / conv_2d) >= MAX_DUP_VIDEO_CPA:
        reasons.append(
            f"[Global Dup Video High CPA 2D] Total 2-Day CPA ${(spend_2d / conv_2d):.2f} >= ${MAX_DUP_VIDEO_CPA:.2f} "
            f"(Total Spend=${spend_2d:.2f}, Total Sales={conv_2d})"
        )
    elif conv_3d > 0 and (spend_3d / conv_3d) >= 18.0:
        reasons.append(
            f"[Global Dup Video High CPA 3D] Total 3-Day CPA ${(spend_3d / conv_3d):.2f} >= $18.00 "
            f"(Total Spend=${spend_3d:.2f}, Total Sales={conv_3d})"
        )
    elif conv_7d > 0 and (spend_7d / conv_7d) >= 18.0 and not (conv_3d > 0 and (spend_3d / conv_3d) <= MAX_VIDEO_CPA):
        reasons.append(
            f"[Global Dup Video High CPA 7D] Total 7-Day CPA ${(spend_7d / conv_7d):.2f} >= $18.00 "
            f"(Total Spend=${spend_7d:.2f}, Total Sales={conv_7d})"
        )
    return reasons


def main():
    cookies_raw = load_cookies()
    history = load_history()
    no_dup_camps = history.setdefault("no_duplicate_campaigns", {})
    session, headers = build_session(cookies_raw)

    total_campaigns_paused = 0
    total_paused = 0
    total_dup_videos_paused = 0
    total_revived = 0

    # Global tracker for duplicated videos across ALL campaigns & ALL 6 accounts
    global_dup_videos = {}

    for acc_name, aadvid, utc_offset in ACCOUNTS:
        now_tz = datetime.now(timezone(timedelta(hours=utc_offset)))
        today_str = now_tz.strftime("%Y-%m-%d")
        yesterday_str = (now_tz - timedelta(days=1)).strftime("%Y-%m-%d")
        two_days_ago_str = (now_tz - timedelta(days=2)).strftime("%Y-%m-%d")
        six_days_ago_str = (now_tz - timedelta(days=6)).strftime("%Y-%m-%d")

        # 1. Campaign-Level 3-Day Check: NEVER pause the whole campaign, but block duplication if 3D CPA > $8.00
        camps_1d = fetch_campaigns(session, headers, aadvid, today_str, today_str)
        camps_2d = fetch_campaigns(session, headers, aadvid, yesterday_str, today_str)
        camps_3d = fetch_campaigns(session, headers, aadvid, two_days_ago_str, today_str)
        camp_1d_map = {str(c.get("campaign_id")): c for c in camps_1d if c.get("campaign_id")}
        camp_2d_map = {str(c.get("campaign_id")): c for c in camps_2d if c.get("campaign_id")}
        camp_3d_map = {str(c.get("campaign_id")): c for c in camps_3d if c.get("campaign_id")}

        active_camps = [
            c for c in camps_3d
            if c.get("primary_status") not in ("disable", "delete")
            and str(c.get("opt_status")) == "0"
        ]

        ads_3d = fetch_ads(session, headers, aadvid, two_days_ago_str, today_str)
        ads_7d = fetch_ads(session, headers, aadvid, six_days_ago_str, today_str)
        ad_7d_map = {str(a.get("creative_id")): a for a in ads_7d if a.get("creative_id")}
        active_ads = [
            a for a in ads_3d
            if a.get("primary_status") not in ("disable", "delete")
            and str(a.get("opt_status")) == "0"
        ]
        active_cids = [str(a.get("creative_id")) for a in active_ads if a.get("creative_id")]
        active_cid_set = set(active_cids)

        relevant_ads = [
            a for a in ads_3d
            if a.get("creative_id") and (
                float((a.get("row_data") or {}).get("stat_cost") or 0) > 0
                or float(((ad_7d_map.get(str(a.get("creative_id"))) or {}).get("row_data") or {}).get("stat_cost") or 0) > 0
                or str(a.get("creative_id")) in active_cid_set
            )
        ]
        relevant_cids = [str(a.get("creative_id")) for a in relevant_ads if a.get("creative_id")]

        print(
            f"\n=== [{acc_name}] ({aadvid}) | 3-Day Window: {two_days_ago_str} -> {today_str} | "
            f"Active Campaigns: {len(active_camps)} | Active AdSets: {len(active_cids)} ==="
        )

        now_sa = datetime.now(timezone(timedelta(hours=3)))
        is_before_6pm_sa = (6 <= now_sa.hour < 17)
        tt_scaled_today = history.setdefault("proactive_scaled_tiktok", {})

        for c3 in active_camps:
            camp_id = str(c3.get("campaign_id") or "")
            camp_name = c3.get("campaign_name") or "Campaign"
            c1 = camp_1d_map.get(camp_id, {})
            c2 = camp_2d_map.get(camp_id, {})
            rd1 = c1.get("row_data") or {}
            rd2 = c2.get("row_data") or {}
            rd3 = c3.get("row_data") or {}

            c_spend_1d = float(rd1.get("stat_cost") or 0.0)
            c_conv_1d = int(float(rd1.get("time_attr_convert_cnt") or 0))
            c_spend_2d = float(rd2.get("stat_cost") or 0.0)
            c_conv_2d = int(float(rd2.get("time_attr_convert_cnt") or 0))
            c_spend_3d = float(rd3.get("stat_cost") or 0.0)
            c_conv_3d = int(float(rd3.get("time_attr_convert_cnt") or 0))
            c_cpa_1d = round(c_spend_1d / c_conv_1d, 2) if c_conv_1d > 0 else None
            c_cpa_3d = round(c_spend_3d / c_conv_3d, 2) if c_conv_3d > 0 else None

            if c_spend_3d > 0 or c_spend_1d > 0:
                print(
                    f"  * Campaign '{camp_name}' (ID={camp_id}, status=ACTIVE) | "
                    f"Today: ${c_spend_1d:.2f} ({c_conv_1d} sales) | "
                    f"3-Day: ${c_spend_3d:.2f} ({c_conv_3d} sales, CPA=${c_cpa_3d})"
                )

            # Never pause the whole campaign: block duplication if 3-Day spend >= $10 w/ 0 sales or 3-Day CPA > $8.00
            if (c_spend_3d >= 10.0 and c_conv_3d == 0) or (c_conv_3d > 0 and c_cpa_3d is not None and c_cpa_3d > 8.0):
                no_dup_camps[camp_id] = {
                    "name": camp_name,
                    "aadvid": aadvid,
                    "spend_3d": c_spend_3d,
                    "purchases_3d": c_conv_3d,
                    "cpa_3d": c_cpa_3d,
                    "reason": "3-Day CPA > $8.00 — Duplication Blocked (AdSet/Material pause only)",
                }
                print(f"    🚫 [NO-DUPLICATE LOCK] Campaign '{camp_name}' 3-Day CPA=${c_cpa_3d} (> $8.00) — Campaign kept ACTIVE, duplication blocked, pausing high-cost items inside it.")
            elif camp_id in no_dup_camps and c_conv_3d > 0 and c_cpa_3d is not None and c_cpa_3d <= 8.0:
                del no_dup_camps[camp_id]

            # Controlled Auto-Scale (+20% budget before 5:00 PM SA time ONLY) for strong winning campaigns (>=3 sales today at CPA <= $6.50 and 3D CPA <= $7.50)
            scale_key = f"{now_sa.strftime('%Y-%m-%d')}:{aadvid}:{camp_id}"
            if (
                is_before_6pm_sa
                and camp_id not in no_dup_camps
                and c_conv_1d >= 3
                and c_cpa_1d is not None
                and c_cpa_1d <= 6.50
                and (c_cpa_3d is None or c_cpa_3d <= 7.50)
                and scale_key not in tt_scaled_today
            ):
                try:
                    cur_bdg = float(str(rd1.get("budget") or rd3.get("budget") or 50.0).replace(",", "").replace("$", ""))
                except Exception:
                    cur_bdg = 50.0
                if 20.0 <= cur_bdg < 150.0:
                    new_bdg = min(180, int(round(cur_bdg * 1.20)))
                    try:
                        h_b = dict(headers)
                        h_b["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
                        rb_resp = session.post(
                            f"https://ads.tiktok.com/api/v2/i18n/overture/campaign/update_budget/?aadvid={aadvid}&req_src=bidding",
                            headers=h_b,
                            json={"campaign_id": camp_id, "budget": new_bdg, "budget_mode": 1, "ad_channel": 1},
                            timeout=15,
                        )
                        if rb_resp.text.strip() and rb_resp.json().get("code") == 0:
                            tt_scaled_today[scale_key] = {
                                "name": camp_name,
                                "old_budget": cur_bdg,
                                "new_budget": new_bdg,
                                "conv_today": c_conv_1d,
                                "cpa_today": c_cpa_1d,
                                "scaled_at": now_sa.isoformat(),
                            }
                            print(
                                f"    🚀 [CONTROLLED AUTO-SCALE +20%] Campaign '{camp_name}' ({c_conv_1d} sales @ CPA=${c_cpa_1d}) -> Budget: ${cur_bdg:.0f} -> ${new_bdg}"
                            )
                    except Exception:
                        pass

        if not relevant_cids:
            continue

        ad_name_map = {
            str(a.get("creative_id")): f"{a.get('campaign_name', '')} / {a.get('creative_name', '')}"
            for a in relevant_ads
        }
        ad_camp_map = {
            str(a.get("creative_id")): str(a.get("campaign_name") or "")
            for a in relevant_ads
        }

        mats_1d = fetch_expanded_materials(session, headers, aadvid, relevant_cids, today_str, today_str)
        mats_2d = fetch_expanded_materials(session, headers, aadvid, relevant_cids, yesterday_str, today_str)
        mats_3d = fetch_expanded_materials(session, headers, aadvid, relevant_cids, two_days_ago_str, today_str)
        mats_7d = fetch_expanded_materials(session, headers, aadvid, relevant_cids, six_days_ago_str, today_str)

        map_1d = {}
        for m in mats_1d:
            key = f"{m.get('ad_id')}:{m.get('ad_material_draft_id')}:{m.get('main_entity_id')}"
            map_1d[key] = m
        map_2d = {}
        for m in mats_2d:
            key = f"{m.get('ad_id')}:{m.get('ad_material_draft_id')}:{m.get('main_entity_id')}"
            map_2d[key] = m
        map_3d = {}
        for m in mats_3d:
            key = f"{m.get('ad_id')}:{m.get('ad_material_draft_id')}:{m.get('main_entity_id')}"
            map_3d[key] = m
        map_7d = {}
        for m in mats_7d:
            key = f"{m.get('ad_id')}:{m.get('ad_material_draft_id')}:{m.get('main_entity_id')}"
            map_7d[key] = m

        # Register ALL video materials (7D + 3D + 2D + 1D) into global_dup_videos for cross-campaign & cross-account aggregation
        all_mat_keys = set(map_7d.keys()) | set(map_3d.keys()) | set(map_2d.keys()) | set(map_1d.keys())
        for k_all in all_mat_keys:
            m7_all = map_7d.get(k_all, {})
            m3_all = map_3d.get(k_all, {})
            m2_all = map_2d.get(k_all, {})
            m1_all = map_1d.get(k_all, {})
            base_m = m3_all or m7_all or m2_all or m1_all
            raw_vname = base_m.get("main_entity_name") or ""
            v_key = canonical_video_name(raw_vname, str(base_m.get("main_entity_id") or k_all))

            sp1_v = float(m1_all.get("stat_cost") or 0.0)
            cv1_v = int(float(m1_all.get("time_attr_convert_cnt") or 0))
            sp2_v = float(m2_all.get("stat_cost") or 0.0)
            cv2_v = int(float(m2_all.get("time_attr_convert_cnt") or 0))
            sp3_v = float(m3_all.get("stat_cost") or 0.0)
            cv3_v = int(float(m3_all.get("time_attr_convert_cnt") or 0))
            sp7_v = float(m7_all.get("stat_cost") or sp3_v)
            cv7_v = int(float(m7_all.get("time_attr_convert_cnt") or cv3_v))

            opt_v = str(base_m.get("mix_material_aco_opt_status") or "0")
            prim_v = str(base_m.get("material_primary_status") or "")
            cid_v = str(base_m.get("creative_id") or "")
            aid_v = str(base_m.get("ad_id") or "")
            camp_id_v = str(base_m.get("campaign_id") or "")
            draft_ids_v = parse_draft_ids(base_m.get("ad_material_draft_id"))
            draft_str_v = ",".join(draft_ids_v)
            hist_key_v = f"{aadvid}:{aid_v}:{draft_str_v}"

            is_inst_active = (
                cid_v in active_cid_set
                and opt_v == "0"
                and prim_v not in ("disable", "delete")
            )

            grp = global_dup_videos.setdefault(v_key, {
                "v_key": v_key,
                "sample_name": raw_vname,
                "ad_ids": set(),
                "campaign_ids": set(),
                "accounts": set(),
                "spend_1d": 0.0,
                "conv_1d": 0,
                "spend_2d": 0.0,
                "conv_2d": 0,
                "spend_3d": 0.0,
                "conv_3d": 0,
                "spend_7d": 0.0,
                "conv_7d": 0,
                "instances": [],
            })
            grp["ad_ids"].add(f"{aadvid}:{aid_v}")
            grp["campaign_ids"].add(f"{aadvid}:{camp_id_v}")
            grp["accounts"].add(acc_name)
            grp["spend_1d"] = round(grp["spend_1d"] + sp1_v, 2)
            grp["conv_1d"] += cv1_v
            grp["spend_2d"] = round(grp["spend_2d"] + sp2_v, 2)
            grp["conv_2d"] += cv2_v
            grp["spend_3d"] = round(grp["spend_3d"] + sp3_v, 2)
            grp["conv_3d"] += cv3_v
            grp["spend_7d"] = round(grp["spend_7d"] + sp7_v, 2)
            grp["conv_7d"] += cv7_v
            grp["instances"].append({
                "acc_name": acc_name,
                "aadvid": aadvid,
                "campaign_id": camp_id_v,
                "campaign_name": ad_camp_map.get(cid_v, ""),
                "ad_id": aid_v,
                "creative_id": cid_v,
                "campaign_ad_name": ad_name_map.get(cid_v, ""),
                "raw_name": raw_vname,
                "draft_ids": draft_ids_v,
                "draft_str": draft_str_v,
                "hist_key": hist_key_v,
                "spend_1d": sp1_v,
                "conv_1d": cv1_v,
                "spend_2d": sp2_v,
                "conv_2d": cv2_v,
                "spend_3d": sp3_v,
                "conv_3d": cv3_v,
                "spend_7d": sp7_v,
                "conv_7d": cv7_v,
                "is_active": is_inst_active,
                "row": base_m,
            })

        if not active_cids:
            continue

        active_mats_3d = [m for m in mats_3d if str(m.get("creative_id") or "") in active_cid_set]
        print(f"  Expanded Smart+ Videos found (3D): {len(active_mats_3d)}")
        # Count active videos per ad_id to prevent starving a Smart+ campaign to 0 active videos
        active_videos_per_ad = {}
        paused_candidates_per_ad = {}
        for m_item in active_mats_3d:
            k_item = f"{m_item.get('ad_id')}:{m_item.get('ad_material_draft_id')}:{m_item.get('main_entity_id')}"
            m1_item = map_1d.get(k_item, {})
            m7_item = map_7d.get(k_item, {})
            opt_st = str(m_item.get("mix_material_aco_opt_status") or m1_item.get("mix_material_aco_opt_status") or "0")
            prim_st = str(m_item.get("material_primary_status") or m1_item.get("material_primary_status") or "")
            aid_str = str(m_item.get("ad_id") or "")
            if not aid_str:
                continue
            v_key_item = canonical_video_name(m_item.get("main_entity_name") or "", str(m_item.get("main_entity_id") or ""))
            if opt_st == "0" and prim_st not in ("disable", "delete"):
                active_videos_per_ad[aid_str] = active_videos_per_ad.get(aid_str, 0) + 1
            else:
                # Never consider a globally disabled duplicated video for starvation recovery
                if v_key_item in history.get("tiktok_disabled_dup_videos", {}):
                    continue
                sp3_i = float(m_item.get("stat_cost") or 0.0)
                sp7_i = float(m7_item.get("stat_cost") or sp3_i)
                cpc3_i = float(m_item.get("cpc") or 0.0)
                if sp3_i < MAX_NO_SALE_SPEND and sp7_i < MAX_NO_SALE_SPEND:
                    paused_candidates_per_ad.setdefault(aid_str, []).append((cpc3_i if cpc3_i > 0 else 9.9, sp3_i, m_item))

        # Smart+ Starvation Auto-Recovery: if an active ad_id has 0 active videos left, re-enable its lowest-CPC video (skip Maybach & TIME X campaigns which are managed by cross_product_monitor.py)
        for a_obj in active_ads:
            if is_cross_monitored_product_campaign(str(a_obj.get("campaign_name") or "")):
                continue
            aid_chk = str(a_obj.get("ad_id") or "")
            if aid_chk and active_videos_per_ad.get(aid_chk, 0) == 0 and paused_candidates_per_ad.get(aid_chk):
                paused_candidates_per_ad[aid_chk].sort(key=lambda x: (x[0], x[1]))
                _, _, best_m = paused_candidates_per_ad[aid_chk][0]
                ok_starve, _ = pause_smart_plus_video(session, headers, aadvid, aid_chk, best_m, operation="enable")
                if ok_starve:
                    active_videos_per_ad[aid_chk] = 1
                    total_revived += 1
                    print(
                        f"    ⚡♻️ [SMART+ STARVATION RECOVERY] Re-enabled video '{best_m.get('main_entity_name')}' "
                        f"in '{a_obj.get('campaign_name')}' because 0 active videos remained!"
                    )

        for m3 in active_mats_3d:
            key = f"{m3.get('ad_id')}:{m3.get('ad_material_draft_id')}:{m3.get('main_entity_id')}"
            m1 = map_1d.get(key, {})
            m2 = map_2d.get(key, m3)
            m7 = map_7d.get(key, m3)

            opt_status = str(m3.get("mix_material_aco_opt_status") or m2.get("mix_material_aco_opt_status") or m1.get("mix_material_aco_opt_status") or "0")
            prim_status = str(m3.get("material_primary_status") or m2.get("material_primary_status") or m1.get("material_primary_status") or "")

            spend_1d = float(m1.get("stat_cost") or 0.0)
            cpc_1d = float(m1.get("cpc") or 0.0)
            clicks_1d = int(float(m1.get("click_cnt") or 0))
            conv_1d = int(float(m1.get("time_attr_convert_cnt") or 0))

            spend_2d = float(m2.get("stat_cost") or 0.0)
            cpc_2d = float(m2.get("cpc") or 0.0)
            clicks_2d = int(float(m2.get("click_cnt") or 0))
            conv_2d = int(float(m2.get("time_attr_convert_cnt") or 0))

            spend_3d = float(m3.get("stat_cost") or 0.0)
            cpc_3d = float(m3.get("cpc") or 0.0)
            clicks_3d = int(float(m3.get("click_cnt") or 0))
            conv_3d = int(float(m3.get("time_attr_convert_cnt") or 0))

            spend_7d = float(m7.get("stat_cost") or spend_3d)
            conv_7d = int(float(m7.get("time_attr_convert_cnt") or conv_3d))

            vid_name = (m3.get("main_entity_name") or "Video")[:45]
            v_key_cur = canonical_video_name(m3.get("main_entity_name") or "", str(m3.get("main_entity_id") or ""))
            cid = str(m3.get("creative_id") or "")
            ad_id = str(m3.get("ad_id") or "")
            if is_cross_monitored_product_campaign(ad_camp_map.get(cid, ""), m3.get("main_entity_name") or ""):
                # Maybach & TIME X campaigns are monitored with exact combined rules in cross_product_monitor.py
                continue
            draft_ids = parse_draft_ids(m3.get("ad_material_draft_id"))
            draft_str = ",".join(draft_ids)
            hist_key = f"{aadvid}:{ad_id}:{draft_str}"

            # Delayed-Attribution Auto-Revival for Paused Smart+ Videos:
            if hist_key in history["tiktok_videos"] and opt_status != "0":
                if v_key_cur in history.get("tiktok_disabled_dup_videos", {}):
                    continue
                if (conv_1d > 0 or conv_2d > 0 or conv_3d > 0) and not evaluate_video_rules(
                    spend_1d, conv_1d, cpc_1d, clicks_1d,
                    spend_2d, conv_2d, cpc_2d, clicks_2d,
                    spend_3d, conv_3d, cpc_3d, clicks_3d,
                    spend_7d, conv_7d,
                ):
                    ok_rev, _ = pause_smart_plus_video(session, headers, aadvid, ad_id, m3, operation="enable")
                    if ok_rev:
                        print(
                            f"    ♻️🔥 [REVIVED VIDEO - DELAYED SALE] Video '{vid_name}' (draft={draft_str}) "
                            f"| 3-Day Spend=${spend_3d:.2f}, Sales={conv_3d} — re-enabled automatically!"
                        )
                        del history["tiktok_videos"][hist_key]
                        total_revived += 1
                continue

            if opt_status != "0" or prim_status in ("disable", "delete"):
                continue
            if spend_7d <= 0 and spend_3d <= 0 and spend_2d <= 0 and spend_1d <= 0:
                continue

            print(
                f"  - Video '{vid_name}' (draft={draft_str}, status={prim_status}) | "
                f"Today: ${spend_1d:.2f} (Conv={conv_1d}) | "
                f"3D: ${spend_3d:.2f} (Conv={conv_3d}) | 7D: ${spend_7d:.2f} (Conv={conv_7d})"
            )

            reasons = evaluate_video_rules(
                spend_1d, conv_1d, cpc_1d, clicks_1d,
                spend_2d, conv_2d, cpc_2d, clicks_2d,
                spend_3d, conv_3d, cpc_3d, clicks_3d,
                spend_7d, conv_7d,
            )
            if v_key_cur in history.get("tiktok_disabled_dup_videos", {}):
                reasons.append(f"Blacklisted Multi-Day Non-Converting Video ('{v_key_cur}')")

            if reasons:
                if hist_key in history["tiktok_videos"] and v_key_cur not in history.get("tiktok_disabled_dup_videos", {}):
                    print(f"    [SKIP - SECOND CHANCE] Already paused once before ({history['tiktok_videos'][hist_key].get('paused_at')}), user re-enabled it.")
                    continue
                # Protect the last 2 remaining active videos in a Smart+ campaign ONLY from early CPC cut (< $5.50 spend), NEVER from >$8 CPA or >$6 0-sale stop-loss!
                if active_videos_per_ad.get(ad_id, 1) <= 2 and spend_3d < 5.50 and spend_7d < 5.50 and conv_3d == 0:
                    print(f"    🛡️ [SMART+ ANTI-STARVATION PROTECTION] Keeping active video '{vid_name}' in ad_id {ad_id} ({active_videos_per_ad.get(ad_id, 1)} active vids left, Spend=${spend_3d:.2f} < $5.50) so Smart+ campaign does not stall!")
                    continue
                reason_str = " | ".join(reasons)
                print(f"    [PAUSING VIDEO] Reason: {reason_str}")
                ok, resp_txt = pause_smart_plus_video(session, headers, aadvid, ad_id, m3)
                if ok:
                    active_videos_per_ad[ad_id] = max(0, active_videos_per_ad.get(ad_id, 1) - 1)
                    for inst_g in global_dup_videos.get(v_key_cur, {}).get("instances", []):
                        if inst_g["hist_key"] == hist_key:
                            inst_g["is_active"] = False
                    print(f"    [SUCCESS] Paused video {draft_str} in ad_id {ad_id}")
                    history["tiktok_videos"][hist_key] = {
                        "aadvid": aadvid,
                        "account_name": acc_name,
                        "ad_id": ad_id,
                        "creative_id": cid,
                        "campaign_ad_name": ad_name_map.get(cid, ""),
                        "video_name": m3.get("main_entity_name"),
                        "draft_ids": draft_ids,
                        "reason": reason_str,
                        "spend_1d": spend_1d,
                        "conv_1d": conv_1d,
                        "spend_2d": spend_2d,
                        "conv_2d": conv_2d,
                        "spend_3d": spend_3d,
                        "conv_3d": conv_3d,
                        "spend_7d": spend_7d,
                        "conv_7d": conv_7d,
                        "paused_at": datetime.now(timezone.utc).isoformat(),
                    }
                    # Permanently blacklist any video that accumulated >$6 over 3D/7D with 0 sales so it is never re-enabled or duplicated
                    if (spend_3d > MAX_NO_SALE_SPEND and conv_3d == 0) or (spend_7d > MAX_NO_SALE_SPEND and conv_7d == 0):
                        history["tiktok_disabled_dup_videos"][v_key_cur] = {
                            "v_key": v_key_cur,
                            "sample_name": m3.get("main_entity_name"),
                            "accounts": [acc_name],
                            "spend_3d": spend_3d,
                            "conv_3d": conv_3d,
                            "spend_7d": spend_7d,
                            "conv_7d": conv_7d,
                            "reason": reason_str,
                            "disabled_at": datetime.now(timezone.utc).isoformat(),
                        }
                    total_paused += 1
                else:
                    print(f"    [ERROR] Failed to pause video {draft_str}: {resp_txt}")

    # =========================================================================
    # PHASE 2: Cross-Campaign & Cross-Account Duplicated Video Aggregation Rule
    # - Applies to any duplicated video (present in >= 2 AdSets across campaigns/accounts):
    #   1) Total Spend >= $20.00 (1D/2D) or >= $12.00 (3D/7D) with 0 sales -> Pause ALL active copies permanently
    #   2) Total Sales >= 1 and Total CPA >= $22.00 (1D/2D) or >= $18.00 (3D/7D) -> Pause ALL active copies permanently
    # =========================================================================
    dup_candidates = [
        g for g in global_dup_videos.values()
        if (len(g["ad_ids"]) >= 2 and (g["spend_7d"] > 0 or g["spend_3d"] > 0 or g["spend_2d"] > 0 or g["spend_1d"] > 0))
        or g["v_key"] in history.get("tiktok_disabled_dup_videos", {})
    ]
    print(f"\n🧮 [Phase 2] Checking {len(dup_candidates)} Duplicated Videos across ALL Campaigns & Accounts (1D/2D/3D/7D Accumulated Spend)...")

    for grp in sorted(dup_candidates, key=lambda x: x["spend_7d"], reverse=True):
        v_key = grp["v_key"]
        tot_sp1 = grp["spend_1d"]
        tot_cv1 = grp["conv_1d"]
        tot_sp2 = grp["spend_2d"]
        tot_cv2 = grp["conv_2d"]
        tot_sp3 = grp["spend_3d"]
        tot_cv3 = grp["conv_3d"]
        tot_sp7 = grp["spend_7d"]
        tot_cv7 = grp["conv_7d"]

        dup_reasons = evaluate_dup_video_rules(tot_sp1, tot_cv1, tot_sp2, tot_cv2, tot_sp3, tot_cv3, tot_sp7, tot_cv7)

        # Permanent Lock: Once a duplicated video is in tiktok_disabled_dup_videos, NEVER revive it and immediately shut down any active copy!
        if v_key in history.get("tiktok_disabled_dup_videos", {}) and not dup_reasons:
            dup_reasons = [
                f"[Permanent Global Dup Video Lock] Video '{v_key}' was permanently disabled across all AdSets and is NEVER allowed to run again"
            ]

        if not dup_reasons:
            continue

        active_insts = [
            inst for inst in grp["instances"]
            if inst["is_active"]
            and not (inst["conv_3d"] > 0 and (inst["spend_3d"] / inst["conv_3d"]) <= MAX_VIDEO_CPA)
            and not (inst["conv_7d"] >= 3 and (inst["spend_7d"] / inst["conv_7d"]) <= MAX_VIDEO_CPA)
        ]
        if not active_insts:
            continue

        dup_reason_str = " | ".join(dup_reasons)
        print(
            f"  🛑🗑️ [GLOBAL DUPLICATED VIDEO KILL] Video '{v_key}' (Copies={len(grp['ad_ids'])} AdSets across {sorted(grp['accounts'])}) "
            f"| Active copies to shut down: {len(active_insts)} | {dup_reason_str}"
        )

        closed_copies = 0
        for inst in active_insts:
            h_key = inst["hist_key"]
            ok_kill, resp_kill = pause_smart_plus_video(
                session, headers, inst["aadvid"], inst["ad_id"], inst["row"], operation="disable"
            )
            if ok_kill:
                closed_copies += 1
                total_paused += 1
                total_dup_videos_paused += 1
                inst["is_active"] = False
                history["tiktok_videos"][h_key] = {
                    "aadvid": inst["aadvid"],
                    "account_name": inst["acc_name"],
                    "ad_id": inst["ad_id"],
                    "creative_id": inst["creative_id"],
                    "campaign_ad_name": inst["campaign_ad_name"],
                    "video_name": inst["raw_name"],
                    "draft_ids": inst["draft_ids"],
                    "reason": dup_reason_str,
                    "spend_1d": inst["spend_1d"],
                    "conv_1d": inst["conv_1d"],
                    "spend_2d": inst["spend_2d"],
                    "conv_2d": inst["conv_2d"],
                    "spend_3d": inst["spend_3d"],
                    "conv_3d": inst["conv_3d"],
                    "spend_7d": inst["spend_7d"],
                    "conv_7d": inst["conv_7d"],
                    "global_spend_1d": tot_sp1,
                    "global_conv_1d": tot_cv1,
                    "global_spend_2d": tot_sp2,
                    "global_conv_2d": tot_cv2,
                    "global_spend_3d": tot_sp3,
                    "global_conv_3d": tot_cv3,
                    "global_spend_7d": tot_sp7,
                    "global_conv_7d": tot_cv7,
                    "paused_at": datetime.now(timezone.utc).isoformat(),
                }
                print(
                    f"    -> [PAUSED COPY] [{inst['acc_name']}] Camp='{inst['campaign_name']}' "
                    f"(ad_id={inst['ad_id']}, draft={inst['draft_str']}) | Copy 3D=${inst['spend_3d']:.2f}/{inst['conv_3d']}cv | 7D=${inst['spend_7d']:.2f}/{inst['conv_7d']}cv"
                )
            else:
                print(f"    -> [ERROR] Failed to pause copy {inst['draft_str']} in ad_id {inst['ad_id']}: {resp_kill}")

        history["tiktok_disabled_dup_videos"][v_key] = {
            "v_key": v_key,
            "sample_name": grp["sample_name"],
            "accounts": sorted(grp["accounts"]),
            "total_copies": len(grp["ad_ids"]),
            "closed_copies": closed_copies,
            "spend_1d": tot_sp1,
            "conv_1d": tot_cv1,
            "spend_2d": tot_sp2,
            "conv_2d": tot_cv2,
            "spend_3d": tot_sp3,
            "conv_3d": tot_cv3,
            "spend_7d": tot_sp7,
            "conv_7d": tot_cv7,
            "reason": dup_reason_str,
            "disabled_at": datetime.now(timezone.utc).isoformat(),
        }

    save_history(history)
    print(
        f"\nDone! Total TikTok Campaigns paused: {total_campaigns_paused} | "
        f"Smart+ videos paused: {total_paused} (Global Dup Video kills: {total_dup_videos_paused}) | "
        f"Auto-Revived (delayed sales): {total_revived}"
    )


if __name__ == "__main__":
    main()
