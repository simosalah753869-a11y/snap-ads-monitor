from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request
from snap_auth import API_BASE, _get_snap_session, api_get, load_config, refresh_access_token
from duplicate_gulf_campaigns import clone_adsquad_exact, make_session

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

TARGET_ACCOUNT_ID = "350cb560-ad77-4fb5-9bbf-4c090c9ad081"  # CSP9860
SECOND_ACCOUNT_ID = "0d00760b-6793-441f-a675-e35371ce9c7c"  # CSP9860 4
TARGET_ACCOUNT_IDS = [TARGET_ACCOUNT_ID, SECOND_ACCOUNT_ID]
DEDICATED_SNAP_TEST_CAMPAIGN_IDS = {
    "25192738-8b28-44c0-9d6f-44408b6b3bf9",  # CSP9860 - تيست - وسادة السيارة ($3.00 Max Bid)
    "57c21f10-0cd3-4bd5-bee1-621bf60aa48b",  # CSP9860 - تيست - باور بانك ($3.00 Max Bid)
    "987600c3-9d2c-4a73-8826-7a7413a086ed",  # CSP9860 4 - تيست - وسادة السيارة ($3.00 Max Bid)
    "c5e25aa8-766e-4cea-a794-9c3c5f832d06",  # CSP9860 4 - تيست - باور بانك ($3.00 Max Bid)
}
TEN_SQUAD_CAMPAIGN_IDS = {
    # CSP9860 (10 AdSquads @ $1.80 Max Bid — #1 Best 3D Video Only)
    "2693cf34-1697-4bd9-b3eb-07ba7a64f6f1",  # وسادة السيارة وينر 2 - 10 أدست - بيد 1.8
    "ee9d1a46-e08e-4a73-a910-f30941356671",  # وسادة السيارة وينر - 10 أدست - بيد 1.8 (بديل 1.6)
    "c89e6059-b826-4cec-8067-71ba2bd45699",  # باور بانك وينر - 10 أدست - بيد 1.8 (بديل 1.5)
    "a9dcd138-a822-4664-bf0a-c36d02233956",  # باور بانك وينر - 10 أدست - بيد 1.8
    "2e87b13a-90ba-4bee-b553-4ebc9c82186e",  # وسادة السيارة سكيلينج - 10 أدست - بيد 1.8
    "ac64cb95-72d4-4043-905a-69fce2968a0a",  # باور بانك وينر - 10 أدست - بيد 1.8 (بديل 1.4)
    # CSP9860 4 (10 AdSquads @ $1.80 Max Bid — #1 Best 3D Video Only)
    "e21421ad-72a1-427e-987d-e6cc49384a31",  # باور بانك وينر - 10 أدست - بيد 1.8 (بديل 1.4)
    "0bacf974-1fc2-4ba4-ac20-42b974911064",  # وسادة السيارة وينر - 10 أدست - بيد 1.8 (بديل 1.5)
    "c2b612d5-acfb-4935-8a5f-1cb090320542",  # وسادة السيارة وينر - 10 أدست - بيد 1.8 (بديل 1.4)
    "22d12681-930c-44a8-91e3-58f112789375",  # باور بانك وينر - 10 أدست - بيد 1.8 (بديل تارقيت كوست)
    "9ff50b24-d5c3-48ba-b74e-7b55e8ffca52",  # وسادة السيارة وينر - 10 أدست - بيد 1.8 (بديل 1.6)
    "828f2e42-96c5-4813-a780-bbc5d976075f",  # وسادة السيارة وينر 2 - 10 أدست - بيد 1.8
}
PERMANENTLY_PAUSED_CPA10_CAMPAIGN_IDS = {
    "87a41d10-b8f3-449c-8a6e-0d80a93b8c9a",  # CSP9860 - وسادة السيارة وينر 2 - 30 أدست 30-10 (CPA > $10)
    "88928e2b-057d-4aed-aafe-f3d73ebc724c",  # CSP9860 - وسادة السيارة وينر - 30 أدست - بيد 1.6 30-10 (CPA > $10)
    "b8507a0f-9e9a-4e11-ad5f-0b4f791445e6",  # CSP9860 - باور بانك وينر - 30 أدست - بيد 1.5 30-10 (CPA > $10)
    "e23e7c95-3f15-4945-9844-8bd73e2d79d3",  # CSP9860 - باور بانك وينر (CPA > $10)
    "f8372cfb-ae46-44d0-bbb4-f39790ecc648",  # CSP9860 - car scaling camp  (CPA > $10)
    "3e82b3ff-64a9-4e74-ba35-7e673150cafe",  # CSP9860 - باور بانك وينر - 30 أدست - بيد 1.4 30-10 (CPA > $10)
    "89aadf62-66f2-4ac8-9106-f92fd83b4277",  # CSP9860 4 - باور بانك وينر - 30 أدست - بيد 1.4 30-10 (CPA > $10)
    "a1121184-41a2-484d-8fa4-67011918698c",  # CSP9860 4 - وسادة السيارة وينر - 30 أدست - بيد 1.5 30-10 (CPA > $10)
    "6cb7f305-66ac-4e9e-8fe0-3cfccd09e3c8",  # CSP9860 4 - وسادة السيارة وينر - 30 أدست - بيد 1.4 30-10 (CPA > $10)
    "dab749f9-2dca-497b-a8c3-39f2201162a7",  # CSP9860 4 - باور بانك وينر - تارقيت كوست (CPA > $10)
    "041280f3-1c1e-43fb-b8d4-860dd6c8957f",  # CSP9860 4 - وسادة السيارة وينر - 30 أدست - بيد 1.6 30-10 (CPA > $10)
    "f82c765e-5b7c-4787-be62-3e07b7602db8",  # CSP9860 4 - وسادة السيارة وينر 2 - 30 أدست 30-10 (CPA > $10)
    "a9dcd138-a822-4664-bf0a-c36d02233956",  # CSP9860 - باور بانك وينر - 10 أدست (1D $11.97 / 0p >= $10)
    "b4ec4259-1868-40f5-ba33-08a2dd0fd578",  # CSP9860 - وسادة السيارة وينر - 30 أدست (2D CPA $15.49 / 3D CPA $11.47 > $11)
    "35ed24f7-2c74-4dea-84bd-d350c6ea7d62",  # CSP9860 4 - وسادة السيارة وينر منزلك (1D $11.35 / 0p >= $10)
    "b454cd6e-ae09-4496-ba36-8345dd544614",  # CSP9860 4 - باور بانك وينر منزلك (1D CPA $13.69 > $11)
    "0bacf974-1fc2-4ba4-ac20-42b974911064",  # CSP9860 4 - وسادة السيارة وينر - 10 أدست (0 active squads)
}

ALLOWED_30X_CAMPAIGN_IDS = (
    {
        "11fa8aef-3a3c-418d-b5c4-8dc7b3ec7ab3",  # CSP9860 - باور بانك وينر 2 - 30 أدست 30-10
        "483f7e57-8a8e-4edb-8b74-7c67eb76419b",  # CSP9860 4 - باور بانك وينر 2 - 30 أدست 30-10
        "35597da7-a911-48f9-9567-7d81bfdebe00",  # CSP9860 - باور بانك وينر - 30 أدست - بيد 1.6 30-10
        "6e187c03-020b-439c-905e-f3d513cd80d6",  # CSP9860 - وسادة السيارة وينر - 30 أدست - بيد 1.5 30-10
        "7fa61e5d-dd41-4ee5-af26-e5c0f773bdef",  # CSP9860 4 - باور بانك وينر - 30 أدست - بيد 1.6 30-10
        "7bffcb77-f2ae-427e-883a-f4e5f249cae9",  # CSP9860 4 - باور بانك وينر - 30 أدست - بيد 1.5 30-10
        "bfe9ec1d-d6c6-416d-a3f0-46c50c2b7a76",  # CSP9860 - باور بانك وينر بديلة - ماكس بيد 1.9 - 02-11
        "0ea95b38-9d67-4e15-973d-86adf3d4104a",  # CSP9860 - وسادة السيارة وينر بديلة - ماكس بيد 1.9 - 02-11
        "51945aea-b2d6-4742-815c-feb85620d8ff",  # CSP9860 4 - وسادة السيارة وينر منزلك بديلة - ماكس بيد 1.9 - 02-11
        "a4518953-cef3-4b77-9af1-20f6724623f8",  # CSP9860 4 - باور بانك وينر منزلك بديلة - ماكس بيد 1.9 - 02-11
        # Newly created from-scratch $1.70 Max Bid campaigns (2026-11-02):
        "a682ed4d-83dd-48a1-bffe-d75dc73d18d2",  # CSP9860 - وسادة السيارة شاملة (22 فيديو) - جديد من الصفر - بيد 1.7 - 02-11
        "c8a2bbba-1c07-4e58-bd6e-b5fbf6112baa",  # CSP9860 - وسادة السيارة سكيلينج (4 أدست) - جديد من الصفر - بيد 1.7 - 02-11
        "521ab366-c2dd-4f1a-beb1-63077655a182",  # CSP9860 - باور بانك وينر (18 أدست) - جديد من الصفر - بيد 1.7 - 02-11
        "f0e66c1c-ac83-459e-97d3-a8c8b0fd6950",  # CSP9860 - باور بانك شاملة (3 أدست) - جديد من الصفر - بيد 1.7 - 02-11
        "35c8668c-903f-4a80-a3af-edb4f8cce5cb",  # CSP9860 - وسادة السيارة وينر (26 أدست) - جديد من الصفر - بيد 1.7 - 02-11
        "81a8160e-878f-4d00-8297-3f4993662048",  # CSP9860 - باور بانك وينر (3 أدست) - جديد من الصفر - بيد 1.7 - 02-11
        "5d768e30-fefb-4cc4-84d4-b7bb973f7798",  # CSP9860 - باور بانك وينر بديلة - جديد من الصفر - بيد 1.7 - 02-11
        "7048ca4c-6e19-4ad1-ad5b-181e2e709bad",  # CSP9860 - وسادة السيارة وينر (3 أدست) - جديد من الصفر - بيد 1.7 - 02-11
        "624a32cf-72b4-410b-8f31-02592f441d51",  # CSP9860 4 - باور بانك وينر 2 (1 أدست) - جديد من الصفر - بيد 1.7 - 02-11
        "35515ff0-e28b-4e8a-b8f7-714c84e98e1b",  # CSP9860 4 - وسادة السيارة وينر منزلك بديلة - جديد من الصفر - بيد 1.7 - 02-11
        "9b90f1d2-7ada-42ff-9e59-7f7757460a46",  # CSP9860 4 - باور بانك وينر أ (2 أدست) - جديد من الصفر - بيد 1.7 - 02-11
        "ad1ecffe-0b2d-4f7a-b5ea-ab5843a11235",  # CSP9860 4 - باور بانك وينر ب (1 أدست) - جديد من الصفر - بيد 1.7 - 02-11
        "f3372321-bb10-4da7-a17c-8ca4f28d3a47",  # CSP9860 4 - وسادة السيارة شاملة (16 فيديو) - جديد من الصفر - بيد 1.7 - 02-11
        "a7b2a8c6-2c1b-43c2-90b9-eb5624e5eaad",  # CSP9860 4 - باور بانك شاملة (3 أدست) - جديد من الصفر - بيد 1.7 - 02-11
    }
    | DEDICATED_SNAP_TEST_CAMPAIGN_IDS
    | TEN_SQUAD_CAMPAIGN_IDS
)
FOUR_SQUAD_CAMPAIGN_IDS = set()
ALLOWED_GULF_CAMPAIGN_IDS = {
    "67de1daa-7ffb-4194-9c44-00dcb7163d4d",  # CSP9860 - باور بانك قطر 18-10
    "efccbe22-8951-49f4-b118-cd67f2cec64b",  # CSP9860 - powe bank oman
    "3fb9b26f-7705-4286-b371-4efcd904670c",  # CSP9860 - وسادة السيارة الكويت 18-10
    "fa5a56ed-56a3-4254-bc2b-fa4add999731",  # CSP9860 - قطر منتج الوساده
    "f7f6b623-46d5-48af-9a21-fb2584106da8",  # CSP9860 - qatar car scaling new 2 text
    "2958697d-af14-4afc-a42a-1de78106b8a1",  # CSP9860 - منتج الكشاف عمان
    "0e6e5109-d336-44f6-8ce3-601bbe2d0262",  # CSP9860 - Copy of kasahf smart+ qatar 3.5
}
TARGET_COST_CAMPAIGN_IDS = {
    "baa69314-505c-4b3a-851e-883c10034935",
    "4e01cc73-1d2f-4198-8cac-ecb5862f29eb",
    "558c5d45-f08e-400c-a499-7668e801c347",
    "dab749f9-2dca-497b-a8c3-39f2201162a7",
}


LOCKED_LOW_BID_CAMPAIGN_MAP = {
    # CSP9860 (All Saudi Copy AdSquads unified to $1.90 per 2026-11-03 User Order; Test campaigns excluded & locked at $3.00)
    "35597da7-a911-48f9-9567-7d81bfdebe00": 1_900_000,
    "b8507a0f-9e9a-4e11-ad5f-0b4f791445e6": 1_900_000,
    "3e82b3ff-64a9-4e74-ba35-7e673150cafe": 1_900_000,
    "88928e2b-057d-4aed-aafe-f3d73ebc724c": 1_900_000,
    "6e187c03-020b-439c-905e-f3d513cd80d6": 1_900_000,
    "b4ec4259-1868-40f5-ba33-08a2dd0fd578": 1_900_000,
    "11fa8aef-3a3c-418d-b5c4-8dc7b3ec7ab3": 1_900_000,
    "2693cf34-1697-4bd9-b3eb-07ba7a64f6f1": 1_900_000,
    "2e87b13a-90ba-4bee-b553-4ebc9c82186e": 1_900_000,
    "a9dcd138-a822-4664-bf0a-c36d02233956": 1_900_000,
    "ac64cb95-72d4-4043-905a-69fce2968a0a": 1_900_000,
    "c89e6059-b826-4cec-8067-71ba2bd45699": 1_900_000,
    "ee9d1a46-e08e-4a73-a910-f30941356671": 1_900_000,
    # CSP9860 4 (All Saudi Copy AdSquads unified to $1.90 per 2026-11-03 User Order; Test campaigns excluded & locked at $3.00)
    "041280f3-1c1e-43fb-b8d4-860dd6c8957f": 1_900_000,
    "a1121184-41a2-484d-8fa4-67011918698c": 1_900_000,
    "6cb7f305-66ac-4e9e-8fe0-3cfccd09e3c8": 1_900_000,
    "7fa61e5d-dd41-4ee5-af26-e5c0f773bdef": 1_900_000,
    "7bffcb77-f2ae-427e-883a-f4e5f249cae9": 1_900_000,
    "89aadf62-66f2-4ac8-9106-f92fd83b4277": 1_900_000,
    "483f7e57-8a8e-4edb-8b74-7c67eb76419b": 1_900_000,
    "e21421ad-72a1-427e-987d-e6cc49384a31": 1_900_000,
    "c2b612d5-acfb-4935-8a5f-1cb090320542": 1_900_000,
    "0bacf974-1fc2-4ba4-ac20-42b974911064": 1_900_000,
    "9ff50b24-d5c3-48ba-b74e-7b55e8ffca52": 1_900_000,
    "22d12681-930c-44a8-91e3-58f112789375": 1_900_000,
    "828f2e42-96c5-4813-a780-bbc5d976075f": 1_900_000,
}

CUSTOM_SQUAD_BID_LOCKS_FILE = Path(__file__).parent / "snap_squad_custom_bid_locks.json"
_custom_locks_cache = {"mtime": 0.0, "data": {}}


def _get_custom_squad_bid_locks() -> dict:
    try:
        if CUSTOM_SQUAD_BID_LOCKS_FILE.exists():
            mt = CUSTOM_SQUAD_BID_LOCKS_FILE.stat().st_mtime
            if mt != _custom_locks_cache["mtime"]:
                _custom_locks_cache["data"] = json.loads(CUSTOM_SQUAD_BID_LOCKS_FILE.read_text(encoding="utf-8"))
                _custom_locks_cache["mtime"] = mt
            return _custom_locks_cache["data"]
    except Exception:
        pass
    return _custom_locks_cache.get("data") or {}


def get_custom_daytime_budget_micro(squad_id: str, today_date_str: str = "") -> int | None:
    if not squad_id:
        return None
    locks = _get_custom_squad_bid_locks()
    lock_date = locks.get("custom_daytime_budgets_date")
    if today_date_str and lock_date and lock_date != today_date_str:
        return None
    by_sq = locks.get("custom_daytime_budgets_micro") or {}
    val = by_sq.get(squad_id)
    if val is not None:
        return int(val)
    return None


def is_dedicated_snap_test_campaign(campaign_id: str = "", campaign_name: str = "", squad_name: str = "") -> bool:
    """أمر المستخدم الصارم (2026-11-01):
    حملات 'تيست' المخصصة للوسادة والباور بانك في الحسابين (تيست - وسادة السيارة / تيست - باور بانك):
    - البيد فيها $3.00 ماكس بيد (3,000,000 micro) ومجدولة للانطلاق صباح الغد، والأتمتة لا تقترب منها.
    - البوت لا يطفي فيها الحملة ولا الأدست أبداً."""
    if campaign_id and campaign_id in DEDICATED_SNAP_TEST_CAMPAIGN_IDS:
        return True
    cn_l = (campaign_name or "").strip().lower()
    if cn_l.startswith("تيست") or "تيست -" in cn_l or "حملة تيست" in cn_l:
        return True
    return False


