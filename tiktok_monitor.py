import csv
import io
import json
import os
import re
import socket
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

_orig_getaddrinfo = socket.getaddrinfo
_dns_cache = {}
_STATIC_DNS_FALLBACK = {
    "adsapi.snapchat.com": "34.110.187.193",
    "accounts.snapchat.com": "34.149.46.130",
    "ads.tiktok.com": "2.21.69.200",
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
        req = urllib.request.Request(
            f"https://1.1.1.1/dns-query?name={host}&type=A",
            headers={"accept": "application/dns-json"},
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        ips = [a["data"] for a in r.get("Answer", []) if a.get("type") == 1]
        if ips:
            _dns_cache[host] = ips[0]
            return _orig_getaddrinfo(ips[0], port, socket.AF_INET, type, proto, flags)
    except Exception:
        pass
    if host in _STATIC_DNS_FALLBACK:
        _dns_cache[host] = _STATIC_DNS_FALLBACK[host]
        return _orig_getaddrinfo(_dns_cache[host], port, socket.AF_INET, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)

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
# 3. Video spend >= $1.00 and CPC > $0.25 (or 0 clicks) with 0 sales -> Pause Video
#    * EXCEPTION: If the video itself has conversions at a profitable CPA, do NOT pause it for CPC;
#      keep it running until its CPA exceeds MAX_VIDEO_CPA, then pause it.
# Aggregated Duplicated Video Level (Across ALL Campaigns & ALL Accounts):
# 1. Duplicated video total spend >= $10.00 (1D/2D/3D/7D) with 0 sales -> Pause video across ALL campaigns & accounts
# 2. Duplicated video total CPA >= $12.00 (1D/2D/3D/7D, with >= 1 sale) -> Pause video across ALL campaigns & accounts
MAX_CAMPAIGN_NO_SALE_SPEND = 4.50
MAX_CAMPAIGN_CPA = 7.00
MAX_REGULAR_CAMPAIGN_NO_SALE_1D = 7.00
MAX_REGULAR_CAMPAIGN_NO_SALE_3D = 12.00
MAX_REGULAR_CAMPAIGN_CPA_3D = 7.00
MAX_NO_SALE_SPEND = 4.50
MAX_VIDEO_CPA = 8.00
GULF_MAX_NO_SALE_SPEND = 8.50
GULF_MAX_VIDEO_CPA = 12.00
MIN_SPEND_FOR_CPC = 1.00
MAX_CPC = 0.25
MAX_CPC_WITH_SALES = 0.35
MAX_CAMPAIGN_CPC = 0.36
MAX_DUP_VIDEO_NO_SALE_SPEND = 7.00
MAX_DUP_VIDEO_CPA = 7.00

GULF_KEYWORDS = (
    "oman", "qatar", "qat", "bahrain", "kuwait", "kuwiat", "kwt", "uae", "emarat", "gcc", "gulf",
    "عمان", "قطر", "البحرين", "بحرين", "كويت", "الكويت", "الامارات", "الإمارات", "خليج",
)


def is_gulf_campaign(camp_name: str, ad_name: str = "") -> bool:
    combined = f"{camp_name or ''} {ad_name or ''}".lower()
    return any(k in combined for k in GULF_KEYWORDS)


def get_gulf_tiktok_sim_limits(camp_name: str, ad_name: str = "") -> tuple[float, float, float]:
    """يرجع (sim_max_cpa, camp_no_sale_stop, video_no_sale_stop) لكل دولة ومنتج خليجي حسب محاكاة شريك الدفع عند الاستلام."""
    combined = f"{camp_name or ''} {ad_name or ''}".lower()
    is_om = any(k in combined for k in ("oman", "عمان", "عُمان"))
    is_kw = any(k in combined for k in ("kuwait", "kuwiat", "kwt", "كويت", "الكويت"))
    is_qa = any(k in combined for k in ("qatar", "qat", "قطر"))
    is_flash = any(k in combined for k in ("kashaf", "kasahf", "كشاف", "flash", "led"))
    is_cushion = any(k in combined for k in ("car", "armrest", "cushion", "pillow", "وساد", "تكاي"))
    if is_om:
        if is_flash:
            return (6.69, 6.69, 2.80)
        return (7.87, 7.87, 2.75)
    if is_kw:
        return (11.48, 11.48, 3.20)
    if is_qa:
        if is_cushion:
            return (10.00, 10.00, 3.20)
        return (11.50, 11.50, 3.50)
    return (10.00, 8.50, 3.20)


def get_gulf_tiktok_max_cpc(camp_name: str, ad_name: str = "") -> float:
    """يرجع السقف الأقصى لسعر النقرة بدون مبيعة في الخليج ($0.25 لعُمان، و$0.26 للكويت وقطر)."""
    combined = f"{camp_name or ''} {ad_name or ''}".lower()
    if any(k in combined for k in ("oman", "عمان", "عُمان")):
        return 0.25
    return 0.26


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
        try:
            cached = json.loads(COOKIES_FILE.read_text(encoding="utf-8-sig"))
            if len(cached) >= 20:
                s_chk, h_chk = build_session(cached)
                today_d = datetime.now().strftime("%Y-%m-%d")
                if fetch_campaigns(s_chk, h_chk, "7631763556322148369", today_d, today_d):
                    return cached
        except Exception:
            pass
    try:
        import glob
        import websocket
        candidate_ports = [9224, 9222, 9223]
        all_port_files = sorted(
            set(
                glob.glob(r"C:\.ADSPOWER_GLOBAL\cache\*\DevToolsActivePort")
                + glob.glob(r"E:\.ADSPOWER_GLOBAL\cache\*\DevToolsActivePort")
            ),
            key=os.path.getmtime,
            reverse=True,
        )
        for d in all_port_files[:6]:
            try:
                lines = Path(d).read_text(encoding="utf-8", errors="ignore").splitlines()
                if lines:
                    candidate_ports.append(int(lines[0].strip()))
            except Exception:
                pass
        for port in candidate_ports:
            try:
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1.5).read().decode("utf-8"))
                ws_urls = [t.get("webSocketDebuggerUrl", "").replace("ws://localhost:", "ws://127.0.0.1:") for t in tabs if t.get("webSocketDebuggerUrl")]
                if not ws_urls:
                    ver = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5).read().decode("utf-8"))
                    if ver.get("webSocketDebuggerUrl"):
                        ws_urls = [ver["webSocketDebuggerUrl"].replace("ws://localhost:", "ws://127.0.0.1:")]
                if not ws_urls:
                    continue
                ws = websocket.create_connection(ws_urls[0], suppress_origin=True, timeout=3.0)
                ws.send(json.dumps({"id": 1, "method": "Network.getAllCookies"}))
                res_msg = json.loads(ws.recv())
                cs = res_msg.get("result", {}).get("cookies", [])
                if not cs:
                    ws.send(json.dumps({"id": 2, "method": "Storage.getCookies"}))
                    cs = json.loads(ws.recv()).get("result", {}).get("cookies", [])
                ws.close()
                tt = [c for c in cs if "tiktok" in c.get("domain", "")]
                has_ads_sess = any(c.get("name") in ("sessionid_ss_ads", "sid_tt_ads", "sessionid_ads") and c.get("value") for c in tt)
                if len(tt) >= 20 and has_ads_sess:
                    s_test, h_test = build_session(tt)
                    today_d = datetime.now().strftime("%Y-%m-%d")
                    if fetch_campaigns(s_test, h_test, "7631763556322148369", today_d, today_d):
                        COOKIES_FILE.write_text(json.dumps(tt, ensure_ascii=False, indent=2), encoding="utf-8")
                        return tt
            except Exception:
                pass
    except Exception:
        pass
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


SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/1AaGXjCDbrD7tLEpYaJf5cncgZ3HW5IP3J8TIVAcn0pE/gviz/tq?tqx=out:csv"
_SHEET_GIFT_KW = ("باور بانك صغير للطوارئ", "سوار رجالي", "سماعات", "هدية", "مجانا", "مجاناً")
_SHEET_UPSELL_KW = (
    "عرض خاص",
    "حامل الجوال المغناطيسي",
    "شاحن سيارة فائق السرعة",
    "كابل شحن 4 في 1",
    "كابل شحن سلك",
    "240w",
    "مكنسة السيارة",
    "ممتص صدمات",
    "فوطة مايكروفايبر",
    "ميدالية",
)


