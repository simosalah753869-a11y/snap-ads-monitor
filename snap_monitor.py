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
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            data.setdefault("adsquads", {})
            data.setdefault("ads", {})
            data.setdefault("bid_bumps", {})
            return data
        except Exception:
            pass
    return {"adsquads": {}, "ads": {}, "bid_bumps": {}}


def parse_iso_utc(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def save_paused_history(history: dict):
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def api_put(endpoint: str, token: str, payload: dict) -> dict:
    url = f"{API_BASE}{endpoint}" if endpoint.startswith("/") else endpoint
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(1.5 * (attempt + 1))



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


def evaluate_adsquad_rule(adsquad_name: str, st: dict, campaign_name: str = "") -> tuple[bool, str]:
    is_copy = "copy" in adsquad_name.lower()
    is_gulf = is_gulf_034_campaign(campaign_name, adsquad_name)
    spend = st["spend"]
    purchases = st["purchases"]
    cpa = st["cpa"]

    if is_copy:
        if is_gulf:
            # دول (قطر، عمان، البحرين، الكويت): صرف > 8$ بدون مبيعة، أو سعر المبيعة >= 14$
            if spend > 8.0 and purchases == 0:
                return True, f"[Gulf Copy Rule] صرفت ${spend} (> $8) بدون مبيعات"
            if purchases > 0 and cpa is not None and cpa >= 14.0:
                return True, f"[Gulf Copy Rule] سعر البيعة CPA = ${cpa} (>= $14) | الصرف: ${spend} | المبيعات: {purchases}"
        else:
            # باقي الدول (السعودية وغيرها): صرف >= 5$ بدون مبيعة، أو سعر المبيعة >= 8$
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

    camps_resp = api_get(f"/adaccounts/{ad_account_id}/campaigns", token)
    camp_names = {}
    active_campaigns = []
    for c_wrap in camps_resp.get("campaigns", []):
        c = c_wrap.get("campaign", {})
        if not c:
            continue
        camp_names[c["id"]] = c.get("name", "")
        if c.get("status") == "ACTIVE":
            active_campaigns.append(c)

    squad_cache = {}

    # 1. One API call for all Ad Squad stats today
    squad_stats = fetch_account_breakdown_stats(token, ad_account_id, "adsquad")
    spend_by_squad = {st["id"]: st["spend"] for st in squad_stats}
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
        c_id = squad.get("campaign_id", "")
        if c_id and c_id not in camp_names:
            c_resp = api_get(f"/campaigns/{c_id}", token)
            camp_names[c_id] = c_resp.get("campaigns", [{}])[0].get("campaign", {}).get("name", "")
        c_name = camp_names.get(c_id, "")

        if s_id in history["adsquads"]:
            print(f"⏭️ تجاهل المجموعة '{s_name}' (صرف: ${st['spend']}) — أعدتَ تشغيلها يدوياً (فرصة أخرى).")
            continue

        should_pause, reason = evaluate_adsquad_rule(s_name, st, c_name)
        if should_pause:
            if dry_run:
                print(f"🔍 [مطابق للشرط] المجموعة: '{s_name}' (حملة: '{c_name}') | {reason}")
            else:
                squad["status"] = "PAUSED"
                api_put(f"/campaigns/{squad['campaign_id']}/adsquads", token, {"adsquads": [squad]})
                history["adsquads"][s_id] = {
                    "name": s_name,
                    "campaign": c_name,
                    "reason": reason,
                    "paused_at": now_str,
                }
                save_paused_history(history)
                print(f"🛑 تم إيقاف المجموعة: '{s_name}' (حملة: '{c_name}') | {reason}")
                time.sleep(0.3)

    # 2. One API call for all Ad (video) stats today
    ad_stats = fetch_account_breakdown_stats(token, ad_account_id, "ad")
    print(f"\n🎬 عدد الفيديوهات الإعلانية (Ads) التي صرفت اليوم: {len(ad_stats)}")

    for st in ad_stats:
        ad_id = st["id"]
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

    # 3. Low-spend (< $1.00) 2-hour +$0.10 Bid Bump rule (max 5 times ONCE per Ad Squad lifetime, 2 hours apart)
    now_utc = datetime.now(timezone.utc)
    today_local = datetime.now(ACCOUNT_TZ)
    today_date_str = today_local.strftime("%Y-%m-%d")
    print(f"\n⏱️ فحص المجموعات النشطة التي صرفت أقل من $1.00 كل ساعتين بحد أقصى 5 مرات (مرة واحدة فقط لكل Ad Squad مدى الحياة) (عدد الحملات النشطة: {len(active_campaigns)})...")
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        sq_resp = api_get(f"/campaigns/{c_id}/adsquads", token)
        for sq_wrap in sq_resp.get("adsquads", []):
            squad = sq_wrap.get("adsquad", {})
            if not squad or squad.get("status") != "ACTIVE":
                continue
            s_id = squad["id"]
            s_name = squad.get("name", "")

            bump_rec = history["bid_bumps"].get(s_id, {"count": 0, "last_bump_at": None})
            last_bump_dt = parse_iso_utc(bump_rec.get("last_bump_at", ""))
            bump_count = bump_rec.get("count", 0)

            today_spend = spend_by_squad.get(s_id, 0.0)
            if today_spend >= 1.0:
                if not bump_rec.get("done"):
                    bump_rec["done"] = True
                    bump_rec["name"] = s_name
                    bump_rec["campaign"] = c_name
                    history["bid_bumps"][s_id] = bump_rec
                    save_paused_history(history)
                continue

            # Once an Ad Squad reaches 5 bumps (or is marked done after spending >= $1), NEVER bump it again on future days
            if bump_count >= 5 or bump_rec.get("done"):
                continue

            created_dt = parse_iso_utc(squad.get("created_at", ""))
            start_dt = parse_iso_utc(squad.get("start_time", ""))
            candidates = [dt for dt in (created_dt, start_dt) if dt is not None]
            effective_start = max(candidates) if candidates else now_utc

            # Do not start or continue bumping old Ad Squads from previous days (> 24 hours since launch)
            if (now_utc - effective_start) > timedelta(hours=24):
                continue

            bid_micro = squad.get("bid_micro")
            if not bid_micro or bid_micro <= 0:
                continue

            if bump_count == 0 or last_bump_dt is None:
                elapsed = now_utc - effective_start
            else:
                elapsed = now_utc - last_bump_dt

            if elapsed < timedelta(hours=2):
                continue

            old_bid = round(bid_micro / 1_000_000, 2)
            new_bid_micro = bid_micro + 100_000  # +$0.10
            new_bid = round(new_bid_micro / 1_000_000, 2)
            new_count = bump_count + 1
            hrs_waited = round(elapsed.total_seconds() / 3600, 1)

            if dry_run:
                print(
                    f"🔍 [مطابق لشرط زيادة البيد ({new_count}/5 مدى الحياة)] المجموعة: '{s_name}' (حملة: '{c_name}') "
                    f"| الصرف: ${today_spend} (< $1) خلال {hrs_waited} ساعة | البيد: ${old_bid} -> ${new_bid}"
                )
            else:
                squad["bid_micro"] = new_bid_micro
                api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                history["bid_bumps"][s_id] = {
                    "name": s_name,
                    "campaign": c_name,
                    "date": today_date_str,
                    "count": new_count,
                    "done": new_count >= 5,
                    "old_bid": old_bid,
                    "new_bid": new_bid,
                    "last_bump_at": now_utc.isoformat(),
                    "last_bump_local": now_str,
                }
                save_paused_history(history)
                print(
                    f"📈 تم رفع البيد (+$0.10) للمرة ({new_count}/5 مدى الحياة) للمجموعة: '{s_name}' (حملة: '{c_name}') "
                    f"| الصرف: ${today_spend} خلال {hrs_waited} ساعة | البيد: ${old_bid} -> ${new_bid}"
                )
                time.sleep(0.3)



def recheck_gulf_adsquads(token: str):
    history = load_paused_history()
    stats_map = {st["id"]: st for st in fetch_account_breakdown_stats(token, TARGET_ACCOUNT_ID, "adsquad")}
    for sid, info in list(history["adsquads"].items()):
        if sid == "036257b8-c553-44cc-97ef-021ed38bc1a8":  # Copy of 14-10
            continue
        s = api_get(f"/adsquads/{sid}", token)["adsquads"][0]["adsquad"]
        c = api_get(f"/campaigns/{s['campaign_id']}", token)["campaigns"][0]["campaign"]
        if is_gulf_034_campaign(c["name"], s["name"]):
            st = stats_map.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None})
            should_pause, _ = evaluate_adsquad_rule(s["name"], st, c["name"])
            if not should_pause:
                s["status"] = "ACTIVE"
                api_put(f"/campaigns/{s['campaign_id']}/adsquads", token, {"adsquads": [s]})
                del history["adsquads"][sid]
                save_paused_history(history)
                print(f"✅ أُعيد تشغيل مجموعة الخليج '{s['name']}' (في حملة '{c['name']}') لأن الصرف/CPA أقل من حد الخليج ($8 / $14)!")


if __name__ == "__main__":
    cfg = load_config()
    token = refresh_access_token(cfg)
    if "--recheck-gulf-squads" in sys.argv:
        recheck_gulf_adsquads(token)
    else:
        dry = "--dry-run" in sys.argv
        run_automation(token, TARGET_ACCOUNT_ID, dry_run=dry)
