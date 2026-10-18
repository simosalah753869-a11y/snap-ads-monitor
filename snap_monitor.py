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
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
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
    is_gulf = is_gulf_034_campaign(campaign_name, adsquad_name, squad)
    spend = st["spend"]
    purchases = st["purchases"]
    cpa = st["cpa"]

    if is_copy:
        if is_gulf:
            # دول (قطر، عمان، البحرين، الكويت): صرف > 8$ بدون مبيعة، أو سعر المبيعة >= 14$
            if spend > 8.0 and purchases == 0:
                return True, f"[Gulf Copy Rule] صرفت ${spend} (> $8) بدون مبيعات"
            if purchases > 0 and cpa is not None and cpa >= 14.0:
                return True, f"[Gulf Copy Rule] سعر البيعة CPA = ${cpa} (>= $14) | الصرف: ${spend} | المبيعات: {purchases}"
        else:
            # باقي الدول (السعودية وغيرها): صرف >= 5$ بدون مبيعة، أو سعر المبيعة >= 8$
            if spend >= 5.0 and purchases == 0:
                return True, f"[Copy Rule] صرفت ${spend} (>= $5) بدون مبيعات"
            if purchases > 0 and cpa is not None and cpa >= 8.0:
                return True, f"[Copy Rule] سعر البيعة CPA = ${cpa} (>= $8) | الصرف: ${spend} | المبيعات: {purchases}"
    else:
        if spend >= 14.0 and purchases == 0:
            return True, f"[Normal Rule] صرفت ${spend} (>= $14) بدون مبيعات"
        if purchases > 0 and cpa is not None and cpa >= 20.0:
            return True, f"[Normal Rule] سعر البيعة CPA = ${cpa} (>= $20) | الصرف: ${spend} | المبيعات: {purchases}"

    return False, ""


