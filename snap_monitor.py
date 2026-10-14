from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request
from snap_auth import API_BASE, api_get, load_config, refresh_access_token

sys.stdout.reconfigure(encoding="utf-8")

TARGET_ACCOUNT_ID = "350cb560-ad77-4fb5-9bbf-4c090c9ad081"  # CSP9860
HISTORY_FILE = Path(__file__).parent / "paused_history.json"
ACCOUNT_TZ = timezone(timedelta(hours=3))  # GMT+3 (Saudi/Qatar time)

# الكلمات المفتاحية لدول (عمان، قطر، البحرين، الكويت) بالإنجليزية والعربية والاختصارات الشائعة في حسابك
GULF_HIGHER_CPC_KEYWORDS = (
    "oman", "qatar", "qat", "bahrain", "kuwait", "kuwiat", "kwt",
    "عمان", "قطر", "البحرين", "بحرين", "كويت"
)


def is_gulf_034_campaign(campaign_name: str, adsquad_name: str = "") -> bool:
    combined = f"{campaign_name} {adsquad_name}".lower()
    return any(k in combined for k in GULF_HIGHER_CPC_KEYWORDS)


def load_paused_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"adsquads": {}, "ads": {}}


def save_paused_history(history: dict):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def api_put(endpoint: str, token: str, payload: dict) -> dict:
    url = f"{API_BASE}{endpoint}" if endpoint.startswith("/") else endpoint
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_account_breakdown_stats(token: str, ad_account_id: str, breakdown: str) -> list[dict]:
    now_local = datetime.now(ACCOUNT_TZ)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)

    start_str = start_local.isoformat()
    end_str = end_local.isoformat()
    fields = "impressions,swipes,spend,conversion_purchases"
    url = (
        f"/adaccounts/{ad_account_id}/stats?granularity=TOTAL"
        f"&start_time={urllib.parse.quote(start_str)}"
        f"&end_time={urllib.parse.quote(end_str)}"
        f"&breakdown={breakdown}&fields={fields}"
    )
    resp = api_get(url, token)
    breakdown_list = (
        resp.get("total_stats", [{}])[0]
        .get("total_stat", {})
        .get("breakdown_stats", {})
        .get(breakdown, [])
    )
    results = []
    for item in breakdown_list:
        st = item.get("stats", {})
        spend = round(st.get("spend", 0) / 1_000_000, 2)
        if spend <= 0:
            continue
        swipes = st.get("swipes", 0)
        purchases = st.get("conversion_purchases", 0)
        cpa = round(spend / purchases, 2) if purchases > 0 else None
        cpc = round(spend / swipes, 3) if swipes > 0 else spend
        results.append({
            "id": item["id"],
            "spend": spend,
            "impressions": st.get("impressions", 0),
            "swipes": swipes,
            "purchases": purchases,
            "cpa": cpa,
            "cpc": cpc,
        })
    return results


def evaluate_adsquad_rule(name: str, st: dict) -> tuple[bool, str]:
    is_copy = "copy" in name.lower()
    spend = st["spend"]
    purchases = st["purchases"]
    cpa = st["cpa"]

    if is_copy:
        if spend >= 5.0 and purchases == 0:
            return True, f"[Copy Rule] صرفت ${spend} (>= $5) بدون مبيعات"
        if purchases > 0 and cpa is not None and cpa >= 8.0:
            return True, f"[Copy Rule] سعر البيعة CPA = ${cpa} (>= $8) | الصرف: ${spend} | المبيعات: {purchases}"
    else:
        if spend >= 14.0 and purchases == 0:
            return True, f"[Normal Rule] صرفت ${spend} (>= $14) بدون مبيعات"
        if purchases > 0 and cpa is not None and cpa >= 20.0:
            return True, f"[Normal Rule] سعر البيعة CPA = ${cpa} (>= $20) | الصرف: ${spend} | المبيعات: {purchases}"

    return False, ""


def evaluate_ad_cpc_rule(st: dict, campaign_name: str, adsquad_name: str) -> tuple[bool, str]:
    spend = st["spend"]
    swipes = st["swipes"]
    cpc = st["cpc"]
    max_cpc = 0.34 if is_gulf_034_campaign(campaign_name, adsquad_name) else 0.28

    if spend >= 1.0:
        if swipes == 0:
            return True, f"[Ad CPC Rule] صرف الفيديو ${spend} بدون أي نقرة (الحد الأقصى ${max_cpc})"
        if cpc > max_cpc:
            return True, f"[Ad CPC Rule] صرف الفيديو ${spend} | النقرات: {swipes} | سعر النقرة CPC = ${cpc} (> ${max_cpc})"
    return False, ""


