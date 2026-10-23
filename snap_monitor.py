from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request
from snap_auth import API_BASE, api_get, load_config, refresh_access_token
from duplicate_gulf_campaigns import clone_adsquad_exact, make_session

sys.stdout.reconfigure(encoding="utf-8")

TARGET_ACCOUNT_ID = "350cb560-ad77-4fb5-9bbf-4c090c9ad081"  # CSP9860
HISTORY_FILE = Path(__file__).parent / "paused_history.json"
ACCOUNT_TZ = timezone(timedelta(hours=3))  # GMT+3 (Saudi/Qatar time)

# الكلمات المفتاحية لدول (عمان، قطر، البحرين، الكويت) بالإنجليزية والعربية والاختصارات الشائعة في حسابك
GULF_HIGHER_CPC_KEYWORDS = (
    "oman", "qatar", "qat", "bahrain", "kuwait", "kuwiat", "kwt",
    "عمان", "قطر", "البحرين", "بحرين", "كويت"
)
GULF_COUNTRY_CODES = {"qa", "om", "bh", "kw"}


def get_squad_countries(squad: dict | None) -> set[str]:
    if not squad:
        return set()
    geos = squad.get("targeting", {}).get("geos", [])
    return {str(g.get("country_code", "")).lower() for g in geos if g.get("country_code")}


def is_saudi_squad(squad: dict | None, campaign_name: str = "", adsquad_name: str = "") -> bool:
    countries = get_squad_countries(squad)
    if countries:
        return "sa" in countries
    return not is_gulf_034_campaign(campaign_name, adsquad_name)


def is_gulf_034_campaign(campaign_name: str, adsquad_name: str = "", squad: dict | None = None) -> bool:
    countries = get_squad_countries(squad)
    if countries & GULF_COUNTRY_CODES:
        return True
    combined = f"{campaign_name} {adsquad_name}".lower()
    return any(k in combined for k in GULF_HIGHER_CPC_KEYWORDS)


def load_paused_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8-sig"))
            data.setdefault("adsquads", {})
            data.setdefault("ads", {})
            data.setdefault("bid_bumps", {})
            data.setdefault("night_bid_reductions", {})
            data.setdefault("creative_media_map", {})
            data.setdefault("disabled_videos", {})
            return data
        except Exception:
            pass
    return {
        "adsquads": {},
        "ads": {},
        "bid_bumps": {},
        "night_bid_reductions": {},
        "creative_media_map": {},
        "disabled_videos": {},
    }


def parse_iso_utc(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def save_paused_history(history: dict):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def api_put(endpoint: str, token: str, payload: dict) -> dict:
    url = f"{API_BASE}{endpoint}" if endpoint.startswith("/") else endpoint
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))


def api_get_paginated(endpoint: str, token: str, list_key: str, item_key: str, max_pages: int = 30) -> list[dict]:
    items = []
    next_url = endpoint
    for _ in range(max_pages):
        if not next_url:
            break
        resp = api_get(next_url, token)
        for wrap in resp.get(list_key, []):
            obj = wrap.get(item_key)
            if obj:
                items.append(obj)
        next_url = (resp.get("paging") or {}).get("next_link")
    return items


def fetch_account_breakdown_stats(token: str, ad_account_id: str, breakdown: str, days_back: int = 0) -> list[dict]:
    now_local = datetime.now(ACCOUNT_TZ)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = today_start - timedelta(days=days_back)
    end_local = today_start + timedelta(days=1)

    start_str = start_local.isoformat()
    end_str = end_local.isoformat()
    fields = "impressions,swipes,spend,conversion_purchases"
    url = (
        f"/adaccounts/{ad_account_id}/stats?granularity=TOTAL"
        f"&start_time={urllib.parse.quote(start_str)}"
        f"&end_time={urllib.parse.quote(end_str)}"
        f"&breakdown={breakdown}&fields={fields}"
        f"&swipe_up_attribution_window=1_DAY&view_attribution_window=1_DAY"
    )
    resp = api_get(url, token)
    breakdown_list = (
        resp.get("total_stats", [{}])[0]
        .get("total_stat", {})
        .get("breakdown_stats", {})
        .get(breakdown, [])
    )
    results = []
    for item in breakdown_list:
        st = item.get("stats", {})
        spend = round(st.get("spend", 0) / 1_000_000, 2)
        if spend <= 0:
            continue
        swipes = st.get("swipes", 0)
        purchases = st.get("conversion_purchases", 0)
        cpa = round(spend / purchases, 2) if purchases > 0 else None
        cpc = round(spend / swipes, 3) if swipes > 0 else spend
        results.append({
            "id": item["id"],
            "spend": spend,
            "impressions": st.get("impressions", 0),
            "swipes": swipes,
            "purchases": purchases,
            "cpa": cpa,
            "cpc": cpc,
        })
    return results


def evaluate_adsquad_rule(adsquad_name: str, st: dict, campaign_name: str = "", squad: dict | None = None) -> tuple[bool, str]:
    is_copy = "copy" in adsquad_name.lower()
    spend = st["spend"]
    purchases = st["purchases"]
    cpa = st["cpa"]
    cpc = st.get("cpc", 0.0)
    swipes = st.get("swipes", 0)
    is_gulf = is_gulf_034_campaign(campaign_name, adsquad_name, squad)
    no_sale_lim = 8.0 if is_gulf else 4.50
    max_cpa_lim = 12.0 if is_gulf else 7.0

    # قاعدة صارمة بأمر المستخدم:
    # أي AdSquad لا يوجد في اسمها كلمة "Copy" لا يتم إطفاؤها أبداً كـ AdSquad (لأنها تحتوي على فيديوهات جديدة يجب اختبارها كلها)،
    # وإنما يتم إطفاء الفيديوهات الفردية بداخلها فقط.
    if not is_copy:
        return False, ""

    # المجموعات المستنسخة (التي تحتوي على Copy في اسمها):
    # فرملة النقرة المبكرة للمجموعات المستنسخة (Copy) لمنع استنزاف $4.50 في جمهور غالٍ:
    early_cpc_lim = 0.28 if is_gulf else 0.165
    if spend >= 1.50 and purchases == 0 and (swipes == 0 or cpc > early_cpc_lim):
        return True, f"[Copy Early CPC Brake] صرفت ${spend} (>= $1.50) بدون مبيعات وسعر النقرة CPC=${cpc} (> ${early_cpc_lim})"
    if spend >= no_sale_lim and purchases == 0:
        return True, f"[Copy Rule] صرفت ${spend} (>= ${no_sale_lim:.2f}) بدون مبيعات"
    if purchases > 0 and cpa is not None and cpa > max_cpa_lim:
        return True, f"[Copy Rule] سعر البيعة CPA = ${cpa} (> ${max_cpa_lim:.2f}) | الصرف: ${spend} | المبيعات: {purchases}"

    return False, ""


def evaluate_single_ad_rule(st: dict, campaign_name: str, adsquad_name: str, squad: dict | None = None) -> tuple[bool, str]:
    spend = st["spend"]
    swipes = st["swipes"]
    purchases = st["purchases"]
    cpa = st.get("cpa")
    cpc = st["cpc"]
    is_copy = "copy" in (adsquad_name or "").lower()
    is_cross_prod = is_cross_monitored_product_campaign(campaign_name, adsquad_name)
    is_gulf = is_gulf_034_campaign(campaign_name, adsquad_name, squad)
    max_cpc = 0.30 if is_cross_prod else (0.35 if is_gulf else 0.28)
    min_cpc_spend = 0.80 if is_cross_prod else 1.0
    max_cpa = 12.0 if is_gulf else 7.0
    no_sale_stop = 10.0 if is_gulf else 5.0

    # في حملات مايباخ و TIME X: إذا جاب الفيديو بيعة نخليه وما نطفيه بسبب الـ CPC ("الا في حالة جاب بيعة يخليه")
    if is_cross_prod and purchases >= 1:
        return False, ""

    # إذا حقق الفيديو مبيعات فعلية وكان سعر البيعة مربحاً (<= $7.00 في السعودية أو <= $12.00 في الخليج)، لا نوقفه بسبب الـ CPC
    if purchases > 0 and cpa is not None and cpa <= max_cpa:
        return False, ""

    # 1. إيقاف الفيديو إذا تخطى CPA الحد الأقصى ($7 للسعودية أو $12 للخليج)
    if purchases > 0 and cpa is not None and cpa > max_cpa:
        return True, f"[Single Video Max ${max_cpa:.0f} CPA Rule] صرف الفيديو ${spend} | المبيعات: {purchases} | CPA = ${cpa} (> ${max_cpa:.2f})"

    # 2. قاعدة إيقاف الفيديو إذا صرف >= $5 للسعودية أو >= $10 للخليج بدون مبيعة داخل المجموعة
    if not is_cross_prod and spend >= no_sale_stop and purchases == 0:
        return True, f"[Single Video ${no_sale_stop:.0f} Stop-Loss] صرف الفيديو ${spend} (>= ${no_sale_stop:.2f}) بدون مبيعة"

    # 2B. فلتر النقرة المبكر المتوازن داخل نسخ الـ Copy (عند $2.50 بدلاً من $1.50 لمنع قتل المجموعة قبل إتمام البيع)
    if is_copy and not is_cross_prod and spend >= 2.50 and purchases == 0:
        copy_early_cpc = 0.35 if is_gulf else 0.24
        if swipes == 0 or cpc > copy_early_cpc:
            return True, f"[Copy Early High-CPC Cut] نسخة Copy دخلت جمهوراً غالياً: صرفت ${spend} (>= $2.50) بدون مبيعات بـ CPC=${cpc} (> ${copy_early_cpc})"

    # 3. قاعدة إيقاف الـ CPC المرتفع بعد صرف >= 0.80$ لحملتي مايباخ و TIME X (أو >= 1.00$ لباقي الحملات) بدون مبيعات
    if spend >= min_cpc_spend:
        if swipes == 0:
            return True, f"[Ad CPC Rule] صرف الفيديو ${spend} (>= ${min_cpc_spend:.2f}) بدون أي نقرة (الحد الأقصى ${max_cpc:.2f})"
        if cpc > max_cpc:
            return True, f"[Ad CPC Rule] صرف الفيديو ${spend} (>= ${min_cpc_spend:.2f}) | النقرات: {swipes} | سعر النقرة CPC = ${cpc} (> ${max_cpc:.2f})"
    return False, ""


