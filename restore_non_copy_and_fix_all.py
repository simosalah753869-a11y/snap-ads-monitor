import os
import json
import re
from datetime import datetime, timezone, timedelta
import snap_monitor as sm

cfg = sm.load_config()
token = sm.refresh_access_token(cfg)
history = sm.load_paused_history()

ACCOUNTS = [
    ("350cb560-ad77-4fb5-9bbf-4c090c9ad081", "CSP9860"),
    ("0d00760b-6793-441f-a675-e35371ce9c7c", "CSP9860 4"),
]

RIYADH_TZ = timezone(timedelta(hours=3))
now_riyadh = datetime.now(RIYADH_TZ)
yesterday_start_utc = (now_riyadh.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)).astimezone(timezone.utc)

empty_st = {"spend": 0.0, "purchases": 0, "swipes": 0, "cpa": None, "cpc": None}

def is_non_copy_squad_to_keep_active(sq: dict, st1: dict, st3: dict, cname: str = "") -> bool:
    nm = (sq.get("name") or "").strip()
    nm_l = nm.lower()
    if not nm or nm_l.startswith("base -") or sm.is_scaling_winner_adsquad(nm, cname):
        return False
    if "بخاخ" in (cname or "") and sq.get("status") == "PAUSED":
        return False
    # أي أدست بدون كلمة copy أُنشئت في آخر يومين/ثلاثة (24-10, 25-10, 26-10, Flow Hook Ads, تيست, test, test adset) أو صرفت في آخر 3 أيام
    created_str = sq.get("created_at", "")
    created_dt = sm.parse_iso_utc(created_str)
    if created_dt and created_dt >= yesterday_start_utc:
        return True
    if any(k in nm_l for k in ("24-10", "25-10", "26-10", "27-10", "28-10", "29-10", "30-10", "31-10", "flow hook", "تيست", "تست", "test")):
        return True
    if st1["spend"] > 0 or st3["spend"] > 0:
        # تأكد أنها من المجموعات الحديثة (ليست من حملات 16-10 أو 18-10 أو 19-10 أو 20-10 أو 21-10 القديمة إلا لو ربحت)
        if not any(old_k in nm_l for old_k in ("16-10", "17-10", "18-10", "19-10", "20-10", "21-10", "22-10", "23-10", "1.5 |", "mega winners", "elite", "target cost men")):
            return True
    return False

def should_pause_test_video(st_ad: dict) -> tuple[bool, str]:
    sp = st_ad["spend"]
    p = st_ad["purchases"]
    sw = st_ad["swipes"]
    cpc = st_ad["cpc"]
    cpa = st_ad["cpa"]
    if p > 0 and cpa is not None and cpa >= 7.00:
        return True, f"CPA=${cpa:.2f} >= $7.00"
    if p == 0 and sp >= 4.00:
        return True, f"Spend=${sp:.2f} >= $4.00 (0 sales)"
    if p == 0 and sp >= 1.00 and (sw == 0 or (cpc is not None and cpc > 0.27)):
        return True, f"Spend=${sp:.2f} >= $1.00 & CPC=${cpc} > $0.27 (swipes={sw})"
    return False, ""

