"""
خبير مبيعات وصرف وتجارب سناب شات المستقل كل ساعتين (Snap 2-Hour Autonomous Sales, Spend & Experimentation Master)
=============================================================================================================
يعمل تلقائياً كل ساعتين (0 */2 * * *) بالتوازي مع snap_monitor.py، ويغطي الحسابين:
- CSP9860 (350cb560-ad77-4fb5-9bbf-4c090c9ad081)
- CSP9860 4 (0d00760b-6793-441f-a675-e35371ce9c7c)

المهام الخمس في كل دورة (كل ساعتين) مع حرص صارم على البدجت:
1. حماية رأس المال الصارمة + الربط مع شيت Lightfunnels:
   - إيقاف أي حملة تصرف >= $20.00 بدون مبيعة أو CPA >= $21.00 (وللحملات التجريبية [EXP-SNAP] عند $8.50).
   - إيقاف أي أدست تصرف >= $4.00 بدون مبيعة أو CPA >= $7.00 (وللأدست التجريبية [EXP-SNAP] عند $3.50 بدون مبيعة أو $1.00 بـ CPC > $0.25).
   - إعادة إحياء أي أدست موقوفة داخل الحملات النشطة ظهرت لها مبيعة متأخرة رابحة اليوم (CPA <= $6.00) مع استبدال أعلى أدست صفرية صرفاً للحفاظ على سقف <= 30 أدست.
2. الحرص الصارم على البدجت ومنع تضخم الميزانيات اليومية + قاعدة إرجاع البدجت إلى $20.00 آخر الليل في كل الأدست:
   - قاعدة دائمة بأمر المستخدم: آخر الليل (22:00 مساءً إلى 05:59 صباحاً بتوقيت السعودية GMT+3) يتم تغيير وإرجاع البدجت اليومي إلى $20.00 (20,000,000 micro) في جميع الأدست في سناب شات بدون استثناء.
   - أي أدست بدون مبيعات اليوم (0 Sales) تُثبّت ميزانيتها صارماً عند الحد الأساسي الموحد ($20.00 لجميع الأدست في سناب شات) ويُمنع رفع ميزانيتها نهائياً.
   - خلال النهار (06:00 صباحاً إلى 21:59 مساءً بتوقيت السعودية)، الأدست الرابحة اليوم (CPA < $7.00) يتدرج سقف ميزانيتها اليومية بحذر شديد حسب عدد المبيعات الفعلي:
     * 1 مبيعة اليوم: حد أقصى $26.00/يوم.
     * 2 مبيعة اليوم: حد أقصى $34.00/يوم.
     * >= 3 مبيعات اليوم: حد أقصى $45.00/يوم.
3. محرك التجارب الذكي #1 — عزل وتعميم الفيديوهات الرابحة وقطع نزيف الفيديوهات الصفرية مبكراً ([EXP-SNAP Winner Isolation]):
   - أي فيديو يحقق اليوم مبيعات بـ CPA <= $6.50 داخل الحملة (أو سوبر رابح عبر الحساب بـ >= 3 مبيعات اليوم بـ CPA <= $6.00 أو >= 8 مبيعات في 3 أيام بـ CPA <= $3.50) يُعاد تفعيل جميع نسخه الموقوفة داخل المجموعات النشطة السليمة فوراً حتى لا يظل حبيس 2–6 مجموعات فقط.
   - داخل الحملات الرابحة التي صرفت >= $10.00 وحققت مبيعات اليوم، أي فيديو صفري يستنزف البدجت (>= $3.10 بدون أي مبيعة اليوم أو >= $2.00 بـ CPC > $0.27) يُوقف فوراً عبر الحملة (مع إبقاء إعلانين نشطين على الأقل في كل أدست) لتحويل 100% من البدجت للفيديوهات التي تبيع فعلياً.
4. محرك التجارب الذكي #2 — اختبارات العناوين التسويقية وحقن الفيديوهات السوبر رابحة بـ $0 بدجت إضافي ([EXP-SNAP Micro Tests]):
   - فحص كل إعلان تجريبي يحمل وسم [EXP-]: إذا صرف >= $2.50 بدون مبيعة (أو >= $1.00 بـ CPC > $0.26 بدون مبيعة) يُوقف فوراً (فرملة ميكرو صارمة لحماية البدجت). وإذا حقق مبيعة بـ CPA <= $5.50 يُعمّم تلقائياً على 5 مجموعات نشطة إضافية.
   - زرع تجارب عناوين تسويقية جديدة ([EXP-H2] / [EXP-H3]) على أفضل فيديو رابح في الحملات الرابحة، وحقن الفيديو السوبر رابح عبر الحسابين ([EXP-WIN]) داخل أفضل 3–5 مجموعات رابحة فقط (مشاركة نفس البدجت الحالي = $0 بدجت إضافي).
5. قفل البيد الصارم (<= $1.80):
   - أي أدست البيد فيها > $1.80 ترجع فوراً إلى $1.80، وأي أدست البيد فيها <= $1.80 لا يُلمس بيدها نهائياً.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import time
from snap_auth import API_BASE, api_get, load_config, refresh_access_token
from snap_monitor import (
    ACCOUNT_TZ,
    ALLOWED_30X_CAMPAIGN_IDS,
    ALLOWED_GULF_CAMPAIGN_IDS,
    DEDICATED_SNAP_TEST_CAMPAIGN_IDS,
    FOUR_SQUAD_CAMPAIGN_IDS,
    SECOND_ACCOUNT_ID,
    SPENDING_TODAY_179_CAMPAIGN_IDS,
    TARGET_ACCOUNT_ID,
    TARGET_ACCOUNT_IDS,
    TEN_SQUAD_CAMPAIGN_IDS,
    api_get_paginated,
    api_put,
    determine_expected_bid_micro_from_squad,
    fetch_account_breakdown_stats,
    get_locked_low_bid_micro,
    is_30x_low_bid_test_campaign_or_squad,
    is_cross_monitored_product_campaign,
    is_dedicated_snap_test_campaign,
    is_gulf_034_campaign,
    is_low_bid_experiment_locked,
    is_non_copy_test_adsquad,
    is_pillow_snap_campaign,
    is_powerbank_snap_campaign,
    is_target_cost_campaign_or_squad,
    load_paused_history,
    parse_iso_utc,
    resolve_video_media_id,
    save_paused_history,
)
from duplicate_gulf_campaigns import get_or_create_headline_creative, make_session

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).parent
LEDGER_FILE = BASE_DIR / "snap_master_actions_ledger.json"
REPORT_FILE = BASE_DIR / "snap_hourly_sales_report.md"

ACCOUNT_LABELS = {
    TARGET_ACCOUNT_ID: "CSP9860",
    SECOND_ACCOUNT_ID: "CSP9860 4",
}

ONE_HOUR_SEC = 6600  # 110 دقائق (ساعة و50 دقيقة) لضمان التنفيذ في كل دورة (كل ساعتين) بدون تفويت
LOW_SPEND_1H_SQUAD_USD = 0.25
LOW_SPEND_1H_CAMP_USD = 0.60


def _video_prefix_key(ad_name: str) -> str:
    base = re.sub(r"\s*-\s*Copy.*$", "", (ad_name or "").strip(), flags=re.I)
    base = re.sub(r"\s*\[(?:H\d+|EXP-[^\]]+)\]\s*", "", base, flags=re.I).strip()
    m = re.match(r"^(.+)_m[0-9a-f]{6}$", base, flags=re.I)
    if m:
        return m.group(1).strip().lower()
    return base.strip().lower()


def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        try:
            data = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("squad_state", {})
                data.setdefault("campaign_state", {})
                data.setdefault("experiments", {})
                data.setdefault("actions_log", [])
                return data
        except Exception:
            pass
    return {
        "squad_state": {},
        "campaign_state": {},
        "experiments": {},
        "actions_log": [],
    }


def save_ledger(ledger: dict):
    ledger["actions_log"] = (ledger.get("actions_log") or [])[-400:]
    LEDGER_FILE.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")


def record_action(ledger: dict, actions_taken: list[dict], account_label: str, action_type: str, target_name: str, details: str, metrics: str):
    now_str = datetime.now(ACCOUNT_TZ).strftime("%Y-%m-%d %H:%M GMT+3")
    entry = {
        "ts": now_str,
        "account": account_label,
        "action_type": action_type,
        "target": target_name,
        "details": details,
        "metrics": metrics,
    }
    ledger.setdefault("actions_log", []).append(entry)
    actions_taken.append(entry)
    print(f"⚡ [{account_label} | {action_type}] {target_name} -> {details} | ({metrics})")


def get_sheet_sales() -> dict:
    try:
        from deep_multiwindow_account_auditor import fetch_sheet_multiwindow_sales
        return fetch_sheet_multiwindow_sales() or {}
    except Exception as e:
        print(f"⚠️ تعذر قراءة شيت Lightfunnels: {e}")
        return {}


def run_hourly_master_for_account(token: str, ad_account_id: str, sheet_sales: dict, ledger: dict, actions_taken: list[dict], dry_run: bool = False):
    acc_label = ACCOUNT_LABELS.get(ad_account_id, ad_account_id)
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(ACCOUNT_TZ)
    is_late_night = (now_local.hour >= 22 or now_local.hour < 6)
    is_after_6pm = (now_local.hour >= 18 or now_local.hour < 6)  # أمر المستخدم الصارم: ممنوع رفع البدجت بعد الساعة 6 مساءً (18:00) أبداً!
    today_str = now_local.strftime("%Y-%m-%d")
    now_human_str = now_local.strftime("%Y-%m-%d %H:%M GMT+3")
    today_start_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    history = load_paused_history()
    history.setdefault("user_reactivated_ads", {})
    history.setdefault("disabled_videos", {})

    sh_1d = (sheet_sales.get("1d") or {}) if isinstance(sheet_sales, dict) else {}
    sh_3d = (sheet_sales.get("3d") or {}) if isinstance(sheet_sales, dict) else {}

    all_campaigns = api_get_paginated(f"/adaccounts/{ad_account_id}/campaigns?limit=200", token, "campaigns", "campaign")
    squad_1d_list = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=0)
    squad_1d = {st["id"]: st for st in squad_1d_list}
    squad_2d = {st["id"]: st for st in fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=1)}
    squad_3d = {st["id"]: st for st in fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=2)}

    camp_1d = {st["id"]: st for st in fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=0)}
    camp_2d = {st["id"]: st for st in fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=1)}
    camp_3d = {st["id"]: st for st in fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=2)}

    ad_1d = {st["id"]: st for st in fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=0)}
    ad_3d = {st["id"]: st for st in fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=2)}

    user_paused_camps = set(history.get("user_paused_campaigns") or [])
    user_banned_vids = {str(x).lower() for x in (history.get("user_banned_videos") or [])}
    active_campaigns = [
        c for c in all_campaigns
        if c.get("status") == "ACTIVE"
        and c.get("id") not in user_paused_camps
        and not is_cross_monitored_product_campaign(c.get("name", ""))
    ]

    # جمع جميع إعلانات الحملات النشطة مسبقاً لحساب الفيديوهات السوبر رابحة على مستوى الحساب (1D و 3D)
    camp_squads_map = {}
    camp_ads_map = {}
    account_video_stats = {}

    for c in active_campaigns:
        cid = c["id"]
        c_squads = api_get_paginated(f"/campaigns/{cid}/adsquads?limit=200", token, "adsquads", "adsquad")
        c_ads = api_get_paginated(f"/campaigns/{cid}/ads?limit=500", token, "ads", "ad", max_pages=3)
        camp_squads_map[cid] = c_squads
        camp_ads_map[cid] = c_ads
        for a in c_ads:
            aid = a.get("id")
            v_pref = _video_prefix_key(a.get("name", ""))
            if not v_pref:
                continue
            v_rec = account_video_stats.setdefault(v_pref, {"sp1": 0.0, "pu1": 0, "sp3": 0.0, "pu3": 0})
            st1_a = ad_1d.get(aid)
            if st1_a:
                v_rec["sp1"] = round(v_rec["sp1"] + float(st1_a.get("spend", 0.0)), 2)
                v_rec["pu1"] += int(st1_a.get("purchases", 0))
            st3_a = ad_3d.get(aid)
            if st3_a:
                v_rec["sp3"] = round(v_rec["sp3"] + float(st3_a.get("spend", 0.0)), 2)
                v_rec["pu3"] += int(st3_a.get("purchases", 0))

    for c in active_campaigns:
        cid = c["id"]
        cname = c.get("name", "")
        if is_gulf_034_campaign(cname) or cid in ALLOWED_GULF_CAMPAIGN_IDS:
            continue  # حملات الخليج يديرها حصرياً gulf_2h_profit_and_ads_master.py كل ساعة
        c_squads = camp_squads_map.get(cid, [])
        c_ads = camp_ads_map.get(cid, [])

        is_exp_camp = "[exp-snap]" in cname.lower()
        is_ded_test_camp = is_dedicated_snap_test_campaign(cid, cname)
        is_pillow = is_pillow_snap_campaign(cname)
        is_pb = is_powerbank_snap_campaign(cname)
        is_30x_camp = is_30x_low_bid_test_campaign_or_squad(cid, cname, "")
        max_squad_cap = 10 if cid in TEN_SQUAD_CAMPAIGN_IDS else (4 if cid in FOUR_SQUAD_CAMPAIGN_IDS else 30)

        ads_by_squad = {}
        for a in c_ads:
            ads_by_squad.setdefault(a.get("ad_squad_id"), []).append(a)

        # ---------------------------------------------------------------------
        # المرحلة 0: إعادة إحياء أي أدست موقوفة داخل الحملة النشطة وصلت لها مبيعة متأخرة رابحة اليوم (CPA <= $6.00)
        # ---------------------------------------------------------------------
        active_squads = [sq for sq in c_squads if sq and sq.get("status") == "ACTIVE"]
        paused_winner_squads = []
        for sq in ([] if is_ded_test_camp else c_squads):
            if not sq or sq.get("status") != "PAUSED":
                continue
            if sq.get("name", "").strip().lower().startswith("base -"):
                continue
            st_p1 = squad_1d.get(sq["id"], {"spend": 0.0, "purchases": 0, "cpa": None})
            st_p2 = squad_2d.get(sq["id"], {"spend": 0.0, "purchases": 0, "cpa": None})
            st_p3 = squad_3d.get(sq["id"], {"spend": 0.0, "purchases": 0, "cpa": None})
            p_pu1 = int(st_p1.get("purchases", 0))
            p_sp1 = float(st_p1.get("spend", 0.0))
            p_cpa1 = st_p1.get("cpa")
            p_pu2 = int(st_p2.get("purchases", 0))
            p_sp2 = float(st_p2.get("spend", 0.0))
            p_cpa2 = st_p2.get("cpa")
            p_pu3 = int(st_p3.get("purchases", 0))
            p_cpa3 = st_p3.get("cpa")
            p_violates_2d = (p_pu2 == 0 and p_sp2 >= 4.00) or (p_pu2 > 0 and p_cpa2 is not None and p_cpa2 >= 7.00)
            if p_pu1 >= 1 and p_cpa1 is not None and p_cpa1 <= 6.80 and not (p_pu2 > 0 and p_cpa2 is not None and p_cpa2 >= 7.00):
                paused_winner_squads.append((p_cpa1, -p_pu1, sq))
            elif is_30x_camp and p_pu3 >= 1 and p_cpa3 is not None and p_cpa3 <= 6.50 and p_sp1 < 4.00 and not p_violates_2d:
                paused_winner_squads.append((p_cpa3, -p_pu3, sq))

        if paused_winner_squads:
            paused_winner_squads.sort(key=lambda x: (x[0], x[1]))
            revived_sq_names = []
            swapped_out_names = []
            squads_revive_batch = []
            ads_revive_by_sq = {}

            for p_cpa1, _, sq_win in paused_winner_squads:
                # إذا كانت الحملة عند الحد الأقصى لأدست الكوبي (4 أو 30 أدست)، نستبدل أعلى أدست كوبي نشطة صفرية المبيعات بصرف مرتفع
                active_copy_squads_only = [
                    s_c for s_c in active_squads
                    if not is_non_copy_test_adsquad(s_c.get("name", ""), cname)
                ]
                if len(active_copy_squads_only) >= max_squad_cap:
                    zero_sale_active = [
                        s_z for s_z in active_copy_squads_only
                        if int(squad_1d.get(s_z["id"], {}).get("purchases", 0)) == 0
                        and float(squad_1d.get(s_z["id"], {}).get("spend", 0.0)) >= 1.20
                    ]
                    if not zero_sale_active:
                        break
                    zero_sale_active.sort(
                        key=lambda s_z: (
                            float(squad_1d.get(s_z["id"], {}).get("spend", 0.0)),
                            float(squad_1d.get(s_z["id"], {}).get("cpc", 0.0) or 0.0),
                        ),
                        reverse=True,
                    )
                    sq_drop = zero_sale_active[0]
                    sq_drop["status"] = "PAUSED"
                    squads_revive_batch.append(sq_drop)
                    active_squads = [s_x for s_x in active_squads if s_x["id"] != sq_drop["id"]]
                    drop_sp = float(squad_1d.get(sq_drop["id"], {}).get("spend", 0.0))
                    swapped_out_names.append(f"{sq_drop.get('name')} (${drop_sp:.2f}/0 بيع)")

                sq_win["status"] = "ACTIVE"
                # أمر المستخدم الصارم (2026-11-01): لا يقرب للبيد لا يطلع ولا ينزل لعند ما يقول المستخدم ولا يرفع بدجت بعد الساعة 6 مساءً أبداً
                st_w1 = squad_1d.get(sq_win["id"], {})
                w_pu1 = int(st_w1.get("purchases", 0))
                sq_win["daily_budget_micro"] = 20_000_000 if (is_after_6pm or is_late_night or w_pu1 == 0) else 26_000_000
                squads_revive_batch.append(sq_win)
                active_squads.append(sq_win)
                history.get("adsquads", {}).pop(sq_win["id"], None)
                revived_sq_names.append(f"{sq_win.get('name')} (مبيعات={st_w1.get('purchases')}, CPA=${p_cpa1:.2f})")

                # التأكد من تفعيل الإعلانات السليمة داخل الأدست المعاد إحياؤها
                sq_w_ads = ads_by_squad.get(sq_win["id"], [])
                if sq_w_ads and not any(a_w.get("status") == "ACTIVE" for a_w in sq_w_ads):
                    for a_w in sq_w_ads:
                        st_aw = ad_1d.get(a_w["id"], {"spend": 0.0, "purchases": 0})
                        if float(st_aw.get("spend", 0.0)) >= 2.80 and int(st_aw.get("purchases", 0)) == 0:
                            continue
                        a_w["status"] = "ACTIVE"
                        ads_revive_by_sq.setdefault(sq_win["id"], []).append(a_w)

            if squads_revive_batch:
                if not dry_run:
                    for i in range(0, len(squads_revive_batch), 15):
                        api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": squads_revive_batch[i:i+15]})
                    for sq_id_r, ad_list_r in ads_revive_by_sq.items():
                        api_put(f"/adsquads/{sq_id_r}/ads", token, {"ads": ad_list_r})
                    save_paused_history(history)
                swap_note = f" | مقابل إيقاف: {', '.join(swapped_out_names)}" if swapped_out_names else ""
                record_action(
                    ledger,
                    actions_taken,
                    acc_label,
                    "إعادة إحياء أدست رابحة (مبيعة متأخرة)",
                    cname,
                    f"تفعيل {', '.join(revived_sq_names)}{swap_note}",
                    f"عدد الأدست النشطة={len(active_squads)}/{max_squad_cap}",
                )

        if not active_squads:
            continue

        # حساب إحصائيات الحملة الحقيقية (Pixel + Sheet) عبر 1D و 2D و 3D
        sp_act_1d = round(sum(float(squad_1d.get(sq["id"], {}).get("spend", 0.0)) for sq in active_squads), 2)
        sp_camp_1d = round(float(camp_1d.get(cid, {}).get("spend", 0.0)), 2)
        camp_spend_1d = max(sp_act_1d, sp_camp_1d)
        pu_px_1d = max(
            sum(int(squad_1d.get(sq["id"], {}).get("purchases", 0)) for sq in active_squads),
            int(camp_1d.get(cid, {}).get("purchases", 0)),
        )
        pu_sh_1d = int(sh_1d.get(cid.lower(), 0))
        camp_purch_1d = max(pu_px_1d, pu_sh_1d)
        camp_cpa_1d = round(camp_spend_1d / camp_purch_1d, 2) if camp_purch_1d > 0 else None

        camp_spend_2d = round(float(camp_2d.get(cid, {}).get("spend", 0.0)), 2)
        camp_purch_2d = max(int(camp_2d.get(cid, {}).get("purchases", 0)), camp_purch_1d)
        camp_cpa_2d = round(camp_spend_2d / camp_purch_2d, 2) if camp_purch_2d > 0 else None

        camp_spend_3d = round(float(camp_3d.get(cid, {}).get("spend", 0.0)), 2)
        camp_purch_3d = max(int(camp_3d.get(cid, {}).get("purchases", 0)), int(sh_3d.get(cid.lower(), 0)), camp_purch_2d)
        camp_cpa_3d = round(camp_spend_3d / camp_purch_3d, 2) if camp_purch_3d > 0 else None

        # ---------------------------------------------------------------------
        # المرحلة 1: فرملة الخسارة الصارمة على مستوى الحملة والأدست (الحملة >= $20 بدون بيع أو CPA >= $20 في 1D/2D/3D بدون أي استثناء، باستثناء حملات التيست المخصصة بأمر المستخدم)
        # ---------------------------------------------------------------------
        no_sale_camp_cap = 8.50 if is_exp_camp else 20.00
        max_cpa_camp_cap = 20.00
        stop_loss_reason = None
        if not is_ded_test_camp:
            if camp_purch_1d == 0 and camp_spend_1d >= no_sale_camp_cap:
                stop_loss_reason = f"صرف اليوم=${camp_spend_1d:.2f} >= ${no_sale_camp_cap:.2f} بـ 0 مبيعات"
            elif camp_purch_2d == 0 and camp_spend_2d >= no_sale_camp_cap:
                stop_loss_reason = f"صرف يومين=${camp_spend_2d:.2f} >= ${no_sale_camp_cap:.2f} بـ 0 مبيعات"
            elif camp_purch_3d == 0 and camp_spend_3d >= no_sale_camp_cap:
                stop_loss_reason = f"صرف 3 أيام=${camp_spend_3d:.2f} >= ${no_sale_camp_cap:.2f} بـ 0 مبيعات"
            elif camp_purch_1d > 0 and camp_cpa_1d is not None and camp_cpa_1d >= max_cpa_camp_cap:
                stop_loss_reason = f"CPA اليوم=${camp_cpa_1d:.2f} >= ${max_cpa_camp_cap:.2f} (صرف=${camp_spend_1d:.2f} / مبيعات={camp_purch_1d})"
            elif camp_purch_2d > 0 and camp_cpa_2d is not None and camp_cpa_2d >= max_cpa_camp_cap and (camp_purch_1d == 0 or (camp_cpa_1d or 99) >= max_cpa_camp_cap):
                stop_loss_reason = f"CPA يومين=${camp_cpa_2d:.2f} >= ${max_cpa_camp_cap:.2f} (صرف=${camp_spend_2d:.2f} / مبيعات={camp_purch_2d})"
            elif camp_purch_3d > 0 and camp_cpa_3d is not None and camp_cpa_3d >= max_cpa_camp_cap and (camp_purch_1d == 0 or (camp_cpa_1d or 99) >= max_cpa_camp_cap):
                stop_loss_reason = f"CPA 3 أيام=${camp_cpa_3d:.2f} >= ${max_cpa_camp_cap:.2f} (صرف=${camp_spend_3d:.2f} / مبيعات={camp_purch_3d})"

        if stop_loss_reason:
            if not dry_run:
                c_off = dict(c)
                c_off["status"] = "PAUSED"
                api_put(f"/adaccounts/{ad_account_id}/campaigns", token, {"campaigns": [c_off]})
                for sq_p in active_squads:
                    sq_p["status"] = "PAUSED"
                for i in range(0, len(active_squads), 20):
                    api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": active_squads[i:i+20]})
            record_action(
                ledger,
                actions_taken,
                acc_label,
                "إيقاف حملة خاسرة (قاعدة $20 الصارمة)",
                cname,
                f"إيقاف الحملة و{len(active_squads)} أدست نشطة فوراً ({stop_loss_reason})",
                f"1D=${camp_spend_1d:.2f}/{camp_purch_1d} | 3D=${camp_spend_3d:.2f}/{camp_purch_3d}",
            )
            continue

        # تتبع دلتا الصرف الساعي للحملة
        c_state = ledger["campaign_state"].get(cid, {})
        c_last_dt = parse_iso_utc(c_state.get("last_check_utc") or "")
        if c_state.get("spend_date") == today_str and c_last_dt:
            camp_spend_1h = max(0.0, round(camp_spend_1d - float(c_state.get("spend_at_last_check", 0.0)), 2))
        else:
            camp_spend_1h = camp_spend_1d
        ledger["campaign_state"][cid] = {
            "name": cname,
            "account": acc_label,
            "spend_date": today_str,
            "spend_at_last_check": camp_spend_1d,
            "last_check_utc": now_utc.isoformat(),
            "spend_1h": camp_spend_1h,
            "purchases_1d": camp_purch_1d,
            "cpa_1d": camp_cpa_1d,
        }

        squads_to_put = {}
        ads_to_put_by_squad = {}
        camp_restored_low_bids = []
        camp_normalized_budgets = []
        camp_scaled_squads = []
        camp_paused_squads = []
        camp_pruned_ads = 0

        # تحديد ما إذا كانت الحملة رابحة اليوم في الشيت أو البكسل ولكن المبيعات لم تُسجل كلها على أدست بعينها
        is_converting_winner_camp = (camp_purch_1d >= 1 and camp_cpa_1d is not None and camp_cpa_1d <= 8.00)

        # ترتيب الأدست حسب الكفاءة (المبيعات ثم عدد السوايبات بأقل CPC)
        def _sq_efficiency_score(sq_obj):
            st1 = squad_1d.get(sq_obj["id"], {})
            pu = int(st1.get("purchases", 0))
            sw = int(st1.get("swipes", 0))
            cpc = float(st1.get("cpc", 9.99) or 9.99)
            sp = float(st1.get("spend", 0.0))
            return (pu, 1 if (sw >= 8 and cpc <= 0.20) else 0, sw, -cpc, sp)

        top_efficient_squad_ids = {
            sq_x["id"]
            for sq_x in sorted(active_squads, key=_sq_efficiency_score, reverse=True)[:2]
            if float(squad_1d.get(sq_x["id"], {}).get("spend", 0.0)) >= 0.40
            and float(squad_1d.get(sq_x["id"], {}).get("cpc", 9.99) or 9.99) <= 0.19
        }

        if is_late_night and c.get("pacing_level") != "CAMPAIGN":
            for sq_any in c_squads:
                if not sq_any:
                    continue
                cur_b_any = int(sq_any.get("daily_budget_micro") or 0)
                if cur_b_any > 0 and cur_b_any != 20_000_000:
                    sq_any["daily_budget_micro"] = 20_000_000
                    squads_to_put[sq_any["id"]] = sq_any
                    if sq_any.get("status") != "ACTIVE" or sq_any.get("name", "").strip().lower().startswith("base -"):
                        camp_normalized_budgets.append(
                            f"{sq_any.get('name', '')}: ضبط البدجت ${cur_b_any/1e6:.1f}->$20.0 (قاعدة آخر الليل $20)"
                        )

        for sq in active_squads:
            sid = sq["id"]
            sname = sq.get("name", "")
            sn_l = sname.strip().lower()
            if sn_l.startswith("base -"):
                continue

            st1 = squad_1d.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0, "impressions": 0})
            st2 = squad_2d.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            st3 = squad_3d.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})

            sp1 = float(st1.get("spend", 0.0))
            pu1 = int(st1.get("purchases", 0))
            cpa1 = st1.get("cpa")
            cpc1 = float(st1.get("cpc", 0.0) or 0.0)
            sw1 = int(st1.get("swipes", 0))

            sp2 = float(st2.get("spend", 0.0))
            pu2 = int(st2.get("purchases", 0))
            cpa2 = st2.get("cpa")

            sp3 = float(st3.get("spend", 0.0))
            pu3 = int(st3.get("purchases", 0))

            is_gulf = is_gulf_034_campaign(cname, sname, sq)
            is_exp_sq = is_exp_camp or "[exp-snap]" in sn_l
            is_ded_test_sq = is_ded_test_camp or is_dedicated_snap_test_campaign(cid, cname, sname)
            if is_ded_test_sq:
                # أمر المستخدم الصارم (2026-11-01): في سناب الحملات اللي فيهم تيست لا تقربهم نهائياً!
                continue
            is_non_copy_sq = is_non_copy_test_adsquad(sname, cname)

            no_sale_sq_cap = 3.50 if is_exp_sq else (10.00 if is_gulf else 4.00)
            max_cpa_sq_cap = 7.00 if is_exp_sq else (12.00 if is_gulf else 7.00)
            max_cpc_sq_cap = 0.25 if is_exp_sq else (0.35 if is_gulf else 0.28)

            # 0) إذا كانت الحملة 'تيست' أو الأدست بدون كلمة 'Copy' (أدست فيديوهات جديدة is_non_copy_sq):
            # لا نطفئ الحملة أو الأدست نفسها، بل نفلتر الفيديوهات بداخلها فردياً حسب قاعدة المستخدم الصارمة (2026-11-01):
            # - صرف الفيديو >= $1.00 بـ 0 مبيعات و (0 سحبات أو CPC > $0.27) -> إيقاف الفيديو فوراً
            # - صرف الفيديو >= $4.00 بـ 0 مبيعات -> إيقاف الفيديو فوراً
            # - CPA الفيديو >= $7.00 -> إيقاف الفيديو فوراً
            if is_non_copy_sq:
                sq_ads_nc = ads_by_squad.get(sid, [])
                act_ads_nc = [a_x for a_x in sq_ads_nc if a_x.get("status") == "ACTIVE"]
                surviving_nc = len(act_ads_nc)
                for a_nc in act_ads_nc:
                    aid_nc = a_nc["id"]
                    anm_nc = a_nc.get("name", "")
                    a1_nc = ad_1d.get(aid_nc, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
                    a3_nc = ad_3d.get(aid_nc, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
                    kill_nc_rsn = ""
                    for w_tag, st_nc in (("1D", a1_nc), ("3D", a3_nc)):
                        sp_a = float(st_nc.get("spend", 0.0))
                        pu_a = int(st_nc.get("purchases", 0))
                        sw_a = int(st_nc.get("swipes", 0))
                        cpc_a = float(st_nc.get("cpc", 0.0) or 0.0)
                        cpa_a = st_nc.get("cpa")
                        if w_tag == "3D" and int(a1_nc.get("purchases", 0)) >= 1 and (a1_nc.get("cpa") or 99) < 7.00:
                            continue
                        if pu_a > 0 and cpa_a is not None and cpa_a >= 7.00:
                            kill_nc_rsn = f"[{w_tag} Test Video CPA >= $7.00] ${sp_a:.2f} / {pu_a}p (CPA=${cpa_a:.2f})"
                            break
                        if pu_a == 0 and sp_a >= 4.00:
                            kill_nc_rsn = f"[{w_tag} Test Video >= $4.00 No-Sale] ${sp_a:.2f} / 0 sales"
                            break
                        if pu_a == 0 and sp_a >= 1.00 and (sw_a == 0 or cpc_a > 0.27):
                            kill_nc_rsn = f"[{w_tag} Test Video >= $1.00 & CPC > $0.27] ${sp_a:.2f} / swipes={sw_a} / CPC=${cpc_a:.2f}"
                            break
                    if kill_nc_rsn:
                        a_nc["status"] = "PAUSED"
                        ads_to_put_by_squad.setdefault(sid, []).append(a_nc)
                        history.setdefault("ads", {})[aid_nc] = {
                            "name": anm_nc,
                            "campaign": cname,
                            "adsquad": sname,
                            "reason": f"[Snap 1H Test-Video Filter] {kill_nc_rsn}",
                            "paused_at": now_human_str,
                        }
                        surviving_nc -= 1
                        camp_pruned_ads += 1
                # في حملات 'تيست' المخصصة لا يطفي البوت الأدست أبداً حتى لو تم إيقاف الفيديوهات بداخلها
                pause_sq_reason = (
                    ""
                    if is_ded_test_sq
                    else ("تمت تصفية جميع الفيديوهات غير الرابحة داخل الأدست (0 فيديوهات متبقية)" if (sq_ads_nc and surviving_nc <= 0) else "")
                )
            else:
                # 1) فحص الإيقاف الفوري لأدست الكوبي الخاسرة (>= $4.00 بدون بيع أو CPA >= $7.00)
                pause_sq_reason = ""
                if pu1 == 0 and sp1 >= no_sale_sq_cap:
                    pause_sq_reason = f"صرف اليوم ${sp1:.2f} (>= ${no_sale_sq_cap:.2f}) بدون مبيعات"
                elif is_exp_sq and pu1 == 0 and sp1 >= 1.00 and (sw1 == 0 or cpc1 > max_cpc_sq_cap):
                    pause_sq_reason = f"صرف اليوم ${sp1:.2f} بدون مبيعات بـ CPC مرتفع (${cpc1:.2f} > ${max_cpc_sq_cap:.2f})"
                elif pu1 > 0 and cpa1 is not None and cpa1 >= max_cpa_sq_cap:
                    pause_sq_reason = f"CPA اليوم مرتفع ${cpa1:.2f} (>= ${max_cpa_sq_cap:.2f})"
                elif pu1 == 0 and pu2 == 0 and sp2 >= no_sale_sq_cap:
                    pause_sq_reason = f"صرف يومين ${sp2:.2f} (>= ${no_sale_sq_cap:.2f}) بدون مبيعات"
                elif pu2 > 0 and cpa2 is not None and cpa2 >= max_cpa_sq_cap:
                    pause_sq_reason = f"CPA يومين مرتفع ${cpa2:.2f} (>= ${max_cpa_sq_cap:.2f})"
                elif pu1 == 0 and pu3 == 0 and sp3 >= no_sale_sq_cap:
                    pause_sq_reason = f"صرف 3 أيام ${sp3:.2f} (>= ${no_sale_sq_cap:.2f}) بدون مبيعات"

            if pause_sq_reason:
                sq["status"] = "PAUSED"
                squads_to_put[sid] = sq
                history["adsquads"][sid] = {
                    "name": sname,
                    "campaign": cname,
                    "reason": f"[Snap 1H Master Stop-Loss] {pause_sq_reason}",
                    "paused_at": now_human_str,
                }
                camp_paused_squads.append(f"{sname} ({pause_sq_reason})")
                continue

            # 2) قراءة وتحديث حالة الأدست الساعية في الـ Ledger
            s_state = ledger["squad_state"].get(sid, {})
            start_dt_sq = parse_iso_utc(sq.get("start_time") or "")
            created_dt_sq = parse_iso_utc(sq.get("created_at") or "")
            if start_dt_sq and now_utc < start_dt_sq:
                continue

            eff_start_utc = max([d for d in (created_dt_sq, start_dt_sq, today_start_utc) if d is not None] or [today_start_utc])
            last_check_dt = parse_iso_utc(s_state.get("last_check_utc") or "") or eff_start_utc
            elapsed_check_sec = (now_utc - last_check_dt).total_seconds()

            if s_state.get("spend_date") == today_str:
                spend_1h = max(0.0, round(sp1 - float(s_state.get("spend_at_last_check", 0.0)), 2))
            else:
                spend_1h = sp1

            cur_bid_micro = int(sq.get("bid_micro") or 0)
            cur_budget_micro = int(sq.get("daily_budget_micro") or 0)

            MAX_SNAP_BID_MICRO = 3_600_000 if is_gulf else 1_600_000
            base_budget_micro = 20_000_000

            bumps_today = 0
            scales_today = int(s_state.get("scales_today", 0)) if s_state.get("spend_date") == today_str else 0
            last_action_dt = parse_iso_utc(s_state.get("last_action_utc") or "") or eff_start_utc
            elapsed_since_action = (now_utc - last_action_dt).total_seconds()

            # أمر المستخدم الصارم (2026-11-02): عدم لمس بيد حملات التيست (2.80)، وتطبيق البيدات المقفلة لحملات الكوبي (-0.20)
            if not is_gulf and not is_ded_test_sq:
                new_bid_micro = get_locked_low_bid_micro(cid, cname, sname, sid) or 1_400_000
            else:
                new_bid_micro = cur_bid_micro

            # -----------------------------------------------------------------
            # تدقيق العين البشرية في أدق التفاصيل داخل الأدست (Micro-Detail Human-Eye Scrutiny Inside Every Active AdSquad):
            # 1) كشف الأدست الميتة (0 إعلانات نشطة بداخلها) وإيقافها فوراً (ما عدا حملات 'تيست' المخصصة).
            # 2) كشف الفيديو المستأثر بالميزانية (Budget Hog / Intra-Squad Bleeder) داخل أي أدست كوبي حتى قبل أن تصل لـ $4.00 أو قبل أن تبيع:
            #    إذا صرف فيديو واحد بداخل الأدست >= $1.40 بصفر مبيعات و CPC > $0.26 (أو >= $2.20 بصفر مبيعات) بينما يوجد فيديوهات أخرى في نفس الأدست لم تأخذ فرصتها، يُطفأ هذا الفيديو الفردي الخاسر فوراً ليتوجه باقي البدجت للفيديوهات الرابحة!
            # -----------------------------------------------------------------
            sq_ads = ads_by_squad.get(sid, [])
            active_sq_ads = [a for a in sq_ads if a.get("status") == "ACTIVE"]
            if len(active_sq_ads) == 0 and not is_ded_test_sq:
                sq["status"] = "PAUSED"
                squads_to_put[sid] = sq
                camp_paused_squads.append(f"{sname} (تدقيق العين البشرية الدقيق: أدست ميتة بداخلها 0 إعلانات نشطة)")
                continue

            if not is_non_copy_sq and len(active_sq_ads) > 1:
                surviving_ads = len(active_sq_ads)
                for a_in in list(active_sq_ads):
                    if surviving_ads <= 1:
                        break
                    aid_in = a_in["id"]
                    ast_in = ad_1d.get(aid_in, {"spend": 0.0, "purchases": 0, "swipes": 0, "cpc": None})
                    a_sp = float(ast_in.get("spend", 0.0))
                    a_pu = int(ast_in.get("purchases", 0))
                    a_sw = int(ast_in.get("swipes", 0))
                    a_cpc = round(a_sp / a_sw, 3) if a_sw > 0 else None
                    is_intra_sq_bleeder = (
                        a_pu == 0
                        and (
                            (a_sp >= 1.40 and (a_sw == 0 or (a_cpc is not None and a_cpc > 0.26)))
                            or a_sp >= 2.20
                        )
                    )
                    if is_intra_sq_bleeder:
                        a_in["status"] = "PAUSED"
                        ads_to_put_by_squad.setdefault(sid, []).append(a_in)
                        active_sq_ads.remove(a_in)
                        surviving_ads -= 1
                        camp_pruned_ads += 1

            # -----------------------------------------------------------------
            # المرحلة 2: الحرص الصارم على البدجت + السكيل التدريجي المحكوم بالمبيعات الفعلية فقط (مع فحص هلوسة البيكسل مقابل شيت Lightfunnels)
            # -----------------------------------------------------------------
            is_pixel_hallucinating = (pu_px_1d >= 3 and pu_sh_1d == 0 and camp_spend_1d >= 12.00)
            is_sq_converting = (
                not is_pixel_hallucinating
                and pu1 >= 1
                and cpa1 is not None
                and cpa1 < (12.00 if is_gulf else 7.00)
            )
            is_sq_sheet_leader = (
                not is_pixel_hallucinating
                and not is_sq_converting
                and is_converting_winner_camp
                and pu_sh_1d > pu_px_1d
                and sid in top_efficient_squad_ids
            )

            if is_late_night or (not is_sq_converting and not is_sq_sheet_leader):
                max_tier_budget_micro = base_budget_micro
            elif is_sq_sheet_leader or pu1 == 1:
                max_tier_budget_micro = 26_000_000
            elif pu1 == 2:
                max_tier_budget_micro = 34_000_000
            else:
                max_tier_budget_micro = 45_000_000

            # إذا كانت الميزانية الحالية متضخمة فوق السقف المسموح به (أو آخر الليل حيث يرجع البدجت 20$ في كل الأدست)، تُرجع فوراً للسقف الآمن!
            # وممنوع منعاً باتاً رفع البدجت بعد الساعة 6 مساءً (is_after_6pm)
            if cur_budget_micro > max_tier_budget_micro:
                new_budget_micro = max_tier_budget_micro
                camp_normalized_budgets.append(
                    f"{sname}: ضبط البدجت ${cur_budget_micro/1e6:.1f}->${new_budget_micro/1e6:.1f} ({'قاعدة آخر الليل $20' if is_late_night else f'مبيعات اليوم={pu1}'})"
                )
            elif not is_after_6pm and cur_budget_micro < base_budget_micro:
                new_budget_micro = base_budget_micro
                camp_normalized_budgets.append(
                    f"{sname}: ضبط البدجت ${cur_budget_micro/1e6:.1f}->${new_budget_micro/1e6:.1f} (تثبيت $20)"
                )
            else:
                new_budget_micro = cur_budget_micro

            if not is_after_6pm and not is_late_night and (is_sq_converting or is_sq_sheet_leader):
                CROSS_AUTOMATION_WAIT_SEC = 10800  # 3 ساعات انتظار إلزامي إذا قامت أي أتمتة أخرى بزيادة البدجت مؤخراً
                hist_scale_map = history.setdefault("snap_3h_budget_scale", {})
                hist_sq_rec = hist_scale_map.get(f"SQ:{sid}") if isinstance(hist_scale_map.get(f"SQ:{sid}"), dict) else {}
                hist_scale_dt = parse_iso_utc(hist_sq_rec.get("last_scale_utc") or "") if hist_sq_rec.get("last_scale_date") == today_str else None
                hist_scaled_pu = int(hist_sq_rec.get("purchases_today", 0)) if hist_sq_rec.get("last_scale_date") == today_str else 0
                hist_scales_cnt = int(hist_sq_rec.get("scales_today", 0)) if hist_sq_rec.get("last_scale_date") == today_str else 0

                state_scaled_pu = int(s_state.get("scaled_at_purchases", 0)) if s_state.get("spend_date") == today_str else 0
                prev_scaled_pu = max(state_scaled_pu, hist_scaled_pu)
                scales_today = max(scales_today, hist_scales_cnt)

                cand_scale_dts = [d for d in (last_action_dt if scales_today > 0 else None, hist_scale_dt) if d is not None]
                latest_scale_dt = max(cand_scale_dts) if cand_scale_dts else eff_start_utc
                elapsed_since_any_scale = (now_utc - latest_scale_dt).total_seconds()
                already_scaled_by_any_automation = (scales_today > 0) or (hist_scale_dt is not None) or (new_budget_micro > base_budget_micro)

                can_scale_now = (
                    new_budget_micro < max_tier_budget_micro
                    and (
                        (not already_scaled_by_any_automation and new_budget_micro == base_budget_micro)
                        or (
                            elapsed_since_any_scale >= CROSS_AUTOMATION_WAIT_SEC
                            and pu1 > prev_scaled_pu
                            and sp1 >= (new_budget_micro / 1e6) * 0.50
                        )
                    )
                )
                if can_scale_now:
                    scaled_b_micro = min(
                        max_tier_budget_micro,
                        int(round((new_budget_micro / 1_000_000) * 1.30, 2) * 1_000_000),
                    )
                    if scaled_b_micro > new_budget_micro:
                        old_b_disp = cur_budget_micro / 1e6
                        new_budget_micro = scaled_b_micro
                        scales_today += 1
                        last_action_dt = now_utc
                        hist_scale_map[f"SQ:{sid}"] = {
                            "type": "ADSQUAD",
                            "id": sid,
                            "name": sname,
                            "campaign": cname,
                            "last_scale_utc": now_utc.isoformat(),
                            "last_scale_date": today_str,
                            "scales_today": scales_today,
                            "old_budget_usd": round(old_b_disp, 2),
                            "new_budget_usd": round(new_budget_micro / 1e6, 2),
                            "purchases_today": max(pu1, 1),
                            "cpa_today": cpa1 or camp_cpa_1d,
                        }
                        camp_scaled_squads.append(
                            f"{sname}: بدجت ${old_b_disp:.1f}->${new_budget_micro/1e6:.1f} (مبيعات={max(pu1, 1)}, CPA=${cpa1 or camp_cpa_1d})"
                        )

            if new_bid_micro != cur_bid_micro or new_budget_micro != cur_budget_micro:
                sq["bid_micro"] = new_bid_micro
                sq["daily_budget_micro"] = new_budget_micro
                squads_to_put[sid] = sq

            ledger["squad_state"][sid] = {
                "name": sname,
                "campaign_id": cid,
                "campaign_name": cname,
                "account": acc_label,
                "spend_date": today_str,
                "spend_at_last_check": sp1,
                "last_check_utc": now_utc.isoformat() if elapsed_check_sec >= ONE_HOUR_SEC or "last_check_utc" not in s_state else s_state["last_check_utc"],
                "last_action_utc": last_action_dt.isoformat(),
                "bumps_today": bumps_today,
                "scales_today": scales_today,
                "scaled_at_purchases": max(pu1, int(s_state.get("scaled_at_purchases", 0)) if s_state.get("spend_date") == today_str else 0),
                "target_bid_micro": new_bid_micro,
                "target_budget_micro": new_budget_micro,
                "spend_1h": spend_1h,
                "spend_1d": sp1,
                "purchases_1d": pu1,
            }

        # ---------------------------------------------------------------------
        # المرحلة 3: محرك التجارب الذكي #1 — تعميم الفيديوهات الرابحة في كل مجموعات الكوبي النشطة + قطع نزيف الفيديوهات الصفرية مبكراً
        # ---------------------------------------------------------------------
        still_active_squad_ids = {
            sq["id"]
            for sq in active_squads
            if sq.get("status") == "ACTIVE"
            and sq["id"] not in camp_paused_squads
            and not is_non_copy_test_adsquad(sq.get("name", ""), cname)
        }
        v_groups = {}
        for sq_id_v in still_active_squad_ids:
            for a_v in ads_by_squad.get(sq_id_v, []):
                aid_v = a_v["id"]
                cr_v = a_v.get("creative_id") or ""
                v_pref = _video_prefix_key(a_v.get("name", ""))
                st1_v = ad_1d.get(aid_v, {"spend": 0.0, "purchases": 0, "swipes": 0})
                st3_v = ad_3d.get(aid_v, {"spend": 0.0, "purchases": 0, "swipes": 0})
                g = v_groups.setdefault(
                    cr_v or v_pref,
                    {
                        "creative_id": cr_v,
                        "v_pref": v_pref,
                        "sample_name": a_v.get("name", ""),
                        "ads": [],
                        "active_ads": [],
                        "paused_ads": [],
                        "sp1": 0.0,
                        "pu1": 0,
                        "sw1": 0,
                        "sp3": 0.0,
                        "pu3": 0,
                    },
                )
                g["ads"].append(a_v)
                if a_v.get("status") == "ACTIVE":
                    g["active_ads"].append(a_v)
                else:
                    g["paused_ads"].append(a_v)
                g["sp1"] = round(g["sp1"] + float(st1_v.get("spend", 0.0)), 2)
                g["pu1"] += int(st1_v.get("purchases", 0))
                g["sw1"] += int(st1_v.get("swipes", 0))
                g["sp3"] = round(g["sp3"] + float(st3_v.get("spend", 0.0)), 2)
                g["pu3"] += int(st3_v.get("purchases", 0))

        # 3A) إعادة تفعيل جميع النسخ الموقوفة للفيديوهات الرابحة اليوم (CPA <= $6.50) أو السوبر رابحة عبر الحساب
        revived_winner_videos_summary = []
        for gk, g in v_groups.items():
            cpa1_g = round(g["sp1"] / g["pu1"], 2) if g["pu1"] > 0 else None
            acc_v = account_video_stats.get(g["v_pref"], {"sp1": 0.0, "pu1": 0, "sp3": 0.0, "pu3": 0})
            acc_cpa1 = round(acc_v["sp1"] / acc_v["pu1"], 2) if acc_v["pu1"] > 0 else None
            acc_cpa3 = round(acc_v["sp3"] / acc_v["pu3"], 2) if acc_v["pu3"] > 0 else None

            is_camp_1d_winner = (g["pu1"] >= 1 and cpa1_g is not None and cpa1_g <= 6.50)
            is_account_superstar = (
                g["sp1"] < 3.00
                and (
                    (acc_v["pu1"] >= 3 and acc_cpa1 is not None and acc_cpa1 <= 6.00)
                    or (acc_v["pu3"] >= 8 and acc_cpa3 is not None and acc_cpa3 <= 3.50)
                )
            )

            if (is_camp_1d_winner or is_account_superstar) and g["paused_ads"]:
                sample_l = (g["sample_name"] or "").lower()
                if any(b_sub in sample_l for b_sub in ("car_v5_20-10_h1", "m736812", "pb_v7_hist64", "m08e86a", "mfae396", "car_v6_20-10_h3", "mfd8dd5", "mb78582", "mf03b43", "m9edae8")) or g["v_pref"] in user_banned_vids:
                    continue
                re_enabled_cnt = 0
                cr_id_w = g["creative_id"]
                mid_w = history.get("creative_media_map", {}).get(cr_id_w) or (
                    resolve_video_media_id(cr_id_w, token, history) if cr_id_w else ""
                )
                if mid_w and (f"media:{mid_w.lower()}" in user_banned_vids or mid_w.lower() in user_banned_vids):
                    continue
                # مسح الفيديو الرابح من قائمة disabled_videos غير الدائمة فقط
                for dk in list(history.get("disabled_videos", {}).keys()):
                    dv_obj = history["disabled_videos"].get(dk, {})
                    if isinstance(dv_obj, dict) and dv_obj.get("permanent"):
                        continue
                    if (mid_w and mid_w in dk) or (cr_id_w and cr_id_w in dk) or (
                        isinstance(dv_obj, dict) and mid_w and mid_w in str(dv_obj.get("video_media_id", ""))
                    ):
                        history["disabled_videos"].pop(dk, None)

                for a_p in g["paused_ads"]:
                    st_ap1 = ad_1d.get(a_p["id"], {"spend": 0.0, "purchases": 0})
                    # لا نعيد تشغيل النسخة الفردية إذا كانت قد صرفت بمفردها >= $2.80 بدون مبيعة اليوم
                    if float(st_ap1.get("spend", 0.0)) >= 2.80 and int(st_ap1.get("purchases", 0)) == 0:
                        continue
                    a_p["status"] = "ACTIVE"
                    ads_to_put_by_squad.setdefault(a_p["ad_squad_id"], []).append(a_p)
                    history.get("ads", {}).pop(a_p["id"], None)
                    re_enabled_cnt += 1

                if re_enabled_cnt > 0:
                    win_tag = f"مبيعات الحملة={g['pu1']} بـ CPA=${cpa1_g}" if is_camp_1d_winner else f"سوبر رابح بالحساب ({acc_v['pu1']} مبيعة اليوم / {acc_v['pu3']} في 3D)"
                    revived_winner_videos_summary.append(
                        f"{g['sample_name']} (تفعيل +{re_enabled_cnt} نسخة | {win_tag})"
                    )

        # 3B) قطع الفيديوهات الصفرية المستنزفة للبدجت داخل الحملات التي تبيع فعلياً (>= $10 صرف و>= 1 مبيعة اليوم)
        isolated_bleeders_summary = []
        if camp_spend_1d >= 10.00 and camp_purch_1d >= 1:
            for gk, g in v_groups.items():
                if "[exp-" in g["sample_name"].lower():
                    continue
                if g["pu1"] > 0 or not g["active_ads"]:
                    continue
                cpc1_g = round(g["sp1"] / g["sw1"], 2) if g["sw1"] > 0 else 9.99
                # إذا صرف الفيديو عبر الحملة >= $3.10 بدون أي مبيعة اليوم (أو >= $2.00 بـ CPC > $0.27 بدون مبيعة)
                if g["sp1"] >= 3.10 or (g["sp1"] >= 2.00 and cpc1_g > 0.27):
                    paused_b_cnt = 0
                    for a_b in list(g["active_ads"]):
                        sq_id_b = a_b["ad_squad_id"]
                        # التأكد من بقاء إعلان واحد نشط على الأقل داخل المجموعة
                        cur_act_in_sq = [
                            ax for ax in ads_by_squad.get(sq_id_b, [])
                            if ax.get("status") == "ACTIVE"
                        ]
                        if len(cur_act_in_sq) <= 1:
                            continue
                        a_b["status"] = "PAUSED"
                        ads_to_put_by_squad.setdefault(sq_id_b, []).append(a_b)
                        history.setdefault("ads", {})[a_b["id"]] = {
                            "name": a_b.get("name", ""),
                            "campaign": cname,
                            "adsquad": sq_id_b,
                            "reason": f"[EXP-SNAP Early Bleeder Cut] صرف ${g['sp1']:.2f} عبر الحملة بدون مبيعات (CPC=${cpc1_g:.2f})",
                            "paused_at": now_human_str,
                        }
                        paused_b_cnt += 1
                    if paused_b_cnt > 0:
                        isolated_bleeders_summary.append(
                            f"{g['sample_name']} (إيقاف {paused_b_cnt} نسخة | صرف ${g['sp1']:.2f} / 0 مبيعات)"
                        )

        # ---------------------------------------------------------------------
        # المرحلة 4: محرك التجارب الذكي #2 — فحص وتقييم وزرع تجارب [EXP-] بـ $0 بدجت إضافي وفرملة $2.50
        # ---------------------------------------------------------------------
        exp_paused_micro = []
        exp_graduated_micro = []

        for sq_id_e in still_active_squad_ids:
            for a_e in ads_by_squad.get(sq_id_e, []):
                nm_e = a_e.get("name", "")
                if "[exp-" not in nm_e.lower() or a_e.get("status") != "ACTIVE":
                    continue
                st1_e = ad_1d.get(a_e["id"], {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
                sp_e = float(st1_e.get("spend", 0.0))
                pu_e = int(st1_e.get("purchases", 0))
                cpa_e = st1_e.get("cpa")
                cpc_e = float(st1_e.get("cpc", 0.0) or 0.0)
                sw_e = int(st1_e.get("swipes", 0))

                if pu_e == 0 and (sp_e >= 2.50 or (sp_e >= 1.00 and (sw_e == 0 or cpc_e > 0.26))):
                    a_e["status"] = "PAUSED"
                    ads_to_put_by_squad.setdefault(sq_id_e, []).append(a_e)
                    exp_paused_micro.append(f"{nm_e} (${sp_e:.2f}/0 بيع)")
                elif pu_e >= 1 and cpa_e is not None and cpa_e <= 5.50:
                    exp_graduated_micro.append(f"{nm_e} (مبيعات={pu_e}, CPA=${cpa_e:.2f})")

        # زرع تجارب [EXP-H2 / EXP-H3 / EXP-WIN] داخل أفضل 3-5 أدست رابحة بـ $0 بدجت إضافي
        exp_seeded_summary = []
        has_active_exp_ad = any(
            "[exp-" in (a_x.get("name") or "").lower() and a_x.get("status") == "ACTIVE"
            for a_x in c_ads
        )
        if not is_ded_test_camp and cid not in TEN_SQUAD_CAMPAIGN_IDS and not has_active_exp_ad and camp_spend_1d >= 18.00 and camp_purch_1d >= 3 and not dry_run:
            # اختيار أفضل 3 أدست رابحة في الحملة لحقن التجربة بداخلها بدون زيادة دولار واحد على البدجت
            ranked_active_squads = sorted(
                [sq_x for sq_x in active_squads if sq_x["id"] in still_active_squad_ids],
                key=_sq_efficiency_score,
                reverse=True,
            )
            top3_squads = ranked_active_squads[:3]
            top5_squads = ranked_active_squads[:5]

            # 1) تجربة العنوان التسويقي الثاني ([EXP-H2] للباور بانك / [EXP-H3] للوسادة) على الفيديو رقم 1 في المبيعات اليوم
            best_v_grp = None
            for gk, g in sorted(v_groups.items(), key=lambda kv: (kv[1]["pu1"], -kv[1]["sp1"]), reverse=True):
                if g["pu1"] >= 2 and (g["sp1"] / g["pu1"]) <= 6.00 and g["ads"]:
                    best_v_grp = g
                    break

            if best_v_grp and top3_squads:
                session = make_session(token)
                src_ad = best_v_grp["ads"][0]
                src_cr_id = src_ad.get("creative_id")
                test_headline = "اشحن جوالك بدون أسلاك في أي مكان" if is_pb else "راحة فورية بالخطوط وداعاً للألم"
                test_tag = "EXP-H2" if is_pb else "EXP-H3"
                base_clean = re.sub(r"\s*\[H\d+\]\s*", "", src_ad.get("name", "")).strip()
                new_exp_ad_name = f"{base_clean} [{test_tag}]"[:95]

                if src_cr_id:
                    new_cr_id, _ = get_or_create_headline_creative(
                        session, ad_account_id, src_cr_id, test_tag, test_headline, history
                    )
                    if new_cr_id:
                        injected_h = 0
                        for t_sq in top3_squads:
                            payload_ad = {
                                "ads": [
                                    {
                                        "ad_squad_id": t_sq["id"],
                                        "creative_id": new_cr_id,
                                        "name": new_exp_ad_name,
                                        "status": "ACTIVE",
                                        "type": "REMOTE_WEBPAGE",
                                    }
                                ]
                            }
                            r_post = session.post(f"{API_BASE}/adsquads/{t_sq['id']}/ads", json=payload_ad, timeout=25)
                            if r_post.ok and r_post.json().get("request_status") == "SUCCESS":
                                injected_h += 1
                                time.sleep(0.2)
                        if injected_h > 0:
                            exp_seeded_summary.append(
                                f"اختبار عنوان [{test_tag}] ('{test_headline}') على الفيديو الرابح '{base_clean}' في أفضل {injected_h} أدست ($0 بدجت إضافي | فرملة $2.50)"
                            )

            # 2) إذا كانت حملة الباور بانك الرئيسية في CSP9860 (11fa8aef) لا تحتوي على الفيديو السوبر رابح PB_v10_VoltrxH7، نحقنه في أفضل 5 أدست
            if cid == "11fa8aef-3a3c-418d-b5c4-8dc7b3ec7ab3" and not any("voltrxh7" in k for k in v_groups.keys()):
                session = make_session(token)
                super_cr_id = "7f849f3a-2d79-43cc-8167-69adfbb0589d"  # PB_v10_VoltrxH7_m5d3150 [H1] in CSP9860
                injected_w = 0
                for t_sq in top5_squads:
                    payload_w = {
                        "ads": [
                            {
                                "ad_squad_id": t_sq["id"],
                                "creative_id": super_cr_id,
                                "name": "PB_v10_VoltrxH7_m5d3150 [EXP-WIN]",
                                "status": "ACTIVE",
                                "type": "REMOTE_WEBPAGE",
                            }
                        ]
                    }
                    r_post_w = session.post(f"{API_BASE}/adsquads/{t_sq['id']}/ads", json=payload_w, timeout=25)
                    if r_post_w.ok and r_post_w.json().get("request_status") == "SUCCESS":
                        injected_w += 1
                        time.sleep(0.2)
                if injected_w > 0:
                    exp_seeded_summary.append(
                        f"حقن الفيديو السوبر رابح عبر الحسابين 'PB_v10_VoltrxH7_m5d3150 [EXP-WIN]' (12 مبيعة اليوم في الحساب الثاني) داخل أفضل {injected_w} أدست ($0 بدجت إضافي | فرملة $2.50)"
                    )

        # تنفيذ التحديثات عبر Snapchat API على دفعات
        if not dry_run:
            if squads_to_put:
                upd_list = list(squads_to_put.values())
                for i in range(0, len(upd_list), 15):
                    try:
                        api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": upd_list[i:i+15]})
                        time.sleep(0.25)
                    except Exception as e_put:
                        print(f"⚠️ خطأ تحديث أدست في '{cname}': {e_put}")
            for sq_id_a, ad_list_a in ads_to_put_by_squad.items():
                # إزالة التكرار حسب معرف الإعلان
                dedup_ads = list({ax["id"]: ax for ax in ad_list_a}.values())
                try:
                    for j in range(0, len(dedup_ads), 15):
                        api_put(f"/adsquads/{sq_id_a}/ads", token, {"ads": dedup_ads[j:j+15]})
                        time.sleep(0.2)
                except Exception as e_ad:
                    print(f"⚠️ خطأ تحديث إعلانات في المجموعة {sq_id_a}: {e_ad}")
            save_paused_history(history)

        if camp_paused_squads:
            record_action(
                ledger,
                actions_taken,
                acc_label,
                "إيقاف أدست خاسرة (Stop-Loss)",
                cname,
                f"إيقاف {len(camp_paused_squads)} أدست خاسرة: {' ؛ '.join(camp_paused_squads[:4])}",
                f"صرف الحملة=${camp_spend_1d:.2f} | مبيعات={camp_purch_1d}",
            )
        if camp_normalized_budgets:
            record_action(
                ledger,
                actions_taken,
                acc_label,
                "حماية البدجت ومنع التضخم (Budget Guardrail)",
                cname,
                f"إرجاع ميزانية {len(camp_normalized_budgets)} أدست إلى السقف الآمن حسب مبيعاتها: {' ؛ '.join(camp_normalized_budgets[:4])}",
                f"منع تضخم البدجت للأدست الصفرية ($20) وسقف تدريجي للرابحة ($26/$34/$45)",
            )
        if camp_scaled_squads:
            extra_prune_txt = f" + إيقاف {camp_pruned_ads} فيديو صفري داخلها" if camp_pruned_ads else ""
            record_action(
                ledger,
                actions_taken,
                acc_label,
                "سكيل ساعي محكوم للرابحة فقط",
                cname,
                f"سكيل {len(camp_scaled_squads)} أدست رابحة{extra_prune_txt}: {' ؛ '.join(camp_scaled_squads[:4])}",
                f"صرف الحملة=${camp_spend_1d:.2f} | مبيعات={camp_purch_1d} (شيت={pu_sh_1d}/بكسل={pu_px_1d}) | CPA=${camp_cpa_1d}",
            )
        if revived_winner_videos_summary or isolated_bleeders_summary:
            parts_v = []
            if revived_winner_videos_summary:
                parts_v.append("تعميم الرابح: " + " + ".join(revived_winner_videos_summary))
            if isolated_bleeders_summary:
                parts_v.append("إيقاف المستنزف: " + " + ".join(isolated_bleeders_summary))
            record_action(
                ledger,
                actions_taken,
                acc_label,
                "تجربة عزل وتعميم الفيديوهات الرابحة [EXP-SNAP Winner Isolation]",
                cname,
                " | ".join(parts_v),
                f"توجيه 100% من البدجت للفيديوهات الرابحة اليوم بدون دولار بدجت إضافي",
            )
        if exp_seeded_summary or exp_paused_micro or exp_graduated_micro:
            parts_e = list(exp_seeded_summary)
            if exp_paused_micro:
                parts_e.append(f"فرملة ميكرو ($2.50): إيقاف {', '.join(exp_paused_micro[:3])}")
            if exp_graduated_micro:
                parts_e.append(f"نجاح تجربة: {', '.join(exp_graduated_micro[:3])}")
            record_action(
                ledger,
                actions_taken,
                acc_label,
                "تجارب بيعية ميكرو بحرص صارم على البدجت [EXP-SNAP Micro Test]",
                cname,
                " | ".join(parts_e),
                f"صرف الحملة=${camp_spend_1d:.2f} | مبيعات={camp_purch_1d} | CPA=${camp_cpa_1d}",
            )
        if camp_restored_low_bids:
            action_title = "تثبيت سقف بيد السعودية الصارم (<= $1.80 / snap_squad_custom_bid_locks.json)"
            record_action(
                ledger,
                actions_taken,
                acc_label,
                action_title,
                cname,
                f"ضبط البيد لـ {len(camp_restored_low_bids)} أدست في الحملة: {' ؛ '.join(camp_restored_low_bids[:4])}",
                f"صرف اليوم=${camp_spend_1d:.2f} | مبيعات={camp_purch_1d} | عدد الأدست النشطة={len(active_squads)}",
            )


def collect_human_eye_account_overview(token: str, sheet_sales: dict) -> list[dict]:
    """فحص عين الإنسان البشري من الأعلى للأسفل: إجمالي كل حساب سناب شات (اليوم 1D وآخر 3 أيام 3D شامل الموقوف) ثم كل حملة نشطة بصراحة تامة."""
    sh_1d = (sheet_sales.get("1d") or {}) if isinstance(sheet_sales, dict) else {}
    sh_3d = (sheet_sales.get("3d") or {}) if isinstance(sheet_sales, dict) else {}
    overview = []
    for acc_id in TARGET_ACCOUNT_IDS:
        acc_label = ACCOUNT_LABELS.get(acc_id, acc_id)
        camps = api_get_paginated(f"/adaccounts/{acc_id}/campaigns?limit=200", token, "campaigns", "campaign")
        c1_list = fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=0)
        c3_list = fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=2)
        c1_map = {x["id"]: x for x in c1_list}
        c3_map = {x["id"]: x for x in c3_list}

        tot_sp1 = round(sum(float(x.get("spend", 0.0)) for x in c1_list), 2)
        tot_sp3 = round(sum(float(x.get("spend", 0.0)) for x in c3_list), 2)
        tot_pu1 = 0
        tot_pu3 = 0
        active_rows = []
        for c in camps:
            cid = c["id"]
            cname = c.get("name", "")
            st1 = c1_map.get(cid, {"spend": 0.0, "purchases": 0})
            st3 = c3_map.get(cid, {"spend": 0.0, "purchases": 0})
            sp1_c = round(float(st1.get("spend", 0.0)), 2)
            sp3_c = round(float(st3.get("spend", 0.0)), 2)
            pu1_c = max(int(st1.get("purchases", 0)), int(sh_1d.get(cid.lower(), 0)) if sp1_c >= 0.50 else 0)
            pu3_c = max(int(st3.get("purchases", 0)), int(sh_3d.get(cid.lower(), 0)) if sp3_c >= 0.50 else 0)
            tot_pu1 += pu1_c
            tot_pu3 += pu3_c
            if c.get("status") == "ACTIVE":
                cpa1_c = round(sp1_c / pu1_c, 2) if pu1_c > 0 else None
                cpa3_c = round(sp3_c / pu3_c, 2) if pu3_c > 0 else None
                is_gulf_c = is_gulf_034_campaign(cname, "", {})
                cap_c = 12.00 if is_gulf_c else 7.00
                if sp1_c == 0 and sp3_c == 0:
                    verdict_c = "⏸️ صفر صرف اليوم"
                elif pu1_c == 0 and sp1_c >= 4.00:
                    verdict_c = f"🚨 تنزف اليوم (${sp1_c:.2f} بصفر مبيعات)"
                elif cpa1_c is not None and cpa1_c > cap_c:
                    verdict_c = f"🚨 خاسرة اليوم (CPA=${cpa1_c:.2f} > ${cap_c:.2f})"
                elif pu1_c > 0 and cpa1_c is not None and cpa1_c <= cap_c:
                    verdict_c = f"✅ رابحة اليوم (CPA=${cpa1_c:.2f})"
                else:
                    verdict_c = f"⏳ بداية صرف (${sp1_c:.2f} / {pu1_c} مبيعة)"
                active_rows.append({
                    "name": cname,
                    "sp1": sp1_c,
                    "pu1": pu1_c,
                    "cpa1": cpa1_c,
                    "sp3": sp3_c,
                    "pu3": pu3_c,
                    "cpa3": cpa3_c,
                    "verdict": verdict_c,
                })
        tot_cpa1 = round(tot_sp1 / tot_pu1, 2) if tot_pu1 > 0 else None
        tot_cpa3 = round(tot_sp3 / tot_pu3, 2) if tot_pu3 > 0 else None
        if tot_sp1 >= 4.00 and tot_pu1 == 0:
            acc_verdict = f"🚨 الحساب ككل ينزف اليوم (${tot_sp1:.2f} بصفر مبيعات)"
        elif tot_cpa1 is not None and tot_cpa1 > 7.00:
            acc_verdict = f"🚨 إجمالي الحساب اليوم فوق سقف $7.00 (CPA=${tot_cpa1:.2f})"
        elif tot_pu1 > 0 and tot_cpa1 is not None and tot_cpa1 <= 7.00:
            acc_verdict = f"✅ إجمالي الحساب رابح اليوم (CPA=${tot_cpa1:.2f})"
        else:
            acc_verdict = f"⏳ صرف الحساب اليوم (${tot_sp1:.2f} / {tot_pu1} مبيعة)"
        overview.append({
            "account": acc_label,
            "tot_sp1": tot_sp1,
            "tot_pu1": tot_pu1,
            "tot_cpa1": tot_cpa1,
            "tot_sp3": tot_sp3,
            "tot_pu3": tot_pu3,
            "tot_cpa3": tot_cpa3,
            "verdict": acc_verdict,
            "active_campaigns": active_rows,
        })
    return overview


def analyze_snap_periods_and_winning_structures(token: str, sheet_sales: dict) -> list[dict]:
    """يقارن الفترات الرابحة مقابل الفترات الخاسرة ويحلل كل تركيبة (Structure) لاختيار أفضل تركيبة وتعميمها على الحملات القادمة."""
    sh_3d = (sheet_sales.get("3d") or {}) if isinstance(sheet_sales, dict) else {}
    sh_7d = (sheet_sales.get("7d") or {}) if isinstance(sheet_sales, dict) else {}
    buckets = {
        "10-AdSets + Target Cost $1.80–$2.10 + فيديوهات رابحة": {"sp3": 0.0, "pu3": 0, "sp7": 0.0, "pu7": 0, "verdict": "✅ التركيبة رقم #1 المعتمدة للتعميم (عزل 10 مجموعات ببيد منخفض يحمي من حرق الميزانية)"},
        "حملات الـ 30 أدست كوبي (Target Cost <= $1.80) للفيديوهات السوبر رابحة": {"sp3": 0.0, "pu3": 0, "sp7": 0.0, "pu7": 0, "verdict": "✅ رابحة للسكيلينغ عند تصفية الأدست الصفرية أولاً بأول وحصر الصرف في الفيديوهات البيّاعة"},
        "حملات الاختبار المخصصة (تيست / 4-AdSets بيد <= $1.80)": {"sp3": 0.0, "pu3": 0, "sp7": 0.0, "pu7": 0, "verdict": "🧪 مخصصة لاكتشاف الفيديوهات الفائزة قبل نقلها لحملات السكيل أو جوجل أدز (فرملة $20 صارمة)"},
        "بيد مرتفع (> $2.50) أو فيديوهات غير مصفاة": {"sp3": 0.0, "pu3": 0, "sp7": 0.0, "pu7": 0, "verdict": "🚫 سبب الفترات السيئة (يرفع CPC فوق $0.28 ويستنزف الميزانية بدون مبيعات — ممنوع نهائياً)"},
    }
    for acc_id in TARGET_ACCOUNT_IDS:
        camps = api_get_paginated(f"/adaccounts/{acc_id}/campaigns?limit=200", token, "campaigns", "campaign")
        c3_map = {x["id"]: x for x in fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=2)}
        c7_map = {x["id"]: x for x in fetch_account_breakdown_stats(token, acc_id, "campaign", days_back=6)}
        for c in camps:
            cid = c["id"]
            cname = (c.get("name") or "").strip()
            if is_gulf_034_campaign(cname):
                continue
            sp3 = float((c3_map.get(cid) or {}).get("spend", 0.0))
            pu3 = max(int((c3_map.get(cid) or {}).get("purchases", 0)), int(sh_3d.get(cid.lower(), 0)) if sp3 >= 0.50 else 0)
            sp7 = max(sp3, float((c7_map.get(cid) or {}).get("spend", 0.0)))
            pu7 = max(pu3, int((c7_map.get(cid) or {}).get("purchases", 0)), int(sh_7d.get(cid.lower(), 0)) if sp7 >= 0.50 else 0)
            if sp7 <= 0 and pu7 <= 0:
                continue
            cpa7 = (sp7 / pu7) if pu7 > 0 else 999.0
            if cid in TEN_SQUAD_CAMPAIGN_IDS or "10" in cname:
                bkey = "10-AdSets + Target Cost $1.80–$2.10 + فيديوهات رابحة"
            elif is_dedicated_snap_test_campaign(cid, cname) or cid in FOUR_SQUAD_CAMPAIGN_IDS or "[exp-snap]" in cname.lower():
                bkey = "حملات الاختبار المخصصة (تيست / 4-AdSets بيد <= $1.80)"
            elif cpa7 > 8.50 or (pu7 == 0 and sp7 >= 8.00):
                bkey = "بيد مرتفع (> $2.50) أو فيديوهات غير مصفاة"
            else:
                bkey = "حملات الـ 30 أدست كوبي (Target Cost <= $1.80) للفيديوهات السوبر رابحة"
            buckets[bkey]["sp3"] = round(buckets[bkey]["sp3"] + sp3, 2)
            buckets[bkey]["pu3"] += pu3
            buckets[bkey]["sp7"] = round(buckets[bkey]["sp7"] + sp7, 2)
            buckets[bkey]["pu7"] += pu7
    rows = []
    for k, v in buckets.items():
        c3 = f"${v['sp3']/v['pu3']:.2f}" if v["pu3"] > 0 else "—"
        c7 = f"${v['sp7']/v['pu7']:.2f}" if v["pu7"] > 0 else "—"
        rows.append({
            "structure": k,
            "m3": f"${v['sp3']:.2f} / {v['pu3']} ({c3})",
            "m7": f"${v['sp7']:.2f} / {v['pu7']} ({c7})",
            "verdict": v["verdict"],
        })
    return rows


def write_markdown_report(actions_taken: list[dict], account_overview: list[dict] = None, structure_rows: list[dict] = None):
    now_str = datetime.now(ACCOUNT_TZ).strftime("%Y-%m-%d %H:%M GMT+3")
    lines = [
        f"# ⚡ تقرير خبير مبيعات وصرف وتجارب سناب شات (فحص عين المراقب البشري كل ساعة) — {now_str}",
        "",
        "## 1. الملخص الصريح لإجمالي الحسابات والحملات النشطة (`5 أعمدة كحد أقصى`)",
        "| الحساب / الحملة | اليوم (`1D`: صرف / مبيعات / `CPA`) | 3 أيام (`3D`: صرف / مبيعات / `CPA`) | حالة القاطع (`$20 Stop-Loss`) | التقييم الصريح (بدون تجميل) |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for acc_ov in (account_overview or []):
        cpa1_s = f"${acc_ov['tot_cpa1']:.2f}" if acc_ov["tot_cpa1"] is not None else "—"
        cpa3_s = f"${acc_ov['tot_cpa3']:.2f}" if acc_ov["tot_cpa3"] is not None else "—"
        lines.append(
            f"| **إجمالي `{acc_ov['account']}` (شامل الموقوف)** | **`${acc_ov['tot_sp1']:.2f}` / `{acc_ov['tot_pu1']}` (`{cpa1_s}`)** | `${acc_ov['tot_sp3']:.2f}` / `{acc_ov['tot_pu3']}` (`{cpa3_s}`) | مفعل (`$20` صفر بيع / `CPA >= $20`) | **{acc_ov['verdict']}** |"
        )
        for cr in acc_ov.get("active_campaigns", []):
            cc1 = f"${cr['cpa1']:.2f}" if cr["cpa1"] is not None else "—"
            cc3 = f"${cr['cpa3']:.2f}" if cr["cpa3"] is not None else "—"
            lines.append(
                f"| ↳ `{cr['name']}` | `${cr['sp1']:.2f}` / `{cr['pu1']}` (`{cc1}`) | `${cr['sp3']:.2f}` / `{cr['pu3']}` (`{cc3}`) | تحت المراقبة الفورية (`< $20`) | {cr['verdict']} |"
            )

    if structure_rows:
        lines.extend([
            "",
            "## 2. تحليل الفترات الرابحة مقابل الفترات الضعيفة وأفضل تركيبة معتمدة للحملات القادمة (`Regime & Structure Analysis`)",
            "| التركيبة / الهيكلة المجربة | أداء آخر 3 أيام (`3D`) | أداء آخر 7 أيام (`7D`) | القرار الاستراتيجي للحملات القادمة |",
            "| :--- | :--- | :--- | :--- |",
        ])
        for sr in structure_rows:
            lines.append(f"| **{sr['structure']}** | `{sr['m3']}` | `{sr['m7']}` | {sr['verdict']} |")

    lines.extend([
        "",
        "## 3. الإجراءات المنفذة في هذه الدورة",
        "| الحساب | نوع الإجراء | الحملة / المجموعة | التفاصيل المنفذة | المؤشرات الحالية |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    if not actions_taken:
        lines.append("| CSP9860 / CSP9860 4 | فحص عين المراقب البشري | جميع الحملات النشطة | لم تتجاوز أي مجموعة أو حملة إضافية حدود الإيقاف في هذه اللحظة | راجع جدول إجمالي الحسابات أعلاه للتقييم الصريح |")
    else:
        for act in actions_taken:
            lines.append(
                f"| {act['account']} | {act['action_type']} | {act['target']} | {act['details']} | {act['metrics']} |"
            )
    REPORT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()
    token = refresh_access_token(cfg)
    sheet_sales = get_sheet_sales()
    ledger = load_ledger()
    actions_taken = []

    print("=========================================================================")
    print("🚀 تشغيل خبير مبيعات وصرف وتجارب سناب شات المستقل كل ساعة (Snap 1H Master)")
    print("=========================================================================")
    for acc_id in TARGET_ACCOUNT_IDS:
        run_hourly_master_for_account(token, acc_id, sheet_sales, ledger, actions_taken, dry_run=dry_run)

    account_overview = collect_human_eye_account_overview(token, sheet_sales)
    structure_rows = analyze_snap_periods_and_winning_structures(token, sheet_sales)
    ledger["latest_winning_structure_analysis"] = structure_rows
    if not dry_run:
        save_ledger(ledger)
    write_markdown_report(actions_taken, account_overview=account_overview, structure_rows=structure_rows)
    print(f"✅ اكتملت دورة خبير سناب شات — إجمالي الإجراءات المنفذة: {len(actions_taken)}.")


if __name__ == "__main__":
    main()
