"""
حارس تثبيت بيدات أدست الكوبي ($1.90) الصارم في سناب شات بالتوازي السريع المباشر
===================================================================================================
يفحص بالتوازي كل 15 ثانية في الخلفية التامة جميع الحملات النشطة في الحسابين (CSP9860 و CSP9860 4):
1. بيدات جميع أدست الكوبي في السعودية تثبّت على $1.90 (1,900,000 micro) بأمر المستخدم الصارم (2026-11-03).
2. حملات التيست المخصصة (تيست - باور بانك / تيست - وسادة السيارة @ $3.00) وحملات الخليج تبقى مستثناة ببيداتها المخصصة.
"""
import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
import snap_monitor as sm
from snap_auth import API_BASE

BASE_DIR = Path(__file__).parent
LOCK_FILE = BASE_DIR / "snap_low_bid_lock_daemon.lock"
LOG_FILE = BASE_DIR / "snap_low_bid_lock_daemon.log"
MAX_BID = 1_950_000

DIRECT_CAMPAIGNS = [
    # CSP9860 (All Saudi Copy AdSquads unified to $1.90 per 2026-11-03 User Order; 'تيست' campaigns excluded @ $3.00)
    ("CSP9860", "35597da7-a911-48f9-9567-7d81bfdebe00", "باور بانك وينر - 30 أدست - بيد 1.6 / 1.65 / 1.67 30-10", 1_900_000),
    ("CSP9860", "6e187c03-020b-439c-905e-f3d513cd80d6", "وسادة السيارة وينر - 30 أدست - بيد 1.6 / 1.65 / 1.67 30-10", 1_900_000),
    ("CSP9860", "b4ec4259-1868-40f5-ba33-08a2dd0fd578", "وسادة السيارة وينر - 30 أدست - بيد 1.6 30-10", 1_900_000),
    ("CSP9860", "11fa8aef-3a3c-418d-b5c4-8dc7b3ec7ab3", "باور بانك وينر 2 - 30 أدست 30-10", 1_900_000),
    ("CSP9860", "2693cf34-1697-4bd9-b3eb-07ba7a64f6f1", "وسادة السيارة وينر 2 - 10 أدست - بيد 1.6", 1_900_000),
    ("CSP9860", "ee9d1a46-e08e-4a73-a910-f30941356671", "وسادة السيارة وينر - 10 أدست - بيد 1.6", 1_900_000),
    ("CSP9860", "c89e6059-b826-4cec-8067-71ba2bd45699", "باور بانك وينر - 10 أدست - بيد 1.6", 1_900_000),
    ("CSP9860", "a9dcd138-a822-4664-bf0a-c36d02233956", "باور بانك وينر - 10 أدست - بيد 1.6", 1_900_000),
    ("CSP9860", "2e87b13a-90ba-4bee-b553-4ebc9c82186e", "وسادة السيارة سكيلينج - 10 أدست - بيد 1.6", 1_900_000),
    ("CSP9860", "ac64cb95-72d4-4043-905a-69fce2968a0a", "باور بانك وينر - 10 أدست - بيد 1.6", 1_900_000),
    # CSP9860 4 (All Saudi Copy AdSquads unified to $1.90 per 2026-11-03 User Order; 'تيست' campaigns excluded @ $3.00)
    ("CSP9860 4", "7fa61e5d-dd41-4ee5-af26-e5c0f773bdef", "باور بانك وينر - 30 أدست - بيد 1.7 / 1.6 30-10", 1_900_000),
    ("CSP9860 4", "7bffcb77-f2ae-427e-883a-f4e5f249cae9", "باور بانك وينر - 30 أدست - بيد 1.7 / 1.6 30-10", 1_900_000),
    ("CSP9860 4", "483f7e57-8a8e-4edb-8b74-7c67eb76419b", "باور بانك وينر 2 - 30 أدست 30-10", 1_900_000),
    ("CSP9860 4", "e21421ad-72a1-427e-987d-e6cc49384a31", "باور بانك وينر - 10 أدست - بيد 1.6", 1_900_000),
    ("CSP9860 4", "0bacf974-1fc2-4ba4-ac20-42b974911064", "وسادة السيارة وينر - 10 أدست - بيد 1.6", 1_900_000),
    ("CSP9860 4", "c2b612d5-acfb-4935-8a5f-1cb090320542", "وسادة السيارة وينر - 10 أدست - بيد 1.6", 1_900_000),
    ("CSP9860 4", "22d12681-930c-44a8-91e3-58f112789375", "باور بانك وينر - 10 أدست - بيد 1.6", 1_900_000),
    ("CSP9860 4", "9ff50b24-d5c3-48ba-b74e-7b55e8ffca52", "وسادة السيارة وينر - 10 أدست - بيد 1.6", 1_900_000),
    ("CSP9860 4", "828f2e42-96c5-4813-a780-bbc5d976075f", "وسادة السيارة وينر 2 - 10 أدست - بيد 1.6", 1_900_000),
    # 2026-11-02 Replacement Campaigns ($1.90 Max Bid)
    ("CSP9860", "bfe9ec1d-d6c6-416d-a3f0-46c50c2b7a76", "باور بانك وينر بديلة - ماكس بيد 1.9 - 02-11", 1_900_000),
    ("CSP9860", "0ea95b38-9d67-4e15-973d-86adf3d4104a", "وسادة السيارة وينر بديلة - ماكس بيد 1.9 - 02-11", 1_900_000),
    ("CSP9860 4", "51945aea-b2d6-4742-815c-feb85620d8ff", "وسادة السيارة وينر منزلك بديلة - ماكس بيد 1.9 - 02-11", 1_900_000),
    ("CSP9860 4", "a4518953-cef3-4b77-9af1-20f6724623f8", "باور بانك وينر منزلك بديلة - ماكس بيد 1.9 - 02-11", 1_900_000),
]