for acc_id, acc_name in ACCOUNTS:
    print(f"\n==================== {acc_name} ({acc_id}) ====================")
    sq_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=0)}
    sq_2d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=1)}
    sq_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=2)}
    sq_4d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=3)}
    ad_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "ad", days_back=0)}
    ad_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "ad", days_back=2)}

    camps = sm.api_get_paginated(f"/adaccounts/{acc_id}/campaigns?limit=200", token, "campaigns", "campaign")
    for c in camps:
        cid = c["id"]
        cname = c.get("name", "")
        if sm.is_gulf_034_campaign(cname) or cid in getattr(sm, "ALLOWED_GULF_CAMPAIGN_IDS", set()):
            continue
        is_ded_test = sm.is_dedicated_snap_test_campaign(cid, cname)
        if is_ded_test and c.get("status") != "ACTIVE":
            c["status"] = "ACTIVE"
            sm.api_put(f"/adaccounts/{acc_id}/campaigns", token, {"campaigns": [c]})
            print(f"  ♻️ [RE-ENABLED DEDICATED TEST CAMPAIGN] '{cname}' ({cid})")
        created_ts = str(c.get("created_at") or "")
        is_recent_camp = bool(created_ts and created_ts >= "2026-10-23T18:00:00")
        if not is_ded_test and ((cid not in sm.ALLOWED_30X_CAMPAIGN_IDS and not is_recent_camp) or sm.is_cross_monitored_product_campaign(cname)):
            if c.get("status") == "ACTIVE":
                c["status"] = "PAUSED"
                sm.api_put(f"/adaccounts/{acc_id}/campaigns", token, {"campaigns": [c]})
                print(f"  🛑 [PAUSED NON-ALLOWED CAMPAIGN] '{cname}' ({cid})")
            continue
        if c.get("status") != "ACTIVE":
            continue
        squads = sm.api_get_paginated(f"/campaigns/{cid}/adsquads?limit=1000", token, "adsquads", "adsquad", max_pages=4)

        active_copy_count = sum(
            1 for s_x in squads
            if s_x.get("status") == "ACTIVE" and sm.is_scaling_winner_adsquad(s_x.get("name", ""), cname)
        )
        cmap = history.setdefault("creative_media_map", {})
        disabled_vids = history.setdefault("disabled_videos", {})

        for sq in squads:
            sq_id = sq["id"]
            sq_name = sq.get("name", "")
            is_ded_sq = is_ded_test or sm.is_dedicated_snap_test_campaign(cid, cname, sq_name)
            if is_ded_sq and sq.get("status") != "ACTIVE":
                sq["status"] = "ACTIVE"
                sm.api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": [sq]})
                print(f"  ♻️ [RE-ENABLED DEDICATED TEST ADSQUAD] '{sq_name}' ({sq_id}) in '{cname}'")
            st1 = sq_1d.get(sq_id, empty_st)
            st2 = sq_2d.get(sq_id, empty_st)
            st3 = sq_3d.get(sq_id, empty_st)
            st4 = sq_4d.get(sq_id, empty_st)

            # 1) فحص حملات التيست المخصصة والمجموعات التي لا تحتوي على Copy (فقط الفيديو يوقف فيها داخلها، ولا تتوقف الحملة أو الأدست!)
            if sq.get("status") == "ACTIVE" and (is_ded_sq or is_non_copy_squad_to_keep_active(sq, st1, st3, cname)):
                print(
                    f"  [NON-COPY SQUAD] '{sq_name}' ({sq_id}) in '{cname}' | status={sq.get('status')} "
                    f"| 1D: ${st1['spend']:.2f}/{st1['purchases']}p/cpc={st1['cpc']} | 3D: ${st3['spend']:.2f}/{st3['purchases']}p/cpc={st3['cpc']}"
                )

                # فحص الفيديوهات بداخلها فردياً (نتابع الفيديوز فقط وليس الأدست!)
                ads_in_sq = sm.api_get_paginated(f"/adsquads/{sq_id}/ads?limit=100", token, "ads", "ad")
                ads_to_upd = []
                for ad in ads_in_sq:
                    aid = ad["id"]
                    aname = ad.get("name", "")
                    a_st1 = ad_1d.get(aid, empty_st)
                    a_st3 = ad_3d.get(aid, empty_st)
                    bad1, r1 = should_pause_test_video(a_st1)
                    bad3, r3 = should_pause_test_video(a_st3)
                    is_bad = bad1 or bad3
                    reason_ad = r1 if bad1 else r3
                    print(
                        f"      - Ad '{aname}' ({aid}) | status={ad.get('status')} "
                        f"| 1D: ${a_st1['spend']:.2f}/{a_st1['purchases']}p/sw={a_st1['swipes']}/cpc={a_st1['cpc']} "
                        f"| 3D: ${a_st3['spend']:.2f}/{a_st3['purchases']}p/sw={a_st3['swipes']}/cpc={a_st3['cpc']} -> bad={is_bad} ({reason_ad})"
                    )
                    if is_bad and ad.get("status") == "ACTIVE":
                        ad["status"] = "PAUSED"
                        ads_to_upd.append(ad)
                        history.setdefault("ads", {})[aid] = {
                            "name": aname,
                            "campaign": cname,
                            "adsquad": sq_name,
                            "reason": f"[Non-Copy New-Video Filter] {reason_ad}",
                            "paused_at": datetime.now(timezone.utc).isoformat(),
                        }
                        print(f"        🛑 Pausing losing video '{aname}' inside non-copy squad: {reason_ad}")
                    elif not is_bad and ad.get("status") != "ACTIVE" and (
                        (a_st1["purchases"] >= 1 and (a_st1["cpa"] or 99) < 7.00)
                        or (a_st3["purchases"] >= 1 and (a_st3["cpa"] or 99) < 7.00 and a_st1["spend"] < 4.00)
                    ):
                        ad["status"] = "ACTIVE"
                        ads_to_upd.append(ad)
                        print(f"        ✅ Re-enabling delayed-conversion winning video '{aname}' inside non-copy squad")
                if ads_to_upd:
                    sm.api_put(f"/adsquads/{sq_id}/ads", token, {"ads": ads_to_upd})

            # 2) فحص لو هناك مجموعة Copy سليمة أو رابحة (< $7.00) أُطفئت بالخطأ أو أُطفئت إعلاناتها بداخلها!
            elif sm.is_scaling_winner_adsquad(sq_name, cname):
                p_rec = (history.get("adsquads") or {}).get(sq_id) or {}
                p_rsn = str(p_rec.get("reason") or "")
                was_paused_by_rule = any(
                    k in p_rsn for k in ("CPC >", "Stop-Loss", "CPA >", "CPA >=", "8.00", "7.00", "4.00", "Manual", "Duplicate")
                )
                has_8_cpa_any = any(
                    wx["purchases"] > 0 and (wx["cpa"] or 99) >= 7.00
                    for wx in (st1, st2, st3, st4)
                )
                has_7_nosale_12 = any(wx["purchases"] == 0 and wx["spend"] >= 4.00 for wx in (st1, st2))
                is_1d_winner = (
                    (not has_8_cpa_any)
                    and (not has_7_nosale_12)
                    and st1["purchases"] >= 1
                    and (st1["cpa"] or 99) < 7.00
                )
                is_3d_winner = (
                    (not has_8_cpa_any)
                    and (not has_7_nosale_12)
                    and st3["purchases"] >= 1
                    and (st3["cpa"] or 99) < 7.00
                )
                is_healthy_unjustly_paused = (
                    sq.get("status") == "PAUSED"
                    and active_copy_count < 30
                    and (not has_8_cpa_any)
                    and (not was_paused_by_rule)
                    and st1["spend"] >= 0.05
                    and st1["spend"] < 4.00
                    and st1["purchases"] == 0
                    and st2["spend"] < 4.00
                    and st3["spend"] < 4.00
                )
                can_re_enable_paused = False  # بأمر المستخدم (2026-10-30): عدم إعادة تشغيل الكوبي الموقوف تلقائياً
                if sq.get("status") == "ACTIVE" and (is_1d_winner or is_3d_winner):
                    was_paused = False
                    if was_paused:
                        sq["status"] = "ACTIVE"
                        sm.api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": [sq]})
                        history.get("adsquads", {}).pop(sq_id, None)
                        active_copy_count += 1
                        print(
                            f"  ✅ [RE-ENABLED HEALTHY/WINNER COPY SQUAD] '{sq_name}' ({sq_id}) in '{cname}' "
                            f"| 1D: ${st1['spend']:.2f}/{st1['purchases']}p/cpc={st1['cpc']} | 3D: ${st3['spend']:.2f}/{st3['purchases']}p/cpa={st3['cpa']}"
                        )
                    # التأكد من تفعيل الإعلانات المسموحة غير المحظورة داخل الأدست إذا كانت كل إعلاناتها مطفأة
                    if was_paused or "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" in (sq.get("delivery_status") or []):
                        ads_in_win_sq = sm.api_get_paginated(f"/adsquads/{sq_id}/ads?limit=100", token, "ads", "ad")
                        if ads_in_win_sq and not any(a.get("status") == "ACTIVE" for a in ads_in_win_sq):
                            ads_to_en = []
                            for a_w in ads_in_win_sq:
                                a_s1 = ad_1d.get(a_w["id"], empty_st)
                                a_s3 = ad_3d.get(a_w["id"], empty_st)
                                if (a_s1["spend"] >= 4.00 and a_s1["purchases"] == 0) or (a_s3["spend"] >= 4.00 and a_s3["purchases"] == 0):
                                    continue
                                cr_w = a_w.get("creative_id")
                                mid_w = cmap.get(cr_w) or (sm.resolve_video_media_id(cr_w, token, history) if cr_w else None)
                                if mid_w and (f"{cid}:MEDIA:{mid_w}" in disabled_vids or f"{cid}:{mid_w}" in disabled_vids):
                                    continue
                                a_w["status"] = "ACTIVE"
                                ads_to_en.append(a_w)
                            if not ads_to_en:
                                for a_w in ads_in_win_sq[:5]:
                                    a_w["status"] = "ACTIVE"
                                    ads_to_en.append(a_w)
                            sm.api_put(f"/adsquads/{sq_id}/ads", token, {"ads": ads_to_en})
                            print(f"    ↳ Re-enabled {len(ads_to_en)} allowed ads inside Copy squad '{sq_name}'")

sm.save_paused_history(history)