def evaluate_single_ad_rule(st: dict, campaign_name: str, adsquad_name: str, squad: dict | None = None) -> tuple[bool, str]:
    spend = st["spend"]
    swipes = st["swipes"]
    purchases = st["purchases"]
    cpa = st.get("cpa")
    cpc = st["cpc"]
    is_gulf = is_gulf_034_campaign(campaign_name, adsquad_name, squad)
    is_saudi = is_saudi_squad(squad, campaign_name, adsquad_name)
    max_cpc = 0.34 if is_gulf else 0.28
    max_cpa = 14.0 if is_gulf else 8.0

    # إذا حقق الفيديو مبيعات فعلية وكان سعر البيعة مربحاً (< $8 للسعودية أو < $14 للخليج)، لا نوقفه بسبب الـ CPC
    if purchases > 0 and cpa is not None and cpa < max_cpa:
        return False, ""

    # 1. قاعدة إيقاف الفيديو في حملات السعودية إذا صرف >= 6$ بدون مبيعة داخل المجموعة
    if is_saudi and spend >= 6.0 and purchases == 0:
        return True, f"[Saudi Single Video Stop-Loss] صرف الفيديو ${spend} (>= $6.00) بدون مبيعة"

    # 2. قاعدة إيقاف الـ CPC المرتفع بعد صرف >= 1$ (بدون مبيعات مربحة)
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

    # 1. One API call for all Ad Squad stats today (+ 2-day stats for delayed attribution check)
    squad_stats = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=0)
    squad_stats_map = {st["id"]: st for st in squad_stats}
    squad_stats_2d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=1)
    squad_stats_2d_map = {st["id"]: st for st in squad_stats_2d}
    spend_by_squad = {st["id"]: st["spend"] for st in squad_stats}
    print(f"📊 عدد المجموعات الإعلانية (Ad Squads) التي صرفت اليوم: {len(squad_stats)}")

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

        if s_id not in squad_cache:
            try:
                s_resp = api_get(f"/adsquads/{s_id}", token)
                squad_cache[s_id] = s_resp.get("adsquads", [{}])[0].get("adsquad", {})
            except Exception:
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
        if not should_pause:
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

    for st in squad_stats:
        s_id = st["id"]
        if st["spend"] < 5.0:
            continue

        s_resp = api_get(f"/adsquads/{s_id}", token)
        squad = s_resp.get("adsquads", [{}])[0].get("adsquad", {})
        if not squad:
            continue
        squad_cache[s_id] = squad
        if squad.get("status") != "ACTIVE":
            continue

        s_name = squad.get("name", "")
        c_id = squad.get("campaign_id", "")
        if c_id and c_id not in camp_names:
            c_resp = api_get(f"/campaigns/{c_id}", token)
            camp_names[c_id] = c_resp.get("campaigns", [{}])[0].get("campaign", {}).get("name", "")
        c_name = camp_names.get(c_id, "")

        if s_id in history["adsquads"]:
            print(f"⏭️ تجاهل المجموعة '{s_name}' (صرف: ${st['spend']}) — أعدتَ تشغيلها يدوياً (فرصة أخرى).")
            continue

        should_pause, reason = evaluate_adsquad_rule(s_name, st, c_name, squad)
        if should_pause:
            if dry_run:
                print(f"🔍 [مطابق للشرط] المجموعة: '{s_name}' (حملة: '{c_name}') | {reason}")
                if is_saudi_squad(squad, c_name, s_name):
                    print(f"   ♻️ [Dry-Run] سيتم فوراً استنساخ مجموعة بديلة مكان '{s_name}' لأنها في حملة سعودية (country_code='sa').")
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

                # إذا كانت المجموعة في حملة تستهدف السعودية (country_code == 'sa')، يتم فوراً إنشاء نسخة بديلة مكانها!
                if is_saudi_squad(squad, c_name, s_name):
                    try:
                        if clone_session is None:
                            clone_session = make_session(token)
                        print(f"♻️ جاري إنشاء نسخة بديلة فورية مكان '{s_name}' في حملة السعودية '{c_name}'...")
                        clone_adsquad_exact(clone_session, squad, 1)
                    except Exception as e:
                        print(f"⚠️ تعذر إنشاء نسخة فورية مكان '{s_name}': {e}")

    # 2. One API call for all Ad (video) stats today (+ 2-day stats for delayed attribution check)
    ad_stats = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=0)
    ad_stats_map = {st["id"]: st for st in ad_stats}
    ad_stats_2d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=1)
    ad_stats_2d_map = {st["id"]: st for st in ad_stats_2d}
    print(f"\n🎬 عدد الفيديوهات الإعلانية (Ads) التي صرفت اليوم: {len(ad_stats)}")

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

        try:
            a_resp = api_get(f"/ads/{ad_id}", token)
            ad = a_resp.get("ads", [{}])[0].get("ad", {})
        except Exception:
            continue
        if not ad:
            continue
        s_id = ad.get("ad_squad_id", "")
        if not s_id:
            continue
        if s_id not in squad_cache:
            try:
                s_resp = api_get(f"/adsquads/{s_id}", token)
                squad_cache[s_id] = s_resp.get("adsquads", [{}])[0].get("adsquad", {})
            except Exception:
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
        max_revive_cpa = 14.0 if is_gulf_034_campaign(c_name, s_name, squad) else 8.0
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

    for st in ad_stats:
        ad_id = st["id"]
        # نفحص أي إعلان صرف >= 1$ (سواء لفحص الـ CPC أو لفحص الـ 6$ بدون مبيعة في السعودية)
        if st["spend"] < 1.0:
            continue
        if st["spend"] < 6.0 and (st["swipes"] > 0 and st["cpc"] <= 0.28):
            continue

        a_resp = api_get(f"/ads/{ad_id}", token)
        ad = a_resp.get("ads", [{}])[0].get("ad", {})
        if not ad or ad.get("status") != "ACTIVE":
            continue

        ad_name = ad.get("name", "")
        s_id = ad.get("ad_squad_id")

        if s_id not in squad_cache:
            s_resp = api_get(f"/adsquads/{s_id}", token)
            squad_cache[s_id] = s_resp.get("adsquads", [{}])[0].get("adsquad", {})
        squad = squad_cache.get(s_id, {})
        s_name = squad.get("name", "")
        c_id = squad.get("campaign_id", "")
        if c_id and c_id not in camp_names:
            c_resp = api_get(f"/campaigns/{c_id}", token)
            camp_names[c_id] = c_resp.get("campaigns", [{}])[0].get("campaign", {}).get("name", "")
        c_name = camp_names.get(c_id, "")

        should_pause, reason = evaluate_single_ad_rule(st, c_name, s_name, squad)
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

    # 2B. فحص مجموع صرف ومبيعات كل فيديو (مكرر في عدة AdSquads) على مستوى الحملة بالكامل:
    # - السعودية (country_code == 'sa'):
    #     * مجموع الصرف >= 15$ بين كل الـ AdSquads مع 0 مبيعات -> إيقاف/حذف الفيديو من كل الـ AdSquads في الحملة.
    #     * مجموع المبيعات >= 1 وسعر البيعة الإجمالي (CPA) >= 18$ بين كل الـ AdSquads -> إيقاف/حذف الفيديو من كل الـ AdSquads في الحملة.
    # - الدول الأخرى غير السعودية (قطر، عمان، إلخ):
    #     * مجموع الصرف >= 13$ بين كل الـ AdSquads مع 0 مبيعات -> إيقاف/حذف الفيديو من كل الـ AdSquads في الحملة.
    #     * مجموع المبيعات >= 1 وسعر البيعة الإجمالي (CPA) >= 14$ بين كل الـ AdSquads -> إيقاف/حذف الفيديو من كل الـ AdSquads في الحملة.
    print(f"\n🧮 فحص مجموع صرف الفيديوهات المكررة عبر كل الـ AdSquads داخل الحملات النشطة ({len(active_campaigns)} حملة)...")
    camp_squads_cache = {}
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        sq_resp = api_get(f"/campaigns/{c_id}/adsquads", token)
        c_squads = [sq_w.get("adsquad", {}) for sq_w in sq_resp.get("adsquads", []) if sq_w.get("adsquad")]
        camp_squads_cache[c_id] = c_squads
        if not c_squads:
            continue

        for sq in c_squads:
            squad_cache[sq["id"]] = sq

        camp_is_saudi = is_saudi_squad(c_squads[0], c_name, c_squads[0].get("name", ""))
        no_sale_limit = 15.0 if camp_is_saudi else 13.0
        max_cpa_limit = 18.0 if camp_is_saudi else 14.0
        region_label = "السعودية" if camp_is_saudi else "غير السعودية/الخليج"

        ads_resp = api_get(f"/campaigns/{c_id}/ads", token)
        c_ads = [a_w.get("ad", {}) for a_w in ads_resp.get("ads", []) if a_w.get("ad")]
        if not c_ads:
            continue

        # معرفة الإعلانات التي صرفت اليوم داخل هذه الحملة لتحديد الفيديوهات التي تحتاج فحص
        spending_creatives = {a.get("creative_id") for a in c_ads if a.get("id") in ad_stats_map and a.get("creative_id")}
        if not spending_creatives:
            continue

        # ربط كل creative_id صرف اليوم بملف الفيديو الأصلي (top_snap_media_id)
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

        for v_key, grp in video_groups.items():
            tot_spend = grp["spend"]
            tot_purch = grp["purchases"]
            active_list = grp["active_ads"]
            if not active_list or tot_spend < no_sale_limit:
                continue

            tot_cpa = round(tot_spend / tot_purch, 2) if tot_purch > 0 else None
            kill_video = False
            kill_reason = ""

            if tot_spend >= no_sale_limit and tot_purch == 0:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video Stop-Loss - {region_label}] مجموع صرف الفيديو عبر كل الـ AdSquads = "
                    f"${tot_spend:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة"
                )
            elif tot_purch > 0 and tot_cpa is not None and tot_cpa >= max_cpa_limit:
                kill_video = True
                kill_reason = (
                    f"[Aggregated Video High CPA - {region_label}] مجموع صرف الفيديو = ${tot_spend:.2f} | "
                    f"المبيعات = {tot_purch} | سعر البيعة الإجمالي CPA = ${tot_cpa:.2f} (>= ${max_cpa_limit:.2f})"
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

    # 3. Night Bid Reduction (00:00 -> 06:00 GMT+3) & Daytime Bid Bump Rules:
    #    - في الليل (من 12:00 منتصف الليل حتى 6:00 صباحاً بتوقيت السعودية GMT+3):
    #      تنخفض المبيعات، لذا يتم تخفيض البيد تلقائياً بمقدار -$0.30 (بحد أدنى $1.50) مرة واحدة كل ليلة
    #      لجميع المجموعات الإعلانية النشطة (AdSquads) لمنع ارتفاع سعر المبيعة (CPA)، مع إيقاف أي رفع للبيد ليلاً.
    #    - في النهار (من 6:00 صباحاً حتى 11:59 مساءً بتوقيت السعودية GMT+3):
    #      زيادة +$0.10 كل ساعتين (2h) لأي AdSquad جديدة (أُنشئت خلال آخر 24 ساعة) طالما الصرف < $1.00 بحد أقصى 5 مرات مدى الحياة.
    now_utc = datetime.now(timezone.utc)
    today_local = datetime.now(ACCOUNT_TZ)
    today_date_str = today_local.strftime("%Y-%m-%d")
    is_night_window = (0 <= today_local.hour < 6)

    if is_night_window:
        print(
            f"\n🌙 الفترة الليلية النشطة الآن ({today_local.strftime('%H:%M')} GMT+3 — بين 12:00 منتصف الليل و 6:00 صباحاً): "
            f"فحص وتطبيق تخفيض البيد الليلي (-$0.30 بحد أدنى $1.50) لجميع المجموعات النشطة ({len(active_campaigns)} حملة)..."
        )
        night_reduced_total = 0
        for c in active_campaigns:
            c_id = c["id"]
            c_name = c.get("name", "")
            c_squads = camp_squads_cache.get(c_id)
            if c_squads is None:
                sq_resp = api_get(f"/campaigns/{c_id}/adsquads", token)
                c_squads = [sq_w.get("adsquad", {}) for sq_w in sq_resp.get("adsquads", []) if sq_w.get("adsquad")]

            squads_to_update = []
            pending_records = []
            for squad in c_squads:
                if not squad or squad.get("status") != "ACTIVE":
                    continue
                s_id = squad["id"]
                s_name = squad.get("name", "")
                bid_micro = squad.get("bid_micro")
                if not bid_micro or bid_micro <= 0:
                    continue

                # لا تخفّض البيد ليلاً للمجموعات المجدولة للبدء في المستقبل (مثل 7:00 صباحاً)
                sq_start_dt = parse_iso_utc(squad.get("start_time", ""))
                if sq_start_dt and sq_start_dt > now_utc:
                    continue

                night_key = f"{today_date_str}:{s_id}"
                if night_key in history["night_bid_reductions"]:
                    continue

                old_bid = round(bid_micro / 1_000_000, 2)
                if bid_micro <= 1_500_000:
                    # البيد بالفعل عند الحد الأدنى الآمن ($1.50 أو أقل)، نسجله حتى لا يُفحص مجدداً هذه الليلة
                    history["night_bid_reductions"][night_key] = {
                        "name": s_name,
                        "campaign": c_name,
                        "date": today_date_str,
                        "old_bid": old_bid,
                        "new_bid": old_bid,
                        "skipped_at_floor": True,
                        "reduced_at": now_str,
                    }
                    continue

                new_bid_micro = max(1_500_000, bid_micro - 300_000)  # -$0.30 مع حد أدنى $1.50
                new_bid = round(new_bid_micro / 1_000_000, 2)

                if dry_run:
                    print(
                        f"🔍 [تخفيض البيد ليلاً] المجموعة: '{s_name}' (حملة: '{c_name}') "
                        f"| البيد: ${old_bid} -> ${new_bid}"
                    )
                else:
                    squad["bid_micro"] = new_bid_micro
                    squads_to_update.append(squad)
                    pending_records.append((night_key, s_name, old_bid, new_bid))

            if not dry_run and squads_to_update:
                # تحديث المجموعات على دفعات (حتى 20 مجموعة في الطلب الواحد) للسرعة الفائقة
                for i in range(0, len(squads_to_update), 20):
                    batch = squads_to_update[i:i + 20]
                    rec_batch = pending_records[i:i + 20]
                    try:
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": batch})
                        for night_key, s_name, old_bid, new_bid in rec_batch:
                            history["night_bid_reductions"][night_key] = {
                                "name": s_name,
                                "campaign": c_name,
                                "date": today_date_str,
                                "old_bid": old_bid,
                                "new_bid": new_bid,
                                "reduced_at": now_str,
                            }
                            night_reduced_total += 1
                            print(
                                f"🌙 [تخفيض البيد ليلاً] تم إنقاص البيد (-$0.30) للمجموعة: '{s_name}' (حملة: '{c_name}') "
                                f"| البيد: ${old_bid} -> ${new_bid}"
                            )
                        save_paused_history(history)
                        time.sleep(0.2)
                    except Exception:
                        # في حال رفض التحديث الجماعي، نحدث كل مجموعة على حدة
                        for sq_item, (night_key, s_name, old_bid, new_bid) in zip(batch, rec_batch):
                            try:
                                api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [sq_item]})
                                history["night_bid_reductions"][night_key] = {
                                    "name": s_name,
                                    "campaign": c_name,
                                    "date": today_date_str,
                                    "old_bid": old_bid,
                                    "new_bid": new_bid,
                                    "reduced_at": now_str,
                                }
                                night_reduced_total += 1
                                print(
                                    f"🌙 [تخفيض البيد ليلاً] تم إنقاص البيد (-$0.30) للمجموعة: '{s_name}' (حملة: '{c_name}') "
                                    f"| البيد: ${old_bid} -> ${new_bid}"
                                )
                                time.sleep(0.2)
                            except Exception as inner_e:
                                print(f"⚠️ تعذر تخفيض البيد للمجموعة '{s_name}': {inner_e}")
                        save_paused_history(history)

        save_paused_history(history)
        print(f"✅ اكتمل فحص الفترة الليلية — إجمالي المجموعات التي تم تخفيض البيد لها هذه الليلة: {night_reduced_total} (وتم إيقاف رفع البيد ليلاً).")
        return

    is_before_6pm_window = (6 <= today_local.hour < 18)

    if is_before_6pm_window:
        print(
            f"\n⏱️ فحص زيادة البيد النهارية (قبل 6:00 مساءً 06:00-17:59) للمجموعات النشطة التي صرفت أقل من $1.00 "
            f"(الجديدة: كل ساعتين حتى 5 مرات دائماً) "
            f"(عدد الحملات النشطة: {len(active_campaigns)})..."
        )
        for c in active_campaigns:
            c_id = c["id"]
            c_name = c.get("name", "")
            c_squads = camp_squads_cache.get(c_id)
            if c_squads is None:
                sq_resp = api_get(f"/campaigns/{c_id}/adsquads", token)
                c_squads = [sq_w.get("adsquad", {}) for sq_w in sq_resp.get("adsquads", []) if sq_w.get("adsquad")]

            for squad in c_squads:
                if not squad or squad.get("status") != "ACTIVE":
                    continue
                s_id = squad["id"]
                s_name = squad.get("name", "")
                bid_micro = squad.get("bid_micro")
                if not bid_micro or bid_micro <= 0 or bid_micro >= 3_000_000:
                    continue

                created_dt = parse_iso_utc(squad.get("created_at", ""))
                start_dt = parse_iso_utc(squad.get("start_time", ""))
                candidates = [dt for dt in (created_dt, start_dt) if dt is not None]
                effective_start = max(candidates) if candidates else now_utc

                today_spend = spend_by_squad.get(s_id, 0.0)

                # (A) القاعدة الدائمة يومياً لأي AdSquad جديدة (خلال أول 24 ساعة من إنشائها، وقبل 6:00 مساءً فقط):
                #     زيادة +$0.10 كل ساعتين (2h) طالما الصرف < $1.00 بحد أقصى 5 مرات مدى الحياة.
                bump_rec = history["bid_bumps"].get(s_id, {"count": 0, "last_bump_at": None})
                last_bump_dt = parse_iso_utc(bump_rec.get("last_bump_at", ""))
                bump_count = bump_rec.get("count", 0)

                if today_spend >= 1.0:
                    if not bump_rec.get("done"):
                        bump_rec["done"] = True
                        bump_rec["name"] = s_name
                        bump_rec["campaign"] = c_name
                        history["bid_bumps"][s_id] = bump_rec
                        save_paused_history(history)
                    continue

                # Once a new Ad Squad reaches 5 bumps (or is marked done after spending >= $1), NEVER bump it again
                if bump_count >= 5 or bump_rec.get("done"):
                    continue

                # Only apply the 5x / 2-hour rule to NEW Ad Squads (created within the last 24 hours)
                if (now_utc - effective_start) > timedelta(hours=24):
                    continue

                if bump_count == 0 or last_bump_dt is None:
                    elapsed = now_utc - effective_start
                else:
                    elapsed = now_utc - last_bump_dt

                if elapsed < timedelta(hours=2):
                    continue

                old_bid = round(bid_micro / 1_000_000, 2)
                new_bid_micro = min(3_000_000, bid_micro + 100_000)  # +$0.10 (سقف $3.00)
                new_bid = round(new_bid_micro / 1_000_000, 2)
                new_count = bump_count + 1
                hrs_waited = round(elapsed.total_seconds() / 3600, 1)

                if dry_run:
                    print(
                        f"🔍 [مطابق لشرط زيادة البيد للمجموعة الجديدة ({new_count}/5 كل ساعتين)] المجموعة: '{s_name}' (حملة: '{c_name}') "
                        f"| الصرف: ${today_spend} (< $1) خلال {hrs_waited} ساعة | البيد: ${old_bid} -> ${new_bid}"
                    )
                else:
                    squad["bid_micro"] = new_bid_micro
                    api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                    history["bid_bumps"][s_id] = {
                        "name": s_name,
                        "campaign": c_name,
                        "date": today_date_str,
                        "count": new_count,
                        "done": new_count >= 5,
                        "old_bid": old_bid,
                        "new_bid": new_bid,
                        "last_bump_at": now_utc.isoformat(),
                        "last_bump_local": now_str,
                    }
                    save_paused_history(history)
                    print(
                        f"📈 [مجموعة جديدة] تم رفع البيد (+$0.10) للمرة ({new_count}/5 كل ساعتين) للمجموعة: '{s_name}' (حملة: '{c_name}') "
                        f"| الصرف: ${today_spend} خلال {hrs_waited} ساعة | البيد: ${old_bid} -> ${new_bid}"
                    )
                    time.sleep(0.3)
    else:
        print(
            f"\n⏸️ بعد الساعة 6:00 مساءً (الساعة الآن {today_local.strftime('%H:%M')} GMT+3 >= 18:00): "
            f"تم إيقاف زيادات البيد والميزانية لتجنب الزيادة ليلاً قرب انتهاء الـ 24 ساعة."
        )

    # 4. حارس حملات سمارت+ (Smart+ Spend Watchdog & Auto-Scaler) + استعادة البيد الصباحية:
    #    أ) استعادة البيد صباحاً (>= 6:00 ص) للمجموعات التي خُفّضت ليلاً حتى لا يموت صرفها نهاراً.
    #    ب) إنعاش أي مجموعة داخل حملات سمارت+ (قبل 6:00 مساءً فقط 06:00-17:59) إذا انعدم صرفها (< $1.50) أو توقف صرفها برفع البيد +$0.15 تلقائياً.
    #    ج) زيادة الميزانية اليومية (+30%) تلقائياً (قبل 6:00 مساءً فقط 06:00-17:59) لمجموعات سمارت+ الرابحة لمنع الزيادة الكبيرة بالليل بعد 6 مساءً.
    history.setdefault("smart_spend_watchdog", {})
    print(f"\n🛡️ فحص حارس حملات سمارت+ (استعادة البيد الصباحية + توسيع وإنعاش قبل 6:00 مساءً فقط)...")
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        c_squads = camp_squads_cache.get(c_id)
        if c_squads is None:
            sq_resp = api_get(f"/campaigns/{c_id}/adsquads", token)
            c_squads = [sq_w.get("adsquad", {}) for sq_w in sq_resp.get("adsquads", []) if sq_w.get("adsquad")]

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

            # يُمنع تماماً عمل أي زيادة في الميزانية أو البيد بعد الساعة 6:00 مساءً (>= 18:00) لأن الـ 24 ساعة قاربت على الانتهاء
            if not is_before_6pm_window:
                continue

            # التحقق مما إذا كانت المجموعة أو الحملة من نوع Smart+
            is_smart_squad = (
                "smart" in c_name.lower()
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

            # (ج) التوسع التلقائي للميزانية اليومية (+30%) لمجموعات سمارت+ الرابحة (قبل 6:00 مساءً فقط)
            daily_budget = budget_micro / 1_000_000 if budget_micro > 0 else 30.0
            if (
                not w_rec.get("scaled_budget_today")
                and st_sq.get("purchases", 0) >= 2
                and st_sq.get("cpa") is not None
                and st_sq["cpa"] <= 9.0
                and today_spend >= (daily_budget * 0.60)
                and daily_budget < 100.0
            ):
                new_budget_micro = min(100_000_000, int(round(budget_micro * 1.30)))
                if not dry_run:
                    squad["daily_budget_micro"] = new_budget_micro
                    try:
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                        w_rec["scaled_budget_today"] = True
                        save_paused_history(history)
                        print(
                            f"🚀 [توسيع تلقائي لميزانية سمارت+ الرابحة (قبل 6 مساءً)] المجموعة '{s_name}' (حملة: '{c_name}') "
                            f"| المبيعات: {st_sq['purchases']} بـ CPA=${st_sq['cpa']} | الميزانية: ${daily_budget:.0f} -> ${new_budget_micro/1e6:.0f}"
                        )
                    except Exception:
                        pass

            # (ب) إنعاش مجموعات سمارت+ إذا انعدم الصرف (< $1.50) أو توقف قبل 6:00 مساءً (< $0.40 خلال ساعتين)
            created_dt = parse_iso_utc(squad.get("created_at", ""))
            start_dt = parse_iso_utc(squad.get("start_time", ""))
            candidates = [dt for dt in (created_dt, start_dt) if dt is not None]
            effective_start = max(candidates) if candidates else (now_utc - timedelta(hours=3))
            if (now_utc - effective_start) < timedelta(hours=1):
                continue

            last_act_dt = parse_iso_utc(w_rec.get("last_action_at", ""))
            last_chk_dt = parse_iso_utc(w_rec.get("last_check_at", "")) or effective_start
            spend_delta = today_spend - float(w_rec.get("last_spend", 0.0))
            chk_elapsed = now_utc - last_chk_dt

            # تحديث قراءة الصرف الدورية كل ساعتين
            is_stalled_midday = (
                today_local.hour >= 8
                and chk_elapsed >= timedelta(hours=2)
                and spend_delta < 0.40
                and today_spend < (daily_budget * 0.70)
            )
            is_zero_or_low = (today_spend < 1.50)

            if chk_elapsed >= timedelta(hours=2):
                w_rec["last_spend"] = today_spend
                w_rec["last_check_at"] = now_utc.isoformat()
                save_paused_history(history)

            if not (is_zero_or_low or is_stalled_midday):
                continue

            if last_act_dt and (now_utc - last_act_dt) < timedelta(hours=2):
                continue
            if int(w_rec.get("bumps_today", 0)) >= 4:
                continue

            is_tc = squad.get("bid_strategy") == "TARGET_COST" or squad.get("target_bid") is True
            max_allowed_bid = 2_950_000 if is_tc else 2_650_000
            min_floor_bid = 2_600_000 if is_tc else 2_150_000

            # إذا كانت Target Cost وعلقت لأكثر من رفعتين مع صرف < $0.50، نحوّلها إلى Max Bid ($2.25) لتنطلق فوراً
            if is_tc and int(w_rec.get("bumps_today", 0)) >= 2 and today_spend < 0.50:
                if not dry_run:
                    squad["target_bid"] = False
                    squad["auto_bid"] = False
                    squad["bid_strategy"] = "LOWEST_COST_WITH_MAX_BID"
                    squad["bid_micro"] = 2_250_000
                    try:
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                        w_rec["bumps_today"] = int(w_rec.get("bumps_today", 0)) + 1
                        w_rec["last_action_at"] = now_utc.isoformat()
                        save_paused_history(history)
                        print(
                            f"⚡ [إنعاش سمارت+ المتوقفة] تحويل '{s_name}' (حملة: '{c_name}') من Target Cost إلى Max Bid ($2.25) لكسر جمود الصرف!"
                        )
                    except Exception:
                        pass
                continue

            if bid_micro <= 0 or bid_micro >= max_allowed_bid:
                continue

            new_bid_micro = max(min_floor_bid, min(max_allowed_bid, bid_micro + 150_000))
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
                    reason_lbl = "انعدام/ضعف الصرف (<$1.50)" if is_zero_or_low else f"توقف الصرف خلال ساعتين (+${spend_delta:.2f})"
                    print(
                        f"⚡ [إنعاش سمارت+] رفع البيد للمجموعة '{s_name}' (حملة: '{c_name}') بسبب {reason_lbl}: ${old_b} -> ${new_b}"
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
