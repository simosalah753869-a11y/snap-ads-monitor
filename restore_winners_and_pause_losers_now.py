import os
import json
import re
import time
from datetime import datetime, timezone, timedelta
import snap_monitor as sm

cfg = sm.load_config()
token = sm.refresh_access_token(cfg)
history = sm.load_paused_history()
disabled_videos = history.get("disabled_videos", {})

ACCOUNTS = [
    ("350cb560-ad77-4fb5-9bbf-4c090c9ad081", "CSP9860"),
    ("0d00760b-6793-441f-a675-e35371ce9c7c", "CSP9860 4"),
]

SCALING_CAMPAIGNS = {
    # CSP9860
    "e23e7c95-3f15-4945-9844-8bd73e2d79d3": {"mode": "MAX", "bid": 1_800_000, "prefix": "Copy - PB 1.80 Max"},
    "4e01cc73-1d2f-4198-8cac-ecb5862f29eb": {"mode": "TC", "bid": 1_800_000, "prefix": "Copy - PB 1.80 TC"},
    "baa69314-505c-4b3a-851e-883c10034935": {"mode": "TC", "bid": 1_800_000, "prefix": "Copy - Car 1.80 TC"},
    "f8372cfb-ae46-44d0-bbb4-f39790ecc648": {"mode": "MAX", "bid": 1_800_000, "prefix": "Copy - Car 1.80 Max"},
    # CSP9860 4
    "558c5d45-f08e-400c-a499-7668e801c347": {"mode": "TC", "bid": 1_800_000, "prefix": "Copy - Car 1.80 TC"},
    "dab749f9-2dca-497b-a8c3-39f2201162a7": {"mode": "TC", "bid": 1_800_000, "prefix": "Copy - PB 1.80 TC"},
    "803324e6-052e-441b-99fb-4d43e56c5c78": {"mode": "MAX", "bid": 1_800_000, "prefix": "Copy - PB 1.80 Max"},
}

def _is_future_sched(sq_obj: dict) -> bool:
    st_raw = str(sq_obj.get("start_time") or "").strip()
    if not st_raw:
        return False
    try:
        dt_s = datetime.fromisoformat(st_raw.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) < dt_s
    except Exception:
        return False


def is_winning_squad(st1: dict, st2: dict, st3: dict, st4: dict | None = None, st7: dict | None = None) -> tuple[bool, str]:
    p1, sp1, cpa1 = st1["purchases"], st1["spend"], st1["cpa"]
    p2, sp2, cpa2 = st2["purchases"], st2["spend"], st2["cpa"]
    p3, sp3, cpa3 = st3["purchases"], st3["spend"], st3["cpa"]

    # بأمر المستخدم (2026-10-30): أي أدست وصل سعر البيعة فيها إلى CPA >= $7.00 أو صرفت >= $4.00 بدون مبيعة ليست رابحة وتُطفأ فوراً
    for wx in (st1, st2, st3):
        if wx["purchases"] > 0 and wx["cpa"] is not None and wx["cpa"] >= 7.00:
            return False, ""
    if p1 == 0 and sp1 >= 4.00:
        return False, ""
    if p2 == 0 and sp2 >= 4.00:
        return False, ""

    # 1) فائزة اليوم (Purchases >= 1 و CPA < $7.00 في 1D و2D و3D)
    if p1 >= 1 and cpa1 is not None and cpa1 < 7.00:
        return True, f"1D Winner (${sp1:.2f} / {p1}p / CPA=${cpa1:.2f})"

    # 2) فائزة في آخر 3 أيام (Purchases >= 1 و CPA < $7.00 في 3D) بشرط ألا تكون خاسرة اليوم أو في 2D بـ >= $4.00 بدون مبيعة
    if p3 >= 1 and cpa3 is not None and cpa3 < 7.00:
        return True, f"3D Winner (${sp3:.2f} / {p3}p / CPA=${cpa3:.2f} | 1D=${sp1:.2f})"

    return False, ""


