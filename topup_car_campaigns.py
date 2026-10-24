from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from snap_auth import API_BASE, load_config, refresh_access_token
from snap_monitor import (
    ACCOUNT_TZ,
    fetch_account_breakdown_stats,
    load_paused_history,
    save_paused_history,
)

sys.stdout.reconfigure(encoding="utf-8")

CAMPAIGNS_TO_MANAGE = [
    {
        "account_name": "CSP9860",
        "account_id": "350cb560-ad77-4fb5-9bbf-4c090c9ad081",
        "campaign_id": "f8372cfb-ae46-44d0-bbb4-f39790ecc648",
        "prod_prefix": "Car",
    },
    {
        "account_name": "CSP9860",
        "account_id": "350cb560-ad77-4fb5-9bbf-4c090c9ad081",
        "campaign_id": "e23e7c95-3f15-4945-9844-8bd73e2d79d3",
        "prod_prefix": "PB",
    },
    {
        "account_name": "CSP9860 4",
        "account_id": "0d00760b-6793-441f-a675-e35371ce9c7c",
        "campaign_id": "558c5d45-f08e-400c-a499-7668e801c347",
        "prod_prefix": "Car",
    },
    {
        "account_name": "CSP9860 4",
        "account_id": "0d00760b-6793-441f-a675-e35371ce9c7c",
        "campaign_id": "803324e6-052e-441b-99fb-4d43e56c5c78",
        "prod_prefix": "PB",
    },
]

COHORT_SPECS = [
    {
        "key": "A",
        "prefix_tpl": "Copy A - {prod} 1.9 Max [H1]",
        "bid_micro": 1_900_000,
        "hl_tag": "H1",
    },
    {
        "key": "B",
        "prefix_tpl": "Copy B - {prod} 2.1 Max [H2]",
        "bid_micro": 2_100_000,
        "hl_tag": "H2",
    },
]

GOOD_COHORT_MAX_CPA = 6.00
GOOD_COHORT_MIN_SALES = 2


def make_session(token: str) -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return s


def get_all_pages(s: requests.Session, url: str, key: str) -> list:
    out = []
    u = f"{API_BASE}{url}" if url.startswith("/") else url
    while u:
        r = s.get(u, timeout=25)
        if not r.ok:
            break
        d = r.json()
        for el in d.get(key, []):
            if key[:-1] in el:
                out.append(el[key[:-1]])
        u = d.get("paging", {}).get("next_link")
    return out


def clone_cohort_15x(
    s: requests.Session,
    camp_id: str,
    template_squad: dict,
    template_ads: list[dict],
    cohort_prefix: str,
    bid_micro: int,
    count_to_create: int,
    start_index: int,
) -> list[str]:
    print("   🚫 الدوبليكيت في سناب شات متوقف نهائياً بأمر المستخدم.", flush=True)
    return []


