from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from snap_auth import API_BASE, api_get, load_config, refresh_access_token
from snap_monitor import (
    ACCOUNT_TZ,
    TARGET_ACCOUNT_ID,
    api_put,
    fetch_account_breakdown_stats,
    load_paused_history,
    save_paused_history,
)
from tiktok_monitor import (
    ACCOUNTS as TT_ACCOUNTS,
    build_session,
    extract_taager_product_key_tt,
    fetch_ads,
    fetch_campaigns,
    fetch_expanded_materials,
    load_cookies,
    parse_draft_ids,
    pause_campaign,
    pause_smart_plus_video,
)

CAMPAIGNS_CONFIG_FILE = BASE_DIR / "maybach_timex_campaigns_21_10.json"
TIKTOK_AADVID = "7631763556322148369"  # ROELLINGER RECYCLING (UTC+2)
TIKTOK_UTC_OFFSET = 2

PRODUCT_DEFS = [
    {
        "key": "maybach",
        "label": "وسادة الرأس والرقبة مايباخ",
        "snap_keywords": ("مايباخ", "maybach"),
        "tt_keywords": ("maybach", "مايباخ"),
    },
    {
        "key": "timex",
        "label": "صندوق دفاتر الخط السحرية TIME X",
        "snap_keywords": ("time x", "timex", "تايم إكس", "دفاتر"),
        "tt_keywords": ("timex", "time x", "دفاتر"),
    },
    {
        "key": "warmer",
        "label": "مسخن رضاعات الحليب اللاسلكي المحمول",
        "snap_keywords": ("مسخن الرضاعات", "مسخن رضاعات", "warmer", "bottle warmer"),
        "tt_keywords": ("bottle warmer", "warmer", "مسخن الرضاعات", "مسخن رضاعات"),
    },
]

# القواعد الـ 4 المحددة من المستخدم للمنتجين (وسادة مايباخ + صندوق دفاتر TIME X):
# 1. لو وصل مجموع الصرف في تيكتوك وسناب معاً إلى >= $20.00 في أي منتج بدون مبيعة -> يطفي الحملة في سناب وتيكتوك
# 2. لو وصل سعر البيعة المشترك (مجموع الصرف / مجموع المبيعات في سناب وتيكتوك معاً) إلى >= $20.00 -> يطفي الحملتين في سناب وتيكتوك
# 3. لو صرف أكثر من > $20.00 وسعر البيعة المشترك أكثر من > $8.00 للمنتج -> يطفي مباشرة جميع الفيديوهات التي ما جابت مبيعات ويخلي فقط الفيديو اللي جاب حتى لو الآخرين لم يصرفوا ولا حاجة ($0)
# 4. لو CPC وصل إلى أكثر من > $0.30 في أي فيديو بعد صرف >= $0.80 -> يطفي الفيديو مباشرة إلا في حالة جاب بيعة (>= 1) يخليه
COMBINED_ZERO_SALE_SPEND_LIMIT = 20.00
COMBINED_MAX_CPA_STOP_CAMPAIGNS = 20.00
COMBINED_PRUNE_MIN_SPEND = 20.00
COMBINED_PRUNE_CPA_THRESHOLD = 7.00
VIDEO_CPC_MIN_SPEND = 0.80
VIDEO_MAX_CPC = 0.30


