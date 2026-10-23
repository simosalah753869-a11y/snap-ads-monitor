import sys
import time
import json
from datetime import datetime, timezone
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from snap_auth import API_BASE, load_config, refresh_access_token

AD_ACCOUNT_ID = "350cb560-ad77-4fb5-9bbf-4c090c9ad081"
HISTORY_FILE = Path(__file__).parent / "paused_history.json"

sys.stdout.reconfigure(encoding="utf-8")

# بنك العناوين السعودية المحفزة للنقر والطلب (جميعها <= 34 حرفاً حسب شرط سناب شات الصارم)
HEADLINE_BANK = {
    "car": [
        ("H1", "هدية مجانية في انتظارك مع الطلب"),      # 31 حرفاً - زاوية الهدية المجانية
        ("H2", "التوصيل مجاني مع ضمان استرجاع"),        # 29 حرفاً - زاوية الضمان والتوصيل المجاني
        ("H3", "راحة فورية بالخطوط وداعاً للألم"),      # 31 حرفاً - زاوية حل ألم الكتف والظهر
        ("H4", "عرض خاص اليوم + الدفع عند الاستلام"),   # 34 حرفاً - زاوية العرض والثقة
        ("H5", "ركبها بثواني وغير راحة سيارتك"),        # 29 حرفاً - زاوية السهولة والتحول الفوري
        ("H6", "الأكثر طلباً بالسعودية لراحة اليد"),     # 33 حرفاً - زاوية الإثبات الاجتماعي
    ],
    "powerbank": [
        ("H1", "هدية مجانية في انتظارك مع الطلب"),      # 31 حرفاً - زاوية الهدية المجانية
        ("H2", "اشحن جوالك بدون أسلاك في أي مكان"),     # 32 حرفاً - زاوية الفائدة المباشرة
        ("H3", "التوصيل مجاني مع ضمان استرجاع"),        # 29 حرفاً - زاوية الضمان والتوصيل المجاني
        ("H4", "شحن سريع وآمن + الدفع عند الاستلام"),   # 34 حرفاً - زاوية السرعة والثقة
        ("H5", "لا يطفى جوالك بالسفر والطلعات"),        # 29 حرفاً - زاوية حل المشكلة
        ("H6", "عرض اليوم: باور بانك + هدية مجانية"),   # 34 حرفاً - زاوية العرض المزدوج
    ],
    "watch": [
        ("H1", "فخامة سويسرية + هدية مجانية اليوم"),    # 33 حرفاً - زاوية الفخامة والهدية
        ("H2", "التوصيل مجاني مع ضمان استرجاع"),        # 29 حرفاً - زاوية الضمان والتوصيل المجاني
        ("H3", "تصميم ملكي فخم + دفع عند الاستلام"),    # 33 حرفاً - زاوية التصميم الملكي والثقة
        ("H4", "خصم خاص اليوم والكمية محدودة جداً"),    # 33 حرفاً - زاوية الندرة والخصم
        ("H5", "هدية تبيض الوجه مع ضمان ذهبي"),         # 28 حرفاً - زاوية الإهداء والضمان
        ("H6", "الأكثر مبيعاً بالسعودية هذا الشهر"),     # 33 حرفاً - زاوية الإثبات الاجتماعي
    ],
    "general": [
        ("H1", "هدية مجانية في انتظارك مع الطلب"),
        ("H2", "التوصيل مجاني مع ضمان استرجاع"),
        ("H3", "عرض خاص اليوم + الدفع عند الاستلام"),
        ("H4", "خصم خاص اليوم والكمية محدودة جداً"),
    ],
}


def detect_product_type(campaign_name: str, adsquad_name: str = "") -> str:
    combined = f"{campaign_name} {adsquad_name}".lower()
    if any(k in combined for k in ("watch", "suis", "ساعة", "ساعات")):
        return "watch"
    if any(k in combined for k in ("باور", "power", "pb", "فولتريكس", "وينر", "hist_tc", "manzilk")):
        return "powerbank"
    if any(k in combined for k in ("car", "وسادة", "تكاية", "سيارة", "text", "1.5 | 23.8")):
        return "car"
    return "general"


