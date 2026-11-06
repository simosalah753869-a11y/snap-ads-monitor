"""
Facebook Ads 100% Background Controller (US Proxy Tunnel + Auto Live Token + Graph API v21.0)
Account: Isabel Van Der Sluis (UID: 61580737875912)
Proxy: 149.18.55.226:12323 (US Residential ISP Proxy from Chrome Profile 5)
Ad Accounts:
  1. act_4504761856414520 (DarAlRimal_1208 - BM: Shopify: 0fix9tqe 1793332235 [1465639688821208], Pixel: 1819055765900235)
  2. act_1164514299337955 (DarAlRimal_2611 - BM: Shopify: 5uy7kbgk 1793331353 [1142937861502611], Pixel: 1516213130335779)
  3. act_2031165734260288 (Isabel Van Der Sluis - Personal Ad Account)
"""
import os, sys, socket, ssl, base64, time, json, re, urllib.parse
from http.client import HTTPResponse

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

PROXY_HOST = (os.environ.get("FB_PROXY_HOST") or "149.18.55.226").strip()
PROXY_PORT = int((os.environ.get("FB_PROXY_PORT") or "12323").strip())
PROXY_AUTH = (os.environ.get("FB_PROXY_AUTH") or base64.b64encode(b"14ad674757614:4ab3aaa0c4").decode()).strip()

UID = "61580737875912"
TOTP_SECRET = "A2DKKDNU2JCTH2CLAU4V3KRRT5VI645S"

COOKIE_STR = (
    os.environ.get("FB_COOKIE_STR")
    or (
        "datr=C9EaaszFEW-rtcYGSDNlGPfB; "
        "sb=TdEaajERkVxza3dgxwahg2zB; "
        "c_user=61580737875912; "
        "xs=33%3A6Zy2gLs6fYlxQw%3A2%3A1780142490%3A-1%3A-1; "
        "fr=0o7lrpL4HKRhEc7ve.AWcqWPUC2Ip7iVV9y3gCcmqDHqVBy2dIRU7lzfqglLxSM_N3EiQ.BqIr8Q..AAA.0.0.BqwqX2.AWcn267xgXwRhVTB6rx-6U753Bo"
    )
).strip()

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fb_account_state.json")

AD_ACCOUNTS = {
    "bm1": {
        "id": "act_4504761856414520",
        "account_id": "4504761856414520",
        "name": "DarAlRimal_1208",
        "bm_id": "1465639688821208",
        "bm_name": "Shopify: 0fix9tqe 1793332235",
        "pixel_id": "1819055765900235",
        "business_user_id": "61596098431559"
    },
    "bm2": {
        "id": "act_1164514299337955",
        "account_id": "1164514299337955",
        "name": "DarAlRimal_2611",
        "bm_id": "1142937861502611",
        "bm_name": "Shopify: 5uy7kbgk 1793331353",
        "pixel_id": "1516213130335779",
        "business_user_id": "61596066303050"
    },
    "bm2_2": {
        "id": "act_1135591735634568",
        "account_id": "1135591735634568",
        "name": "DarAlRimal_BM2_2",
        "bm_id": "1142937861502611",
        "bm_name": "Shopify: 5uy7kbgk 1793331353",
        "pixel_id": "1516213130335779",
        "business_user_id": "61596066303050"
    }
    # NOTE (2026-11-06 User Directive): حساب فيسبوك الشخصي (Isabel Van Der Sluis / act_2031165734260288 / UID 61580737875912) تم إيقاف العمل به نهائياً وللأبد
}

RETIRED_PERSONAL_FB_ACCOUNTS = {"act_2031165734260288", "2031165734260288", "61580737875912"}

def open_tunnel(target_host, target_port=443, max_retries=25):
    for _ in range(max_retries):
        s = None
        try:
            s = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=15)
            s.settimeout(20.0)
            req = (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n"
                f"Proxy-Authorization: Basic {PROXY_AUTH}\r\n"
                f"Proxy-Connection: keep-alive\r\n\r\n"
            ).encode()
            s.sendall(req)
            resp = s.recv(4096).decode("utf-8", "ignore")
            if resp and "200" in resp.splitlines()[0]:
                ctx = ssl.create_default_context()
                ssock = ctx.wrap_socket(s, server_hostname=target_host)
                ssock.settimeout(60.0)
                return ssock
        except Exception:
            pass
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        time.sleep(1.5)
    raise RuntimeError(f"Could not open US proxy tunnel to {target_host}")