def get_locked_low_bid_micro(campaign_id: str = "", campaign_name: str = "", squad_name: str = "", squad_id: str = "") -> int | None:
    """أمر المستخدم الصارم الدائم (محدث 2026-11-03):
    - توحيد وإرجاع بيد جميع أدست الكوبي في سناب شات (CSP9860 و CSP9860 4) إلى $1.90 ماكس بيد (1,900,000 micro).
    - حملات التيست المخصصة (تيست - باور بانك / تيست - وسادة السيارة) تبقى على $3.00 ماكس بيد (3,000,000 micro).
    - حملات الخليج تبقى مستثناة ببيداتها المخصصة."""
    locks = _get_custom_squad_bid_locks()
    if squad_id:
        b_id = (locks.get("by_squad_id") or {}).get(squad_id)
        if b_id and 1_000_000 <= int(b_id) <= 4_500_000:
            return int(b_id)
    if campaign_id and squad_name:
        key_cn = f"{campaign_id}|{squad_name.strip().lower()}"
        b_cn = (locks.get("by_camp_and_name") or {}).get(key_cn)
        if b_cn and 1_000_000 <= int(b_cn) <= 4_500_000:
            return int(b_cn)
    if campaign_id:
        b_fc = (locks.get("fixed_campaign_bids") or {}).get(campaign_id)
        if b_fc and 1_000_000 <= int(b_fc) <= 4_500_000:
            return int(b_fc)
    if is_dedicated_snap_test_campaign(campaign_id, campaign_name, squad_name):
        return 3_000_000
    if campaign_id and campaign_id in ALLOWED_GULF_CAMPAIGN_IDS:
        return None
    if is_gulf_034_campaign(campaign_name, squad_name):
        return None
    return 1_900_000


def is_low_bid_experiment_locked(campaign_id: str, campaign_name: str = "", squad_name: str = "", squad_id: str = "") -> bool:
    """أمر المستخدم: المجموعات والحملات ذات البيد الأقل من 1.80 (بما فيها الموزعة عشوائياً 1.65 / 1.55 / 1.70 / 1.67) تبقى كما هي بدون أي تغيير للبيد."""
    if is_dedicated_snap_test_campaign(campaign_id, campaign_name, squad_name):
        return False
    custom_locks = _get_custom_squad_bid_locks()
    if campaign_id and campaign_id in (custom_locks.get("randomized_campaigns") or []):
        return True
    return get_locked_low_bid_micro(campaign_id, campaign_name, squad_name, squad_id) is not None


def is_30x_low_bid_test_campaign_or_squad(campaign_id: str, campaign_name: str = "", squad_name: str = "") -> bool:
    if is_dedicated_snap_test_campaign(campaign_id, campaign_name, squad_name):
        return False
    if squad_name and is_non_copy_test_adsquad(squad_name, campaign_name):
        return False
    if campaign_id in FOUR_SQUAD_CAMPAIGN_IDS:
        return False
    if is_low_bid_experiment_locked(campaign_id, campaign_name, squad_name):
        return True
    cn_l = (campaign_name or "").lower()
    sn_l = (squad_name or "").lower()
    if "30 أدست" in cn_l or "30 ادست" in cn_l or "10 أدست" in cn_l or "10 ادست" in cn_l or "وينر 2" in cn_l:
        return True
    if any(
        k in sn_l
        for k in (
            "1.80", "1.8 max", "1.90", "1.9 max", "2.00", "2.0 max",
        )
    ):
        return True
    return False


def is_scaling_winner_adsquad(adsquad_name: str, campaign_name: str = "") -> bool:
    if is_dedicated_snap_test_campaign("", campaign_name, adsquad_name):
        return False
    nm = (adsquad_name or "").strip().lower()
    cn = (campaign_name or "").strip().lower()
    if "copy" in nm:
        return True
    if re.search(r"#\d+", nm):
        return True
    if any(k in nm for k in ("تيست", "test", "فيديوهات جديدة", "new ads", "flow hook")):
        return False
    if any(
        k in nm
        for k in (
            "1.80 max", "1.8 max", "1.70 max", "1.7 max", "1.60 max", "1.6 max", "1.50 max", "1.5 max", "1.40 max", "1.4 max",
            "1.90 max", "1.9 max", "2.00 max", "2.0 max",
        )
    ):
        return True
    if "30 أدست" in cn or "30 ادست" in cn or "10 أدست" in cn or "10 ادست" in cn or "بيد 1." in cn:
        return True
    return False


def is_pillow_snap_campaign(campaign_name: str) -> bool:
    cn = (campaign_name or "").lower()
    return (
        any(k in cn for k in ("وسادة", "وساده", "تكاية", "تكايه", "car", "armrest", "pillow", "cushion"))
        and "مايباخ" not in cn
        and "maybach" not in cn
    )


def is_powerbank_snap_campaign(campaign_name: str) -> bool:
    cn = (campaign_name or "").lower()
    return any(k in cn for k in ("باور بانك", "باوربانك", "شاحن", "power bank", "powerbank", "voltrx", "pb"))


def is_target_cost_campaign_or_squad(campaign_id: str, campaign_name: str = "", squad_name: str = "", ad_account_id: str = "") -> bool:
    if is_dedicated_snap_test_campaign(campaign_id, campaign_name, squad_name) or campaign_id in TEN_SQUAD_CAMPAIGN_IDS:
        return False
    cn_l = (campaign_name or "").lower()
    sn_l = (squad_name or "").lower()
    if is_30x_low_bid_test_campaign_or_squad(campaign_id, campaign_name, squad_name):
        return False
    if ("max" in sn_l or "ماكس بيد" in sn_l or "بيد 1." in cn_l) and " tc " not in f" {sn_l} ":
        return False
    if campaign_id in ("803324e6-052e-441b-99fb-4d43e56c5c78", "8953b893-82fc-41e3-b1c0-24102bdcbb01", "e23e7c95-3f15-4945-9844-8bd73e2d79d3", "f8372cfb-ae46-44d0-bbb4-f39790ecc648"):
        return " tc " in f" {sn_l} "
    if ad_account_id == SECOND_ACCOUNT_ID:
        return True
    if campaign_id in TARGET_COST_CAMPAIGN_IDS:
        return True
    if "تارقيت كوست" in cn_l or "target cost" in cn_l or " tc " in f" {sn_l} ":
        return True
    return False


SPENDING_TODAY_179_CAMPAIGN_IDS = set()
MAX_SNAP_BID_MICRO = 1_900_000  # بيد جميع أدست الكوبي السعودية الموحد ($1.90 بأمر المستخدم 2026-11-03)
MAX_NON_COPY_TEST_BID_MICRO = 1_900_000  # السقف الأقصى لحملات السعودية العادية ($1.90)


def determine_expected_bid_micro_from_squad(
    squad_name: str, current_bid_micro: int = 0, expect_tc: bool = False, campaign_name: str = "", campaign_id: str = "", squad_id: str = ""
) -> int:
    """
    أمر المستخدم الصارم (محدث 2026-11-03):
    - توحيد وإرجاع بيدات جميع أدست الكوبي في حملات سناب شات إلى $1.90 وتثبيتها.
    """
    locked_b = get_locked_low_bid_micro(campaign_id, campaign_name, squad_name, squad_id)
    if locked_b and 1_000_000 <= int(locked_b) <= 4_500_000:
        return int(locked_b)
    return 1_900_000
HISTORY_FILE = Path(__file__).parent / "paused_history.json"
ACCOUNT_TZ = timezone(timedelta(hours=3))  # GMT+3 (Saudi/Qatar time)

# الكلمات المفتاحية لدول (عمان، قطر، البحرين، الكويت) بالإنجليزية والعربية والاختصارات الشائعة في حسابك
GULF_HIGHER_CPC_KEYWORDS = (
    "oman", "qatar", "qat", "bahrain", "kuwait", "kuwiat", "kwt",
    "عمان", "قطر", "البحرين", "بحرين", "كويت"
)
GULF_COUNTRY_CODES = {"qa", "om", "bh", "kw"}


def get_squad_countries(squad: dict | None) -> set[str]:
    if not squad:
        return set()
    geos = squad.get("targeting", {}).get("geos", [])
    return {str(g.get("country_code", "")).lower() for g in geos if g.get("country_code")}


def is_saudi_squad(squad: dict | None, campaign_name: str = "", adsquad_name: str = "") -> bool:
    countries = get_squad_countries(squad)
    if countries:
        return "sa" in countries
    return not is_gulf_034_campaign(campaign_name, adsquad_name)


def is_gulf_034_campaign(campaign_name: str, adsquad_name: str = "", squad: dict | None = None) -> bool:
    countries = get_squad_countries(squad)
    if countries & GULF_COUNTRY_CODES:
        return True
    combined = f"{campaign_name} {adsquad_name}".lower()
    return any(k in combined for k in GULF_HIGHER_CPC_KEYWORDS)


def get_gulf_simulation_limits(
    campaign_name: str = "",
    adsquad_name: str = "",
    squad: dict | None = None,
    campaign_id: str = "",
) -> tuple[float, float, float]:
    """
    Returns (sim_max_cpa, sq_no_sale_stop, camp_no_sale_stop) for each Gulf country & product
    strictly matching COD Partner Simulation (2026-11-01 User Zero-Loss Rule):
    - OM Flashlight: Sim Safe CPA = $6.69 | Squad No-Sale Stop = $2.80 | Camp No-Sale Stop = $6.69
    - OM Power Bank: Sim Safe CPA = $7.87 | Squad No-Sale Stop = $2.75 | Camp No-Sale Stop = $7.87
    - QA Cushion:    Sim Safe CPA = $10.00 | Squad No-Sale Stop = $3.20 | Camp No-Sale Stop = $10.00
    - KW Cushion:    Sim Safe CPA = $11.48 | Squad No-Sale Stop = $3.20 | Camp No-Sale Stop = $11.48
    - QA Power Bank / Flashlight: Sim Safe CPA = $11.50 | Squad No-Sale Stop = $3.50 | Camp No-Sale Stop = $11.50
    """
    cid = (campaign_id or (squad or {}).get("campaign_id") or "").strip().lower()
    if cid == "2958697d-af14-4afc-a42a-1de78106b8a1":
        return (6.69, 2.80, 6.69)
    if cid == "efccbe22-8951-49f4-b118-cd67f2cec64b":
        return (7.87, 2.75, 7.87)
    if cid == "f7f6b623-46d5-48af-9a21-fb2584106da8":
        return (10.00, 3.20, 10.00)
    if cid == "3fb9b26f-7705-4286-b371-4efcd904670c":
        return (11.48, 3.20, 11.48)
    if cid in ("67de1daa-7ffb-4194-9c44-00dcb7163d4d", "0e6e5109-d336-44f6-8ce3-601bbe2d0262"):
        return (11.50, 3.50, 11.50)

    countries = get_squad_countries(squad)
    combined = f"{campaign_name} {adsquad_name}".lower()
    is_om = ("om" in countries) or any(k in combined for k in ("oman", "عمان", "عُمان"))
    is_kw = ("kw" in countries) or any(k in combined for k in ("kuwait", "kuwiat", "kwt", "كويت"))
    is_qa = ("qa" in countries) or any(k in combined for k in ("qatar", "qat", "قطر"))
    is_flash = any(k in combined for k in ("كشاف", "kasahf", "kashaf", "flash", "led"))
    is_car = any(k in combined for k in ("وسادة", "مسند", "car", "cushion", "armrest"))

    if is_om:
        return (6.69, 2.80, 6.69) if is_flash else (7.87, 2.75, 7.87)
    if is_kw:
        return (11.48, 3.20, 11.48)
    if is_qa:
        return (10.00, 3.20, 10.00) if is_car else (11.50, 3.50, 11.50)
    return (10.00, 3.50, 10.00)


def get_gulf_max_cpc(
    campaign_name: str = "",
    adsquad_name: str = "",
    squad: dict | None = None,
    campaign_id: str = "",
) -> float:
    """يرجع السقف الأقصى لسعر النقرة بدون مبيعة في الخليج ($0.25 لعُمان، و$0.26 للكويت وقطر) بعد صرف $0.80."""
    cid = (campaign_id or (squad or {}).get("campaign_id") or "").strip().lower()
    if cid in ("2958697d-af14-4afc-a42a-1de78106b8a1", "efccbe22-8951-49f4-b118-cd67f2cec64b"):
        return 0.25
    countries = get_squad_countries(squad)
    combined = f"{campaign_name} {adsquad_name}".lower()
    if ("om" in countries) or any(k in combined for k in ("oman", "عمان", "عُمان")):
        return 0.25
    return 0.26


def load_paused_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8-sig"))
            for dict_sec in ("adsquads", "ads", "bid_bumps", "scaling_bid_bumps", "night_bid_reductions", "disabled_videos", "snap_3h_budget_scale"):
                sec_val = data.get(dict_sec)
                if isinstance(sec_val, dict):
                    data[dict_sec] = {k: v for k, v in sec_val.items() if isinstance(v, dict)}
                else:
                    data[dict_sec] = {}
            data.setdefault("creative_media_map", {})
            return data
        except Exception:
            pass
    return {
        "adsquads": {},
        "ads": {},
        "bid_bumps": {},
        "scaling_bid_bumps": {},
        "night_bid_reductions": {},
        "creative_media_map": {},
        "disabled_videos": {},
        "snap_3h_budget_scale": {},
    }


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
    sess = _get_snap_session()
    for attempt in range(4):
        try:
            r = sess.put(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=35,
            )
            if r.ok:
                return r.json()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(1.2)
    return {}


def api_get_paginated(endpoint: str, token: str, list_key: str, item_key: str, max_pages: int = 50) -> list[dict]:
    items = []
    next_url = endpoint
    for _ in range(max_pages):
        if not next_url:
            break
        resp = None
        for attempt in range(3):
            try:
                resp = api_get(next_url, token)
                if resp:
                    break
            except Exception:
                if attempt == 2:
                    resp = None
                else:
                    time.sleep(0.8)
        if not resp:
            break
        for wrap in resp.get(list_key, []):
            obj = wrap.get(item_key)
            if obj:
                items.append(obj)
        next_url = (resp.get("paging") or {}).get("next_link")
    return items


def fetch_account_breakdown_stats(token: str, ad_account_id: str, breakdown: str, days_back: int = 0) -> list[dict]:
    now_local = datetime.now(ACCOUNT_TZ)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = today_start - timedelta(days=days_back)
    end_local = today_start + timedelta(days=1)

    start_str = start_local.isoformat()
    end_str = end_local.isoformat()
    fields = "impressions,swipes,spend,conversion_purchases"
    url = (
        f"/adaccounts/{ad_account_id}/stats?granularity=TOTAL"
        f"&start_time={urllib.parse.quote(start_str)}"
        f"&end_time={urllib.parse.quote(end_str)}"
        f"&breakdown={breakdown}&fields={fields}"
        f"&swipe_up_attribution_window=1_DAY&view_attribution_window=1_DAY"
    )
    try:
        resp = api_get(url, token)
    except Exception as e:
        print(f"⚠️ تخطي استعلام إحصائيات ({breakdown}, days_back={days_back}) بسبب مهلة الاتصال: {e}")
        return []
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


def is_non_copy_test_adsquad(adsquad_name: str, campaign_name: str = "") -> bool:
    """يرجع True فقط لمجموعات التيست الفردية غير الكوبي في حملات السعودية (مثل 24-10 Flow Hook Ads / تيست / test / test adset)."""
    nm = (adsquad_name or "").strip().lower()
    if is_gulf_034_campaign(campaign_name, adsquad_name):
        return False
    if not nm or nm.startswith("base -") or is_scaling_winner_adsquad(adsquad_name, campaign_name):
        return False
    return True


PROVEN_WINNER_MEDIA_IDS = {
    "bb146f4e-7624-4e31-b853-847810e85ffa",
    "1efb6ffc-aced-4da1-8103-da4ce6012aaf",
    "9edae80d-dbd4-4253-b227-8bdcee74c4a6",
    "ad992647-c8c5-4afa-b50a-46a889ecd55c",
    "788db4d2-54aa-409d-8190-54db3ff9bfa4",
    "b7751519-8006-478c-81ec-4223d51be3de",
    "2e23fe3a-8089-42dc-9fa7-7dc462a3ac33",
    "fd8dd57b-ac7d-43e8-8e54-e9f8c621b460",
    "7ad455cb-e919-4abd-9eb9-66260c7dce09",
    "207c2a54-97a8-4445-b7b2-1e8cc46f9b14",
    "89aba96a-6aa7-4a63-9819-0daca54a74e4",
    "f03b4395-e1da-4ed3-8606-7e5e3d86d3e4",
    "04d7ae2c-e2c4-41ee-a5c9-1a6d3ace7f38",
    "7f9ad9fb-aa50-4c59-b485-c058ad5666a0",
    "5f5742e1-5944-43ee-b9aa-a6a2ea85f211",
    "08e86a33-6d7c-4f4a-8e4f-23b7b49072ff",
    "fb6c08bc-8d29-4d05-9735-6e435bcf54ab",
    "0f6a2dd9-9955-41ac-a2e9-21cf7f5eebc6",
    "5d315093-a5af-4ccb-992e-ebb1cce47cd1",
    "42c43ab7-ddaf-45a4-a044-cf5466d11bbb",
    "f01fe69a-ec5b-4360-9897-7b64a42d0fe4",
    "0f2f0a11-671d-4b7a-89e2-9e2094869bbe",
    "0f93d640-5927-4cef-9467-5c735246fa11",
    "f2aa3337-ecae-4ad3-a5a6-ce3e4bd7af92",
    "bff898f4-dacc-44eb-ae2c-bed2f07aa374",
    "ac883791-5538-4bb5-b033-893c1d21c420",
    "30213b6c-56f0-42a1-ad1f-203b850d1151",
    "89630bd3-a9fe-4698-a457-8aebe25e3361",
    "617d9c71-666b-4440-8f07-284857d33d27",
    "198c6ee8-d20a-483d-bf48-8b97fecd4697",
    "4582d635-1313-487f-babe-2decd07d7119",
    "b7161f3e-7d5a-42e1-9861-5bda9b14488d",
    "773a093c-a918-4afa-b369-48c7dba00d6d",
    "bd022732-08ce-43f3-854c-d66f1e472714",
    "fae396e6-c536-43a5-a207-7af64945a4e5",
    "8314f694-5f1b-402a-9060-cfae078f4728",
    "dad71b63-5e86-43fb-bde9-7e53e2da77f3",
    "bf3fa914-1816-4101-8cd7-e3a4ec14d060",
    "39351c6b-3151-4afd-9727-feba78830e11",
    "cfaa82c8-5ae2-46d7-9169-d0da70145a24",
    "9c252a90-aa3f-4e14-bf7b-ccad7a47a0fc",
    "81d72ecc-1235-49bf-a5c1-451051efa0b2",
    "1de62189-1cb3-4527-bc95-6390a9e79b9b",
    "18673fad-9a89-46c6-b229-e2f564586649",
    "736812bb-b0dc-49ca-8a2f-e765c442f04a",
    "be6c1565-a788-49c3-a63f-92ad7a55c813",
    "916b052e-5361-43df-897b-39ed1edddbd7",
    "c16269f0-5667-4cfa-b04c-3d1b2f1a4083",
    "6dd56783-5b12-4156-99da-1c9042f04f82",
    "00dd1a56-241a-49a2-81ee-48b5c09b7a7d",
    "b7858223-a275-46cf-bf54-e7dcd730f4ff",
}