def resolve_video_media_id(creative_id: str, token: str, history: dict) -> str:
    if not creative_id:
        return "unknown"
    cmap = history.setdefault("creative_media_map", {})
    if creative_id in cmap:
        return cmap[creative_id]
    try:
        cr_resp = api_get(f"/creatives/{creative_id}", token)
        cr = cr_resp.get("creatives", [{}])[0].get("creative", {})
        media_id = cr.get("top_snap_media_id") or creative_id
        cmap[creative_id] = media_id
        return media_id
    except Exception:
        return creative_id


def is_cross_monitored_product_campaign(campaign_name: str, adsquad_name: str = "") -> bool:
    combined = f"{campaign_name} {adsquad_name}".lower()
    return any(k in combined for k in ("مايباخ", "maybach", "time x", "timex", "تايم إكس", "دفاتر", "مسخن الرضاعات", "مسخن رضاعات", "warmer", "bottle warmer"))


def run_automation(token: str, ad_account_id: str, dry_run: bool = False):
    history = load_paused_history()
    now_str = datetime.now(ACCOUNT_TZ).strftime("%Y-%m-%d %H:%M GMT+3")
    clone_session = None

    all_campaigns_list = api_get_paginated(f"/adaccounts/{ad_account_id}/campaigns?limit=200", token, "campaigns", "campaign")
    camp_names = {}
    active_campaigns = []
    for c in all_campaigns_list:
        camp_names[c["id"]] = c.get("name", "")
        if c.get("status") == "ACTIVE":
            if is_cross_monitored_product_campaign(c.get("name", "")):
                if not dry_run:
                    c_off = dict(c)
                    c_off["status"] = "PAUSED"
                    api_put(f"/adaccounts/{ad_account_id}/campaigns", token, {"campaigns": [c_off]})
                    print(f"🛑 [إيقاف دائم للمنتجات الجديدة] تم إيقاف حملة '{c.get('name', '')}' ({c['id']}) بأمر المستخدم.")
                continue
            active_campaigns.append(c)

    squad_cache = {}
    now_local_dt = datetime.now(ACCOUNT_TZ)
    today_d_str = now_local_dt.strftime("%Y-%m-%d")
    yesterday_d_str = (now_local_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    active_camp_ids = {c["id"] for c in active_campaigns}

    # 1. One API call for all Ad Squad stats today + 2-day + 3-day + 7-day stats (and 3-day campaign stats to block duplication in >$7 CPA campaigns)
    squad_stats = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=0)
    squad_stats_map = {st["id"]: st for st in squad_stats}
    squad_stats_2d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=1)
    squad_stats_2d_map = {st["id"]: st for st in squad_stats_2d}
    squad_stats_3d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=2)
    squad_stats_3d_map = {st["id"]: st for st in squad_stats_3d}
    squad_stats_7d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=6)
    squad_stats_7d_map = {st["id"]: st for st in squad_stats_7d}
    camp_stats_3d = fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=2)
    camp_stats_3d_map = {st["id"]: st for st in camp_stats_3d}

    no_dup_camps = history.setdefault("no_duplicate_campaigns", {})
    for cid in list(no_dup_camps.keys()):
        cname_chk = camp_names.get(cid, "") or no_dup_camps[cid].get("name", "")
        if is_cross_monitored_product_campaign(cname_chk):
            continue
        is_gulf_c = is_gulf_034_campaign(cname_chk)
        cpa_lim_c = 12.0 if is_gulf_c else 7.0
        sp_lim_c = 10.0
        cst_chk = camp_stats_3d_map.get(cid)
        if not cst_chk:
            del no_dup_camps[cid]
        elif cst_chk.get("purchases", 0) > 0 and cst_chk.get("cpa") is not None and cst_chk["cpa"] <= cpa_lim_c:
            del no_dup_camps[cid]
        elif cst_chk.get("purchases", 0) == 0 and cst_chk.get("spend", 0) < sp_lim_c:
            del no_dup_camps[cid]
    for cid, cname_val in camp_names.items():
        if is_cross_monitored_product_campaign(cname_val):
            no_dup_camps[cid] = {
                "name": cname_val,
                "reason": "Cross-Platform Monitored Product Campaign (1 AdSquad Only)",
                "updated_at": now_str,
            }
    for cid, cst in camp_stats_3d_map.items():
        cname_c = camp_names.get(cid, cid)
        is_gulf_c = is_gulf_034_campaign(cname_c)
        cpa_lim_c = 12.0 if is_gulf_c else 7.0
        sp_lim_c = 10.0
        if (cst["spend"] >= sp_lim_c and cst["purchases"] == 0) or (cst["purchases"] > 0 and cst["cpa"] is not None and cst["cpa"] > cpa_lim_c):
            no_dup_camps[cid] = {
                "name": cname_c,
                "spend_3d": cst["spend"],
                "purchases_3d": cst["purchases"],
                "cpa_3d": cst["cpa"],
                "updated_at": now_str,
            }
    save_paused_history(history)

    spend_by_squad = {st["id"]: st["spend"] for st in squad_stats}
    print(f"📊 عدد المجموعات الإعلانية (Ad Squads) التي صرفت اليوم: {len(squad_stats)} (خلال 3 أيام: {len(squad_stats_3d)})")

    camp_squads_cache = {}
    for c in active_campaigns:
        c_id = c["id"]
        try:
            c_squads = api_get_paginated(f"/campaigns/{c_id}/adsquads?limit=200", token, "adsquads", "adsquad")
            camp_squads_cache[c_id] = c_squads
            for sq in c_squads:
                squad_cache[sq["id"]] = sq
        except Exception:
            camp_squads_cache[c_id] = []

    # جلب إحصائيات وإعلانات الفيديوهات مسبقاً لحماية أي مجموعة Copy تحتوي بداخلها على فيديو رابح بكوست ممتاز (<= $8.50) من الإطفاء بسبب الفيديوهات الأخرى!
    ad_stats = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=0)
    ad_stats_map = {st["id"]: st for st in ad_stats}
    ad_stats_2d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=1)
    ad_stats_2d_map = {st["id"]: st for st in ad_stats_2d}
    ad_stats_3d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=2)
    ad_stats_3d_map = {st["id"]: st for st in ad_stats_3d}
    ad_stats_7d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=6)
    ad_stats_7d_map = {st["id"]: st for st in ad_stats_7d}

    def clean_base_ad_name(raw_nm: str) -> str:
        s = (raw_nm or "").strip()
        while s.lower().startswith("copy of "):
            s = s[8:].strip()
        s = re.sub(r"\s*\[H\d+\].*$", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"_hl\d+$", "", s, flags=re.IGNORECASE).strip()
        return s

    ad_cache = {}
    camp_ads_cache = {}
    ads_by_squad_cache = {}
    all_account_ads = []
    for c in active_campaigns:
        c_id = c["id"]
        try:
            c_ads = api_get_paginated(f"/campaigns/{c_id}/ads?limit=500", token, "ads", "ad")
            camp_ads_cache[c_id] = c_ads
            for a in c_ads:
                ad_cache[a["id"]] = a
                all_account_ads.append(a)
                ads_by_squad_cache.setdefault(a.get("ad_squad_id"), []).append(a)
        except Exception:
            camp_ads_cache[c_id] = []

    # 1A. مراجعة المجموعات التي أطفأها البوت مسبقاً:
    # - أي مجموعة لا يوجد في اسمها "Copy" يعيد البوت تشغيلها فوراً (ما دامت تحتوي على فيديوهات نشطة) لأن القاعدة هي إطفاء الفيديوهات داخلها فقط وليس المجموعة.
    # - وأي مجموعة Copy ظهرت لها مبيعة متأخرة وانخفض سعر المبيعة (CPA) تحت الحد المسموح يعيد البوت تشغيلها فوراً!
    for s_id, info in list(history["adsquads"].items()):
        squad = squad_cache.get(s_id, {})
        if not squad:
            continue
        c_id = squad.get("campaign_id", "")
        if c_id not in active_camp_ids:
            continue
        s_name = squad.get("name", info.get("name", ""))
        c_name = camp_names.get(c_id, info.get("campaign", ""))

        is_consolidated_pause = "[Consolidation" in str(info.get("reason", ""))
        if "copy" not in s_name.lower() and not is_consolidated_pause:
            has_no_active_ads = "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" in (squad.get("delivery_status") or [])
            if not has_no_active_ads and squad.get("status") == "PAUSED":
                if dry_run:
                    print(f"🔍 [إعادة تفعيل أدست بدون Copy لاختبار فيديوهاتها] المجموعة: '{s_name}' (حملة: '{c_name}')")
                else:
                    squad["status"] = "ACTIVE"
                    api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                    del history["adsquads"][s_id]
                    save_paused_history(history)
                    print(f"♻️✅ [حماية الأدست بدون Copy] تم إعادة تفعيل المجموعة: '{s_name}' (حملة: '{c_name}') ليتم إطفاء الفيديوهات الخاسرة بداخلها فقط!")
                    time.sleep(0.3)
            elif squad.get("status") == "ACTIVE" and not dry_run:
                del history["adsquads"][s_id]
                save_paused_history(history)
            continue

        paused_at_str = str(info.get("paused_at", ""))
        is_recent_pause = (today_d_str in paused_at_str) or (yesterday_d_str in paused_at_str)
        st1_chk = squad_stats_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None})
        st2_chk = squad_stats_2d_map.get(s_id)
        st3_chk = squad_stats_3d_map.get(s_id)

        # إذا نزفت المجموعة اليوم >= $5 بدون مبيعات، لا نعيد تشغيلها اليوم
        if st1_chk.get("spend", 0.0) >= 5.0 and st1_chk.get("purchases", 0) == 0:
            continue

        st_check = None
        if st1_chk.get("purchases", 0) > 0:
            st_check = st1_chk
        elif not is_consolidated_pause and is_recent_pause and st2_chk and st2_chk.get("purchases", 0) > 0:
            st_check = st2_chk
        elif not is_consolidated_pause and is_recent_pause and st3_chk and st3_chk.get("purchases", 0) > 0:
            st_check = st3_chk
        else:
            continue

        should_pause, _ = evaluate_adsquad_rule(s_name, st_check, c_name, squad)
        should_pause_3d = False
        if st3_chk and st3_chk.get("spend", 0.0) >= 5.0:
            should_pause_3d, _ = evaluate_adsquad_rule(s_name, st3_chk, c_name, squad)
        if not should_pause and not should_pause_3d:
            if squad.get("status") == "PAUSED":
                if dry_run:
                    print(
                        f"🔍 [إعادة تشغيل تلقائي — مبيعة متأخرة] المجموعة: '{s_name}' (حملة: '{c_name}') "
                        f"| الصرف: ${st_check['spend']} | المبيعات: {st_check['purchases']} | CPA = ${st_check['cpa']}"
                    )
                else:
                    squad["status"] = "ACTIVE"
                    api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                    del history["adsquads"][s_id]
                    save_paused_history(history)
                    print(
                        f"♻️🔥 [إعادة تشغيل تلقائي — مبيعة متأخرة] تم إعادة تشغيل المجموعة: '{s_name}' (حملة: '{c_name}') "
                        f"بعد ظهور مبيعات ({st_check['purchases']}) وانخفاض سعر المبيعة إلى CPA = ${st_check['cpa']} (الصرف: ${st_check['spend']})!"
                    )
                    time.sleep(0.3)
            elif squad.get("status") == "ACTIVE" and not dry_run:
                del history["adsquads"][s_id]
                save_paused_history(history)

    all_candidate_sids = (
        set(squad_stats_map.keys())
        | set(squad_stats_2d_map.keys())
        | set(squad_stats_3d_map.keys())
        | set(squad_stats_7d_map.keys())
    )
    for s_id in all_candidate_sids:
        squad = squad_cache.get(s_id)
        if not squad or squad.get("status") != "ACTIVE":
            continue
        st_1d = squad_stats_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st_2d = squad_stats_2d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st_3d = squad_stats_3d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st_7d = squad_stats_7d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        if max(st_1d["spend"], st_2d["spend"], st_3d["spend"], st_7d["spend"]) < 1.50:
            continue

        s_name = squad.get("name", "")
        c_id = squad.get("campaign_id", "")
        if c_id and c_id not in camp_names:
            c_resp = api_get(f"/campaigns/{c_id}", token)
            camp_names[c_id] = c_resp.get("campaigns", [{}])[0].get("campaign", {}).get("name", "")
        c_name = camp_names.get(c_id, "")

        if s_id in history["adsquads"]:
            print(f"⏭️ تجاهل المجموعة '{s_name}' — أعدتَ تشغيلها يدوياً (فرصة أخرى).")
            continue

        if is_cross_monitored_product_campaign(c_name, s_name):
            # هذه الحملات (وسادة مايباخ ودفاتر TIME X) يراقبها cross_product_monitor.py بمجموع الصرف في سناب وتيكتوك معاً (20$)
            continue

        should_pause_1d, reason_1d = evaluate_adsquad_rule(s_name, st_1d, c_name, squad) if st_1d["spend"] >= 1.50 else (False, "")
        should_pause_2d, reason_2d = evaluate_adsquad_rule(s_name, st_2d, c_name, squad) if st_2d["spend"] >= 4.50 else (False, "")
        should_pause_3d, reason_3d = evaluate_adsquad_rule(s_name, st_3d, c_name, squad) if st_3d["spend"] >= 4.50 else (False, "")
        should_pause_7d, reason_7d = evaluate_adsquad_rule(s_name, st_7d, c_name, squad) if st_7d["spend"] >= 4.50 else (False, "")
        should_pause = should_pause_1d or should_pause_2d or should_pause_3d or should_pause_7d
        if should_pause_1d:
            reason = reason_1d
        elif should_pause_2d:
            reason = f"[2-Day Audit] {reason_2d}"
        elif should_pause_3d:
            reason = f"[3-Day Audit] {reason_3d}"
        else:
            reason = f"[7-Day Audit] {reason_7d}"

        if should_pause:
            # حماية الفيديو الرابح داخل مجموعة Copy: إذا كان داخل المجموعة فيديو نشط غير متراكم خاسر وحقق مبيعات بكوست ممتاز (<= $7.00 للسعودية / <= $12.00 للخليج) ولم ينزف اليوم >= $5 بدون مبيعات، نُبقي المجموعة والفيديو الرابح شغالين ونطفئ فقط الفيديوهات الصفرية الأخرى بداخلها!
            sq_ads_list = ads_by_squad_cache.get(s_id, [])
            max_win_ad_cpa = 12.0 if is_gulf_034_campaign(c_name, s_name, squad) else 7.00
            winning_active_ads = []
            losing_active_siblings = []
            for a_sq in sq_ads_list:
                if a_sq.get("status") != "ACTIVE":
                    continue
                aid_sq = a_sq["id"]
                cr_sq = a_sq.get("creative_id", "")
                mid_sq = history.get("creative_media_map", {}).get(cr_sq) or resolve_video_media_id(cr_sq, token, history)
                st3_sq_ad = ad_stats_3d_map.get(aid_sq, {"spend": 0.0, "purchases": 0, "cpa": None})
                st1_sq_ad = ad_stats_map.get(aid_sq, {"spend": 0.0, "purchases": 0, "cpa": None})
                is_disabled_vid = any(
                    mid_sq and dv.get("video_media_id") in (mid_sq, f"MEDIA:{mid_sq}", f"SA:MEDIA:{mid_sq}", f"GULF:MEDIA:{mid_sq}")
                    for dv in history.get("disabled_videos", {}).values()
                )
                if (
                    not is_disabled_vid
                    and st3_sq_ad.get("purchases", 0) >= 1
                    and st3_sq_ad.get("cpa") is not None
                    and st3_sq_ad["cpa"] <= max_win_ad_cpa
                    and not (st1_sq_ad.get("spend", 0.0) >= 5.0 and st1_sq_ad.get("purchases", 0) == 0)
                    and not (st1_sq_ad.get("purchases", 0) > 0 and st1_sq_ad.get("cpa") is not None and st1_sq_ad["cpa"] > max_win_ad_cpa)
                ):
                    winning_active_ads.append(a_sq)
                else:
                    losing_active_siblings.append(a_sq)

            if winning_active_ads:
                if losing_active_siblings and not dry_run:
                    for ls_ad in losing_active_siblings:
                        ls_ad["status"] = "PAUSED"
                    try:
                        api_put(f"/adsquads/{s_id}/ads", token, {"ads": losing_active_siblings})
                    except Exception:
                        pass
                print(
                    f"🛡️ [حماية فيديو رابح داخل المجموعة] الإبقاء على المجموعة '{s_name}' نشطة لأن بداخلها {len(winning_active_ads)} فيديو رابح بكوست ممتاز (وإطفاء {len(losing_active_siblings)} فيديو خاسر بداخلها)"
                )
                continue

            if dry_run:
                print(f"🔍 [مطابق للشرط] المجموعة: '{s_name}' (حملة: '{c_name}') | {reason}")
            else:
                squad["status"] = "PAUSED"
                api_put(f"/campaigns/{squad['campaign_id']}/adsquads", token, {"adsquads": [squad]})
                history["adsquads"][s_id] = {
                    "name": s_name,
                    "campaign": c_name,
                    "reason": reason,
                    "paused_at": now_str,
                }
                save_paused_history(history)
                print(f"🛑 تم إيقاف المجموعة: '{s_name}' (حملة: '{c_name}') | {reason}")
                time.sleep(0.3)

    print(f"\n🎬 عدد الفيديوهات الإعلانية (Ads) التي صرفت اليوم: {len(ad_stats)} (وخلال 3 أيام: {len(ad_stats_3d)})")
    for c in active_campaigns:
        c_id = c["id"]

        # تلقائياً: إطفاء أي مجموعة AdSquad نشطة أصبحت فارغة تماماً (0 إعلانات نشطة بداخلها) حتى لا تحجز خانة من الـ 15 مجموعة بدون فائدة
        if not dry_run and not is_cross_monitored_product_campaign(c.get("name", "")) and camp_ads_cache.get(c_id) is not None:
            active_ad_squad_ids = {
                a.get("ad_squad_id") for a in camp_ads_cache[c_id] if a.get("status") == "ACTIVE"
            }
            empty_active_squads = [
                sq for sq in camp_squads_cache.get(c_id, [])
                if sq.get("status") == "ACTIVE"
                and (
                    sq["id"] not in active_ad_squad_ids
                    or "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" in (sq.get("delivery_status") or [])
                )
            ]
            if empty_active_squads:
                for esq in empty_active_squads:
                    esq["status"] = "PAUSED"
                for i in range(0, len(empty_active_squads), 20):
                    try:
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": empty_active_squads[i:i+20]})
                    except Exception:
                        pass
                print(f"🧹 تم إيقاف {len(empty_active_squads)} مجموعة فارغة (0 إعلانات نشطة) تلقائياً في حملة '{c.get('name', '')}'")

    # 2A. مراجعة الفيديوهات الفردية التي أطفأها البوت: إذا ظهرت مبيعة متأخرة وكان سعر البيعة الفردي مربحاً (<= $8 للسعودية أو <= $13 للخليج)، يعيد تشغيل النسخة الفردية الرابحة (إلا إذا كان الفيديو التراكمي موقوفاً!)
    for ad_id, info in list(history["ads"].items()):
        paused_at_str = str(info.get("paused_at", ""))
        is_recent_pause = (today_d_str in paused_at_str) or (yesterday_d_str in paused_at_str)
        st1_a = ad_stats_map.get(ad_id, {"spend": 0.0, "purchases": 0, "cpa": None})
        st2_a = ad_stats_2d_map.get(ad_id)
        st3_a = ad_stats_3d_map.get(ad_id)

        if st1_a.get("spend", 0.0) >= 5.0 and st1_a.get("purchases", 0) == 0:
            continue

        st_check = None
        if st1_a.get("purchases", 0) > 0 and st1_a.get("cpa") is not None:
            st_check = st1_a
        elif is_recent_pause and st2_a and st2_a.get("purchases", 0) > 0 and st2_a.get("cpa") is not None:
            st_check = st2_a
        elif is_recent_pause and st3_a and st3_a.get("purchases", 0) > 0 and st3_a.get("cpa") is not None:
            st_check = st3_a
        else:
            continue

        ad = ad_cache.get(ad_id, {})
        if not ad:
            continue
        s_id = ad.get("ad_squad_id", "")
        if not s_id:
            continue
        squad = squad_cache.get(s_id, {})
        if not squad:
            continue
        c_id = squad.get("campaign_id", "")
        if c_id not in active_camp_ids:
            continue

        s_name = squad.get("name", "")
        c_name = camp_names.get(c_id, info.get("campaign", ""))
        ad_name = ad.get("name", info.get("name", ""))
        cr_id_chk = ad.get("creative_id", "")
        v_key_chk = history.get("creative_media_map", {}).get(cr_id_chk) or resolve_video_media_id(cr_id_chk, token, history) or cr_id_chk
        disabled_vids = history.get("disabled_videos", {})
        is_disabled_accumulated = (
            f"{c_id}:{v_key_chk}" in disabled_vids
            or f"{c_id}:MEDIA:{v_key_chk}" in disabled_vids
            or f"{c_id}:{cr_id_chk}" in disabled_vids
            or (v_key_chk and f"GLOBAL:MEDIA:{v_key_chk}" in disabled_vids)
            or "Aggregated Video" in str(info.get("reason", ""))
            or "Global Cross-Campaign" in str(info.get("reason", ""))
            or "Global Accumulated" in str(info.get("reason", ""))
            or any(
                v_key_chk and dv.get("video_media_id") in (v_key_chk, f"MEDIA:{v_key_chk}", f"SA:MEDIA:{v_key_chk}", f"GULF:MEDIA:{v_key_chk}")
                for dv in disabled_vids.values()
            )
        )
        if is_disabled_accumulated:
            # لا نعيد أبداً تشغيل فيديو تم إيقافه بسبب تراكم الصرف عبر المجموعات!
            continue
        max_revive_cpa = 12.0 if is_gulf_034_campaign(c_name, s_name, squad) else 7.0
        # إذا كان للفيديو نفسه في هذه المجموعة مبيعات مربحة لـ 3 أيام (<= $7.00)، نسمح بإعادة تشغيل هذه النسخة الفردية الرابحة حتى لو أُغلقت النسخ الأخرى
        if st3_a and st3_a.get("purchases", 0) > 0 and st3_a.get("cpa") is not None and st3_a["cpa"] > max_revive_cpa:
            continue
        if st_check["cpa"] <= max_revive_cpa:
            if ad.get("status") == "PAUSED":
                if dry_run:
                    print(
                        f"🔍 [إعادة تشغيل فيديو — مبيعة متأخرة] الفيديو: '{ad_name}' (مجموعة: '{s_name}' / حملة: '{c_name}') "
                        f"| الصرف: ${st_check['spend']} | المبيعات: {st_check['purchases']} | CPA = ${st_check['cpa']}"
                    )
                else:
                    ad["status"] = "ACTIVE"
                    api_put(f"/adsquads/{s_id}/ads", token, {"ads": [ad]})
                    del history["ads"][ad_id]
                    # إذا كانت المجموعة الأم قد أُطفئت فقط لأنها أصبحت فارغة (0 إعلانات)، وكان أداؤها لـ 3 أيام مربحاً (<= $7.00)، نعيد تفعيلها بالبيد الآمن
                    st3_sq_rev = squad_stats_3d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None})
                    if squad.get("status") == "PAUSED" and (
                        "copy" not in s_name.lower()
                        or (st3_sq_rev.get("purchases", 0) >= 1 and st3_sq_rev.get("cpa") is not None and st3_sq_rev["cpa"] <= max_revive_cpa)
                    ):
                        squad["status"] = "ACTIVE"
                        if (squad.get("bid_micro") or 0) > 2_350_000:
                            squad["bid_micro"] = 2_250_000
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                        history.get("adsquads", {}).pop(s_id, None)
                    save_paused_history(history)
                    print(
                        f"♻️🔥 [إعادة تشغيل فيديو — مبيعة متأخرة] تم إعادة تشغيل الفيديو: '{ad_name}' (مجموعة: '{s_name}' / حملة: '{c_name}') "
                        f"بعد ظهور مبيعات ({st_check['purchases']}) بـ CPA = ${st_check['cpa']} (الصرف: ${st_check['spend']})!"
                    )
                    time.sleep(0.3)
            elif ad.get("status") == "ACTIVE" and not dry_run:
                del history["ads"][ad_id]
                save_paused_history(history)

    all_candidate_ad_ids = (
        set(ad_stats_map.keys())
        | set(ad_stats_2d_map.keys())
        | set(ad_stats_3d_map.keys())
        | set(ad_stats_7d_map.keys())
    )
    for ad_id in all_candidate_ad_ids:
        st = ad_stats_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st2 = ad_stats_2d_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st3 = ad_stats_3d_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st7 = ad_stats_7d_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})

        ad = ad_cache.get(ad_id, {})
        if not ad or ad.get("status") != "ACTIVE":
            continue

        ad_name = ad.get("name", "")
        s_id = ad.get("ad_squad_id")
        squad = squad_cache.get(s_id, {})
        if not squad:
            continue
        s_name = squad.get("name", "")
        c_id = squad.get("campaign_id", "")
        c_name = camp_names.get(c_id, "")

        min_spend_chk = 0.80 if is_cross_monitored_product_campaign(c_name, s_name) else 1.0
        max_cpa_3d_ind = 12.0 if is_gulf_034_campaign(c_name, s_name, squad) else 7.0
        no_sale_1d_ind = 10.0 if is_gulf_034_campaign(c_name, s_name, squad) else 5.0
        is_proven_3d_winner = (
            st3["purchases"] >= 1
            and st3["cpa"] is not None
            and st3["cpa"] <= max_cpa_3d_ind
            and not (st["spend"] >= no_sale_1d_ind and st["purchases"] == 0)
            and not (st["purchases"] > 0 and st["cpa"] is not None and st["cpa"] > max_cpa_3d_ind)
            and not (st2["purchases"] > 0 and st2["cpa"] is not None and st2["cpa"] > max_cpa_3d_ind)
        )
        should_pause, reason = (
            evaluate_single_ad_rule(st, c_name, s_name, squad)
            if (st["spend"] >= min_spend_chk and not is_proven_3d_winner)
            else (False, "")
        )
        if not should_pause and st2["spend"] >= min_spend_chk and not is_proven_3d_winner:
            should_pause, reason = evaluate_single_ad_rule(st2, c_name, s_name, squad)
            if should_pause:
                reason = f"[2-Day Audit] {reason}"
        if not should_pause and st3["spend"] >= min_spend_chk:
            should_pause, reason = evaluate_single_ad_rule(st3, c_name, s_name, squad)
            if should_pause:
                reason = f"[3-Day Audit] {reason}"
        if not should_pause and st7["spend"] >= min_spend_chk and not is_proven_3d_winner:
            should_pause, reason = evaluate_single_ad_rule(st7, c_name, s_name, squad)
            if should_pause:
                reason = f"[7-Day Audit] {reason}"
        if not should_pause:
            continue

        if ad_id in history["ads"]:
            print(f"⏭️ تجاهل الفيديو '{ad_name}' (حملة: '{c_name}', صرف: ${st['spend']}, CPC: ${st['cpc']}) — أعدتَ تشغيله يدوياً (فرصة أخرى).")
            continue

        if dry_run:
            print(f"🔍 [مطابق لشرط الفيديو الفردي] الفيديو: '{ad_name}' (حملة: '{c_name}' / مجموعة: '{s_name}') | {reason}")
        else:
            ad["status"] = "PAUSED"
            api_put(f"/adsquads/{s_id}/ads", token, {"ads": [ad]})
            history["ads"][ad_id] = {
                "name": ad_name,
                "campaign": c_name,
                "adsquad": s_name,
                "reason": reason,
                "paused_at": now_str,
            }
            # إذا خسر إعلان يحمل عنواناً مختبراً [H1..H6]، نمنع تكرار هذا العنوان الخاسر نهائياً في النسخ القادمة!
            if "[H" in ad_name:
                cr_id_lost = ad.get("creative_id", "")
                if cr_id_lost:
                    history.setdefault("disabled_videos", {})[f"{c_id}:{cr_id_lost}"] = {
                        "campaign_id": c_id,
                        "campaign_name": c_name,
                        "video_media_id": cr_id_lost,
                        "sample_ad_name": ad_name.strip(),
                        "reason": f"[Headline Variant Lost] {reason}",
                        "disabled_at": now_str,
                    }
                try:
                    hl_tag_lost = "H" + ad_name.split("[H", 1)[1].split("]", 1)[0].strip()
                    history.setdefault("disabled_headlines", {})[f"{c_id}:{hl_tag_lost}"] = {
                        "campaign_id": c_id,
                        "campaign_name": c_name,
                        "headline_tag": hl_tag_lost,
                        "sample_ad_name": ad_name.strip(),
                        "reason": reason,
                        "disabled_at": now_str,
                    }
                    print(f"   🚫 تم حظر العنوان الخاسر [{hl_tag_lost}] في حملة '{c_name}' ولن يتم تكراره مرة أخرى!")
                except Exception:
                    pass
            save_paused_history(history)
            print(f"🛑 تم إيقاف الفيديو: '{ad_name}' (حملة: '{c_name}' / مجموعة: '{s_name}') | {reason}")
            time.sleep(0.3)

    # 2B. فحص مجموع صرف ومبيعات كل فيديو تراكمي (مكرر في عدة AdSquads بالاسم أو بالـ Media ID) على مستوى الحملة بالكامل (اليوم 1D + يومين 2D + 3 أيام 3D + 7 أيام 7D):
    print(f"\n🧮 فحص مجموع صرف الفيديوهات التراكمية عبر كل الـ AdSquads داخل الحملات النشطة ({len(active_campaigns)} حملة - 1D + 2D + 3D + 7D)...")

    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        c_squads = camp_squads_cache.get(c_id, [])
        if not c_squads or is_cross_monitored_product_campaign(c_name):
            continue

        camp_is_saudi = is_saudi_squad(c_squads[0], c_name, c_squads[0].get("name", ""))
        no_sale_limit = 7.0 if camp_is_saudi else 12.0
        max_cpa_limit = 7.0 if camp_is_saudi else 12.0
        region_label = "السعودية" if camp_is_saudi else "الخليج"

        c_ads = camp_ads_cache.get(c_id, [])
        if not c_ads:
            continue

        squad_status_map = {sq["id"]: sq.get("status") for sq in c_squads if sq.get("id")}
        relevant_creatives = {
            a.get("creative_id")
            for a in c_ads
            if a.get("creative_id")
            and (
                a.get("id") in ad_stats_map
                or a.get("id") in ad_stats_2d_map
                or a.get("id") in ad_stats_3d_map
                or a.get("id") in ad_stats_7d_map
                or (a.get("status") == "ACTIVE" and squad_status_map.get(a.get("ad_squad_id")) == "ACTIVE")
            )
        }
        if not relevant_creatives:
            continue

        for cr_id in relevant_creatives:
            resolve_video_media_id(cr_id, token, history)
        # تنظيف أي مفاتيح قديمة مبنية على الاسم (NAME:) من disabled_videos لاعتماد معرف الفيديو الحقيقي (top_snap_media_id) حصراً
        for dk_old in list(history.get("disabled_videos", {}).keys()):
            if ":NAME:" in dk_old or str(history["disabled_videos"][dk_old].get("video_media_id", "")).startswith("NAME:"):
                del history["disabled_videos"][dk_old]
        save_paused_history(history)

        video_groups = {}
        for a in c_ads:
            cr_id = a.get("creative_id")
            ad_nm = (a.get("name") or "").strip()
            base_nm = clean_base_ad_name(ad_nm)
            media_id = history["creative_media_map"].get(cr_id) or (resolve_video_media_id(cr_id, token, history) if cr_id else None)
            # التجميع الصارم بناءً على ملف الفيديو الحقيقي نفسه (top_snap_media_id) حصراً — وليس الاسم المكتوب عليه!
            v_key = f"MEDIA:{media_id}" if media_id else f"CR:{cr_id or a['id']}"

            grp = video_groups.setdefault(v_key, {
                "ads": [],
                "active_ads": [],
                "spend": 0.0,
                "purchases": 0,
                "swipes": 0,
                "spend_2d": 0.0,
                "purchases_2d": 0,
                "spend_3d": 0.0,
                "purchases_3d": 0,
                "spend_7d": 0.0,
                "purchases_7d": 0,
                "sample_name": base_nm or ad_nm,
            })
            grp["ads"].append(a)
            if a.get("status") == "ACTIVE" and squad_status_map.get(a.get("ad_squad_id")) == "ACTIVE":
                grp["active_ads"].append(a)
            st = ad_stats_map.get(a["id"])
            if st:
                grp["spend"] = round(grp["spend"] + st["spend"], 2)
                grp["purchases"] += st["purchases"]
                grp["swipes"] += st["swipes"]
            st2 = ad_stats_2d_map.get(a["id"])
            if st2:
                grp["spend_2d"] = round(grp["spend_2d"] + st2["spend"], 2)
                grp["purchases_2d"] += st2["purchases"]
            st3 = ad_stats_3d_map.get(a["id"])
            if st3:
                grp["spend_3d"] = round(grp["spend_3d"] + st3["spend"], 2)
                grp["purchases_3d"] += st3["purchases"]
            st7 = ad_stats_7d_map.get(a["id"])
            if st7:
                grp["spend_7d"] = round(grp["spend_7d"] + st7["spend"], 2)
                grp["purchases_7d"] += st7["purchases"]

        for v_key, grp in video_groups.items():
            tot_spend = grp["spend"]
            tot_purch = grp["purchases"]
            sp2, pu2 = grp["spend_2d"], grp["purchases_2d"]
            sp3, pu3 = grp["spend_3d"], grp["purchases_3d"]
            sp7, pu7 = grp["spend_7d"], grp["purchases_7d"]
            active_list = [a for a in grp["active_ads"] if a.get("status") == "ACTIVE"]
            if not active_list:
                continue

            raw_v_key = v_key.split(":", 1)[1] if ":" in v_key else v_key
            tot_cpa = round(tot_spend / tot_purch, 2) if tot_purch > 0 else None
            cpa2 = round(sp2 / pu2, 2) if pu2 > 0 else None
            cpa3 = round(sp3 / pu3, 2) if pu3 > 0 else None
            cpa7 = round(sp7 / pu7, 2) if pu7 > 0 else None

            has_recent_bleed = (tot_spend >= no_sale_limit and tot_purch == 0) or (sp2 >= no_sale_limit and pu2 == 0)
            if not has_recent_bleed and (
                (pu3 >= 1 and cpa3 is not None and cpa3 <= max_cpa_limit)
                and (pu2 == 0 or (cpa2 is not None and cpa2 <= max_cpa_limit))
                and (tot_purch == 0 or (tot_cpa is not None and tot_cpa <= max_cpa_limit))
            ):
                for dk in list(history.get("disabled_videos", {}).keys()):
                    dv = history["disabled_videos"][dk]
                    if dv.get("campaign_id") in (c_id, "GLOBAL") and dv.get("video_media_id") in (v_key, raw_v_key):
                        del history["disabled_videos"][dk]
                continue

            already_permanently_disabled = (
                f"{c_id}:{v_key}" in history.get("disabled_videos", {})
                or f"{c_id}:{raw_v_key}" in history.get("disabled_videos", {})
                or f"GLOBAL:{v_key}" in history.get("disabled_videos", {})
                or any(
                    dv.get("campaign_id") in (c_id, "GLOBAL")
                    and dv.get("video_media_id") in (v_key, raw_v_key)
                    for dv in history.get("disabled_videos", {}).values()
                )
            )
            if not already_permanently_disabled and max(tot_spend, sp2, sp3, sp7) < no_sale_limit:
                continue

            kill_video = False
            kill_reason = ""
            if already_permanently_disabled:
                kill_video = True
                kill_reason = (
                    f"[Permanent Disabled Video Lock - {region_label}] هذا الفيديو التراكمي موقوف نهائياً ولن يُسمح بإعادة تشغيله في أي AdSquad"
                )
            if tot_spend >= no_sale_limit and tot_purch == 0:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video Stop-Loss 1D - {region_label}] مجموع صرف الفيديو التراكمي اليوم عبر كل الـ AdSquads = "
                    f"${tot_spend:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة اليوم"
                )
            elif sp2 >= no_sale_limit and pu2 == 0:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video Stop-Loss 2D - {region_label}] مجموع صرف الفيديو التراكمي ليومين عبر كل الـ AdSquads = "
                    f"${sp2:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة في يومين"
                )
            elif sp3 >= no_sale_limit and pu3 == 0:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video Stop-Loss 3D - {region_label}] مجموع صرف الفيديو التراكمي لـ 3 أيام عبر كل الـ AdSquads = "
                    f"${sp3:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة"
                )
            elif sp7 >= no_sale_limit and pu7 == 0:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video Stop-Loss 7D - {region_label}] مجموع صرف الفيديو التراكمي لـ 7 أيام عبر كل الـ AdSquads = "
                    f"${sp7:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة"
                )
            elif sp3 >= no_sale_limit and pu3 > 0 and cpa3 is not None and cpa3 > max_cpa_limit:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video High CPA 3D - {region_label}] مجموع صرف الفيديو التراكمي لـ 3 أيام = "
                    f"${sp3:.2f} | المبيعات = {pu3} | CPA التراكمي = ${cpa3:.2f} (> ${max_cpa_limit:.2f})"
                )
            elif sp2 >= no_sale_limit and pu2 > 0 and cpa2 is not None and cpa2 > max_cpa_limit:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video High CPA 2D - {region_label}] مجموع صرف الفيديو التراكمي ليومين = "
                    f"${sp2:.2f} | المبيعات = {pu2} | CPA التراكمي = ${cpa2:.2f} (> ${max_cpa_limit:.2f})"
                )
            elif tot_spend >= no_sale_limit and tot_purch > 0 and tot_cpa is not None and tot_cpa > max_cpa_limit:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video High CPA 1D - {region_label}] مجموع صرف الفيديو التراكمي اليوم = "
                    f"${tot_spend:.2f} | المبيعات = {tot_purch} | CPA التراكمي = ${tot_cpa:.2f} (> ${max_cpa_limit:.2f})"
                )
            elif sp7 >= no_sale_limit and pu7 > 0 and cpa7 is not None and cpa7 > max_cpa_limit and (pu3 == 0 or (cpa3 is not None and cpa3 > max_cpa_limit)):
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video High CPA 7D - {region_label}] مجموع صرف الفيديو التراكمي لـ 7 أيام = "
                    f"${sp7:.2f} | المبيعات = {pu7} | CPA التراكمي = ${cpa7:.2f} (> ${max_cpa_limit:.2f})"
                )

            if not kill_video:
                continue

            # حماية النسخ الفردية المربحة (CPA <= $7.00 للسعودية / <= $12.00 للخليج) من الإيقاف التراكمي
            win_copy_cpa_lim = 7.00 if camp_is_saudi else 12.0
            targets_to_pause = []
            for a_cand in active_list:
                aid_c = a_cand["id"]
                st1_c = ad_stats_map.get(aid_c, {"spend": 0.0, "purchases": 0, "cpa": None})
                st3_c = ad_stats_3d_map.get(aid_c, {"spend": 0.0, "purchases": 0, "cpa": None})
                is_profitable_copy = (
                    (
                        (st1_c["purchases"] >= 1 and st1_c["cpa"] is not None and st1_c["cpa"] <= win_copy_cpa_lim)
                        or (st3_c["purchases"] >= 1 and st3_c["cpa"] is not None and st3_c["cpa"] <= win_copy_cpa_lim)
                    )
                    and not (st1_c["spend"] >= 5.0 and st1_c["purchases"] == 0)
                    and not (st1_c["purchases"] > 0 and st1_c["cpa"] is not None and st1_c["cpa"] > win_copy_cpa_lim)
                )
                if not is_profitable_copy:
                    targets_to_pause.append(a_cand)

            if not targets_to_pause:
                continue

            if dry_run:
                print(
                    f"🔍 [حذف/إيقاف النسخ الخاسرة للفيديو التراكمي] الحملة: '{c_name}' | الفيديو: '{grp['sample_name']}' ({v_key}) "
                    f"| النسخ التي ستُغلق: {len(targets_to_pause)} | {kill_reason}"
                )
            else:
                print(
                    f"🗑️ جاري إيقاف النسخ الخاسرة للفيديو التراكمي '{grp['sample_name']}' ({v_key}) في {len(targets_to_pause)} مجموعة في حملة '{c_name}' | {kill_reason}"
                )
                ads_by_squad = {}
                for a in targets_to_pause:
                    a["status"] = "PAUSED"
                    ads_by_squad.setdefault(a["ad_squad_id"], []).append(a)

                closed_cnt = 0
                for sq_id, sq_ads in ads_by_squad.items():
                    try:
                        api_put(f"/adsquads/{sq_id}/ads", token, {"ads": sq_ads})
                        for a in sq_ads:
                            history["ads"][a["id"]] = {
                                "name": a.get("name", ""),
                                "campaign": c_name,
                                "adsquad": squad_cache.get(sq_id, {}).get("name", sq_id),
                                "reason": kill_reason,
                                "paused_at": now_str,
                            }
                            closed_cnt += 1
                        time.sleep(0.2)
                    except Exception as e:
                        print(f"   ⚠️ خطأ أثناء إغلاق الفيديو في المجموعة {sq_id}: {e}")

                # يُحظر الفيديو نهائياً في disabled_videos بمعرفه الحقيقي (top_snap_media_id) فقط إذا لم يحقق أي مبيعة على الإطلاق (0 مبيعات)
                if max(tot_purch, pu2, pu3, pu7) == 0:
                    history["disabled_videos"][f"{c_id}:{v_key}"] = {
                        "campaign_id": c_id,
                        "campaign_name": c_name,
                        "video_media_id": raw_v_key,
                        "sample_ad_name": grp["sample_name"],
                        "closed_instances": closed_cnt,
                        "total_spend": max(tot_spend, sp2, sp3, sp7),
                        "total_purchases": 0,
                        "reason": kill_reason,
                        "disabled_at": now_str,
                    }
                save_paused_history(history)
                print(f"   ✅ تم إيقاف النسخ الخاسرة للفيديو التراكمي ({closed_cnt} نسخة) بنجاح مع حماية النسخ الفردية المربحة!")

    # 2B-Global. فحص مجموع صرف ومبيعات الفيديوهات التراكمية على مستوى الحساب بالكامل بناءً على معرف الفيديو الحقيقي (top_snap_media_id) حصراً:
    print("\n🌐 فحص مجموع صرف الفيديوهات التراكمية على مستوى الحساب بالكامل بمعرف الفيديو الحقيقي (top_snap_media_id) عبر جميع الحملات والمجموعات (1D + 2D + 3D + 7D)...")
    global_video_groups = {}
    ads_for_global_audit = all_account_ads if all_account_ads else list(ad_cache.values())
    for a in ads_for_global_audit:
        ad_id = a.get("id")
        sq_id = a.get("ad_squad_id")
        sq_obj = squad_cache.get(sq_id, {})
        c_id = sq_obj.get("campaign_id", "")
        c_name = camp_names.get(c_id, "")
        s_name = sq_obj.get("name", "")
        if is_cross_monitored_product_campaign(c_name, s_name):
            continue
        is_Active_Now = (
            c_id in active_camp_ids
            and sq_obj.get("status") == "ACTIVE"
            and a.get("status") == "ACTIVE"
        )
        has_any_stats = (
            ad_id in ad_stats_map
            or ad_id in ad_stats_2d_map
            or ad_id in ad_stats_3d_map
            or ad_id in ad_stats_7d_map
        )
        if not is_Active_Now and not has_any_stats:
            continue

        cr_id = a.get("creative_id")
        if cr_id and cr_id not in history.get("creative_media_map", {}):
            resolve_video_media_id(cr_id, token, history)
        media_id = history.get("creative_media_map", {}).get(cr_id)
        raw_nm = (a.get("name") or "").strip()
        clean_nm = clean_base_ad_name(raw_nm)
        ad_is_saudi = is_saudi_squad(sq_obj, c_name, s_name)
        reg_prefix = "SA" if ad_is_saudi else "GULF"

        v_key_glob = f"{reg_prefix}:MEDIA:{media_id}" if media_id else f"{reg_prefix}:CR:{cr_id or ad_id}"
        g_grp = global_video_groups.setdefault(v_key_glob, {
            "is_saudi": ad_is_saudi,
            "active_ads": [],
            "campaigns": set(),
            "spend_1d": 0.0,
            "purchases_1d": 0,
            "spend_2d": 0.0,
            "purchases_2d": 0,
            "spend_3d": 0.0,
            "purchases_3d": 0,
            "spend_7d": 0.0,
            "purchases_7d": 0,
            "sample_name": clean_nm or raw_nm,
        })
        if c_name:
            g_grp["campaigns"].add(c_name)
        if is_Active_Now:
            g_grp["active_ads"].append((c_id, c_name, a))
        st1_a = ad_stats_map.get(ad_id)
        if st1_a:
            g_grp["spend_1d"] = round(g_grp["spend_1d"] + st1_a["spend"], 2)
            g_grp["purchases_1d"] += st1_a["purchases"]
        st2_a = ad_stats_2d_map.get(ad_id)
        if st2_a:
            g_grp["spend_2d"] = round(g_grp["spend_2d"] + st2_a["spend"], 2)
            g_grp["purchases_2d"] += st2_a["purchases"]
        st3_a = ad_stats_3d_map.get(ad_id)
        if st3_a:
            g_grp["spend_3d"] = round(g_grp["spend_3d"] + st3_a["spend"], 2)
            g_grp["purchases_3d"] += st3_a["purchases"]
        st7_a = ad_stats_7d_map.get(ad_id)
        if st7_a:
            g_grp["spend_7d"] = round(g_grp["spend_7d"] + st7_a["spend"], 2)
            g_grp["purchases_7d"] += st7_a["purchases"]

    for v_key_glob, g_grp in global_video_groups.items():
        active_glob_list = [
            (cid_x, cnm_x, ax) for (cid_x, cnm_x, ax) in g_grp["active_ads"] if ax.get("status") == "ACTIVE"
        ]
        if not active_glob_list:
            continue
        is_sa_glob = g_grp["is_saudi"]
        global_no_sale_limit = 7.0 if is_sa_glob else 12.0
        global_max_cpa_limit = 7.0 if is_sa_glob else 12.0
        g_sp1, g_pu1 = g_grp["spend_1d"], g_grp["purchases_1d"]
        g_sp2, g_pu2 = g_grp["spend_2d"], g_grp["purchases_2d"]
        g_sp3, g_pu3 = g_grp["spend_3d"], g_grp["purchases_3d"]
        g_sp7, g_pu7 = g_grp["spend_7d"], g_grp["purchases_7d"]
        if max(g_sp1, g_sp2, g_sp3, g_sp7) < global_no_sale_limit:
            continue
        g_cpa1 = round(g_sp1 / g_pu1, 2) if g_pu1 > 0 else None
        g_cpa2 = round(g_sp2 / g_pu2, 2) if g_pu2 > 0 else None
        g_cpa3 = round(g_sp3 / g_pu3, 2) if g_pu3 > 0 else None
        g_cpa7 = round(g_sp7 / g_pu7, 2) if g_pu7 > 0 else None
        has_glob_bleed = (g_sp1 >= global_no_sale_limit and g_pu1 == 0) or (g_sp2 >= global_no_sale_limit and g_pu2 == 0)
        if (
            not has_glob_bleed
            and (g_pu3 >= 1 and g_cpa3 is not None and g_cpa3 <= global_max_cpa_limit)
            and (g_pu2 == 0 or (g_cpa2 is not None and g_cpa2 <= global_max_cpa_limit))
            and (g_pu1 == 0 or (g_cpa1 is not None and g_cpa1 <= global_max_cpa_limit))
        ):
            continue

        g_kill = False
        g_reason = ""
        if g_sp1 >= global_no_sale_limit and g_pu1 == 0:
            g_kill = True
            g_reason = f"[Global Accumulated Video Stop-Loss 1D] مجموع صرف الفيديو التراكمي اليوم على مستوى الحساب = ${g_sp1:.2f} (>= ${global_no_sale_limit:.2f}) بدون أي مبيعة اليوم"
        elif g_sp2 >= global_no_sale_limit and g_pu2 == 0:
            g_kill = True
            g_reason = f"[Global Accumulated Video Stop-Loss 2D] مجموع صرف الفيديو التراكمي ليومين على مستوى الحساب = ${g_sp2:.2f} (>= ${global_no_sale_limit:.2f}) بدون أي مبيعة في يومين"
        elif g_sp3 >= global_no_sale_limit and g_pu3 == 0:
            g_kill = True
            g_reason = f"[Global Accumulated Video Stop-Loss 3D] مجموع صرف الفيديو التراكمي لـ 3 أيام على مستوى الحساب = ${g_sp3:.2f} (>= ${global_no_sale_limit:.2f}) بدون أي مبيعة"
        elif g_sp7 >= global_no_sale_limit and g_pu7 == 0:
            g_kill = True
            g_reason = f"[Global Accumulated Video Stop-Loss 7D] مجموع صرف الفيديو التراكمي لـ 7 أيام على مستوى الحساب = ${g_sp7:.2f} (>= ${global_no_sale_limit:.2f}) بدون أي مبيعة"
        elif g_sp3 >= global_no_sale_limit and g_pu3 > 0 and g_cpa3 is not None and g_cpa3 > global_max_cpa_limit:
            g_kill = True
            g_reason = f"[Global Accumulated Video High CPA 3D] مجموع صرف الفيديو التراكمي لـ 3 أيام على مستوى الحساب = ${g_sp3:.2f} | المبيعات = {g_pu3} | CPA التراكمي = ${g_cpa3:.2f} (> ${global_max_cpa_limit:.2f})"
        elif g_sp2 >= global_no_sale_limit and g_pu2 > 0 and g_cpa2 is not None and g_cpa2 > global_max_cpa_limit:
            g_kill = True
            g_reason = f"[Global Accumulated Video High CPA 2D] مجموع صرف الفيديو التراكمي ليومين على مستوى الحساب = ${g_sp2:.2f} | المبيعات = {g_pu2} | CPA التراكمي = ${g_cpa2:.2f} (> ${global_max_cpa_limit:.2f})"
        elif g_sp1 >= global_no_sale_limit and g_pu1 > 0 and g_cpa1 is not None and g_cpa1 > global_max_cpa_limit:
            g_kill = True
            g_reason = f"[Global Accumulated Video High CPA 1D] مجموع صرف الفيديو التراكمي اليوم على مستوى الحساب = ${g_sp1:.2f} | المبيعات = {g_pu1} | CPA التراكمي = ${g_cpa1:.2f} (> ${global_max_cpa_limit:.2f})"
        elif g_sp7 >= global_no_sale_limit and g_pu7 > 0 and g_cpa7 is not None and g_cpa7 > global_max_cpa_limit and (g_pu3 == 0 or (g_cpa3 is not None and g_cpa3 > global_max_cpa_limit)):
            g_kill = True
            g_reason = f"[Global Accumulated Video High CPA 7D] مجموع صرف الفيديو التراكمي لـ 7 أيام على مستوى الحساب = ${g_sp7:.2f} | المبيعات = {g_pu7} | CPA التراكمي = ${g_cpa7:.2f} (> ${global_max_cpa_limit:.2f})"

        if not g_kill:
            continue

        # حماية النسخ الفردية المربحة (CPA <= $7.00 للسعودية / <= $12.00 للخليج) من الإيقاف التراكمي على مستوى الحساب
        g_win_cpa_lim = 7.00 if is_sa_glob else 12.0
        g_targets = []
        for c_id_g, c_name_g, a_g in active_glob_list:
            aid_g = a_g["id"]
            st1_g = ad_stats_map.get(aid_g, {"spend": 0.0, "purchases": 0, "cpa": None})
            st3_g = ad_stats_3d_map.get(aid_g, {"spend": 0.0, "purchases": 0, "cpa": None})
            is_prof_g = (
                (
                    (st1_g["purchases"] >= 1 and st1_g["cpa"] is not None and st1_g["cpa"] <= g_win_cpa_lim)
                    or (st3_g["purchases"] >= 1 and st3_g["cpa"] is not None and st3_g["cpa"] <= g_win_cpa_lim)
                )
                and not (st1_g["spend"] >= 5.0 and st1_g["purchases"] == 0)
                and not (st1_g["purchases"] > 0 and st1_g["cpa"] is not None and st1_g["cpa"] > g_win_cpa_lim)
            )
            if not is_prof_g:
                g_targets.append((c_id_g, c_name_g, a_g))

        if not g_targets:
            continue

        if dry_run:
            print(f"🔍 [إيقاف النسخ الخاسرة لفيديو تراكمي على مستوى الحساب] الفيديو: '{g_grp['sample_name']}' ({v_key_glob}) | النسخ المستهدفة: {len(g_targets)} | {g_reason}")
        else:
            ads_by_sq_glob = {}
            for c_id_g, c_name_g, a_g in g_targets:
                a_g["status"] = "PAUSED"
                ads_by_sq_glob.setdefault((c_id_g, c_name_g, a_g["ad_squad_id"]), []).append(a_g)
            closed_g = 0
            for (c_id_g, c_name_g, sq_id_g), sq_ads_g in ads_by_sq_glob.items():
                try:
                    api_put(f"/adsquads/{sq_id_g}/ads", token, {"ads": sq_ads_g})
                    for a_g in sq_ads_g:
                        history["ads"][a_g["id"]] = {
                            "name": a_g.get("name", ""),
                            "campaign": c_name_g,
                            "adsquad": squad_cache.get(sq_id_g, {}).get("name", sq_id_g),
                            "reason": g_reason,
                            "paused_at": now_str,
                        }
                        closed_g += 1
                    if max(g_pu1, g_pu2, g_pu3, g_pu7) == 0:
                        history["disabled_videos"][f"{c_id_g}:{v_key_glob}"] = {
                            "campaign_id": c_id_g,
                            "campaign_name": c_name_g,
                            "video_media_id": v_key_glob,
                            "sample_ad_name": g_grp["sample_name"],
                            "closed_instances": len(sq_ads_g),
                            "total_spend": max(g_sp1, g_sp2, g_sp3, g_sp7),
                            "total_purchases": 0,
                            "reason": g_reason,
                            "disabled_at": now_str,
                        }
                    time.sleep(0.2)
                except Exception as e:
                    print(f"   ⚠️ خطأ أثناء إغلاق الفيديو التراكمي في المجموعة {sq_id_g}: {e}")
            save_paused_history(history)
            print(f"🗑️✅ [إيقاف النسخ الخاسرة لفيديو تراكمي على مستوى الحساب] تم إيقاف '{g_grp['sample_name']}' في {closed_g} مجموعة مع حماية النسخ الفردية المربحة | {g_reason}")

    # 2C. تنظيف المجموعات الإعلانية الميتة التي أصبحت بدون أي فيديو نشط (INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS)
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        c_squads = camp_squads_cache.get(c_id)
        if not c_squads or is_cross_monitored_product_campaign(c_name):
            continue
        active_sq_ids_after_2b = {
            a.get("ad_squad_id") for a in camp_ads_cache.get(c_id, []) if a.get("status") == "ACTIVE"
        }
        dead_to_pause = [
            sq for sq in c_squads
            if sq.get("status") == "ACTIVE"
            and (
                sq["id"] not in active_sq_ids_after_2b
                or "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" in (sq.get("delivery_status") or [])
            )
        ]
        if dead_to_pause and not dry_run:
            for sq in dead_to_pause:
                sq["status"] = "PAUSED"
            for i in range(0, len(dead_to_pause), 20):
                try:
                    api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": dead_to_pause[i:i+20]})
                except Exception:
                    pass
            print(f"🧹 تم إيقاف {len(dead_to_pause)} مجموعة ميتة (0 إعلانات نشطة) تلقائياً في حملة '{c_name}'")

    # 3. نظام ضبط سلم البيدات ($2.05-$2.35) + كشّاف الجمود (نسختان فقط لو الحملة متوقفة < $2.00) + السكيل العمودي ($20 -> $35 -> $50):
    now_utc = datetime.now(timezone.utc)
    today_local = datetime.now(ACCOUNT_TZ)
    today_date_str = today_local.strftime("%Y-%m-%d")
    is_night_window = (0 <= today_local.hour < 6)
    is_before_6pm_window = (6 <= today_local.hour < 17)
    bid_ladder_micro = [2_250_000, 2_350_000, 2_450_000, 2_500_000]

    history.setdefault("smart_spend_watchdog", {})
    print(
        f"\n🛡️ فحص ضبط سلم البيدات ($2.25-$2.55 للماكس بيد / $2.75-$2.85 للتارجت كوست) + السكيل العمودي للمجموعات الفائزة ($20 -> $35 -> $50) + كشّاف الجمود..."
    )
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        c_squads = camp_squads_cache.get(c_id)
        if c_squads is None:
            c_squads = api_get_paginated(f"/campaigns/{c_id}/adsquads?limit=200", token, "adsquads", "adsquad")

        camp_total_spend_today = sum(spend_by_squad.get(sq.get("id", ""), 0.0) for sq in c_squads if sq)
        scouts_nudged_in_camp = 0

        for idx_sq, squad in enumerate(c_squads):
            if not squad or squad.get("status") != "ACTIVE":
                continue
            s_id = squad["id"]
            s_name = squad.get("name", "")
            bid_micro = squad.get("bid_micro") or 0
            budget_micro = squad.get("daily_budget_micro") or 0
            today_spend = spend_by_squad.get(s_id, 0.0)
            st_sq = squad_stats_map.get(s_id, {"spend": today_spend, "purchases": 0, "cpa": None})
            st3_sq = squad_stats_3d_map.get(s_id, {"spend": today_spend, "purchases": 0, "cpa": None})
            is_gulf_sq = is_gulf_034_campaign(c_name, s_name, squad)
            is_cross_sq = is_cross_monitored_product_campaign(c_name, s_name)
            is_tc_sq = squad.get("bid_strategy") == "TARGET_COST" or bool(squad.get("target_bid"))
            is_winning_today = (
                (st_sq.get("purchases", 0) >= 1 and st_sq.get("cpa") is not None and st_sq["cpa"] <= 7.00)
                or (st3_sq.get("purchases", 0) >= 1 and st3_sq.get("cpa") is not None and st3_sq["cpa"] <= 7.00)
            )

            if is_cross_sq:
                max_safe_bid_micro = 3_600_000
            elif is_tc_sq:
                max_safe_bid_micro = 2_850_000
            else:
                max_safe_bid_micro = 2_650_000 if is_gulf_sq else 2_550_000

            # (أ) تهدئة البيدات المتضخمة فوق السقف الآمن إلى سلم البيدات المتدرج لمنع النزيف والخسارة:
            if bid_micro > max_safe_bid_micro and not is_winning_today:
                if is_cross_sq:
                    target_ladder_micro = 3_400_000 if is_tc_sq else 3_500_000
                elif is_tc_sq:
                    target_ladder_micro = 2_750_000
                else:
                    target_ladder_micro = bid_ladder_micro[idx_sq % len(bid_ladder_micro)]
                    if is_gulf_sq:
                        target_ladder_micro = min(2_650_000, target_ladder_micro + 100_000)
                old_b = round(bid_micro / 1_000_000, 2)
                new_b = round(target_ladder_micro / 1_000_000, 2)
                if dry_run:
                    print(f"🔍 [تهدئة بيد متضخم إلى سلم الأمان] المجموعة '{s_name}' (حملة: '{c_name}'): ${old_b} -> ${new_b}")
                else:
                    squad["bid_micro"] = target_ladder_micro
                    try:
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                        bid_micro = target_ladder_micro
                        print(f"📉✅ [تهدئة بيد متضخم إلى سلم الأمان] المجموعة '{s_name}' (حملة: '{c_name}'): ${old_b} -> ${new_b}")
                        time.sleep(0.2)
                    except Exception:
                        pass

            if is_night_window or not is_before_6pm_window:
                continue

            w_rec = history["smart_spend_watchdog"].get(s_id, {})
            if w_rec.get("date") != today_date_str:
                w_rec = {
                    "date": today_date_str,
                    "name": s_name,
                    "campaign": c_name,
                    "bumps_today": 0,
                    "last_action_at": None,
                    "last_spend": today_spend,
                    "last_check_at": now_utc.isoformat(),
                    "scaled_budget_today": False,
                }
                history["smart_spend_watchdog"][s_id] = w_rec

            # (ب) السكيل العمودي الفوري ($20 -> $35 -> $50) لأي مجموعة نشطة تجلب >= 2 مبيعات بـ CPA <= $7.00 وتصرف >= 45% من ميزانيتها (بدون لمس البيد نهائياً!)
            daily_budget = budget_micro / 1_000_000 if budget_micro > 0 else 20.0
            has_vertical_scale_win = (
                (st_sq.get("purchases", 0) >= 2 and st_sq.get("cpa") is not None and st_sq["cpa"] <= 7.00)
                or (
                    st3_sq.get("purchases", 0) >= 3
                    and st3_sq.get("cpa") is not None
                    and st3_sq["cpa"] <= 7.00
                    and st_sq.get("purchases", 0) >= 1
                    and st_sq.get("cpa") is not None
                    and st_sq["cpa"] <= 7.50
                )
            )
            if (
                not is_cross_sq
                and not w_rec.get("scaled_budget_today")
                and has_vertical_scale_win
                and today_spend >= (daily_budget * 0.45)
                and daily_budget < 80.0
            ):
                if daily_budget <= 22.0:
                    new_budget_micro = 35_000_000
                elif daily_budget <= 38.0:
                    new_budget_micro = 50_000_000
                else:
                    new_budget_micro = min(80_000_000, int(round(budget_micro * 1.30)))
                if not dry_run:
                    squad["daily_budget_micro"] = new_budget_micro
                    try:
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                        w_rec["scaled_budget_today"] = True
                        save_paused_history(history)
                        print(
                            f"🚀 [سكيل عمودي للمجموعة الفائزة (بدون لمس البيد)] المجموعة '{s_name}' (حملة: '{c_name}') "
                            f"| المبيعات اليوم: {st_sq['purchases']} (CPA=${st_sq['cpa']}) | الميزانية: ${daily_budget:.0f} -> ${new_budget_micro/1e6:.0f}"
                        )
                    except Exception:
                        pass

            # (ج) كشّاف الجمود (Scout Rule): تحريك البيد +$0.10 للمجموعات البطيئة الصرف (< $0.80) بحد أقصى 4 مجموعات في الدورة لكل حملة وبسقف آمن:
            if is_cross_sq or scouts_nudged_in_camp >= 4:
                continue
            if today_spend >= 0.80:
                continue

            created_dt = parse_iso_utc(squad.get("created_at", ""))
            start_dt = parse_iso_utc(squad.get("start_time", ""))
            candidates = [dt for dt in (created_dt, start_dt) if dt is not None]
            effective_start = max(candidates) if candidates else (now_utc - timedelta(hours=4))
            if (now_utc - effective_start) < timedelta(hours=2):
                continue

            last_act_dt = parse_iso_utc(w_rec.get("last_action_at", ""))
            if last_act_dt and (now_utc - last_act_dt) < timedelta(hours=2):
                continue
            if int(w_rec.get("bumps_today", 0)) >= 1:
                continue

            if bid_micro <= 0 or bid_micro >= max_safe_bid_micro:
                continue

            new_bid_micro = min(max_safe_bid_micro, bid_micro + 100_000)
            if new_bid_micro <= bid_micro:
                continue

            old_b = round(bid_micro / 1_000_000, 2)
            new_b = round(new_bid_micro / 1_000_000, 2)
            if not dry_run:
                squad["bid_micro"] = new_bid_micro
                try:
                    api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                    w_rec["bumps_today"] = int(w_rec.get("bumps_today", 0)) + 1
                    w_rec["last_action_at"] = now_utc.isoformat()
                    save_paused_history(history)
                    scouts_nudged_in_camp += 1
                    print(
                        f"⚡ [كشّاف الجمود {scouts_nudged_in_camp}/2] تحريك طفيف +$0.10 للمجموعة '{s_name}' لأن الحملة كاملة صرفت ${camp_total_spend_today:.2f} فقط: ${old_b} -> ${new_b}"
                    )
                except Exception:
                    pass