def load_product_campaign_ids() -> dict:
    if CAMPAIGNS_CONFIG_FILE.exists():
        try:
            return json.loads(CAMPAIGNS_CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {}


def match_snap_campaigns(all_snap_camps: list[dict], prod_def: dict, saved_cfg: dict, snap_token: str = "", snap_stats_7d: dict = None) -> list[dict]:
    key = prod_def["key"]
    saved_snap_id = str(saved_cfg.get(key, {}).get("snapchat", {}).get("campaign_id") or "")
    matched = []
    seen_ids = set()
    for c in all_snap_camps:
        cid = str(c.get("id") or "")
        cname = (c.get("name") or "").lower()
        has_spend = bool(snap_stats_7d and snap_stats_7d.get(cid, {}).get("spend", 0.0) > 0)
        if c.get("status") != "ACTIVE" and cid != saved_snap_id and not has_spend:
            continue
        if (saved_snap_id and cid == saved_snap_id) or any(k in cname for k in prod_def["snap_keywords"]):
            matched.append(c)
            seen_ids.add(cid)
    if saved_snap_id and saved_snap_id not in seen_ids and snap_token:
        try:
            r_c = api_get(f"/campaigns/{saved_snap_id}", snap_token)
            c_obj = r_c.get("campaigns", [{}])[0].get("campaign", {})
            if c_obj and c_obj.get("id"):
                matched.append(c_obj)
        except Exception:
            pass
    return matched


def match_tiktok_campaigns(all_tt_camps: list[dict], prod_def: dict, saved_cfg: dict) -> list[dict]:
    key = prod_def["key"]
    saved_tt_id = str(saved_cfg.get(key, {}).get("tiktok", {}).get("campaign_id") or "")
    matched = []
    for c in all_tt_camps:
        cid = str(c.get("campaign_id") or "")
        cname = (c.get("campaign_name") or "").lower()
        if (saved_tt_id and cid == saved_tt_id) or any(k in cname for k in prod_def["tt_keywords"]):
            matched.append(c)
    return matched


def pause_snap_campaign(token: str, ad_account_id: str, camp_obj: dict, reason: str, history: dict, now_str: str, dry_run: bool = False):
    cid = camp_obj["id"]
    cname = camp_obj.get("name", cid)
    if camp_obj.get("status") != "ACTIVE":
        print(f"   ℹ️ حملة سناب '{cname}' ({cid}) متوقفة مسبقاً.")
        return False
    if dry_run:
        print(f"   🔍 [DRY-RUN] سيتم إيقاف حملة سناب '{cname}' ({cid}) | {reason}")
        return True
    camp_payload = dict(camp_obj)
    camp_payload["status"] = "PAUSED"
    api_put(f"/adaccounts/{ad_account_id}/campaigns", token, {"campaigns": [camp_payload]})
    history.setdefault("cross_paused_campaigns", {})[f"snap:{cid}"] = {
        "platform": "snapchat",
        "campaign_id": cid,
        "campaign_name": cname,
        "reason": reason,
        "paused_at": now_str,
    }
    save_paused_history(history)
    print(f"   🛑 [Snapchat Campaign Paused] تم إيقاف حملة سناب '{cname}' ({cid}) | {reason}")
    return True


def pause_tt_campaign_wrapper(session, headers, aadvid: str, camp_row: dict, reason: str, history: dict, now_str: str, dry_run: bool = False):
    cid = str(camp_row.get("campaign_id") or "")
    cname = camp_row.get("campaign_name") or cid
    is_active = camp_row.get("primary_status") not in ("disable", "delete") and str(camp_row.get("opt_status")) == "0"
    if not is_active:
        print(f"   ℹ️ حملة تيك توك '{cname}' ({cid}) متوقفة مسبقاً.")
        return False
    if dry_run:
        print(f"   🔍 [DRY-RUN] سيتم إيقاف حملة تيك توك '{cname}' ({cid}) | {reason}")
        return True
    ok, resp_txt = pause_campaign(session, headers, aadvid, cid, operation="disable")
    if ok:
        history.setdefault("cross_paused_campaigns", {})[f"tiktok:{cid}"] = {
            "platform": "tiktok",
            "aadvid": aadvid,
            "campaign_id": cid,
            "campaign_name": cname,
            "reason": reason,
            "paused_at": now_str,
        }
        save_paused_history(history)
        print(f"   🛑 [TikTok Campaign Paused] تم إيقاف حملة تيك توك '{cname}' ({cid}) | {reason}")
        return True
    print(f"   ⚠️ فشل إيقاف حملة تيك توك '{cname}' ({cid}): {resp_txt}")
    return False


def run_cross_product_monitor(dry_run: bool = False):
    print("\n======================================================================")
    print("🔗 بدء المراقب المشترك (Snapchat + TikTok Combined) لحملتي مايباخ و TIME X")
    print("======================================================================")

    history = load_paused_history()
    saved_cfg = load_product_campaign_ids()
    now_sa = datetime.now(ACCOUNT_TZ)
    now_str = now_sa.strftime("%Y-%m-%d %H:%M GMT+3")

    # 1. Authenticate & Fetch Snapchat Data (Today + 3-Day + 7-Day)
    snap_cfg = load_config()
    snap_token = refresh_access_token(snap_cfg)
    all_snap_camps = []
    next_url = f"/adaccounts/{TARGET_ACCOUNT_ID}/campaigns?limit=1000"
    while next_url:
        camps_resp = api_get(next_url, snap_token)
        all_snap_camps.extend([w.get("campaign", {}) for w in camps_resp.get("campaigns", []) if w.get("campaign")])
        next_url = camps_resp.get("paging", {}).get("next_link") or ""

    snap_camp_1d = {x["id"]: x for x in fetch_account_breakdown_stats(snap_token, TARGET_ACCOUNT_ID, "campaign", days_back=0)}
    snap_camp_3d = {x["id"]: x for x in fetch_account_breakdown_stats(snap_token, TARGET_ACCOUNT_ID, "campaign", days_back=2)}
    snap_camp_7d = {x["id"]: x for x in fetch_account_breakdown_stats(snap_token, TARGET_ACCOUNT_ID, "campaign", days_back=6)}

    snap_ad_1d = {x["id"]: x for x in fetch_account_breakdown_stats(snap_token, TARGET_ACCOUNT_ID, "ad", days_back=0)}
    snap_ad_3d = {x["id"]: x for x in fetch_account_breakdown_stats(snap_token, TARGET_ACCOUNT_ID, "ad", days_back=2)}
    try:
        snap_ad_7d = {x["id"]: x for x in fetch_account_breakdown_stats(snap_token, TARGET_ACCOUNT_ID, "ad", days_back=6)}
    except Exception:
        snap_ad_7d = dict(snap_ad_3d)

    # 2. Authenticate & Fetch TikTok Data (Today + 3-Day + 7-Day)
    tt_session, tt_headers = build_session(load_cookies())
    now_tt = datetime.now(timezone(timedelta(hours=TIKTOK_UTC_OFFSET)))
    tt_today = now_tt.strftime("%Y-%m-%d")
    tt_3d_start = (now_tt - timedelta(days=2)).strftime("%Y-%m-%d")
    tt_7d_start = (now_tt - timedelta(days=6)).strftime("%Y-%m-%d")

    tt_camps_1d_list = fetch_campaigns(tt_session, tt_headers, TIKTOK_AADVID, tt_today, tt_today)
    tt_camps_3d_list = fetch_campaigns(tt_session, tt_headers, TIKTOK_AADVID, tt_3d_start, tt_today)
    tt_camps_7d_list = fetch_campaigns(tt_session, tt_headers, TIKTOK_AADVID, tt_7d_start, tt_today)

    tt_camp_1d = {str(c.get("campaign_id")): c for c in tt_camps_1d_list if c.get("campaign_id")}
    tt_camp_3d = {str(c.get("campaign_id")): c for c in tt_camps_3d_list if c.get("campaign_id")}
    tt_camp_7d = {str(c.get("campaign_id")): c for c in tt_camps_7d_list if c.get("campaign_id")}

    tt_ads_7d = fetch_ads(tt_session, tt_headers, TIKTOK_AADVID, tt_7d_start, tt_today)

    for prod_def in PRODUCT_DEFS:
        pkey = prod_def["key"]
        plabel = prod_def["label"]
        snap_camps = match_snap_campaigns(all_snap_camps, prod_def, saved_cfg, snap_token=snap_token, snap_stats_7d=snap_camp_7d)
        tt_camps = match_tiktok_campaigns(tt_camps_7d_list or tt_camps_3d_list, prod_def, saved_cfg)

        print(f"\n📦 المنتج: {plabel} ({pkey})")
        print(f"   • حملات سناب شات المطابقة: {[c.get('name') + ' (' + c.get('id') + ')' for c in snap_camps]}")
        print(f"   • حملات تيك توك المطابقة: {[str(c.get('campaign_name')) + ' (' + str(c.get('campaign_id')) + ')' for c in tt_camps]}")

        # حظر دائم لإيقاف حملات المنتجات الجديدة (مسخن الرضاعات، وسادة مايباخ، وصندوق دفاتر تايم إكس) نهائياً بأمر المستخدم
        if pkey in ("warmer", "maybach", "timex"):
            for sc in snap_camps:
                if sc.get("status") == "ACTIVE":
                    pause_snap_campaign(snap_token, TARGET_ACCOUNT_ID, sc, f"إيقاف نهائي لحملات منتج {plabel} بأمر المستخدم", history, now_str, dry_run=dry_run)
            for tc in tt_camps:
                pause_tt_campaign_wrapper(tt_session, tt_headers, TIKTOK_AADVID, tc, f"إيقاف نهائي لحملات منتج {plabel} بأمر المستخدم", history, now_str, dry_run=dry_run)
            print(f"   🛑 [Permanent Shutoff] منتج {plabel} ({pkey}) موقوف نهائياً في جميع المنصات.")
            continue

        # Keep no_duplicate_campaigns locked for these product campaigns so snap_monitor never clones 20 squads inside them
        for sc in snap_camps:
            history.setdefault("no_duplicate_campaigns", {})[sc["id"]] = {
                "name": sc.get("name", ""),
                "reason": "Single-AdSquad Cross-Platform Monitored Product Campaign",
                "updated_at": now_str,
            }
        for tc in tt_camps:
            tc_id = str(tc.get("campaign_id") or "")
            if tc_id:
                history.setdefault("no_duplicate_campaigns", {})[tc_id] = {
                    "name": tc.get("campaign_name", ""),
                    "aadvid": TIKTOK_AADVID,
                    "reason": "Single-Campaign Cross-Platform Monitored Product Campaign",
                    "updated_at": now_str,
                }
        save_paused_history(history)

        # Fetch all ads inside the matched Snapchat campaigns (and pause any accidental 'Copy' adsquad)
        snap_ads_by_camp = {}
        for sc in snap_camps:
            cid = sc["id"]
            try:
                r_sqs = api_get(f"/campaigns/{cid}/adsquads?limit=200", snap_token)
                for w_sq in r_sqs.get("adsquads", []):
                    sq_obj = w_sq.get("adsquad", {})
                    if sq_obj.get("status") == "ACTIVE" and "copy" in (sq_obj.get("name") or "").lower() and not dry_run:
                        sq_obj["status"] = "PAUSED"
                        api_put(f"/campaigns/{cid}/adsquads", snap_token, {"adsquads": [sq_obj]})
                        print(f"   🛑 تم إيقاف المجموعة المستنسخة غير المطلوبة '{sq_obj.get('name')}' داخل '{sc.get('name')}'")
                r_ads = api_get(f"/campaigns/{cid}/ads?limit=200", snap_token)
                snap_ads_by_camp[cid] = [w.get("ad", {}) for w in r_ads.get("ads", []) if w.get("ad")]
            except Exception as e:
                print(f"   ⚠️ تعذر جلب إعلانات حملة سناب {cid}: {e}")
                snap_ads_by_camp[cid] = []

        # Fetch all expanded materials inside the matched TikTok campaigns
        tt_camp_ids = {str(tc.get("campaign_id")) for tc in tt_camps if tc.get("campaign_id")}
        tt_cids_for_prod = [
            str(a.get("creative_id"))
            for a in tt_ads_7d
            if str(a.get("campaign_id")) in tt_camp_ids and a.get("creative_id")
        ]
        tt_mats_1d = fetch_expanded_materials(tt_session, tt_headers, TIKTOK_AADVID, tt_cids_for_prod, tt_today, tt_today) if tt_cids_for_prod else []
        tt_mats_3d = fetch_expanded_materials(tt_session, tt_headers, TIKTOK_AADVID, tt_cids_for_prod, tt_3d_start, tt_today) if tt_cids_for_prod else []
        tt_mats_7d = fetch_expanded_materials(tt_session, tt_headers, TIKTOK_AADVID, tt_cids_for_prod, tt_7d_start, tt_today) if tt_cids_for_prod else []

        tt_mat_1d_map = {f"{m.get('ad_id')}:{m.get('ad_material_draft_id')}:{m.get('main_entity_id')}": m for m in tt_mats_1d}
        tt_mat_3d_map = {f"{m.get('ad_id')}:{m.get('ad_material_draft_id')}:{m.get('main_entity_id')}": m for m in tt_mats_3d}
        tt_mat_7d_map = {f"{m.get('ad_id')}:{m.get('ad_material_draft_id')}:{m.get('main_entity_id')}": m for m in tt_mats_7d}
        all_tt_mat_keys = set(tt_mat_7d_map.keys()) | set(tt_mat_3d_map.keys()) | set(tt_mat_1d_map.keys())

        # =========================================================================
        # RULE 4: Early Video CPC Cut (Spend >= $0.80 and CPC > $0.30 -> Pause Video unless Sales >= 1)
        # "لو cpc وصل الى اكثر من 0.3 في اي فيدو بعد صرف 0.8$ يطفي الفيديو مباشرة الا في حالة جاب بيعة يخليه"
        # =========================================================================
        # 4A. Check Snapchat videos for this product
        for sc in snap_camps:
            cid = sc["id"]
            cname = sc.get("name", "")
            for ad in snap_ads_by_camp.get(cid, []):
                if ad.get("status") != "ACTIVE":
                    continue
                ad_id = ad["id"]
                ad_name = ad.get("name", ad_id)
                sq_id = ad.get("ad_squad_id", "")
                st1 = snap_ad_1d.get(ad_id, {"spend": 0.0, "purchases": 0, "swipes": 0, "cpc": 0.0})
                st3 = snap_ad_3d.get(ad_id, {"spend": 0.0, "purchases": 0, "swipes": 0, "cpc": 0.0})
                st7 = snap_ad_7d.get(ad_id, {"spend": 0.0, "purchases": 0, "swipes": 0, "cpc": 0.0})

                total_vid_sales = max(st1["purchases"], st3["purchases"], st7["purchases"])
                if total_vid_sales >= 1:
                    # "الا في حالة جاب بيعة يخليه"
                    continue

                cpc_reason = None
                for window_lbl, st_w in (("Today", st1), ("3-Day", st3), ("7-Day", st7)):
                    sp_w = st_w["spend"]
                    sw_w = st_w["swipes"]
                    cpc_w = st_w["cpc"]
                    if sp_w >= VIDEO_CPC_MIN_SPEND and (sw_w == 0 or cpc_w > VIDEO_MAX_CPC):
                        cpc_reason = (
                            f"[{pkey} Early CPC Rule - {window_lbl}] صرف الفيديو ${sp_w:.2f} (>= ${VIDEO_CPC_MIN_SPEND:.2f}) "
                            f"| النقرات: {sw_w} | CPC = ${cpc_w:.2f} (> ${VIDEO_MAX_CPC:.2f}) مع 0 مبيعات"
                        )
                        break

                if cpc_reason:
                    if dry_run:
                        print(f"   🔍 [Snap Video CPC Cut] '{ad_name}' ({ad_id}) | {cpc_reason}")
                    else:
                        ad["status"] = "PAUSED"
                        api_put(f"/adsquads/{sq_id}/ads", snap_token, {"ads": [ad]})
                        history["ads"][ad_id] = {
                            "name": ad_name,
                            "campaign": cname,
                            "adsquad": sq_id,
                            "reason": cpc_reason,
                            "paused_at": now_str,
                        }
                        save_paused_history(history)
                        print(f"   🛑 [Snap Video CPC Cut] تم إيقاف فيديو سناب '{ad_name}' في '{cname}' | {cpc_reason}")
                        time.sleep(0.2)

        # 4B. Check TikTok videos for this product
        for m_key in all_tt_mat_keys:
            m1 = tt_mat_1d_map.get(m_key, {})
            m3 = tt_mat_3d_map.get(m_key, {})
            m7 = tt_mat_7d_map.get(m_key, {})
            base_m = m3 or m7 or m1

            opt_st = str(base_m.get("mix_material_aco_opt_status") or "0")
            prim_st = str(base_m.get("material_primary_status") or "")
            if opt_st != "0" or prim_st in ("disable", "delete"):
                continue

            sp1 = float(m1.get("stat_cost") or 0.0)
            clk1 = int(float(m1.get("click_cnt") or 0))
            cpc1 = float(m1.get("cpc") or (sp1 if clk1 == 0 else 0.0))
            cv1 = int(float(m1.get("time_attr_convert_cnt") or 0))

            sp3 = float(m3.get("stat_cost") or 0.0)
            clk3 = int(float(m3.get("click_cnt") or 0))
            cpc3 = float(m3.get("cpc") or (sp3 if clk3 == 0 else 0.0))
            cv3 = int(float(m3.get("time_attr_convert_cnt") or 0))

            sp7 = float(m7.get("stat_cost") or sp3)
            clk7 = int(float(m7.get("click_cnt") or clk3))
            cpc7 = float(m7.get("cpc") or (sp7 if clk7 == 0 else 0.0))
            cv7 = int(float(m7.get("time_attr_convert_cnt") or cv3))

            total_vid_sales = max(cv1, cv3, cv7)
            if total_vid_sales >= 1:
                # "الا في حالة جاب بيعة يخليه"
                continue

            cpc_reason = None
            tt_cpc_min_spend = 1.00
            tt_max_cpc = 0.25
            for window_lbl, sp_w, clk_w, cpc_w in (
                ("Today", sp1, clk1, cpc1),
                ("3-Day", sp3, clk3, cpc3),
                ("7-Day", sp7, clk7, cpc7),
            ):
                eff_cpc_w = max(round(cpc_w, 2), round(sp_w / clk_w, 2)) if clk_w > 0 else sp_w
                if sp_w >= tt_cpc_min_spend and (clk_w == 0 or eff_cpc_w > tt_max_cpc):
                    cpc_reason = (
                        f"[{pkey} Early CPC Rule - {window_lbl}] Spend ${sp_w:.2f} (>= ${tt_cpc_min_spend:.2f}) "
                        f"| Clicks={clk_w} | CPC=${eff_cpc_w:.2f} (> ${tt_max_cpc:.2f}) with 0 sales"
                    )
                    break

            if cpc_reason:
                ad_id = str(base_m.get("ad_id") or "")
                vname = base_m.get("main_entity_name") or m_key
                draft_ids = parse_draft_ids(base_m.get("ad_material_draft_id"))
                draft_str = ",".join(draft_ids)
                hist_key = f"{TIKTOK_AADVID}:{ad_id}:{draft_str}"
                if dry_run:
                    print(f"   🔍 [TikTok Video CPC Cut] '{vname}' (draft={draft_str}) | {cpc_reason}")
                else:
                    ok_p, resp_p = pause_smart_plus_video(tt_session, tt_headers, TIKTOK_AADVID, ad_id, base_m, operation="disable")
                    if ok_p:
                        history.setdefault("tiktok_videos", {})[hist_key] = {
                            "aadvid": TIKTOK_AADVID,
                            "account_name": "ROELLINGER RECYCLING",
                            "ad_id": ad_id,
                            "creative_id": str(base_m.get("creative_id") or ""),
                            "video_name": vname,
                            "draft_ids": draft_ids,
                            "reason": cpc_reason,
                            "spend_1d": sp1,
                            "conv_1d": cv1,
                            "spend_3d": sp3,
                            "conv_3d": cv3,
                            "paused_at": datetime.now(timezone.utc).isoformat(),
                        }
                        save_paused_history(history)
                        print(f"   🛑 [TikTok Video CPC Cut] تم إيقاف فيديو تيك توك '{vname}' (draft={draft_str}) | {cpc_reason}")
                    else:
                        print(f"   ⚠️ تعذر إيقاف فيديو تيك توك '{vname}': {resp_p}")

        # =========================================================================
        # COMBINED SNAPCHAT + TIKTOK TOTALS FOR THIS PRODUCT
        # =========================================================================
        snap_sp_1d = sum(snap_camp_1d.get(c["id"], {}).get("spend", 0.0) for c in snap_camps)
        snap_cv_1d = sum(snap_camp_1d.get(c["id"], {}).get("purchases", 0) for c in snap_camps)
        snap_sp_3d = sum(snap_camp_3d.get(c["id"], {}).get("spend", 0.0) for c in snap_camps)
        snap_cv_3d = sum(snap_camp_3d.get(c["id"], {}).get("purchases", 0) for c in snap_camps)
        snap_sp_7d = sum(snap_camp_7d.get(c["id"], {}).get("spend", 0.0) for c in snap_camps)
        snap_cv_7d = sum(snap_camp_7d.get(c["id"], {}).get("purchases", 0) for c in snap_camps)

        def _tt_stat(cmap, cid_set):
            sp = 0.0
            cv = 0
            for cid_s in cid_set:
                rd = (cmap.get(cid_s) or {}).get("row_data") or {}
                sp += float(rd.get("stat_cost") or 0.0)
                cv += int(float(rd.get("time_attr_convert_cnt") or 0))
            return round(sp, 2), cv

        tt_sp_1d, tt_cv_1d = _tt_stat(tt_camp_1d, tt_camp_ids)
        tt_sp_3d, tt_cv_3d = _tt_stat(tt_camp_3d, tt_camp_ids)
        tt_sp_7d, tt_cv_7d = _tt_stat(tt_camp_7d, tt_camp_ids)

        comb_sp_1d = round(snap_sp_1d + tt_sp_1d, 2)
        comb_cv_1d = snap_cv_1d + tt_cv_1d
        comb_cpa_1d = round(comb_sp_1d / comb_cv_1d, 2) if comb_cv_1d > 0 else None

        comb_sp_3d = round(snap_sp_3d + tt_sp_3d, 2)
        comb_cv_3d = snap_cv_3d + tt_cv_3d
        comb_cpa_3d = round(comb_sp_3d / comb_cv_3d, 2) if comb_cv_3d > 0 else None

        comb_sp_7d = round(snap_sp_7d + tt_sp_7d, 2)
        comb_cv_7d = snap_cv_7d + tt_cv_7d
        comb_cpa_7d = round(comb_sp_7d / comb_cv_7d, 2) if comb_cv_7d > 0 else None

        # Use the broader accumulated window (7D >= 3D >= 1D) so multi-day spend never slips through
        tot_spend = max(comb_sp_7d, comb_sp_3d, comb_sp_1d)
        tot_sales = max(comb_cv_7d, comb_cv_3d, comb_cv_1d)
        tot_cpa = round(tot_spend / tot_sales, 2) if tot_sales > 0 else None

        print(
            f"   📊 الإجمالي المشترك (Snap + TikTok) للمنتج '{plabel}':\n"
            f"      - اليوم (1D): Snap=${snap_sp_1d:.2f} ({snap_cv_1d} مبيعة) + TikTok=${tt_sp_1d:.2f} ({tt_cv_1d} مبيعة) "
            f"=> المجموع = ${comb_sp_1d:.2f} | المبيعات = {comb_cv_1d} | CPA = ${comb_cpa_1d}\n"
            f"      - التراكمي (7D): Snap=${snap_sp_7d:.2f} ({snap_cv_7d} مبيعة) + TikTok=${tt_sp_7d:.2f} ({tt_cv_7d} مبيعة) "
            f"=> المجموع = ${tot_spend:.2f} | المبيعات = {tot_sales} | CPA = ${tot_cpa}"
        )

        # =========================================================================
        # RULE 1: Combined Spend >= $20.00 with 0 Sales -> Pause BOTH Snap & TikTok Campaigns
        # "لو وصل مجموع الصرف في تيكتوك وسناب معاً الى 20$ في اي منتج بدون مبيعة يطفي الحملة في سناب وتيكتوك"
        # =========================================================================
        if tot_spend >= COMBINED_ZERO_SALE_SPEND_LIMIT and tot_sales == 0:
            reason_r1 = (
                f"[Cross-Platform Rule 1: >= $20 Zero-Sale Stop] مجموع الصرف المشترك (Snap + TikTok) = "
                f"${tot_spend:.2f} (>= ${COMBINED_ZERO_SALE_SPEND_LIMIT:.2f}) بدون أي مبيعة (0 مبيعات)"
            )
            print(f"   🚨 {reason_r1} -> إيقاف الحملتين في سناب وتيكتوك فوراً!")
            for sc in snap_camps:
                pause_snap_campaign(snap_token, TARGET_ACCOUNT_ID, sc, reason_r1, history, now_str, dry_run=dry_run)
            for tc in tt_camps:
                pause_tt_campaign_wrapper(tt_session, tt_headers, TIKTOK_AADVID, tc, reason_r1, history, now_str, dry_run=dry_run)
            continue

        # =========================================================================
        # RULE 2: Combined CPA >= $20.00 -> Pause BOTH Snap & TikTok Campaigns
        # "ولو وصل سعر البيعة الى 20$ ايضاً يطفي الحملتين"
        # =========================================================================
        if (
            (tot_sales >= 1 and tot_cpa is not None and tot_cpa >= COMBINED_MAX_CPA_STOP_CAMPAIGNS)
            or (comb_cv_1d >= 1 and comb_cpa_1d is not None and comb_cpa_1d >= COMBINED_MAX_CPA_STOP_CAMPAIGNS)
        ):
            active_cpa_val = tot_cpa if (tot_cpa is not None and tot_cpa >= COMBINED_MAX_CPA_STOP_CAMPAIGNS) else comb_cpa_1d
            reason_r2 = (
                f"[Cross-Platform Rule 2: Combined CPA >= $20 Stop] سعر البيعة المشترك (Snap + TikTok) وصل إلى "
                f"CPA = ${active_cpa_val:.2f} (>= ${COMBINED_MAX_CPA_STOP_CAMPAIGNS:.2f} | الصرف المشترك: ${tot_spend:.2f} | المبيعات: {tot_sales})"
            )
            print(f"   🚨 {reason_r2} -> إيقاف الحملتين في سناب وتيكتوك فوراً!")
            for sc in snap_camps:
                pause_snap_campaign(snap_token, TARGET_ACCOUNT_ID, sc, reason_r2, history, now_str, dry_run=dry_run)
            for tc in tt_camps:
                pause_tt_campaign_wrapper(tt_session, tt_headers, TIKTOK_AADVID, tc, reason_r2, history, now_str, dry_run=dry_run)
            continue

        # =========================================================================
        # RULE 3: Combined Spend > $20.00 and Combined CPA > $8.00 ->
        # Pause ALL videos that have 0 sales immediately and keep ONLY the video(s) that brought sales,
        # EVEN IF the other videos spent $0.00!
        # "ولو صرف اكثر من 20$ وسعر البيعة هوا اكثر من 8$ للمنتج فيقوم مباشرة باطفاء الفيديوز التي ماجابت ويخلي فقط الفيديو اللي جاب حتى لو الخرين لم يصرفو ولا حاجة"
        # =========================================================================
        is_r3_triggered = (
            (tot_spend > COMBINED_PRUNE_MIN_SPEND and tot_sales >= 1 and tot_cpa is not None and tot_cpa > COMBINED_PRUNE_CPA_THRESHOLD)
            or (comb_sp_1d > COMBINED_PRUNE_MIN_SPEND and comb_cv_1d >= 1 and comb_cpa_1d is not None and comb_cpa_1d > COMBINED_PRUNE_CPA_THRESHOLD)
        )
        if is_r3_triggered:
            cpa_triggered = tot_cpa if (tot_cpa and tot_cpa > COMBINED_PRUNE_CPA_THRESHOLD) else comb_cpa_1d
            reason_r3 = (
                f"[Cross-Platform Rule 3: Survivor-Only Pruning] الصرف المشترك ${tot_spend:.2f} (> $20.00) "
                f"وسعر البيعة المشترك CPA = ${cpa_triggered:.2f} (> $8.00) -> إيقاف كل فيديو لم يحقق مبيعات نهائياً (حتى لو صرف $0.00) والإبقاء فقط على الفيديو الرابح!"
            )
            print(f"   ✂️🔥 {reason_r3}")

            # 3A. Prune all 0-sale Snapchat ads in this product's campaign (including $0 spend ads)
            for sc in snap_camps:
                if sc.get("status") != "ACTIVE":
                    continue
                cid = sc["id"]
                cname = sc.get("name", "")
                for ad in snap_ads_by_camp.get(cid, []):
                    ad_id = ad["id"]
                    ad_name = ad.get("name", ad_id)
                    sq_id = ad.get("ad_squad_id", "")
                    st1 = snap_ad_1d.get(ad_id, {"spend": 0.0, "purchases": 0})
                    st3 = snap_ad_3d.get(ad_id, {"spend": 0.0, "purchases": 0})
                    st7 = snap_ad_7d.get(ad_id, {"spend": 0.0, "purchases": 0})
                    ad_sales = max(st1["purchases"], st3["purchases"], st7["purchases"])
                    ad_spend = max(st1["spend"], st3["spend"], st7["spend"])
                    if ad_sales == 0:
                        if ad.get("status") != "ACTIVE":
                            continue
                        if dry_run:
                            print(f"      🔍 [Snap Prune 0-Sale Video] '{ad_name}' (Spend=${ad_spend:.2f}, Sales=0)")
                        else:
                            ad["status"] = "PAUSED"
                            api_put(f"/adsquads/{sq_id}/ads", snap_token, {"ads": [ad]})
                            history["ads"][ad_id] = {
                                "name": ad_name,
                                "campaign": cname,
                                "adsquad": sq_id,
                                "reason": reason_r3,
                                "paused_at": now_str,
                            }
                            save_paused_history(history)
                            print(f"      🛑 [Snap Prune 0-Sale Video] تم إيقاف فيديو سناب '{ad_name}' (الصرف: ${ad_spend:.2f}، المبيعات: 0)")
                            time.sleep(0.2)
                    else:
                        if False and ad.get("status") != "ACTIVE" and "[H" not in ad_name and not dry_run:
                            ad["status"] = "ACTIVE"
                            api_put(f"/adsquads/{sq_id}/ads", snap_token, {"ads": [ad]})
                            history.get("ads", {}).pop(ad_id, None)
                            save_paused_history(history)
                            print(f"      ♻️🏆 [Snap Survivor Video Restored] إعادة تفعيل الفيديو الفائز '{ad_name}' (المبيعات: {ad_sales}، الصرف: ${ad_spend:.2f})")
                        else:
                            print(f"      🏆 [Snap Survivor Video Kept] الإبقاء على الفيديو الفائز '{ad_name}' (المبيعات: {ad_sales}، الصرف: ${ad_spend:.2f})")

            # 3B. Prune all 0-sale TikTok videos in this product's campaign (including $0 spend videos)
            for m_key in all_tt_mat_keys:
                m1 = tt_mat_1d_map.get(m_key, {})
                m3 = tt_mat_3d_map.get(m_key, {})
                m7 = tt_mat_7d_map.get(m_key, {})
                base_m = m3 or m7 or m1

                opt_st = str(base_m.get("mix_material_aco_opt_status") or "0")
                prim_st = str(base_m.get("material_primary_status") or "")
                if opt_st != "0" or prim_st in ("disable", "delete"):
                    continue

                sp1 = float(m1.get("stat_cost") or 0.0)
                cv1 = int(float(m1.get("time_attr_convert_cnt") or 0))
                sp3 = float(m3.get("stat_cost") or 0.0)
                cv3 = int(float(m3.get("time_attr_convert_cnt") or 0))
                sp7 = float(m7.get("stat_cost") or sp3)
                cv7 = int(float(m7.get("time_attr_convert_cnt") or cv3))

                mat_sales = max(cv1, cv3, cv7)
                mat_spend = max(sp1, sp3, sp7)
                vname = base_m.get("main_entity_name") or m_key
                ad_id = str(base_m.get("ad_id") or "")
                draft_ids = parse_draft_ids(base_m.get("ad_material_draft_id"))
                draft_str = ",".join(draft_ids)
                hist_key = f"{TIKTOK_AADVID}:{ad_id}:{draft_str}"

                if mat_sales == 0:
                    if dry_run:
                        print(f"      🔍 [TikTok Prune 0-Sale Video] '{vname}' (Spend=${mat_spend:.2f}, Sales=0)")
                    else:
                        ok_p, resp_p = pause_smart_plus_video(tt_session, tt_headers, TIKTOK_AADVID, ad_id, base_m, operation="disable")
                        if ok_p:
                            history.setdefault("tiktok_videos", {})[hist_key] = {
                                "aadvid": TIKTOK_AADVID,
                                "account_name": "ROELLINGER RECYCLING",
                                "ad_id": ad_id,
                                "creative_id": str(base_m.get("creative_id") or ""),
                                "video_name": vname,
                                "draft_ids": draft_ids,
                                "reason": reason_r3,
                                "spend_1d": sp1,
                                "conv_1d": cv1,
                                "spend_3d": sp3,
                                "conv_3d": cv3,
                                "paused_at": datetime.now(timezone.utc).isoformat(),
                            }
                            save_paused_history(history)
                            print(f"      🛑 [TikTok Prune 0-Sale Video] تم إيقاف فيديو تيك توك '{vname}' (الصرف: ${mat_spend:.2f}، المبيعات: 0)")
                        else:
                            print(f"      ⚠️ تعذر إيقاف فيديو تيك توك '{vname}': {resp_p}")
                else:
                    print(f"      🏆 [TikTok Survivor Video Kept] الإبقاء على الفيديو الفائز '{vname}' (المبيعات: {mat_sales}، الصرف: ${mat_spend:.2f})")

    # =========================================================================
    # SECTION B: TAAGER TEST PRODUCTS MONITOR (Per-Product Across Google Ads + TikTok)
    # "اريد امر لمراقب حملات تاجر في الاوتوميشن التي عملناها سابقاً ان يعرف هاذا مجرد تيست للمنتجات
    #  يعني لو مجموع صرف منتجات تاجر في الحسابات مجموعة لكل منتج تعدى 25$ بدون ولا بيعة يعني المنتج فاشل
    #  ويطفي كل الحملات سواء غوغل او تيكتوك ولو وصل سعر البيعة فيهم 27$ يطفيه انا اتكلم كل منتج على حدة"
    # =========================================================================
    run_taager_test_products_monitor(tt_session, tt_headers, history, now_str, dry_run=dry_run)


TAAGER_PRODUCT_DEFS = {
    "washer_hp909": {
        "key": "washer_hp909",
        "label": "غسالة الملابس المحمولة 3 في 1 HP-909 (تاجر)",
        "keywords": ("washer", "hp909", "hp-909", "غسالة", "غساله"),
        "sheet_cat": "washer",
    },
    "bakhoor_bw20": {
        "key": "bakhoor_bw20",
        "label": "مبخرة الملابس المرنة 360° BW-20 (تاجر)",
        "keywords": ("bakhoor", "bw20", "bw-20", "مبخرة", "مبخره"),
        "sheet_cat": "bakhoor",
    },
    "cooker_raf8019": {
        "key": "cooker_raf8019",
        "label": "موقد السيراميك الكهربائي راف 3500W R.8019 (تاجر)",
        "keywords": ("r8019", "r.8019", "8019", "raf ", "cooker", "موقد", "سيراميك"),
        "sheet_cat": "cooker",
    },
}

TAAGER_ZERO_SALE_SPEND_LIMIT = 25.00
TAAGER_MAX_CPA_LIMIT = 27.00


def _pause_gads_campaigns_for_taager_product(prod_key: str, prod_keywords: tuple, gads_camp_names: list[str], reason: str, dry_run: bool = False) -> list[str]:
    """Opens AdsPower CDP connection if available and pauses all Google Ads campaigns matching this Taager product."""
    paused_results = []
    if dry_run:
        for cn in gads_camp_names:
            paused_results.append(f"[DRY-RUN GAds Pause] '{cn}'")
        return paused_results
    try:
        from gads_hourly_tcpa_bumper import ACCOUNTS_META, find_open_adspower_ports, keep_offscreen
        from deep_multiwindow_account_auditor import select_date_preset, set_google_campaign_status_carefully
        import urllib.request
        import websocket

        ports = find_open_adspower_ports()
        for prof_id, meta in ACCOUNTS_META.items():
            port = ports.get(prof_id)
            if not port:
                continue
            ocid = meta["ocid"]
            euid = meta["euid"]
            u_param = meta["u"]
            c_param = meta["c"]
            acc_label = meta["name"]
            try:
                keep_offscreen(port)
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=4).read().decode("utf-8"))
                page_tabs = [t for t in tabs if t.get("type") == "page" and "ads.google.com" in (t.get("url") or "") and "/campaigns/new" not in (t.get("url") or "")]
                if not page_tabs:
                    continue
                ws = websocket.create_connection(
                    page_tabs[0]["webSocketDebuggerUrl"].replace("ws://localhost:", "ws://127.0.0.1:"),
                    suppress_origin=True,
                    timeout=30,
                )
                camps_url = f"https://ads.google.com/aw/campaigns?ocid={ocid}&euid={euid}&__u={u_param}&uscid={ocid}&__c={c_param}&authuser=0"
                ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": f"window.location.href = {json.dumps(camps_url)}; true;", "returnByValue": True}}))
                ws.recv()
                time.sleep(4.5)
                select_date_preset(ws, "Today", port=port)
                time.sleep(1.0)
                targets_to_pause = set(gads_camp_names)
                for cn_target in targets_to_pause:
                    res_p = set_google_campaign_status_carefully(ws, cn_target, "Pause", port=port)
                    if "SUCCESS" in str(res_p) or "ALREADY_" in str(res_p):
                        paused_results.append(f"{acc_label}: '{cn_target}' -> {res_p}")
                        print(f"   🛑 [Google Ads Taager Campaign Paused] [{acc_label}] '{cn_target}' -> {res_p} | {reason}")
                ws.close()
                keep_offscreen(port)
            except Exception as e_g:
                print(f"   ⚠️ تعذر إيقاف حملة Google Ads في {acc_label}: {e_g}")
    except Exception as e_outer:
        print(f"   ⚠️ تعذر الاتصال بـ Google Ads لإيقاف حملات تاجر: {e_outer}")
    return paused_results


