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

CONFIG_FILE = Path(__file__).parent / "snap_config.json"
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


def api_get(endpoint: str, token: str) -> dict:
    import time, requests
    url = f"{API_BASE}{endpoint}" if endpoint.startswith("/") else endpoint
    for attempt in range(6):
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {token}", "Connection": "close"}, timeout=30)
            if r.status_code == 200:
                return r.json()
        except Exception:
            try:
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Connection": "close"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception:
                if attempt == 5:
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