def fetch_sheet_tiktok_sales() -> dict:
    """
    Returns {date_yyyy_mm_dd_ksa: {utm_lower: deduplicated_main_orders_count}} from Lightfunnels Google Sheet.
    Excludes free gifts and order-bump upsells, and deduplicates multi-row bundles within 60s per UTM.
    """
    by_day_utm = {}
    csv_text = ""
    for _ in range(2):
        try:
            r = requests.get(SHEET_CSV_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if r.status_code == 200 and r.text:
                csv_text = r.text
                break
        except Exception:
            time.sleep(0.8)
    if not csv_text:
        return by_day_utm

    sa_tz = timezone(timedelta(hours=3))
    seen_keys = set()
    last_ts_by_utm = {}
    try:
        rows = list(csv.reader(io.StringIO(csv_text)))
        for row in rows[1:]:
            if len(row) < 3:
                continue
            p_raw = (row[0] or "").strip()
            u_raw = (row[1] or "").strip()
            d_raw = (row[2] or "").strip()
            if not p_raw or not u_raw or len(d_raw) < 10:
                continue
            pl = p_raw.lower()
            if any(g in pl for g in _SHEET_GIFT_KW) or any(u in pl for u in _SHEET_UPSELL_KW):
                continue
            ul = u_raw.lower()
            if ul in ("direct", "__campaign_name__"):
                continue
            try:
                dt_sa = datetime.strptime(d_raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(sa_tz)
                ts = dt_sa.timestamp()
                d_str = dt_sa.strftime("%Y-%m-%d")
            except Exception:
                ts = None
                d_str = d_raw[:10]
            ukey = f"{ul}|{p_raw}|{d_raw[:16]}"
            if ukey in seen_keys:
                continue
            if ts is not None and ul in last_ts_by_utm and abs(ts - last_ts_by_utm[ul]) <= 60:
                seen_keys.add(ukey)
                continue
            seen_keys.add(ukey)
            if ts is not None:
                last_ts_by_utm[ul] = ts
            day_map = by_day_utm.setdefault(d_str, {})
            day_map[ul] = day_map.get(ul, 0) + 1
    except Exception as e:
        print(f"  [WARN] fetch_sheet_tiktok_sales parse warning: {e}")
    return by_day_utm


def sum_sheet_utm_window(sheet_by_day: dict, utm_keys: list[str], st_date: str, et_date: str) -> int:
    if not sheet_by_day or not utm_keys:
        return 0
    total = 0
    for d_str, umap in sheet_by_day.items():
        if st_date <= d_str <= et_date:
            for uk in utm_keys:
                if uk:
                    total += int(umap.get(uk, 0))
    return total


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

    _orig_request = s.request
    def _resilient_request(method, url, **kwargs):
        last_exc = None
        for attempt in range(3):
            try:
                return _orig_request(method, url, **kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
        raise last_exc
    s.request = _resilient_request

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


def is_cross_monitored_product_campaign(campaign_name: str, video_name: str = "") -> bool:
    combined = f"{campaign_name} {video_name}".lower()
    return any(k in combined for k in ("مايباخ", "maybach", "time x", "timex", "تايم إكس", "دفاتر", "مسخن الرضاعات", "مسخن رضاعات", "warmer", "bottle warmer"))


def is_shutoff_pillow_campaign_tt(campaign_name: str, video_name: str = "") -> bool:
    """جميع حملات الوسادة القديمة مطفأة نهائياً في تيك توك باستثناء حملات الخليج المعتمدة (الكويت وقطر) وحملات التجارب المصغرة [EXP-LEADS] أو AutoPlacement أو النقل CrossPlatform المعتمدة بقاعدة 50 أو دفعات الوسادة الجديدة (Recycled 31-10 / 01-11)."""
    combined = f"{campaign_name or ''} {video_name or ''}".lower()
    if is_gulf_campaign(campaign_name, video_name):
        # سلطنة عُمان لا يوجد فيها وسادة سيارة نهائياً (باور بانك + كشاف فقط)
        if any(k in combined for k in ("oman", "عمان")) and any(k in combined for k in ("car armrest", "car cushion", "وسادة", "تكاية")):
            return True
        return False
    if any(k in combined for k in ("[exp-leads]", "[exp-", "autoplacement", "auto placement", "crossplatform", "recycled", "31-10", "01-11", "02-11", "manzilk", "منزلك", "6 winners")):
        return False
    return any(k in combined for k in ("car armrest", "car cushion", "وسادة", "تكاية", "maybach"))


# توافق كامل مع run_all_monitors_5h و tiktok_2h_sales_master
is_banned_pillow_tiktok = is_shutoff_pillow_campaign_tt


CORE_PRODUCT_KEYWORDS_TT = (
    "car", "armrest", "cushion", "pillow", "console", "وساد", "تكاي", "مسند", "سيار",
    "power", "pb", "voltrx", "voltrix", "bank", "باور", "فولتريكس", "شاحن",
    "watch", "suis", "ساع",
)


def is_taager_test_campaign_tt(campaign_name: str, video_name: str = "") -> bool:
    """يرجع True لأي حملة تابعة لمنتجات تاجر قيد الاختبار (غسالة HP-909، مبخرة BW-20، موقد راف R.8019، أو أي حملة تحمل وسم taager/تاجر)."""
    combined = f"{campaign_name or ''} {video_name or ''}".lower()
    return any(
        k in combined
        for k in (
            "taager", "تاجر",
            "washer", "hp909", "hp-909", "غسالة", "غساله",
            "bakhoor", "bw20", "bw-20", "مبخرة", "مبخره",
            "r8019", "r.8019", "8019", "raf ", "cooker", "موقد", "سيراميك",
        )
    )


def extract_taager_product_key_tt(campaign_name: str, video_name: str = "") -> str | None:
    """يرجع مفتاح منتج تاجر المستقل ('washer_hp909' أو 'bakhoor_bw20' أو 'cooker_raf8019' أو مفتاح مخصص) ليتم تقييم كل منتج تاجر على حدة بقاعدة 25$ بدون مبيعة / 27$ CPA المشتركة بين غوغل وتيك توك."""
    combined = f"{campaign_name or ''} {video_name or ''}".lower()
    if any(k in combined for k in ("washer", "hp909", "hp-909", "غسالة", "غساله")):
        return "washer_hp909"
    if any(k in combined for k in ("bakhoor", "bw20", "bw-20", "مبخرة", "مبخره")):
        return "bakhoor_bw20"
    if any(k in combined for k in ("r8019", "r.8019", "8019", "raf ", "cooker", "موقد", "سيراميك")):
        return "cooker_raf8019"
    if "taager" in combined or "تاجر" in combined:
        cleaned = re.sub(r"\([^)]*\)", " ", combined)
        cleaned = re.sub(r"\b\d{1,2}[-_/]\d{1,2}(?:[-_/]\d{2,4})?\b", " ", cleaned)
        cleaned = re.sub(r"\b(taager|تاجر|test|testing|copy|scale|ads|ad|camp|campaign|ksa|sa|a|b|c|d|v\d+|\d+)\b", " ", cleaned)
        cleaned = re.sub(r"[-_/#|+]+", " ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return f"taager_{cleaned}" if cleaned else "taager_product"
    return None


def extract_new_product_key_tt(campaign_name: str) -> str | None:
    """يرجع مفتاحاً موحداً لأي منتج جديد غير المنتجات الثلاثة الأساسية (الوسادة، الباور بانك، الساعة) وحملات الخليج المعتمدة ومنتجات تاجر (لأن منتجات تاجر تخضع لقاعدتها المستقلة 25$ بدون بيع / 27$ CPA المشتركة بين غوغل وتيك توك لكل منتج على حدة)."""
    raw = (campaign_name or "").strip().lower()
    if not raw or is_cross_monitored_product_campaign(raw) or is_gulf_campaign(raw) or is_taager_test_campaign_tt(raw):
        return None
    if any(k in raw for k in CORE_PRODUCT_KEYWORDS_TT):
        return None
    cleaned = re.sub(r"\([^)]*\)", " ", raw)
    cleaned = re.sub(r"\b\d{1,2}[-_/]\d{1,2}(?:[-_/]\d{2,4})?\b", " ", cleaned)
    cleaned = re.sub(
        r"\b(test|testing|copy|scale|boost|turbo|elite|rescue|clean|ads|ad|camp|campaign|ksa|sa|gulf|gcc|a|b|c|d|v\d+|\d+)\b",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"[-_/#|+]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else "new_product"


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
        try:
            r = session.post(
                f"https://ads.tiktok.com/api/v4/i18n/statistics/op/expand/material/list/?aadvid={aadvid}&req_src=bidding",
                headers=h,
                json=body,
                timeout=15,
            ).json()
            table = (r.get("data", {}) if isinstance(r, dict) else {}).get("table", []) or []
        except Exception as e:
            print(f"    [WARN] fetch_expanded_materials page {page} error: {e}")
            break
        all_rows.extend(table)
        last_spend = float((table[-1].get("stat_cost") if table else 0) or 0)
        if len(table) < 100 or page >= 5 or (page >= 2 and last_spend <= 0.0):
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


def is_new_test_video_tt(camp_name: str, video_name: str = "") -> bool:
    """يرجع True لأي فيديو جديد نختبره في تيك توك (حملات الاختبار أو الدفعات الجديدة غير المصنفة كـ Elite/Winners/Rescue/Fresh Clean/Flow Hooks)."""
    cn_l = (camp_name or "").strip().lower()
    vn_l = (video_name or "").strip().lower()
    combined = f"{cn_l} {vn_l}"
    if any(k in combined for k in ("test", "تيست", "تست", "batch", "seedance", "hooks", "new", "جديد", "25-10", "26-10", "27-10", "28-10", "29-10", "30-10", "31-10")):
        if not any(w in cn_l for w in ("winners", "elite", "rescue", "fresh clean", "scale", "flow hooks", "flow hook", "recycled")):
            return True
    return False


def evaluate_video_rules(
    spend_1d, conv_1d, cpc_1d, clicks_1d,
    spend_2d, conv_2d, cpc_2d, clicks_2d,
    spend_3d=0.0, conv_3d=0, cpc_3d=0.0, clicks_3d=0,
    spend_7d=0.0, conv_7d=0, cpc_7d=0.0, clicks_7d=0,
    is_gulf=False,
    is_new_test=False,
    gulf_no_sale_limit=None,
    gulf_cpa_limit=None,
    gulf_max_cpc=None,
):
    reasons = []
    if is_gulf:
        is_new_test = False
    no_sale_limit = 4.00 if is_new_test else ((gulf_no_sale_limit or GULF_MAX_NO_SALE_SPEND) if is_gulf else MAX_NO_SALE_SPEND)
    cpa_limit = 8.00 if is_new_test else ((gulf_cpa_limit or GULF_MAX_VIDEO_CPA) if is_gulf else MAX_VIDEO_CPA)
    min_cpc_spend = MIN_SPEND_FOR_CPC  # $1.00 unified across all TikTok videos
    max_cpc_lim = (gulf_max_cpc or MAX_CPC) if is_gulf else MAX_CPC
    max_cpc_with_sales_lim = MAX_CPC_WITH_SALES  # $0.35 max CPC cap even when video has sales
    region_tag = "[New Test Video] " if is_new_test else ("[Gulf] " if is_gulf else "")

    eff_cpc_1d = max(round(cpc_1d, 2), round(spend_1d / clicks_1d, 2)) if clicks_1d > 0 else 0.0
    eff_cpc_2d = max(round(cpc_2d, 2), round(spend_2d / clicks_2d, 2)) if clicks_2d > 0 else 0.0
    eff_cpc_3d = max(round(cpc_3d, 2), round(spend_3d / clicks_3d, 2)) if clicks_3d > 0 else 0.0
    eff_cpc_7d = max(round(cpc_7d, 2), round(spend_7d / clicks_7d, 2)) if clicks_7d > 0 else 0.0

    has_profitable_3d = conv_3d > 0 and (spend_3d / conv_3d) <= cpa_limit
    has_profitable_2d = conv_2d > 0 and (spend_2d / conv_2d) <= cpa_limit
    has_profitable_1d = conv_1d > 0 and (spend_1d / conv_1d) <= cpa_limit
    has_profitable_sales = has_profitable_3d or has_profitable_2d or has_profitable_1d
    good_cpc_today_35 = spend_1d >= min_cpc_spend and clicks_1d > 0 and eff_cpc_1d <= max_cpc_with_sales_lim

    if is_new_test:
        # القاعدة الصارمة للفيديوهات الجديدة قيد الاختبار بأمر المستخدم:
        # 1) لو ما جاب ولا بيعة بعد $4.00 يطفيه (>= $4.00 بدون مبيعة)
        if spend_1d >= 4.00 and conv_1d == 0:
            reasons.append(f"{region_tag}Today Spend ${spend_1d:.2f} >= $4.00 with 0 sales")
        elif spend_2d >= 4.00 and conv_2d == 0:
            reasons.append(f"{region_tag}2-Day Spend ${spend_2d:.2f} >= $4.00 with 0 sales")
        elif spend_3d >= 4.00 and conv_3d == 0:
            reasons.append(f"{region_tag}3-Day Spend ${spend_3d:.2f} >= $4.00 with 0 sales")
        elif spend_7d >= 4.00 and conv_7d == 0:
            reasons.append(f"{region_tag}7-Day Spend ${spend_7d:.2f} >= $4.00 with 0 sales")

        # 2) لو عنده مبيعات و CPC <= $0.35 يبقى شغالاً طالما CPA <= $8.00 (أو $9.99 لـ >= 3 مبيعات)
        test_cpa_cap = 9.99 if (conv_1d >= 3 or conv_3d >= 3) else 8.00
        if conv_1d > 0 and (spend_1d / conv_1d) > test_cpa_cap:
            reasons.append(f"{region_tag}Today Video CPA ${(spend_1d / conv_1d):.2f} > ${test_cpa_cap:.2f} (Spend=${spend_1d:.2f}, Sales={conv_1d})")
        elif conv_2d > 0 and (spend_2d / conv_2d) > test_cpa_cap and not has_profitable_1d:
            reasons.append(f"{region_tag}2-Day Video CPA ${(spend_2d / conv_2d):.2f} > ${test_cpa_cap:.2f} (Spend=${spend_2d:.2f}, Sales={conv_2d})")
        elif conv_3d > 0 and (spend_3d / conv_3d) > test_cpa_cap and not has_profitable_1d:
            reasons.append(f"{region_tag}3-Day Video CPA ${(spend_3d / conv_3d):.2f} > ${test_cpa_cap:.2f} (Spend=${spend_3d:.2f}, Sales={conv_3d})")
        elif conv_7d > 0 and (spend_7d / conv_7d) > test_cpa_cap and not has_profitable_3d and not has_profitable_1d:
            reasons.append(f"{region_tag}7-Day Video CPA ${(spend_7d / conv_7d):.2f} > ${test_cpa_cap:.2f} (Spend=${spend_7d:.2f}, Sales={conv_7d})")

        # 3) قاعدة CPC تيك توك (محدثة 2026-10-30):
        #    - بدون مبيعات: أي فيديو يصرف >= $1.00 و CPC > $0.25 (أو 0 نقرات) يطفيه فوراً
        #    - مع وجود مبيعات: يبقى شغالاً بشرط ألا يتجاوز سعر النقرة $0.35 (إذا فات $0.35 يطفيه فوراً حتى لو عنده مبيعات)
        if spend_1d >= min_cpc_spend and (clicks_1d == 0 or eff_cpc_1d > max_cpc_with_sales_lim):
            reasons.append(f"{region_tag}Today Spend ${spend_1d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_1d:.2f} > ${max_cpc_with_sales_lim:.2f} (Exceeds $0.35 Max CPC Cap even with sales)")
        elif not good_cpc_today_35 and spend_2d >= min_cpc_spend and (clicks_2d == 0 or eff_cpc_2d > max_cpc_with_sales_lim):
            reasons.append(f"{region_tag}2-Day Spend ${spend_2d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_2d:.2f} > ${max_cpc_with_sales_lim:.2f} (Exceeds $0.35 Max CPC Cap even with sales)")
        elif not good_cpc_today_35 and spend_3d >= min_cpc_spend and (clicks_3d == 0 or eff_cpc_3d > max_cpc_with_sales_lim):
            reasons.append(f"{region_tag}3-Day Spend ${spend_3d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_3d:.2f} > ${max_cpc_with_sales_lim:.2f} (Exceeds $0.35 Max CPC Cap even with sales)")

        if not has_profitable_1d and not reasons and conv_1d == 0 and spend_1d >= min_cpc_spend:
            if clicks_1d == 0:
                reasons.append(f"{region_tag}Today Spend ${spend_1d:.2f} >= ${min_cpc_spend:.2f} with 0 clicks (0 sales)")
            elif eff_cpc_1d > max_cpc_lim:
                reasons.append(f"{region_tag}Today Spend ${spend_1d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_1d:.2f} > ${max_cpc_lim:.2f} (Clicks={clicks_1d}, 0 sales)")
        if not reasons and not has_profitable_sales and conv_2d == 0 and spend_2d >= min_cpc_spend:
            if clicks_2d == 0:
                reasons.append(f"{region_tag}2-Day Spend ${spend_2d:.2f} >= ${min_cpc_spend:.2f} with 0 clicks (0 sales)")
            elif eff_cpc_2d > max_cpc_lim:
                reasons.append(f"{region_tag}2-Day Spend ${spend_2d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_2d:.2f} > ${max_cpc_lim:.2f} (Clicks={clicks_2d}, 0 sales)")
        if not reasons and not has_profitable_sales and conv_3d == 0 and spend_3d >= min_cpc_spend:
            if clicks_3d == 0:
                reasons.append(f"{region_tag}3-Day Spend ${spend_3d:.2f} >= ${min_cpc_spend:.2f} with 0 clicks (0 sales)")
            elif eff_cpc_3d > max_cpc_lim:
                reasons.append(f"{region_tag}3-Day Spend ${spend_3d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_3d:.2f} > ${max_cpc_lim:.2f} (Clicks={clicks_3d}, 0 sales)")
        if not reasons and not has_profitable_sales and conv_7d == 0 and spend_7d >= min_cpc_spend:
            if clicks_7d == 0:
                reasons.append(f"{region_tag}7-Day Spend ${spend_7d:.2f} >= ${min_cpc_spend:.2f} with 0 clicks (0 sales)")
            elif eff_cpc_7d > max_cpc_lim:
                reasons.append(f"{region_tag}7-Day Spend ${spend_7d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_7d:.2f} > ${max_cpc_lim:.2f} (Clicks={clicks_7d}, 0 sales)")
        return reasons

    # Rule 1: Spend > $4.50 (Saudi) or > $8.50 (Gulf) with 0 conversions
    # CRITICAL: Today or 2-Day 0-sale spend > limit ALWAYS pauses immediately even if previous days had sales!
    if spend_1d > no_sale_limit and conv_1d == 0:
        reasons.append(f"{region_tag}Today Spend ${spend_1d:.2f} > ${no_sale_limit:.2f} with 0 sales Today")
    elif spend_2d > no_sale_limit and conv_2d == 0:
        reasons.append(f"{region_tag}2-Day Spend ${spend_2d:.2f} > ${no_sale_limit:.2f} with 0 sales")
    elif spend_3d > no_sale_limit and conv_3d == 0:
        reasons.append(f"{region_tag}3-Day Spend ${spend_3d:.2f} > ${no_sale_limit:.2f} with 0 sales")
    elif spend_7d > no_sale_limit and conv_7d == 0:
        reasons.append(f"{region_tag}7-Day Accumulated Spend ${spend_7d:.2f} > ${no_sale_limit:.2f} with 0 sales")

    # Rule 2: Video CPA > $7.00 (Saudi) or > Gulf Sim CPA (Kuwait/Qatar/Oman)
    # Protect proven multi-sale winners (conv_1d >= 3 or conv_3d >= 3 with 3D CPA <= $9.99) up to $9.99 CPA (Saudi only)
    is_multi_sale_winner = (not is_gulf) and ((conv_1d >= 3 and (spend_1d / conv_1d) <= 9.99) or (conv_3d >= 3 and (spend_3d / conv_3d) <= 9.99))
    eff_cpa_lim = max(cpa_limit, 9.99) if is_multi_sale_winner else cpa_limit
    if conv_1d > 0 and (spend_1d / conv_1d) > eff_cpa_lim:
        reasons.append(f"{region_tag}Today Video CPA ${(spend_1d / conv_1d):.2f} > ${eff_cpa_lim:.2f} (Spend=${spend_1d:.2f}, Sales={conv_1d})")
    elif conv_2d > 0 and (spend_2d / conv_2d) > eff_cpa_lim and not has_profitable_1d and not has_profitable_3d:
        reasons.append(f"{region_tag}2-Day Video CPA ${(spend_2d / conv_2d):.2f} > ${eff_cpa_lim:.2f} (Spend=${spend_2d:.2f}, Sales={conv_2d})")
    elif conv_3d > 0 and (spend_3d / conv_3d) > eff_cpa_lim and not has_profitable_1d:
        reasons.append(f"{region_tag}3-Day Video CPA ${(spend_3d / conv_3d):.2f} > ${eff_cpa_lim:.2f} (Spend=${spend_3d:.2f}, Sales={conv_3d})")
    elif conv_7d > 0 and (spend_7d / conv_7d) > eff_cpa_lim and not has_profitable_3d and not has_profitable_1d:
        reasons.append(f"{region_tag}7-Day Video CPA ${(spend_7d / conv_7d):.2f} > ${eff_cpa_lim:.2f} (Spend=${spend_7d:.2f}, Sales={conv_7d})")

    # Rule 3: Two-Tier TikTok Video CPC Cut (Updated 2026-10-30 per user instruction):
    # - Tier A (With Sales: has_profitable_sales): Keep video ONLY if CPC <= $0.35; if Spend >= $1.00 & CPC > $0.35 (or 0 clicks), pause immediately!
    # - Tier B (0 Sales: not has_profitable_sales): If Spend >= $1.00 & CPC > $0.25 (or 0 clicks), pause immediately!
    if spend_1d >= min_cpc_spend and (clicks_1d == 0 or eff_cpc_1d > max_cpc_with_sales_lim):
        reasons.append(f"{region_tag}Today Spend ${spend_1d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_1d:.2f} > ${max_cpc_with_sales_lim:.2f} (Exceeds $0.35 Max CPC Cap even with sales)")
    elif not good_cpc_today_35 and spend_2d >= min_cpc_spend and (clicks_2d == 0 or eff_cpc_2d > max_cpc_with_sales_lim):
        reasons.append(f"{region_tag}2-Day Spend ${spend_2d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_2d:.2f} > ${max_cpc_with_sales_lim:.2f} (Exceeds $0.35 Max CPC Cap even with sales)")
    elif not good_cpc_today_35 and spend_3d >= min_cpc_spend and (clicks_3d == 0 or eff_cpc_3d > max_cpc_with_sales_lim):
        reasons.append(f"{region_tag}3-Day Spend ${spend_3d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_3d:.2f} > ${max_cpc_with_sales_lim:.2f} (Exceeds $0.35 Max CPC Cap even with sales)")

    if not has_profitable_1d and not reasons and conv_1d == 0 and spend_1d >= min_cpc_spend:
        if clicks_1d == 0:
            reasons.append(f"{region_tag}Today Spend ${spend_1d:.2f} >= ${min_cpc_spend:.2f} with 0 clicks (0 sales Today)")
        elif eff_cpc_1d > max_cpc_lim:
            reasons.append(f"{region_tag}Today Spend ${spend_1d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_1d:.2f} > ${max_cpc_lim:.2f} (Clicks={clicks_1d}, 0 sales Today)")
    if not has_profitable_sales and not reasons and conv_2d == 0 and spend_2d >= min_cpc_spend:
        if clicks_2d == 0:
            reasons.append(f"{region_tag}2-Day Spend ${spend_2d:.2f} >= ${min_cpc_spend:.2f} with 0 clicks (0 sales)")
        elif eff_cpc_2d > max_cpc_lim:
            reasons.append(f"{region_tag}2-Day Spend ${spend_2d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_2d:.2f} > ${max_cpc_lim:.2f} (Clicks={clicks_2d}, 0 sales)")
    if not has_profitable_sales and not reasons:
        if conv_3d == 0 and spend_3d >= min_cpc_spend:
            if clicks_3d == 0:
                reasons.append(f"{region_tag}3-Day Spend ${spend_3d:.2f} >= ${min_cpc_spend:.2f} with 0 clicks (0 sales)")
            elif eff_cpc_3d > max_cpc_lim:
                reasons.append(f"{region_tag}3-Day Spend ${spend_3d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_3d:.2f} > ${max_cpc_lim:.2f} (Clicks={clicks_3d}, 0 sales)")
        elif conv_7d == 0 and spend_7d >= min_cpc_spend:
            if clicks_7d == 0:
                reasons.append(f"{region_tag}7-Day Spend ${spend_7d:.2f} >= ${min_cpc_spend:.2f} with 0 clicks (0 sales)")
            elif eff_cpc_7d > max_cpc_lim:
                reasons.append(f"{region_tag}7-Day Spend ${spend_7d:.2f} >= ${min_cpc_spend:.2f} & CPC ${eff_cpc_7d:.2f} > ${max_cpc_lim:.2f} (Clicks={clicks_7d}, 0 sales)")
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
    # Strip duplicate leading upload-date prefix when followed by original date prefix (e.g. 19_10_17-10_ola_3 -> 17-10_ola_3)
    stem = re.sub(r"^\d{1,2}[_-]\d{1,2}[_-](?=\d{1,2}[_-]\d{1,2}[_-])", "", stem)
    return stem.strip().lower()


def evaluate_dup_video_rules(
    spend_1d: float, conv_1d: int,
    spend_2d: float, conv_2d: int,
    spend_3d: float = 0.0, conv_3d: int = 0,
    spend_7d: float = 0.0, conv_7d: int = 0,
    all_gulf: bool = False,
) -> list[str]:
    reasons = []
    dup_cpa_lim = GULF_MAX_VIDEO_CPA if all_gulf else MAX_DUP_VIDEO_CPA
    has_good_3d = conv_3d > 0 and (spend_3d / conv_3d) <= dup_cpa_lim
    # Rule 1: Aggregated Today / 2D / 3D / 7D Spend >= $12.00 across all copies with 0 conversions
    if spend_1d >= MAX_DUP_VIDEO_NO_SALE_SPEND and conv_1d == 0 and not has_good_3d:
        reasons.append(
            f"[Global Dup Video Stop-Loss 1D] Total Today Spend ${spend_1d:.2f} >= ${MAX_DUP_VIDEO_NO_SALE_SPEND:.2f} with 0 sales"
        )
    elif spend_2d >= MAX_DUP_VIDEO_NO_SALE_SPEND and conv_2d == 0 and not has_good_3d:
        reasons.append(
            f"[Global Dup Video Stop-Loss 2D] Total 2-Day Spend ${spend_2d:.2f} >= ${MAX_DUP_VIDEO_NO_SALE_SPEND:.2f} with 0 sales"
        )
    elif spend_3d >= MAX_DUP_VIDEO_NO_SALE_SPEND and conv_3d == 0:
        reasons.append(
            f"[Global Dup Video Stop-Loss 3D] Total 3-Day Spend ${spend_3d:.2f} >= ${MAX_DUP_VIDEO_NO_SALE_SPEND:.2f} with 0 sales"
        )
    elif spend_7d >= MAX_DUP_VIDEO_NO_SALE_SPEND and conv_7d == 0:
        reasons.append(
            f"[Global Dup Video Stop-Loss 7D] Total 7-Day Spend ${spend_7d:.2f} >= ${MAX_DUP_VIDEO_NO_SALE_SPEND:.2f} with 0 sales"
        )
    # Rule 2: Aggregated CPA >= $9.50 ($13.00 Gulf) across all copies (when Spend >= $12.00)
    if conv_3d > 0 and spend_3d >= MAX_DUP_VIDEO_NO_SALE_SPEND and (spend_3d / conv_3d) >= dup_cpa_lim:
        reasons.append(
            f"[Global Dup Video High CPA 3D] Total 3-Day CPA ${(spend_3d / conv_3d):.2f} >= ${dup_cpa_lim:.2f} "
            f"(Total Spend=${spend_3d:.2f}, Total Sales={conv_3d})"
        )
    elif conv_7d > 0 and spend_7d >= MAX_DUP_VIDEO_NO_SALE_SPEND and (spend_7d / conv_7d) >= dup_cpa_lim and not has_good_3d:
        reasons.append(
            f"[Global Dup Video High CPA 7D] Total 7-Day CPA ${(spend_7d / conv_7d):.2f} >= ${dup_cpa_lim:.2f} "
            f"(Total Spend=${spend_7d:.2f}, Total Sales={conv_7d})"
        )
    elif conv_2d > 0 and spend_2d >= MAX_DUP_VIDEO_NO_SALE_SPEND and (spend_2d / conv_2d) >= dup_cpa_lim and not has_good_3d:
        reasons.append(
            f"[Global Dup Video High CPA 2D] Total 2-Day CPA ${(spend_2d / conv_2d):.2f} >= ${dup_cpa_lim:.2f} "
            f"(Total Spend=${spend_2d:.2f}, Total Sales={conv_2d})"
        )
    elif conv_1d > 0 and spend_1d >= MAX_DUP_VIDEO_NO_SALE_SPEND and (spend_1d / conv_1d) >= dup_cpa_lim and not has_good_3d:
        reasons.append(
            f"[Global Dup Video High CPA 1D] Total Today CPA ${(spend_1d / conv_1d):.2f} >= ${dup_cpa_lim:.2f} "
            f"(Total Spend=${spend_1d:.2f}, Total Sales={conv_1d})"
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
    # Global tracker for NEW products across ALL campaigns & ALL 6 accounts ($7.00 0-sale stop-loss rule)
    global_new_products = {}
    sheet_sales_by_day = fetch_sheet_tiktok_sales()

    prefetched_acc = {}
    cn_max_spender_1d = {}
    cn_max_sp_val_1d = {}
    cn_max_spender_3d = {}
    cn_max_sp_val_3d = {}
    for acc_name, aadvid, utc_offset in ACCOUNTS:
        now_tz = datetime.now(timezone(timedelta(hours=utc_offset)))
        today_str = now_tz.strftime("%Y-%m-%d")
        yesterday_str = (now_tz - timedelta(days=1)).strftime("%Y-%m-%d")
        two_days_ago_str = (now_tz - timedelta(days=2)).strftime("%Y-%m-%d")
        six_days_ago_str = (now_tz - timedelta(days=6)).strftime("%Y-%m-%d")
        camps_1d = fetch_campaigns(session, headers, aadvid, today_str, today_str)
        camps_yest = fetch_campaigns(session, headers, aadvid, yesterday_str, yesterday_str)
        camps_2d = fetch_campaigns(session, headers, aadvid, yesterday_str, today_str)
        camps_3d = fetch_campaigns(session, headers, aadvid, two_days_ago_str, today_str)
        camps_7d = fetch_campaigns(session, headers, aadvid, six_days_ago_str, today_str)
        prefetched_acc[aadvid] = (today_str, yesterday_str, two_days_ago_str, six_days_ago_str, camps_1d, camps_yest, camps_2d, camps_3d, camps_7d)
        for c_it in camps_1d:
            cn_l = (c_it.get("campaign_name") or "").strip().lower()
            sp_v = float((c_it.get("row_data") or {}).get("stat_cost") or 0.0)
            if cn_l and sp_v > cn_max_sp_val_1d.get(cn_l, 0.0):
                cn_max_sp_val_1d[cn_l] = sp_v
                cn_max_spender_1d[cn_l] = aadvid
        for c_it in camps_3d:
            cn_l = (c_it.get("campaign_name") or "").strip().lower()
            sp_v = float((c_it.get("row_data") or {}).get("stat_cost") or 0.0)
            if cn_l and sp_v > cn_max_sp_val_3d.get(cn_l, 0.0):
                cn_max_sp_val_3d[cn_l] = sp_v
                cn_max_spender_3d[cn_l] = aadvid

    for acc_name, aadvid, utc_offset in ACCOUNTS:
        today_str, yesterday_str, two_days_ago_str, six_days_ago_str, camps_1d, camps_yest, camps_2d, camps_3d, camps_7d = prefetched_acc[aadvid]
        camp_1d_map = {str(c.get("campaign_id")): c for c in camps_1d if c.get("campaign_id")}
        camp_yest_map = {str(c.get("campaign_id")): c for c in camps_yest if c.get("campaign_id")}
        camp_2d_map = {str(c.get("campaign_id")): c for c in camps_2d if c.get("campaign_id")}
        camp_3d_map = {str(c.get("campaign_id")): c for c in camps_3d if c.get("campaign_id")}
        camp_7d_map = {str(c.get("campaign_id")): c for c in camps_7d if c.get("campaign_id")}
        camp_sheet_protected_1d = {}

        # Aggregate all campaigns (active + paused) per new product key across accounts
        all_known_cids_acc = set(camp_7d_map.keys()) | set(camp_3d_map.keys()) | set(camp_1d_map.keys())
        for cid_np in all_known_cids_acc:
            c_ref = camp_3d_map.get(cid_np) or camp_7d_map.get(cid_np) or camp_1d_map.get(cid_np) or {}
            cname_np = c_ref.get("campaign_name") or ""
            np_key = extract_new_product_key_tt(cname_np)
            if not np_key:
                continue
            rd1_np = (camp_1d_map.get(cid_np) or {}).get("row_data") or {}
            rd2_np = (camp_2d_map.get(cid_np) or {}).get("row_data") or {}
            rd3_np = (camp_3d_map.get(cid_np) or {}).get("row_data") or {}
            rd7_np = (camp_7d_map.get(cid_np) or {}).get("row_data") or {}
            sp1_np = float(rd1_np.get("stat_cost") or 0.0)
            cv1_np = int(float(rd1_np.get("time_attr_convert_cnt") or 0))
            sp2_np = float(rd2_np.get("stat_cost") or 0.0)
            cv2_np = int(float(rd2_np.get("time_attr_convert_cnt") or 0))
            sp3_np = float(rd3_np.get("stat_cost") or 0.0)
            cv3_np = int(float(rd3_np.get("time_attr_convert_cnt") or 0))
            sp7_np = float(rd7_np.get("stat_cost") or sp3_np)
            cv7_np = int(float(rd7_np.get("time_attr_convert_cnt") or cv3_np))
            is_act_np = (
                c_ref.get("primary_status") not in ("disable", "delete")
                and str(c_ref.get("opt_status")) == "0"
            )
            np_bucket = global_new_products.setdefault(np_key, {
                "product_key": np_key,
                "spend_1d": 0.0, "conv_1d": 0,
                "spend_2d": 0.0, "conv_2d": 0,
                "spend_3d": 0.0, "conv_3d": 0,
                "spend_7d": 0.0, "conv_7d": 0,
                "active_campaigns": [],
            })
            np_bucket["spend_1d"] = round(np_bucket["spend_1d"] + sp1_np, 2)
            np_bucket["conv_1d"] += cv1_np
            np_bucket["spend_2d"] = round(np_bucket["spend_2d"] + sp2_np, 2)
            np_bucket["conv_2d"] += cv2_np
            np_bucket["spend_3d"] = round(np_bucket["spend_3d"] + sp3_np, 2)
            np_bucket["conv_3d"] += cv3_np
            np_bucket["spend_7d"] = round(np_bucket["spend_7d"] + sp7_np, 2)
            np_bucket["conv_7d"] += cv7_np
            if is_act_np:
                np_bucket["active_campaigns"].append({
                    "acc_name": acc_name,
                    "aadvid": aadvid,
                    "campaign_id": cid_np,
                    "campaign_name": cname_np,
                    "spend_1d": sp1_np,
                    "conv_1d": cv1_np,
                    "spend_3d": sp3_np,
                    "conv_3d": cv3_np,
                })

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
        is_before_6pm_sa = (6 <= now_sa.hour < 18)  # أمر المستخدم الصارم: لا يرفع بدجت بعد الساعة 6 مساءً أبداً!
        tt_scaled_today = history.setdefault("proactive_scaled_tiktok", {})

        for c3 in active_camps:
            camp_id = str(c3.get("campaign_id") or "")
            camp_name = c3.get("campaign_name") or "Campaign"
            if is_shutoff_pillow_campaign_tt(camp_name) or is_cross_monitored_product_campaign(camp_name):
                ok_new_p, _ = pause_campaign(session, headers, aadvid, camp_id, operation="disable")
                if ok_new_p:
                    total_campaigns_paused += 1
                    print(f"    🛑 [PERMANENT SHUTOFF PILLOW/NEW PRODUCT] '{camp_name}' ({camp_id}) -> Paused permanently per user command")
                continue

            # Strict Target Cost (Cost Cap) enforcement: NEVER allow Maximum Delivery / Maximum Bid without Target CPA
            camp_cbo_bid = float((c3.get("row_data") or {}).get("cpa_bid") or c3.get("cpa_bid") or 0.0)
            camp_active_adsets = [a for a in active_ads if str(a.get("campaign_id") or "") == camp_id]
            non_target_cost_adsets = (
                []
                if camp_cbo_bid > 0.0
                else [
                    a for a in camp_active_adsets
                    if float((a.get("row_data") or {}).get("cpa_bid") or a.get("cpa_bid") or 0.0) <= 0.0
                ]
            )
            if non_target_cost_adsets:
                try:
                    h_chk_cbo = dict(headers)
                    h_chk_cbo["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
                    det_cbo = (
                        session.get(
                            f"https://ads.tiktok.com/api/v3/i18n/perf/campaign/detail/?aadvid={aadvid}&campaign_id={camp_id}",
                            headers=h_chk_cbo,
                            timeout=12,
                        ).json().get("data")
                        or {}
                    )
                    if float(det_cbo.get("cpa_bid") or 0.0) > 0.0:
                        non_target_cost_adsets = []
                except Exception:
                    pass
            if non_target_cost_adsets:
                ok_mb_p, _ = pause_campaign(session, headers, aadvid, camp_id, operation="disable")
                if ok_mb_p:
                    total_campaigns_paused += 1
                    print(f"    🛑 [STRICT TARGET COST ENFORCEMENT] '{camp_name}' ({camp_id}) -> Paused immediately because it has NO Target Cost (cpa_bid=0 / Maximum Delivery)")
                continue
            c1 = camp_1d_map.get(camp_id, {})
            cy = camp_yest_map.get(camp_id, {})
            c2 = camp_2d_map.get(camp_id, {})
            c7 = camp_7d_map.get(camp_id, {})
            rd1 = c1.get("row_data") or {}
            rdy = cy.get("row_data") or {}
            rd2 = c2.get("row_data") or {}
            rd3 = c3.get("row_data") or {}
            rd7 = c7.get("row_data") or {}

            c_spend_1d = float(rd1.get("stat_cost") or 0.0)
            c_conv_1d = int(float(rd1.get("time_attr_convert_cnt") or 0))
            c_clicks_1d = int(float(rd1.get("click_cnt") or 0))
            c_spend_yest = float(rdy.get("stat_cost") or 0.0)
            c_conv_yest = int(float(rdy.get("time_attr_convert_cnt") or 0))
            c_clicks_yest = int(float(rdy.get("click_cnt") or 0))
            c_spend_2d = float(rd2.get("stat_cost") or 0.0)
            c_conv_2d = int(float(rd2.get("time_attr_convert_cnt") or 0))
            c_clicks_2d = int(float(rd2.get("click_cnt") or 0))
            c_spend_3d = float(rd3.get("stat_cost") or 0.0)
            c_conv_3d = int(float(rd3.get("time_attr_convert_cnt") or 0))
            c_clicks_3d = int(float(rd3.get("click_cnt") or 0))
            c_spend_7d = float(rd7.get("stat_cost") or c_spend_3d)
            c_conv_7d = int(float(rd7.get("time_attr_convert_cnt") or c_conv_3d))

            cn_low_sh = camp_name.strip().lower()
            utm_k1 = [camp_id.lower()] + ([cn_low_sh] if (c_spend_1d >= 0.50 and cn_max_spender_1d.get(cn_low_sh) == aadvid) else [])
            utm_k3 = [camp_id.lower()] + ([cn_low_sh] if (c_spend_3d >= 0.50 and cn_max_spender_3d.get(cn_low_sh) == aadvid) else [])
            sh_c1 = sum_sheet_utm_window(sheet_sales_by_day, utm_k1, today_str, today_str)
            sh_cy = sum_sheet_utm_window(sheet_sales_by_day, utm_k3, yesterday_str, yesterday_str)
            sh_c2 = sum_sheet_utm_window(sheet_sales_by_day, utm_k3, yesterday_str, today_str)
            sh_c3 = sum_sheet_utm_window(sheet_sales_by_day, utm_k3, two_days_ago_str, today_str)
            sh_c7 = sum_sheet_utm_window(sheet_sales_by_day, utm_k3, six_days_ago_str, today_str)
            c_conv_1d = max(c_conv_1d, sh_c1)
            c_conv_yest = max(c_conv_yest, sh_cy)
            c_conv_2d = max(c_conv_2d, sh_c2)
            c_conv_3d = max(c_conv_3d, sh_c3)
            c_conv_7d = max(c_conv_7d, sh_c7)
            c_cpc_1d = float(rd1.get("cpc") or 0.0)
            c_cpc_yest = float(rdy.get("cpc") or 0.0)
            c_cpc_2d = float(rd2.get("cpc") or 0.0)
            c_cpc_3d = float(rd3.get("cpc") or 0.0)
            c_cpc_7d = float(rd7.get("cpc") or 0.0)
            c_cpa_1d = round(c_spend_1d / c_conv_1d, 2) if c_conv_1d > 0 else None
            c_cpa_yest = round(c_spend_yest / c_conv_yest, 2) if c_conv_yest > 0 else None
            c_cpa_2d = round(c_spend_2d / c_conv_2d, 2) if c_conv_2d > 0 else None
            c_cpa_3d = round(c_spend_3d / c_conv_3d, 2) if c_conv_3d > 0 else None
            c_cpa_7d = round(c_spend_7d / c_conv_7d, 2) if c_conv_7d > 0 else None

            if c_spend_7d > 0 or c_spend_3d > 0 or c_spend_1d > 0:
                print(
                    f"  * Campaign '{camp_name}' (ID={camp_id}, status=ACTIVE) | "
                    f"Today: ${c_spend_1d:.2f} (CPC=${c_cpc_1d:.2f}, {c_conv_1d} sales) | "
                    f"2D: ${c_spend_2d:.2f} ({c_conv_2d} sales, CPA=${c_cpa_2d}) | "
                    f"3D: ${c_spend_3d:.2f} (CPC=${c_cpc_3d:.2f}, {c_conv_3d} sales, CPA=${c_cpa_3d}) | "
                    f"7D: ${c_spend_7d:.2f} ({c_conv_7d} sales, CPA=${c_cpa_7d})"
                )

            camp_is_gulf = is_gulf_campaign(camp_name)
            gulf_sim_cpa, gulf_camp_no_sale, _ = get_gulf_tiktok_sim_limits(camp_name) if camp_is_gulf else (GULF_MAX_VIDEO_CPA, GULF_MAX_NO_SALE_SPEND, 3.20)
            camp_no_sale_lim = gulf_camp_no_sale if camp_is_gulf else MAX_CAMPAIGN_NO_SALE_SPEND
            camp_cpa_lim = gulf_sim_cpa if camp_is_gulf else MAX_CAMPAIGN_CPA
            good_cpa_lim = gulf_sim_cpa if camp_is_gulf else 7.00
            if sh_c1 >= 1 and c_spend_1d > 0 and (c_spend_1d / sh_c1) <= good_cpa_lim:
                camp_sheet_protected_1d[camp_id] = sh_c1
            reg_no_sale_1d = gulf_camp_no_sale if camp_is_gulf else 5.00
            reg_no_sale_3d = (gulf_camp_no_sale + 2.50) if camp_is_gulf else MAX_REGULAR_CAMPAIGN_NO_SALE_3D
            reg_cpa_3d_lim = gulf_sim_cpa if camp_is_gulf else MAX_REGULAR_CAMPAIGN_CPA_3D

            is_cloned_camp = any(k in camp_name.lower() for k in ("copy", "scale", "boost", "turbo", "elite", "accel"))
            new_prod_key = extract_new_product_key_tt(camp_name)
            taager_prod_key = extract_taager_product_key_tt(camp_name)
            np_agg = global_new_products.get(new_prod_key) if new_prod_key else None
            camp_pause_reasons = []
            # STRICT USER RULES (2026-11-01):
            # 0) Taager Product Test Rule ("لو مجموع صرف منتجات تاجر في الحسابات مجموعة لكل منتج تعدى 25$ بدون ولا بيعة يعني المنتج فاشل ويطفي كل الحملات سواء غوغل او تيكتوك ولو وصل سعر البيعة فيهم 27$ يطفيه كل منتج على حدة"):
            #    Taager test campaigns are governed at the product level across Google Ads + TikTok combined ($25.00 0-sale stop / $27.00 CPA stop per product).
            # 1) New Product Rule: Any non-Taager new product spending >= $7.00 (single campaign OR combined across campaigns) with 0 sales -> Pause immediately!
            # 2) TikTok Campaign Stop-Loss Rule ("اهم شي حملة في تيكتوك صرفت 20$ بدون بيع يطفي او سعر البيعة 20$ بطفي"):
            #    Any regular TikTok campaign spending >= $20.00 with 0 sales OR reaching CPA >= $20.00 -> Pause immediately!
            #    For experimental [EXP-LEADS] campaigns, enforce a tighter micro-stop-loss ($8.50 with 0 sales) so experiments never waste the full $20.00.
            # 3) Rescue underserved clean videos (< $1.00 spend, CPC <= $0.25) ONLY for core products when count >= 3 (3 to 5 videos per campaign).
            is_exp_leads = "[exp-leads]" in camp_name.lower() or "exp leads" in camp_name.lower()
            if taager_prod_key:
                strict_no_sale_lim = 25.00
                strict_camp_cpa_lim = 27.00
                if taager_prod_key in (history.get("taager_failed_products") or {}):
                    camp_pause_reasons.append(f"[Taager Failed Product Lock '{taager_prod_key}'] المنتج فاشل وتجاوز سقف الاختبار ($25 بدون بيع أو $27 CPA) وتم إيقاف جميع حملاته")
            elif new_prod_key:
                strict_no_sale_lim = 7.00
                strict_camp_cpa_lim = 20.00
            elif camp_is_gulf:
                strict_no_sale_lim = gulf_camp_no_sale
                strict_camp_cpa_lim = gulf_sim_cpa
            elif is_exp_leads:
                strict_no_sale_lim = 8.50
                strict_camp_cpa_lim = 9.50
            else:
                strict_no_sale_lim = 20.00
                strict_camp_cpa_lim = 20.00

            # Early Campaign CPC Cut: only trigger at campaign level once spend >= $3.00 (so 3-5 video Smart+ campaigns allow per-video >= $1.00 & CPC > $0.25 pruning first)
            allow_multi_camp_chk = (not camp_is_gulf) or (c_spend_1d >= 0.25)
            gulf_3d_zero_stop = min(20.00, round(strict_no_sale_lim * 1.85, 2)) if c_spend_1d < 0.25 else strict_no_sale_lim
            if not taager_prod_key and c_spend_1d >= 3.00 and c_conv_1d == 0 and (c_cpc_1d > 0.35 or c_clicks_1d == 0):
                camp_pause_reasons.append(f"Campaign Early CPC Cut: Today Spend ${c_spend_1d:.2f} >= $3.00 with CPC ${c_cpc_1d:.2f} > $0.35 (Clicks={c_clicks_1d}) and 0 sales")
            if (c_spend_1d > 25.00 if taager_prod_key else c_spend_1d >= strict_no_sale_lim) and c_conv_1d == 0:
                camp_pause_reasons.append(f"{'[Taager Test >$25 Rule] ' if taager_prod_key else ('[Gulf Sim Stop-Loss] ' if camp_is_gulf else ('[New Product $7 Rule] ' if new_prod_key else ('[EXP-LEADS Micro Stop-Loss] ' if is_exp_leads else '')))}Today Spend ${c_spend_1d:.2f} {'>' if taager_prod_key else '>='} ${strict_no_sale_lim:.2f} with 0 sales")
            elif allow_multi_camp_chk and (c_spend_2d > 25.00 if taager_prod_key else c_spend_2d >= strict_no_sale_lim) and c_conv_2d == 0:
                camp_pause_reasons.append(f"{'[Taager Test >$25 Rule] ' if taager_prod_key else ('[Gulf Sim Stop-Loss] ' if camp_is_gulf else ('[New Product $7 Rule] ' if new_prod_key else ('[EXP-LEADS Micro Stop-Loss] ' if is_exp_leads else '')))}2-Day Spend ${c_spend_2d:.2f} {'>' if taager_prod_key else '>='} ${strict_no_sale_lim:.2f} with 0 sales")
            elif (c_spend_3d > 25.00 if taager_prod_key else c_spend_3d >= (gulf_3d_zero_stop if camp_is_gulf else strict_no_sale_lim)) and c_conv_3d == 0:
                camp_pause_reasons.append(f"{'[Taager Test >$25 Rule] ' if taager_prod_key else ('[Gulf Sim Stop-Loss] ' if camp_is_gulf else ('[New Product $7 Rule] ' if new_prod_key else ('[EXP-LEADS Micro Stop-Loss] ' if is_exp_leads else '')))}3-Day Spend ${c_spend_3d:.2f} {'>' if taager_prod_key else '>='} ${(gulf_3d_zero_stop if camp_is_gulf else strict_no_sale_lim):.2f} with 0 sales")
            elif allow_multi_camp_chk and (c_spend_7d > 25.00 if taager_prod_key else c_spend_7d >= strict_no_sale_lim) and c_conv_7d == 0:
                camp_pause_reasons.append(f"{'[Taager Test >$25 Rule] ' if taager_prod_key else ('[Gulf Sim Stop-Loss] ' if camp_is_gulf else ('[New Product $7 Rule] ' if new_prod_key else ('[EXP-LEADS Micro Stop-Loss] ' if is_exp_leads else '')))}7-Day Spend ${c_spend_7d:.2f} {'>' if taager_prod_key else '>='} ${strict_no_sale_lim:.2f} with 0 sales")
            elif np_agg and np_agg["spend_1d"] >= 7.00 and np_agg["conv_1d"] == 0:
                camp_pause_reasons.append(f"[New Product '{new_prod_key}' Combined $7 Rule] Combined Today Spend ${np_agg['spend_1d']:.2f} >= $7.00 with 0 sales")
            elif np_agg and np_agg["spend_2d"] >= 7.00 and np_agg["conv_2d"] == 0:
                camp_pause_reasons.append(f"[New Product '{new_prod_key}' Combined $7 Rule] Combined 2D Spend ${np_agg['spend_2d']:.2f} >= $7.00 with 0 sales")
            elif np_agg and np_agg["spend_3d"] >= 7.00 and np_agg["conv_3d"] == 0:
                camp_pause_reasons.append(f"[New Product '{new_prod_key}' Combined $7 Rule] Combined 3D Spend ${np_agg['spend_3d']:.2f} >= $7.00 with 0 sales")
            elif np_agg and np_agg["spend_7d"] >= 7.00 and np_agg["conv_7d"] == 0:
                camp_pause_reasons.append(f"[New Product '{new_prod_key}' Combined $7 Rule] Combined 7D Spend ${np_agg['spend_7d']:.2f} >= $7.00 with 0 sales")
            elif c_conv_1d > 0 and c_cpa_1d is not None and (c_cpa_1d > strict_camp_cpa_lim if camp_is_gulf else c_cpa_1d >= strict_camp_cpa_lim):
                camp_pause_reasons.append(f"Today CPA ${c_cpa_1d:.2f} {'>' if camp_is_gulf else '>='} ${strict_camp_cpa_lim:.2f} (Spend=${c_spend_1d:.2f}, Sales={c_conv_1d})")
            elif allow_multi_camp_chk and c_conv_2d > 0 and c_cpa_2d is not None and (c_cpa_2d > strict_camp_cpa_lim if camp_is_gulf else c_cpa_2d >= strict_camp_cpa_lim) and (c_conv_1d == 0 or (c_cpa_1d or 99) > strict_camp_cpa_lim):
                camp_pause_reasons.append(f"2-Day CPA ${c_cpa_2d:.2f} {'>' if camp_is_gulf else '>='} ${strict_camp_cpa_lim:.2f} (Spend=${c_spend_2d:.2f}, Sales={c_conv_2d})")
            elif allow_multi_camp_chk and c_conv_3d > 0 and c_cpa_3d is not None and (c_cpa_3d > strict_camp_cpa_lim if camp_is_gulf else c_cpa_3d >= strict_camp_cpa_lim) and (c_conv_1d == 0 or (c_cpa_1d or 99) > strict_camp_cpa_lim):
                camp_pause_reasons.append(f"3-Day CPA ${c_cpa_3d:.2f} {'>' if camp_is_gulf else '>='} ${strict_camp_cpa_lim:.2f} (Spend=${c_spend_3d:.2f}, Sales={c_conv_3d})")
            elif allow_multi_camp_chk and c_conv_7d > 0 and c_cpa_7d is not None and (c_cpa_7d > strict_camp_cpa_lim if camp_is_gulf else c_cpa_7d >= strict_camp_cpa_lim) and (c_conv_1d == 0 or (c_cpa_1d or 99) > strict_camp_cpa_lim):
                camp_pause_reasons.append(f"7-Day CPA ${c_cpa_7d:.2f} {'>' if camp_is_gulf else '>='} ${strict_camp_cpa_lim:.2f} (Spend=${c_spend_7d:.2f}, Sales={c_conv_7d})")

            if camp_pause_reasons and not is_cross_monitored_product_campaign(camp_name):
                c_reason_str = " | ".join(camp_pause_reasons)
                ok_cp, _ = pause_campaign(session, headers, aadvid, camp_id, operation="disable")
                if ok_cp:
                    total_campaigns_paused += 1
                    active_cid_set -= {str(a.get("creative_id")) for a in active_ads if str(a.get("campaign_id")) == camp_id}
                    if np_agg:
                        np_agg["active_campaigns"] = [
                            ac_item for ac_item in np_agg["active_campaigns"]
                            if not (ac_item["aadvid"] == aadvid and ac_item["campaign_id"] == camp_id)
                        ]
                    history["tiktok_campaigns"][f"{aadvid}:{camp_id}"] = {
                        "account_name": acc_name,
                        "aadvid": aadvid,
                        "campaign_id": camp_id,
                        "campaign_name": camp_name,
                        "reason": c_reason_str,
                        "spend_1d": c_spend_1d,
                        "conv_1d": c_conv_1d,
                        "spend_3d": c_spend_3d,
                        "conv_3d": c_conv_3d,
                        "paused_at": datetime.now(timezone.utc).isoformat(),
                    }
                    print(f"    🛑 [PAUSED CAMPAIGN] '{camp_name}' ({camp_id}) -> {c_reason_str}")
                    # Disabled auto-rescue campaign creation: stop-loss pause only
                    if False and not new_prod_key:
                        try:
                            c_ads_for_rescue = [str(a.get("creative_id")) for a in ads_7d if str(a.get("campaign_id")) == camp_id and a.get("creative_id")]
                            v_rescue_7d = fetch_expanded_materials(session, headers, aadvid, c_ads_for_rescue, six_days_ago_str, today_str) if c_ads_for_rescue else []
                            clean_vnames = set()
                            for vr in v_rescue_7d:
                                vrd = vr.get("row_data") or vr
                                v_sp = float(vrd.get("stat_cost") or 0.0)
                                v_cv = int(float(vrd.get("time_attr_convert_cnt") or 0))
                                v_clk = int(float(vrd.get("click_cnt") or 0))
                                v_cpc = float(vrd.get("cpc") or 0.0)
                                v_nm = (vr.get("main_entity_name") or vrd.get("main_entity_name") or "").strip()
                                if v_nm and v_sp < 1.00 and v_cv == 0 and (v_sp < 0.30 or (v_clk > 0 and v_cpc <= 0.25)):
                                    clean_vnames.add(v_nm)
                            if clean_vnames:
                                prod_key = "power_bank" if ("power" in camp_name.lower() or "pb" in camp_name.lower() or "voltrx" in camp_name.lower()) else "car_armrest"
                                rescue_bucket = history.setdefault("tiktok_pending_rescue_videos", {}).setdefault(f"{aadvid}:{prod_key}", {"template_cid": camp_id, "items": {}})
                                rescue_bucket["template_cid"] = camp_id
                                h_cp = dict(headers)
                                h_cp["Referer"] = f"https://ads.tiktok.com/i18n/creation/1nn/create/campaign?aadvid={aadvid}"
                                rc_snap = session.post(
                                    f"https://ads.tiktok.com/api/v4/i18n/creation/campaign_snap/copy/?aadvid={aadvid}",
                                    headers=h_cp,
                                    json={"campaign_id": str(camp_id), "name": "_tmp_rescue", "with_sketch": False, "with_ad": True, "with_creative": True, "risk_info": {}},
                                    timeout=15,
                                ).json().get("data") or {}
                                c_s = (rc_snap.get("new_campaign_snap_info_item") or {}).get("campaign_snap_id")
                                a_s_list = rc_snap.get("new_ad_snap_info_item_list") or []
                                a_s = a_s_list[0].get("ad_snap_id") if a_s_list else None
                                cr_map_vals = list((rc_snap.get("new_ad_and_creative_snap_info_item_map") or {}).values())
                                cr_s = (cr_map_vals[0][0].get("creative_snap_id") if isinstance(cr_map_vals[0][0], dict) else str(cr_map_vals[0][0])) if cr_map_vals and cr_map_vals[0] else None
                                if c_s and a_s and cr_s:
                                    rd_s = session.post(
                                        f"https://ads.tiktok.com/api/v4/i18n/creation/snap/detail/?aadvid={aadvid}",
                                        headers=h_cp,
                                        json={"campaign_snap_ids": [c_s], "ad_snap_ids": [a_s], "creative_snap_ids": [cr_s]},
                                        timeout=15,
                                    ).json().get("data") or {}
                                    cr_f = (rd_s.get("asset_group_creative_snap_map") or {}).get(cr_s) or {}
                                    for im_it in cr_f.get("image_list") or []:
                                        mn = (im_it.get("material_name") or (im_it.get("video_info") or {}).get("file_name") or "").strip()
                                        if mn in clean_vnames:
                                            rescue_bucket["items"][mn] = im_it
                                    pending_items = list(rescue_bucket["items"].values())
                                    if len(pending_items) >= 3:
                                        import uuid as _uuid
                                        batch_imgs = pending_items[:5]
                                        batch_names = list(rescue_bucket["items"].keys())[:5]
                                        c_f = (rd_s.get("campaign_snap_map") or {}).get(c_s) or {}
                                        a_f = (rd_s.get("ad_snap_map") or {}).get(a_s) or {}
                                        p_lbl = "power bank" if prod_key == "power_bank" else "car armrest"
                                        new_cname = f"{p_lbl} {today_str[8:10]}-{today_str[5:7]} Rescue Clean ({len(batch_imgs)} Ads)"
                                        c_f["campaign_name"] = new_cname
                                        c_f["budget"] = "50"
                                        c_f["cpa_bid"] = "6.5"
                                        c_f["campaign_bid_strategy_selection"] = 0
                                        session.post(f"https://ads.tiktok.com/api/v4/i18n/creation/campaign_snap/save/?aadvid={aadvid}", headers=h_cp, json={"campaign_sketch_form_data": c_f, "clear_lower_level_sketches": False, "with_sketch": False, "is_skip_check_fields": True, "risk_info": {}}, timeout=15)
                                        a_f["ad_name"] = f"{new_cname} - AdGroup"
                                        a_f["inventory_flow_type"] = 1
                                        a_f["inventory_flow"] = [3000]
                                        a_f["is_comment_public_disable"] = 1
                                        a_f["smart_bid_type"] = 0
                                        a_f["cpa_bid"] = "6.5"
                                        session.post(f"https://ads.tiktok.com/api/v4/i18n/creation/ad_snap/save/?aadvid={aadvid}", headers=h_cp, json={"ad_sketch_form_data": a_f, "campaign_id": "", "campaign_sketch_id": "", "campaign_snap_id": c_s, "clear_lower_level_sketches": False, "is_skip_check_fields": True, "spc_upgrade_mode": 1, "with_sketch": False, "risk_info": {}}, timeout=15)
                                        cr_f["creative_name"] = f"{new_cname} - Creative"
                                        cr_f["image_list"] = batch_imgs
                                        cr_f["auto_pull_by_destination_toggle"] = 0
                                        cr_f["auto_pull_by_aigc_toggle"] = 0
                                        cr_f["aigc_approval_auto_pull_toggle"] = 0
                                        cr_f["carousel_automation_toggle"] = 0
                                        session.post(f"https://ads.tiktok.com/api/v4/i18n/creation/creative_snap/save/?aadvid={aadvid}", headers=h_cp, json={"asset_group_sketch_form_data_list": [cr_f], "ad_id": "", "ad_sketch_id": "", "ad_snap_id": a_s, "campaign_id": "", "campaign_sketch_id": "", "campaign_snap_id": c_s, "with_sketch": False, "risk_info": {}}, timeout=15)
                                        pub_h = dict(h_cp)
                                        pub_h["x-ttam-deduptoken"] = str(_uuid.uuid4())
                                        session.post(f"https://ads.tiktok.com/api/v4/i18n/creation/async_creation/create_by_snap/?aadvid={aadvid}", headers=pub_h, json={"campaign_id": "", "campaign_snap_id": c_s, "ad_and_creative_snap_info_list": [{"ad_id": "", "ad_snap_id": a_s, "ad_sketch_id": "", "creative_snap_info_list": [{"creative_id": "", "creative_snap_id": cr_s, "creative_sketch_id": "", "need_publish": True}], "need_publish": True}], "coming_source_type": 0, "sketch_publish_source": 0, "campaign_sketch_id": "", "is_partial_publish": False, "create_cta_id": True, "risk_info": {}}, timeout=15)
                                        for bn in batch_names:
                                            rescue_bucket["items"].pop(bn, None)
                                        print(f"    ♻️ [AUTO-RESCUED {len(batch_imgs)} CLEAN VIDEOS] Launched '{new_cname}' with {batch_names}")
                                    else:
                                        print(f"    ⏳ [QUEUED {len(pending_items)}/3 CLEAN VIDEOS] Waiting for >=3 videos before creating rescue campaign: {list(rescue_bucket['items'].keys())}")
                        except Exception as _re_exc:
                            print(f"    [WARN] Rescue underserved videos check error: {_re_exc}")
                    continue

            nodup_spend_lim = GULF_MAX_NO_SALE_SPEND if camp_is_gulf else 7.0
            nodup_cpa_lim = GULF_MAX_VIDEO_CPA if camp_is_gulf else 7.0
            if (c_spend_3d >= nodup_spend_lim and c_conv_3d == 0) or (c_conv_3d > 0 and c_cpa_3d is not None and c_cpa_3d > nodup_cpa_lim):
                no_dup_camps[camp_id] = {
                    "name": camp_name,
                    "aadvid": aadvid,
                    "spend_3d": c_spend_3d,
                    "purchases_3d": c_conv_3d,
                    "cpa_3d": c_cpa_3d,
                    "reason": f"3-Day CPA > ${nodup_cpa_lim:.2f} — Duplication Blocked",
                }
            elif camp_id in no_dup_camps and c_conv_3d > 0 and c_cpa_3d is not None and c_cpa_3d <= nodup_cpa_lim:
                del no_dup_camps[camp_id]

            # Controlled Auto-Scale (+30% budget from 06:00 to 23:00 SA time, with strict cross-automation check & 3h cooldown)
            scale_cpa_1d_max = 10.50 if camp_is_gulf else 6.80
            scale_cpa_3d_max = 12.50 if camp_is_gulf else 8.00
            scale_bdg_cap = 80 if camp_is_gulf else 400
            scale_key = f"{now_sa.strftime('%Y-%m-%d')}:{aadvid}:{camp_id}"
            tt_master_ledger_path = BASE_DIR / "tiktok_master_actions_ledger.json"
            other_auto_recently_scaled = False
            try:
                if tt_master_ledger_path.exists():
                    tt_ml = json.loads(tt_master_ledger_path.read_text(encoding="utf-8"))
                    ml_sc = (tt_ml.get("scaled_campaigns") or {}).get(f"{aadvid}:{camp_id}") or {}
                    if ml_sc:
                        ml_cv = int(ml_sc.get("conv_today", 0))
                        ml_dt_str = str(ml_sc.get("scaled_at") or "")
                        if ml_cv >= c_conv_1d:
                            other_auto_recently_scaled = True
                        elif ml_dt_str:
                            ml_dt = datetime.strptime(ml_dt_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=3)))
                            if (now_sa - ml_dt).total_seconds() < 10800:
                                other_auto_recently_scaled = True
            except Exception:
                pass
            # أمر المستخدم الصارم (2026-11-03): تثبيت وتنزيل البدجت في تيك توك إلى $50.00 كحد أقصى ومنع التضخم التلقائي فوق $50:
            if not (os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("STOP_LOSS_PAUSE_ONLY") == "1"):
                try:
                    cur_bdg_chk = float(c1.get("budget") or c3.get("budget") or 0.0)
                    if cur_bdg_chk > 50.01:
                        h_b = dict(headers)
                        h_b["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
                        det_r = session.get(
                            f"https://ads.tiktok.com/api/v3/i18n/perf/campaign/detail/?aadvid={aadvid}&campaign_id={camp_id}",
                            headers=h_b,
                            timeout=15,
                        ).json().get("data") or {}
                        real_bdg = float(det_r.get("budget") or cur_bdg_chk)
                        if det_r and real_bdg > 50.01:
                            up_payload = {**det_r, "campaign_id": str(camp_id), "campaign_name": det_r.get("name") or camp_name, "budget": 50.0}
                            rb_resp = session.post(
                                f"https://ads.tiktok.com/api/v3/i18n/perf/campaign/update/?aadvid={aadvid}",
                                headers=h_b,
                                json=up_payload,
                                timeout=15,
                            )
                            if rb_resp.text.strip() and rb_resp.json().get("code") == 0:
                                print(f"    🔧 [BUDGET CAP $50] Campaign '{camp_name}' lowered: ${real_bdg:.0f} -> $50")
                except Exception:
                    pass
            if (
                False
                and not (os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("STOP_LOSS_PAUSE_ONLY") == "1")
                and not camp_is_gulf
                and is_before_6pm_sa
                and c_conv_1d >= 2
                and c_cpa_1d is not None
                and c_cpa_1d <= scale_cpa_1d_max
                and (c_cpa_3d is None or c_cpa_3d <= scale_cpa_3d_max)
                and scale_key not in tt_scaled_today
                and not other_auto_recently_scaled
            ):
                try:
                    h_b = dict(headers)
                    h_b["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
                    det_r = session.get(
                        f"https://ads.tiktok.com/api/v3/i18n/perf/campaign/detail/?aadvid={aadvid}&campaign_id={camp_id}",
                        headers=h_b,
                        timeout=15,
                    ).json().get("data") or {}
                    cur_bdg = float(det_r.get("budget") or c1.get("budget") or c3.get("budget") or 50.0)
                    if det_r and 20.0 <= cur_bdg < scale_bdg_cap:
                        new_bdg = min(scale_bdg_cap, int(round(cur_bdg * 1.30)))
                        up_payload = {**det_r, "campaign_id": str(camp_id), "campaign_name": det_r.get("name") or camp_name, "budget": float(new_bdg)}
                        rb_resp = session.post(
                            f"https://ads.tiktok.com/api/v3/i18n/perf/campaign/update/?aadvid={aadvid}",
                            headers=h_b,
                            json=up_payload,
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
                                f"    🚀 [AUTO-SCALE +30%] Campaign '{camp_name}' ({c_conv_1d} sales @ CPA=${c_cpa_1d}) -> Budget: ${cur_bdg:.0f} -> ${new_bdg}"
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
            if is_gulf_campaign(ad_camp_map.get(cid_v, ""), ad_name_map.get(cid_v, "")):
                # عزل فيديوهات الخليج 100% عن تجميع الفيديوهات المكررة للسعودية (يديرها gulf_2h_profit_and_ads_master.py)
                continue
            aid_v = str(base_m.get("ad_id") or "")
            camp_id_v = str(base_m.get("campaign_id") or "")
            sh_dup_1d = camp_sheet_protected_1d.get(camp_id_v, 0)
            clk1_v = int(float(m1_all.get("click_cnt") or 0))
            if (
                cv1_v == 0
                and sh_dup_1d >= 1
                and clk1_v > 0
                and round(sp1_v / clk1_v, 2) <= MAX_CPC_WITH_SALES
                and (sp1_v / sh_dup_1d) <= 7.00
            ):
                cv1_v = sh_dup_1d
                cv2_v += sh_dup_1d
                cv3_v += sh_dup_1d
                cv7_v += sh_dup_1d
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
        converting_videos_per_ad = {}
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
                cv_item = int(float(m_item.get("time_attr_convert_cnt") or 0)) + int(float(m1_item.get("time_attr_convert_cnt") or 0))
                if cv_item > 0:
                    converting_videos_per_ad[aid_str] = converting_videos_per_ad.get(aid_str, 0) + 1
            else:
                # Never consider a globally disabled duplicated video for starvation recovery
                if v_key_item in history.get("tiktok_disabled_dup_videos", {}):
                    continue
                sp3_i = float(m_item.get("stat_cost") or 0.0)
                sp7_i = float(m7_item.get("stat_cost") or sp3_i)
                cpc3_i = float(m_item.get("cpc") or 0.0)
                if sp3_i < MAX_NO_SALE_SPEND and sp7_i < MAX_NO_SALE_SPEND:
                    paused_candidates_per_ad.setdefault(aid_str, []).append((cpc3_i if cpc3_i > 0 else 9.9, sp3_i, m_item))

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
            cpc_7d = float(m7.get("cpc") or cpc_3d)
            clicks_7d = int(float(m7.get("click_cnt") or clicks_3d))
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

            v_is_gulf = is_gulf_campaign(ad_camp_map.get(cid, ""), ad_name_map.get(cid, ""))
            v_gulf_cpa, _, v_gulf_no_sale = get_gulf_tiktok_sim_limits(ad_camp_map.get(cid, ""), ad_name_map.get(cid, "")) if v_is_gulf else (GULF_MAX_VIDEO_CPA, GULF_MAX_NO_SALE_SPEND, GULF_MAX_NO_SALE_SPEND)
            v_gulf_max_cpc = get_gulf_tiktok_max_cpc(ad_camp_map.get(cid, ""), ad_name_map.get(cid, "")) if v_is_gulf else None
            v_is_new_test = (not v_is_gulf) and is_new_test_video_tt(ad_camp_map.get(cid, ""), m3.get("main_entity_name") or "")
            v_no_sale_lim = 4.00 if v_is_new_test else (v_gulf_no_sale if v_is_gulf else MAX_NO_SALE_SPEND)
            v_cpa_lim = 5.99 if v_is_new_test else (v_gulf_cpa if v_is_gulf else MAX_VIDEO_CPA)

            # Delayed-Attribution Winner Revival INSIDE ALREADY-ACTIVE Campaigns ONLY:
            # Prevents starving a profitable active campaign to '0 active videos remaining' ONLY when delayed conversions arrive TODAY (conv_1d >= 1).
            # Never revive a video with 0 sales Today just because it had 3D sales!
            # Stop-Loss Pause Only: Do not auto-re-enable paused videos
            if False and opt_status != "0" and cid in active_cid_set and not extract_new_product_key_tt(ad_camp_map.get(cid, "")):
                v_now_profitable = (conv_1d >= 1 and (spend_1d / conv_1d) <= v_cpa_lim)
                if v_now_profitable and not evaluate_video_rules(
                    spend_1d, conv_1d, cpc_1d, clicks_1d,
                    spend_2d, conv_2d, cpc_2d, clicks_2d,
                    spend_3d, conv_3d, cpc_3d, clicks_3d,
                    spend_7d, conv_7d, cpc_7d, clicks_7d,
                    is_gulf=v_is_gulf,
                    is_new_test=v_is_new_test,
                    gulf_no_sale_limit=v_gulf_no_sale,
                    gulf_cpa_limit=v_gulf_cpa,
                ):
                    ok_rev, _ = pause_smart_plus_video(session, headers, aadvid, ad_id, m3, operation="enable")
                    if ok_rev:
                        active_videos_per_ad[ad_id] = active_videos_per_ad.get(ad_id, 0) + 1
                        print(
                            f"    ♻️🔥 [REVIVED DELAYED-SALE WINNER IN ACTIVE CAMPAIGN] Video '{vid_name}' (draft={draft_str}) "
                            f"| Today=${spend_1d:.2f}/{conv_1d}cv, 3D=${spend_3d:.2f}/{conv_3d}cv — re-enabled so profitable campaign keeps its winning video!"
                        )
                        history["tiktok_videos"].pop(hist_key, None)
                        dup_blk = history.get("tiktok_disabled_dup_videos", {}).get(v_key_cur)
                        if dup_blk and int(dup_blk.get("conv_3d") or 0) == 0:
                            history["tiktok_disabled_dup_videos"].pop(v_key_cur, None)
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

            camp_id_for_v = str(m3.get("campaign_id") or "")
            sh_c1_for_v = camp_sheet_protected_1d.get(camp_id_for_v, 0)
            eff_v_conv_1d = conv_1d
            eff_v_conv_2d = conv_2d
            eff_v_conv_3d = conv_3d
            eff_v_conv_7d = conv_7d
            if (
                eff_v_conv_1d == 0
                and sh_c1_for_v >= 1
                and clicks_1d > 0
                and round(spend_1d / clicks_1d, 2) <= (MAX_CPC_WITH_SALES if (conv_3d >= 1 or sh_c1_for_v >= 2) else (v_gulf_max_cpc or MAX_CPC))
                and (spend_1d / sh_c1_for_v) <= (v_gulf_cpa if v_is_gulf else 7.00)
            ):
                eff_v_conv_1d = sh_c1_for_v
                eff_v_conv_2d = conv_2d + sh_c1_for_v
                eff_v_conv_3d = conv_3d + sh_c1_for_v
                eff_v_conv_7d = conv_7d + sh_c1_for_v

            reasons = evaluate_video_rules(
                spend_1d, eff_v_conv_1d, cpc_1d, clicks_1d,
                spend_2d, eff_v_conv_2d, cpc_2d, clicks_2d,
                spend_3d, eff_v_conv_3d, cpc_3d, clicks_3d,
                spend_7d, eff_v_conv_7d, cpc_7d, clicks_7d,
                is_gulf=v_is_gulf,
                is_new_test=v_is_new_test,
                gulf_no_sale_limit=v_gulf_no_sale,
                gulf_cpa_limit=v_gulf_cpa,
                gulf_max_cpc=v_gulf_max_cpc,
            )
            v_cpa_lim = 5.99 if v_is_new_test else (v_gulf_cpa if v_is_gulf else MAX_VIDEO_CPA)
            v_has_profitable_sales = (
                (eff_v_conv_3d > 0 and (spend_3d / eff_v_conv_3d) <= v_cpa_lim)
                or (eff_v_conv_1d > 0 and (spend_1d / eff_v_conv_1d) <= v_cpa_lim)
                or (eff_v_conv_7d > 0 and (spend_7d / eff_v_conv_7d) <= v_cpa_lim)
            )
            if v_key_cur in history.get("tiktok_disabled_dup_videos", {}) and not v_has_profitable_sales:
                reasons.append(f"Blacklisted Multi-Day Non-Converting Video ('{v_key_cur}')")

            if reasons:
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
                    # Permanently blacklist any video that accumulated >$6 (Saudi) or >$10 (Gulf) over 3D/7D with 0 sales
                    if (spend_3d > v_no_sale_lim and conv_3d == 0) or (spend_7d > v_no_sale_lim and conv_7d == 0):
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
                    # Only pause the campaign if ALL of its videos have now been paused (0 active videos left)
                    # SAFETY GUARD: Before pausing an entire campaign as empty, verify no paused video inside this ad_id actually has profitable delayed conversions!
                    if active_videos_per_ad.get(ad_id, 0) == 0:
                        camp_id_empty = str(m3.get("campaign_id") or "")
                        camp_name_empty = ad_camp_map.get(cid, "Campaign")
                        saved_by_winner = False
                        if False and not extract_new_product_key_tt(camp_name_empty):
                            for m_chk in active_mats_3d:
                                if str(m_chk.get("ad_id") or "") != ad_id:
                                    continue
                                k_chk = f"{m_chk.get('ad_id')}:{m_chk.get('ad_material_draft_id')}:{m_chk.get('main_entity_id')}"
                                m1_chk = map_1d.get(k_chk, {})
                                m2_chk = map_2d.get(k_chk, m_chk)
                                m7_chk = map_7d.get(k_chk, m_chk)
                                sp1_c = float(m1_chk.get("stat_cost") or 0.0)
                                cv1_c = int(float(m1_chk.get("time_attr_convert_cnt") or 0))
                                clk1_c = int(float(m1_chk.get("click_cnt") or 0))
                                cpc1_c = float(m1_chk.get("cpc") or 0.0)
                                sp2_c = float(m2_chk.get("stat_cost") or 0.0)
                                cv2_c = int(float(m2_chk.get("time_attr_convert_cnt") or 0))
                                clk2_c = int(float(m2_chk.get("click_cnt") or 0))
                                cpc2_c = float(m2_chk.get("cpc") or 0.0)
                                sp3_c = float(m_chk.get("stat_cost") or 0.0)
                                cv3_c = int(float(m_chk.get("time_attr_convert_cnt") or 0))
                                clk3_c = int(float(m_chk.get("click_cnt") or 0))
                                cpc3_c = float(m_chk.get("cpc") or 0.0)
                                sp7_c = float(m7_chk.get("stat_cost") or sp3_c)
                                cv7_c = int(float(m7_chk.get("time_attr_convert_cnt") or cv3_c))
                                clk7_c = int(float(m7_chk.get("click_cnt") or clk3_c))
                                cpc7_c = float(m7_chk.get("cpc") or cpc3_c)
                                chk_new_t = is_new_test_video_tt(camp_name_empty, m_chk.get("main_entity_name") or "")
                                chk_cpa_lim = 5.99 if chk_new_t else (GULF_MAX_VIDEO_CPA if v_is_gulf else MAX_VIDEO_CPA)
                                chk_prof = (cv1_c >= 1 and (sp1_c / cv1_c) <= chk_cpa_lim)
                                if chk_prof and not evaluate_video_rules(
                                    sp1_c, cv1_c, cpc1_c, clk1_c,
                                    sp2_c, cv2_c, cpc2_c, clk2_c,
                                    sp3_c, cv3_c, cpc3_c, clk3_c,
                                    sp7_c, cv7_c, cpc7_c, clk7_c,
                                    is_gulf=v_is_gulf,
                                    is_new_test=chk_new_t,
                                ):
                                    ok_sv, _ = pause_smart_plus_video(session, headers, aadvid, ad_id, m_chk, operation="enable")
                                    if ok_sv:
                                        active_videos_per_ad[ad_id] = active_videos_per_ad.get(ad_id, 0) + 1
                                        saved_by_winner = True
                                        print(f"    🛡️🔥 [PROTECTED PROFITABLE CAMPAIGN] Re-enabled delayed-sale winner '{m_chk.get('main_entity_name')}' instead of pausing '{camp_name_empty}'!")
                        if not saved_by_winner and camp_id_empty and not is_cross_monitored_product_campaign(camp_name_empty):
                            ok_ce, _ = pause_campaign(session, headers, aadvid, camp_id_empty, operation="disable")
                            if ok_ce:
                                total_campaigns_paused += 1
                                history["tiktok_campaigns"][f"{aadvid}:{camp_id_empty}"] = {
                                    "account_name": acc_name,
                                    "aadvid": aadvid,
                                    "campaign_id": camp_id_empty,
                                    "campaign_name": camp_name_empty,
                                    "reason": "All videos in Smart+ AdSet were paused individually (0 active videos remaining)",
                                    "spend_1d": spend_1d,
                                    "conv_1d": conv_1d,
                                    "spend_3d": spend_3d,
                                    "conv_3d": conv_3d,
                                    "paused_at": datetime.now(timezone.utc).isoformat(),
                                }
                                print(f"    🛑 [PAUSED EMPTY CAMPAIGN] '{camp_name_empty}' ({camp_id_empty}) -> 0 active videos remaining")
                else:
                    print(f"    [ERROR] Failed to pause video {draft_str}: {resp_txt}")

    # =========================================================================
    # PHASE 1.5: Cross-Campaign & Cross-Account NEW PRODUCT $7.00 Stop-Loss Rule
    # =========================================================================
    for np_key_g, np_grp in global_new_products.items():
        if not np_grp["active_campaigns"]:
            continue
        np_reason = None
        if np_grp["spend_1d"] >= 7.00 and np_grp["conv_1d"] == 0:
            np_reason = f"[New Product '{np_key_g}' Combined $7 Rule] Total Today Spend ${np_grp['spend_1d']:.2f} >= $7.00 with 0 sales"
        elif np_grp["spend_2d"] >= 7.00 and np_grp["conv_2d"] == 0:
            np_reason = f"[New Product '{np_key_g}' Combined $7 Rule] Total 2D Spend ${np_grp['spend_2d']:.2f} >= $7.00 with 0 sales"
        elif np_grp["spend_3d"] >= 7.00 and np_grp["conv_3d"] == 0:
            np_reason = f"[New Product '{np_key_g}' Combined $7 Rule] Total 3D Spend ${np_grp['spend_3d']:.2f} >= $7.00 with 0 sales"
        elif np_grp["spend_7d"] >= 7.00 and np_grp["conv_7d"] == 0:
            np_reason = f"[New Product '{np_key_g}' Combined $7 Rule] Total 7D Spend ${np_grp['spend_7d']:.2f} >= $7.00 with 0 sales"
        if np_reason:
            for ac_np in list(np_grp["active_campaigns"]):
                ok_np_g, _ = pause_campaign(session, headers, ac_np["aadvid"], ac_np["campaign_id"], operation="disable")
                if ok_np_g:
                    total_campaigns_paused += 1
                    history["tiktok_campaigns"][f"{ac_np['aadvid']}:{ac_np['campaign_id']}"] = {
                        "account_name": ac_np["acc_name"],
                        "aadvid": ac_np["aadvid"],
                        "campaign_id": ac_np["campaign_id"],
                        "campaign_name": ac_np["campaign_name"],
                        "reason": np_reason,
                        "spend_1d": ac_np["spend_1d"],
                        "conv_1d": ac_np["conv_1d"],
                        "spend_3d": ac_np["spend_3d"],
                        "conv_3d": ac_np["conv_3d"],
                        "paused_at": datetime.now(timezone.utc).isoformat(),
                    }
                    print(f"  🛑 [NEW PRODUCT $7 STOP-LOSS] [{ac_np['acc_name']}] '{ac_np['campaign_name']}' ({ac_np['campaign_id']}) -> {np_reason}")

    # =========================================================================
    # PHASE 2: Cross-Campaign & Cross-Account Duplicated Video Aggregation Rule
    # - Applies to any duplicated video (present in >= 2 AdSets across campaigns/accounts):
    #   1) Total Spend >= $10.00 with 0 sales -> Pause ALL active copies permanently
    #   2) Total Sales >= 1 and Total CPA >= $12.00 ($13.00 for Gulf copies) -> Pause non-profitable copies
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

        all_insts_gulf = bool(grp["instances"]) and all(is_gulf_campaign(inst["campaign_name"]) for inst in grp["instances"])
        dup_reasons = evaluate_dup_video_rules(
            tot_sp1, tot_cv1, tot_sp2, tot_cv2, tot_sp3, tot_cv3, tot_sp7, tot_cv7, all_gulf=all_insts_gulf
        )

        # Permanent Lock: Only applies to 0-sale blacklisted videos; profitable individual copies are always protected!
        if v_key in history.get("tiktok_disabled_dup_videos", {}) and not dup_reasons and tot_cv3 == 0 and tot_cv7 == 0:
            dup_reasons = [
                f"[Permanent Global Dup Video Lock] Video '{v_key}' was permanently disabled across all AdSets and is NEVER allowed to run again"
            ]

        if not dup_reasons:
            continue

        active_insts = [
            inst for inst in grp["instances"]
            if inst["is_active"]
            and not (
                (tot_cv3 > 0 or tot_cv7 > 0)
                and inst["spend_7d"] < 1.00
                and inst["spend_3d"] < 1.00
                and inst["spend_1d"] < 1.00
            )
            and not (
                inst["conv_3d"] > 0
                and (inst["spend_3d"] / inst["conv_3d"]) <= (GULF_MAX_VIDEO_CPA if is_gulf_campaign(inst["campaign_name"]) else (9.99 if inst["conv_3d"] >= 3 else MAX_VIDEO_CPA))
            )
            and not (
                inst["conv_1d"] > 0
                and (inst["spend_1d"] / inst["conv_1d"]) <= (GULF_MAX_VIDEO_CPA if is_gulf_campaign(inst["campaign_name"]) else (9.99 if inst["conv_1d"] >= 3 else MAX_VIDEO_CPA))
            )
            and not (
                inst["conv_7d"] >= 3
                and (inst["spend_7d"] / inst["conv_7d"]) <= (GULF_MAX_VIDEO_CPA if is_gulf_campaign(inst["campaign_name"]) else 9.99)
            )
        ]
        if not active_insts:
            if tot_cv3 == 0 and tot_cv7 == 0 and v_key not in history.get("tiktok_disabled_dup_videos", {}):
                history["tiktok_disabled_dup_videos"][v_key] = {
                    "v_key": v_key,
                    "sample_name": grp["sample_name"],
                    "accounts": sorted(grp["accounts"]),
                    "total_copies": len(grp["ad_ids"]),
                    "closed_copies": 0,
                    "spend_1d": tot_sp1,
                    "conv_1d": tot_cv1,
                    "spend_2d": tot_sp2,
                    "conv_2d": tot_cv2,
                    "spend_3d": tot_sp3,
                    "conv_3d": tot_cv3,
                    "spend_7d": tot_sp7,
                    "conv_7d": tot_cv7,
                    "reason": " | ".join(dup_reasons),
                    "disabled_at": datetime.now(timezone.utc).isoformat(),
                }
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

        if tot_cv3 == 0 and tot_cv7 == 0:
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
    try:
        from tiktok_spammer_audience_manager import enforce_spammer_audience_exclusion
        enforce_spammer_audience_exclusion(session, headers, force_all=False)
    except Exception as _spam_err:
        print(f"ℹ️ ملاحظة استبعاد جمهور السبامرز في tiktok_monitor: {_spam_err}")
    print(
        f"\nDone! Total TikTok Campaigns paused: {total_campaigns_paused} | "
        f"Smart+ videos paused: {total_paused} (Global Dup Video kills: {total_dup_videos_paused}) | "
        f"Auto-Revived (delayed sales): {total_revived}"
    )
    if os.environ.get("GITHUB_ACTIONS") != "true":
        try:
            from github_deploy import sync_state_and_rules_to_github
            sync_state_and_rules_to_github("tiktok_monitor")
        except Exception as _sync_err:
            print(f"ℹ️ ملاحظة مزامنة GitHub من tiktok_monitor: {_sync_err}")


if __name__ == "__main__":
    main()
