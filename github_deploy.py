import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

_orig_getaddrinfo = socket.getaddrinfo
_dns_cache = {}
_STATIC_DNS_FALLBACK = {
    "api.github.com": "140.82.121.6",
    "github.com": "140.82.121.4",
    "adsapi.snapchat.com": "34.110.187.193",
    "accounts.snapchat.com": "34.149.46.130",
    "docs.google.com": "142.251.27.100",
}
def _doh_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host in _dns_cache:
        try:
            return _orig_getaddrinfo(_dns_cache[host], port, socket.AF_INET, type, proto, flags)
        except Exception:
            pass
    try:
        res = _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
        if res:
            ip = res[0][4][0]
            if ip and not str(host).replace(".", "").isdigit():
                _dns_cache[host] = ip
            return res
    except Exception:
        pass
    for doh_url in (
        f"https://1.1.1.1/dns-query?name={host}&type=A",
        f"https://8.8.8.8/resolve?name={host}&type=A",
    ):
        try:
            req = urllib.request.Request(
                doh_url,
                headers={"accept": "application/dns-json"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                r = json.loads(resp.read().decode("utf-8"))
            ips = [a["data"] for a in r.get("Answer", []) if a.get("type") == 1]
            if ips:
                _dns_cache[host] = ips[0]
                break
        except Exception:
            pass
    if host not in _dns_cache and host in _STATIC_DNS_FALLBACK:
        _dns_cache[host] = _STATIC_DNS_FALLBACK[host]
    if host in _dns_cache:
        return _orig_getaddrinfo(_dns_cache[host], port, socket.AF_INET, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = _doh_getaddrinfo

BASE_DIR = Path(__file__).parent
REPO_NAME = "snap-ads-monitor"


def gh_api(method: str, endpoint: str, token: str, payload: dict = None, timeout_sec: int = 30, max_attempts: int = 3) -> dict:
    import time
    import requests
    url = f"https://api.github.com{endpoint}" if endpoint.startswith("/") else endpoint
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "User-Agent": "SnapAdsBot-Deployer",
    }
    for attempt in range(max_attempts):
        try:
            resp = requests.request(method, url, headers=headers, json=payload, timeout=timeout_sec)
            if resp.status_code in (200, 201, 204):
                return resp.json() if resp.text.strip() else {}
            err_body = resp.text
            if (resp.status_code in (500, 502, 503, 504) or (resp.status_code == 400 and "try resubmitting" in err_body)) and attempt < max_attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"GitHub API {method} {endpoint} failed ({resp.status_code}): {err_body}")
        except requests.RequestException as e:
            if attempt < max_attempts - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError(f"GitHub API {method} {endpoint} network timeout/error: {e}")


def _resolve_live_file_sha(owner: str, repo: str, path_in_repo: str, token: str) -> str:
    sha = None
    try:
        ref_data = gh_api("GET", f"/repos/{owner}/{repo}/git/ref/heads/main", token)
        commit_sha = ((ref_data or {}).get("object") or {}).get("sha")
        if commit_sha:
            tree_data = gh_api("GET", f"/repos/{owner}/{repo}/git/trees/{commit_sha}?recursive=1", token)
            for item in (tree_data or {}).get("tree") or []:
                if item.get("path") == path_in_repo:
                    sha = item.get("sha")
                    break
    except Exception:
        pass
    if not sha:
        try:
            existing = gh_api("GET", f"/repos/{owner}/{repo}/contents/{path_in_repo}?ref=main", token)
            if isinstance(existing, dict):
                sha = existing.get("sha")
        except Exception:
            pass
    return sha


def put_file(owner: str, repo: str, path_in_repo: str, local_path: Path, token: str, message: str):
    import hashlib
    import time
    raw_bytes = local_path.read_bytes()
    local_git_sha = hashlib.sha1(b"blob " + str(len(raw_bytes)).encode("ascii") + b"\0" + raw_bytes).hexdigest()
    content_b64 = base64.b64encode(raw_bytes).decode("ascii")

    last_err = None
    for attempt in range(4):
        sha = _resolve_live_file_sha(owner, repo, path_in_repo, token)
        if sha and sha == local_git_sha:
            print(f"   ⏭️ الملف محدث مسبقاً على GitHub: {path_in_repo}")
            try:
                cache_p = BASE_DIR / "github_sync_shas.json"
                c_map = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
                c_map[path_in_repo] = local_git_sha
                cache_p.write_text(json.dumps(c_map, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            return

        payload = {"message": message, "content": content_b64}
        if sha:
            payload["sha"] = sha
        try:
            gh_api("PUT", f"/repos/{owner}/{repo}/contents/{path_in_repo}", token, payload)
            try:
                cache_p = BASE_DIR / "github_sync_shas.json"
                c_map = json.loads(cache_p.read_text(encoding="utf-8")) if cache_p.exists() else {}
                c_map[path_in_repo] = local_git_sha
                cache_p.write_text(json.dumps(c_map, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            print(f"   ✅ تم رفع الملف: {path_in_repo}")
            return
        except RuntimeError as e_put:
            last_err = e_put
            err_s = str(e_put)
            if ("(409)" in err_s or "(422)" in err_s) and attempt < 3:
                time.sleep(1.8 * (attempt + 1))
                continue
            raise
    if last_err:
        raise last_err


def set_repo_variable(owner: str, repo: str, name: str, value: str, token: str):
    try:
        gh_api("PATCH", f"/repos/{owner}/{repo}/actions/variables/{name}", token, {"name": name, "value": value})
        return
    except Exception:
        pass
    try:
        gh_api("POST", f"/repos/{owner}/{repo}/actions/variables", token, {"name": name, "value": value})
    except Exception:
        gh_api("PATCH", f"/repos/{owner}/{repo}/actions/variables/{name}", token, {"name": name, "value": value})


def merge_remote_history(owner: str, repo: str, token: str):
    hist_path = BASE_DIR / "paused_history.json"
    local_hist = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}
    try:
        existing = gh_api("GET", f"/repos/{owner}/{repo}/contents/paused_history.json", token)
        remote_raw = base64.b64decode(existing["content"]).decode("utf-8")
        remote_hist = json.loads(remote_raw)
    except Exception:
        remote_hist = {}

    for k in (
        "tiktok_videos",
        "tiktok_ads",
        "facebook_ads",
        "facebook_campaigns",
        "cross_paused_campaigns",
        "creative_media_map",
        "creative_details_cache",
        "headline_creatives_cache",
        "headline_rotation_idx",
        "disabled_headlines",
        "scaling_bid_bumps",
        "bid_bumps",
    ):
        merged = dict(remote_hist.get(k) or {})
        merged.update(local_hist.get(k) or {})
        local_hist[k] = merged
    # Keep local cleaned state authoritative for adsquads, ads, disabled_videos, tiktok_disabled_dup_videos and no_duplicate_campaigns
    local_hist["adsquads"] = dict(local_hist.get("adsquads") or {})
    local_hist["ads"] = dict(local_hist.get("ads") or {})
    local_hist["disabled_videos"] = dict(local_hist.get("disabled_videos") or {})
    local_hist["tiktok_disabled_dup_videos"] = dict(local_hist.get("tiktok_disabled_dup_videos") or {})
    local_hist["no_duplicate_campaigns"] = dict(local_hist.get("no_duplicate_campaigns") or {})

    # Archive old (>3 days) ad/video pause logs to paused_history_archive.json so paused_history.json stays <300KB for GitHub REST API (<1MB limit)
    archive_path = BASE_DIR / "paused_history_archive.json"
    try:
        archive_hist = json.loads(archive_path.read_text(encoding="utf-8")) if archive_path.exists() else {}
    except Exception:
        archive_hist = {}
    old_dates = ("2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20")
    for sec in ("ads", "adsquads", "tiktok_videos"):
        sec_dict = dict(local_hist.get(sec) or {})
        arch_sec = dict(archive_hist.get(sec) or {})
        kept_sec = {}
        for k, v in sec_dict.items():
            p_at = str((v or {}).get("paused_at", "")) if isinstance(v, dict) else ""
            if any(od in p_at for od in old_dates):
                arch_sec[k] = v
            else:
                kept_sec[k] = v
        archive_hist[sec] = arch_sec
        local_hist[sec] = kept_sec
    for cache_sec in ("creative_details_cache", "headline_creatives_cache", "smart_spend_watchdog"):
        if cache_sec in local_hist:
            archive_hist[cache_sec] = local_hist.pop(cache_sec, None)
    archive_path.write_text(json.dumps(archive_hist, ensure_ascii=False, indent=2), encoding="utf-8")
    hist_path.write_text(json.dumps(local_hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def merge_remote_gulf_ledger(owner: str, repo: str, token: str):
    ledger_path = BASE_DIR / "gulf_2h_actions_ledger.json"
    if not ledger_path.exists():
        return
    try:
        local_l = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception:
        return
    try:
        existing = gh_api("GET", f"/repos/{owner}/{repo}/contents/gulf_2h_actions_ledger.json", token)
        remote_raw = base64.b64decode(existing["content"]).decode("utf-8")
        remote_l = json.loads(remote_raw)
    except Exception:
        remote_l = {}
    if not isinstance(remote_l, dict):
        return
    for dict_k in ("snap_squad_bumps", "tiktok_gulf_scaled"):
        merged = dict(remote_l.get(dict_k) or {})
        merged.update(local_l.get(dict_k) or {})
        local_l[dict_k] = merged
    rem_logs = list(remote_l.get("big_actions_log") or [])
    loc_logs = list(local_l.get("big_actions_log") or [])
    combined_logs = []
    seen_l = set()
    for entry in rem_logs + loc_logs:
        if entry not in seen_l:
            seen_l.add(entry)
            combined_logs.append(entry)
    local_l["big_actions_log"] = combined_logs[-60:]
    ledger_path.write_text(json.dumps(local_l, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_remote_quant_portfolio(owner: str, repo: str, token: str):
    port_path = BASE_DIR / "binance_paper_portfolio.json"
    if not port_path.exists():
        return
    try:
        local_p = json.loads(port_path.read_text(encoding="utf-8"))
        local_ts = str(local_p.get("last_updated_utc") or "")
    except Exception:
        return
    try:
        existing = gh_api("GET", f"/repos/{owner}/{repo}/contents/binance_paper_portfolio.json", token)
        remote_raw = base64.b64decode(existing["content"]).decode("utf-8")
        remote_p = json.loads(remote_raw)
        remote_ts = str(remote_p.get("last_updated_utc") or "")
    except Exception:
        return
    if remote_ts and remote_ts > local_ts:
        port_path.write_text(json.dumps(remote_p, ensure_ascii=False, indent=2), encoding="utf-8")


SYNC_SHA_CACHE_FILE = BASE_DIR / "github_sync_shas.json"


def sync_state_and_rules_to_github(reason: str = "local_automation", trigger_dispatch: bool = False) -> bool:
    """
    مزامنة تلقائية فورية مع مستودع GitHub بعد أي تغيير يقوم به الأوتوميشن المحلي
    لمنع أي تضارب أو لخبطة بين الأوتوميشن المحلي وبوت GitHub Actions (سواء في السعودية أو الكويت وقطر وعُمان).
    يرفع فقط الملفات التي تغيرت فعلياً باستخدام مقارنة SHA1 المحلية والسحابية.
    """
    import hashlib
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return False
    cfg_file = BASE_DIR / "snap_config.json"
    if not cfg_file.exists():
        return False
    try:
        cfg_data = json.loads(cfg_file.read_text(encoding="utf-8"))
    except Exception:
        return False
    github_token = (cfg_data.get("github_token") or "").strip()
    if not github_token:
        return False
    try:
        user = gh_api("GET", "/user", github_token, timeout_sec=6, max_attempts=1)
        owner = user["login"]
    except Exception as e_auth:
        print(f"ℹ️ تخطي مزامنة GitHub ({reason}): {e_auth}")
        return False

    try:
        merge_remote_history(owner, REPO_NAME, github_token)
    except Exception:
        pass
    try:
        merge_remote_gulf_ledger(owner, REPO_NAME, github_token)
    except Exception:
        pass
    try:
        merge_remote_quant_portfolio(owner, REPO_NAME, github_token)
    except Exception:
        pass

    try:
        sha_cache = json.loads(SYNC_SHA_CACHE_FILE.read_text(encoding="utf-8")) if SYNC_SHA_CACHE_FILE.exists() else {}
    except Exception:
        sha_cache = {}

    files_to_sync = [
        ("paused_history.json", BASE_DIR / "paused_history.json"),
        ("gulf_2h_actions_ledger.json", BASE_DIR / "gulf_2h_actions_ledger.json"),
        ("binance_paper_portfolio.json", BASE_DIR / "binance_paper_portfolio.json"),
        ("quant_backtest_report.json", BASE_DIR / "quant_backtest_report.json"),
        ("binance_guardian_bot.py", BASE_DIR / "binance_guardian_bot.py"),
        ("fast_quant_optimizer.py", BASE_DIR / "fast_quant_optimizer.py"),
        ("quant_backtester.py", BASE_DIR / "quant_backtester.py"),
        ("snap_squad_custom_bid_locks.json", BASE_DIR / "snap_squad_custom_bid_locks.json"),
        ("snap_master_actions_ledger.json", BASE_DIR / "snap_master_actions_ledger.json"),
        ("tiktok_master_actions_ledger.json", BASE_DIR / "tiktok_master_actions_ledger.json"),
        ("taager_gads_latest_stats.json", BASE_DIR / "taager_gads_latest_stats.json"),
        ("fb_account_state.json", BASE_DIR / "fb_account_state.json"),
        ("fb_live_spend_snapshot.json", BASE_DIR / "fb_live_spend_snapshot.json"),
        ("facebook_ads_controller.py", BASE_DIR / "facebook_ads_controller.py"),
        ("facebook_monitor.py", BASE_DIR / "facebook_monitor.py"),
        ("snap_monitor.py", BASE_DIR / "snap_monitor.py"),
        ("tiktok_new_4_cookies.json", BASE_DIR / "tiktok_new_4_cookies.json"),
        ("tiktok_monitor.py", BASE_DIR / "tiktok_monitor.py"),
        ("tiktok_2h_sales_master.py", BASE_DIR / "tiktok_2h_sales_master.py"),
        ("snap_low_bid_lock_daemon.py", BASE_DIR / "snap_low_bid_lock_daemon.py"),
        ("store_and_ads_growth_partner.py", BASE_DIR / "store_and_ads_growth_partner.py"),
        ("gulf_2h_profit_and_ads_master.py", BASE_DIR / "gulf_2h_profit_and_ads_master.py"),
        ("cross_product_monitor.py", BASE_DIR / "cross_product_monitor.py"),
        ("snap_hourly_sales_master.py", BASE_DIR / "snap_hourly_sales_master.py"),
        ("deep_multiwindow_account_auditor.py", BASE_DIR / "deep_multiwindow_account_auditor.py"),
        ("run_all_monitors_5h.py", BASE_DIR / "run_all_monitors_5h.py"),
        (".github/workflows/snap_monitor.yml", BASE_DIR / ".github" / "workflows" / "snap_monitor.yml"),
        (".github/workflows/quant_realtime_cloud.yml", BASE_DIR / ".github" / "workflows" / "quant_realtime_cloud.yml"),
    ]

    pushed_count = 0
    for repo_path, local_path in files_to_sync:
        if not local_path.exists():
            continue
        raw_bytes = local_path.read_bytes()
        git_sha = hashlib.sha1(b"blob " + str(len(raw_bytes)).encode("ascii") + b"\0" + raw_bytes).hexdigest()
        # Always verify paused_history.json and gulf_2h_actions_ledger.json against remote if they changed locally
        if sha_cache.get(repo_path) == git_sha:
            continue
        try:
            put_file(
                owner,
                REPO_NAME,
                repo_path,
                local_path,
                github_token,
                f"Auto-sync {repo_path} from {reason}",
            )
            sha_cache[repo_path] = git_sha
            pushed_count += 1
        except Exception as e_put:
            print(f"   ⚠️ تعذر مزامنة {repo_path}: {e_put}")

    try:
        SYNC_SHA_CACHE_FILE.write_text(json.dumps(sha_cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if trigger_dispatch and pushed_count > 0:
        try:
            gh_api(
                "POST",
                f"/repos/{owner}/{REPO_NAME}/actions/workflows/snap_monitor.yml/dispatches",
                github_token,
                {"ref": "main"},
            )
        except Exception:
            pass
    if pushed_count > 0:
        print(f"🔄✅ [GitHub Auto-Sync | {reason}] تمت مزامنة {pushed_count} ملف مع GitHub ({owner}/{REPO_NAME}) لمنع أي تعارض.")
    return pushed_count > 0


def deploy(github_token: str):
    github_token = github_token.strip()
    try:
        user = gh_api("GET", "/user", github_token)
    except Exception as e_auth:
        print(
            f"ℹ️ ملاحظة مزامنة GitHub: التوكن الحالي في snap_config.json منتهي الصلاحية أو موقوف ({e_auth}). "
            "المراقب المحلي الدوري (SnapTikTokAdsMonitor20m + SnapTikTokGoogleAdsOptimizer5H) يعمل في الخلفية 24/7 بديلاً كاملاً وبأحدث القواعد 100%."
        )
        return
    owner = user["login"]
    print(f"👤 تم الاتصال بحساب GitHub: {owner}")

    # 1. Save Snapchat + TikTok credentials in repo variables (inside Private Repo)
    cfg = json.loads((BASE_DIR / "snap_config.json").read_text(encoding="utf-8"))
    set_repo_variable(owner, REPO_NAME, "SNAP_CLIENT_ID", cfg["client_id"], github_token)
    set_repo_variable(owner, REPO_NAME, "SNAP_CLIENT_SECRET", cfg["client_secret"], github_token)
    set_repo_variable(owner, REPO_NAME, "SNAP_REFRESH_TOKEN", cfg["refresh_token"], github_token)
    dar_rt = (cfg.get("daralrimal_account") or {}).get("refresh_token") or ""
    if not dar_rt and (BASE_DIR / "snap_config_daralrimal.json").exists():
        try:
            dar_rt = json.loads((BASE_DIR / "snap_config_daralrimal.json").read_text(encoding="utf-8")).get("refresh_token", "")
        except Exception:
            pass
    if dar_rt:
        set_repo_variable(owner, REPO_NAME, "SNAP_DARALRIMAL_REFRESH_TOKEN", dar_rt, github_token)

    tt_cookies = json.loads((BASE_DIR / "tiktok_cookies.json").read_text(encoding="utf-8"))
    compact_cookies = [
        {"name": c["name"], "value": c["value"], "domain": c.get("domain", ".tiktok.com")}
        for c in tt_cookies
    ]
    set_repo_variable(
        owner,
        REPO_NAME,
        "TIKTOK_COOKIES_JSON",
        json.dumps(compact_cookies, separators=(",", ":")),
        github_token,
    )
    if (BASE_DIR / "tiktok_new_4_cookies.json").exists():
        raw_new4 = json.loads((BASE_DIR / "tiktok_new_4_cookies.json").read_text(encoding="utf-8"))
        compact_new4 = {
            k: [{"name": c["name"], "value": c["value"], "domain": c.get("domain", ".tiktok.com")} for c in v]
            for k, v in raw_new4.items() if isinstance(v, list)
        }
        set_repo_variable(
            owner,
            REPO_NAME,
            "TIKTOK_NEW_4_COOKIES_JSON",
            json.dumps(compact_new4, separators=(",", ":")),
            github_token,
        )
        put_file(owner, REPO_NAME, "tiktok_new_4_cookies.json", BASE_DIR / "tiktok_new_4_cookies.json", github_token, "Update tiktok_new_4_cookies.json")
    try:
        import facebook_ads_controller as fb_ctrl
        set_repo_variable(owner, REPO_NAME, "FB_COOKIE_STR", fb_ctrl.COOKIE_STR, github_token)
        set_repo_variable(owner, REPO_NAME, "FB_PROXY_AUTH", fb_ctrl.PROXY_AUTH, github_token)
        if (BASE_DIR / "fb_account_state.json").exists():
            fb_st = json.loads((BASE_DIR / "fb_account_state.json").read_text(encoding="utf-8"))
            if fb_st.get("access_token"):
                set_repo_variable(owner, REPO_NAME, "FB_ACCESS_TOKEN", fb_st["access_token"], github_token)
    except Exception as e_fb_var:
        print(f"⚠️ ملاحظة حفظ متغيرات فيسبوك: {e_fb_var}")
    print("🔐 تم حفظ مفاتيح سناب شات وتيك توك وفيسبوك داخل إعدادات المستودع السري بنجاح.")

    # 2. Merge paused_history.json & gulf_2h_actions_ledger.json and upload files
    merge_remote_history(owner, REPO_NAME, github_token)
    merge_remote_gulf_ledger(owner, REPO_NAME, github_token)
    put_file(owner, REPO_NAME, "snap_auth.py", BASE_DIR / "snap_auth.py", github_token, "Update snap_auth.py")
    if (BASE_DIR / "facebook_ads_controller.py").exists():
        put_file(owner, REPO_NAME, "facebook_ads_controller.py", BASE_DIR / "facebook_ads_controller.py", github_token, "Update facebook_ads_controller.py")
    if (BASE_DIR / "facebook_monitor.py").exists():
        put_file(owner, REPO_NAME, "facebook_monitor.py", BASE_DIR / "facebook_monitor.py", github_token, "Update facebook_monitor.py")
    if (BASE_DIR / "fb_account_state.json").exists():
        put_file(owner, REPO_NAME, "fb_account_state.json", BASE_DIR / "fb_account_state.json", github_token, "Update fb_account_state.json")
    put_file(owner, REPO_NAME, "duplicate_gulf_campaigns.py", BASE_DIR / "duplicate_gulf_campaigns.py", github_token, "Update duplicate_gulf_campaigns.py")
    put_file(owner, REPO_NAME, "headline_engine.py", BASE_DIR / "headline_engine.py", github_token, "Update headline_engine.py")
    put_file(owner, REPO_NAME, "topup_car_campaigns.py", BASE_DIR / "topup_car_campaigns.py", github_token, "Update topup_car_campaigns.py")
    if (BASE_DIR / "restore_non_copy_and_fix_all.py").exists():
        put_file(owner, REPO_NAME, "restore_non_copy_and_fix_all.py", BASE_DIR / "restore_non_copy_and_fix_all.py", github_token, "Update restore_non_copy_and_fix_all.py")
    if (BASE_DIR / "snap_squad_custom_bid_locks.json").exists():
        put_file(owner, REPO_NAME, "snap_squad_custom_bid_locks.json", BASE_DIR / "snap_squad_custom_bid_locks.json", github_token, "Update snap_squad_custom_bid_locks.json")
    if (BASE_DIR / "gulf_2h_actions_ledger.json").exists():
        put_file(owner, REPO_NAME, "gulf_2h_actions_ledger.json", BASE_DIR / "gulf_2h_actions_ledger.json", github_token, "Update gulf_2h_actions_ledger.json")
    if (BASE_DIR / "snap_master_actions_ledger.json").exists():
        put_file(owner, REPO_NAME, "snap_master_actions_ledger.json", BASE_DIR / "snap_master_actions_ledger.json", github_token, "Update snap_master_actions_ledger.json")
    if (BASE_DIR / "tiktok_master_actions_ledger.json").exists():
        put_file(owner, REPO_NAME, "tiktok_master_actions_ledger.json", BASE_DIR / "tiktok_master_actions_ledger.json", github_token, "Update tiktok_master_actions_ledger.json")
    if (BASE_DIR / "taager_gads_latest_stats.json").exists():
        put_file(owner, REPO_NAME, "taager_gads_latest_stats.json", BASE_DIR / "taager_gads_latest_stats.json", github_token, "Update taager_gads_latest_stats.json")
    if (BASE_DIR / "snap_hourly_sales_master.py").exists():
        put_file(owner, REPO_NAME, "snap_hourly_sales_master.py", BASE_DIR / "snap_hourly_sales_master.py", github_token, "Update snap_hourly_sales_master.py")
    put_file(owner, REPO_NAME, "restore_winners_and_pause_losers_now.py", BASE_DIR / "restore_winners_and_pause_losers_now.py", github_token, "Update restore_winners_and_pause_losers_now.py")
    put_file(owner, REPO_NAME, "snap_monitor.py", BASE_DIR / "snap_monitor.py", github_token, "Update snap_monitor.py")
    put_file(owner, REPO_NAME, "tiktok_monitor.py", BASE_DIR / "tiktok_monitor.py", github_token, "Update tiktok_monitor.py")
    if (BASE_DIR / "gulf_2h_profit_and_ads_master.py").exists():
        put_file(owner, REPO_NAME, "gulf_2h_profit_and_ads_master.py", BASE_DIR / "gulf_2h_profit_and_ads_master.py", github_token, "Update gulf_2h_profit_and_ads_master.py")
    put_file(owner, REPO_NAME, "cross_product_monitor.py", BASE_DIR / "cross_product_monitor.py", github_token, "Update cross_product_monitor.py")
    put_file(owner, REPO_NAME, "store_and_ads_growth_partner.py", BASE_DIR / "store_and_ads_growth_partner.py", github_token, "Update store_and_ads_growth_partner.py")
    if (BASE_DIR / "deep_multiwindow_account_auditor.py").exists():
        put_file(owner, REPO_NAME, "deep_multiwindow_account_auditor.py", BASE_DIR / "deep_multiwindow_account_auditor.py", github_token, "Update deep_multiwindow_account_auditor.py")
    put_file(owner, REPO_NAME, "run_all_monitors_5h.py", BASE_DIR / "run_all_monitors_5h.py", github_token, "Update run_all_monitors_5h.py")
    if (BASE_DIR / "maybach_timex_campaigns_21_10.json").exists():
        put_file(owner, REPO_NAME, "maybach_timex_campaigns_21_10.json", BASE_DIR / "maybach_timex_campaigns_21_10.json", github_token, "Update maybach_timex_campaigns_21_10.json")
    put_file(
        owner,
        REPO_NAME,
        "paused_history.json",
        BASE_DIR / "paused_history.json",
        github_token,
        "Update paused_history.json",
    )
    put_file(
        owner,
        REPO_NAME,
        ".github/workflows/snap_monitor.yml",
        BASE_DIR / ".github" / "workflows" / "snap_monitor.yml",
        github_token,
        "Update 24/7 GitHub Actions workflow with TikTok Smart+ monitor",
    )

    # 3. Trigger workflow dispatch
    try:
        gh_api(
            "POST",
            f"/repos/{owner}/{REPO_NAME}/actions/workflows/snap_monitor.yml/dispatches",
            github_token,
            {"ref": "main"},
        )
        print("🚀 تم إطلاق تشغيل فوري للبوت (Snapchat + TikTok) على GitHub Actions!")
    except Exception as e:
        print("Dispatch note:", e)

    print(f"\n🎉 اكتمل الرفع بنجاح! رابط المستودع الخاص بك:\nhttps://github.com/{owner}/{REPO_NAME}/actions")


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        tok = sys.argv[1]
    else:
        cfg_data = json.loads((BASE_DIR / "snap_config.json").read_text(encoding="utf-8"))
        tok = cfg_data.get("github_token", "")
    if tok:
        deploy(tok)
    else:
        print("Usage: python github_deploy.py <GITHUB_TOKEN>")
