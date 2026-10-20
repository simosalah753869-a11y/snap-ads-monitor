from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
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

    # القاعدة العامة الموحدة (السعودية + دول الخليج): أي مجموعة إعلانية سعر البيعة فيها يتعدى 8$ يتم إيقافها فوراً
    if purchases > 0 and cpa is not None and cpa > 8.0:
        return True, f"[Max $8 CPA Rule] سعر البيعة CPA = ${cpa} (> $8.00) | الصرف: ${spend} | المبيعات: {purchases}"

    if is_copy:
        if spend >= 5.0 and purchases == 0:
            return True, f"[Copy Rule] صرفت ${spend} (>= $5.00) بدون مبيعات"
        if purchases > 0 and cpa is not None and cpa >= 8.0:
            return True, f"[Copy Rule] سعر البيعة CPA = ${cpa} (>= $8.00) | الصرف: ${spend} | المبيعات: {purchases}"
    else:
        if spend > 8.0 and purchases == 0:
            return True, f"[Normal Stop-Loss Rule] صرفت ${spend} (> $8.00) بدون مبيعات"

    return False, ""


def evaluate_single_ad_rule(st: dict, campaign_name: str, adsquad_name: str, squad: dict | None = None) -> tuple[bool, str]:
    spend = st["spend"]
    swipes = st["swipes"]
    purchases = st["purchases"]
    cpa = st.get("cpa")
    cpc = st["cpc"]
    is_gulf = is_gulf_034_campaign(campaign_name, adsquad_name, squad)
    max_cpc = 0.34 if is_gulf else 0.28
    max_cpa = 8.0

    # إذا حقق الفيديو مبيعات فعلية وكان سعر البيعة مربحاً (<= $8.00 في السعودية والخليج)، لا نوقفه بسبب الـ CPC
    if purchases > 0 and cpa is not None and cpa <= max_cpa:
        return False, ""

    # 1. إيقاف الفيديو في جميع الدول (السعودية والخليج) إذا تخطى CPA حاجز 8$
    if purchases > 0 and cpa is not None and cpa > max_cpa:
        return True, f"[Single Video Max $8 CPA Rule] صرف الفيديو ${spend} | المبيعات: {purchases} | CPA = ${cpa} (> $8.00)"

    # 2. قاعدة إيقاف الفيديو الموحدة (السعودية والخليج) إذا صرف >= 6$ بدون مبيعة داخل المجموعة
    if spend >= 6.0 and purchases == 0:
        return True, f"[Single Video Stop-Loss] صرف الفيديو ${spend} (>= $6.00) بدون مبيعة"

    # 3. قاعدة إيقاف الـ CPC المرتفع بعد صرف >= 1$ (بدون مبيعات مربحة)
    if spend >= 1.0:
        if swipes == 0:
            return True, f"[Ad CPC Rule] صرف الفيديو ${spend} بدون أي نقرة (الحد الأقصى ${max_cpc})"
        if cpc > max_cpc:
            return True, f"[Ad CPC Rule] صرف الفيديو ${spend} | النقرات: {swipes} | سعر النقرة CPC = ${cpc} (> ${max_cpc})"
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