_tls = threading.local()
_last_tt_budget_check = 0.0
_PENDING_TT_50_RESET_CIDS = [
    ("7631763556322148369", "1880695954907169"),
    ("7631763556322148369", "1880695903993266"),
    ("7631763556322148369", "1880695916990625"),
    ("7631765070816509959", "1880696438045873"),
]


def enforce_tiktok_50_cap_once() -> int:
    global _last_tt_budget_check
    now_ts = time.time()
    if now_ts - _last_tt_budget_check < 60.0:
        return 0
    _last_tt_budget_check = now_ts
    fixed_tt = 0
    try:
        from datetime import timedelta, timezone
        import tiktok_monitor as tm
        sess, hdrs = tm.build_session(tm.load_cookies())
        checked_cids = set()
        for acc_name, aadvid, tz_off in tm.ACCOUNTS:
            now_acc = datetime.now(timezone(timedelta(hours=tz_off)))
            t_str = (now_acc + timedelta(days=1)).strftime("%Y-%m-%d")
            y_str = (now_acc - timedelta(days=1)).strftime("%Y-%m-%d")
            camps = tm.fetch_campaigns(sess, hdrs, aadvid, y_str, t_str) or []
            for c in camps:
                cid = str(c.get("campaign_id") or "")
                if not cid:
                    continue
                checked_cids.add((aadvid, cid))
                prim = str(c.get("primary_status") or "")
                opt = str(c.get("opt_status") or c.get("campaign_opt_status") or "")
                is_en = (prim not in ("disable", "delete")) and (opt == "0")
                rd = c.get("row_data") or {}
                bdg = float(rd.get("budget") or c.get("budget") or 0.0)
                if not is_en and (aadvid, cid) not in _PENDING_TT_50_RESET_CIDS:
                    continue
                if bdg <= 50.01 and (aadvid, cid) not in _PENDING_TT_50_RESET_CIDS:
                    continue
                h_b = dict(hdrs)
                h_b["Referer"] = f"https://ads.tiktok.com/i18n/manage/campaign?aadvid={aadvid}"
                det = (
                    sess.get(
                        f"https://ads.tiktok.com/api/v3/i18n/perf/campaign/detail/?aadvid={aadvid}&campaign_id={cid}",
                        headers=h_b,
                        timeout=12,
                    ).json().get("data")
                    or {}
                )
                if not det:
                    continue
                cur_b = float(det.get("budget") or 0.0)
                if cur_b > 50.01:
                    up_p = {
                        **det,
                        "campaign_id": cid,
                        "campaign_name": det.get("name") or c.get("campaign_name") or cid,
                        "budget": 50.0,
                    }
                    rb = sess.post(
                        f"https://ads.tiktok.com/api/v3/i18n/perf/campaign/update/?aadvid={aadvid}",
                        headers=h_b,
                        json=up_p,
                        timeout=12,
                    ).json()
                    if rb.get("code") == 0:
                        fixed_tt += 1
                        with open(LOG_FILE, "a", encoding="utf-8") as lf:
                            lf.write(
                                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] TikTok $50 Cap Locked: {acc_name} | {cid} (${cur_b:.0f} -> $50.00)\n"
                            )
    except Exception:
        pass
    return fixed_tt


