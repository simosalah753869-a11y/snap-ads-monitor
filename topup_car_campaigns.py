from pathlib import Path
import sys
import json
from snap_auth import API_BASE, load_config, refresh_access_token
from duplicate_gulf_campaigns import clone_adsquad_exact, make_session

sys.stdout.reconfigure(encoding="utf-8")

AD_ACCOUNT_ID = "350cb560-ad77-4fb5-9bbf-4c090c9ad081"
TARGET_ACTIVE_COUNT = 20


def main():
    token = refresh_access_token(load_config())
    session = make_session(token)

    rc = session.get(f"{API_BASE}/adaccounts/{AD_ACCOUNT_ID}/campaigns", timeout=20)
    rc.raise_for_status()
    campaigns = [c["campaign"] for c in rc.json().get("campaigns", []) if "campaign" in c]

    car_camps = [
        c for c in campaigns
        if c.get("status") == "ACTIVE" and "car" in c.get("name", "").lower()
    ]

    print(f"🔍 تم العثور على {len(car_camps)} حملة نشطة تحتوي على 'car':", flush=True)
    for c in car_camps:
        cid = c["id"]
        cname = c["name"]
        rsq = session.get(f"{API_BASE}/campaigns/{cid}/adsquads", timeout=20)
        rsq.raise_for_status()
        squads = [s["adsquad"] for s in rsq.json().get("adsquads", []) if "adsquad" in s]
        active_squads = [s for s in squads if s.get("status") == "ACTIVE"]
        active_count = len(active_squads)

        print(f"\n=== حملة: '{cname}' (ID: {cid}) ===", flush=True)
        print(f"   📊 عدد المجموعات النشطة حالياً: {active_count} / {TARGET_ACTIVE_COUNT}", flush=True)

        if active_count >= TARGET_ACTIVE_COUNT:
            print(f"   ✅ الحملة تحتوي بالفعل على {active_count} مجموعة نشطة (>= 20) — لا تحتاج استنساخ.", flush=True)
            continue

        needed = TARGET_ACTIVE_COUNT - active_count
        copy_squads = [s for s in active_squads if "copy" in s.get("name", "").lower()]
        source_squad = copy_squads[0] if copy_squads else (active_squads[0] if active_squads else (squads[0] if squads else None))
        if not source_squad:
            print("   ⚠️ لا توجد أي مجموعة إعلانية للنسخ منها في هذه الحملة.", flush=True)
            continue

        print(f"   🚀 جاري استنساخ {needed} مجموعة جديدة للوصول إلى {TARGET_ACTIVE_COUNT} مجموعة نشطة...", flush=True)
        clone_adsquad_exact(session, source_squad, needed)


if __name__ == "__main__":
    main()