def run_automation(token: str, ad_account_id: str, dry_run: bool = False):
    history = load_paused_history()
    now_str = datetime.now(ACCOUNT_TZ).strftime("%Y-%m-%d %H:%M GMT+3")
    clone_session = None

    camps_resp = api_get(f"/adaccounts/{ad_account_id}/campaigns", token)
    camp_names = {}
    active_campaigns = []
    for c_wrap in camps_resp.get("campaigns", []):
        c = c_wrap.get("campaign", {})
        if not c:
            continue
        camp_names[c["id"]] = c.get("name", "")
        if c.get("status") == "ACTIVE":
            active_campaigns.append(c)

    squad_cache = {}
    now_local_dt = datetime.now(ACCOUNT_TZ)
    today_d_str = now_local_dt.strftime("%Y-%m-%d")
    yesterday_d_str = (now_local_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    active_camp_ids = {c["id"] for c in active_campaigns}

    # 1. One API call for all Ad Squad stats today + 2-day + 3-day stats (and 3-day campaign stats to block duplication in >$8 CPA campaigns)
    squad_stats = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=0)
    squad_stats_map = {st["id"]: st for st in squad_stats}
    squad_stats_2d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=1)
    squad_stats_2d_map = {st["id"]: st for st in squad_stats_2d}
    squad_stats_3d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=2)
    squad_stats_3d_map = {st["id"]: st for st in squad_stats_3d}
    camp_stats_3d = fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=2)
    camp_stats_3d_map = {st["id"]: st for st in camp_stats_3d}

    no_dup_camps = history.setdefault("no_duplicate_campaigns", {})
    for cid in list(no_dup_camps.keys()):
        cst_chk = camp_stats_3d_map.get(cid)
        if not cst_chk:
            del no_dup_camps[cid]
        elif cst_chk.get("purchases", 0) > 0 and cst_chk.get("cpa") is not None and cst_chk["cpa"] <= 8.0:
            del no_dup_camps[cid]
        elif cst_chk.get("purchases", 0) == 0 and cst_chk.get("spend", 0) < 12.0:
            del no_dup_camps[cid]
    for cid, cst in camp_stats_3d_map.items():
        if (cst["spend"] >= 12.0 and cst["purchases"] == 0) or (cst["purchases"] > 0 and cst["cpa"] is not None and cst["cpa"] > 8.0):
            no_dup_camps[cid] = {
                "name": camp_names.get(cid, cid),
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
            sq_resp = api_get(f"/campaigns/{c_id}/adsquads?limit=200", token)
            c_squads = [sq_w.get("adsquad", {}) for sq_w in sq_resp.get("adsquads", []) if sq_w.get("adsquad")]
            camp_squads_cache[c_id] = c_squads
            for sq in c_squads:
                squad_cache[sq["id"]] = sq
        except Exception:
            camp_squads_cache[c_id] = []

    # 1A. مراجعة المجموعات التي أطفأها البوت مسبقاً: إذا ظهرت مبيعة متأخرة وانخفض سعر المبيعة (CPA) تحت الحد المسموح، يعيد البوت تشغيلها فوراً!
    for s_id, info in list(history["adsquads"].items()):
        paused_at_str = str(info.get("paused_at", ""))
        is_recent_pause = (today_d_str in paused_at_str) or (yesterday_d_str in paused_at_str)
        if s_id in squad_stats_map:
            st_check = squad_stats_map[s_id]
        elif is_recent_pause and s_id in squad_stats_2d_map:
            st_check = squad_stats_2d_map[s_id]
        else:
            continue

        if st_check.get("purchases", 0) <= 0:
            continue

        squad = squad_cache.get(s_id, {})
        if not squad:
            continue
        c_id = squad.get("campaign_id", "")
        if c_id not in active_camp_ids:
            continue

        s_name = squad.get("name", info.get("name", ""))
        c_name = camp_names.get(c_id, info.get("campaign", ""))
        should_pause, _ = evaluate_adsquad_rule(s_name, st_check, c_name, squad)
        st3_check = squad_stats_3d_map.get(s_id)
        should_pause_3d = False
        if st3_check:
            should_pause_3d, _ = evaluate_adsquad_rule(s_name, st3_check, c_name, squad)
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

    all_candidate_sids = set(squad_stats_map.keys()) | set(squad_stats_3d_map.keys())
    for s_id in all_candidate_sids:
        squad = squad_cache.get(s_id)
        if not squad or squad.get("status") != "ACTIVE":
            continue
        st_1d = squad_stats_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st_3d = squad_stats_3d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        if max(st_1d["spend"], st_3d["spend"]) < 5.0:
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

        should_pause_1d, reason_1d = evaluate_adsquad_rule(s_name, st_1d, c_name, squad) if st_1d["spend"] >= 5.0 else (False, "")
        should_pause_3d, reason_3d = evaluate_adsquad_rule(s_name, st_3d, c_name, squad) if st_3d["spend"] >= 5.0 else (False, "")
        should_pause = should_pause_1d or should_pause_3d
        reason = reason_1d if should_pause_1d else f"[3-Day Audit] {reason_3d}"

        if should_pause:
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

                # استنساخ بديل هادئ (قبل 5 مساءً فقط، وفي حملات السعودية الرابحة التي CPA آخر 3 أيام فيها <= $8.00، وبدون إغراق الحملة)
                active_in_camp = sum(1 for x in camp_squads_cache.get(c_id, []) if x.get("status") == "ACTIVE")
                if c_id in no_dup_camps:
                    print(f"🚫 تم منع عمل Duplicate بديل في حملة '{c_name}' لأن سعر البيعة في الحملة لآخر 3 أيام متعدي $8.00 ({no_dup_camps[c_id].get('cpa_3d')}).")
                elif not (6 <= now_local_dt.hour < 17):
                    print(f"⏸️ تخطي إنشاء Duplicate بديل في '{c_name}' لأننا خارج نافذة التوسع النهارية الآمنة (قبل 5:00 مساءً فقط).")
                elif is_gulf_034_campaign(c_name, s_name, squad) or active_in_camp >= 18:
                    print(f"ℹ️ اكتفاء بالمجموعات النشطة الحالية ({active_in_camp}) في '{c_name}' لمنع التزاحم وارتفاع التكلفة.")
                else:
                    try:
                        if clone_session is None:
                            clone_session = make_session(token)
                        print(f"♻️ جاري إنشاء نسخة بديلة هادئة مكان '{s_name}' في حملة '{c_name}'...")
                        clone_adsquad_exact(clone_session, squad, 1)
                    except Exception as e:
                        print(f"⚠️ تعذر إنشاء نسخة فورية مكان '{s_name}': {e}")

    # 2. One API call for all Ad (video) stats today (+ 2-day, 3-day, and 7-day stats for multi-day accumulated spend check)
    ad_stats = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=0)
    ad_stats_map = {st["id"]: st for st in ad_stats}
    ad_stats_2d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=1)
    ad_stats_2d_map = {st["id"]: st for st in ad_stats_2d}
    ad_stats_3d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=2)
    ad_stats_3d_map = {st["id"]: st for st in ad_stats_3d}
    ad_stats_7d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=6)
    ad_stats_7d_map = {st["id"]: st for st in ad_stats_7d}
    print(f"\n🎬 عدد الفيديوهات الإعلانية (Ads) التي صرفت اليوم: {len(ad_stats)} (وخلال 3 أيام: {len(ad_stats_3d)})")

    ad_cache = {}
    camp_ads_cache = {}
    for c in active_campaigns:
        c_id = c["id"]
        try:
            ads_resp = api_get(f"/campaigns/{c_id}/ads?limit=500", token)
            c_ads = [a_w.get("ad", {}) for a_w in ads_resp.get("ads", []) if a_w.get("ad")]
            camp_ads_cache[c_id] = c_ads
            for a in c_ads:
                ad_cache[a["id"]] = a
        except Exception:
            camp_ads_cache[c_id] = []

    # 2A. مراجعة الفيديوهات الفردية التي أطفأها البوت: إذا ظهرت مبيعة متأخرة وكان سعر البيعة مربحاً (< $8 للسعودية أو < $14 للخليج)، يعيد تشغيلها!
    for ad_id, info in list(history["ads"].items()):
        paused_at_str = str(info.get("paused_at", ""))
        is_recent_pause = (today_d_str in paused_at_str) or (yesterday_d_str in paused_at_str)
        if ad_id in ad_stats_map:
            st_check = ad_stats_map[ad_id]
        elif is_recent_pause and ad_id in ad_stats_2d_map:
            st_check = ad_stats_2d_map[ad_id]
        else:
            continue

        if st_check.get("purchases", 0) <= 0 or st_check.get("cpa") is None:
            continue

        ad = ad_cache.get(ad_id, {})
        if not ad:
            continue
        s_id = ad.get("ad_squad_id", "")
        if not s_id:
            continue
        squad = squad_cache.get(s_id, {})
        if not squad or squad.get("status") != "ACTIVE":
            continue
        c_id = squad.get("campaign_id", "")
        if c_id not in active_camp_ids:
            continue

        s_name = squad.get("name", "")
        c_name = camp_names.get(c_id, info.get("campaign", ""))
        ad_name = ad.get("name", info.get("name", ""))
        cr_id_chk = ad.get("creative_id", "")
        v_key_chk = history.get("creative_media_map", {}).get(cr_id_chk) or cr_id_chk
        disabled_vids = history.get("disabled_videos", {})
        if (
            f"{c_id}:{v_key_chk}" in disabled_vids
            or f"{c_id}:{cr_id_chk}" in disabled_vids
            or f"{c_id}:{ad_name.strip()}" in disabled_vids
            or "Aggregated Video" in str(info.get("reason", ""))
            or any(
                dv.get("campaign_id") == c_id and dv.get("sample_ad_name", "").strip() == ad_name.strip()
                for dv in disabled_vids.values()
            )
        ):
            # Never revive a video that was killed across all AdSquads!
            continue
        max_revive_cpa = 8.0
        if st_check["cpa"] < max_revive_cpa:
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
                    save_paused_history(history)
                    print(
                        f"♻️🔥 [إعادة تشغيل فيديو — مبيعة متأخرة] تم إعادة تشغيل الفيديو: '{ad_name}' (مجموعة: '{s_name}' / حملة: '{c_name}') "
                        f"بعد ظهور مبيعات ({st_check['purchases']}) بـ CPA = ${st_check['cpa']} (الصرف: ${st_check['spend']})!"
                    )
                    time.sleep(0.3)
            elif ad.get("status") == "ACTIVE" and not dry_run:
                del history["ads"][ad_id]
                save_paused_history(history)

    all_candidate_ad_ids = set(ad_stats_map.keys()) | set(ad_stats_3d_map.keys()) | set(ad_stats_7d_map.keys())
    for ad_id in all_candidate_ad_ids:
        st = ad_stats_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st3 = ad_stats_3d_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st7 = ad_stats_7d_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})

        ad = ad_cache.get(ad_id, {})
        if not ad or ad.get("status") != "ACTIVE":
            continue

        ad_name = ad.get("name", "")
        s_id = ad.get("ad_squad_id")
        squad = squad_cache.get(s_id, {})
        if not squad or squad.get("status") != "ACTIVE":
            continue
        s_name = squad.get("name", "")
        c_id = squad.get("campaign_id", "")
        c_name = camp_names.get(c_id, "")

        should_pause, reason = evaluate_single_ad_rule(st, c_name, s_name, squad) if st["spend"] >= 1.0 else (False, "")
        if not should_pause and st3["spend"] >= 6.0:
            should_pause, reason = evaluate_single_ad_rule(st3, c_name, s_name, squad)
            if should_pause:
                reason = f"[3-Day Audit] {reason}"
        if not should_pause and st7["spend"] >= 6.0 and st7["purchases"] == 0:
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
            save_paused_history(history)
            print(f"🛑 تم إيقاف الفيديو: '{ad_name}' (حملة: '{c_name}' / مجموعة: '{s_name}') | {reason}")
            time.sleep(0.3)

    # 2B. فحص مجموع صرف ومبيعات كل فيديو (مكرر في عدة AdSquads) على مستوى الحملة بالكامل (اليوم + 3 أيام + 7 أيام):
    print(f"\n🧮 فحص مجموع صرف الفيديوهات المكررة عبر كل الـ AdSquads داخل الحملات النشطة ({len(active_campaigns)} حملة - اليوم + 3 أيام + 7 أيام)...")
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        c_squads = camp_squads_cache.get(c_id, [])
        if not c_squads:
            continue

        camp_is_saudi = is_saudi_squad(c_squads[0], c_name, c_squads[0].get("name", ""))
        no_sale_limit = 25.0
        max_cpa_limit = 22.0
        region_label = "السعودية" if camp_is_saudi else "الخليج"

        c_ads = camp_ads_cache.get(c_id, [])
        if not c_ads:
            continue

        spending_creatives = {
            a.get("creative_id")
            for a in c_ads
            if (a.get("id") in ad_stats_map or a.get("id") in ad_stats_3d_map or a.get("id") in ad_stats_7d_map)
            and a.get("creative_id")
        }
        if not spending_creatives:
            continue

        name_to_media = {}
        for cr_id in spending_creatives:
            m_id = resolve_video_media_id(cr_id, token, history)
            for a in c_ads:
                if a.get("creative_id") == cr_id and a.get("name"):
                    name_to_media[a["name"].strip()] = m_id
        save_paused_history(history)

        video_groups = {}
        for a in c_ads:
            cr_id = a.get("creative_id")
            ad_nm = (a.get("name") or "").strip()
            v_key = (
                history["creative_media_map"].get(cr_id)
                or name_to_media.get(ad_nm)
                or ad_nm
                or cr_id
                or "unknown"
            )
            grp = video_groups.setdefault(v_key, {
                "ads": [],
                "active_ads": [],
                "spend": 0.0,
                "purchases": 0,
                "swipes": 0,
                "spend_3d": 0.0,
                "purchases_3d": 0,
                "spend_7d": 0.0,
                "purchases_7d": 0,
                "sample_name": a.get("name", ""),
            })
            grp["ads"].append(a)
            if a.get("status") == "ACTIVE":
                grp["active_ads"].append(a)
            st = ad_stats_map.get(a["id"])
            if st:
                grp["spend"] = round(grp["spend"] + st["spend"], 2)
                grp["purchases"] += st["purchases"]
                grp["swipes"] += st["swipes"]
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
            sp3, pu3 = grp["spend_3d"], grp["purchases_3d"]
            sp7, pu7 = grp["spend_7d"], grp["purchases_7d"]
            active_list = grp["active_ads"]
            if not active_list:
                continue

            already_permanently_disabled = (
                f"{c_id}:{v_key}" in history.get("disabled_videos", {})
                or any(
                    dv.get("campaign_id") == c_id
                    and (
                        dv.get("video_media_id") == v_key
                        or (grp["sample_name"] and dv.get("sample_ad_name", "").strip() == grp["sample_name"].strip())
                    )
                    for dv in history.get("disabled_videos", {}).values()
                )
            )
            if not already_permanently_disabled and max(tot_spend, sp3, sp7) < no_sale_limit:
                continue

            tot_cpa = round(tot_spend / tot_purch, 2) if tot_purch > 0 else None
            cpa3 = round(sp3 / pu3, 2) if pu3 > 0 else None
            cpa7 = round(sp7 / pu7, 2) if pu7 > 0 else None

            # حماية الفيديوهات الرابحة: لا نعدم فيديو على مستوى كل المجموعات إذا كان محققاً لمبيعات رابحة في آخر 3 أو 7 أيام
            if (pu7 >= 2 and cpa7 is not None and cpa7 <= 14.0) or (pu3 >= 1 and cpa3 is not None and cpa3 <= 14.0) or (tot_purch >= 1 and tot_cpa is not None and tot_cpa <= 14.0):
                if f"{c_id}:{v_key}" in history.get("disabled_videos", {}):
                    del history["disabled_videos"][f"{c_id}:{v_key}"]
                    save_paused_history(history)
                continue

            kill_video = False
            kill_reason = ""
            if already_permanently_disabled:
                kill_video = True
                kill_reason = (
                    f"[Permanent Disabled Video Lock - {region_label}] هذا الفيديو موقوف نهائياً في هذه الحملة ولن يُسمح بإعادة تشغيله في أي AdSquad"
                )

            if tot_spend >= no_sale_limit and tot_purch == 0 and pu3 == 0 and pu7 == 0:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video Stop-Loss 1D - {region_label}] مجموع صرف الفيديو اليوم عبر كل الـ AdSquads = "
                    f"${tot_spend:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة"
                )
            elif sp3 >= no_sale_limit and pu3 == 0 and pu7 == 0:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video Stop-Loss 3D - {region_label}] مجموع صرف الفيديو لـ 3 أيام عبر كل الـ AdSquads = "
                    f"${sp3:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة"
                )
            elif sp7 >= no_sale_limit and pu7 == 0:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video Stop-Loss 7D - {region_label}] مجموع صرف الفيديو لـ 7 أيام عبر كل الـ AdSquads = "
                    f"${sp7:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة"
                )

            if not kill_video:
                continue

            if dry_run:
                print(
                    f"🔍 [حذف/إيقاف الفيديو من كل الـ AdSquads] الحملة: '{c_name}' | الفيديو: '{grp['sample_name']}' (Media: {v_key}) "
                    f"| عدد النسخ النشطة التي ستُغلق: {len(active_list)} | {kill_reason}"
                )
            else:
                print(
                    f"🗑️ جاري إيقاف/إزالة الفيديو '{grp['sample_name']}' (Media: {v_key}) من جميع الـ AdSquads ({len(active_list)} مجموعة) في حملة '{c_name}' | {kill_reason}"
                )
                # تجميع الإعلانات النشطة حسب الـ AdSquad لإغلاقها دفعة واحدة لكل مجموعة
                ads_by_squad = {}
                for a in active_list:
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

                history["disabled_videos"][f"{c_id}:{v_key}"] = {
                    "campaign_id": c_id,
                    "campaign_name": c_name,
                    "video_media_id": v_key,
                    "sample_ad_name": grp["sample_name"],
                    "closed_instances": closed_cnt,
                    "total_spend": tot_spend,
                    "total_purchases": tot_purch,
                    "reason": kill_reason,
                    "disabled_at": now_str,
                }
                save_paused_history(history)
                print(f"   ✅ تم إيقاف/حذف الفيديو من {closed_cnt} مجموعة إعلانية بنجاح!")

    # 2C. تنظيف المجموعات الإعلانية الميتة التي أصبحت بدون أي فيديو نشط (INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS)
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        c_squads = camp_squads_cache.get(c_id)
        if not c_squads:
            continue
        dead_to_pause = [
            sq for sq in c_squads
            if sq.get("status") == "ACTIVE"
            and "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" in (sq.get("delivery_status") or [])
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

    # 3. Daytime Bid Bump Rules (06:00 -> 17:59 GMT+3 Only — Night Bid Reduction disabled permanently so sales never choke at night):
    now_utc = datetime.now(timezone.utc)
    today_local = datetime.now(ACCOUNT_TZ)
    today_date_str = today_local.strftime("%Y-%m-%d")
    is_night_window = (0 <= today_local.hour < 6)

    if is_night_window:
        print(
            f"\n🌙 الفترة الليلية ({today_local.strftime('%H:%M')} GMT+3): "
            f"الإبقاء على البيدات الطبيعية بدون تخفيض ليلي للحفاظ على استمرار المبيعات، مع إيقاف زيادات البيد حتى 6:00 صباحاً."
        )
        return

    is_before_6pm_window = (6 <= today_local.hour < 17)

    if is_before_6pm_window:
        print(
            f"\n⏱️ فحص زيادة البيد النهارية الهادئة (06:00-16:59 فقط) للمجموعات النشطة التي صرفت أقل من $0.80 "
            f"(بحد أقصى مرتين فقط وبسقف آمن لمنع الخسارة وارتفاع الـ CPC) "
            f"(عدد الحملات النشطة: {len(active_campaigns)})..."
        )
        for c in active_campaigns:
            c_id = c["id"]
            c_name = c.get("name", "")
            c_squads = camp_squads_cache.get(c_id)
            if c_squads is None:
                sq_resp = api_get(f"/campaigns/{c_id}/adsquads", token)
                c_squads = [sq_w.get("adsquad", {}) for sq_w in sq_resp.get("adsquads", []) if sq_w.get("adsquad")]

            # إذا كانت الحملة تصرف بشكل طبيعي اليوم (>= $4.00)، لا نضغط برفع البيد لنسخ الـ Copy داخلها
            camp_today_spend = sum(spend_by_squad.get(sq.get("id", ""), 0.0) for sq in c_squads if sq)
            bumps_in_this_camp = 0

            for squad in c_squads:
                if not squad or squad.get("status") != "ACTIVE":
                    continue
                if bumps_in_this_camp >= 2:
                    break
                s_id = squad["id"]
                s_name = squad.get("name", "")
                if "copy" in s_name.lower() and camp_today_spend >= 4.0:
                    continue
                bid_micro = squad.get("bid_micro")
                is_gulf_sq = is_gulf_034_campaign(c_name, s_name, squad)
                bid_cap_micro = 2_750_000 if is_gulf_sq else 2_500_000
                if not bid_micro or bid_micro <= 0 or bid_micro >= bid_cap_micro:
                    continue

                created_dt = parse_iso_utc(squad.get("created_at", ""))
                start_dt = parse_iso_utc(squad.get("start_time", ""))
                candidates = [dt for dt in (created_dt, start_dt) if dt is not None]
                effective_start = max(candidates) if candidates else now_utc

                today_spend = spend_by_squad.get(s_id, 0.0)

                # زيادة هادئة (+$0.08 كل 4 ساعات) بحد أقصى مرتين فقط للمجموعات الجديدة المتوقفة تماماً (< $0.80)
                bump_rec = history["bid_bumps"].get(s_id, {"count": 0, "last_bump_at": None})
                last_bump_dt = parse_iso_utc(bump_rec.get("last_bump_at", ""))
                bump_count = bump_rec.get("count", 0)

                if today_spend >= 0.80:
                    if not bump_rec.get("done"):
                        bump_rec["done"] = True
                        bump_rec["name"] = s_name
                        bump_rec["campaign"] = c_name
                        history["bid_bumps"][s_id] = bump_rec
                        save_paused_history(history)
                    continue

                if bump_count >= 2 or bump_rec.get("done"):
                    continue

                if (now_utc - effective_start) > timedelta(hours=24):
                    continue

                if bump_count == 0 or last_bump_dt is None:
                    elapsed = now_utc - effective_start
                else:
                    elapsed = now_utc - last_bump_dt

                if elapsed < timedelta(hours=4):
                    continue

                old_bid = round(bid_micro / 1_000_000, 2)
                new_bid_micro = min(bid_cap_micro, bid_micro + 80_000)  # +$0.08 فقط
                new_bid = round(new_bid_micro / 1_000_000, 2)
                new_count = bump_count + 1
                hrs_waited = round(elapsed.total_seconds() / 3600, 1)

                if dry_run:
                    print(
                        f"🔍 [مطابق لشرط زيادة البيد الهادئة ({new_count}/2)] المجموعة: '{s_name}' (حملة: '{c_name}') "
                        f"| الصرف: ${today_spend} (< $0.80) خلال {hrs_waited} ساعة | البيد: ${old_bid} -> ${new_bid}"
                    )
                else:
                    squad["bid_micro"] = new_bid_micro
                    api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                    history["bid_bumps"][s_id] = {
                        "name": s_name,
                        "campaign": c_name,
                        "date": today_date_str,
                        "count": new_count,
                        "done": new_count >= 2,
                        "old_bid": old_bid,
                        "new_bid": new_bid,
                        "last_bump_at": now_utc.isoformat(),
                        "last_bump_local": now_str,
                    }
                    save_paused_history(history)
                    bumps_in_this_camp += 1
                    print(
                        f"📈 [مجموعة جديدة - تحريك هادئ] تم رفع البيد (+$0.08) للمرة ({new_count}/2) للمجموعة: '{s_name}' (حملة: '{c_name}') "
                        f"| الصرف: ${today_spend} خلال {hrs_waited} ساعة | البيد: ${old_bid} -> ${new_bid}"
                    )
                    time.sleep(0.3)
    else:
        print(
            f"\n⏸️ خارج نافذة الزيادة النهارية الآمنة (الساعة الآن {today_local.strftime('%H:%M')} GMT+3 >= 17:00): "
            f"تم إيقاف زيادات البيد والميزانية والاستنساخ لحماية الربح وعدم الضغط على الحملات ليلاً."
        )

    # 4. حارس حملات سمارت+ والخليج (استعادة البيد الصباحية + تحريك هادئ للحملات المتوقفة تماماً فقط بدون ضغط):
    history.setdefault("smart_spend_watchdog", {})
    print(f"\n🛡️ فحص حارس حملات سمارت+ ودول الخليج (استعادة البيد الصباحية + توسع آمن قبل 5:00 مساءً فقط)...")
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        c_squads = camp_squads_cache.get(c_id)
        if c_squads is None:
            sq_resp = api_get(f"/campaigns/{c_id}/adsquads", token)
            c_squads = [sq_w.get("adsquad", {}) for sq_w in sq_resp.get("adsquads", []) if sq_w.get("adsquad")]

        camp_total_spend_today = sum(spend_by_squad.get(sq.get("id", ""), 0.0) for sq in c_squads if sq)
        watchdog_nudged_in_camp = 0

        for squad in c_squads:
            if not squad or squad.get("status") != "ACTIVE":
                continue
            s_id = squad["id"]
            s_name = squad.get("name", "")
            bid_micro = squad.get("bid_micro") or 0
            budget_micro = squad.get("daily_budget_micro") or 0
            today_spend = spend_by_squad.get(s_id, 0.0)
            st_sq = squad_stats_map.get(s_id, {"spend": today_spend, "purchases": 0, "cpa": None})

            # (أ) استعادة البيد الصباحية بعد الساعة 6:00 صباحاً للمجموعات المخفّضة ليلاً
            night_key = f"{today_date_str}:{s_id}"
            n_rec = history["night_bid_reductions"].get(night_key)
            if n_rec and not n_rec.get("restored") and not n_rec.get("skipped_at_floor") and bid_micro > 0:
                target_restore_micro = int(round(float(n_rec.get("old_bid", 2.15)) * 1_000_000))
                if bid_micro < target_restore_micro:
                    old_b = round(bid_micro / 1_000_000, 2)
                    new_b = round(target_restore_micro / 1_000_000, 2)
                    if not dry_run:
                        squad["bid_micro"] = target_restore_micro
                        try:
                            api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                            n_rec["restored"] = True
                            n_rec["restored_at"] = now_str
                            save_paused_history(history)
                            bid_micro = target_restore_micro
                            print(f"☀️ [استعادة البيد صباحاً] المجموعة '{s_name}' (حملة: '{c_name}'): ${old_b} -> ${new_b}")
                        except Exception:
                            pass

            # يُمنع تماماً عمل أي زيادة في الميزانية أو البيد بعد الساعة 5:00 مساءً (>= 17:00)
            if not is_before_6pm_window:
                continue

            is_gulf_sq = is_gulf_034_campaign(c_name, s_name, squad)
            is_smart_squad = (
                is_gulf_sq
                or "smart" in c_name.lower()
                or "سمارت" in c_name
                or "smart" in s_name.lower()
                or "سمارت" in s_name
            )
            if not is_smart_squad:
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

            # (ج) التوسع الهادئ للميزانية اليومية (+20% فقط) للمجموعات السوبر رابحة (CPA <= $6.50 وقبل 5:00 مساءً فقط)
            daily_budget = budget_micro / 1_000_000 if budget_micro > 0 else 30.0
            if (
                not w_rec.get("scaled_budget_today")
                and c_id not in no_dup_camps
                and st_sq.get("purchases", 0) >= 2
                and st_sq.get("cpa") is not None
                and st_sq["cpa"] <= 6.50
                and today_spend >= (daily_budget * 0.60)
                and daily_budget < 80.0
            ):
                new_budget_micro = min(80_000_000, int(round(budget_micro * 1.20)))
                if not dry_run:
                    squad["daily_budget_micro"] = new_budget_micro
                    try:
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                        w_rec["scaled_budget_today"] = True
                        save_paused_history(history)
                        print(
                            f"🚀 [توسيع هادئ للميزانية +20% (قبل 5 مساءً)] المجموعة '{s_name}' (حملة: '{c_name}') "
                            f"| المبيعات: {st_sq['purchases']} بـ CPA=${st_sq['cpa']} | الميزانية: ${daily_budget:.0f} -> ${new_budget_micro/1e6:.0f}"
                        )
                    except Exception:
                        pass

            # (ب) عدم الضغط بالبيد نهائياً إذا كانت الحملة تصرف بالفعل (>= $2.50 إجمالي اليوم) أو إذا كانت المجموعة نسخة Copy
            if camp_total_spend_today >= 2.50 or watchdog_nudged_in_camp >= 1 or "copy" in s_name.lower():
                continue

            created_dt = parse_iso_utc(squad.get("created_at", ""))
            start_dt = parse_iso_utc(squad.get("start_time", ""))
            candidates = [dt for dt in (created_dt, start_dt) if dt is not None]
            effective_start = max(candidates) if candidates else (now_utc - timedelta(hours=4))
            if (now_utc - effective_start) < timedelta(hours=3):
                continue

            last_act_dt = parse_iso_utc(w_rec.get("last_action_at", ""))
            if last_act_dt and (now_utc - last_act_dt) < timedelta(hours=4):
                continue
            if int(w_rec.get("bumps_today", 0)) >= 2:
                continue

            max_allowed_bid = 2_750_000 if is_gulf_sq else 2_550_000
            if bid_micro <= 0 or bid_micro >= max_allowed_bid:
                continue

            new_bid_micro = min(max_allowed_bid, bid_micro + 80_000)
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
                    watchdog_nudged_in_camp += 1
                    print(
                        f"⚡ [تحريك هادئ لحملة متوقفة تماماً] رفع طفيف للبيد (+$0.08) للمجموعة '{s_name}' (حملة: '{c_name}'): ${old_b} -> ${new_b}"
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
