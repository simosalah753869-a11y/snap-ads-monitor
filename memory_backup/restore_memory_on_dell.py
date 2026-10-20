import base64
import json
import os
import socket
import subprocess
import sys
import urllib.request
import zipfile
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

TOKEN = sys.argv[1].strip()
OWNER = "simosalah753869-a11y"
REPO = "snap-ads-monitor"

def gh_get_raw(path_in_repo: str) -> bytes:
    # First get metadata/sha via contents API or trees API
    url = f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path_in_repo}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github.raw+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Antigravity-Memory-Restore",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()

def main():
    home = Path.home()
    print(f"🚀 جاري استعادة ذاكرة Antigravity الكاملة إلى: {home}")
    manifest = json.loads(gh_get_raw("memory_backup/manifest.json").decode("utf-8"))
    parts = manifest["parts"]
    all_bytes = bytearray()
    for p in parts:
        print(f"⬇️ جاري تحميل {p}...")
        all_bytes.extend(gh_get_raw(p))

    zip_tmp = home / "antigravity_memory_restore.zip"
    zip_tmp.write_bytes(bytes(all_bytes))
    print(f"📦 اكتمل التحميل ({len(all_bytes)/(1024*1024):.2f} MB)، جاري فك الضغط...")

    with zipfile.ZipFile(zip_tmp, "r") as zf:
        zf.extractall(home)

    zip_tmp.unlink(missing_ok=True)

    # If the Windows username on Dell is not 'salah', update paths in configs & create junction if possible
    if home.name.lower() != "salah":
        old_u = r"C:\Users\salah"
        new_u = str(home)
        print(f"🔧 تحديث المسارات تلقائياً من {old_u} إلى {new_u}...")
        for root_dir in [home / ".gemini"]:
            for dp, dn, fn in os.walk(root_dir):
                for f in fn:
                    fp = Path(dp) / f
                    if fp.suffix.lower() in {".md", ".json", ".py", ".txt", ".bat", ".ps1"}:
                        try:
                            txt = fp.read_text(encoding="utf-8")
                            updated = (
                                txt.replace(old_u, new_u)
                                .replace(old_u.replace("\\", "\\\\"), new_u.replace("\\", "\\\\"))
                                .replace("C:/Users/salah", new_u.replace("\\", "/"))
                            )
                            if updated != txt:
                                fp.write_text(updated, encoding="utf-8")
                        except Exception:
                            pass
        salah_path = Path(r"C:\Users\salah")
        if not salah_path.exists():
            try:
                subprocess.run(["cmd", "/c", "mklink", "/J", str(salah_path), str(home)], capture_output=True)
            except Exception:
                pass

    print("🎉 تمت استعادة الذاكرة والمهارات والقواعد والسكريبتات وصور المنتج المرجعية بنجاح 100%!")

if __name__ == "__main__":
    main()