def _get_thread_sess(token: str):
    sess = getattr(_tls, "sess", None)
    if sess is None or getattr(_tls, "tok", None) != token:
        sess = sm.make_session(token)
        _tls.sess = sess
        _tls.tok = token
    return sess


def is_already_running() -> bool:
    if not LOCK_FILE.exists():
        return False
    try:
        import ctypes
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
        hb = float(data.get("heartbeat") or data.get("started_at") or 0.0)
        if pid and pid != os.getpid() and (time.time() - hb < 180.0):
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                exit_code = ctypes.c_ulong(0)
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
                ctypes.windll.kernel32.CloseHandle(h)
                if ok and exit_code.value == 259:  # STILL_ACTIVE
                    return True
    except Exception:
        pass
    return False


def _process_direct_campaign(item: tuple, token: str) -> int:
    acc_name, cid, cname, forced_low_bid = item
    is_gulf_or_test_camp = (
        sm.is_dedicated_snap_test_campaign(cid, cname)
        or sm.is_gulf_034_campaign(cname)
        or cid in getattr(sm, "ALLOWED_GULF_CAMPAIGN_IDS", set())
    )
    s = _get_thread_sess(token)
    try:
        r = s.get(f"{API_BASE}/campaigns/{cid}/adsquads?limit=100", timeout=15)
        if not r.ok:
            return 0
        squads = [el["adsquad"] for el in r.json().get("adsquads", []) if "adsquad" in el]
    except Exception:
        return 0

    to_put = []
    for sq in squads:
        if not sq:
            continue
        is_active_sq = (sq.get("status") == "ACTIVE")
        changed = False
        b = int(sq.get("bid_micro") or 0)
        cur_budget = int(sq.get("daily_budget_micro") or 0)
        sname = sq.get("name", "")
        sid = sq.get("id", "")
        is_special_sq = (
            is_gulf_or_test_camp
            or sm.is_gulf_034_campaign(cname, sname, sq)
            or sm.is_dedicated_snap_test_campaign(cid, cname, sname)
        )
        if not is_special_sq:
            target_bid = sm.get_locked_low_bid_micro(cid, cname, sname, sid) or 1_900_000
            if b != target_bid or sq.get("bid_strategy") != "LOWEST_COST_WITH_MAX_BID" or bool(sq.get("target_bid")):
                sq["bid_micro"] = target_bid
                sq["bid_strategy"] = "LOWEST_COST_WITH_MAX_BID"
                sq["target_bid"] = False
                changed = True
        if is_active_sq and cur_budget > 0:
            now_ksa = datetime.now(sm.ACCOUNT_TZ)
            is_late_night_ksa = (now_ksa.hour >= 22 or now_ksa.hour < 6)
            today_ksa_str = now_ksa.strftime("%Y-%m-%d")
            expected_budget = 20_000_000 if is_late_night_ksa else (sm.get_custom_daytime_budget_micro(sid, today_ksa_str) or 20_000_000)
            if cur_budget != expected_budget:
                sq["daily_budget_micro"] = expected_budget
                changed = True

        if changed:
            to_put.append(sq)

    fixed_in_camp = 0
    if to_put:
        for i in range(0, len(to_put), 20):
            try:
                r_p = s.put(f"{API_BASE}/campaigns/{cid}/adsquads", json={"adsquads": to_put[i : i + 20]}, timeout=20)
                if r_p.ok:
                    fixed_in_camp += len(to_put[i : i + 20])
            except Exception:
                pass
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as lf:
                lf.write(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Restored/Locked {len(to_put)} AdSquads in {acc_name} | {cname}\n"
                )
        except Exception:
            pass
    return fixed_in_camp


