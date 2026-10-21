import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import requests

_orig_getaddrinfo = socket.getaddrinfo


def _doh_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    except socket.gaierror:
        r = requests.get(
            f"https://1.1.1.1/dns-query?name={host}&type=A",
            headers={"accept": "application/dns-json"},
            timeout=5,
        ).json()
        for ans in r.get("Answer", []):
            if ans.get("type") == 1:
                return _orig_getaddrinfo(ans["data"], port, family, type, proto, flags)
        raise


socket.getaddrinfo = _doh_getaddrinfo

from snap_auth import API_BASE, load_config, refresh_access_token
from snap_monitor import TARGET_ACCOUNT_ID, api_get_paginated, fetch_account_breakdown_stats, is_cross_monitored_product_campaign
from duplicate_gulf_campaigns import (
    HEADLINE_BANK,
    detect_product_type,
    get_or_create_headline_creative,
    make_session,
)

sys.stdout.reconfigure(encoding="utf-8")

HISTORY_FILE = Path(__file__).parent / "paused_history.json"


def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_history(hist: dict):
    HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_headline_testing_live():
    token = refresh_access_token(load_config())
    session = make_session(token)
    hist = load_history()

    camps = api_get_paginated(f"/adaccounts/{TARGET_ACCOUNT_ID}/campaigns?limit=200", token, "campaigns", "campaign")
    active_camps = [c for c in camps if c.get("status") == "ACTIVE"]
    st7_ad = {x["id"]: x for x in fetch_account_breakdown_stats(token, TARGET_ACCOUNT_ID, "ad", days_back=6)}
    st3_ad = {x["id"]: x for x in fetch_account_breakdown_stats(token, TARGET_ACCOUNT_ID, "ad", days_back=2)}
    st7_sq = {x["id"]: x for x in fetch_account_breakdown_stats(token, TARGET_ACCOUNT_ID, "adsquad", days_back=6)}

    total_method2_ads_created = 0
    total_method3_squads_rotated = 0

    for c in active_camps:
        cid = c["id"]
        cname = c["name"]
        if is_cross_monitored_product_campaign(cname):
            print(f"⏭️ تخطي حملة الاختبار المشترك الباردة: '{cname}'")
            continue

        ptype = detect_product_type(cname)
        hl_bank = HEADLINE_BANK.get(ptype, HEADLINE_BANK["general"])

        squads = api_get_paginated(f"/campaigns/{cid}/adsquads?limit=100", token, "adsquads", "adsquad")
        ads = api_get_paginated(f"/campaigns/{cid}/ads?limit=500", token, "ads", "ad")
        if not squads or not ads:
            continue

        # 1. جمع وترتيب أفضل الفيديوهات الرابحة داخل الحملة (بدون تكرار نفس الفيديو)
        vid_candidates = {}
        for a in ads:
            aid = a["id"]
            cr_id = a.get("creative_id")
            if not cr_id:
                continue
            s7 = st7_ad.get(aid, {"spend": 0.0, "purchases": 0, "swipes": 0})
            s3 = st3_ad.get(aid, {"spend": 0.0, "purchases": 0, "swipes": 0})
            raw_nm = (a.get("name") or "Ad").split(" [H")[0].strip()
            clean_key = raw_nm
            while clean_key.lower().startswith("copy of "):
                clean_key = clean_key[8:].strip()

            v = vid_candidates.setdefault(clean_key, {
                "creative_id": cr_id,
                "name": raw_nm,
                "clean_key": clean_key,
                "type": a.get("type", "REMOTE_WEBPAGE"),
                "active_count": 0,
                "spend_7d": 0.0,
                "purchases_7d": 0,
                "swipes_7d": 0,
                "spend_3d": 0.0,
                "purchases_3d": 0,
            })
            if a.get("status") == "ACTIVE":
                v["active_count"] += 1
                v["creative_id"] = cr_id
                v["name"] = raw_nm
            v["spend_7d"] = round(v["spend_7d"] + s7["spend"], 2)
            v["purchases_7d"] += s7["purchases"]
            v["swipes_7d"] += s7["swipes"]
            v["spend_3d"] = round(v["spend_3d"] + s3["spend"], 2)
            v["purchases_3d"] += s3["purchases"]

        # نختار الفيديوهات النشطة أولاً، أو الفيديوهات الرابحة تاريخياً إذا كانت كل المجموعات موقوفة (مثل حملة الساعات)
        pool = [v for v in vid_candidates.values() if v["active_count"] > 0]
        if not pool:
            pool = [v for v in vid_candidates.values() if v["purchases_7d"] > 0]
        if not pool:
            continue

        top_vids_raw = sorted(
            pool,
            key=lambda x: (
                x["purchases_7d"],
                -(x["spend_7d"] / x["purchases_7d"]) if x["purchases_7d"] > 0 else -999999.0,
                x["swipes_7d"],
            ),
            reverse=True,
        )

        # التأكد من عدم تكرار نفس الـ top_snap_media_id
        top_vids = []
        seen_media_ids = set()
        for tv in top_vids_raw:
            try:
                _, m_id = get_or_create_headline_creative(
                    session, TARGET_ACCOUNT_ID, tv["creative_id"], hl_bank[0][0], hl_bank[0][1], hist
                )
            except Exception:
                m_id = tv["creative_id"]
            if m_id not in seen_media_ids:
                seen_media_ids.add(m_id)
                tv["media_id"] = m_id
                top_vids.append(tv)
            if len(top_vids) >= 3:
                break

        if not top_vids:
            continue

        print(f"\n🚀 === حملة: '{cname}' ({ptype}) ===")
        for idx_v, tv in enumerate(top_vids, 1):
            cpa_v = round(tv["spend_7d"] / tv["purchases_7d"], 2) if tv["purchases_7d"] > 0 else None
            print(f"   🏆 فيديو رابح #{idx_v}: '{tv['name']}' (مبيعات 7 أيام: {tv['purchases_7d']} | CPA: ${cpa_v})")

        active_squads = [
            s for s in squads
            if s.get("status") == "ACTIVE"
            and "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" not in (s.get("delivery_status") or [])
        ]
        non_copy_active = [s for s in active_squads if "copy" not in s.get("name", "").lower()]
        copy_active = [s for s in active_squads if "copy" in s.get("name", "").lower()]

        # =========================================================================
        # الطريقة الثانية (Method 2): اختبار العناوين وجهاً لوجه داخل نفس المجموعة (Multi-Headline Inside 1 AdSquad)
        # =========================================================================
        target_m2_squad = None
        if non_copy_active:
            target_m2_squad = sorted(
                non_copy_active,
                key=lambda s: (
                    st7_sq.get(s["id"], {}).get("purchases", 0),
                    st7_sq.get(s["id"], {}).get("swipes", 0),
                ),
                reverse=True,
            )[0]
        elif active_squads:
            target_m2_squad = sorted(
                active_squads,
                key=lambda s: (
                    st7_sq.get(s["id"], {}).get("purchases", 0),
                    st7_sq.get(s["id"], {}).get("swipes", 0),
                ),
                reverse=True,
            )[0]
        else:
            # مثل حملة الساعات smart+ watch suis SA 18-10 التي أُوقفت نسخ الـ Copy فيها: ننشئ مجموعة أصلية (بدون Copy) لاختبار العناوين وجهاً لوجه!
            template_sq = squads[0]
            exclude_keys = {
                "id", "updated_at", "created_at", "targeting_reach_status",
                "placement", "creation_state", "delivery_status",
                "skadnetwork_properties", "delivery_properties_version", "end_time"
            }
            new_sq = {k: v for k, v in template_sq.items() if k not in exclude_keys}
            new_sq["name"] = "Headline Lab H1-H3 | Elite Winners"
            new_sq["status"] = "ACTIVE"
            new_sq["bid_micro"] = 2_150_000  # $2.15 safe bid ladder
            new_sq["start_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            if "placement_v2" not in new_sq:
                new_sq["placement_v2"] = {"config": "AUTOMATIC"}
            r_sq = session.post(f"{API_BASE}/campaigns/{cid}/adsquads", json={"adsquads": [new_sq]}, timeout=25)
            if r_sq.ok:
                target_m2_squad = r_sq.json().get("adsquads", [{}])[0].get("adsquad")
                print(f"   ✨ تم إنشاء وتفعيل مجموعة اختبار العناوين الأصلية: '{target_m2_squad['name']}' (ID: {target_m2_squad['id']})")

        if target_m2_squad:
            sq_id = target_m2_squad["id"]
            sq_name = target_m2_squad["name"]
            sq_ads = [a for a in ads if a.get("ad_squad_id") == sq_id]
            existing_ad_names = {(a.get("name") or "").strip() for a in sq_ads}
            existing_creatives = {a.get("creative_id") for a in sq_ads if a.get("status") == "ACTIVE"}

            # نختار أفضل 3 عناوين مختلفة لكل فيديو رابح داخل نفس المجموعة ليتنافسوا وجهاً لوجه
            m2_new_ads = []
            for tv in top_vids:
                base_cr_id = tv["creative_id"]
                base_hl = (hist.get("creative_details_cache", {}).get(base_cr_id, {}).get("headline") or "").strip()

                # نختار 3 عناوين من البنك (وإذا كان الفيديو موجوداً بالفعل في المجموعة بعنوانه الأصلي، نضيف له 2 عناوين جديدة مختلفة ليكون المجموع 3 عناوين وجهاً لوجه!)
                already_in_squad = any(
                    a.get("status") == "ACTIVE"
                    and (hist.get("creative_media_map", {}).get(a.get("creative_id")) == tv["media_id"] or a.get("creative_id") == base_cr_id)
                    for a in sq_ads
                )
                needed_variants = 2 if already_in_squad else 3
                added_for_tv = 0
                for hl_tag, hl_text in hl_bank:
                    if added_for_tv >= needed_variants:
                        break
                    if already_in_squad and hl_text.strip() == base_hl:
                        continue
                    new_cid, _ = get_or_create_headline_creative(
                        session, TARGET_ACCOUNT_ID, base_cr_id, hl_tag, hl_text, hist
                    )
                    ad_label = f"{tv['clean_key']} [{hl_tag}]"[:95]
                    if new_cid in existing_creatives or ad_label in existing_ad_names:
                        continue
                    m2_new_ads.append({
                        "ad_squad_id": sq_id,
                        "creative_id": new_cid,
                        "name": ad_label,
                        "status": "ACTIVE",
                        "type": tv.get("type", "REMOTE_WEBPAGE"),
                    })
                    existing_creatives.add(new_cid)
                    existing_ad_names.add(ad_label)
                    added_for_tv += 1

            if m2_new_ads:
                r_post_ads = session.post(f"{API_BASE}/adsquads/{sq_id}/ads", json={"ads": m2_new_ads}, timeout=25)
                if r_post_ads.ok:
                    total_method2_ads_created += len(m2_new_ads)
                    print(
                        f"   ✅ [الطريقة 2 - وجهاً لوجه داخل نفس المجموعة] تم حقن {len(m2_new_ads)} إعلان بعناوين متنافسة (H1/H2/H3) داخل المجموعة '{sq_name}'!"
                    )
                else:
                    print(f"   ⚠️ تعذر حقن إعلانات الطريقة 2 في '{sq_name}': {r_post_ads.status_code} {r_post_ads.text}")

        # =========================================================================
        # الطريقة الثالثة (Method 3): تدوير العناوين على مجموعات الـ Copy النشطة (بالإضافة للتدوير التلقائي عند كل نسخ جديد)
        # =========================================================================
        untagged_copies = [
            s for s in copy_active
            if "[H" not in s.get("name", "")
            and st7_sq.get(s["id"], {}).get("purchases", 0) == 0
        ]
        # نحدث حتى 3 مجموعات Copy حالية بعناوين مدورة H3 / H4 / H5 / H6 لكسر تشبع الإعلان فوراً
        for rot_i, c_sq in enumerate(untagged_copies[:3]):
            csq_id = c_sq["id"]
            hl_tag, hl_text = hl_bank[(rot_i + 2) % len(hl_bank)]  # نبدأ من H3, H4, H5 لأنها عناوين جديدة كلياً
            csq_ads = [a for a in ads if a.get("ad_squad_id") == csq_id and a.get("status") == "ACTIVE"]
            existing_c_names = {(a.get("name") or "").strip() for a in csq_ads}

            rot_ads_payload = []
            for tv in top_vids[:2]:
                new_cid, _ = get_or_create_headline_creative(
                    session, TARGET_ACCOUNT_ID, tv["creative_id"], hl_tag, hl_text, hist
                )
                ad_label = f"{tv['clean_key']} [{hl_tag}]"[:95]
                if ad_label in existing_c_names:
                    continue
                rot_ads_payload.append({
                    "ad_squad_id": csq_id,
                    "creative_id": new_cid,
                    "name": ad_label,
                    "status": "ACTIVE",
                    "type": tv.get("type", "REMOTE_WEBPAGE"),
                })
                existing_c_names.add(ad_label)

            if rot_ads_payload:
                r_rot = session.post(f"{API_BASE}/adsquads/{csq_id}/ads", json={"ads": rot_ads_payload}, timeout=25)
                if r_rot.ok:
                    c_sq["name"] = f"{c_sq['name'].strip()} [{hl_tag}]"[:95]
                    session.put(f"{API_BASE}/campaigns/{cid}/adsquads", json={"adsquads": [c_sq]}, timeout=20)
                    total_method3_squads_rotated += 1
                    print(
                        f"   🔄 [الطريقة 3 - تدوير العناوين على الـ Copy] المجموعة '{c_sq['name']}' أخذت العنوان [{hl_tag}]: '{hl_text}' ({len(rot_ads_payload)} فيديوهات رابحة)"
                    )
                    time.sleep(0.2)

        save_history(hist)

    print(
        f"\n🎉 اكتمل التطبيق الحي بنجاح! إجمالي إعلانات المنافسة وجهاً لوجه (الطريقة 2): {total_method2_ads_created} إعلان | إجمالي مجموعات الـ Copy المدورة بعناوين جديدة (الطريقة 3): {total_method3_squads_rotated} مجموعات."
    )


if __name__ == "__main__":
    apply_headline_testing_live()