def is_new_test_video(adsquad_name: str, ad_name: str = "", campaign_name: str = "", media_id: str = "") -> bool:
    """يرجع True فقط لمجموعات التيست الفردية غير السكيلينج."""
    sn_l = (adsquad_name or "").strip().lower()
    if not sn_l or sn_l.startswith("base -") or is_scaling_winner_adsquad(adsquad_name, campaign_name):
        return False
    return is_non_copy_test_adsquad(adsquad_name, campaign_name)


def evaluate_adsquad_rule(adsquad_name: str, st: dict, campaign_name: str = "", squad: dict | None = None) -> tuple[bool, str]:
    # بأمر المستخدم (2026-10-30 & 2026-11-01):
    # - حملات 'تيست' المخصصة ومجموعات التيست بدون كوبي لا يُطفأ فيها الأدست أبداً (يُطفأ الفيديو الفردي فقط).
    # - في الأدست الكوبي، أي أدست تصرف $4.00 فأكثر بدون بيعة تُطفأ فوراً، ولو كان سعر البيعة فيها $7.00 فأكثر (CPA >= $7.00) تُطفأ فوراً!
    # - في حملات الخليج (الكويت وقطر وعُمان): تطبق حدود المحاكاة المالية لكل دولة ومنتج بدقة 100%.
    sn_l = (adsquad_name or "").strip().lower()
    c_id_sq = (squad or {}).get("campaign_id", "") if isinstance(squad, dict) else ""
    is_gulf = (c_id_sq in ALLOWED_GULF_CAMPAIGN_IDS) or is_gulf_034_campaign(campaign_name, adsquad_name, squad)
    if not is_gulf and (sn_l.startswith("base -") or is_dedicated_snap_test_campaign("", campaign_name, adsquad_name) or is_non_copy_test_adsquad(adsquad_name, campaign_name)):
        return False, ""

    spend = st.get("spend", 0.0)
    purchases = st.get("purchases", 0)
    cpa = st.get("cpa")
    swipes = st.get("swipes", 0)
    cpc = st.get("cpc", 0.0)
    if is_gulf:
        max_cpa_limit, no_sale_limit, _ = get_gulf_simulation_limits(campaign_name, adsquad_name, squad, c_id_sq)
        g_max_cpc = get_gulf_max_cpc(campaign_name, adsquad_name, squad, c_id_sq)
        if purchases == 0 and spend >= 0.80 and (swipes == 0 or cpc > g_max_cpc):
            return (
                True,
                f"[Gulf AdSquad CPC > ${g_max_cpc:.2f} after $0.80] صرفت المجموعة ${spend:.2f} بدون مبيعات | النقرات: {swipes} | CPC = ${cpc:.3f} (> ${g_max_cpc:.2f})",
            )
        if purchases == 0 and spend >= no_sale_limit:
            return (
                True,
                f"[Gulf AdSquad >= ${no_sale_limit:.2f} No-Sale Stop] صرفت المجموعة ${spend:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة (0 مبيعات)",
            )
        if purchases > 0 and cpa is not None and cpa > max_cpa_limit:
            return (
                True,
                f"[Gulf AdSquad CPA > ${max_cpa_limit:.2f} Sim Rule] صرفت المجموعة ${spend:.2f} | المبيعات: {purchases} | سعر البيعة CPA = ${cpa:.2f} (> ${max_cpa_limit:.2f})",
            )
        return False, ""
    else:
        no_sale_limit = 4.00
        max_cpa_limit = 7.00

    if purchases == 0 and spend >= no_sale_limit:
        return (
            True,
            f"[AdSquad >= ${no_sale_limit:.2f} No-Sale Stop] صرفت المجموعة ${spend:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة (0 مبيعات)",
        )
    if purchases > 0 and cpa is not None and cpa >= max_cpa_limit:
        return (
            True,
            f"[AdSquad CPA >= ${max_cpa_limit:.2f} Rule] صرفت المجموعة ${spend:.2f} | المبيعات: {purchases} | سعر البيعة CPA = ${cpa:.2f} (>= ${max_cpa_limit:.2f})",
        )
    return False, ""


def evaluate_single_ad_rule(
    st: dict,
    campaign_name: str,
    adsquad_name: str,
    squad: dict | None = None,
    ad_name: str = "",
    media_id: str = "",
    campaign_id: str = "",
) -> tuple[bool, str]:
    spend = st["spend"]
    swipes = st["swipes"]
    purchases = st["purchases"]
    cpa = st.get("cpa")
    cpc = st["cpc"]
    is_copy = is_scaling_winner_adsquad(adsquad_name, campaign_name)
    is_test_non_copy = is_new_test_video(adsquad_name, ad_name, campaign_name, media_id) or is_dedicated_snap_test_campaign(campaign_id, campaign_name, adsquad_name)
    is_cross_prod = is_cross_monitored_product_campaign(campaign_name, adsquad_name)
    is_gulf = (campaign_id in ALLOWED_GULF_CAMPAIGN_IDS) or is_gulf_034_campaign(campaign_name, adsquad_name, squad)

    # 1) فحص الفيديوهات الفردية داخل حملات ومجموعات 'تيست' السعودية (أمر المستخدم 2026-11-01):
    # - فقط الفيديوهات التي تصرف $4.00 بدون بيعة تُطفأ
    # - أو سعر البيعة $7.00 فأكثر (CPA >= $7.00) تُطفأ
    # - أو CPC أكثر من $0.27 بعد أول $1.00 صرف بدون مبيعة تُطفأ
    if (not is_copy or is_test_non_copy) and not is_gulf:
        if purchases > 0 and cpa is not None and cpa >= 7.00:
            return True, f"[Test Video Rule: CPA >= $7.00] صرف الفيديو ${spend:.2f} | المبيعات: {purchases} | سعر البيعة CPA = ${cpa:.2f} (>= $7.00)"
        if spend >= 4.00 and purchases == 0:
            return True, f"[Test Video Rule: $4.00 Stop-Loss] صرف الفيديو ${spend:.2f} (>= $4.00) بدون مبيعة"
        if purchases == 0 and spend >= 1.00:
            if swipes == 0:
                return True, f"[Test Video Rule: CPC > $0.27 after $1.00] صرف الفيديو ${spend:.2f} (>= $1.00) بدون أي نقرة (الحد الأقصى للنقرة $0.27)"
            if cpc > 0.27:
                return True, f"[Test Video Rule: CPC > $0.27 after $1.00] صرف الفيديو ${spend:.2f} (>= $1.00) | النقرات: {swipes} | سعر النقرة CPC = ${cpc:.3f} (> $0.27)"
        return False, ""

    # 2) حملات ومجموعات الخليج (الكويت وقطر وعُمان) أو مجموعات Copy السعودية:
    if is_gulf:
        max_cpa, no_sale_stop, _ = get_gulf_simulation_limits(campaign_name, adsquad_name, squad, campaign_id)
        g_max_cpc = get_gulf_max_cpc(campaign_name, adsquad_name, squad, campaign_id)
        if purchases == 0 and spend >= 0.80 and (swipes == 0 or cpc > g_max_cpc):
            return True, f"[Gulf Video CPC > ${g_max_cpc:.2f} after $0.80] صرف الفيديو ${spend:.2f} بدون مبيعات | النقرات: {swipes} | CPC = ${cpc:.3f} (> ${g_max_cpc:.2f})"
        if purchases == 0 and spend >= no_sale_stop:
            return True, f"[Gulf Video >= ${no_sale_stop:.2f} No-Sale Stop] صرف الفيديو ${spend:.2f} (>= ${no_sale_stop:.2f}) بدون مبيعة"
        if purchases > 0 and cpa is not None and cpa > max_cpa:
            return True, f"[Gulf Video CPA > ${max_cpa:.2f} Sim Rule] صرف الفيديو ${spend:.2f} | المبيعات: {purchases} | CPA = ${cpa:.2f} (> ${max_cpa:.2f})"
        return False, ""
    else:
        max_cpa = 6.99
        no_sale_stop = 4.00

    if is_cross_prod and purchases >= 1:
        return False, ""

    if purchases > 0 and cpa is not None and cpa <= max_cpa:
        return False, ""

    if spend >= max_cpa and purchases > 0 and cpa is not None and cpa > max_cpa:
        return True, f"[Copy Video CPA >= $7.00 Rule] صرف الفيديو ${spend} | المبيعات: {purchases} | CPA = ${cpa} (> ${max_cpa:.2f})"

    if not is_cross_prod and spend >= no_sale_stop and purchases == 0:
        return True, f"[Copy Video >= $4.00 Stop-Loss] صرف الفيديو ${spend} (>= $4.00) بدون مبيعة"

    return False, ""


def resolve_video_media_id(creative_id: str, token: str, history: dict) -> str:
    if not creative_id:
        return "unknown"
    cmap = history.setdefault("creative_media_map", {})
    if creative_id in cmap:
        return cmap[creative_id]
    try:
        cr_resp = api_get(f"/creatives/{creative_id}", token)
        cr = cr_resp.get("creatives", [{}])[0].get("creative", {})
        media_id = cr.get("top_snap_media_id") or creative_id
        cmap[creative_id] = media_id
        return media_id
    except Exception:
        return creative_id


def is_cross_monitored_product_campaign(campaign_name: str, adsquad_name: str = "") -> bool:
    combined = f"{campaign_name} {adsquad_name}".lower()
    return any(k in combined for k in ("مايباخ", "maybach", "time x", "timex", "تايم إكس", "دفاتر", "مسخن الرضاعات", "مسخن رضاعات", "warmer", "bottle warmer"))


def is_shutoff_pillow_campaign(campaign_name: str, adsquad_name: str = "") -> bool:
    """بأمر المستخدم (2026-10-29): تفعيل حملة الوسادة القديمة (car scaling camp) في سناب شات."""
    return False