def run_automation(token: str, ad_account_id: str, dry_run: bool = False):
    history = load_paused_history()
    now_str = datetime.now(ACCOUNT_TZ).strftime("%Y-%m-%d %H:%M GMT+3")

    # Cache campaign names for quick lookup
    camps_resp = api_get(f"/adaccounts/{ad_account_id}/campaigns", token)
    camp_names = {
        c_wrap["campaign"]["id"]: c_wrap["campaign"].get("name", "")
        for c_wrap in camps_resp.get("campaigns", [])
        if "campaign" in c_wrap
    }
    squad_cache = {}

    # 1. One API call for all Ad Squad stats today
    squad_stats = fetch_account_breakdown_stats(token, ad_account_id, "adsquad")
    print(f"📊 عدد المجموعات الإعلانية (Ad Squads) التي صرفت اليوم: {len(squad_stats)}")

    for st in squad_stats:
        s_id = st["id"]
        if st["spend"] < 5.0:
            continue

        s_resp = api_get(f"/adsquads/{s_id}", token)
        squad = s_resp.get("adsquads", [{}])[0].get("adsquad", {})
        if not squad:
            continue
        squad_cache[s_id] = squad
        if squad.get("status") != "ACTIVE":
            continue

        s_name = squad.get("name", "")
        if s_id in history["adsquads"]:
            print(f"⏭️ تجاهل المجموعة '{s_name}' (صرف: ${st['spend']}) — أعدتَ تشغيلها يدوياً (فرصة أخرى).")
            continue

        should_pause, reason = evaluate_adsquad_rule(s_name, st)
        if should_pause:
            if dry_run:
                print(f"🔍 [مطابق للشرط] المجموعة: '{s_name}' | {reason}")
            else:
                squad["status"] = "PAUSED"
                api_put(f"/campaigns/{squad['campaign_id']}/adsquads", token, {"adsquads": [squad]})
                history["adsquads"][s_id] = {"name": s_name, "reason": reason, "paused_at": now_str}
                save_paused_history(history)
                print(f"🛑 تم إيقاف المجموعة: '{s_name}' | {reason}")
                time.sleep(0.3)

    # 2. One API call for all Ad (video) stats today
    ad_stats = fetch_account_breakdown_stats(token, ad_account_id, "ad")
    print(f"\n🎬 عدد الفيديوهات الإعلانية (Ads) التي صرفت اليوم: {len(ad_stats)}")

    for st in ad_stats:
        ad_id = st["id"]
        # Pre-filter: only inspect ads that spent >= $1.0 and have CPC > $0.28
        if st["spend"] < 1.0 or (st["swipes"] > 0 and st["cpc"] <= 0.28):
            continue

        a_resp = api_get(f"/ads/{ad_id}", token)
        ad = a_resp.get("ads", [{}])[0].get("ad", {})
        if not ad or ad.get("status") != "ACTIVE":
            continue

        ad_name = ad.get("name", "")
        s_id = ad.get("ad_squad_id")

        if s_id not in squad_cache:
            s_resp = api_get(f"/adsquads/{s_id}", token)
            squad_cache[s_id] = s_resp.get("adsquads", [{}])[0].get("adsquad", {})
        squad = squad_cache.get(s_id, {})
        s_name = squad.get("name", "")
        c_id = squad.get("campaign_id", "")
        if c_id and c_id not in camp_names:
            c_resp = api_get(f"/campaigns/{c_id}", token)
            camp_names[c_id] = c_resp.get("campaigns", [{}])[0].get("campaign", {}).get("name", "")
        c_name = camp_names.get(c_id, "")

        should_pause, reason = evaluate_ad_cpc_rule(st, c_name, s_name)
        if not should_pause:
            continue

        if ad_id in history["ads"]:
            print(f"⏭️ تجاهل الفيديو '{ad_name}' (حملة: '{c_name}', صرف: ${st['spend']}, CPC: ${st['cpc']}) — أعدتَ تشغيله يدوياً (فرصة أخرى).")
            continue

        if dry_run:
            print(f"🔍 [مطابق للشرط] الفيديو: '{ad_name}' (حملة: '{c_name}' / مجموعة: '{s_name}') | {reason}")
        else:
            ad["status"] = "PAUSED"
            api_put(f"/adsquads/{s_id}/ads", token, {"ads": [ad]})
            history["ads"][ad_id] = {
                "name": ad_name,
                "campaign": c_name,
                "adsquad": s_name,
                "reason": reason,
                "paused_at": now_str,
            }
            save_paused_history(history)
            print(f"🛑 تم إيقاف الفيديو: '{ad_name}' (حملة: '{c_name}' / مجموعة: '{s_name}') | {reason}")
            time.sleep(0.3)


def recheck_gulf_ads(token: str):
    history = load_paused_history()
    for aid, info in list(history["ads"].items()):
        s_id = info.get("adsquad_id")
        if not s_id:
            continue
        s = api_get(f"/adsquads/{s_id}", token)["adsquads"][0]["adsquad"]
        c = api_get(f"/campaigns/{s['campaign_id']}", token)["campaigns"][0]["campaign"]
        gulf = is_gulf_034_campaign(c["name"], s["name"])
        reason = info.get("reason", "")
        print(f"Ad: {info['name']} | Camp: {c['name']} | Squad: {s['name']} | Gulf($0.34): {gulf} | {reason}")
        if gulf and "0.357" not in reason and "0.343" not in reason:
            a = api_get(f"/ads/{aid}", token)["ads"][0]["ad"]
            a["status"] = "ACTIVE"
            api_put(f"/adsquads/{s_id}/ads", token, {"ads": [a]})
            del history["ads"][aid]
            save_paused_history(history)
            print(f"   ✅ أُعيد تشغيل الفيديو '{info['name']}' لأنه في حملة '{c['name']}' وسعر نقرته أقل من $0.34!")


if __name__ == "__main__":
    cfg = load_config()
    token = refresh_access_token(cfg)
    if "--recheck-gulf" in sys.argv:
        recheck_gulf_ads(token)
    else:
        dry = "--dry-run" in sys.argv
        run_automation(token, TARGET_ACCOUNT_ID, dry_run=dry)