def get_or_create_headline_creative(
    session: requests.Session,
    ad_account_id: str,
    base_creative_id: str,
    headline_tag: str,
    headline_text: str,
    hist: dict,
) -> tuple[str, str]:
    """
    Fetches base creative details (media_id, url, brand_name, etc.) and returns
    (creative_id_with_headline, media_id), caching in hist['headline_creatives_cache'].
    """
    headline_text = headline_text.strip()[:34]
    cr_cache = hist.setdefault("creative_details_cache", {})
    if base_creative_id not in cr_cache:
        rc = session.get(f"{API_BASE}/creatives/{base_creative_id}", timeout=20)
        rc.raise_for_status()
        cr_obj = rc.json().get("creatives", [{}])[0].get("creative", {})
        if not cr_obj:
            raise RuntimeError(f"Could not fetch creative {base_creative_id}")
        cr_cache[base_creative_id] = {
            "name": cr_obj.get("name", "Creative"),
            "type": cr_obj.get("type", "WEB_VIEW"),
            "brand_name": cr_obj.get("brand_name") or "دار الرمال",
            "headline": cr_obj.get("headline") or "",
            "call_to_action": cr_obj.get("call_to_action") or "ORDER_NOW",
            "top_snap_media_id": cr_obj.get("top_snap_media_id"),
            "top_snap_crop_position": cr_obj.get("top_snap_crop_position") or "MIDDLE",
            "forced_view_eligibility": cr_obj.get("forced_view_eligibility") or "SIX_SECONDS",
            "ad_product": cr_obj.get("ad_product") or "SNAP_AD",
            "web_view_properties": cr_obj.get("web_view_properties") or {},
        }
        hist.setdefault("creative_media_map", {})[base_creative_id] = cr_obj.get("top_snap_media_id") or base_creative_id

    base_info = cr_cache[base_creative_id]
    media_id = base_info.get("top_snap_media_id") or base_creative_id
    url_str = (base_info.get("web_view_properties") or {}).get("url", "")

    if (base_info.get("headline") or "").strip() == headline_text:
        return base_creative_id, media_id

    hl_cache = hist.setdefault("headline_creatives_cache", {})
    cache_key = f"{media_id}|{url_str}|{headline_text}"
    if cache_key in hl_cache:
        cached_cid = hl_cache[cache_key]
        hist.setdefault("creative_media_map", {})[cached_cid] = media_id
        return cached_cid, media_id

    clean_base_name = (base_info.get("name") or "Video").split(" [H")[0].strip()
    new_cr_name = f"{clean_base_name} [{headline_tag}]"[:95]

    wvp = dict(base_info.get("web_view_properties") or {})
    if not wvp.get("url"):
        wvp["url"] = "https://www.daralrimal.shop/tvfAkFHdl"

    payload = {
        "creatives": [
            {
                "ad_account_id": ad_account_id,
                "name": new_cr_name,
                "type": base_info.get("type", "WEB_VIEW"),
                "brand_name": (base_info.get("brand_name") or "دار الرمال")[:25],
                "headline": headline_text,
                "call_to_action": base_info.get("call_to_action") or "ORDER_NOW",
                "shareable": True,
                "top_snap_media_id": media_id,
                "top_snap_crop_position": base_info.get("top_snap_crop_position") or "MIDDLE",
                "forced_view_eligibility": base_info.get("forced_view_eligibility") or "SIX_SECONDS",
                "ad_product": base_info.get("ad_product") or "SNAP_AD",
                "web_view_properties": wvp,
            }
        ]
    }
    r_post = session.post(f"{API_BASE}/adaccounts/{ad_account_id}/creatives", json=payload, timeout=25)
    if not r_post.ok:
        return base_creative_id, media_id

    new_cr = r_post.json().get("creatives", [{}])[0].get("creative", {})
    new_cid = new_cr.get("id") or base_creative_id
    hl_cache[cache_key] = new_cid
    hist.setdefault("creative_media_map", {})[new_cid] = media_id
    cr_cache[new_cid] = {
        **base_info,
        "name": new_cr_name,
        "headline": headline_text,
    }
    return new_cid, media_id