_PERSISTENT_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=6)
_cached_snap_token = ""
_cached_snap_token_ts = 0.0


def _get_cached_snap_token(force_refresh: bool = False) -> str:
    global _cached_snap_token, _cached_snap_token_ts
    now_t = time.time()
    if (not force_refresh) and _cached_snap_token and (now_t - _cached_snap_token_ts < 2400.0):
        return _cached_snap_token
    cfg = sm.load_config()
    tok = sm.refresh_access_token(cfg)
    if tok:
        _cached_snap_token = tok
        _cached_snap_token_ts = now_t
    return tok


def enforce_once() -> int:
    fixed_count = 0
    token = _get_cached_snap_token(force_refresh=False)
    camp_items = {cid: (acc_name, cid, cname, forced_low_bid) for acc_name, cid, cname, forced_low_bid in DIRECT_CAMPAIGNS}
    # أيضاً جلب أي حملة سعودية نشطة في الحسابين لضمان عدم إفلات أي حملة سعودية من سقف $1.80
    s_acc = _get_thread_sess(token)
    for acc_id, acc_name in ((sm.TARGET_ACCOUNT_ID, "CSP9860"), (sm.SECOND_ACCOUNT_ID, "CSP9860 4")):
        try:
            r_c = s_acc.get(f"{API_BASE}/adaccounts/{acc_id}/campaigns?limit=200", timeout=15)
            if r_c.status_code == 401:
                token = _get_cached_snap_token(force_refresh=True)
                s_acc = _get_thread_sess(token)
                r_c = s_acc.get(f"{API_BASE}/adaccounts/{acc_id}/campaigns?limit=200", timeout=15)
            camps = [el["campaign"] for el in (r_c.json().get("campaigns", []) if r_c.ok else []) if "campaign" in el]
            for c in camps:
                if c and c.get("status") == "ACTIVE":
                    cid = c["id"]
                    cname = c.get("name", "")
                    cb_micro = int(c.get("daily_budget_micro") or 0)
                    if cb_micro > 0 and cb_micro != 20_000_000:
                        c["daily_budget_micro"] = 20_000_000
                        try:
                            s_acc.put(f"{API_BASE}/adaccounts/{acc_id}/campaigns", json={"campaigns": [c]}, timeout=15)
                            fixed_count += 1
                        except Exception:
                            pass
                    camp_items.setdefault(cid, (acc_name, cid, cname, None))
        except Exception:
            pass

    futures = [_PERSISTENT_POOL.submit(_process_direct_campaign, item, token) for item in camp_items.values()]
    for f in concurrent.futures.as_completed(futures):
        try:
            fixed_count += int(f.result() or 0)
        except Exception:
            pass
    fixed_count += enforce_tiktok_50_cap_once()
    return fixed_count


_SNAP_BID_MUTEX = None


def _acquire_daemon_mutex() -> bool:
    global _SNAP_BID_MUTEX
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        _SNAP_BID_MUTEX = k.CreateMutexW(None, False, "Local\\AntigravitySnapLowBidLockDaemon_v2")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:
        return not is_already_running()


def _is_daemon_mutex_alive() -> bool:
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenMutexW(0x00100000, False, "Local\\AntigravitySnapLowBidLockDaemon_v2")
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return is_already_running()


def main():
    if not _acquire_daemon_mutex():
        return
    try:
        LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}), encoding="utf-8")
    except Exception:
        pass
    while True:
        try:
            LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "heartbeat": time.time()}), encoding="utf-8")
            enforce_once()
        except Exception as e:
            try:
                with open(LOG_FILE, "a", encoding="utf-8") as lf:
                    lf.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error: {e}\n")
            except Exception:
                pass
        time.sleep(15)


if __name__ == "__main__":
    if "--once" in sys.argv:
        # Skip redundant parallel 22-campaign API sweep if the 15s background daemon is already active
        if not _is_daemon_mutex_alive():
            enforce_once()
    else:
        main()
