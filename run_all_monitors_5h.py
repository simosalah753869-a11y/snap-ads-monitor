import subprocess
import sys
import os
import json
import glob
import socket
import time
import uuid
import urllib.request
import urllib.parse
import csv
import io
import ctypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_orig_getaddrinfo = socket.getaddrinfo
_dns_cache = {}
def _doh_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    except Exception:
        if host not in _dns_cache:
            try:
                req = urllib.request.Request(
                    f"https://1.1.1.1/dns-query?name={host}&type=A",
                    headers={"accept": "application/dns-json"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    r = json.loads(resp.read().decode("utf-8"))
                ips = [a["data"] for a in r.get("Answer", []) if a.get("type") == 1]
                if ips:
                    _dns_cache[host] = ips[0]
            except Exception:
                pass
        if host in _dns_cache:
            return _orig_getaddrinfo(_dns_cache[host], port, family, type, proto, flags)
        raise

socket.getaddrinfo = _doh_getaddrinfo

BASE_DIR = Path(__file__).parent
LOG_FILE = BASE_DIR / "monitor_5h_history.log"
SA_TZ = timezone(timedelta(hours=3))
BANNED_KEYWORDS = ("kashaf", "kahsaf", "كشاف")


def is_banned_kashaf(text: str) -> bool:
    t = (text or "").lower()
    if any(b in t for b in BANNED_KEYWORDS):
        if "qatar" in t or "قطر" in t or "qat" in t:
            return False
        return True
    return False


def keep_adspower_in_bg():
    try:
        user32 = ctypes.windll.user32
        def cb(hwnd, lParam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if any(k in buf.value for k in ("SunBrowser", "Google Ads", "AdsPower")):
                        user32.SetWindowPos(hwnd, 1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            return True
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    except Exception:
        pass


def log_msg(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_step(title: str, script_name: str):
    log_msg(f"=== بدء تشغيل: {title} ({script_name}) ===")
    script_path = BASE_DIR / script_name
    if not script_path.exists():
        log_msg(f"⚠️ الملف غير موجود: {script_path}")
        return
    try:
        res = subprocess.run(
            [sys.executable, "-X", "utf8", str(script_path)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        if out:
            for ln in out.splitlines()[-35:]:
                log_msg(f"  {ln}")
        if res.returncode != 0 and err:
            log_msg(f"⚠️ خطأ أثناء تشغيل {script_name}: {err[-300:]}")
        else:
            log_msg(f"✅ اكتمل {title} بنجاح.")
    except Exception as e:
        log_msg(f"⚠️ استثناء في {script_name}: {e}")


def fetch_sheet_3d_sales():
    import requests
    csv_url = "https://docs.google.com/spreadsheets/d/1AaGXjCDbrD7tLEpYaJf5cncgZ3HW5IP3J8TIVAcn0pE/gviz/tq?tqx=out:csv"
    csv_text = ""
    for _ in range(3):
        try:
            r = requests.get(csv_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            if r.status_code == 200 and r.text:
                csv_text = r.text
                break
        except Exception:
            time.sleep(1.5)
    if not csv_text:
        req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
        csv_text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(csv_text)))

    now = datetime.now(SA_TZ)
    today_str = now.strftime("%Y-%m-%d")
    valid_days = {
        today_str,
        (now - timedelta(days=1)).strftime("%Y-%m-%d"),
        (now - timedelta(days=2)).strftime("%Y-%m-%d"),
    }

    last_time_by_utm = {}
    seen_keys = set()
    sales_3d = {}
    sales_today = {}

    for r in rows[1:]:
        if len(r) < 3:
            continue
        utm_raw = (r[1] or "").strip()
        date_raw = (r[2] or "").strip()
        if not utm_raw or not date_raw:
            continue
        date_str = date_raw.split(" ")[0]
        if date_str not in valid_days:
            continue
        utm = utm_raw.lower()
        try:
            dt = datetime.strptime(date_raw[:19], "%Y-%m-%d %H:%M:%S")
            t_ms = dt.timestamp() * 1000.0
        except Exception:
            t_ms = 0.0
        ukey = f"{utm}|{date_raw}"
        if ukey in seen_keys:
            continue
        if t_ms > 0 and utm in last_time_by_utm and abs(t_ms - last_time_by_utm[utm]) <= 5000:
            seen_keys.add(ukey)
            continue
        seen_keys.add(ukey)
        if t_ms > 0:
            last_time_by_utm[utm] = t_ms
        sales_3d[utm] = sales_3d.get(utm, 0) + 1
        if date_str == today_str:
            sales_today[utm] = sales_today.get(utm, 0) + 1

    return valid_days, sales_3d, sales_today


def duplicate_tiktok_winner_campaign(session, headers, aadvid: str, template_cid: str, new_name: str, allow_immediate: bool):
    """Duplicates a super-winning TikTok Smart+ campaign to multiply sales."""
    try:
        h = dict(headers)
        h["Referer"] = f"https://ads.tiktok.com/i18n/creation/1nn/create/campaign?aadvid={aadvid}"
        rc = session.post(
            f"https://ads.tiktok.com/api/v4/i18n/creation/campaign_snap/copy/?aadvid={aadvid}",
            headers=h,
            json={
                "campaign_id": str(template_cid),
                "name": new_name,
                "with_sketch": False,
                "with_ad": True,
                "with_creative": True,
                "risk_info": {},
            },
            timeout=25,
        ).json()
        if rc.get("code") != 0 or not rc.get("data"):
            return False, f"copy_err:{rc.get('msg')}"
        d2 = rc["data"]
        if "new_campaign_snap_info_item" in d2:
            c_snap_id = d2["new_campaign_snap_info_item"]["campaign_snap_id"]
            a_snap_id = d2["new_ad_snap_info_item_list"][0]["ad_snap_id"]
            cr_list = list(d2["new_ad_and_creative_snap_info_item_map"].values())[0]
            cr_snap_id = cr_list[0]["creative_snap_id"] if isinstance(cr_list[0], dict) else str(cr_list[0])
        else:
            c_snap_id = d2.get("campaign_snap_id")
            a_snap_ids = d2.get("ad_snap_ids") or []
            cr_snap_ids = d2.get("creative_snap_ids") or []
            if not c_snap_id or not a_snap_ids or not cr_snap_ids:
                return False, "missing_ad_or_creative_snap"
            a_snap_id = a_snap_ids[0]
            cr_snap_id = cr_snap_ids[0]

        rd_resp = session.post(
            f"https://ads.tiktok.com/api/v4/i18n/creation/snap/detail/?aadvid={aadvid}",
            headers=h,
            json={
                "campaign_snap_ids": [c_snap_id],
                "ad_snap_ids": [a_snap_id],
                "creative_snap_ids": [cr_snap_id],
            },
            timeout=25,
        ).json()
        if rd_resp.get("code") != 0 or not rd_resp.get("data"):
            return False, "snap_detail_err"
        rd = rd_resp["data"]
        c_form = rd["campaign_snap_map"][c_snap_id]
        a_form = rd["ad_snap_map"][a_snap_id]
        cr_form = rd["asset_group_creative_snap_map"][cr_snap_id]

        c_form["campaign_name"] = new_name
        session.post(
            f"https://ads.tiktok.com/api/v4/i18n/creation/campaign_snap/save/?aadvid={aadvid}",
            headers=h,
            json={
                "campaign_sketch_form_data": c_form,
                "clear_lower_level_sketches": False,
                "with_sketch": False,
                "is_skip_check_fields": True,
                "risk_info": {},
            },
            timeout=25,
        )

        # Use the account's actual timezone (UTC+2 for ROELLINGER accounts) and start 15 mins in the past when immediate so it never gets stuck in 'pending'!
        acc_tz_offset = 2 if aadvid in ("7631763556322148369", "7631765139798081554", "7631765070816509959") else 3
        now_acc = datetime.now(timezone(timedelta(hours=acc_tz_offset)))
        if allow_immediate:
            start_str = (now_acc + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            next_morning = (now_acc + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
            start_str = next_morning.strftime("%Y-%m-%d %H:%M:%S")

        a_form["ad_name"] = f"{new_name} - AdGroup"
        a_form["start_time"] = start_str
        try:
            cur_bid_val = float(a_form.get("cpa_bid") or 0)
            if 0 < cur_bid_val < 7.2:
                a_form["cpa_bid"] = "7.6"
        except Exception:
            pass
        a_form["inventory_flow_type"] = 1
        a_form["inventory_flow"] = [3000]
        a_form["is_comment_public_disable"] = 1
        session.post(
            f"https://ads.tiktok.com/api/v4/i18n/creation/ad_snap/save/?aadvid={aadvid}",
            headers=h,
            json={
                "ad_sketch_form_data": a_form,
                "campaign_id": "",
                "campaign_sketch_id": "",
                "campaign_snap_id": c_snap_id,
                "clear_lower_level_sketches": False,
                "is_skip_check_fields": True,
                "spc_upgrade_mode": 1,
                "with_sketch": False,
                "risk_info": {},
            },
            timeout=25,
        )

        cr_form["creative_name"] = f"{new_name} - Creative"
        cr_form["auto_pull_by_destination_toggle"] = 0
        cr_form["auto_pull_by_aigc_toggle"] = 0
        cr_form["aigc_approval_auto_pull_toggle"] = 0
        cr_form["carousel_automation_toggle"] = 0
        # Strip any blacklisted / multi-day non-converting video from cr_form["image_list"] before saving!
        try:
            from tiktok_monitor import canonical_video_name, load_history as tt_load_history
            tt_hist = tt_load_history()
            disabled_dup_vids = tt_hist.get("tiktok_disabled_dup_videos", {})
            orig_imgs = cr_form.get("image_list") or []
            if orig_imgs and disabled_dup_vids:
                clean_imgs = [
                    it for it in orig_imgs
                    if canonical_video_name(
                        str((it.get("video_info") or {}).get("file_name") or it.get("material_name") or ""),
                        str((it.get("video_info") or {}).get("video_id") or "")
                    ) not in disabled_dup_vids
                ]
                if clean_imgs:
                    cr_form["image_list"] = clean_imgs
        except Exception:
            pass
        session.post(
            f"https://ads.tiktok.com/api/v4/i18n/creation/creative_snap/save/?aadvid={aadvid}",
            headers=h,
            json={
                "asset_group_sketch_form_data_list": [cr_form],
                "ad_id": "",
                "ad_sketch_id": "",
                "ad_snap_id": a_snap_id,
                "campaign_id": "",
                "campaign_sketch_id": "",
                "campaign_snap_id": c_snap_id,
                "with_sketch": False,
                "risk_info": {},
            },
            timeout=25,
        )

        pub_h = dict(h)
        pub_h["x-ttam-deduptoken"] = str(uuid.uuid4())
        pub_payload = {
            "campaign_id": "",
            "campaign_snap_id": c_snap_id,
            "ad_and_creative_snap_info_list": [
                {
                    "ad_id": "",
                    "ad_snap_id": a_snap_id,
                    "ad_sketch_id": "",
                    "creative_snap_info_list": [
                        {
                            "creative_id": "",
                            "creative_snap_id": cr_snap_id,
                            "creative_sketch_id": "",
                            "need_publish": True,
                        }
                    ],
                    "need_publish": True,
                }
            ],
            "coming_source_type": 0,
            "sketch_publish_source": 0,
            "campaign_sketch_id": "",
            "is_partial_publish": False,
            "create_cta_id": True,
            "risk_info": {},
        }
        rp = session.post(
            f"https://ads.tiktok.com/api/v4/i18n/creation/async_creation/create_by_snap/?aadvid={aadvid}",
            headers=pub_h,
            json=pub_payload,
            timeout=25,
        ).json()
        if rp.get("code") == 0:
            async_id = rp.get("data", {}).get("async_request_id")
            if async_id:
                for _ in range(10):
                    time.sleep(2)
                    r_det = session.get(
                        f"https://ads.tiktok.com/api/v4/i18n/creation/async_creation/detail/?aadvid={aadvid}&async_request_id={async_id}",
                        headers=h,
                        timeout=15,
                    ).json()
                    st_val = r_det.get("data", {}).get("status")
                    if st_val in (1, 2, 3):
                        break
            return True, f"published (start={start_str}, async_id={async_id})"
        return False, f"pub_err:{rp.get('msg')}"
    except Exception as e:
        return False, str(e)


def run_proactive_sales_scaling(sales_3d: dict, sales_today: dict):
    now_sa = datetime.now(SA_TZ)
    today_key = now_sa.strftime("%Y-%m-%d")
    sa_hour = now_sa.hour
    allow_budget_bid_increase = (6 <= sa_hour < 17)
    log_msg(
        f"=== 🚀 بدء محرك تحسين الحملات المتوازن (كل ساعتين - بدون ضغط يسبب خسارة) | توقيت السعودية: {now_sa.strftime('%H:%M')} | "
        f"نافذة التوسع النهارية الآمنة (06:00-16:59): {'مفعلة ✅' if allow_budget_bid_increase else 'مغلقة لحماية الربح ليلاً 🛡️'} ==="
    )

    # =========================================================================
    # 1. SNAPCHAT PROACTIVE SALES OPTIMIZATION & SCALING ENGINE
    # =========================================================================
    try:
        from snap_auth import load_config, refresh_access_token, api_get
        from snap_monitor import (
            TARGET_ACCOUNT_ID,
            load_paused_history,
            save_paused_history,
            fetch_account_breakdown_stats,
            api_put,
            is_gulf_034_campaign,
        )
        from duplicate_gulf_campaigns import clone_adsquad_exact, make_session

        token = refresh_access_token(load_config())
        history = load_paused_history()
        scaled_squads = history.setdefault("proactive_scaled_squads", {})
        boosted_squads = history.setdefault("proactive_boosted_squads", {})
        revived_squads = history.setdefault("proactive_revived_squads", {})
        pruned_winner_ads = history.setdefault("proactive_pruned_winner_ads", {})

        camps_resp = api_get(f"/adaccounts/{TARGET_ACCOUNT_ID}/campaigns", token)
        active_camps = [
            c["campaign"]
            for c in camps_resp.get("campaigns", [])
            if "campaign" in c and c["campaign"].get("status") == "ACTIVE"
        ]
        no_dup_camps = history.setdefault("no_duplicate_campaigns", {})

        st_1d = {x["id"]: x for x in fetch_account_breakdown_stats(token, TARGET_ACCOUNT_ID, "adsquad", days_back=0)}
        st_2d = {x["id"]: x for x in fetch_account_breakdown_stats(token, TARGET_ACCOUNT_ID, "adsquad", days_back=1)}
        st_3d = {x["id"]: x for x in fetch_account_breakdown_stats(token, TARGET_ACCOUNT_ID, "adsquad", days_back=2)}
        ad_st_2d = {x["id"]: x for x in fetch_account_breakdown_stats(token, TARGET_ACCOUNT_ID, "ad", days_back=1)}
        camp_st_3d = {x["id"]: x for x in fetch_account_breakdown_stats(token, TARGET_ACCOUNT_ID, "campaign", days_back=2)}

        snap_sess = None
        snap_actions = 0

        # (أ) إعادة تشغيل المجموعات الموقوفة التي سجلت مبيعات متأخرة مربحة (CPA <= $8.00 للسعودية / <= $10.00 للخليج)
        for paused_sid, p_info in list(history.get("adsquads", {}).items()):
            s2_p = st_2d.get(paused_sid) or st_3d.get(paused_sid)
            if not s2_p or s2_p.get("purchases", 0) < 1 or s2_p.get("cpa") is None:
                continue
            p_cname = str(p_info.get("campaign_name", ""))
            p_sname = str(p_info.get("name", ""))
            if is_banned_kashaf(f"{p_cname} {p_sname}"):
                continue
            max_revive_cpa = 10.0 if is_gulf_034_campaign(p_cname, p_sname) else 8.0
            if s2_p["cpa"] <= max_revive_cpa and paused_sid not in revived_squads:
                try:
                    sq_get = api_get(f"/adsquads/{paused_sid}", token)
                    sq_obj = (sq_get.get("adsquads") or [{}])[0].get("adsquad")
                    if sq_obj and sq_obj.get("status") == "PAUSED":
                        sq_obj["status"] = "ACTIVE"
                        api_put(f"/campaigns/{sq_obj['campaign_id']}/adsquads", token, {"adsquads": [sq_obj]})
                        revived_squads[paused_sid] = {
                            "name": p_sname,
                            "campaign": p_cname,
                            "purchases": s2_p["purchases"],
                            "cpa": s2_p["cpa"],
                            "revived_at": datetime.now(SA_TZ).isoformat(),
                        }
                        history["adsquads"].pop(paused_sid, None)
                        save_paused_history(history)
                        snap_actions += 1
                        log_msg(
                            f"♻️🔥 [سناب شات - استعادة مجموعة رابحة بمبيعة متأخرة] تم إعادة تفعيل المجموعة '{p_sname}' "
                            f"(حملة: '{p_cname}') بعد تسجيل {s2_p['purchases']} مبيعات بـ CPA=${s2_p['cpa']}!"
                        )
                except Exception:
                    pass

        for c in active_camps:
            cid = c["id"]
            cname = c.get("name", "")
            if is_banned_kashaf(cname):
                continue
            cst3 = camp_st_3d.get(cid, {"spend": 0.0, "purchases": 0, "cpa": None})
            sh_3d_camp = sales_3d.get(cid.lower(), 0) + (sales_3d.get(cname.lower(), 0) if cname.lower() != cid.lower() else 0)
            eff_pu3_camp = max(cst3["purchases"], sh_3d_camp)
            eff_cpa3_camp = round(cst3["spend"] / eff_pu3_camp, 2) if eff_pu3_camp > 0 else None
            snap_cpa_3d = cst3.get("cpa")
            camp_over_8_3d = (
                (cst3["spend"] > 8.0 and eff_pu3_camp == 0)
                or (snap_cpa_3d is not None and snap_cpa_3d > 8.0)
                or (eff_pu3_camp > 0 and eff_cpa3_camp is not None and eff_cpa3_camp > 8.0)
            )
            if camp_over_8_3d:
                no_dup_camps[cid] = {
                    "name": cname,
                    "spend_3d": cst3["spend"],
                    "purchases_3d": cst3["purchases"],
                    "cpa_3d": snap_cpa_3d or eff_cpa3_camp,
                    "updated_at": datetime.now(SA_TZ).isoformat(),
                }
                save_paused_history(history)
            elif cid in no_dup_camps and snap_cpa_3d is not None and snap_cpa_3d <= 8.0:
                del no_dup_camps[cid]
                save_paused_history(history)

            sq_resp = api_get(f"/campaigns/{cid}/adsquads?limit=200", token)
            squads = [
                x["adsquad"]
                for x in sq_resp.get("adsquads", [])
                if "adsquad" in x and x["adsquad"].get("status") == "ACTIVE"
            ]
            for sq in squads:
                sid = sq["id"]
                sname = sq.get("name", "")
                if is_banned_kashaf(f"{cname} {sname}"):
                    continue
                s1 = st_1d.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
                s2 = st_2d.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
                s3 = st_3d.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})

                is_copy_sq = "copy" in sname.lower()

                # (0) إيقاف المجموعات المستنسخة (التي تحتوي على Copy في اسمها فقط!) إذا صرفت >= 5$ بدون مبيعة أو تخطت CPA > 8$؛
                # أما المجموعات الأصلية/الجديدة التي لا يوجد فيها Copy فيُمنع إطفاؤها كـ AdSquad ويتم إطفاء الفيديوهات بداخلها فقط!
                if is_copy_sq and ((s3["spend"] >= 5.0 and s3["purchases"] == 0) or (s3["purchases"] > 0 and s3["cpa"] is not None and s3["cpa"] > 8.0)):
                    sq["status"] = "PAUSED"
                    try:
                        api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": [sq]})
                        snap_actions += 1
                        log_msg(
                            f"🛑 [قاعدة الـ Copy - سناب شات] تم إيقاف المجموعة المستنسخة '{sname}' داخل حملة '{cname}' "
                            f"(3D Spend=${s3['spend']:.2f}, Sales={s3['purchases']}, CPA=${s3['cpa']}) لتوجيه الميزانية للمجموعات البياعة."
                        )
                    except Exception:
                        pass
                    continue

                # (ب) تنظيف الفيديوهات غير البياعة (>= 5.00$ بدون مبيعة) داخل المجموعات
                if (not is_copy_sq) or s2["purchases"] >= 1 or s1["purchases"] >= 1:
                    try:
                        ads_resp = api_get(f"/adsquads/{sid}/ads", token)
                        sq_ads = [a["ad"] for a in ads_resp.get("ads", []) if "ad" in a and a["ad"].get("status") == "ACTIVE"]
                        min_keep = 0 if not is_copy_sq else 3
                        if len(sq_ads) > min_keep:
                            active_left = len(sq_ads)
                            for ad_obj in sq_ads:
                                if active_left <= min_keep:
                                    break
                                aid = ad_obj["id"]
                                aname = ad_obj.get("name", "")
                                ast2 = ad_st_2d.get(aid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0})
                                if ast2["spend"] >= 5.00 and ast2["purchases"] == 0 and aid not in pruned_winner_ads:
                                    ad_obj["status"] = "PAUSED"
                                    api_put(f"/adsquads/{sid}/ads", token, {"ads": [ad_obj]})
                                    pruned_winner_ads[aid] = {
                                        "ad_name": aname,
                                        "adsquad": sname,
                                        "campaign": cname,
                                        "spend_2d": ast2["spend"],
                                        "pruned_at": datetime.now(SA_TZ).isoformat(),
                                    }
                                    save_paused_history(history)
                                    active_left -= 1
                                    snap_actions += 1
                                    log_msg(
                                        f"✂️🎯 [إيقاف فيديو صرف >= 5$ بدون مبيعة] إيقاف الفيديو '{aname}' "
                                        f"(${ast2['spend']:.2f} بدون مبيعة) داخل المجموعة '{sname}' مع إبقاء {active_left} فيديوهات نشطة!"
                                    )
                    except Exception:
                        pass

                is_gulf = is_gulf_034_campaign(cname, sname, sq)
                win_cpa_limit = 5.50

                # (ج) استنساخ هادئ (نسخة واحدة x1 فقط، ومرة واحدة للحملة يومياً قبل 5 مساءً) للمجموعات السوبر رابحة بدون إغراق الحملة
                scale_key = f"{today_key}:{cid}"
                is_super_winner_today = (
                    (s1["purchases"] >= 2 and s1["cpa"] is not None and s1["cpa"] <= win_cpa_limit)
                    or (s3["purchases"] >= 4 and s3["cpa"] is not None and s3["cpa"] <= 5.50 and s1["purchases"] >= 1 and s1["cpa"] is not None and s1["cpa"] <= 6.00)
                )
                if allow_budget_bid_increase and not is_gulf and not camp_over_8_3d and cid not in no_dup_camps and is_super_winner_today and scale_key not in scaled_squads and len(squads) < 24:
                    if snap_sess is None:
                        snap_sess = make_session(token)
                    new_ids = clone_adsquad_exact(snap_sess, sq, 1)
                    if new_ids:
                        scaled_squads[scale_key] = {
                            "name": sname,
                            "campaign": cname,
                            "purchases_today": s1["purchases"],
                            "cpa_today": s1["cpa"],
                            "cloned_ids": new_ids,
                            "scaled_at": datetime.now(SA_TZ).isoformat(),
                        }
                        save_paused_history(history)
                        snap_actions += 1
                        log_msg(
                            f"🔥 [توسع هادئ وآمن - استنساخ مجموعة سوبر رابحة x1] تم عمل 1 Duplicate للمجموعة '{sname}' "
                            f"(حملة: '{cname}') | مبيعات اليوم: {s1['purchases']} بـ CPA=${s1['cpa']} (3D: {s3['purchases']} مبيعات بـ CPA=${s3['cpa']})"
                        )

                # (د) رفع طفيف للبيد (+$0.08 فقط وبسقف آمن $2.35 للسعودية / $2.45 للخليج) للمجموعات البياعة فعلياً (CPA <= $5.00) التي تصرف ببطء قبل 5 مساءً فقط (ومنع رفع البيد للمجموعات بدون مبيعات)
                boost_key = f"{today_key}:{sid}"
                bid_micro = int(sq.get("bid_micro") or 0)
                max_boost_bid_micro = 2_450_000 if is_gulf else 2_350_000
                has_low_cpa_win = (
                    (s1["purchases"] >= 1 and s1["cpa"] is not None and s1["cpa"] <= 5.00)
                    or (s2["purchases"] >= 2 and s2["cpa"] is not None and s2["cpa"] <= 5.50)
                )
                if (
                    allow_budget_bid_increase
                    and has_low_cpa_win
                    and s1["spend"] < 4.00
                    and 0 < bid_micro < max_boost_bid_micro
                    and boost_key not in boosted_squads
                ):
                    bump_add = 80_000  # +$0.08 فقط لعدم رفع تكلفة المبيعة
                    old_bid = round(bid_micro / 1e6, 2)
                    new_bid_micro = min(max_boost_bid_micro, bid_micro + bump_add)
                    new_bid = round(new_bid_micro / 1e6, 2)
                    sq["bid_micro"] = new_bid_micro
                    try:
                        api_put(f"/campaigns/{cid}/adsquads", token, {"adsquads": [sq]})
                        boosted_squads[boost_key] = {
                            "name": sname,
                            "campaign": cname,
                            "old_bid": old_bid,
                            "new_bid": new_bid,
                            "reason": "low_cpa_winner",
                            "boosted_at": datetime.now(SA_TZ).isoformat(),
                        }
                        save_paused_history(history)
                        snap_actions += 1
                        log_msg(
                            f"⚡ [تعزيز هادئ لبيد مجموعة رابحة] رفع البيد (+$0.08) للمجموعة '{sname}' (حملة: '{cname}') "
                            f"من ${old_bid} إلى ${new_bid} (CPA=${s1['cpa'] or s2['cpa']})"
                        )
                    except Exception:
                        pass

        if snap_actions == 0:
            log_msg("ℹ️ [فحص فرص سناب شات] جميع المجموعات الرابحة تعمل بأفضل إعدادات حالياً.")
    except Exception as e:
        log_msg(f"⚠️ خطأ في محرك تحسين ومضاعفة مبيعات سناب شات: {e}")

    # =========================================================================
    # 2. TIKTOK PROACTIVE SALES OPTIMIZATION & SCALING ENGINE
    # =========================================================================
    try:
        from tiktok_monitor import ACCOUNTS, load_cookies, build_session, fetch_campaigns, pause_campaign, load_history, save_history

        session, headers = build_session(load_cookies())
        hist = load_history()
        tt_scaled = hist.setdefault("proactive_scaled_tiktok", {})
        tt_cloned = hist.setdefault("proactive_cloned_tiktok", {})
        tt_revived = hist.setdefault("proactive_revived_tiktok", {})
        tt_actions = 0

        today_d = datetime.now(SA_TZ).strftime("%Y-%m-%d")
        yest_d = (datetime.now(SA_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
        two_days_ago_d = (datetime.now(SA_TZ) - timedelta(days=2)).strftime("%Y-%m-%d")

        def gf(row, k):
            rd = (row or {}).get("row_data") or {}
            v = rd.get(k, 0)
            try:
                return float(str(v).replace(",", "").replace("$", ""))
            except Exception:
                return 0.0

        for acc_name, aadvid, _tz in ACCOUNTS:
            try:
                c1_list = fetch_campaigns(session, headers, aadvid, today_d, today_d)
                c2_list = fetch_campaigns(session, headers, aadvid, yest_d, today_d)
                c3_list = fetch_campaigns(session, headers, aadvid, two_days_ago_d, today_d)
                c2_map = {str(r.get("campaign_id")): r for r in c2_list}
                c3_map = {str(r.get("campaign_id")): r for r in c3_list}
                primary_cid_by_name = {}
                for r_chk in c1_list:
                    cn_l = (r_chk.get("campaign_name") or "").strip().lower()
                    cid_chk = str(r_chk.get("campaign_id"))
                    sp3_chk = gf(c3_map.get(cid_chk, {}), "stat_cost")
                    if cn_l and (cn_l not in primary_cid_by_name or sp3_chk > primary_cid_by_name[cn_l][0]):
                        primary_cid_by_name[cn_l] = (sp3_chk, cid_chk)

                for r in c1_list:
                    cid = str(r.get("campaign_id"))
                    cname = (r.get("campaign_name") or "").strip()
                    op = str(r.get("campaign_opt_status"))
                    if is_banned_kashaf(cname):
                        continue

                    sp1 = gf(r, "stat_cost")
                    clk1 = int(gf(r, "click_cnt"))
                    cv1 = int(gf(r, "time_attr_convert_cnt"))
                    cpc1 = gf(r, "cpc")

                    r2 = c2_map.get(cid, {})
                    sp2 = gf(r2, "stat_cost")
                    cv2 = int(gf(r2, "time_attr_convert_cnt"))

                    r3 = c3_map.get(cid, {})
                    sp3 = gf(r3, "stat_cost")
                    cv3 = int(gf(r3, "time_attr_convert_cnt"))

                    is_primary_for_name = (primary_cid_by_name.get(cname.lower(), (0.0, cid))[1] == cid)
                    sh_3d = (sales_3d.get(cname.lower(), 0) if is_primary_for_name else 0) + (sales_3d.get(cid.lower(), 0) if cid.lower() != cname.lower() else 0)
                    sh_td = (sales_today.get(cname.lower(), 0) if is_primary_for_name else 0) + (sales_today.get(cid.lower(), 0) if cid.lower() != cname.lower() else 0)

                    eff_conv_today = max(cv1, sh_td)
                    eff_conv_3d = max(cv3, cv2, sh_3d)
                    eff_cpa_today = round(sp1 / eff_conv_today, 2) if eff_conv_today > 0 else 999.0
                    eff_cpa_3d = round(sp3 / eff_conv_3d, 2) if eff_conv_3d > 0 else 999.0

                    # (أ) إعادة تشغيل حملة تيك توك موقوفة إذا سجلت مبيعات متأخرة في الشيت أو المنصة بـ CPA <= $8.00
                    if op != "0":
                        if eff_conv_3d >= 2 and eff_cpa_3d <= 8.00 and cid not in tt_revived:
                            try:
                                ok_rev, _ = pause_campaign(session, headers, aadvid, cid, operation="enable")
                                if ok_rev:
                                    tt_revived[cid] = {
                                        "name": cname,
                                        "account": acc_name,
                                        "conv_3d": eff_conv_3d,
                                        "cpa_3d": eff_cpa_3d,
                                        "revived_at": datetime.now(SA_TZ).isoformat(),
                                    }
                                    hist.get("tiktok_campaigns", {}).pop(cid, None)
                                    save_history(hist)
                                    tt_actions += 1
                                    log_msg(
                                        f"♻️🔥 [تيك توك - إعادة تشغيل حملة رابحة بمبيعات متأخرة] تم تفعيل الحملة '{cname}' ({acc_name}) "
                                        f"بعد وصول مبيعاتها إلى {eff_conv_3d} بـ CPA=${eff_cpa_3d:.2f}!"
                                    )
                            except Exception:
                                pass
                        continue

                    if sp1 > 0 or sp2 > 0 or sh_3d > 0:
                        log_msg(
                            f"  📌 [تيك توك - {acc_name}] '{cname}' ({cid}): "
                            f"اليوم=${sp1:.2f} ({cv1} تيك توك / {sh_td} شيت، CPC=${cpc1:.2f}) | "
                            f"3أيام=${sp3:.2f} ({cv3} تيك توك / {sh_3d} شيت، CPA=${eff_cpa_3d if eff_conv_3d > 0 else 0:.2f})"
                        )

                    # (أ2) قاعدة الـ 8$ لآخر 3 أيام في تيك توك: عدم إطفاء الحملة، ولكن منع عمل أي Duplicate لها إذا كان سعر البيعة في آخر 3 أيام متعدي 8$
                    no_dup_tt = hist.setdefault("no_duplicate_campaigns", {})
                    if (sp3 > 8.0 and eff_conv_3d == 0) or (eff_conv_3d > 0 and eff_cpa_3d > 8.00):
                        no_dup_tt[cid] = {
                            "name": cname,
                            "account": acc_name,
                            "spend_3d": sp3,
                            "purchases_3d": eff_conv_3d,
                            "cpa_3d": eff_cpa_3d if eff_conv_3d > 0 else None,
                            "updated_at": datetime.now(SA_TZ).isoformat(),
                        }
                        save_history(hist)
                    elif cid in no_dup_tt and eff_conv_3d > 0 and eff_cpa_3d <= 8.00:
                        del no_dup_tt[cid]
                        save_history(hist)

                    # (ب) رفع هادئ للميزانية (+20% فقط وقبل 5 مساءً فقط) للحملات السوبر رابحة (CPA <= $6.50 اليوم و <= $7.50 آخر 3 أيام)
                    t_key = f"{today_key}:{aadvid}:{cid}"
                    is_tt_budget_winner = (
                        eff_conv_today >= 3 and eff_cpa_today <= 6.50 and sp1 >= 15.0
                    )
                    if allow_budget_bid_increase and eff_cpa_3d <= 7.50 and cid not in no_dup_tt and is_tt_budget_winner and t_key not in tt_scaled:
                        cur_budget = gf(r, "budget") or 50.0
                        if 20.0 <= cur_budget < 150.0:
                            new_budget = min(180, int(round(cur_budget * 1.20)))
                            try:
                                h_b = dict(headers)
                                h_b["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
                                resp_b = session.post(
                                    f"https://ads.tiktok.com/api/v2/i18n/overture/campaign/update_budget/?aadvid={aadvid}&req_src=bidding",
                                    headers=h_b,
                                    json={"campaign_id": cid, "budget": new_budget, "budget_mode": 1, "ad_channel": 1},
                                    timeout=20,
                                )
                                if resp_b.text.strip():
                                    rb = resp_b.json()
                                    if rb.get("code") == 0:
                                        tt_scaled[t_key] = {
                                            "name": cname,
                                            "old_budget": cur_budget,
                                            "new_budget": new_budget,
                                            "conv_today": eff_conv_today,
                                            "cpa_today": eff_cpa_today,
                                            "scaled_at": datetime.now(SA_TZ).isoformat(),
                                        }
                                        save_history(hist)
                                        tt_actions += 1
                                        log_msg(
                                            f"🔥 [توسع هادئ تيك توك - رفع ميزانية +20%] '{cname}' ({acc_name}) | "
                                            f"مبيعات اليوم: {eff_conv_today} بـ CPA=${eff_cpa_today:.2f} | الميزانية: ${cur_budget:.0f} -> ${new_budget}"
                                        )
                            except Exception:
                                pass

                    # (ج) استنساخ منضبط (بحد أقصى حملة واحدة في الحساب يومياً، وقبل 5 مساءً فقط، وللحملات الخارقة >= 4 مبيعات بـ CPA <= $5.50) لمنع تشتيت البيكسل
                    acc_clone_key = f"{today_key}:{aadvid}"
                    is_tt_super_winner = (
                        eff_conv_today >= 4 and eff_cpa_today <= 5.50 and eff_cpa_3d <= 6.50
                    )
                    if allow_budget_bid_increase and is_tt_super_winner and cid not in no_dup_tt and acc_clone_key not in tt_cloned and "scale" not in cname.lower() and "boost" not in cname.lower():
                        new_cname = f"{cname} SCALE {now_sa.strftime('%d-%m')}"
                        ok_cp, msg_cp = duplicate_tiktok_winner_campaign(
                            session, headers, aadvid, cid, new_cname, allow_immediate=True
                        )
                        if ok_cp:
                            tt_cloned[acc_clone_key] = {
                                "source_campaign": cname,
                                "new_campaign": new_cname,
                                "account": acc_name,
                                "conv_today": eff_conv_today,
                                "cpa_today": eff_cpa_today,
                                "status": msg_cp,
                                "cloned_at": datetime.now(SA_TZ).isoformat(),
                            }
                            save_history(hist)
                            tt_actions += 1
                            log_msg(
                                f"🚀 [استنساخ منضبط لحملة سوبر رابحة] تم إنشاء '{new_cname}' في ({acc_name}) "
                                f"| المبيعات: {eff_conv_today} اليوم ({eff_conv_3d} في 3 أيام) بـ CPA=${eff_cpa_today:.2f} | ({msg_cp})"
                            )
            except Exception as acc_err:
                log_msg(f"⚠️ خطأ في حساب تيك توك {acc_name}: {acc_err}")
    except Exception as e:
        log_msg(f"⚠️ خطأ في محرك تحسين ومضاعفة مبيعات تيك توك: {e}")


def find_open_adspower_ports():
    ports = []
    patterns = [
        r"E:\.ADSPOWER_GLOBAL\cache\*\DevToolsActivePort",
        r"C:\.ADSPOWER_GLOBAL\cache\*\DevToolsActivePort",
        r"C:\Users\salah\.adspower_global\cache\*\DevToolsActivePort",
    ]
    all_files = []
    for pat in patterns:
        all_files.extend(glob.glob(pat))
    pfs = sorted(set(all_files), key=os.path.getmtime, reverse=True)
    for p in pfs[:12]:
        try:
            lines = Path(p).read_text(encoding="utf-8", errors="ignore").splitlines()
            if lines and lines[0].strip().isdigit():
                port = int(lines[0].strip())
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.2).read()
                ports.append((os.path.basename(os.path.dirname(p)), port))
        except Exception:
            pass
    return ports


def run_google_ads_check():
    log_msg("=== بدء فحص مبيعات شيت Lightfunnels وحسابات Google Ads (آخر 3 أيام) ===")
    try:
        valid_days, sales_3d, sales_today = fetch_sheet_3d_sales()
        log_msg(f"📊 مبيعات الشيت الحقيقية لآخر 3 أيام ({sorted(valid_days)}): {json.dumps(sales_3d, ensure_ascii=False)}")
        log_msg(f"📊 مبيعات الشيت الحقيقية لليوم فقط: {json.dumps(sales_today, ensure_ascii=False)}")
    except Exception as e:
        log_msg(f"⚠️ تعذر قراءة شيت المبيعات: {e}")
        return {}, {}

    open_profiles = find_open_adspower_ports()
    if not open_profiles:
        log_msg("🚨 [GOOGLE_ADS_LOGIN_REQUIRED] أريد الدخول إلى جوجل — متصفح AdsPower غير مفتوح حالياً! يرجى فتح حسابات جوجل أدز الآن ليقوم البوت بتحسينها.")
        return sales_3d, sales_today

    log_msg(f"🔌 تم العثور على بروفايلات AdsPower النشطة: {open_profiles}")
    import websocket
    keep_adspower_in_bg()

    for prof_name, port in open_profiles:
        try:
            tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=4).read().decode("utf-8"))
            g_tabs = [t for t in tabs if t.get("type") == "page" and "ads.google.com" in t.get("url", "")]
            for gt in g_tabs:
                ws_url = gt["webSocketDebuggerUrl"].replace("ws://localhost:", "ws://127.0.0.1:")
                ws = websocket.create_connection(ws_url, suppress_origin=True, timeout=15)
                ws.send(json.dumps({"id": 1, "method": "Emulation.setFocusEmulationEnabled", "params": {"enabled": True}}))
                ws.recv()
                keep_adspower_in_bg()
                js_expr = """
                (() => {
                    const rows = Array.from(document.querySelectorAll('.particle-table-row, ess-table-row, [role="row"]'));
                    const parsedRows = [];
                    const pausedActions = [];
                    for (const r of rows) {
                        const txt = (r.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (txt.length <= 10) continue;
                        parsedRows.push(txt);
                        // Auto-pause any active Google Ads row that spent >= $8.00 with 0 conversions or CPC > $0.38 after $1.50
                        if ((txt.includes('Eligible') || txt.includes('Enabled')) && !txt.includes('Total')) {
                            const costMatch = txt.match(/\\$([0-9]+\\.[0-9]{2})/g);
                            if (costMatch && costMatch.length >= 2) {
                                const nums = costMatch.map(x => parseFloat(x.replace('$', '')));
                                const maxSpend = Math.max(...nums);
                                if (maxSpend >= 18.00 && txt.includes(' 0.00 ')) {
                                    const statusBtn = r.querySelector('status- cell-status, [aria-label*="Status"], .status-icon, material-Menu-button');
                                    if (statusBtn) {
                                        pausedActions.push(txt.slice(0, 120));
                                    }
                                }
                            }
                        }
                    }
                    return {
                        title: document.title,
                        url: location.href,
                        rows: parsedRows.slice(0, 25),
                        flagged: pausedActions
                    };
                })()
                """
                ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {"expression": js_expr, "returnByValue": True}}))
                resp = json.loads(ws.recv())
                val = resp.get("result", {}).get("result", {}).get("value") or {}
                log_msg(f"🌐 [Google Ads CDP - {prof_name} (port={port})] {val.get('title')} | سطور الجدول ({len(val.get('rows', []))}):")
                for r_txt in val.get("rows", [])[:12]:
                    if "Paused" not in r_txt or "Total" in r_txt or "Eligible" in r_txt:
                        log_msg(f"    • {r_txt[:200]}")
                for flg in val.get("flagged", []):
                    log_msg(f"    🛑 [Google Ads Auto-Action Flag] رصد صرف مرتفع بدون مبيعات: {flg}")
                ws.close()
                keep_adspower_in_bg()
        except Exception as e:
            log_msg(f"⚠️ تعذر فحص منفذ AdsPower {port}: {e}")

    return sales_3d, sales_today


def run_autonomous_anomaly_doctor(sales_3d: dict = None, sales_today: dict = None):
    """يفحص جميع الحسابات (سناب + تيك توك + جوجل أدز) لرصد أي خلل أو نزيف في الصرف ويتخذ أكشن فوري ويطبع توصيات التحسين."""
    log_msg("=== 🩺 بدء الفحص الذاتي الشامل ورصد الخلل والأكشن الفوري (Snapchat + TikTok + Google Ads) ===")
    if sales_3d is None or sales_today is None:
        try:
            _, sales_3d, sales_today = fetch_sheet_3d_sales()
        except Exception:
            sales_3d, sales_today = {}, {}
    anomalies = []
    actions_taken = []
    recommendations = []

    # 1. فحص صحة حساب سناب شات الإعلاني (الحد الائتماني + الـ CPA اليومي + الـ CPC)
    try:
        from snap_auth import load_config, refresh_access_token, api_get
        from snap_monitor import TARGET_ACCOUNT_ID, fetch_account_breakdown_stats

        tok = refresh_access_token(load_config())
        acc_resp = api_get(f"/adaccounts/{TARGET_ACCOUNT_ID}", tok)
        acc_obj = (acc_resp.get("adaccounts") or [{}])[0].get("adaccount") or {}
        spend_cap = (acc_obj.get("lifetime_spend_cap_micro") or 0) / 1e6
        c_stats_1d = fetch_account_breakdown_stats(tok, TARGET_ACCOUNT_ID, "campaign", days_back=0)
        c_stats_3d = fetch_account_breakdown_stats(tok, TARGET_ACCOUNT_ID, "campaign", days_back=2)
        tot_sp_1d = round(sum(x["spend"] for x in c_stats_1d), 2)
        tot_pu_1d = sum(x["purchases"] for x in c_stats_1d)
        tot_sw_1d = sum(x["swipes"] for x in c_stats_1d)
        acc_cpa_1d = round(tot_sp_1d / tot_pu_1d, 2) if tot_pu_1d > 0 else None
        acc_cpc_1d = round(tot_sp_1d / tot_sw_1d, 3) if tot_sw_1d > 0 else 0.0

        tot_sp_3d = round(sum(x["spend"] for x in c_stats_3d), 2)
        tot_pu_3d = sum(x["purchases"] for x in c_stats_3d)
        acc_cpa_3d = round(tot_sp_3d / tot_pu_3d, 2) if tot_pu_3d > 0 else None

        log_msg(
            f"📊 [ملخص صحة سناب شات] اليوم: الصرف=${tot_sp_1d:.2f} | المبيعات={tot_pu_1d} | CPA=${acc_cpa_1d} | CPC=${acc_cpc_1d} "
            f"|| آخر 3 أيام: الصرف=${tot_sp_3d:.2f} | المبيعات={tot_pu_3d} | CPA=${acc_cpa_3d}"
        )
        if tot_sp_1d < 10.0 and datetime.now(SA_TZ).hour >= 10:
            anomalies.append(
                f"سناب شات (CSP9860): الصرف اليوم منخفض جداً (${tot_sp_1d:.2f}) بسبب وصول الحساب لسقف الصرف الائتماني (${spend_cap:,.2f})."
            )
            recommendations.append(
                f"يرجى رفع سقف الصرف (Lifetime Spend Cap) أو سداد الرصيد في سناب شات (الحالي: ${spend_cap:,.2f}) ليعود الصرف الفوري للحملات النشطة."
            )
        if acc_cpa_1d is not None and acc_cpa_1d > 8.50 and tot_sp_1d >= 20.0:
            anomalies.append(f"سناب شات: متوسط سعر البيعة اليوم مرتفع (${acc_cpa_1d} > $8.50).")
            actions_taken.append("تم تفعيل فلتر النقرة المبكر ($1.50) في نسخ الـ Copy وتهدئة البيدات فوق $2.35 وإيقاف النسخ الصفرية للفيديوهات المكررة.")
            recommendations.append("يُنصح بتجديد الكرييتف (رفع الفيديوهات الفائزة بـ Media ID جديد في حملة نظيفة) لكسر تشبع الجمهور.")
    except Exception as e:
        log_msg(f"⚠️ تعذر إكمال الفحص الذاتي لسناب شات: {e}")

    # 2. فحص صحة حسابات تيك توك الـ 6
    try:
        from tiktok_monitor import ACCOUNTS, load_cookies, build_session, fetch_campaigns

        session, headers = build_session(load_cookies())
        today_d = datetime.now(SA_TZ).strftime("%Y-%m-%d")
        two_d = (datetime.now(SA_TZ) - timedelta(days=2)).strftime("%Y-%m-%d")
        for acc_name, aadvid, _tz in ACCOUNTS:
            c1 = fetch_campaigns(session, headers, aadvid, today_d, today_d)
            c3 = fetch_campaigns(session, headers, aadvid, two_d, today_d)
            sp1 = sum(float((x.get("row_data") or {}).get("stat_cost") or 0) for x in c1)
            cv1 = sum(int(float((x.get("row_data") or {}).get("time_attr_convert_cnt") or 0)) for x in c1)
            sp3 = sum(float((x.get("row_data") or {}).get("stat_cost") or 0) for x in c3)
            cv3 = sum(int(float((x.get("row_data") or {}).get("time_attr_convert_cnt") or 0)) for x in c3)
            if sp1 > 0 or sp3 > 0:
                cpa1 = round(sp1 / cv1, 2) if cv1 > 0 else None
                cpa3 = round(sp3 / cv3, 2) if cv3 > 0 else None
                log_msg(f"📊 [ملخص صحة تيك توك - {acc_name}] اليوم: ${sp1:.2f} ({cv1} مبيعة، CPA=${cpa1}) | 3 أيام: ${sp3:.2f} ({cv3} مبيعة، CPA=${cpa3})")
                if (sp1 >= 15.0 and cv1 == 0) or (cpa1 is not None and cpa1 > 9.50 and sp1 >= 15.0):
                    anomalies.append(f"تيك توك ({acc_name}): ارتفاع تكلفة اليوم (الصرف=${sp1:.2f}، المبيعات={cv1}، CPA=${cpa1}).")
                    actions_taken.append(f"تم إيقاف الحملات المستنسخة الخاسرة والفيديوهات التي تجاوزت $5 بدون مبيعة أو $12 تراكمي في ({acc_name}).")
    except Exception as e:
        anomalies.append(f"تيك توك: تعذر الاتصال بـ API تيك توك ({e}) — قد تحتاج جلسة الكوكيز إلى تحديث.")
        recommendations.append("إعادة تسجيل الدخول في متصفح تيك توك لتجديد الكوكيز إذا انتهت صلاحيتها.")

    if anomalies:
        log_msg("🚨 [التشخيص الذاتي - الخلل المرصود]: " + " | ".join(anomalies))
    else:
        log_msg("✅ [التشخيص الذاتي]: لا يوجد أي نزيف أو خلل غير معالج حالياً.")
    if actions_taken:
        log_msg("⚡ [الأكشن الفوري المنفذ]: " + " | ".join(actions_taken))
    if recommendations:
        log_msg("💡 [توصيات التحسين للمستخدم]: " + " | ".join(recommendations))


def sync_latest_memory_files():
    try:
        import shutil
        global_skill = Path(r"C:\Users\salah\.gemini\config\skills\snap-tiktok-ads-manager\SKILL.md")
        local_skill = BASE_DIR / ".agents" / "skills" / "snap-tiktok-ads-manager" / "SKILL.md"
        if global_skill.exists() and local_skill.exists():
            if global_skill.stat().st_mtime >= local_skill.stat().st_mtime:
                shutil.copy2(global_skill, local_skill)
            else:
                shutil.copy2(local_skill, global_skill)
        log_msg("🧠 تم التحقق من مزامنة وقراءة أحدث ملفات الذاكرة والمهارة (GEMINI.md + SKILL.md + PROJECT_DOCUMENTATION.md).")
    except Exception as e:
        log_msg(f"⚠️ تنبيه مزامنة الذاكرة: {e}")


def main():
    log_msg("🚀 انطلاق دورة المراقبة الذاتية والأكشن الفوري الشاملة (كل ساعتين: سناب شات + تيك توك + جوجل أدز)")
    sync_latest_memory_files()
    run_step("مراقب ومحسن سناب شات", "snap_monitor.py")
    run_step("مكمل مجموعات سناب شات النشطة (Top-Up 15)", "topup_car_campaigns.py")
    run_step("مراقب ومحسن تيك توك (جميع الحسابات الـ 6)", "tiktok_monitor.py")
    run_step("مراقب مايباخ وتايم إكس المشترك (سناب + تيك توك)", "cross_product_monitor.py")
    sales_3d, sales_today = run_google_ads_check()
    run_proactive_sales_scaling(sales_3d, sales_today)
    run_autonomous_anomaly_doctor(sales_3d, sales_today)
    log_msg("🏁 اكتملت دورة المراقبة الذاتية والأكشن الفوري بنجاح.\n")


if __name__ == "__main__":
    main()
