from datetime import datetime, timedelta
import sys
import urllib.parse
from snap_auth import API_BASE, load_config, refresh_access_token, api_get
from snap_monitor import ACCOUNT_TZ, TARGET_ACCOUNT_ID, load_paused_history, fetch_account_breakdown_stats
from duplicate_gulf_campaigns import clone_adsquad_exact, make_session

sys.stdout.reconfigure(encoding="utf-8")

AD_ACCOUNT_ID = "350cb560-ad77-4fb5-9bbf-4c090c9ad081"
MIN_ACTIVE_COUNT = 15

# الكلمات المفتاحية لحملات السكالينج والدوبليكيت (السعودية + دول الخليج بنفس الطريقة، مع حظر كشاف السعودية فقط)
SCALING_KEYWORDS = ("scaling", "car", "وينر", "winner", "kashaf qatar", "كشاف قطر")
BANNED_KASHAF = ("kashaf", "kahsaf", "كشاف")


def is_banned_saudi_kashaf(cname: str) -> bool:
    low = (cname or "").lower()
    if any(b in low for b in BANNED_KASHAF):
        if "qatar" in low or "قطر" in low or "qat" in low:
            return False
        return True
    return False


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
    name_low = cname.lower()
    if is_banned_saudi_kashaf(name_low):
        return False
    # استثناء حملات الخليج الصغرى وحملات الاختبار من التوب-أب إلى 20 مجموعة لمنع التزاحم والخسارة
    if any(k in name_low for k in ("عمان", "قطر", "كويت", "بحرين", "oman", "qatar", "kuwait", "bahrain", "علا وحنان", "دفعة", "super winners", "gems")):
        return False
    has_copy = any("copy" in s.get("name", "").lower() for s in active_squads)
    if has_copy and len(active_squads) >= 5:
        return True
    if any(k in name_low for k in SCALING_KEYWORDS) and len(active_squads) >= 5:
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
    history = load_paused_history()
    no_dup_camps = history.get("no_duplicate_campaigns", {})
    camp_3d_stats = {x["id"]: x for x in fetch_account_breakdown_stats(token, AD_ACCOUNT_ID, "campaign", days_back=2)}

    rc = session.get(f"{API_BASE}/adaccounts/{AD_ACCOUNT_ID}/campaigns", timeout=20)
    rc.raise_for_status()
    campaigns = [c["campaign"] for c in rc.json().get("campaigns", []) if "campaign" in c]
    active_camps = [c for c in campaigns if c.get("status") == "ACTIVE"]

    now_sa = datetime.now(ACCOUNT_TZ)
    allow_topup_window = (6 <= now_sa.hour < 17)
    print(f"🔍 فحص حملات السكالينج النشطة (التوقيت: {now_sa.strftime('%H:%M')} GMT+3 | التوسع الهادئ قبل 5 مساءً: {'مفعل ✅' if allow_topup_window else 'موقوف لحماية الربح ليلاً ⏸️'}):", flush=True)
    for c in active_camps:
        cid = c["id"]
        cname = c["name"]
        cst3 = camp_3d_stats.get(cid, {"spend": 0.0, "purchases": 0, "cpa": None})
        is_cpa_over_8 = (
            cid in no_dup_camps
            or (cst3["spend"] >= 12.0 and cst3["purchases"] == 0)
            or (cst3["purchases"] > 0 and cst3["cpa"] is not None and cst3["cpa"] > 8.0)
        )
        rsq = session.get(f"{API_BASE}/campaigns/{cid}/adsquads", timeout=20)
        rsq.raise_for_status()
        squads = [s["adsquad"] for s in rsq.json().get("adsquads", []) if "adsquad" in s]
        raw_active_squads = [s for s in squads if s.get("status") == "ACTIVE"]

        # Automatically pause any "Zombie" ACTIVE AdSquad that has 0 active ads (INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS)
        dead_squads = [
            s for s in raw_active_squads
            if "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" in (s.get("delivery_status") or [])
        ]
        if dead_squads:
            for ds in dead_squads:
                ds["status"] = "PAUSED"
            for i in range(0, len(dead_squads), 20):
                session.put(f"{API_BASE}/campaigns/{cid}/adsquads", json={"adsquads": dead_squads[i:i+20]}, timeout=25)
            print(f"   🛑 تم إيقاف {len(dead_squads)} مجموعة ميتة (بدون إعلانات نشطة) في حملة '{cname}'", flush=True)

        active_squads = [
            s for s in raw_active_squads
            if "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" not in (s.get("delivery_status") or [])
        ]
        active_count = len(active_squads)

        name_low = cname.lower()
        is_gulf_camp = any(k in name_low for k in ("عمان", "قطر", "كويت", "بحرين", "oman", "qatar", "kuwait", "bahrain"))
        max_cap = 5 if is_gulf_camp else MIN_ACTIVE_COUNT

        if active_count > max_cap:
            sorted_keep = sorted(
                active_squads,
                key=lambda s: (
                    stats_map.get(s["id"], {}).get("purchases", 0),
                    -stats_map.get(s["id"], {}).get("cpa", 999999.0),
                    "copy" not in s.get("name", "").lower(),
                    stats_map.get(s["id"], {}).get("spend", 0.0),
                ),
                reverse=True,
            )
            excess = [s for s in sorted_keep[max_cap:] if stats_map.get(s["id"], {}).get("purchases", 0) == 0]
            if excess:
                for ex in excess:
                    ex["status"] = "PAUSED"
                for i in range(0, len(excess), 20):
                    session.put(f"{API_BASE}/campaigns/{cid}/adsquads", json={"adsquads": excess[i:i+20]}, timeout=25)
                print(f"   ✂️ تم تقليص {len(excess)} مجموعة زائدة في '{cname}' للالتزام بالسقف ({max_cap} مجموعة).", flush=True)
                active_squads = [s for s in active_squads if s.get("status") == "ACTIVE"]
                active_count = len(active_squads)

        if not is_scaling_or_duplicate_campaign(cname, squads, active_squads or squads):
            continue

        print(f"\n=== حملة: '{cname}' (ID: {cid}) ===", flush=True)
        print(f"   📊 عدد المجموعات النشطة الفعّالة حالياً: {active_count} (الحد المستهدف: {MIN_ACTIVE_COUNT})", flush=True)

        if is_cpa_over_8:
            print(f"   🚫 منع تكرار الدوبليكيت في هذه الحملة لأن سعر البيعة فيها لآخر 3 أيام متعدي $8.00 (CPA=${cst3.get('cpa')}) — لن يتم عمل أي Duplicate جديد هنا.", flush=True)
            continue

        if not allow_topup_window:
            print("   ⏸️ خارج نافذة التوسع النهارية (بعد 5:00 مساءً) — لا يتم إضافة مجموعات جديدة ليلاً لمنع الخسارة.", flush=True)
            continue

        if active_count >= MIN_ACTIVE_COUNT:
            print(f"   ✅ الحملة تحتوي على {active_count} مجموعة نشطة فعّالة (>= {MIN_ACTIVE_COUNT}) — لا تحتاج أي إضافة.", flush=True)
            continue

        # إضافة هادئة بحد أقصى نسختين فقط في كل دورة (كل ساعتين) لمنع صدمة المزاد
        needed = min(2, MIN_ACTIVE_COUNT - active_count)
        source_squad = pick_best_source_squad(session, active_squads or squads, stats_map)
        if not source_squad:
            print("   ⚠️ لا توجد أي مجموعة إعلانية صالحة للنسخ منها في هذه الحملة.", flush=True)
            continue

        st = stats_map.get(source_squad["id"], {"spend": 0.0, "purchases": 0, "cpa": None})
        if st.get("purchases", 0) < 2 or (st.get("cpa") is not None and st["cpa"] > 7.50):
            print(f"   ⏸️ تخطي الاستنساخ في '{cname}' لأن أفضل مجموعة متاحة ليس لديها >= 2 مبيعات بـ CPA <= $7.50 (CPA=${st.get('cpa')}).", flush=True)
            continue

        print(
            f"   🚀 جاري استنساخ {needed} مجموعة بهدوء من أفضل مجموعة رابحة ('{source_squad['name']}', مبيعات: {st['purchases']}, CPA: ${st['cpa']})...",
            flush=True,
        )
        clone_adsquad_exact(session, source_squad, needed)


if __name__ == "__main__":
    main()