def main(dry_run: bool = False):
    cfg = load_config()
    token = refresh_access_token(cfg)
    s = make_session(token)
    history = load_paused_history()
    cohort_dup_log = history.setdefault("cohort_15x_reduplications", {})
    today_str = datetime.now(ACCOUNT_TZ).strftime("%Y-%m-%d")

    stats_cache_1d = {}
    stats_cache_3d = {}
    for item in CAMPAIGNS_TO_MANAGE:
        acc_id = item["account_id"]
        if acc_id not in stats_cache_1d:
            stats_cache_1d[acc_id] = {
                x["id"]: x for x in fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=0)
            }
            stats_cache_3d[acc_id] = {
                x["id"]: x for x in fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=2)
            }

    for item in CAMPAIGNS_TO_MANAGE:
        acc_name = item["account_name"]
        acc_id = item["account_id"]
        camp_id = item["campaign_id"]
        prod = item["prod_prefix"]

        rc = s.get(f"{API_BASE}/campaigns/{camp_id}", timeout=20).json()
        camp_obj = (rc.get("campaigns") or [{}])[0].get("campaign", {})
        if camp_obj and camp_obj.get("status") != "ACTIVE" and not dry_run:
            camp_obj["status"] = "ACTIVE"
            s.put(f"{API_BASE}/adaccounts/{acc_id}/campaigns", json={"campaigns": [camp_obj]}, timeout=20)
            print(f"✅ [{acc_name}] إعادة تفعيل الحملة '{camp_obj.get('name')}' ({camp_id})", flush=True)

        squads = get_all_pages(s, f"/campaigns/{camp_id}/adsquads?limit=100", "adsquads")
        ads = get_all_pages(s, f"/campaigns/{camp_id}/ads?limit=100", "ads")
        ads_by_sq = {}
        for a in ads:
            ads_by_sq.setdefault(a.get("ad_squad_id"), []).append(a)

        st1_map = stats_cache_1d.get(acc_id, {})
        st3_map = stats_cache_3d.get(acc_id, {})

        for c_spec in COHORT_SPECS:
            c_key = c_spec["key"]
            c_prefix = c_spec["prefix_tpl"].format(prod=prod)
            bid_micro = c_spec["bid_micro"]

            cohort_squads = [sq for sq in squads if (sq.get("name") or "").startswith(c_prefix)]
            if not cohort_squads:
                continue

            active_cohort_squads = [sq for sq in cohort_squads if sq.get("status") == "ACTIVE"]

            tot_spend_3d = 0.0
            tot_purch_3d = 0
            tot_spend_1d = 0.0
            tot_purch_1d = 0
            for sq in cohort_squads:
                sid = sq["id"]
                s1 = st1_map.get(sid, {"spend": 0.0, "purchases": 0})
                s3 = st3_map.get(sid, {"spend": 0.0, "purchases": 0})
                tot_spend_1d = round(tot_spend_1d + s1["spend"], 2)
                tot_purch_1d += s1["purchases"]
                tot_spend_3d = round(tot_spend_3d + s3["spend"], 2)
                tot_purch_3d += s3["purchases"]

            eff_spend = tot_spend_3d if tot_spend_3d > 0 else tot_spend_1d
            eff_purch = tot_purch_3d if tot_purch_3d > 0 else tot_purch_1d
            cohort_cpa = round(eff_spend / eff_purch, 2) if eff_purch > 0 else None

            print(
                f"📊 [{acc_name} | {c_prefix}] النشط: {len(active_cohort_squads)}/{len(cohort_squads)} | "
                f"الصرف: ${eff_spend:.2f} | المبيعات: {eff_purch} | CPA المجموعة 15 = ${cohort_cpa if cohort_cpa is not None else 'N/A'}",
                flush=True,
            )

            # قاعدة المستخدم الصارمة:
            # 1) إطفاء أي أدست فيها CPC > $0.28 بعد أول $1.00 على طول، أو صرفت >= $4.00 بدون مبيعات، أو CPA > $6.00
            # 2) عدم تجاوز 30 نسخة أدست في كل حملة نهائياً (15 كحد أقصى لـ Copy A و 15 كحد أقصى لـ Copy B = 30 إجمالاً)
            non_working_to_pause = []
            for sq in active_cohort_squads:
                sid = sq["id"]
                s1 = st1_map.get(sid, {"spend": 0.0, "purchases": 0, "cpc": 0.0, "swipes": 0})
                s3 = st3_map.get(sid, {"spend": 0.0, "purchases": 0, "cpc": 0.0, "swipes": 0})
                deliv = sq.get("delivery_status") or []
                is_broken_or_bad = (
                    "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" in deliv
                    or (s1["spend"] >= 4.00 and s1["purchases"] == 0)
                    or (s3["spend"] >= 4.00 and s3["purchases"] == 0)
                    or (s1["spend"] >= 1.00 and (s1.get("swipes", 0) == 0 or s1.get("cpc", 0.0) > 0.28))
                    or (s3["spend"] >= 1.00 and (s3.get("swipes", 0) == 0 or s3.get("cpc", 0.0) > 0.28))
                    or (s1["purchases"] > 0 and (s1["spend"] / s1["purchases"]) > GOOD_COHORT_MAX_CPA)
                )
                if is_broken_or_bad:
                    non_working_to_pause.append(sq)

            if non_working_to_pause and not dry_run:
                for sq_p in non_working_to_pause:
                    sq_p["status"] = "PAUSED"
                for i in range(0, len(non_working_to_pause), 20):
                    s.put(f"{API_BASE}/campaigns/{camp_id}/adsquads", json={"adsquads": non_working_to_pause[i : i + 20]}, timeout=25)
                print(
                    f"   🛑 تم إطفاء {len(non_working_to_pause)} أدست غير شغالة / خاسرة داخل '{c_prefix}'.",
                    flush=True,
                )
                active_cohort_squads = [sq for sq in active_cohort_squads if sq.get("status") == "ACTIVE"]

            # تقليص أي نسخ زائدة فوق 15 في نفس المجموعة الفرعية (لضمان عدم تجاوز 30 أدست في الحملة)
            if len(active_cohort_squads) > 15 and not dry_run:
                excess_cohort = active_cohort_squads[15:]
                for sq_ex in excess_cohort:
                    sq_ex["status"] = "PAUSED"
                for i in range(0, len(excess_cohort), 20):
                    s.put(f"{API_BASE}/campaigns/{camp_id}/adsquads", json={"adsquads": excess_cohort[i : i + 20]}, timeout=25)
                active_cohort_squads = active_cohort_squads[:15]
                print(f"   🛑 [سقف 15 للمجموعة / 30 للحملة] تم إيقاف {len(excess_cohort)} نسخة زائدة في '{c_prefix}'.", flush=True)

            # إيقاف الدوبليكيت في سناب شات نهائياً بأمر المستخدم: نكتفي بإطفاء أي أدست خاسرة دون إنشاء أي نسخ جديدة
            print(
                f"   🚫 الدوبليكيت متوقف نهائياً في سناب شات — الإبقاء على {len(active_cohort_squads)} أدست رابحة/سليمة فقط في '{c_prefix}'.",
                flush=True,
            )
            continue


def enforce_exact_15_per_cohort(target_start_iso: str = "2026-10-24T03:00:00.000Z"):
    print("🚫 الدوبليكيت وإعادة تفعيل الأدست الموقوفة في سناب شات متوقفان نهائياً بأمر المستخدم.", flush=True)
    return


if __name__ == "__main__":
    if "--enforce-15" in sys.argv:
        enforce_exact_15_per_cohort()
    else:
        dry = "--dry-run" in sys.argv
        main(dry_run=dry)