def run_automation(token: str, ad_account_id: str, dry_run: bool = False):
    history = load_paused_history()
    now_str = datetime.now(ACCOUNT_TZ).strftime("%Y-%m-%d %H:%M GMT+3")
    clone_session = None

    all_campaigns_list = api_get_paginated(f"/adaccounts/{ad_account_id}/campaigns?limit=200", token, "campaigns", "campaign")
    camp_names = {}
    active_campaigns = []
    # السماح بحملات الباور بانك والوسادة المعتمدة في الحسابين CSP9860 و CSP9860 4 أو الحملات المنشأة بعد 2026-10-23T18:00:00
    LEGACY_SNAP_CUTOFF = "2026-10-23T18:00:00"
    user_paused_camps_set = set(history.get("user_paused_campaigns") or [])
    for c in all_campaigns_list:
        camp_names[c["id"]] = c.get("name", "")
        # ممنوع منعاً باتاً إعادة تشغيل أي حملة موقوفة تلقائياً
        if c.get("status") == "ACTIVE":
            created_ts = str(c.get("created_at") or "")
            if (
                c["id"] in user_paused_camps_set
                or c["id"] in PERMANENTLY_PAUSED_CPA10_CAMPAIGN_IDS
                or is_shutoff_pillow_campaign(c.get("name", ""))
                or is_cross_monitored_product_campaign(c.get("name", ""))
                or (
                    c["id"] not in ALLOWED_30X_CAMPAIGN_IDS
                    and c["id"] not in ALLOWED_GULF_CAMPAIGN_IDS
                    and not is_gulf_034_campaign(c.get("name", ""))
                    and created_ts
                    and created_ts < LEGACY_SNAP_CUTOFF
                )
            ):
                if not dry_run:
                    c_off = dict(c)
                    c_off["status"] = "PAUSED"
                    api_put(f"/adaccounts/{ad_account_id}/campaigns", token, {"campaigns": [c_off]})
                    print(f"🛑 [إيقاف دائم للحملات القديمة أو الموقوفة بأمر المستخدم في سناب] تم إيقاف حملة '{c.get('name', '')}' ({c['id']}).")
                continue
            active_campaigns.append(c)

    # 1. جلب إحصائيات الـ AdSquads أولاً + تحميل الـ AdSquads النشطة لكل حملة حتى يُحسَب شرط إيقاف الحملة ($15.00) على الـ AdSquads النشطة حالياً فقط بدون المجموعات القديمة الموقوفة!
    squad_stats = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=0)
    squad_stats_map = {st["id"]: st for st in squad_stats}
    squad_stats_2d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=1)
    squad_stats_2d_map = {st["id"]: st for st in squad_stats_2d}
    squad_stats_3d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=2)
    squad_stats_3d_map = {st["id"]: st for st in squad_stats_3d}
    squad_stats_4d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=3) or list(squad_stats_3d)
    squad_stats_4d_map = {st["id"]: st for st in squad_stats_4d}
    squad_stats_5d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=4) or list(squad_stats_4d)
    squad_stats_5d_map = {st["id"]: st for st in squad_stats_5d}
    squad_stats_7d = fetch_account_breakdown_stats(token, ad_account_id, "adsquad", days_back=6) or list(squad_stats_5d)
    squad_stats_7d_map = {st["id"]: st for st in squad_stats_7d}

    camp_stats_1d = {st["id"]: st for st in fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=0)}
    camp_stats_2d = {st["id"]: st for st in fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=1)}
    camp_stats_3d_list = fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=2)
    camp_stats_3d_map = {st["id"]: st for st in camp_stats_3d_list}
    camp_stats_7d = {st["id"]: st for x_st in [fetch_account_breakdown_stats(token, ad_account_id, "campaign", days_back=6)] for st in x_st}
    camp_stats_3d = camp_stats_3d_list

    squad_cache = {}
    camp_squads_cache = {}
    for c in active_campaigns:
        c_id = c["id"]
        try:
            c_squads = api_get_paginated(f"/campaigns/{c_id}/adsquads?limit=1000", token, "adsquads", "adsquad", max_pages=5)
            camp_squads_cache[c_id] = c_squads
            for sq in c_squads:
                squad_cache[sq["id"]] = sq
        except Exception:
            camp_squads_cache[c_id] = []

    # 0. فحص الحملات وإيقاف أي حملة تصرف >= $20.00 بدون مبيعة، وأي حملة يصل فيها سعر البيعة CPA >= $21.00 (أمر المستخدم 2026-10-30):
    sheet_sales_multi = {}
    try:
        from deep_multiwindow_account_auditor import fetch_sheet_multiwindow_sales
        sheet_sales_multi = fetch_sheet_multiwindow_sales() or {}
    except Exception:
        sheet_sales_multi = {}

    kept_active_campaigns = []
    for c in active_campaigns:
        cid_chk = c["id"]
        cname_chk = c.get("name", "")
        if is_dedicated_snap_test_campaign(cid_chk, cname_chk):
            kept_active_campaigns.append(c)
            continue
        act_sq_ids_c = [sq["id"] for sq in camp_squads_cache.get(cid_chk, []) if sq and sq.get("status") == "ACTIVE"]
        is_gulf_camp_chk = (cid_chk in ALLOWED_GULF_CAMPAIGN_IDS) or is_gulf_034_campaign(cname_chk)
        if is_gulf_camp_chk:
            g_sim_cpa, _, g_camp_stop = get_gulf_simulation_limits(cname_chk, campaign_id=cid_chk)
            no_sale_camp_limit = g_camp_stop
            max_cpa_camp_limit = g_sim_cpa
        else:
            # أمر المستخدم الصارم (2026-11-02): أي حملة في سناب صرفت $10.00 بدون بيعة أو سعر البيعة فيها أكثر من $11.00 (CPA > $11.00) يطفئها البوت فوراً!
            no_sale_camp_limit = 10.00
            max_cpa_camp_limit = 11.00
        c_kill_reasons = []
        c_1d_healthy = False
        sp1_camp_check = round(max(
            sum(squad_stats_map.get(sid_x, {}).get("spend", 0.0) for sid_x in act_sq_ids_c),
            camp_stats_1d.get(cid_chk, {}).get("spend", 0.0),
        ), 2)
        for w_lbl_c, sq_map_w, camp_map_w, sh_key_w in (
            ("1D (اليوم)", squad_stats_map, camp_stats_1d, "1d"),
            ("2D (يومين)", squad_stats_2d_map, camp_stats_2d, "2d"),
            ("3D (آخر 3 أيام)", squad_stats_3d_map, camp_stats_3d_map, "3d"),
            ("7D (آخر 7 أيام)", squad_stats_7d_map, camp_stats_7d, "7d"),
        ):
            if is_gulf_camp_chk and sh_key_w != "1d" and sp1_camp_check < 0.25:
                continue
            sp_sq_act = round(sum(sq_map_w.get(sid_x, {}).get("spend", 0.0) for sid_x in act_sq_ids_c), 2)
            sp_camp = round(camp_map_w.get(cid_chk, {}).get("spend", 0.0), 2)
            if sh_key_w == "1d" or (cid_chk not in FOUR_SQUAD_CAMPAIGN_IDS and not is_gulf_camp_chk):
                sp_c = max(sp_sq_act, sp_camp)
            else:
                sp_c = min(sp_sq_act, sp_camp) if (sp_camp > 0 and sp_sq_act > 0) else sp_sq_act
            pu_sq_act = sum(sq_map_w.get(sid_x, {}).get("purchases", 0) for sid_x in act_sq_ids_c)
            pu_camp = camp_map_w.get(cid_chk, {}).get("purchases", 0)
            sh_map_w = sheet_sales_multi.get(sh_key_w, {}) or {}
            pu_sh = sh_map_w.get(cid_chk.lower(), 0)
            pu_c = max(pu_sq_act, pu_camp, pu_sh)
            cpa_c = round(sp_c / pu_c, 2) if pu_c > 0 else None
            if sh_key_w in ("1d", "2d", "3d") and pu_c > 0 and cpa_c is not None and cpa_c <= max_cpa_camp_limit:
                c_1d_healthy = True
            if sh_key_w != "1d" and c_1d_healthy:
                continue
            if pu_c > 0 and cpa_c is not None and cpa_c > max_cpa_camp_limit:
                c_kill_reasons.append(f"{w_lbl_c}: CPA=${cpa_c:.2f} (${sp_c:.2f}/{pu_c} مبيعات > ${max_cpa_camp_limit:.2f})")
            elif pu_c == 0 and sp_c >= no_sale_camp_limit:
                c_kill_reasons.append(f"{w_lbl_c}: صرف ${sp_c:.2f} بدون مبيعات (>= ${no_sale_camp_limit:.2f})")
        if c_kill_reasons:
            if dry_run:
                print(f"🔍 [إيقاف حملة خاسرة] سيتم إيقاف حملة '{cname_chk}' ({cid_chk}) | {' ؛ '.join(c_kill_reasons)}")
            else:
                c_off = dict(c)
                c_off["status"] = "PAUSED"
                api_put(f"/adaccounts/{ad_account_id}/campaigns", token, {"campaigns": [c_off]})
                c["status"] = "PAUSED"
                act_squads_to_pause = [sq for sq in camp_squads_cache.get(cid_chk, []) if sq and sq.get("status") == "ACTIVE"]
                for sq_p in act_squads_to_pause:
                    sq_p["status"] = "PAUSED"
                for i_p in range(0, len(act_squads_to_pause), 20):
                    try:
                        api_put(f"/campaigns/{cid_chk}/adsquads", token, {"adsquads": act_squads_to_pause[i_p:i_p+20]})
                    except Exception:
                        pass
                print(f"🛑 [إيقاف فوري لحملة خاسرة] تم إيقاف حملة '{cname_chk}' ({cid_chk}) و{len(act_squads_to_pause)} أدست بداخلها | {' ؛ '.join(c_kill_reasons)}")
            continue
        kept_active_campaigns.append(c)
    active_campaigns = kept_active_campaigns

    now_local_dt = datetime.now(ACCOUNT_TZ)
    today_d_str = now_local_dt.strftime("%Y-%m-%d")
    yesterday_d_str = (now_local_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    active_camp_ids = {c["id"] for c in active_campaigns}

    no_dup_camps = history.setdefault("no_duplicate_campaigns", {})
    for cid in list(no_dup_camps.keys()):
        cname_chk = camp_names.get(cid, "") or no_dup_camps[cid].get("name", "")
        if is_cross_monitored_product_campaign(cname_chk):
            continue
        is_gulf_c = is_gulf_034_campaign(cname_chk)
        cpa_lim_c = 12.0 if is_gulf_c else 7.0
        sp_lim_c = 10.0
        cst_chk = camp_stats_3d_map.get(cid)
        if not cst_chk:
            del no_dup_camps[cid]
        elif cst_chk.get("purchases", 0) > 0 and cst_chk.get("cpa") is not None and cst_chk["cpa"] <= cpa_lim_c:
            del no_dup_camps[cid]
        elif cst_chk.get("purchases", 0) == 0 and cst_chk.get("spend", 0) < sp_lim_c:
            del no_dup_camps[cid]
    for cid, cname_val in camp_names.items():
        if is_cross_monitored_product_campaign(cname_val):
            no_dup_camps[cid] = {
                "name": cname_val,
                "reason": "Cross-Platform Monitored Product Campaign (1 AdSquad Only)",
                "updated_at": now_str,
            }
    for cid, cst in camp_stats_3d_map.items():
        cname_c = camp_names.get(cid, cid)
        is_gulf_c = is_gulf_034_campaign(cname_c)
        cpa_lim_c = 12.0 if is_gulf_c else 7.0
        sp_lim_c = 10.0
        if (cst["spend"] >= sp_lim_c and cst["purchases"] == 0) or (cst["purchases"] > 0 and cst["cpa"] is not None and cst["cpa"] > cpa_lim_c):
            no_dup_camps[cid] = {
                "name": cname_c,
                "spend_3d": cst["spend"],
                "purchases_3d": cst["purchases"],
                "cpa_3d": cst["cpa"],
                "updated_at": now_str,
            }
    save_paused_history(history)

    spend_by_squad = {st["id"]: st["spend"] for st in squad_stats}
    print(f"📊 عدد المجموعات الإعلانية (Ad Squads) التي صرفت اليوم: {len(squad_stats)} (خلال 3 أيام: {len(squad_stats_3d)})")

    # جلب إحصائيات وإعلانات الفيديوهات مسبقاً لحماية أي مجموعة Copy تحتوي بداخلها على فيديو رابح بكوست ممتاز (<= $8.50) من الإطفاء بسبب الفيديوهات الأخرى!
    ad_stats = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=0)
    ad_stats_map = {st["id"]: st for st in ad_stats}
    ad_stats_2d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=1)
    ad_stats_2d_map = {st["id"]: st for st in ad_stats_2d}
    ad_stats_3d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=2)
    ad_stats_3d_map = {st["id"]: st for st in ad_stats_3d}
    ad_stats_4d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=3) or list(ad_stats_3d)
    ad_stats_4d_map = {st["id"]: st for st in ad_stats_4d}
    ad_stats_5d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=4) or list(ad_stats_4d)
    ad_stats_5d_map = {st["id"]: st for st in ad_stats_5d}
    ad_stats_7d = fetch_account_breakdown_stats(token, ad_account_id, "ad", days_back=6) or list(ad_stats_5d)
    ad_stats_7d_map = {st["id"]: st for st in ad_stats_7d}

    def clean_base_ad_name(raw_nm: str) -> str:
        s = (raw_nm or "").strip()
        while s.lower().startswith("copy of "):
            s = s[8:].strip()
        s = re.sub(r"\s*\[H\d+\].*$", "", s, flags=re.IGNORECASE).strip()
        s = re.sub(r"_hl\d+$", "", s, flags=re.IGNORECASE).strip()
        return s

    ad_cache = {}
    camp_ads_cache = {}
    ads_by_squad_cache = {}
    all_account_ads = []
    for c in active_campaigns:
        c_id = c["id"]
        try:
            c_ads = api_get_paginated(f"/campaigns/{c_id}/ads?limit=1000", token, "ads", "ad", max_pages=3)
            camp_ads_cache[c_id] = c_ads
            for a in c_ads:
                ad_cache[a["id"]] = a
                all_account_ads.append(a)
                ads_by_squad_cache.setdefault(a.get("ad_squad_id"), []).append(a)
            # ضمان تحميل إعلانات جميع المجموعات النشطة بالكامل بسرعة فائقة عبر التوازي
            missing_active_sqs = [
                sq_chk for sq_chk in camp_squads_cache.get(c_id, [])
                if sq_chk.get("status") == "ACTIVE" and len(ads_by_squad_cache.get(sq_chk["id"], [])) < 5
            ]
            if missing_active_sqs:
                import concurrent.futures
                def _fetch_sq_ads(sq_id_val):
                    try:
                        return sq_id_val, api_get_paginated(f"/adsquads/{sq_id_val}/ads?limit=100", token, "ads", "ad", max_pages=2)
                    except Exception:
                        return sq_id_val, []
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                    for sq_id_val, sq_direct_ads in ex.map(_fetch_sq_ads, [s["id"] for s in missing_active_sqs]):
                        for da in sq_direct_ads:
                            if da["id"] not in ad_cache:
                                ad_cache[da["id"]] = da
                                c_ads.append(da)
                                all_account_ads.append(da)
                                ads_by_squad_cache.setdefault(sq_id_val, []).append(da)
        except Exception:
            camp_ads_cache[c_id] = None

    # 1A. (موقوف نهائياً بأمر المستخدم: ممنوع إعادة تشغيل أي مجموعة إعلانية موقوفة في سناب شات — التزام كامل بحملة واحدة وأدست واحدة فقط)
    for s_id, info in []:
        squad = squad_cache.get(s_id, {})
        if not squad:
            continue
        c_id = squad.get("campaign_id", "")
        if c_id not in active_camp_ids:
            continue
        s_name = squad.get("name", info.get("name", ""))
        c_name = camp_names.get(c_id, info.get("campaign", ""))

        is_consolidated_pause = "[Consolidation" in str(info.get("reason", ""))
        if "copy" not in s_name.lower() and not is_consolidated_pause:
            has_no_active_ads = "INVALID_AD_SQUAD_HAS_NO_ACTIVE_ADS" in (squad.get("delivery_status") or [])
            if not has_no_active_ads and squad.get("status") == "PAUSED":
                if dry_run:
                    print(f"🔍 [إعادة تفعيل أدست بدون Copy لاختبار فيديوهاتها] المجموعة: '{s_name}' (حملة: '{c_name}')")
                else:
                    squad["status"] = "ACTIVE"
                    api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                    del history["adsquads"][s_id]
                    save_paused_history(history)
                    print(f"♻️✅ [حماية الأدست بدون Copy] تم إعادة تفعيل المجموعة: '{s_name}' (حملة: '{c_name}') ليتم إطفاء الفيديوهات الخاسرة بداخلها فقط!")
                    time.sleep(0.3)
            elif squad.get("status") == "ACTIVE" and not dry_run:
                del history["adsquads"][s_id]
                save_paused_history(history)
            continue

        paused_at_str = str(info.get("paused_at", ""))
        is_recent_pause = (today_d_str in paused_at_str) or (yesterday_d_str in paused_at_str)
        st1_chk = squad_stats_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None})
        st2_chk = squad_stats_2d_map.get(s_id)
        st3_chk = squad_stats_3d_map.get(s_id)

        # إذا نزفت المجموعة اليوم >= $5 بدون مبيعات، لا نعيد تشغيلها اليوم
        if st1_chk.get("spend", 0.0) >= 5.0 and st1_chk.get("purchases", 0) == 0:
            continue

        st_check = None
        if st1_chk.get("purchases", 0) > 0:
            st_check = st1_chk
        elif not is_consolidated_pause and is_recent_pause and st2_chk and st2_chk.get("purchases", 0) > 0:
            st_check = st2_chk
        elif not is_consolidated_pause and is_recent_pause and st3_chk and st3_chk.get("purchases", 0) > 0:
            st_check = st3_chk
        else:
            continue

        should_pause, _ = evaluate_adsquad_rule(s_name, st_check, c_name, squad)
        should_pause_3d = False
        if st3_chk and st3_chk.get("spend", 0.0) >= 5.0:
            should_pause_3d, _ = evaluate_adsquad_rule(s_name, st3_chk, c_name, squad)
        if not should_pause and not should_pause_3d:
            if squad.get("status") == "PAUSED":
                if dry_run:
                    print(
                        f"🔍 [إعادة تشغيل تلقائي — مبيعة متأخرة] المجموعة: '{s_name}' (حملة: '{c_name}') "
                        f"| الصرف: ${st_check['spend']} | المبيعات: {st_check['purchases']} | CPA = ${st_check['cpa']}"
                    )
                else:
                    squad["status"] = "ACTIVE"
                    api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                    del history["adsquads"][s_id]
                    save_paused_history(history)
                    print(
                        f"♻️🔥 [إعادة تشغيل تلقائي — مبيعة متأخرة] تم إعادة تشغيل المجموعة: '{s_name}' (حملة: '{c_name}') "
                        f"بعد ظهور مبيعات ({st_check['purchases']}) وانخفاض سعر المبيعة إلى CPA = ${st_check['cpa']} (الصرف: ${st_check['spend']})!"
                    )
                    time.sleep(0.3)
            elif squad.get("status") == "ACTIVE" and not dry_run:
                del history["adsquads"][s_id]
                save_paused_history(history)

    all_candidate_sids = (
        set(squad_stats_map.keys())
        | set(squad_stats_2d_map.keys())
        | set(squad_stats_3d_map.keys())
        | set(squad_stats_4d_map.keys())
        | set(squad_stats_5d_map.keys())
        | set(squad_stats_7d_map.keys())
    )
    for s_id in all_candidate_sids:
        squad = squad_cache.get(s_id)
        if not squad or squad.get("status") != "ACTIVE":
            continue
        st_1d = squad_stats_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st_2d = squad_stats_2d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st_3d = squad_stats_3d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st_4d = squad_stats_4d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st_5d = squad_stats_5d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st_7d = squad_stats_7d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        if max(st_1d["spend"], st_2d["spend"], st_3d["spend"], st_4d["spend"], st_5d["spend"], st_7d["spend"]) < 0.70:
            continue

        s_name = squad.get("name", "")
        c_id = squad.get("campaign_id", "")
        if c_id and c_id not in camp_names:
            c_resp = api_get(f"/campaigns/{c_id}", token)
            camp_names[c_id] = c_resp.get("campaigns", [{}])[0].get("campaign", {}).get("name", "")
        c_name = camp_names.get(c_id, "")

        if is_cross_monitored_product_campaign(c_name, s_name):
            # هذه الحملات (وسادة مايباخ ودفاتر TIME X) يراقبها cross_product_monitor.py بمجموع الصرف في سناب وتيكتوك معاً (20$)
            continue
        if is_dedicated_snap_test_campaign(c_id, c_name, s_name):
            # أمر المستخدم الصارم (2026-11-01): حملة 'تيست' البوت لا يطفي فيها الحملة ولا الأدست أبداً (يُطفأ الفيديو الفردي فقط في الخطوة 2)
            continue

        is_gulf_sq = (c_id in ALLOWED_GULF_CAMPAIGN_IDS) or is_gulf_034_campaign(c_name, s_name, squad)
        is_copy_sq = is_scaling_winner_adsquad(s_name, c_name)
        if is_gulf_sq:
            win_cpa_cap, no_sale_cap_sq, _ = get_gulf_simulation_limits(c_name, s_name, squad, c_id)
        else:
            win_cpa_cap = 6.99
            no_sale_cap_sq = 4.00

        copy_high_cpa_hit = False
        copy_high_cpa_reason = ""
        if (is_copy_sq or is_gulf_sq) and not is_non_copy_test_adsquad(s_name, c_name):
            for w_lbl, w_st in (("1D", st_1d), ("2D", st_2d), ("3D", st_3d)):
                if is_gulf_sq and w_lbl != "1D" and st_1d["spend"] < 0.35:
                    continue
                if w_st["purchases"] > 0 and w_st["cpa"] is not None and w_st["cpa"] > win_cpa_cap:
                    if w_lbl != "1D" and st_1d["purchases"] >= 1 and st_1d["cpa"] is not None and st_1d["cpa"] <= win_cpa_cap:
                        continue
                    copy_high_cpa_hit = True
                    copy_high_cpa_reason = (
                        f"[{w_lbl} AdSquad CPA > ${win_cpa_cap:.2f} Sim Rule] صرفت المجموعة ${w_st['spend']:.2f} | المبيعات: {w_st['purchases']} | سعر البيعة CPA = ${w_st['cpa']:.2f} (> ${win_cpa_cap:.2f})"
                    )
                    break
                if w_st["purchases"] == 0 and w_st["spend"] >= no_sale_cap_sq:
                    if w_lbl != "1D" and st_1d["purchases"] >= 1 and st_1d["cpa"] is not None and st_1d["cpa"] <= win_cpa_cap:
                        continue
                    copy_high_cpa_hit = True
                    copy_high_cpa_reason = (
                        f"[{w_lbl} AdSquad >= ${no_sale_cap_sq:.2f} No-Sale Stop] صرفت المجموعة ${w_st['spend']:.2f} (>= ${no_sale_cap_sq:.2f}) بدون أي مبيعة (0 مبيعات)"
                    )
                    break

        is_winner_today = (
            not copy_high_cpa_hit
            and st_1d["purchases"] >= 1
            and st_1d["cpa"] is not None
            and st_1d["cpa"] <= win_cpa_cap
        )
        is_winner_3d = (
            not copy_high_cpa_hit
            and st_3d["purchases"] >= 1
            and st_3d["cpa"] is not None
            and st_3d["cpa"] <= win_cpa_cap
            and st_1d["spend"] < no_sale_cap_sq
            and not (st_2d["purchases"] == 0 and st_2d["spend"] >= no_sale_cap_sq)
        )

        if copy_high_cpa_hit:
            should_pause = True
            reason = copy_high_cpa_reason
        elif is_winner_today:
            # حماية للمجموعة الرابحة اليوم (CPA < $7.00) من الإيقاف بسبب نوافذ قديمة (4D..7D)
            should_pause = False
            reason = ""
        else:
            should_pause_1d, reason_1d = evaluate_adsquad_rule(s_name, st_1d, c_name, squad) if st_1d["spend"] >= 0.70 else (False, "")
            allow_multi_sq = (not is_gulf_sq) or (st_1d["spend"] >= 0.35)
            should_pause_2d, reason_2d = evaluate_adsquad_rule(s_name, st_2d, c_name, squad) if (allow_multi_sq and st_2d["spend"] >= 0.70) else (False, "")
            should_pause_3d, reason_3d = evaluate_adsquad_rule(s_name, st_3d, c_name, squad) if (allow_multi_sq and st_3d["spend"] >= 0.70) else (False, "")
            if is_winner_3d and not should_pause_1d:
                should_pause = False
                reason = ""
            else:
                should_pause_4d, reason_4d = evaluate_adsquad_rule(s_name, st_4d, c_name, squad) if (allow_multi_sq and st_4d["spend"] >= 0.70) else (False, "")
                should_pause_5d, reason_5d = evaluate_adsquad_rule(s_name, st_5d, c_name, squad) if (allow_multi_sq and st_5d["spend"] >= 0.70) else (False, "")
                should_pause_7d, reason_7d = evaluate_adsquad_rule(s_name, st_7d, c_name, squad) if (allow_multi_sq and st_7d["spend"] >= 4.00) else (False, "")
                should_pause = should_pause_1d or should_pause_2d or should_pause_3d or should_pause_4d or should_pause_5d or should_pause_7d
                if should_pause_1d:
                    reason = reason_1d
                elif should_pause_2d:
                    reason = f"[2-Day Audit] {reason_2d}"
                elif should_pause_3d:
                    reason = f"[3-Day Audit] {reason_3d}"
                elif should_pause_4d:
                    reason = f"[4-Day Audit] {reason_4d}"
                elif should_pause_5d:
                    reason = f"[5-Day Audit] {reason_5d}"
                else:
                    reason = f"[7-Day Audit] {reason_7d}"

        if should_pause:
            # بأمر المستخدم: أي أدست تصل CPA >= $7.00 أو تصرف >= $4.00 بدون مبيعة تُطفأ على طول فوراً!
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

    print(f"\n🎬 عدد الفيديوهات الإعلانية (Ads) التي صرفت اليوم: {len(ad_stats)} (وخلال 3 أيام: {len(ad_stats_3d)})")
    for c in active_campaigns:
        c_id = c["id"]

        # تلقائياً: إطفاء أي مجموعة AdSquad نشطة أصبحت فارغة تماماً (0 إعلانات ACTIVE بداخلها) بشرط أن تكون خاسرة، أما لو كانت سليمة (لم تتجاوز $7.00 بدون بيع أو CPA < $8.00) فلا تُطفأ!
        if (
            not dry_run
            and not is_cross_monitored_product_campaign(c.get("name", ""))
            and c_id not in ALLOWED_GULF_CAMPAIGN_IDS
            and not is_gulf_034_campaign(c.get("name", ""))
        ):
            c_ads_list = camp_ads_cache.get(c_id)
            if c_ads_list is not None:
                sq_ids_with_active_ads = {a.get("ad_squad_id") for a in c_ads_list if a.get("status") == "ACTIVE"}
                candidate_empty_squads = []
                for sq in camp_squads_cache.get(c_id, []):
                    if sq.get("status") != "ACTIVE" or sq["id"] in sq_ids_with_active_ads:
                        continue
                    if is_non_copy_test_adsquad(sq.get("name", ""), c.get("name", "")) or not is_scaling_winner_adsquad(sq.get("name", ""), c.get("name", "")):
                        continue
                    sq_start_dt = parse_iso_utc(sq.get("start_time") or "")
                    if sq_start_dt and datetime.now(timezone.utc) < sq_start_dt:
                        continue
                    s1_e = squad_stats_map.get(sq["id"], {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
                    s3_e = squad_stats_3d_map.get(sq["id"], {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
                    if (s1_e.get("purchases", 0) >= 1 and (s1_e.get("cpa") or 99) < 7.00) or (
                        s3_e.get("purchases", 0) >= 1 and (s3_e.get("cpa") or 99) < 7.00
                    ):
                        continue
                    # إذا كانت المجموعة السليمة لم تخسر بعد (< $4.00) لا نطفئها أبداً بسبب 0 إعلانات
                    if (
                        s1_e.get("spend", 0.0) < 4.00
                        and s3_e.get("spend", 0.0) < 4.00
                    ):
                        continue
                    candidate_empty_squads.append(sq)
                empty_active_squads = []
                for esq in candidate_empty_squads:
                    try:
                        direct_ads = api_get_paginated(f"/adsquads/{esq['id']}/ads?limit=100", token, "ads", "ad", max_pages=2)
                        if not direct_ads:
                            continue
                        if any(da.get("status") == "ACTIVE" for da in direct_ads):
                            for da in direct_ads:
                                ad_cache[da["id"]] = da
                                c_ads_list.append(da)
                                ads_by_squad_cache.setdefault(esq["id"], []).append(da)
                            continue
                        empty_active_squads.append(esq)
                    except Exception:
                        continue
                if empty_active_squads:
                    for esq in empty_active_squads:
                        esq["status"] = "PAUSED"
                    for i in range(0, len(empty_active_squads), 20):
                        try:
                            api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": empty_active_squads[i:i+20]})
                        except Exception:
                            pass
                    print(f"🧹 تم إيقاف {len(empty_active_squads)} مجموعة فارغة خاسرة (0 إعلانات نشطة) تلقائياً في حملة '{c.get('name', '')}'")

    # 2A. (موقوف نهائياً بأمر المستخدم: ممنوع إعادة تشغيل الفيديوهات أو المجموعات الموقوفة في سناب شات تلقائياً)
    for ad_id, info in []:
        paused_at_str = str(info.get("paused_at", ""))
        is_recent_pause = (today_d_str in paused_at_str) or (yesterday_d_str in paused_at_str)
        st1_a = ad_stats_map.get(ad_id, {"spend": 0.0, "purchases": 0, "cpa": None})
        st2_a = ad_stats_2d_map.get(ad_id)
        st3_a = ad_stats_3d_map.get(ad_id)

        if st1_a.get("spend", 0.0) >= 5.0 and st1_a.get("purchases", 0) == 0:
            continue

        st_check = None
        if st1_a.get("purchases", 0) > 0 and st1_a.get("cpa") is not None:
            st_check = st1_a
        elif is_recent_pause and st2_a and st2_a.get("purchases", 0) > 0 and st2_a.get("cpa") is not None:
            st_check = st2_a
        elif is_recent_pause and st3_a and st3_a.get("purchases", 0) > 0 and st3_a.get("cpa") is not None:
            st_check = st3_a
        else:
            continue

        ad = ad_cache.get(ad_id, {})
        if not ad:
            continue
        s_id = ad.get("ad_squad_id", "")
        if not s_id:
            continue
        squad = squad_cache.get(s_id, {})
        if not squad:
            continue
        c_id = squad.get("campaign_id", "")
        if c_id not in active_camp_ids:
            continue

        s_name = squad.get("name", "")
        c_name = camp_names.get(c_id, info.get("campaign", ""))
        ad_name = ad.get("name", info.get("name", ""))
        cr_id_chk = ad.get("creative_id", "")
        v_key_chk = history.get("creative_media_map", {}).get(cr_id_chk) or resolve_video_media_id(cr_id_chk, token, history) or cr_id_chk
        disabled_vids = history.get("disabled_videos", {})
        is_disabled_accumulated = (
            f"{c_id}:{v_key_chk}" in disabled_vids
            or f"{c_id}:MEDIA:{v_key_chk}" in disabled_vids
            or f"{c_id}:{cr_id_chk}" in disabled_vids
            or (v_key_chk and f"GLOBAL:MEDIA:{v_key_chk}" in disabled_vids)
            or "Aggregated Video" in str(info.get("reason", ""))
            or "Global Cross-Campaign" in str(info.get("reason", ""))
            or "Global Accumulated" in str(info.get("reason", ""))
            or any(
                isinstance(dv, dict)
                and v_key_chk
                and dv.get("video_media_id") in (v_key_chk, f"MEDIA:{v_key_chk}", f"SA:MEDIA:{v_key_chk}", f"GULF:MEDIA:{v_key_chk}")
                for dv in disabled_vids.values()
            )
        )
        if is_disabled_accumulated:
            # لا نعيد أبداً تشغيل فيديو تم إيقافه بسبب تراكم الصرف عبر المجموعات!
            continue
        max_revive_cpa = 12.0 if is_gulf_034_campaign(c_name, s_name, squad) else 7.0
        # إذا كان للفيديو نفسه في هذه المجموعة مبيعات مربحة لـ 3 أيام (<= $7.00)، نسمح بإعادة تشغيل هذه النسخة الفردية الرابحة حتى لو أُغلقت النسخ الأخرى
        if st3_a and st3_a.get("purchases", 0) > 0 and st3_a.get("cpa") is not None and st3_a["cpa"] > max_revive_cpa:
            continue
        if st_check["cpa"] <= max_revive_cpa:
            if ad.get("status") == "PAUSED":
                if dry_run:
                    print(
                        f"🔍 [إعادة تشغيل فيديو — مبيعة متأخرة] الفيديو: '{ad_name}' (مجموعة: '{s_name}' / حملة: '{c_name}') "
                        f"| الصرف: ${st_check['spend']} | المبيعات: {st_check['purchases']} | CPA = ${st_check['cpa']}"
                    )
                else:
                    ad["status"] = "ACTIVE"
                    api_put(f"/adsquads/{s_id}/ads", token, {"ads": [ad]})
                    del history["ads"][ad_id]
                    # إذا كانت المجموعة الأم قد أُطفئت فقط لأنها أصبحت فارغة (0 إعلانات)، وكان أداؤها لـ 3 أيام مربحاً (<= $7.00)، نعيد تفعيلها بالبيد الآمن
                    st3_sq_rev = squad_stats_3d_map.get(s_id, {"spend": 0.0, "purchases": 0, "cpa": None})
                    if squad.get("status") == "PAUSED" and (
                        "copy" not in s_name.lower()
                        or (st3_sq_rev.get("purchases", 0) >= 1 and st3_sq_rev.get("cpa") is not None and st3_sq_rev["cpa"] <= max_revive_cpa)
                    ):
                        squad["status"] = "ACTIVE"
                        if not is_gulf_034_campaign(c_name, s_name, squad) and (squad.get("bid_micro") or 0) > 1_900_000:
                            squad["bid_micro"] = 1_900_000
                        elif (squad.get("bid_micro") or 0) > 2_350_000:
                            squad["bid_micro"] = 2_250_000
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": [squad]})
                        history.get("adsquads", {}).pop(s_id, None)
                    save_paused_history(history)
                    print(
                        f"♻️🔥 [إعادة تشغيل فيديو — مبيعة متأخرة] تم إعادة تشغيل الفيديو: '{ad_name}' (مجموعة: '{s_name}' / حملة: '{c_name}') "
                        f"بعد ظهور مبيعات ({st_check['purchases']}) بـ CPA = ${st_check['cpa']} (الصرف: ${st_check['spend']})!"
                    )
                    time.sleep(0.3)
            elif ad.get("status") == "ACTIVE" and not dry_run:
                del history["ads"][ad_id]
                save_paused_history(history)

    all_candidate_ad_ids = (
        set(ad_stats_map.keys())
        | set(ad_stats_2d_map.keys())
        | set(ad_stats_3d_map.keys())
        | set(ad_stats_4d_map.keys())
        | set(ad_stats_5d_map.keys())
        | set(ad_stats_7d_map.keys())
    )
    for ad_id in all_candidate_ad_ids:
        st = ad_stats_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st2 = ad_stats_2d_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st3 = ad_stats_3d_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st4 = ad_stats_4d_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st5 = ad_stats_5d_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
        st7 = ad_stats_7d_map.get(ad_id, {"id": ad_id, "spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})

        ad = ad_cache.get(ad_id, {})
        if not ad or ad.get("status") != "ACTIVE":
            continue

        ad_name = ad.get("name", "")
        s_id = ad.get("ad_squad_id")
        squad = squad_cache.get(s_id, {})
        if not squad:
            continue
        s_name = squad.get("name", "")
        c_id = squad.get("campaign_id", "")
        c_name = camp_names.get(c_id, "")

        cr_id_ad = ad.get("creative_id", "")
        ad_media_id = history.get("creative_media_map", {}).get(cr_id_ad) or (
            resolve_video_media_id(cr_id_ad, token, history) if cr_id_ad else ""
        )
        is_test_sq = is_new_test_video(s_name, ad_name, c_name, ad_media_id) or is_dedicated_snap_test_campaign(c_id, c_name, s_name)
        is_gulf_sq = (c_id in ALLOWED_GULF_CAMPAIGN_IDS) or is_gulf_034_campaign(c_name, s_name, squad)
        min_spend_chk = 1.00 if is_test_sq else (0.80 if (is_cross_monitored_product_campaign(c_name, s_name) or is_gulf_sq) else 7.0)
        if is_gulf_sq:
            max_cpa_3d_ind, no_sale_1d_ind, _ = get_gulf_simulation_limits(c_name, s_name, squad, c_id)
            max_cpc_1d_ind = get_gulf_max_cpc(c_name, s_name, squad, c_id)
        else:
            max_cpa_3d_ind = 6.99 if is_test_sq else 7.99
            no_sale_1d_ind = 4.0 if is_test_sq else 7.01
            max_cpc_1d_ind = 0.27 if is_test_sq else 0.28
        has_today_high_cpc_bleed = (
            (is_test_sq or is_gulf_sq)
            and st["spend"] >= min_spend_chk
            and st["purchases"] == 0
            and (st["swipes"] == 0 or st["cpc"] > max_cpc_1d_ind)
        )
        is_proven_3d_winner = (
            st3["purchases"] >= 1
            and st3["cpa"] is not None
            and st3["cpa"] <= max_cpa_3d_ind
            and (st4["purchases"] == 0 or (st4["cpa"] is not None and st4["cpa"] <= max_cpa_3d_ind))
            and (st5["purchases"] == 0 or (st5["cpa"] is not None and st5["cpa"] <= max_cpa_3d_ind))
            and not (st["spend"] >= no_sale_1d_ind and st["purchases"] == 0)
            and not has_today_high_cpc_bleed
            and not (st["purchases"] > 0 and st["cpa"] is not None and st["cpa"] > max_cpa_3d_ind)
            and not (st2["purchases"] > 0 and st2["cpa"] is not None and st2["cpa"] > max_cpa_3d_ind)
        )
        allow_multi_ad = (not is_gulf_sq) or (st["spend"] >= 0.25)
        should_pause, reason = (
            evaluate_single_ad_rule(st, c_name, s_name, squad, ad_name=ad_name, media_id=ad_media_id, campaign_id=c_id)
            if (st["spend"] >= min_spend_chk and not is_proven_3d_winner)
            else (False, "")
        )
        if not should_pause and allow_multi_ad and st2["spend"] >= min_spend_chk and not is_proven_3d_winner:
            should_pause, reason = evaluate_single_ad_rule(st2, c_name, s_name, squad, ad_name=ad_name, media_id=ad_media_id, campaign_id=c_id)
            if should_pause:
                reason = f"[2-Day Audit] {reason}"
        if not should_pause and allow_multi_ad and st3["spend"] >= min_spend_chk:
            should_pause, reason = evaluate_single_ad_rule(st3, c_name, s_name, squad, ad_name=ad_name, media_id=ad_media_id, campaign_id=c_id)
            if should_pause:
                reason = f"[3-Day Audit] {reason}"
        if not should_pause and allow_multi_ad and st4["spend"] >= min_spend_chk:
            should_pause, reason = evaluate_single_ad_rule(st4, c_name, s_name, squad, ad_name=ad_name, media_id=ad_media_id, campaign_id=c_id)
            if should_pause:
                reason = f"[4-Day Audit] {reason}"
        if not should_pause and allow_multi_ad and st5["spend"] >= min_spend_chk:
            should_pause, reason = evaluate_single_ad_rule(st5, c_name, s_name, squad, ad_name=ad_name, media_id=ad_media_id, campaign_id=c_id)
            if should_pause:
                reason = f"[5-Day Audit] {reason}"
        if not should_pause and allow_multi_ad and st7["spend"] >= min_spend_chk and not is_proven_3d_winner:
            should_pause, reason = evaluate_single_ad_rule(st7, c_name, s_name, squad, ad_name=ad_name, media_id=ad_media_id, campaign_id=c_id)
            if should_pause:
                reason = f"[7-Day Audit] {reason}"
        if not should_pause:
            continue

        if dry_run:
            print(f"🔍 [مطابق لشرط الفيديو الفردي] الفيديو: '{ad_name}' (حملة: '{c_name}' / مجموعة: '{s_name}') | {reason}")
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

    # 2B. فحص مجموع صرف ومبيعات كل فيديو تراكمي (مكرر في عدة AdSquads بالـ Media ID الحقيقي) على مستوى الحملة بالكامل (1D + 2D + 3D + 4D + 5D + 7D):
    print(f"\n🧮 فحص مجموع صرف الفيديوهات التراكمية عبر كل الـ AdSquads داخل الحملات النشطة ({len(active_campaigns)} حملة - 1D + 2D + 3D + 4D + 5D + 7D)...")

    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        c_squads = camp_squads_cache.get(c_id, [])
        if (
            not c_squads
            or is_cross_monitored_product_campaign(c_name)
            or is_dedicated_snap_test_campaign(c_id, c_name)
        ):
            continue

        camp_is_gulf = (c_id in ALLOWED_GULF_CAMPAIGN_IDS) or is_gulf_034_campaign(c_name, c_squads[0].get("name", ""), c_squads[0])
        if camp_is_gulf:
            g_sim_cpa, _, g_camp_stop = get_gulf_simulation_limits(c_name, c_squads[0].get("name", ""), c_squads[0], c_id)
            no_sale_limit = g_camp_stop
            max_cpa_limit = g_sim_cpa
            region_label = "الخليج (KW/QA/OM)"
        else:
            camp_is_saudi = is_saudi_squad(c_squads[0], c_name, c_squads[0].get("name", ""))
            no_sale_limit = 7.01 if camp_is_saudi else 10.00
            max_cpa_limit = 7.99 if camp_is_saudi else 12.00
            region_label = "السعودية" if camp_is_saudi else "الخليج"

        c_ads = camp_ads_cache.get(c_id, [])
        if not c_ads:
            continue

        squad_status_map = {sq["id"]: sq.get("status") for sq in c_squads if sq.get("id")}
        relevant_creatives = {
            a.get("creative_id")
            for a in c_ads
            if a.get("creative_id")
            and (
                a.get("id") in ad_stats_map
                or a.get("id") in ad_stats_2d_map
                or a.get("id") in ad_stats_3d_map
                or a.get("id") in ad_stats_4d_map
                or a.get("id") in ad_stats_5d_map
                or a.get("id") in ad_stats_7d_map
                or (a.get("status") == "ACTIVE" and squad_status_map.get(a.get("ad_squad_id")) == "ACTIVE")
            )
        }
        if not relevant_creatives:
            continue

        for cr_id in relevant_creatives:
            resolve_video_media_id(cr_id, token, history)
        # تنظيف أي مفاتيح قديمة مبنية على الاسم (NAME:) أو غير صالحة
        for dk_old in list(history.get("disabled_videos", {}).keys()):
            dv_old = history["disabled_videos"].get(dk_old)
            if not isinstance(dv_old, dict) or ":NAME:" in dk_old or str(dv_old.get("video_media_id", "")).startswith("NAME:"):
                del history["disabled_videos"][dk_old]
        save_paused_history(history)

        video_groups = {}
        for a in c_ads:
            aid_cur = a.get("id")
            if squad_status_map.get(a.get("ad_squad_id")) != "ACTIVE":
                continue
            is_act_in_act_sq = (a.get("status") == "ACTIVE" and squad_status_map.get(a.get("ad_squad_id")) == "ACTIVE")
            if (
                not is_act_in_act_sq
                and aid_cur not in ad_stats_map
                and aid_cur not in ad_stats_2d_map
                and aid_cur not in ad_stats_3d_map
                and aid_cur not in ad_stats_4d_map
                and aid_cur not in ad_stats_5d_map
                and aid_cur not in ad_stats_7d_map
            ):
                continue
            cr_id = a.get("creative_id")
            ad_nm = (a.get("name") or "").strip()
            base_nm = clean_base_ad_name(ad_nm)
            media_id = history["creative_media_map"].get(cr_id) or (resolve_video_media_id(cr_id, token, history) if cr_id else None)
            # التجميع الصارم بناءً على ملف الفيديو الحقيقي نفسه (top_snap_media_id) حصراً — وليس الاسم المكتوب عليه!
            v_key = f"MEDIA:{media_id}" if media_id else f"CR:{cr_id or a['id']}"

            grp = video_groups.setdefault(v_key, {
                "ads": [],
                "active_ads": [],
                "in_copy": False,
                "spend": 0.0,
                "purchases": 0,
                "swipes": 0,
                "spend_2d": 0.0,
                "purchases_2d": 0,
                "spend_3d": 0.0,
                "purchases_3d": 0,
                "spend_4d": 0.0,
                "purchases_4d": 0,
                "spend_5d": 0.0,
                "purchases_5d": 0,
                "spend_7d": 0.0,
                "purchases_7d": 0,
                "sample_name": base_nm or ad_nm,
            })
            grp["ads"].append(a)
            sq_nm_cur = (squad_cache.get(a.get("ad_squad_id"), {}).get("name") or "").lower()
            if "copy" in sq_nm_cur:
                grp["in_copy"] = True
            if a.get("status") == "ACTIVE" and squad_status_map.get(a.get("ad_squad_id")) == "ACTIVE":
                grp["active_ads"].append(a)
            st = ad_stats_map.get(a["id"])
            if st:
                grp["spend"] = round(grp["spend"] + st["spend"], 2)
                grp["purchases"] += st["purchases"]
                grp["swipes"] += st["swipes"]
            st2 = ad_stats_2d_map.get(a["id"])
            if st2:
                grp["spend_2d"] = round(grp["spend_2d"] + st2["spend"], 2)
                grp["purchases_2d"] += st2["purchases"]
            st3 = ad_stats_3d_map.get(a["id"])
            if st3:
                grp["spend_3d"] = round(grp["spend_3d"] + st3["spend"], 2)
                grp["purchases_3d"] += st3["purchases"]
            st4 = ad_stats_4d_map.get(a["id"])
            if st4:
                grp["spend_4d"] = round(grp["spend_4d"] + st4["spend"], 2)
                grp["purchases_4d"] += st4["purchases"]
            st5 = ad_stats_5d_map.get(a["id"])
            if st5:
                grp["spend_5d"] = round(grp["spend_5d"] + st5["spend"], 2)
                grp["purchases_5d"] += st5["purchases"]
            st7 = ad_stats_7d_map.get(a["id"])
            if st7:
                grp["spend_7d"] = round(grp["spend_7d"] + st7["spend"], 2)
                grp["purchases_7d"] += st7["purchases"]

        # الخطوة 1: تقييم كل فيديو تراكمياً عبر جميع النوافذ الست (1D / 2D / 3D / 4D / 5D / 7D) وحساب أفضل CPA له
        for v_key, grp in video_groups.items():
            tot_spend, tot_purch = grp["spend"], grp["purchases"]
            sp2, pu2 = grp["spend_2d"], grp["purchases_2d"]
            sp3, pu3 = grp["spend_3d"], grp["purchases_3d"]
            sp4, pu4 = grp["spend_4d"], grp["purchases_4d"]
            sp5, pu5 = grp["spend_5d"], grp["purchases_5d"]
            sp7, pu7 = grp["spend_7d"], grp["purchases_7d"]
            tot_cpa = round(tot_spend / tot_purch, 2) if tot_purch > 0 else None
            cpa2 = round(sp2 / pu2, 2) if pu2 > 0 else None
            cpa3 = round(sp3 / pu3, 2) if pu3 > 0 else None
            cpa4 = round(sp4 / pu4, 2) if pu4 > 0 else None
            cpa5 = round(sp5 / pu5, 2) if pu5 > 0 else None
            cpa7 = round(sp7 / pu7, 2) if pu7 > 0 else None
            cpas_valid = [x for x in (tot_cpa, cpa2, cpa3, cpa4, cpa5, cpa7) if x is not None]
            grp["best_cpa"] = min(cpas_valid) if cpas_valid else 9999.0

            is_1d_win_v = tot_purch >= 1 and tot_cpa is not None and tot_cpa <= max_cpa_limit
            is_3d_win_v = (
                pu3 >= 1
                and cpa3 is not None
                and cpa3 <= max_cpa_limit
                and not (tot_spend >= no_sale_limit and tot_purch == 0)
                and not (tot_spend >= max_cpa_limit and tot_purch > 0 and tot_cpa is not None and tot_cpa > max_cpa_limit)
                and not (sp2 >= no_sale_limit and pu2 == 0)
                and not (sp2 >= max_cpa_limit and pu2 > 0 and cpa2 is not None and cpa2 > max_cpa_limit)
            )
            is_7d_win_v = (
                pu7 >= 1
                and cpa7 is not None
                and cpa7 <= max_cpa_limit
                and sp3 < no_sale_limit
            )

            kill_video = False
            kill_reason = ""
            for w_lbl, w_sp, w_pu, w_cpa in (
                ("1D (اليوم)", tot_spend, tot_purch, tot_cpa),
                ("2D (يومين)", sp2, pu2, cpa2),
                ("3D (آخر 3 أيام)", sp3, pu3, cpa3),
                ("4D (4 أيام)", sp4, pu4, cpa4),
                ("5D (5 أيام)", sp5, pu5, cpa5),
                ("7D (7 أيام)", sp7, pu7, cpa7),
            ):
                if camp_is_gulf and w_lbl != "1D (اليوم)" and tot_spend < 0.25:
                    continue
                if w_lbl not in ("1D (اليوم)",) and is_1d_win_v:
                    continue
                if w_lbl in ("4D (4 أيام)", "5D (5 أيام)", "7D (7 أيام)") and (is_3d_win_v or is_7d_win_v):
                    continue
                if w_sp >= no_sale_limit and w_pu == 0:
                    kill_video = True
                    kill_reason = (
                        f"[Aggregated Video Stop-Loss {w_lbl} - {region_label}] مجموع صرف الفيديو التراكمي عبر كل الـ AdSquads = "
                        f"${w_sp:.2f} (>= ${no_sale_limit:.2f}) بدون أي مبيعة"
                    )
                    break
                if w_sp >= max_cpa_limit and w_pu > 0 and w_cpa is not None and w_cpa > max_cpa_limit:
                    kill_video = True
                    kill_reason = (
                        f"[Aggregated Video High CPA {w_lbl} - {region_label}] مجموع صرف الفيديو التراكمي = "
                        f"${w_sp:.2f} | المبيعات = {w_pu} | CPA التراكمي = ${w_cpa:.2f} (> ${max_cpa_limit:.2f})"
                    )
                    break
            grp["raw_kill"] = kill_video
            grp["kill_reason"] = kill_reason

        for v_key, grp in video_groups.items():
            tot_spend = grp["spend"]
            tot_purch = grp["purchases"]
            sp2, pu2 = grp["spend_2d"], grp["purchases_2d"]
            sp3, pu3 = grp["spend_3d"], grp["purchases_3d"]
            sp4, pu4 = grp["spend_4d"], grp["purchases_4d"]
            sp5, pu5 = grp["spend_5d"], grp["purchases_5d"]
            sp7, pu7 = grp["spend_7d"], grp["purchases_7d"]
            active_list = [a for a in grp["active_ads"] if a.get("status") == "ACTIVE"]

            raw_v_key = v_key.split(":", 1)[1] if ":" in v_key else v_key
            kill_video = grp["raw_kill"]
            kill_reason = grp["kill_reason"]

            if not kill_video:
                # تنظيف أي قفل قديم للفيديو في هذه الحملة طالما أنه سليم أو ضمن أفضل 5 فيديوهات ناجية في الحملة
                for stale_dk in (f"{c_id}:{v_key}", f"{c_id}:{raw_v_key}", f"{c_id}:SA:{v_key}"):
                    history.get("disabled_videos", {}).pop(stale_dk, None)
                continue

            # تسجيل الفيديو الخاسر تراكمياً في disabled_videos لمنع نسخه أو إعادة تشغيله نهائياً
            if not dry_run:
                history["disabled_videos"][f"{c_id}:{v_key}"] = {
                    "campaign_id": c_id,
                    "campaign_name": c_name,
                    "video_media_id": raw_v_key,
                    "sample_ad_name": grp["sample_name"],
                    "closed_instances": len(active_list),
                    "total_spend": max(tot_spend, sp2, sp3),
                    "total_purchases": max(tot_purch, pu2, pu3),
                    "reason": kill_reason,
                    "disabled_at": now_str,
                }
                save_paused_history(history)

            if not active_list:
                continue

            # إيقاف النسخ الخاسرة فقط للفيديو التراكمي مع حماية النسخ الفردية الرابحة (CPA <= max_cpa_limit) فقط
            targets_to_pause = []
            for a_cand in active_list:
                aid_c = a_cand.get("id")
                st1_c = ad_stats_map.get(aid_c, {"spend": 0.0, "purchases": 0, "cpa": None})
                st3_c = ad_stats_3d_map.get(aid_c, {"spend": 0.0, "purchases": 0, "cpa": None})
                if (st1_c["purchases"] >= 1 and (st1_c["cpa"] or 99) <= max_cpa_limit) or (
                    st3_c["purchases"] >= 1 and (st3_c["cpa"] or 99) <= max_cpa_limit and (st1_c["purchases"] == 0 or (st1_c["cpa"] or 99) <= max_cpa_limit)
                ):
                    continue
                targets_to_pause.append(a_cand)
            if not targets_to_pause:
                continue

            if dry_run:
                print(
                    f"🔍 [حذف/إيقاف الفيديو التراكمي بالكامل] الحملة: '{c_name}' | الفيديو: '{grp['sample_name']}' ({v_key}) "
                    f"| النسخ التي ستُغلق: {len(targets_to_pause)} | {kill_reason}"
                )
            else:
                print(
                    f"🗑️ جاري إيقاف الفيديو التراكمي '{grp['sample_name']}' ({v_key}) في جميع الـ {len(targets_to_pause)} مجموعة نشطة في حملة '{c_name}' | {kill_reason}"
                )
                ads_by_squad = {}
                for a in targets_to_pause:
                    a["status"] = "PAUSED"
                    ads_by_squad.setdefault(a["ad_squad_id"], []).append(a)

                closed_cnt = 0
                for sq_id, sq_ads in ads_by_squad.items():
                    # منع تصفير الإعلانات النشطة بالكامل داخل أي أدست نشطة (حتى لا تتحول إلى 0 Active Ads وتُطفأ بالخطأ)
                    all_sq_ads_in_cache = [ax for ax in c_ads if ax.get("ad_squad_id") == sq_id]
                    remaining_active = [ax for ax in all_sq_ads_in_cache if ax.get("status") == "ACTIVE"]
                    if not remaining_active and sq_ads:
                        # أبقِ إعلاناً واحداً على الأقل نشطاً داخل المجموعة السليمة إلى أن يتم حقن الفيديوهات الجديدة
                        keep_one = sq_ads.pop(0)
                        keep_one["status"] = "ACTIVE"
                    if not sq_ads:
                        continue
                    try:
                        api_put(f"/adsquads/{sq_id}/ads", token, {"ads": sq_ads})
                        for a in sq_ads:
                            history["ads"][a["id"]] = {
                                "name": a.get("name", ""),
                                "campaign": c_name,
                                "adsquad": squad_cache.get(sq_id, {}).get("name", sq_id),
                                "reason": kill_reason,
                                "paused_at": now_str,
                            }
                            closed_cnt += 1
                        time.sleep(0.2)
                    except Exception as e:
                        print(f"   ⚠️ خطأ أثناء إغلاق الفيديو في المجموعة {sq_id}: {e}")

                history["disabled_videos"][f"{c_id}:{v_key}"]["closed_instances"] = closed_cnt
                save_paused_history(history)
                print(f"   ✅ تم إيقاف الفيديو التراكمي بالكامل ({closed_cnt} نسخة) بنجاح!")

    # 2B-Global. فحص مجموع صرف ومبيعات الفيديوهات التراكمية على مستوى الحساب بالكامل بناءً على معرف الفيديو الحقيقي (top_snap_media_id) حصراً:
    print("\n🌐 فحص مجموع صرف الفيديوهات التراكمية على مستوى الحساب بالكامل بمعرف الفيديو الحقيقي (top_snap_media_id) عبر جميع الحملات والمجموعات النشطة (1D + 2D + 3D + 4D + 5D + 7D)...")
    global_video_groups = {}
    ads_for_global_audit = all_account_ads if all_account_ads else list(ad_cache.values())
    for a in ads_for_global_audit:
        ad_id = a.get("id")
        sq_id = a.get("ad_squad_id")
        sq_obj = squad_cache.get(sq_id, {})
        c_id = sq_obj.get("campaign_id", "")
        if c_id not in active_camp_ids or sq_obj.get("status") != "ACTIVE":
            continue
        c_name = camp_names.get(c_id, "")
        s_name = sq_obj.get("name", "")
        if (
            is_cross_monitored_product_campaign(c_name, s_name)
            or is_dedicated_snap_test_campaign(c_id, c_name, s_name)
            or c_id in ALLOWED_GULF_CAMPAIGN_IDS
            or is_gulf_034_campaign(c_name, s_name, sq_obj)
        ):
            continue
        is_Active_Now = (
            c_id in active_camp_ids
            and sq_obj.get("status") == "ACTIVE"
            and a.get("status") == "ACTIVE"
        )
        has_any_stats = (
            ad_id in ad_stats_map
            or ad_id in ad_stats_2d_map
            or ad_id in ad_stats_3d_map
            or ad_id in ad_stats_4d_map
            or ad_id in ad_stats_5d_map
            or ad_id in ad_stats_7d_map
        )
        if not is_Active_Now and not has_any_stats:
            continue

        cr_id = a.get("creative_id")
        if cr_id and cr_id not in history.get("creative_media_map", {}):
            resolve_video_media_id(cr_id, token, history)
        media_id = history.get("creative_media_map", {}).get(cr_id)
        raw_nm = (a.get("name") or "").strip()
        clean_nm = clean_base_ad_name(raw_nm)
        ad_is_saudi = is_saudi_squad(sq_obj, c_name, s_name)
        reg_prefix = "SA" if ad_is_saudi else "GULF"

        v_key_glob = f"{reg_prefix}:MEDIA:{media_id}" if media_id else f"{reg_prefix}:CR:{cr_id or ad_id}"
        g_grp = global_video_groups.setdefault(v_key_glob, {
            "is_saudi": ad_is_saudi,
            "active_ads": [],
            "campaigns": set(),
            "spend_1d": 0.0,
            "purchases_1d": 0,
            "spend_2d": 0.0,
            "purchases_2d": 0,
            "spend_3d": 0.0,
            "purchases_3d": 0,
            "spend_4d": 0.0,
            "purchases_4d": 0,
            "spend_5d": 0.0,
            "purchases_5d": 0,
            "spend_7d": 0.0,
            "purchases_7d": 0,
            "sample_name": clean_nm or raw_nm,
        })
        if c_name:
            g_grp["campaigns"].add(c_name)
        if is_Active_Now:
            g_grp["active_ads"].append((c_id, c_name, a))
        st1_a = ad_stats_map.get(ad_id)
        if st1_a:
            g_grp["spend_1d"] = round(g_grp["spend_1d"] + st1_a["spend"], 2)
            g_grp["purchases_1d"] += st1_a["purchases"]
        st2_a = ad_stats_2d_map.get(ad_id)
        if st2_a:
            g_grp["spend_2d"] = round(g_grp["spend_2d"] + st2_a["spend"], 2)
            g_grp["purchases_2d"] += st2_a["purchases"]
        st3_a = ad_stats_3d_map.get(ad_id)
        if st3_a:
            g_grp["spend_3d"] = round(g_grp["spend_3d"] + st3_a["spend"], 2)
            g_grp["purchases_3d"] += st3_a["purchases"]
        st4_a = ad_stats_4d_map.get(ad_id)
        if st4_a:
            g_grp["spend_4d"] = round(g_grp["spend_4d"] + st4_a["spend"], 2)
            g_grp["purchases_4d"] += st4_a["purchases"]
        st5_a = ad_stats_5d_map.get(ad_id)
        if st5_a:
            g_grp["spend_5d"] = round(g_grp["spend_5d"] + st5_a["spend"], 2)
            g_grp["purchases_5d"] += st5_a["purchases"]
        st7_a = ad_stats_7d_map.get(ad_id)
        if st7_a:
            g_grp["spend_7d"] = round(g_grp["spend_7d"] + st7_a["spend"], 2)
            g_grp["purchases_7d"] += st7_a["purchases"]

    for v_key_glob, g_grp in global_video_groups.items():
        active_glob_list = [
            (cid_x, cnm_x, ax) for (cid_x, cnm_x, ax) in g_grp["active_ads"] if ax.get("status") == "ACTIVE"
        ]
        if not active_glob_list:
            continue
        is_sa_glob = g_grp["is_saudi"]
        global_no_sale_limit = 7.01 if is_sa_glob else 10.00
        global_max_cpa_limit = 7.99 if is_sa_glob else 12.00
        g_sp1, g_pu1 = g_grp["spend_1d"], g_grp["purchases_1d"]
        g_sp2, g_pu2 = g_grp["spend_2d"], g_grp["purchases_2d"]
        g_sp3, g_pu3 = g_grp["spend_3d"], g_grp["purchases_3d"]
        g_sp4, g_pu4 = g_grp["spend_4d"], g_grp["purchases_4d"]
        g_sp5, g_pu5 = g_grp["spend_5d"], g_grp["purchases_5d"]
        g_sp7, g_pu7 = g_grp["spend_7d"], g_grp["purchases_7d"]
        if max(g_sp1, g_sp2, g_sp3, g_sp4, g_sp5, g_sp7) < global_no_sale_limit:
            continue
        g_cpa1 = round(g_sp1 / g_pu1, 2) if g_pu1 > 0 else None
        g_cpa2 = round(g_sp2 / g_pu2, 2) if g_pu2 > 0 else None
        g_cpa3 = round(g_sp3 / g_pu3, 2) if g_pu3 > 0 else None
        g_cpa4 = round(g_sp4 / g_pu4, 2) if g_pu4 > 0 else None
        g_cpa5 = round(g_sp5 / g_pu5, 2) if g_pu5 > 0 else None
        g_cpa7 = round(g_sp7 / g_pu7, 2) if g_pu7 > 0 else None

        g_is_1d_win = g_pu1 >= 1 and g_cpa1 is not None and g_cpa1 <= global_max_cpa_limit
        g_is_3d_win = (
            g_pu3 >= 1
            and g_cpa3 is not None
            and g_cpa3 <= global_max_cpa_limit
            and not (g_sp1 >= global_no_sale_limit and g_pu1 == 0)
            and not (g_sp1 >= global_max_cpa_limit and g_pu1 > 0 and g_cpa1 is not None and g_cpa1 > global_max_cpa_limit)
            and not (g_sp2 >= global_no_sale_limit and g_pu2 == 0)
            and not (g_sp2 >= global_max_cpa_limit and g_pu2 > 0 and g_cpa2 is not None and g_cpa2 > global_max_cpa_limit)
        )
        g_is_7d_win = g_pu7 >= 1 and g_cpa7 is not None and g_cpa7 <= global_max_cpa_limit and g_sp3 < global_no_sale_limit

        g_kill = False
        g_reason = ""
        for gw_lbl, gw_sp, gw_pu, gw_cpa in (
            ("1D (اليوم)", g_sp1, g_pu1, g_cpa1),
            ("2D (يومين)", g_sp2, g_pu2, g_cpa2),
            ("3D (آخر 3 أيام)", g_sp3, g_pu3, g_cpa3),
            ("4D (4 أيام)", g_sp4, g_pu4, g_cpa4),
            ("5D (5 أيام)", g_sp5, g_pu5, g_cpa5),
            ("7D (7 أيام)", g_sp7, g_pu7, g_cpa7),
        ):
            if gw_lbl not in ("1D (اليوم)",) and g_is_1d_win:
                continue
            if gw_lbl in ("4D (4 أيام)", "5D (5 أيام)", "7D (7 أيام)") and (g_is_3d_win or g_is_7d_win):
                continue
            if gw_sp >= global_no_sale_limit and gw_pu == 0:
                g_kill = True
                g_reason = f"[Global Accumulated Video Stop-Loss {gw_lbl}] مجموع صرف الفيديو التراكمي على مستوى الحساب = ${gw_sp:.2f} (>= ${global_no_sale_limit:.2f}) بدون أي مبيعة"
                break
            if gw_sp >= global_max_cpa_limit and gw_pu > 0 and gw_cpa is not None and gw_cpa > global_max_cpa_limit:
                g_kill = True
                g_reason = f"[Global Accumulated Video High CPA {gw_lbl}] مجموع صرف الفيديو التراكمي على مستوى الحساب = ${gw_sp:.2f} | المبيعات = {gw_pu} | CPA التراكمي = ${gw_cpa:.2f} (> ${global_max_cpa_limit:.2f})"
                break

        if not g_kill:
            continue

        # نغلق الفيديو التراكمي الخاسر على مستوى الحساب في جميع الحملات باستثناء النسخ الفردية الرابحة فقط
        g_targets = []
        for cid_x, cnm_x, ax in active_glob_list:
            aid_x = ax.get("id")
            a_st1_x = ad_stats_map.get(aid_x, {"spend": 0.0, "purchases": 0, "cpa": None})
            a_st3_x = ad_stats_3d_map.get(aid_x, {"spend": 0.0, "purchases": 0, "cpa": None})
            if (a_st1_x["purchases"] >= 1 and (a_st1_x["cpa"] or 99) <= global_max_cpa_limit) or (
                a_st3_x["purchases"] >= 1 and (a_st3_x["cpa"] or 99) <= global_max_cpa_limit
            ):
                continue
            g_targets.append((cid_x, cnm_x, ax))

        if not g_targets:
            continue

        if dry_run:
            print(f"🔍 [إيقاف فيديو تراكمي خاسر على مستوى الحساب بالكامل] الفيديو: '{g_grp['sample_name']}' ({v_key_glob}) | النسخ المستهدفة: {len(g_targets)} | {g_reason}")
        else:
            ads_by_sq_glob = {}
            for c_id_g, c_name_g, a_g in g_targets:
                a_g["status"] = "PAUSED"
                ads_by_sq_glob.setdefault((c_id_g, c_name_g, a_g["ad_squad_id"]), []).append(a_g)
            closed_g = 0
            for (c_id_g, c_name_g, sq_id_g), sq_ads_g in ads_by_sq_glob.items():
                try:
                    api_put(f"/adsquads/{sq_id_g}/ads", token, {"ads": sq_ads_g})
                    for a_g in sq_ads_g:
                        history["ads"][a_g["id"]] = {
                            "name": a_g.get("name", ""),
                            "campaign": c_name_g,
                            "adsquad": squad_cache.get(sq_id_g, {}).get("name", sq_id_g),
                            "reason": g_reason,
                            "paused_at": now_str,
                        }
                        closed_g += 1
                    raw_glob_mid = v_key_glob.split(":")[-1]
                    history["disabled_videos"][f"{c_id_g}:MEDIA:{raw_glob_mid}"] = {
                        "campaign_id": c_id_g,
                        "campaign_name": c_name_g,
                        "video_media_id": raw_glob_mid,
                        "sample_ad_name": g_grp["sample_name"],
                        "closed_instances": len(sq_ads_g),
                        "total_spend": max(g_sp1, g_sp2, g_sp3, g_sp4, g_sp5, g_sp7),
                        "total_purchases": max(g_pu1, g_pu2, g_pu3, g_pu4, g_pu5, g_pu7),
                        "reason": g_reason,
                        "disabled_at": now_str,
                    }
                    time.sleep(0.2)
                except Exception as e:
                    print(f"   ⚠️ خطأ أثناء إغلاق الفيديو التراكمي في المجموعة {sq_id_g}: {e}")
            save_paused_history(history)
            print(f"🗑️✅ [إيقاف فيديو تراكمي خاسر على مستوى الحساب] تم إيقاف '{g_grp['sample_name']}' في {closed_g} مجموعة بالكامل | {g_reason}")

    # 2C. تنظيف المجموعات الإعلانية الخاسرة الميتة التي أصبحت بدون أي فيديو نشط (مع عدم إطفاء أي مجموعة Copy سليمة لم تخسر بعد!)
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        if is_dedicated_snap_test_campaign(c_id, c_name) or c_id in ALLOWED_GULF_CAMPAIGN_IDS or is_gulf_034_campaign(c_name):
            continue
        c_squads = camp_squads_cache.get(c_id)
        if (
            not c_squads
            or is_cross_monitored_product_campaign(c_name)
            or is_dedicated_snap_test_campaign(c_id, c_name)
            or c_id in ALLOWED_GULF_CAMPAIGN_IDS
            or is_gulf_034_campaign(c_name)
        ):
            continue
        active_sq_ids_after_2b = {
            a.get("ad_squad_id") for a in camp_ads_cache.get(c_id, []) if a.get("status") == "ACTIVE"
        }
        candidate_dead = []
        for sq in c_squads:
            if sq.get("status") != "ACTIVE" or sq["id"] in active_sq_ids_after_2b:
                continue
            if is_non_copy_test_adsquad(sq.get("name", ""), c_name) or not is_scaling_winner_adsquad(sq.get("name", ""), c_name):
                continue
            sq_start_d = parse_iso_utc(sq.get("start_time") or "")
            if sq_start_d and datetime.now(timezone.utc) < sq_start_d:
                continue
            s1_d = squad_stats_map.get(sq["id"], {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            s3_d = squad_stats_3d_map.get(sq["id"], {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            if (s1_d.get("purchases", 0) >= 1 and (s1_d.get("cpa") or 99) < 7.00) or (
                s3_d.get("purchases", 0) >= 1 and (s3_d.get("cpa") or 99) < 7.00
            ):
                continue
            # ممنوع منعاً باتاً إطفاء أي أدست Copy سليمة (< $4.00) بسبب 0 إعلانات!
            if (
                s1_d.get("spend", 0.0) < 4.00
                and s3_d.get("spend", 0.0) < 4.00
            ):
                continue
            candidate_dead.append(sq)
        dead_to_pause = []
        for dsq in candidate_dead:
            try:
                direct_ads = api_get_paginated(f"/adsquads/{dsq['id']}/ads?limit=100", token, "ads", "ad", max_pages=2)
                if not direct_ads or any(da.get("status") == "ACTIVE" for da in direct_ads):
                    continue
                dead_to_pause.append(dsq)
            except Exception:
                continue
        if dead_to_pause and not dry_run:
            for sq in dead_to_pause:
                sq["status"] = "PAUSED"
            for i in range(0, len(dead_to_pause), 20):
                try:
                    api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": dead_to_pause[i:i+20]})
                except Exception:
                    pass
            print(f"🧹 تم إيقاف {len(dead_to_pause)} مجموعة ميتة خاسرة (0 إعلانات نشطة) تلقائياً في حملة '{c_name}'")

    # 2D. شرط عدم تجاوز 4 مجموعات أدست لحملات الـ 4 أدست، و 30 أدست لحملات الـ 30 أدست الموازية (أمر المستخدم 2026-10-30):
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        c_squads = camp_squads_cache.get(c_id, [])
        if (
            not c_squads
            or is_cross_monitored_product_campaign(c_name)
            or is_dedicated_snap_test_campaign(c_id, c_name)
            or c_id in ALLOWED_GULF_CAMPAIGN_IDS
            or is_gulf_034_campaign(c_name)
        ):
            continue
        max_copy_for_camp = 10 if c_id in TEN_SQUAD_CAMPAIGN_IDS else (4 if c_id in FOUR_SQUAD_CAMPAIGN_IDS else 30)

        def _squad_priority_key(sq_item: dict):
            sid_k = sq_item["id"]
            st1_k = squad_stats_map.get(sid_k, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0})
            st3_k = squad_stats_3d_map.get(sid_k, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0})
            p3 = st3_k.get("purchases", 0)
            p1 = st1_k.get("purchases", 0)
            sp1 = st1_k.get("spend", 0.0)
            cpc1 = st1_k.get("cpc", 0.0) or 9.99
            nm_k = sq_item.get("name", "")
            m_num = re.search(r"#(\d+)", nm_k)
            num_idx = int(m_num.group(1)) if m_num else 999
            sq_st_dt = parse_iso_utc(sq_item.get("start_time") or "")
            is_future_sched = 1 if (sq_st_dt and datetime.now(timezone.utc) < sq_st_dt) else 0
            return (
                1 if (p3 > 0 and (st3_k.get("cpa") or 999) < 7.00) else 0,
                1 if (p1 > 0 and (st1_k.get("cpa") or 999) < 7.00) else 0,
                is_future_sched,
                p3 + p1,
                1 if (sp1 > 0 and cpc1 <= 0.28) else 0,
                round(sp1, 2),
                -num_idx,
            )

        wrong_strat_to_pause = []

        active_copy_squads = [
            sq for sq in c_squads
            if sq.get("status") == "ACTIVE"
            and is_scaling_winner_adsquad(sq.get("name", ""), c_name)
        ]
        extra_non_copy_to_pause = []

        excess_to_pause = list(wrong_strat_to_pause) + list(extra_non_copy_to_pause)
        kept_copy = list(active_copy_squads)
        if len(active_copy_squads) > max_copy_for_camp:
            all_sorted = sorted(active_copy_squads, key=_squad_priority_key, reverse=True)
            kept_copy = all_sorted[:max_copy_for_camp]
            for sq_cand in all_sorted[max_copy_for_camp:]:
                excess_to_pause.append(sq_cand)

        if excess_to_pause:
            if dry_run:
                print(
                    f"🔍 [شرط سقف {max_copy_for_camp} أدست فقط] سيتم إيقاف {len(excess_to_pause)} مجموعة في حملة '{c_name}' للإبقاء على {len(kept_copy)} مجموعة فقط (<= {max_copy_for_camp})."
                )
            else:
                for sq_ex in excess_to_pause:
                    sq_ex["status"] = "PAUSED"
                    history["adsquads"][sq_ex["id"]] = {
                        "name": sq_ex.get("name", ""),
                        "campaign": c_name,
                        "reason": f"[Max {max_copy_for_camp} AdSquads Rule] إيقاف نسخ الكوبي الزائدة والإبقاء على {max_copy_for_camp} أدست فقط لكل حملة",
                        "paused_at": now_str,
                    }
                for i in range(0, len(excess_to_pause), 20):
                    try:
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": excess_to_pause[i:i+20]})
                    except Exception:
                        pass
                save_paused_history(history)
                print(
                    f"🛑 [شرط سقف {max_copy_for_camp} أدست فقط] تم إيقاف {len(excess_to_pause)} مجموعة في حملة '{c_name}' (النشط الآن: {len(kept_copy)} <= {max_copy_for_camp})."
                )

    # أمر المستخدم الصارم (2026-11-02):
    # غيت هاب لا تعطيه أوامر بيد أنه يطلع وينزل، فقط يوقف الحملات والمجموعات والفيديوهات المتعدية للرقم المحدد!
    if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("STOP_LOSS_PAUSE_ONLY") == "1":
        if not dry_run:
            save_paused_history(history)
        print("\n🛡️ [وضع الإيقاف الحصري على GitHub Actions] تم فحص وإيقاف الحملات والمجموعات والفيديوهات المتعدية للأرقام المحددة فقط بدون أي تعديل على البيد أو البدجت.")
        return

    # 3. نظام تجميد البيد الكامل (ممنوع رفع أو إنقاص البيد تلقائياً) والسكيل كل ساعة (+30% بدجت للحملة أو الأدست الرابحة):
    now_utc = datetime.now(timezone.utc)
    today_local = datetime.now(ACCOUNT_TZ)
    today_date_str = today_local.strftime("%Y-%m-%d")
    today_start_utc = today_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    is_night_window = (today_local.hour >= 22 or today_local.hour < 6)
    is_after_6pm = (today_local.hour >= 18 or today_local.hour < 6)  # أمر المستخدم الصارم: لا يرفع بدجت بعد الساعة 6 مساءً أبداً!

    BUMP_STEP_MICRO = 0        # ممنوع رفع البيد نهائياً بأمر المستخدم
    MAX_LOW_SPEND_BUMPS = 0    # صفر زيادات للبيد
    TWO_HOURS_SEC = 3300
    THREE_HOURS_SEC = 3300     # ساعة واحدة (~55 دقيقة) لأول زيادة، و 3 ساعات كاملة (10800 ثانية) إذا زادت أتمتة أخرى البدجت مؤخراً
    CROSS_AUTOMATION_WAIT_SEC = 10800  # 3 ساعات انتظار إلزامي إذا قامت أتمتة أخرى بزيادة البدجت
    LOW_SPEND_THRESHOLD = 0.25

    master_ledger_file = Path(__file__).parent / "snap_master_actions_ledger.json"
    master_squad_state = {}
    master_camp_state = {}
    if master_ledger_file.exists():
        try:
            _m_data = json.loads(master_ledger_file.read_text(encoding="utf-8"))
            master_squad_state = _m_data.get("squad_state", {}) or {}
            master_camp_state = _m_data.get("campaign_state", {}) or {}
        except Exception:
            master_squad_state = {}
            master_camp_state = {}

    history.setdefault("smart_spend_watchdog", {})
    history.setdefault("bid_bumps", {})
    history.setdefault("snap_3h_budget_scale", {})
    for old_bk in list(history["bid_bumps"].keys()):
        if not isinstance(history["bid_bumps"].get(old_bk), dict) or history["bid_bumps"][old_bk].get("schema_v") != 2:
            del history["bid_bumps"][old_bk]
    print(
        f"\n🛡️ فحص ثبات البيد ($1.90 لجميع أدست الكوبي في سناب شات بدون أي تغيير تلقائي للبيد) + زيادة البدجت +30% كل ساعة للأدست الرابحة..."
    )
    for c in active_campaigns:
        c_id = c["id"]
        c_name = c.get("name", "")
        if is_dedicated_snap_test_campaign(c_id, c_name) or c_id in ALLOWED_GULF_CAMPAIGN_IDS or is_gulf_034_campaign(c_name):
            continue
        c_squads = camp_squads_cache.get(c_id)
        if c_squads is None:
            c_squads = api_get_paginated(f"/campaigns/{c_id}/adsquads?limit=200", token, "adsquads", "adsquad")

        camp_today_spend = sum(spend_by_squad.get(sq["id"], 0.0) for sq in c_squads if sq and sq.get("status") == "ACTIVE")
        camp_today_purch = sum(squad_stats_map.get(sq["id"], {}).get("purchases", 0) for sq in c_squads if sq and sq.get("status") == "ACTIVE")
        sh_1d_map = (sheet_sales_multi.get("1d", {}) or {}) if isinstance(sheet_sales_multi, dict) else {}
        camp_sh_purch = int(sh_1d_map.get(c_id.lower(), 0))
        camp_today_purch = max(camp_today_purch, camp_sh_purch)
        camp_today_cpa = round(camp_today_spend / camp_today_purch, 2) if camp_today_purch > 0 else None
        is_zero_spend_campaign = (camp_today_spend < 0.25)

        # (أ) تثبيت ميزانية الحملة على سقف $20.00 (أمر المستخدم الصارم: تنزيل وتثبيت البدجت في سناب على $20.00 ومنع التضخم التلقائي):
        camp_budget_micro = int(c.get("daily_budget_micro") or 0)
        if camp_budget_micro > 20_000_000 and not dry_run:
            try:
                c_upd_cap = dict(c)
                c_upd_cap["daily_budget_micro"] = 20_000_000
                api_put(f"/adaccounts/{ad_account_id}/campaigns", token, {"campaigns": [c_upd_cap]})
                c["daily_budget_micro"] = 20_000_000
                camp_budget_micro = 20_000_000
            except Exception:
                pass
        if False and not is_after_6pm and not is_night_window and camp_budget_micro > 0 and camp_today_purch >= 1 and (camp_today_cpa or 0) < 12.00:
            c_scale_key = f"CAMP:{c_id}"
            c_scale_rec = history["snap_3h_budget_scale"].get(c_scale_key)
            m_camp_rec = master_camp_state.get(c_id) if isinstance(master_camp_state.get(c_id), dict) else {}
            cand_c_scale_dts = []
            c_scales_today = 0
            prev_c_scaled_pu = 0
            if isinstance(c_scale_rec, dict) and c_scale_rec.get("last_scale_date") == today_date_str and parse_iso_utc(c_scale_rec.get("last_scale_utc") or ""):
                cand_c_scale_dts.append(parse_iso_utc(c_scale_rec["last_scale_utc"]))
                c_scales_today = max(c_scales_today, int(c_scale_rec.get("scales_today", 0)))
                prev_c_scaled_pu = max(prev_c_scaled_pu, int(c_scale_rec.get("purchases_today", 0)))
            if m_camp_rec.get("last_action_type") == "scale_campaign_budget" and parse_iso_utc(m_camp_rec.get("last_action_utc") or ""):
                m_c_dt = parse_iso_utc(m_camp_rec["last_action_utc"])
                if m_c_dt and m_c_dt >= today_start_utc:
                    cand_c_scale_dts.append(m_c_dt)
                    c_scales_today = max(c_scales_today, 1)
                    prev_c_scaled_pu = max(prev_c_scaled_pu, int(m_camp_rec.get("purchases_today", 0)))

            if cand_c_scale_dts:
                ref_c_dt = max(cand_c_scale_dts)
                req_c_wait = CROSS_AUTOMATION_WAIT_SEC
            else:
                c_created_dt = parse_iso_utc(c.get("created_at") or "")
                c_start_dt = parse_iso_utc(c.get("start_time") or "")
                cand_c_starts = [d for d in (c_created_dt, c_start_dt, today_start_utc) if d is not None]
                ref_c_dt = max(cand_c_starts) if cand_c_starts else today_start_utc
                req_c_wait = THREE_HOURS_SEC

            if (now_utc - ref_c_dt).total_seconds() >= req_c_wait and c_scales_today < 4 and camp_today_purch > prev_c_scaled_pu:
                new_camp_budget_micro = int(round((camp_budget_micro / 1_000_000) * 1.30, 2) * 1_000_000)
                old_c_usd = round(camp_budget_micro / 1_000_000, 2)
                new_c_usd = round(new_camp_budget_micro / 1_000_000, 2)
                if dry_run:
                    print(
                        f"🔍 [زيادة بدجت الحملة +30%] الحملة '{c_name}' (المبيعات اليوم={camp_today_purch}, CPA=${camp_today_cpa}) | البدجت: ${old_c_usd:.2f} -> ${new_c_usd:.2f}"
                    )
                else:
                    c_upd = dict(c)
                    c_upd["daily_budget_micro"] = new_camp_budget_micro
                    try:
                        api_put(f"/adaccounts/{ad_account_id}/campaigns", token, {"campaigns": [c_upd]})
                        c["daily_budget_micro"] = new_camp_budget_micro
                        history["snap_3h_budget_scale"][c_scale_key] = {
                            "type": "CAMPAIGN",
                            "id": c_id,
                            "name": c_name,
                            "last_scale_utc": now_utc.isoformat(),
                            "last_scale_date": today_date_str,
                            "scales_today": c_scales_today + 1,
                            "old_budget_usd": old_c_usd,
                            "new_budget_usd": new_c_usd,
                            "purchases_today": camp_today_purch,
                            "cpa_today": camp_today_cpa,
                        }
                        save_paused_history(history)
                        print(
                            f"🚀📈 [زيادة بدجت الحملة +30% لوجود مبيعات جديدة وبعد انتظار الأتمتات الأخرى] الحملة '{c_name}' (مبيعات اليوم={camp_today_purch}, CPA=${camp_today_cpa:.2f}) | البدجت: ${old_c_usd:.2f} -> ${new_c_usd:.2f}"
                        )
                    except Exception as e_c_sc:
                        print(f"⚠️ خطأ أثناء زيادة بدجت الحملة '{c_name}' بنسبة +30%: {e_c_sc}")

        squads_to_update_batch = []
        clamps_logged = []
        bumps_logged = []
        scaled_3h_logged = []

        for idx_sq, squad in enumerate(c_squads):
            if not squad or squad.get("status") != "ACTIVE":
                continue
            s_id = squad["id"]
            s_name = squad.get("name", "")
            bid_micro = int(squad.get("bid_micro") or 0)
            budget_micro = int(squad.get("daily_budget_micro") or 0)
            today_spend = spend_by_squad.get(s_id, 0.0)
            st_sq = squad_stats_map.get(s_id, {"spend": today_spend, "purchases": 0, "cpa": None})
            st3_sq = squad_stats_3d_map.get(s_id, {"spend": today_spend, "purchases": 0, "cpa": None})

            expect_tc = is_target_cost_campaign_or_squad(c_id, c_name, s_name, ad_account_id)
            base_bid_micro = determine_expected_bid_micro_from_squad(s_name, bid_micro, expect_tc=expect_tc, campaign_name=c_name, campaign_id=c_id, squad_id=s_id)
            expected_strat = "TARGET_COST" if expect_tc else "LOWEST_COST_WITH_MAX_BID"
            expected_target_bid_flag = True if expect_tc else False

            sn_lower = s_name.strip().lower()
            is_low_bid_locked = True
            is_30x_fixed_19_20 = is_30x_low_bid_test_campaign_or_squad(c_id, c_name, s_name)
            is_fixed_smart_pb = (
                c.get("pacing_level") == "CAMPAIGN"
            )
            is_eligible_for_2h_bump = False

            # إذا كانت المجموعة مجدولة للمستقبل (مثل 07:00 صباحاً)، لا نلمس بيدها ولا نبدأ عداد الساعة قبل وقت بدئها الفعلي!
            start_dt_sq = parse_iso_utc(squad.get("start_time") or "")
            if start_dt_sq and now_utc < start_dt_sq:
                continue

            # أمر المستخدم الصارم (2026-11-02): ممنوع منعاً باتاً تغيير البيد (لا يطلع ولا ينزل تلقائياً)
            is_gulf_sq = is_gulf_034_campaign(c_name, s_name, squad)
            is_ded_test = is_dedicated_snap_test_campaign(c_id, c_name, s_name)
            target_bid_micro = bid_micro

            m_sq = master_squad_state.get(s_id, {})
            b_rec = {
                "schema_v": 2,
                "name": s_name,
                "campaign": c_name,
                "base_bid_micro": target_bid_micro,
                "bumps_count": 0,
                "last_check_utc": now_utc.isoformat(),
                "last_bump_utc": None,
                "spend_at_last_check": today_spend,
                "spend_date": today_date_str,
                "current_bid_micro": target_bid_micro,
            }
            history["bid_bumps"][s_id] = b_rec

            # (ب) قاعدة البدجت الحريصة والمنضبطة ($20.00 لجميع الأدست والحملات بأمر المستخدم 2026-11-02، وممنوع رفع البدجت بعد 6 مساءً):
            # ملاحظة: حملات الخليج (الكويت وقطر وعمان) تُدار ميزانياتها حصرياً عبر gulf_2h_profit_and_ads_master.py
            if is_gulf_sq or c_id in ALLOWED_GULF_CAMPAIGN_IDS:
                continue
            is_cbo_campaign = (c.get("pacing_level") == "CAMPAIGN")
            base_floor_micro = 20_000_000
            sq_pu_today = int(st_sq.get("purchases", 0))
            sq_cpa_today = st_sq.get("cpa")
            max_win_cpa_sq = 12.0 if is_gulf_034_campaign(c_name, s_name, squad) else 7.00
            is_sq_winning_today = (sq_pu_today >= 1 and (sq_cpa_today is None or sq_cpa_today < max_win_cpa_sq))

            # البدجت الأساسي $20.00 (20_000_000 micro) مع السماح بالبدجت النهاري المرفوع للأدست المحققة >= 3 مبيعات بتكلفة رابحة وإرجاع الكل لـ $20 آخر الليل (22:00-05:59):
            custom_dt_b_micro = None if is_night_window else get_custom_daytime_budget_micro(s_id, today_date_str)
            sq_pu_3d = int(st3_sq.get("purchases", 0))
            sq_cpa_3d = st3_sq.get("cpa")
            is_3sales_winner_sq = (
                not is_night_window
                and sq_pu_3d >= 3
                and sq_cpa_3d is not None
                and sq_cpa_3d <= max_win_cpa_sq
                and (today_spend < 4.00 or sq_pu_today >= 1)
            )
            if custom_dt_b_micro and custom_dt_b_micro > base_floor_micro:
                base_floor_micro = custom_dt_b_micro
                max_allowed_budget_micro = custom_dt_b_micro
            elif is_3sales_winner_sq:
                max_allowed_budget_micro = 40_000_000
            else:
                max_allowed_budget_micro = base_floor_micro

            if not is_cbo_campaign:
                if (not is_after_6pm and budget_micro < base_floor_micro) or budget_micro > max_allowed_budget_micro:
                    clamped_b_micro = max(base_floor_micro if not is_after_6pm else budget_micro, min(budget_micro, max_allowed_budget_micro))
                    if clamped_b_micro != budget_micro:
                        if not dry_run:
                            squad["daily_budget_micro"] = clamped_b_micro
                            squads_to_update_batch.append(squad)
                            print(
                                f"🔧 [ضبط البدجت الحريص ضمن السقف الآمن ${clamped_b_micro/1e6:.2f}] المجموعة '{s_name}' (حملة: '{c_name}', مبيعات={sq_pu_today}) | الميزانية: ${budget_micro/1e6:.2f} -> ${clamped_b_micro/1e6:.2f}"
                            )
                        budget_micro = clamped_b_micro

                # زيادة البدجت +30% قبل الساعة 6 مساءً فقط للأدست التي تجيب مبيعات بـ CPA < $7.00 ضمن السقف التدريجي الصارم فقط:
                if not is_after_6pm and not is_night_window and not sn_lower.startswith("base -") and is_sq_winning_today and budget_micro < max_allowed_budget_micro:
                    sq_scale_key = f"SQ:{s_id}"
                    sq_scale_rec = history["snap_3h_budget_scale"].get(sq_scale_key)
                    cand_sq_scale_dts = []
                    sq_scales_today = 0
                    prev_sq_scaled_pu = 0
                    if isinstance(sq_scale_rec, dict) and sq_scale_rec.get("last_scale_date") == today_date_str and parse_iso_utc(sq_scale_rec.get("last_scale_utc") or ""):
                        cand_sq_scale_dts.append(parse_iso_utc(sq_scale_rec["last_scale_utc"]))
                        sq_scales_today = max(sq_scales_today, int(sq_scale_rec.get("scales_today", 0)))
                        prev_sq_scaled_pu = max(prev_sq_scaled_pu, int(sq_scale_rec.get("purchases_today", 0)))
                    if isinstance(m_sq, dict):
                        m_sq_act = m_sq.get("last_action_type") or ""
                        m_sq_dt = parse_iso_utc(m_sq.get("last_action_utc") or "")
                        if m_sq_dt and m_sq_dt >= today_start_utc and ("scale" in m_sq_act or int(m_sq.get("target_budget_micro") or 0) > base_floor_micro):
                            cand_sq_scale_dts.append(m_sq_dt)
                            sq_scales_today = max(sq_scales_today, 1)
                            prev_sq_scaled_pu = max(prev_sq_scaled_pu, int(m_sq.get("scaled_at_purchases") or m_sq.get("purchases_1d") or 0))

                    already_scaled_by_any = bool(cand_sq_scale_dts) or (budget_micro > base_floor_micro)
                    if cand_sq_scale_dts:
                        ref_sq_dt = max(cand_sq_scale_dts)
                    else:
                        sq_created_dt = parse_iso_utc(squad.get("created_at") or "")
                        cand_sq_starts = [d for d in (sq_created_dt, start_dt_sq, today_start_utc) if d is not None]
                        ref_sq_dt = max(cand_sq_starts) if cand_sq_starts else today_start_utc

                    can_scale_sq_now = (
                        (not already_scaled_by_any)
                        or (
                            (now_utc - ref_sq_dt).total_seconds() >= CROSS_AUTOMATION_WAIT_SEC
                            and sq_pu_today > prev_sq_scaled_pu
                            and today_spend >= (budget_micro / 1_000_000) * 0.50
                        )
                    )
                    if can_scale_sq_now:
                        new_sq_budget_micro = min(max_allowed_budget_micro, int(round((budget_micro / 1_000_000) * 1.30, 2) * 1_000_000))
                        old_sq_usd = round(budget_micro / 1_000_000, 2)
                        new_sq_usd = round(new_sq_budget_micro / 1_000_000, 2)
                        if new_sq_budget_micro > budget_micro:
                            squad["daily_budget_micro"] = new_sq_budget_micro
                            squads_to_update_batch.append(squad)
                            scaled_3h_logged.append(
                                f"'{s_name}' (${old_sq_usd:.2f} -> ${new_sq_usd:.2f} | مبيعات={sq_pu_today}, CPA=${sq_cpa_today})"
                            )
                            if not dry_run:
                                history["snap_3h_budget_scale"][sq_scale_key] = {
                                    "type": "ADSQUAD",
                                    "id": s_id,
                                    "name": s_name,
                                    "campaign": c_name,
                                    "last_scale_utc": now_utc.isoformat(),
                                    "last_scale_date": today_date_str,
                                    "scales_today": sq_scales_today + 1,
                                    "old_budget_usd": old_sq_usd,
                                    "new_budget_usd": new_sq_usd,
                                    "purchases_today": sq_pu_today,
                                    "cpa_today": sq_cpa_today,
                                }

        if squads_to_update_batch:
            if dry_run:
                print(
                    f"🔍 [Dry-Run | حملة '{c_name}'] سيتم زيادة بدجت {len(scaled_3h_logged)} أدست (+30% كل 3 ساعات) وزيادة بيد {len(bumps_logged)} أدست وضبط {len(clamps_logged)} أدست."
                )
            else:
                # إزالة التكرار مع الحفاظ على آخر نسخة من كل squad
                dedup_map = {sq_u["id"]: sq_u for sq_u in squads_to_update_batch}
                dedup_list = list(dedup_map.values())
                for i in range(0, len(dedup_list), 10):
                    try:
                        api_put(f"/campaigns/{c_id}/adsquads", token, {"adsquads": dedup_list[i:i+10]})
                        time.sleep(0.25)
                    except Exception as e_put:
                        print(f"⚠️ خطأ أثناء تحديث دفعة في حملة '{c_name}': {e_put}")
                save_paused_history(history)
                if scaled_3h_logged:
                    print(f"🚀📈 [زيادة بدجت +30% بعد 3 ساعات للأدست الرابحة | حملة '{c_name}'] تم رفع بدجت {len(scaled_3h_logged)} أدست: {', '.join(scaled_3h_logged[:6])}")
                if bumps_logged:
                    print(f"📈✅ [زيادة +$0.10 كل ساعتين لضعف الصرف (<$0.20) | حملة '{c_name}'] تم رفع بيد {len(bumps_logged)} أدست: {', '.join(bumps_logged[:6])}")
                if clamps_logged:
                    print(f"📉✅ [ضبط البيد الأساسي | حملة '{c_name}'] تم ضبط {len(clamps_logged)} أدست: {', '.join(clamps_logged[:6])}")
        elif not dry_run:
            save_paused_history(history)


def recheck_gulf_adsquads(token: str):
    print("🚫 إعادة تشغيل المجموعات الموقوفة معطلة نهائياً حسب قاعدة المستخدم.")
    return


if __name__ == "__main__":
    cfg = load_config()
    token = refresh_access_token(cfg)
    if "--recheck-gulf-squads" in sys.argv:
        recheck_gulf_adsquads(token)
    else:
        dry = "--dry-run" in sys.argv
        for acc_id in TARGET_ACCOUNT_IDS:
            print(f"\n==================================================================")
            print(f"🛡️ تشغيل مراقب سناب شات على الحساب: {acc_id}")
            print(f"==================================================================")
            run_automation(token, acc_id, dry_run=dry)
        print("🛡️ [أمر المستخدم 2026-10-30] إيقاف أي أدست تصرف >= $4.00 بدون بيعة أو CPA >= $7.00 | إيقاف أي حملة تصرف >= $20.00 بدون بيعة أو CPA >= $21.00 | زيادة البدجت +30% كل ساعة للحملة أو الأدست الرابحة.")
        if not dry and os.environ.get("GITHUB_ACTIONS") != "true":
            try:
                from github_deploy import sync_state_and_rules_to_github
                sync_state_and_rules_to_github("snap_monitor")
            except Exception as _sync_err:
                print(f"ℹ️ ملاحظة مزامنة GitHub من snap_monitor: {_sync_err}")