def refresh_live_token():
    ssock = open_tunnel("adsmanager.facebook.com")
    hdr_lines = [
        "GET /adsmanager/manage/campaigns?act=1164514299337955 HTTP/1.1",
        "Host: adsmanager.facebook.com",
        "Connection: keep-alive",
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language: en-US,en;q=0.9",
        "Sec-Fetch-Dest: document",
        "Sec-Fetch-Mode: navigate",
        "Sec-Fetch-Site: none",
        "Sec-Fetch-User: ?1",
        "Upgrade-Insecure-Requests: 1",
        f"Cookie: {COOKIE_STR}"
    ]
    ssock.sendall(("\r\n".join(hdr_lines) + "\r\n\r\n").encode("utf-8"))
    resp = HTTPResponse(ssock)
    resp.begin()
    text = resp.read().decode("utf-8", "ignore")
    ssock.close()
    tokens = list(set(re.findall(r"EAA[A-Za-z0-9]+", text)))
    dtsg = re.findall(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"', text)
    if not tokens:
        env_tok = (os.environ.get("FB_ACCESS_TOKEN") or "").strip()
        if env_tok:
            return env_tok
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    st = json.load(f)
                if st.get("access_token"):
                    return st["access_token"]
            except Exception:
                pass
        raise RuntimeError("Failed to extract live EAA token from Ads Manager")
    state = {"access_token": tokens[0], "fb_dtsg": dtsg[0] if dtsg else "", "updated_at": time.time(), "ad_accounts": AD_ACCOUNTS}
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return state["access_token"]

def get_token():
    saved_tok = ""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
            saved_tok = (st.get("access_token") or "").strip()
            if time.time() - st.get("updated_at", 0) < 86400 and saved_tok:
                return saved_tok
        except Exception:
            pass
    env_tok = (os.environ.get("FB_ACCESS_TOKEN") or "").strip()
    if env_tok:
        try:
            return refresh_live_token()
        except Exception:
            return env_tok
    try:
        return refresh_live_token()
    except Exception:
        if saved_tok:
            return saved_tok
        raise

def graph_call(ssock, method, path, params=None, data=None, token=None):
    tok = token or get_token()
    p = {"access_token": tok}
    if params:
        p.update(params)
    qs = urllib.parse.urlencode(p)
    full_path = f"/v21.0/{path.lstrip('/')}?{qs}"
    body_bytes = b""
    hdr_lines = [
        f"{method} {full_path} HTTP/1.1",
        "Host: graph.facebook.com",
        "Connection: keep-alive",
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        f"Cookie: {COOKIE_STR}",
        "Accept: application/json"
    ]
    if data:
        body_bytes = urllib.parse.urlencode(data).encode("utf-8")
        hdr_lines.append("Content-Type: application/x-www-form-urlencoded")
        hdr_lines.append(f"Content-Length: {len(body_bytes)}")
    ssock.sendall(("\r\n".join(hdr_lines) + "\r\n\r\n").encode("utf-8") + body_bytes)
    resp = HTTPResponse(ssock)
    resp.begin()
    raw_body = resp.read().decode("utf-8", "ignore")
    return json.loads(raw_body)

def status_summary():
    tok = get_token()
    ssock = open_tunnel("graph.facebook.com")
    accs = graph_call(ssock, "GET", "me/adaccounts", {
        "fields": "id,account_id,name,account_status,disable_reason,currency,timezone_name,amount_spent,spend_cap,balance,funding_source,funding_source_details,business{id,name},adspixels{id,name,last_fired_time},campaigns{id,name,status,effective_status,daily_budget}"
    }, token=tok)
    ssock.close()
    return accs

if __name__ == "__main__":
    print(json.dumps(status_summary(), indent=2, ensure_ascii=False))
