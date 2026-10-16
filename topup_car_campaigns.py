import sys
from snap_auth import API_BASE, load_config, refresh_access_token
from duplicate_gulf_campaigns import clone_adsquad_exact, make_session

sys.stdout.reconfigure(encoding="utf-8")

AD_ACCOUNT_ID = "350cb560-ad77-4fb5-9bbf-4c090c9ad081"
TARGET_ACTIVE_COUNT = 20


def is_saudi_squad(squad: dict) -> bool:
    geos = squad.get("targeting", {}).get("geos", [])
    countries = {str(g.get("country_code", "")).lower() for g in geos if g.get("country_code")}
    return "sa" in countries


def main():
    dry_run = "--dry-run" in sys.argv
    token = refresh_access_token(load_config())
    session = make_session(token)

    rc = session.get(f"{API_BASE}/adaccounts/{AD_ACCOUNT_ID}/campaigns", timeout=20)
    rc.raise_for_status()
    campaigns = [c["campaign"] for c in rc.json().get("campaigns", []) if "campaign" in c]

    active_camps = [c for c in campaigns if c.get("status") == "ACTIVE"]

    print(f"🔍 فحص جميع الحملات النشطة ({len(active_camps)} حملة) للبحث عن حملات السعودية التي تحتوي على Copy:", flush=True)
    for c in active_camps:
        cid = c["id"]
        cname = c["name"]
        rsq = session.get(f"{API_BASE}/campaigns/{cid}/adsquads", timeout=20)
        rsq.raise_for_status()
        squads = [s["adsquad"] for s in rsq.json().get("adsquads", []) if "adsquad" in s]
        if not squads:
            continue

        # فحص الدولة من إعدادات الاستهداف الفعلية داخل AdSquad (targeting.geos -> country_code == 'sa')
        saudi_squads = [s for s in squads if is_saudi_squad(s)]
        has_copy_squads = any("copy" in s.get("name", "").lower() for s in squads)
        geos_sample = [g.get("country_code") for g in squads[0].get("targeting", {}).get("geos", [])]

        active_squads = [s for s in squads if s.get("status") == "ACTIVE"]
        active_count = len(active_squads)

        print(
            f"\n=== حملة: '{cname}' (ID: {cid}) | الدول المستهدفة: {geos_sample} | تحتوي Copy: {has_copy_squads} | النشطة: {active_count} ===",
            flush=True,
        )

        if not saudi_squads:
            print("   ⏭️ تخطي: الحملة لا تستهدف السعودية (country_code != 'sa').", flush=True)
            continue

        if not has_copy_squads:
            print("   ⏭️ تخطي: الحملة لا تحتوي على مجموعات Copy/دوبليكيت.", flush=True)
            continue

        if active_count >= TARGET_ACTIVE_COUNT:
            print(f"   ✅ الحملة تحتوي بالفعل على {active_count} مجموعة نشطة (>= {TARGET_ACTIVE_COUNT}) — لا تحتاج استنساخ.", flush=True)
            continue

        needed = TARGET_ACTIVE_COUNT - active_count
        copy_squads = [s for s in active_squads if "copy" in s.get("name", "").lower()]
        if not copy_squads:
            copy_squads = [s for s in squads if "copy" in s.get("name", "").lower()]

        # تجميع المجموعات الفريدة بالاسم (مثلاً Copy of 16-10 1 و Copy of 16-10 2) لتوزيع النسخ بالتساوي بينها
        distinct_templates = {}
        for sq in copy_squads:
            sq_name = sq.get("name", "").strip()
            if sq_name not in distinct_templates:
                distinct_templates[sq_name] = sq
        templates_list = list(distinct_templates.values()) if distinct_templates else [active_squads[0] if active_squads else squads[0]]

        num_templates = len(templates_list)
        base_per_tmpl = needed // num_templates
        rem = needed % num_templates

        for idx, tmpl in enumerate(templates_list):
            cnt = base_per_tmpl + (1 if idx < rem else 0)
            if cnt <= 0:
                continue
            if dry_run:
                print(f"   🔍 [Dry-Run] سيتم استنساخ {cnt} مجموعة جديدة من '{tmpl['name']}' للوصول إلى {TARGET_ACTIVE_COUNT} مجموعة نشطة.", flush=True)
            else:
                print(f"   🚀 جاري استنساخ {cnt} مجموعة جديدة من '{tmpl['name']}' للوصول إلى {TARGET_ACTIVE_COUNT} مجموعة نشطة...", flush=True)
                clone_adsquad_exact(session, tmpl, cnt)


if __name__ == "__main__":
    main()
