from datetime import datetime, timedelta
import sys
import urllib.parse
from snap_auth import API_BASE, load_config, refresh_access_token, api_get
from snap_monitor import ACCOUNT_TZ, TARGET_ACCOUNT_ID
from duplicate_gulf_campaigns import clone_adsquad_exact, make_session

sys.stdout.reconfigure(encoding="utf-8")

AD_ACCOUNT_ID = "350cb560-ad77-4fb5-9bbf-4c090c9ad081"
MIN_ACTIVE_COUNT = 20

# الكلمات المفتاحية أو شروط حملات السكالينج والدوبليكيت
SCALING_KEYWORDS = ("scaling", "car", "qatar", "oman", "قطر", "عمان", "وينر", "winner", "kashaf", "kahsaf")


def get_7d_squad_stats(token: str) -> dict:
    now_local = datetime.now(ACCOUNT_TZ)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    week_start = today_start - timedelta(days=7)

    url = (
        f"/adaccounts/{TARGET_ACCOUNT_ID}/stats?granularity=TOTAL"
        f"&start_time={urllib.parse.quote(week_start.isoformat())}"
        f"&end_time={urllib.parse.quote(tomorrow_start.isoformat())}"
        f"&breakdown=adsquad&fields=impressions,swipes,spend,conversion_purchases"
    )
    resp = api_get(url, token)
    b_list = resp.get("total_stats", [{}])[0].get("total_stat", {}).get("breakdown_stats", {}).get("adsquad", [])
    stats_map = {}
    for item in b_list:
        st = item.get("stats", {})
        sp = round(st.get("spend", 0) / 1e6, 2)
        pu = st.get("conversion_purchases", 0)
        cpa = round(sp / pu, 2) if pu > 0 else 999999.0
        stats_map[item["id"]] = {"spend": sp, "purchases": pu, "cpa": cpa}
    return stats_map


def is_scaling_or_duplicate_campaign(cname: str, squads: list[dict], active_squads: list[dict]) -> bool:
    if not active_squads:
        return False
    has_copy = any("copy" in s.get("name", "").lower() for s in squads)
    if has_copy and len(squads) >= 5:
        return True
    name_low = cname.lower()
    if any(k in name_low for k in SCALING_KEYWORDS) and len(squads) >= 5:
        return True
    return False


def pick_best_source_squad(session, active_squads: list[dict], stats_map: dict) -> dict | None:
    sorted_candidates = sorted(
        active_squads,
        key=lambda s: (
            stats_map.get(s["id"], {}).get("purchases", 0),
            -stats_map.get(s["id"], {}).get("cpa", 999999.0),
            "copy" in s.get("name", "").lower(),
            stats_map.get(s["id"], {}).get("spend", 0.0),
        ),
        reverse=True,
    )
    for sq in sorted_candidates:
        ra = session.get(f"{API_BASE}/adsquads/{sq['id']}/ads", timeout=20)
        if ra.ok:
            ads = [a["ad"] for a in ra.json().get("ads", []) if "ad" in a]
            active_ads = [a for a in ads if a.get("status") == "ACTIVE"]
            if active_ads:
                return sq
    return sorted_candidates[0] if sorted_candidates else None


def main():
    token = refresh_access_token(load_config())
    session = make_session(token)
    stats_map = get_7d_squad_stats(token)

    rc = session.get(f"{API_BASE}/adaccounts/{AD_ACCOUNT_ID}/campaigns", timeout=20)
    rc.raise_for_status()
    campaigns = [c["campaign"] for c in rc.json().get("campaigns", []) if "campaign" in c]
    active_camps = [c for c in campaigns if c.get("status") == "ACTIVE"]

    print(f"🔍 فحص حملات السكالينج والدوبليكيت النشطة للتأكد من أن عدد المجموعات النشطة لا يقل عن {MIN_ACTIVE_COUNT}:", flush=True)
    for c in active_camps:
        cid = c["id"]
        cname = c["name"]
        rsq = session.get(f"{API_BASE}/campaigns/{cid}/adsquads", timeout=20)
        rsq.raise_for_status()
        squads = [s["adsquad"] for s in rsq.json().get("adsquads", []) if "adsquad" in s]
        active_squads = [s for s in squads if s.get("status") == "ACTIVE"]
        active_count = len(active_squads)

        if not is_scaling_or_duplicate_campaign(cname, squads, active_squads):
            continue

        print(f"\n=== حملة: '{cname}' (ID: {cid}) ===", flush=True)
        print(f"   📊 عدد المجموعات النشطة حالياً: {active_count} (الحد الأدنى المطلوب: {MIN_ACTIVE_COUNT})", flush=True)

        if active_count >= MIN_ACTIVE_COUNT:
            print(f"   ✅ الحملة تحتوي على {active_count} مجموعة نشطة (>= {MIN_ACTIVE_COUNT}) — لا تحتاج أي إضافة.", flush=True)
            continue

        needed = MIN_ACTIVE_COUNT - active_count
        source_squad = pick_best_source_squad(session, active_squads, stats_map)
        if not source_squad:
            print("   ⚠️ لا توجد أي مجموعة إعلانية صالحة للنسخ منها في هذه الحملة.", flush=True)
            continue

        st = stats_map.get(source_squad["id"], {"spend": 0.0, "purchases": 0, "cpa": None})
        print(
            f"   🚀 جاري استنساخ {needed} مجموعة من أفضل مجموعة رابحة ('{source_squad['name']}', مبيعات: {st['purchases']}, CPA: ${st['cpa']}) للوصول إلى {MIN_ACTIVE_COUNT} مجموعة نشطة...",
            flush=True,
        )
        clone_adsquad_exact(session, source_squad, needed)


if __name__ == "__main__":
    main()