def make_session(token: str) -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


def clone_adsquad_exact(session: requests.Session, src: dict, copies_count: int) -> list[str]:
    source_squad_id = src["id"]
    campaign_id = src["campaign_id"]
    src_name = src["name"]
    base_clean_squad_name = src_name.split(" [H")[0].strip()
    new_name_base = base_clean_squad_name if base_clean_squad_name.lower().startswith("copy of") else f"Copy of {base_clean_squad_name}"

    ra = session.get(f"{API_BASE}/adsquads/{source_squad_id}/ads", timeout=20)
    ra.raise_for_status()
    all_ads = [a["ad"] for a in ra.json().get("ads", []) if "ad" in a]

    # استبعاد أي فيديو تم إيقافه/حذفه نهائياً على مستوى الحملة أو الحساب
    disabled_keys = set()
    disabled_names_for_camp = set()
    disabled_headlines = {}
    paused_ads_map = {}
    cmap = {}
    hist = {}
    if HISTORY_FILE.exists():
        try:
            hist = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            disabled_vids_dict = hist.get("disabled_videos") or {}
            disabled_keys = set(disabled_vids_dict.keys())
            disabled_headlines = hist.get("disabled_headlines") or {}
            paused_ads_map = hist.get("ads") or {}
            cmap = hist.get("creative_media_map") or {}
        except Exception:
            hist = {}

    def is_ad_allowed(a: dict) -> bool:
        cr_id = a.get("creative_id")
        ad_nm = (a.get("name") or "").strip()
        if "[H" in ad_nm:
            try:
                hl_t = "H" + ad_nm.split("[H", 1)[1].split("]", 1)[0].strip()
                if f"{campaign_id}:{hl_t}" in disabled_headlines:
                    return False
            except Exception:
                pass
        v_key = cmap.get(cr_id) or cr_id
        if (
            f"{campaign_id}:MEDIA:{v_key}" in disabled_keys
            or f"GLOBAL:MEDIA:{v_key}" in disabled_keys
            or f"{campaign_id}:{v_key}" in disabled_keys
            or f"{campaign_id}:{cr_id}" in disabled_keys
        ):
            return False
        p_info = paused_ads_map.get(a.get("id", ""))
        if p_info and (
            "Aggregated Video" in str(p_info.get("reason", ""))
            or "Permanent Disabled" in str(p_info.get("reason", ""))
            or "Global Cross-Campaign" in str(p_info.get("reason", ""))
        ):
            return False
        return True

    # نسخ الفيديوهات الرابحة النشطة فقط داخل المجموعة وعدم إعادة تفعيل أي فيديو خاسر موقوف!
    allowed_active = [a for a in all_ads if a.get("status") == "ACTIVE" and is_ad_allowed(a)]
    seen_media_or_base = set()
    ads_to_copy = []
    for a in allowed_active:
        cid_k = a.get("creative_id")
        base_ad_nm = (a.get("name") or "").split(" [H")[0].strip()
        media_k = cmap.get(cid_k) or base_ad_nm or cid_k
        if media_k and media_k not in seen_media_or_base:
            seen_media_or_base.add(media_k)
            ads_to_copy.append(a)

    if not ads_to_copy:
        print(f"   ⚠️ تخطي نسخ '{src_name}': لا توجد فيديوهات نشطة مسموحة بداخلها.", flush=True)
        return []

    # تحديد نوع المنتج وبنك العناوين لتدوير العناوين تلقائياً (الطريقة 3) مع استبعاد أي عنوان خاسر محظور نهائياً!
    product_type = detect_product_type("", src_name)
    raw_headlines_list = HEADLINE_BANK.get(product_type, HEADLINE_BANK["general"])
    headlines_list = [
        (htag, htxt) for (htag, htxt) in raw_headlines_list
        if f"{campaign_id}:{htag}" not in disabled_headlines
    ]
    if not headlines_list:
        headlines_list = raw_headlines_list[:2]
    rot_map = hist.setdefault("headline_rotation_idx", {})
    start_rot_idx = int(rot_map.get(campaign_id, 0))

    is_tc = str(src.get("bid_strategy") or "") == "TARGET_COST"
    if is_tc:
        bid_ladder_micro = [2_650_000, 2_750_000, 2_800_000, 2_850_000]
        raw_bid_micro = src.get("bid_micro") or 2_750_000
        raw_bid_micro = max(2_600_000, min(2_850_000, raw_bid_micro))
    else:
        bid_ladder_micro = [2_250_000, 2_350_000, 2_400_000, 2_450_000]
        raw_bid_micro = src.get("bid_micro") or 2_400_000
        raw_bid_micro = max(2_250_000, min(2_500_000, raw_bid_micro))
    bid_dollars = round(raw_bid_micro / 1_000_000, 2)

    print(
        f"   📋 نسخ '{src_name}' عدد {copies_count} مرات | سلم البيد الذكي (الأساس ${bid_dollars}) | تدوير العناوين ({product_type}) | الفيديوهات النشطة: {len(ads_to_copy)}...",
        flush=True,
    )

    exclude_keys = {
        "id",
        "updated_at",
        "created_at",
        "targeting_reach_status",
        "placement",
        "creation_state",
        "delivery_status",
        "skadnetwork_properties",
        "delivery_properties_version",
    }

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    batch_squads = []
    copy_headlines = []
    for copy_idx in range(copies_count):
        hl_pair_1 = headlines_list[(start_rot_idx + copy_idx) % len(headlines_list)]
        hl_pair_2 = headlines_list[(start_rot_idx + copy_idx + 1) % len(headlines_list)]
        copy_headlines.append((hl_pair_1, hl_pair_2))

        new_squad = {k: v for k, v in src.items() if k not in exclude_keys}
        new_squad["name"] = f"{new_name_base} [{hl_pair_1[0]}]"[:95]
        new_squad["status"] = "ACTIVE"
        step_bid_micro = bid_ladder_micro[copy_idx % len(bid_ladder_micro)] if copies_count > 1 else raw_bid_micro
        new_squad["bid_micro"] = step_bid_micro
        new_squad["start_time"] = now_iso
        if "placement_v2" not in new_squad:
            new_squad["placement_v2"] = {"config": "AUTOMATIC"}
        new_squad.pop("end_time", None)
        batch_squads.append(new_squad)

    rot_map[campaign_id] = (start_rot_idx + copies_count) % len(headlines_list)

    r_create = session.post(
        f"{API_BASE}/campaigns/{campaign_id}/adsquads",
        json={"adsquads": batch_squads},
        timeout=30,
    )
    if not r_create.ok:
        raise RuntimeError(f"Batch AdSquad create failed: {r_create.status_code} {r_create.text}")

    created_squads = [item["adsquad"] for item in r_create.json().get("adsquads", []) if "adsquad" in item]

    for idx, sq in enumerate(created_squads, start=1):
        sq_id = sq["id"]
        sq_bid_d = round((sq.get("bid_micro") or raw_bid_micro) / 1_000_000, 2)
        (hl1_tag, hl1_text), (hl2_tag, hl2_text) = copy_headlines[idx - 1]

        new_ads_payload = []
        for v_idx, ad in enumerate(ads_to_copy):
            base_ad_nm = (ad.get("name") or "Ad").split(" [H")[0].strip()
            # العنوان الأساسي المدور لهذه النسخة (الطريقة 3)
            cid_1, mid_1 = get_or_create_headline_creative(
                session, AD_ACCOUNT_ID, ad["creative_id"], hl1_tag, hl1_text, hist
            )
            if mid_1 and f"_m{mid_1[:6]}" not in base_ad_nm:
                base_ad_nm = f"{base_ad_nm}_m{mid_1[:6]}"
            new_ads_payload.append(
                {
                    "ad_squad_id": sq_id,
                    "creative_id": cid_1,
                    "name": f"{base_ad_nm} [{hl1_tag}]"[:95],
                    "status": "ACTIVE",
                    "type": ad.get("type", "REMOTE_WEBPAGE"),
                }
            )
            # إذا كانت المجموعة تحتوي على <= 6 فيديوهات، نضيف لأفضل فيديوهين عنواناً ثانياً منافساً وجهاً لوجه داخل نفس المجموعة (الطريقة 2)
            if len(ads_to_copy) <= 6 and v_idx < 2 and hl2_tag != hl1_tag:
                cid_2, _ = get_or_create_headline_creative(
                    session, AD_ACCOUNT_ID, ad["creative_id"], hl2_tag, hl2_text, hist
                )
                if cid_2 != cid_1:
                    new_ads_payload.append(
                        {
                            "ad_squad_id": sq_id,
                            "creative_id": cid_2,
                            "name": f"{base_ad_nm} [{hl2_tag}]"[:95],
                            "status": "ACTIVE",
                            "type": ad.get("type", "REMOTE_WEBPAGE"),
                        }
                    )

        r_ads = session.post(
            f"{API_BASE}/adsquads/{sq_id}/ads",
            json={"ads": new_ads_payload},
            timeout=25,
        )
        r_ads.raise_for_status()
        print(
            f"      ✅ نسخة {idx}/{copies_count}: '{sq.get('name')}' (ID: {sq_id}) | العنوان: '{hl1_text}' (+منافس {hl2_tag}) | Bid: ${sq_bid_d} | ACTIVE ({len(new_ads_payload)} ads)",
            flush=True,
        )
        time.sleep(0.2)

    try:
        HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    return [sq["id"] for sq in created_squads]