def recheck_gulf_adsquads(token: str):
    history = load_paused_history()
    stats_map = {st["id"]: st for st in fetch_account_breakdown_stats(token, TARGET_ACCOUNT_ID, "adsquad")}
    for sid, info in list(history["adsquads"].items()):
        if sid == "036257b8-c553-44cc-97ef-021ed38bc1a8":  # Copy of 14-10
            continue
        s = api_get(f"/adsquads/{sid}", token)["adsquads"][0]["adsquad"]
        c = api_get(f"/campaigns/{s['campaign_id']}", token)["campaigns"][0]["campaign"]
        if is_gulf_034_campaign(c["name"], s["name"], s):
            st = stats_map.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None})
            should_pause, _ = evaluate_adsquad_rule(s["name"], st, c["name"], s)
            if not should_pause:
                s["status"] = "ACTIVE"
                api_put(f"/campaigns/{s['campaign_id']}/adsquads", token, {"adsquads": [s]})
                del history["adsquads"][sid]
                save_paused_history(history)
                print(f"✅ أُعيد تشغيل مجموعة الخليج '{s['name']}' (في حملة '{c['name']}') لأن الصرف/CPA أقل من حد الخليج ($8 / $14)!")


if __name__ == "__main__":
    cfg = load_config()
    token = refresh_access_token(cfg)
    if "--recheck-gulf-squads" in sys.argv:
        recheck_gulf_adsquads(token)
    else:
        dry = "--dry-run" in sys.argv
        run_automation(token, TARGET_ACCOUNT_ID, dry_run=dry)