def is_losing_squad(st1: dict, st2: dict, st3: dict, st4: dict | None = None) -> tuple[bool, str]:
    # بأمر المستخدم (2026-10-30): أي أدست تصرف >= $4.00 بدون بيعة تُطفأ ولو كان سعر البيعة فيها >= $7.00 أيضاً تُطفأ
    for win_label, st in [("1D", st1), ("2D", st2), ("3D", st3)]:
        if st["purchases"] > 0 and st["cpa"] is not None and st["cpa"] >= 7.00:
            if win_label != "1D" and st1["purchases"] >= 1 and st1["cpa"] is not None and st1["cpa"] < 7.00:
                continue
            return True, f"[{win_label} AdSquad CPA >= $7.00] Spend=${st['spend']:.2f}, Purchases={st['purchases']}, CPA=${st['cpa']:.2f}"
    for win_label, st in [("1D", st1), ("2D", st2), ("3D", st3)]:
        if st["purchases"] == 0 and st["spend"] >= 4.00:
            if win_label != "1D" and st1["purchases"] >= 1 and st1["cpa"] is not None and st1["cpa"] < 7.00:
                continue
            return True, f"[{win_label} AdSquad >= $4.00 No-Sale Stop] Spend=${st['spend']:.2f}, Purchases=0"

    win_ok, _ = is_winning_squad(st1, st2, st3, st4)
    if win_ok:
        return False, ""

    if st4 is not None:
        if st4["purchases"] == 0 and st4["spend"] >= 4.00:
            return True, f"[4D AdSquad >= $4.00 No-Sale Stop] Spend=${st4['spend']:.2f}, Purchases=0"
        if st4["purchases"] > 0 and st4["cpa"] is not None and st4["cpa"] >= 7.00:
            return True, f"[4D AdSquad CPA >= $7.00] Spend=${st4['spend']:.2f}, Purchases={st4['purchases']}, CPA=${st4['cpa']:.2f}"
    return False, ""

restored_total = []
paused_losing_total = []

sheet_sales_multi = {}
try:
    from deep_multiwindow_account_auditor import fetch_sheet_multiwindow_sales
    sheet_sales_multi = fetch_sheet_multiwindow_sales() or {}
except Exception:
    sheet_sales_multi = {}