def main():
    token = refresh_access_token(load_config())
    session = make_session(token)

    rc = session.get(f"{API_BASE}/adaccounts/{AD_ACCOUNT_ID}/campaigns", timeout=20)
    rc.raise_for_status()
    campaigns = [c["campaign"] for c in rc.json().get("campaigns", []) if "campaign" in c]

    keywords = ("qatar", "qat", "oman", "قطر", "عمان")
    active_gulf_camps = [
        c for c in campaigns
        if c.get("status") == "ACTIVE"
        and any(k in c.get("name", "").lower() for k in keywords)
    ]

    print(f"🔍 تم العثور على {len(active_gulf_camps)} حملة نشطة لقطر/عمان:")
    for c in active_gulf_camps:
        cid = c["id"]
        cname = c["name"]
        print(f"\n=== حملة: '{cname}' (ID: {cid}) ===", flush=True)
        rsq = session.get(f"{API_BASE}/campaigns/{cid}/adsquads", timeout=20)
        rsq.raise_for_status()
        squads = [s["adsquad"] for s in rsq.json().get("adsquads", []) if "adsquad" in s]
        active_squads = [s for s in squads if s.get("status") == "ACTIVE"]
        source_squad = active_squads[0] if active_squads else (squads[0] if squads else None)
        if not source_squad:
            print("   ⚠️ لا توجد Ad Squads في هذه الحملة للنسخ منها.")
            continue
        clone_adsquad_exact(session, source_squad, 5)

    print("\n🎉 اكتمل نسخ المجموعات بنجاح!", flush=True)


if __name__ == "__main__":
    main()
