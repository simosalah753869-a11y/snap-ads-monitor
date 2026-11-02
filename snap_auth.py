import http.server
import json
import os
import socket
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

_orig_getaddrinfo = socket.getaddrinfo
_dns_cache = {}
_STATIC_DNS_FALLBACK = {
    "adsapi.snapchat.com": "34.110.187.193",
    "accounts.snapchat.com": "34.149.46.130",
    "ads.tiktok.com": "2.19.51.52",
    "business-api.tiktok.com": "2.19.51.52",
    "docs.google.com": "142.251.27.100",
    "api.github.com": "140.82.121.5",
    "github.com": "140.82.121.4",
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
    if host not in _dns_cache:
        for doh_url in (
            f"https://1.1.1.1/dns-query?name={host}&type=A",
            f"https://8.8.8.8/resolve?name={host}&type=A",
        ):
            try:
                req = urllib.request.Request(
                    doh_url,
                    headers={"accept": "application/dns-json"},
                )
                with urllib.request.urlopen(req, timeout=2.5) as resp:
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

CONFIG_FILE = Path(__file__).parent / "snap_config.json"
DARALRIMAL_CONFIG_FILE = Path(__file__).parent / "snap_config_daralrimal.json"
DARALRIMAL_AD_ACCOUNT_ID = "c6447397-25a3-418a-8529-422581e1bbe0"
DARALRIMAL_ORG_ID = "ba7867b0-bd79-4026-bf90-13ab06361520"
DARALRIMAL_PIXEL_ID = "17658288-4114-4eb3-afc9-62da64437fee"
REDIRECT_URI = "https://example.com/callback"
AUTH_URL = "https://accounts.snapchat.com/login/oauth2/authorize"
TOKEN_URL = "https://accounts.snapchat.com/login/oauth2/access_token"
API_BASE = "https://adsapi.snapchat.com/v1"


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def load_daralrimal_config():
    if DARALRIMAL_CONFIG_FILE.exists():
        return json.loads(DARALRIMAL_CONFIG_FILE.read_text(encoding="utf-8-sig"))
    main_cfg = load_config()
    dar = main_cfg.get("daralrimal_account", {})
    if dar.get("refresh_token"):
        return {
            "client_id": main_cfg.get("client_id"),
            "client_secret": main_cfg.get("client_secret"),
            "refresh_token": dar.get("refresh_token"),
            "access_token": dar.get("access_token", ""),
        }
    return {}


def refresh_daralrimal_token() -> str:
    import time, requests
    dcfg = load_daralrimal_config()
    if not dcfg.get("refresh_token"):
        raise RuntimeError("No Daralrimal refresh_token configured")
    payload = {
        "client_id": dcfg["client_id"],
        "client_secret": dcfg["client_secret"],
        "refresh_token": dcfg["refresh_token"],
        "grant_type": "refresh_token",
    }
    tokens = None
    for attempt in range(4):
        try:
            r = requests.post(TOKEN_URL, data=payload, timeout=25)
            if r.status_code == 200:
                tokens = r.json()
                break
        except Exception:
            if attempt == 3 and dcfg.get("access_token"):
                return dcfg["access_token"]
            time.sleep(1.5 * (attempt + 1))
    if not tokens:
        if dcfg.get("access_token"):
            return dcfg["access_token"]
        raise RuntimeError("Failed to refresh Daralrimal Snapchat token")
    dcfg["access_token"] = tokens["access_token"]
    if "refresh_token" in tokens:
        dcfg["refresh_token"] = tokens["refresh_token"]
    DARALRIMAL_CONFIG_FILE.write_text(json.dumps(dcfg, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        main_cfg = load_config()
        if "daralrimal_account" in main_cfg:
            main_cfg["daralrimal_account"]["access_token"] = dcfg["access_token"]
            main_cfg["daralrimal_account"]["refresh_token"] = dcfg["refresh_token"]
            save_config(main_cfg)
    except Exception:
        pass
    return dcfg["access_token"]


def get_token_for_account(account_id: str) -> str:
    if account_id == DARALRIMAL_AD_ACCOUNT_ID:
        return refresh_daralrimal_token()
    return refresh_access_token(load_config())


def exchange_code_for_token(client_id: str, client_secret: str, code: str) -> dict:
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def refresh_access_token(cfg: dict) -> str:
    import time, requests
    payload = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "refresh_token": cfg["refresh_token"],
        "grant_type": "refresh_token",
    }
    tokens = None
    for attempt in range(4):
        try:
            r = requests.post(TOKEN_URL, data=payload, timeout=25)
            if r.status_code == 200:
                tokens = r.json()
                break
        except Exception:
            if attempt == 3 and cfg.get("access_token"):
                return cfg["access_token"]
            time.sleep(1.5 * (attempt + 1))
    if not tokens:
        if cfg.get("access_token"):
            return cfg["access_token"]
        raise RuntimeError("Failed to refresh Snapchat token")
    cfg["access_token"] = tokens["access_token"]
    if "refresh_token" in tokens:
        cfg["refresh_token"] = tokens["refresh_token"]
    save_config(cfg)
    return cfg["access_token"]


_SNAP_SESSION = None


def _get_snap_session():
    global _SNAP_SESSION
    if _SNAP_SESSION is None:
        import requests
        from requests.adapters import HTTPAdapter
        _SNAP_SESSION = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
        _SNAP_SESSION.mount("https://", adapter)
        _SNAP_SESSION.mount("http://", adapter)
    return _SNAP_SESSION


def api_get(endpoint: str, token: str) -> dict:
    import time
    url = f"{API_BASE}{endpoint}" if endpoint.startswith("/") else endpoint
    sess = _get_snap_session()
    use_token = token
    for attempt in range(3):
        try:
            r = sess.get(url, headers={"Authorization": f"Bearer {use_token}"}, timeout=35)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403) and (DARALRIMAL_AD_ACCOUNT_ID in url or DARALRIMAL_ORG_ID in url):
                try:
                    use_token = refresh_daralrimal_token()
                    continue
                except Exception:
                    pass
            if r.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}



def get_auth_url(client_id: str, client_secret: str) -> str:
    cfg = load_config()
    cfg["client_id"] = client_id.strip()
    cfg["client_secret"] = client_secret.strip()
    save_config(cfg)

    params = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "snapchat-marketing-api",
    })
    return f"{AUTH_URL}?{params}"


def complete_oauth_with_code(code_or_url: str):
    cfg = load_config()
    code = code_or_url.strip()
    if "code=" in code:
        parsed = urllib.parse.urlparse(code)
        qs = urllib.parse.parse_qs(parsed.query)
        code = qs.get("code", [code])[0]

    tokens = exchange_code_for_token(cfg["client_id"], cfg["client_secret"], code)
    cfg["access_token"] = tokens["access_token"]
    cfg["refresh_token"] = tokens["refresh_token"]
    save_config(cfg)
    print("✅ Tokens saved successfully!")


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--exchange":
        complete_oauth_with_code(sys.argv[2])
    elif len(sys.argv) >= 3:
        url = get_auth_url(sys.argv[1], sys.argv[2])
        print(f"Auth URL:\n{url}")
        webbrowser.open(url)
    else:
        print("Usage:\n  python snap_auth.py <CLIENT_ID> <CLIENT_SECRET>\n  python snap_auth.py --exchange <REDIRECTED_URL_OR_CODE>")