for acc_id, acc_name in ACCOUNTS:
    print(f"\n==================== {acc_name} ({acc_id}) ====================")
    c_st_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=0)}
    c_st_2d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=1)}
    c_st_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=2)}
    c_st_7d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=6)}
    stats_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=0)}
    stats_2d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=1)}
    stats_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=2)}
    stats_4d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=3)}
    stats_7d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=6)}
    empty_st = {"spend": 0.0, "purchases": 0, "swipes": 0, "cpa": None, "cpc": None}

    camps = sm.api_get_paginated(f"/adaccounts/{acc_id}/campaigns?limit=200", token, "campaigns", "campaign")
    for c in camps:
        cid = c["id"]
        cname = c.get("name", "")
        # ممنوع منعاً باتاً إعادة تشغيل أي حملة مطفأة PAUSED نهائياً
        if c.get("status") != "ACTIVE":
            continue
        if sm.is_gulf_034_campaign(cname) or cid in getattr(sm, "ALLOWED_GULF_CAMPAIGN_IDS", set()):
            continue
        created_ts = str(c.get("created_at") or "")
        is_recent_camp = bool(created_ts and created_ts >= "2026-10-23T18:00:00")
        if (cid not in sm.ALLOWED_30X_CAMPAIGN_IDS and not is_recent_camp) or sm.is_cross_monitored_product_campaign(cname) or cid == "66f9bf48-6e76-4601-911f-a4d9c9a71810":
            c["status"] = "PAUSED"
            sm.api_put(f"/adaccounts/{acc_id}/campaigns", token, {"campaigns": [c]})
            print(f"  🛑 [PAUSED NON-ALLOWED CAMPAIGN] '{cname}' ({cid})")
            continue

        scfg = SCALING_CAMPAIGNS.get(cid)
        is_ded_test_camp = sm.is_dedicated_snap_test_campaign(cid, cname)
        squads = sm.api_get_paginated(f"/campaigns/{cid}/adsquads?limit=1000", token, "adsquads", "adsquad", max_pages=4)
        act_sq_ids = [s_x["id"] for s_x in squads if s_x and s_x.get("status") == "ACTIVE"]

        has_15_violation = False
        c_1d_healthy = False
        no_sale_camp_limit = 10.00
        max_cpa_camp_limit = 11.00
        for sq_map_w, camp_map_w, sh_key_w in (
            (stats_1d, c_st_1d, "1d"),
            (stats_2d, c_st_2d, "2d"),
            (stats_3d, c_st_3d, "3d"),
            (stats_7d, c_st_7d, "7d"),
        ):
            if is_ded_test_camp:
                break
            sp_sq_act = round(sum(sq_map_w.get(sid_x, empty_st).get("spend", 0.0) for sid_x in act_sq_ids), 2)
            sp_camp = round(camp_map_w.get(cid, empty_st).get("spend", 0.0), 2)
            if sh_key_w == "1d" or cid not in sm.FOUR_SQUAD_CAMPAIGN_IDS:
                sp_c = max(sp_sq_act, sp_camp)
            else:
                sp_c = min(sp_sq_act, sp_camp) if sp_camp > 0 else sp_sq_act
            pu_sq_act = sum(sq_map_w.get(sid_x, empty_st).get("purchases", 0) for sid_x in act_sq_ids)
            pu_camp = camp_map_w.get(cid, empty_st).get("purchases", 0)
            sh_map_w = sheet_sales_multi.get(sh_key_w, {}) or {}
            pu_sh = sh_map_w.get(cid.lower(), 0)
            pu_c = max(pu_sq_act, pu_camp, pu_sh)
            cpa_c = round(sp_c / pu_c, 2) if pu_c > 0 else None
            if sh_key_w in ("1d", "2d", "3d") and pu_c > 0 and cpa_c is not None and cpa_c <= max_cpa_camp_limit:
                c_1d_healthy = True
            if sh_key_w != "1d" and c_1d_healthy:
                continue
            if (pu_c > 0 and cpa_c is not None and cpa_c > max_cpa_camp_limit) or (pu_c == 0 and sp_c >= no_sale_camp_limit):
                has_15_violation = True
                break

        if has_15_violation:
            c["status"] = "PAUSED"
            sm.api_put(f"/adaccounts/{acc_id}/campaigns", token, {"campaigns": [c]})
            act_sq_objs = [s_x for s_x in squads if s_x and s_x.get("status") == "ACTIVE"]
            for s_x in act_sq_objs:
                s_x["status"] = "PAUSED"
            for i_x in range(0, len(act_sq_objs), 20):
                sm.api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": act_sq_objs[i_x:i_x+20]})
            print(f"  🛑 [PAUSED LOSING CAMPAIGN] '{cname}' ({cid}) + {len(act_sq_objs)} AdSquads")
            continue

        print(f"\n--- Campaign: {cname} ({cid}) ---")

        to_update_squads = []
        active_copy_cnt_init = sum(
            1
            for s_i in squads
            if s_i.get("status") == "ACTIVE"
            and sm.is_scaling_winner_adsquad(s_i.get("name", ""), cname)
            and not sm.is_non_copy_test_adsquad(s_i.get("name", ""), cname)
        )

        # 1) فحص المجموعات النشطة لإيقاف أي أدست تصرف > $7.00 بدون بيعة أو CPA >= $8.00، وحماية المجموعات الرابحة (< $8.00)
        for sq in squads:
            sq_id = sq["id"]
            sq_name = sq.get("name", "")
            st1 = stats_1d.get(sq_id, empty_st)
            st2 = stats_2d.get(sq_id, empty_st)
            st3 = stats_3d.get(sq_id, empty_st)
            st4 = stats_4d.get(sq_id, empty_st)
            st7 = stats_7d.get(sq_id, empty_st)

            if sq.get("status") == "PAUSED":
                if is_ded_test_camp:
                    continue
                # في حملات الـ 30 أدست والـ 10 أدست الموازية النشطة: إعادة تفعيل أي أدست رابحة (CPA < $7.00 في 1D أو 3D وصرف اليوم < $4.00) أُطفئت بالخطأ
                max_cap_for_restore = 10 if cid in getattr(sm, "TEN_SQUAD_CAMPAIGN_IDS", set()) else (4 if cid in sm.FOUR_SQUAD_CAMPAIGN_IDS else 30)
                if (cid in sm.ALLOWED_30X_CAMPAIGN_IDS or "30 أدست" in cname or "30 ادست" in cname or "10 أدست" in cname) and not sq_name.strip().lower().startswith("base -") and active_copy_cnt_init < max_cap_for_restore:
                    win_ok_p, win_rsn_p = is_winning_squad(st1, st2, st3, st4, st7)
                    if win_ok_p:
                        sq["status"] = "ACTIVE"
                        active_copy_cnt_init += 1
                        # أمر المستخدم الصارم (2026-11-01): لا يقرب للبيد لا يطلع ولا ينزل لعند ما يقول المستخدم
                        to_update_squads.append(sq)
                        if sq_id in history.get("adsquads", {}):
                            del history["adsquads"][sq_id]
                        restored_total.append((acc_name, cname, sq_name, sq_id, win_rsn_p))
                        print(f"  ♻️ [RESTORED PAUSED WINNING ADSQUAD] {sq_name} ({sq_id}) | {win_rsn_p}")
                        ads_in_sq = sm.api_get_paginated(f"/adsquads/{sq_id}/ads?limit=100", token, "ads", "ad")
                        active_ads = [a for a in ads_in_sq if a.get("status") == "ACTIVE"]
                        if not active_ads and ads_in_sq:
                            for a in ads_in_sq:
                                a["status"] = "ACTIVE"
                            sm.api_put(f"/adsquads/{sq_id}/ads", token, {"ads": ads_in_sq})
                continue

            elif sq.get("status") == "ACTIVE":
                # أمر المستخدم الصارم (2026-11-01): لا يقرب للبيد لا يطلع ولا ينزل لعند ما يقول المستخدم
                is_ded_sq = is_ded_test_camp or sm.is_dedicated_snap_test_campaign(cid, cname, sq_name)
                if _is_future_sched(sq) or is_ded_sq:
                    continue
                lose_ok, lose_rsn = is_losing_squad(st1, st2, st3, st4)
                if lose_ok and not sm.is_non_copy_test_adsquad(sq_name, cname):
                    sq["status"] = "PAUSED"
                    to_update_squads.append(sq)
                    history.setdefault("adsquads", {})[sq_id] = {
                        "name": sq_name,
                        "campaign": cname,
                        "reason": lose_rsn,
                        "paused_at": datetime.now(timezone.utc).isoformat(),
                    }
                    paused_losing_total.append((acc_name, cname, sq_name, sq_id, lose_rsn))
                    print(f"  🛑 [PAUSED LOSING ADSQUAD] {sq_name} ({sq_id}) | {lose_rsn}")
                    continue

                win_ok_act, win_rsn_act = is_winning_squad(st1, st2, st3, st4, st7)
                if win_ok_act:
                    if "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" in (sq.get("delivery_status") or []):
                        ads_in_sq = sm.api_get_paginated(f"/adsquads/{sq_id}/ads?limit=100", token, "ads", "ad")
                        active_ads = [a for a in ads_in_sq if a.get("status") == "ACTIVE"]
                        if not active_ads and ads_in_sq:
                            ads_to_enable = []
                            for a in ads_in_sq:
                                a["status"] = "ACTIVE"
                                ads_to_enable.append(a)
                            if ads_to_enable:
                                sm.api_put(f"/adsquads/{sq_id}/ads", token, {"ads": ads_to_enable})
                                print(f"     ↳ Re-enabled {len(ads_to_enable)} ads inside already-ACTIVE winning squad {sq_name} ({win_rsn_act})")
                    continue
                continue

        if to_update_squads:
            for i in range(0, len(to_update_squads), 20):
                sm.api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": to_update_squads[i:i+20]})
                time.sleep(0.4)

        # 2) شرط عدم تجاوز 4 مجموعات أدست لحملات الـ 4 أدست، و 10 أدست لحملات الـ 10 أدست، و 30 أدست لحملات الـ 30 أدست الموازية:
        if scfg:
            max_cap_sq = 10 if cid in getattr(sm, "TEN_SQUAD_CAMPAIGN_IDS", set()) else (4 if cid in sm.FOUR_SQUAD_CAMPAIGN_IDS else 30)
            squads_after = squads
            active_copy = [
                s for s in squads_after
                if s.get("status") == "ACTIVE"
                and not sm.is_non_copy_test_adsquad(s.get("name", ""), cname)
                and sm.is_scaling_winner_adsquad(s.get("name", ""), cname)
            ]
            if len(active_copy) > max_cap_sq:
                def _prio(s_obj):
                    sid = s_obj["id"]
                    s1 = stats_1d.get(sid, empty_st)
                    s2 = stats_2d.get(sid, empty_st)
                    s3 = stats_3d.get(sid, empty_st)
                    s4 = stats_4d.get(sid, empty_st)
                    s7 = stats_7d.get(sid, empty_st)
                    w_ok, _ = is_winning_squad(s1, s2, s3, s4, s7)
                    return (
                        1 if w_ok else 0,
                        1 if _is_future_sched(s_obj) else 0,
                        s1["purchases"] + s3["purchases"],
                        round(s1["spend"], 2),
                        round(s3["spend"], 2),
                    )
                sorted_copy = sorted(active_copy, key=_prio, reverse=True)
                keep_4 = sorted_copy[:max_cap_sq]
                trim_excess = sorted_copy[max_cap_sq:]
                for s_trim in trim_excess:
                    s_trim["status"] = "PAUSED"
                    print(f"  ✂️ [TRIM EXCESS ABOVE {max_cap_sq} ADSQUADS] {s_trim.get('name')} ({s_trim['id']}) | 1D spend=${stats_1d.get(s_trim['id'], empty_st)['spend']:.2f}")
                for i in range(0, len(trim_excess), 20):
                    sm.api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": trim_excess[i:i+20]})
                    time.sleep(0.3)

sm.save_paused_history(history)
print(f"\n================ SUMMARY ================")
print(f"Total Winning Squads Restored: {len(restored_total)}")
for item in restored_total:
    print(f"  + [{item[0]}] {item[1]} | {item[2]} ({item[3]}) -> {item[4]}")
print(f"Total Losing Squads Paused: {len(paused_losing_total)}")
for item in paused_losing_total:
    print(f"  - [{item[0]}] {item[1]} | {item[2]} ({item[3]}) -> {item[4]}")
