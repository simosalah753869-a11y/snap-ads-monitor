import sys
import time
from datetime import datetime, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from snap_auth import API_BASE, load_config, refresh_access_token

AD_ACCOUNT_ID = "350cb560-ad77-4fb5-9bbf-4c090c9ad081"

sys.stdout.reconfigure(encoding="utf-8")


def make_session(token: str) -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


import json
from pathlib import Path

HISTORY_FILE = Path(__file__).parent / "paused_history.json"


def clone_adsquad_exact(session: requests.Session, src: dict, copies_count: int):
    source_squad_id = src["id"]
    campaign_id = src["campaign_id"]
    src_name = src["name"]
    new_name = src_name if src_name.lower().startswith("copy of") else f"Copy of {src_name}"
    bid_dollars = round(src.get("bid_micro", 0) / 1_000_000, 2)

    ra = session.get(f"{API_BASE}/adsquads/{source_squad_id}/ads", timeout=20)
    ra.raise_for_status()
    all_ads = [a["ad"] for a in ra.json().get("ads", []) if "ad" in a]

    # استبعاد أي فيديو تم إيقافه/حذفه نهائياً على مستوى الحملة بالكامل (disabled_videos)
    disabled_keys = set()
    cmap = {}
    if HISTORY_FILE.exists():
        try:
            hist = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            disabled_keys = set((hist.get("disabled_videos") or {}).keys())
            cmap = hist.get("creative_media_map") or {}
        except Exception:
            pass

    def is_ad_allowed(a: dict) -> bool:
        cr_id = a.get("creative_id")
        v_key = cmap.get(cr_id) or cr_id
        return f"{campaign_id}:{v_key}" not in disabled_keys and f"{campaign_id}:{cr_id}" not in disabled_keys

    allowed_all = [a for a in all_ads if is_ad_allowed(a)]
    ads_to_copy = [a for a in allowed_all if a.get("status") == "ACTIVE"] or allowed_all
    if not ads_to_copy:
        print(f"   ⚠️ تخطي نسخ '{src_name}': جميع الفيديوهات بداخلها تم إيقافها/حذفها.", flush=True)
        return

    print(
        f"   📋 نسخ '{src_name}' عدد {copies_count} مرات | نفس الـ Bid (${bid_dollars}) | الإعلانات النشطة المسموحة: {len(ads_to_copy)}...",
        flush=True,
    )

    exclude_keys = {
        "id",
        "updated_at",
        "created_at",
        "targeting_reach_status",
        "placement",
        "creation_state",
        "delivery_status",
        "skadnetwork_properties",
        "delivery_properties_version",
    }

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    batch_squads = []
    for _ in range(copies_count):
        new_squad = {k: v for k, v in src.items() if k not in exclude_keys}
        new_squad["name"] = new_name
        new_squad["status"] = "ACTIVE"
        new_squad["start_time"] = now_iso
        if "placement_v2" not in new_squad:
            new_squad["placement_v2"] = {"config": "AUTOMATIC"}
        new_squad.pop("end_time", None)
        batch_squads.append(new_squad)

    r_create = session.post(
        f"{API_BASE}/campaigns/{campaign_id}/adsquads",
        json={"adsquads": batch_squads},
        timeout=30,
    )
    if not r_create.ok:
        raise RuntimeError(f"Batch AdSquad create failed: {r_create.status_code} {r_create.text}")

    created_squads = [item["adsquad"] for item in r_create.json().get("adsquads", []) if "adsquad" in item]

    for idx, sq in enumerate(created_squads, start=1):
        sq_id = sq["id"]
        new_ads_payload = [
            {
                "ad_squad_id": sq_id,
                "creative_id": ad["creative_id"],
                "name": ad["name"],
                "status": "ACTIVE",
                "type": ad.get("type", "REMOTE_WEBPAGE"),
            }
            for ad in ads_to_copy
        ]
        r_ads = session.post(
            f"{API_BASE}/adsquads/{sq_id}/ads",
            json={"ads": new_ads_payload},
            timeout=25,
        )
        r_ads.raise_for_status()
        print(
            f"      ✅ نسخة {idx}/{copies_count}: '{new_name}' (ID: {sq_id}) | Bid: ${bid_dollars} | ACTIVE",
            flush=True,
        )
        time.sleep(0.2)


def main():
    token = refresh_access_token(load_config())
    session = make_session(token)

    rc = session.get(f"{API_BASE}/adaccounts/{AD_ACCOUNT_ID}/campaigns", timeout=20)
    rc.raise_for_status()
    campaigns = [c["campaign"] for c in rc.json().get("campaigns", []) if "campaign" in c]

    keywords = ("qatar", "qat", "oman", "قطر", "عمان")
    active_gulf_camps = [
        c for c in campaigns
        if c.get("status") == "ACTIVE"
        and any(k in c.get("name", "").lower() for k in keywords)
    ]

    print(f"🔍 تم العثور على {len(active_gulf_camps)} حملة نشطة لقطر/عمان:")
    for c in active_gulf_camps:
        cid = c["id"]
        cname = c["name"]
        print(f"\n=== حملة: '{cname}' (ID: {cid}) ===", flush=True)
        rsq = session.get(f"{API_BASE}/campaigns/{cid}/adsquads", timeout=20)
        rsq.raise_for_status()
        squads = [s["adsquad"] for s in rsq.json().get("adsquads", []) if "adsquad" in s]
        active_squads = [s for s in squads if s.get("status") == "ACTIVE"]
        source_squad = active_squads[0] if active_squads else (squads[0] if squads else None)
        if not source_squad:
            print("   ⚠️ لا توجد Ad Squads في هذه الحملة للنسخ منها.")
            continue
        clone_adsquad_exact(session, source_squad, 10)

    print("\n🎉 اكتمل نسخ 10 Ad Squads جديدة لكل حملات قطر وعمان النشطة بنجاح!", flush=True)


if __name__ == "__main__":
    main()