def run_taager_test_products_monitor(tt_session, tt_headers, history: dict, now_str: str, dry_run: bool = False):
    print("\n========================================================================")
    print("🧪 [Taager Products Test Monitor] فحص منتجات تاجر قيد الاختبار (كل منتج على حدة عبر تيك توك + غوغل أدز)")
    print("   * القاعدة 1: مجموع صرف المنتج في الحسابات (غوغل + تيك توك) > $25.00 بدون ولا بيعة => المنتج فاشل وإيقاف كل حملاته في غوغل وتيك توك فوراً")
    print("   * القاعدة 2: سعر البيعة المشترك للمنتج (CPA) >= $27.00 => إيقاف كل حملات المنتج في غوغل وتيك توك فوراً")
    print("========================================================================")

    # 1. Load cached Google Ads stats for Taager campaigns (updated by deep_multiwindow_account_auditor.py)
    gads_cache_path = BASE_DIR / "taager_gads_latest_stats.json"
    gads_cached_camps = {}
    if gads_cache_path.exists():
        try:
            gads_cached_camps = (json.loads(gads_cache_path.read_text(encoding="utf-8")) or {}).get("campaigns") or {}
        except Exception:
            gads_cached_camps = {}

    # 2. Load Sheet multiwindow sales to catch any real orders on Lightfunnels for each Taager product
    sheet_data = {}
    try:
        from deep_multiwindow_account_auditor import fetch_sheet_multiwindow_sales
        sheet_data = fetch_sheet_multiwindow_sales() or {}
    except Exception:
        sheet_data = {}

    # 3. Scan all 6 TikTok accounts for any Taager product campaigns
    buckets = {}
    for p_key, p_def in TAAGER_PRODUCT_DEFS.items():
        buckets[p_key] = {
            "key": p_key,
            "label": p_def["label"],
            "keywords": p_def["keywords"],
            "tt_sp_1d": 0.0, "tt_cv_1d": 0,
            "tt_sp_3d": 0.0, "tt_cv_3d": 0,
            "tt_sp_7d": 0.0, "tt_cv_7d": 0,
            "gads_sp_1d": 0.0, "gads_cv_1d": 0.0,
            "gads_sp_3d": 0.0, "gads_cv_3d": 0.0,
            "gads_sp_7d": 0.0, "gads_cv_7d": 0.0,
            "tt_campaigns": [],
            "gads_campaigns": [],
        }

    for acc_name, aadvid, utc_offset in TT_ACCOUNTS:
        now_tz = datetime.now(timezone(timedelta(hours=utc_offset)))
        t_str = now_tz.strftime("%Y-%m-%d")
        two_str = (now_tz - timedelta(days=2)).strftime("%Y-%m-%d")
        six_str = (now_tz - timedelta(days=6)).strftime("%Y-%m-%d")
        try:
            c1_map = {str(c.get("campaign_id")): c for c in fetch_campaigns(tt_session, tt_headers, aadvid, t_str, t_str) if c.get("campaign_id")}
            c3_map = {str(c.get("campaign_id")): c for c in fetch_campaigns(tt_session, tt_headers, aadvid, two_str, t_str) if c.get("campaign_id")}
            c7_map = {str(c.get("campaign_id")): c for c in fetch_campaigns(tt_session, tt_headers, aadvid, six_str, t_str) if c.get("campaign_id")}
        except Exception as e_tt:
            print(f"   ⚠️ تعذر جلب حملات تيك توك من {acc_name}: {e_tt}")
            continue

        all_cids = set(c7_map.keys()) | set(c3_map.keys()) | set(c1_map.keys())
        for cid in all_cids:
            c_ref = c3_map.get(cid) or c7_map.get(cid) or c1_map.get(cid) or {}
            cname = (c_ref.get("campaign_name") or "").strip()
            t_key = extract_taager_product_key_tt(cname)
            if not t_key:
                continue
            if t_key not in buckets:
                buckets[t_key] = {
                    "key": t_key,
                    "label": f"منتج تاجر ({t_key})",
                    "keywords": (t_key.replace("taager_", ""),),
                    "tt_sp_1d": 0.0, "tt_cv_1d": 0,
                    "tt_sp_3d": 0.0, "tt_cv_3d": 0,
                    "tt_sp_7d": 0.0, "tt_cv_7d": 0,
                    "gads_sp_1d": 0.0, "gads_cv_1d": 0.0,
                    "gads_sp_3d": 0.0, "gads_cv_3d": 0.0,
                    "gads_sp_7d": 0.0, "gads_cv_7d": 0.0,
                    "tt_campaigns": [],
                    "gads_campaigns": [],
                }
            rd1 = (c1_map.get(cid) or {}).get("row_data") or {}
            rd3 = (c3_map.get(cid) or {}).get("row_data") or {}
            rd7 = (c7_map.get(cid) or {}).get("row_data") or {}
            sp1 = float(rd1.get("stat_cost") or 0.0)
            cv1 = int(float(rd1.get("time_attr_convert_cnt") or 0))
            sp3 = float(rd3.get("stat_cost") or 0.0)
            cv3 = int(float(rd3.get("time_attr_convert_cnt") or 0))
            sp7 = float(rd7.get("stat_cost") or sp3)
            cv7 = int(float(rd7.get("time_attr_convert_cnt") or cv3))

            b = buckets[t_key]
            b["tt_sp_1d"] = round(b["tt_sp_1d"] + sp1, 2)
            b["tt_cv_1d"] += cv1
            b["tt_sp_3d"] = round(b["tt_sp_3d"] + sp3, 2)
            b["tt_cv_3d"] += cv3
            b["tt_sp_7d"] = round(b["tt_sp_7d"] + sp7, 2)
            b["tt_cv_7d"] += cv7
            b["tt_campaigns"].append({
                "acc_name": acc_name,
                "aadvid": aadvid,
                "campaign_id": cid,
                "campaign_name": cname,
                "row": c_ref,
                "sp1": sp1, "cv1": cv1,
                "sp3": sp3, "cv3": cv3,
                "sp7": sp7, "cv7": cv7,
                "is_active": c_ref.get("primary_status") not in ("disable", "delete") and str(c_ref.get("opt_status")) == "0",
            })

    # 4. Add Google Ads spend & conversions per Taager product
    for g_key, g_info in gads_cached_camps.items():
        cname_g = (g_info.get("campaign_name") or "").strip()
        p_key_g = g_info.get("product_key") or extract_taager_product_key_tt(cname_g)
        if not p_key_g:
            continue
        if p_key_g not in buckets:
            buckets[p_key_g] = {
                "key": p_key_g,
                "label": f"منتج تاجر ({p_key_g})",
                "keywords": (p_key_g,),
                "tt_sp_1d": 0.0, "tt_cv_1d": 0,
                "tt_sp_3d": 0.0, "tt_cv_3d": 0,
                "tt_sp_7d": 0.0, "tt_cv_7d": 0,
                "gads_sp_1d": 0.0, "gads_cv_1d": 0.0,
                "gads_sp_3d": 0.0, "gads_cv_3d": 0.0,
                "gads_sp_7d": 0.0, "gads_cv_7d": 0.0,
                "tt_campaigns": [],
                "gads_campaigns": [],
            }
        b = buckets[p_key_g]
        b["gads_sp_1d"] = round(b["gads_sp_1d"] + float(g_info.get("sp1") or 0.0), 2)
        b["gads_cv_1d"] = round(b["gads_cv_1d"] + float(g_info.get("eff1") or 0.0), 2)
        b["gads_sp_3d"] = round(b["gads_sp_3d"] + float(g_info.get("sp3") or 0.0), 2)
        b["gads_cv_3d"] = round(b["gads_cv_3d"] + float(g_info.get("eff3") or 0.0), 2)
        b["gads_sp_7d"] = round(b["gads_sp_7d"] + float(g_info.get("sp7") or 0.0), 2)
        b["gads_cv_7d"] = round(b["gads_cv_7d"] + float(g_info.get("eff7") or 0.0), 2)
        b["gads_campaigns"].append(g_info)

    # 5. Evaluate each Taager product separately (كل منتج على حدة)
    for p_key, b in buckets.items():
        if not b["tt_campaigns"] and not b["gads_campaigns"]:
            continue
        comb_sp_1d = round(b["tt_sp_1d"] + b["gads_sp_1d"], 2)
        comb_cv_1d = round(b["tt_cv_1d"] + b["gads_cv_1d"], 2)
        comb_sp_3d = round(b["tt_sp_3d"] + b["gads_sp_3d"], 2)
        comb_cv_3d = round(b["tt_cv_3d"] + b["gads_cv_3d"], 2)
        comb_sp_7d = round(b["tt_sp_7d"] + b["gads_sp_7d"], 2)
        comb_cv_7d = round(b["tt_cv_7d"] + b["gads_cv_7d"], 2)

        tot_spend = max(comb_sp_7d, comb_sp_3d, comb_sp_1d)
        tot_sales = max(comb_cv_7d, comb_cv_3d, comb_cv_1d)
        tot_cpa = round(tot_spend / tot_sales, 2) if tot_sales > 0 else None
        cpa_1d = round(comb_sp_1d / comb_cv_1d, 2) if comb_cv_1d > 0 else None

        print(
            f"\n📦 [تيست منتج تاجر: {b['label']}] ({p_key})\n"
            f"   - تيك توك (TikTok): اليوم=${b['tt_sp_1d']:.2f} ({b['tt_cv_1d']} مبيعة) | التراكمي 7D=${b['tt_sp_7d']:.2f} ({b['tt_cv_7d']} مبيعة) | الحملات={len(b['tt_campaigns'])}\n"
            f"   - غوغل أدز (Google Ads): اليوم=${b['gads_sp_1d']:.2f} ({b['gads_cv_1d']:g} مبيعة) | التراكمي 7D=${b['gads_sp_7d']:.2f} ({b['gads_cv_7d']:g} مبيعة) | الحملات={len(b['gads_campaigns'])}\n"
            f"   - الإجمالي المشترك للمنتج (TikTok + Google Ads): الصرف=${tot_spend:.2f} | المبيعات={tot_sales:g} | CPA={('$'+f'{tot_cpa:.2f}') if tot_cpa is not None else '—'} "
            f"(سقف التيست: > ${TAAGER_ZERO_SALE_SPEND_LIMIT:.2f} بدون بيعة أو CPA >= ${TAAGER_MAX_CPA_LIMIT:.2f})"
        )

        fail_reason = None
        if tot_spend > TAAGER_ZERO_SALE_SPEND_LIMIT and tot_sales == 0:
            fail_reason = (
                f"[Taager Product Test Failed: > $25 Zero-Sale Stop] مجموع صرف المنتج '{b['label']}' في الحسابات (غوغل + تيك توك) = "
                f"${tot_spend:.2f} (> ${TAAGER_ZERO_SALE_SPEND_LIMIT:.2f}) بدون ولا بيعة (0 مبيعات) -> المنتج فاشل"
            )
        elif (tot_sales >= 1 and tot_cpa is not None and tot_cpa >= TAAGER_MAX_CPA_LIMIT) or (comb_cv_1d >= 1 and cpa_1d is not None and cpa_1d >= TAAGER_MAX_CPA_LIMIT):
            bad_cpa = tot_cpa if (tot_cpa is not None and tot_cpa >= TAAGER_MAX_CPA_LIMIT) else cpa_1d
            fail_reason = (
                f"[Taager Product Test Failed: Combined CPA >= $27 Stop] سعر البيعة المشترك للمنتج '{b['label']}' في الحسابات (غوغل + تيك توك) وصل إلى "
                f"CPA = ${bad_cpa:.2f} (>= ${TAAGER_MAX_CPA_LIMIT:.2f} | الصرف المشترك=${tot_spend:.2f} | المبيعات={tot_sales:g})"
            )

        if fail_reason:
            print(f"   🚨 {fail_reason} -> إيقاف جميع حملات المنتج في غوغل أدز وتيك توك فوراً!")
            history.setdefault("taager_failed_products", {})[p_key] = {
                "product_key": p_key,
                "label": b["label"],
                "combined_spend": tot_spend,
                "combined_sales": tot_sales,
                "combined_cpa": tot_cpa,
                "reason": fail_reason,
                "failed_at": now_str,
            }
            save_paused_history(history)
            for tc in b["tt_campaigns"]:
                pause_tt_campaign_wrapper(tt_session, tt_headers, tc["aadvid"], tc["row"], fail_reason, history, now_str, dry_run=dry_run)
            gads_names = [g.get("campaign_name") for g in b["gads_campaigns"] if g.get("campaign_name")]
            if p_key == "washer_hp909" and "washer hp909 taager 01-11" not in gads_names:
                gads_names.append("washer hp909 taager 01-11")
            elif p_key == "bakhoor_bw20" and "bakhoor bw20 taager 01-11" not in gads_names:
                gads_names.append("bakhoor bw20 taager 01-11")
            _pause_gads_campaigns_for_taager_product(p_key, b["keywords"], gads_names, fail_reason, dry_run=dry_run)
        else:
            rem_zero_sale = max(0.0, round(TAAGER_ZERO_SALE_SPEND_LIMIT - tot_spend, 2))
            print(f"   ✅ المنتج ضمن حدود التيست المسموحة (متبقي ${rem_zero_sale:.2f} قبل سقف الـ $25.00 بدون مبيعة).")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    run_cross_product_monitor(dry_run=dry)
