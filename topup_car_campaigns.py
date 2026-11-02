from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from snap_auth import API_BASE, load_config, refresh_access_token
from snap_monitor import (
    ACCOUNT_TZ,
    SECOND_ACCOUNT_ID,
    TARGET_ACCOUNT_ID,
    fetch_account_breakdown_stats,
    is_non_copy_test_adsquad,
    load_paused_history,
    resolve_video_media_id,
    save_paused_history,
)

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

PIXEL_ID = "205dde59-f5b1-4b58-972b-79133f717798"
BRAND_NAME = "دار الرمال"
CAR_URL = "https://www.daralrimal.org/tvfAkFHdl/split"
PB_URL = "https://www.daralrimal.shop/rluajrRjM/split"
MAX_BID_MICRO = 2_400_000  # $2.40 for LOWEST_COST_WITH_MAX_BID
TC_BID_MICRO = 3_000_000   # $3.00 for TARGET_COST
MAX_CAMPAIGN_COPY_ADSQUADS = 30

CAMPAIGNS_TO_MANAGE = [
    {
        "account_name": "CSP9860",
        "account_id": TARGET_ACCOUNT_ID,
        "campaign_id": "8953b893-82fc-41e3-b1c0-24102bdcbb01",
        "prod_prefix": "PB",
        "mode_lbl": "Max",
        "is_target_cost": False,
        "base_bid_micro": 3_200_000,
        "bid_str": "3.20",
        "force_hl_tag": "H1",
        "no_zero_spend_bump": True,
        "daily_budget_micro": 20_000_000,
        "landing_url": PB_URL,
    },
    {
        "account_name": "CSP9860 4",
        "account_id": SECOND_ACCOUNT_ID,
        "campaign_id": "dab749f9-2dca-497b-a8c3-39f2201162a7",
        "prod_prefix": "PB",
        "mode_lbl": "TC",
        "is_target_cost": True,
        "daily_budget_micro": 20_000_000,
        "landing_url": PB_URL,
    },
    {
        "account_name": "CSP9860 4",
        "account_id": SECOND_ACCOUNT_ID,
        "campaign_id": "803324e6-052e-441b-99fb-4d43e56c5c78",
        "prod_prefix": "PB",
        "mode_lbl": "Max",
        "is_target_cost": False,
        "daily_budget_micro": 20_000_000,
        "landing_url": PB_URL,
    },
]

HEADLINES = {
    "Car": {
        "H1": "شحن مجاني + الدفع عند الاستلام",
        "H2": "التوصيل مجاني مع ضمان استرجاع",
    },
    "PB": {
        "H1": "شحن مجاني + الدفع عند الاستلام",
        "H2": "اشحن جوالك بدون أسلاك في أي مكان",
    },
}

VIDEOS_CATALOG = {
    (TARGET_ACCOUNT_ID, "Car"): [
        ("Car_v10_Fast01", "c1df8bee-a6e8-46e9-a0d6-ab7578173329"),
        ("Car_v11_Fast03", "705166b8-44fd-46f9-bf26-e0230773c999"),
        ("Car_v12_Fast04", "b275e059-5e78-4ddc-9de5-4a2beb13bcfd"),
        ("Car_v13_Fast05", "336db6c7-8226-48ab-b4f4-f036131dff1c"),
        ("Car_v14_Fast06", "ca817182-2e8a-41cd-a72d-4afbc78d470a"),
        ("Car_v15_Fast07", "dcc1fccf-9496-4de2-9337-22689649a362"),
        ("Car_v16_Fast08", "45363b3a-ad2f-4085-90e6-a25a5d42788c"),
        ("Car_v17_Fast09", "b31a3b8b-af76-4554-ad6d-096582585bdd"),
        ("Car_v1_21_9_2", "bb146f4e-7624-4e31-b853-847810e85ffa"),
        ("Car_v2_15_10_4", "1efb6ffc-aced-4da1-8103-da4ce6012aaf"),
        ("Car_v3_13_10_hanan5", "9edae80d-dbd4-4253-b227-8bdcee74c4a6"),
        ("Car_v4_12_10_1", "ad992647-c8c5-4afa-b50a-46a889ecd55c"),
        ("Car_v5_12_10_2", "788db4d2-54aa-409d-8190-54db3ff9bfa4"),
        ("Car_v6_21_10_hanan1", "b7751519-8006-478c-81ec-4223d51be3de"),
        ("Car_v7_20_10_hook2", "2e23fe3a-8089-42dc-9fa7-7dc462a3ac33"),
        ("Car_v8_21_10_hanan3", "fd8dd57b-ac7d-43e8-8e54-e9f8c621b460"),
        ("Car_v9_21_10_hananE1", "7ad455cb-e919-4abd-9eb9-66260c7dce09"),
    ],
    (TARGET_ACCOUNT_ID, "PB"): [
        ("PB_v1_Winner1", "207c2a54-97a8-4445-b7b2-1e8cc46f9b14"),
        ("PB_v2_Winner2", "89aba96a-6aa7-4a63-9819-0daca54a74e4"),
        ("PB_v7_Hist64", "08e86a33-6d7c-4f4a-8e4f-23b7b49072ff"),
        ("PB_v10_VoltrxH7", "5d315093-a5af-4ccb-992e-ebb1cce47cd1"),
        ("PB_v9_Hist122", "0f6a2dd9-9955-41ac-a2e9-21cf7f5eebc6"),
        ("PB_v3_Winner3", "f03b4395-e1da-4ed3-8606-7e5e3d86d3e4"),
    ],
    (SECOND_ACCOUNT_ID, "Car"): [
        ("Car_v10_Fast01", "d26bc6f8-a33a-4214-888b-50901d4c31b6"),
        ("Car_v11_Fast03", "638f0e0d-e1e6-4ae7-ba74-87dd0fb53e4a"),
        ("Car_v12_Fast04", "c9fc86a2-9048-4ed5-ab18-57fc3f379a74"),
        ("Car_v13_Fast05", "98c651c5-185e-4e48-ae3b-5da5152673ac"),
        ("Car_v14_Fast06", "1de46034-855e-4d4a-8c69-3ff0b456994c"),
        ("Car_v15_Fast07", "b7aebde0-97f8-416e-a43a-27c47aa0a4cd"),
        ("Car_v16_Fast08", "0de75e1a-7b63-4d40-a67d-0ee76dfce804"),
        ("Car_v17_Fast09", "1775d521-a4f7-442c-8097-66754c573c6a"),
        ("Car_v1_21_9_2", "42c43ab7-ddaf-45a4-a044-cf5466d11bbb"),
        ("Car_v2_15_10_4", "f01fe69a-ec5b-4360-9897-7b64a42d0fe4"),
        ("Car_v3_13_10_hanan5", "0f2f0a11-671d-4b7a-89e2-9e2094869bbe"),
        ("Car_v4_12_10_1", "0f93d640-5927-4cef-9467-5c735246fa11"),
        ("Car_v5_12_10_2", "f2aa3337-ecae-4ad3-a5a6-ce3e4bd7af92"),
        ("Car_v6_21_10_hanan1", "bff898f4-dacc-44eb-ae2c-bed2f07aa374"),
        ("Car_v7_20_10_hook2", "ac883791-5538-4bb5-b033-893c1d21c420"),
        ("Car_v8_21_10_hanan3", "30213b6c-56f0-42a1-ad1f-203b850d1151"),
        ("Car_v9_21_10_hananE1", "89630bd3-a9fe-4698-a457-8aebe25e3361"),
    ],
    (SECOND_ACCOUNT_ID, "PB"): [
        ("PB_v1_Winner1", "617d9c71-666b-4440-8f07-284857d33d27"),
        ("PB_v2_Winner2", "198c6ee8-d20a-483d-bf48-8b97fecd4697"),
        ("PB_v3_Winner3", "4582d635-1313-487f-babe-2decd07d7119"),
        ("PB_v4_Winner4", "b7161f3e-7d5a-42e1-9861-5bda9b14488d"),
        ("PB_v5_Winner5", "773a093c-a918-4afa-b369-48c7dba00d6d"),
        ("PB_v6_Winner6", "bd022732-08ce-43f3-854c-d66f1e472714"),
        ("PB_v7_Hist64", "fae396e6-c536-43a5-a207-7af64945a4e5"),
        ("PB_v9_Hist122", "dad71b63-5e86-43fb-bde9-7e53e2da77f3"),
        ("PB_v10_VoltrxH7", "bf3fa914-1816-4101-8cd7-e3a4ec14d060"),
    ],
}

DEFAULT_CREATIVES = {
    f"{TARGET_ACCOUNT_ID}|bb146f4e-7624-4e31-b853-847810e85ffa|{CAR_URL}|H1": "8a7f11e4-f303-4176-9e00-85e97359b573",
    f"{TARGET_ACCOUNT_ID}|bb146f4e-7624-4e31-b853-847810e85ffa|{CAR_URL}|H2": "402ee724-e7c8-45c8-94fc-17547d6850bf",
    f"{TARGET_ACCOUNT_ID}|1efb6ffc-aced-4da1-8103-da4ce6012aaf|{CAR_URL}|H1": "d9b9f97d-6763-4afd-9bcb-5ca81dcc937c",
    f"{TARGET_ACCOUNT_ID}|1efb6ffc-aced-4da1-8103-da4ce6012aaf|{CAR_URL}|H2": "46426247-c657-47d0-8ac3-d1dbe0a314e9",
    f"{TARGET_ACCOUNT_ID}|9edae80d-dbd4-4253-b227-8bdcee74c4a6|{CAR_URL}|H1": "cfed2f54-5792-4741-a0a1-83d1f22f27a3",
    f"{TARGET_ACCOUNT_ID}|9edae80d-dbd4-4253-b227-8bdcee74c4a6|{CAR_URL}|H2": "e92fb3a3-fe42-4c5b-879e-00a3d507ae51",
    f"{TARGET_ACCOUNT_ID}|ad992647-c8c5-4afa-b50a-46a889ecd55c|{CAR_URL}|H1": "6da8db9b-6447-4f62-8440-b9adc7ef056c",
    f"{TARGET_ACCOUNT_ID}|ad992647-c8c5-4afa-b50a-46a889ecd55c|{CAR_URL}|H2": "ff608e30-3d45-4b68-9117-da0e288908af",
    f"{TARGET_ACCOUNT_ID}|788db4d2-54aa-409d-8190-54db3ff9bfa4|{CAR_URL}|H1": "38057abd-29de-4293-a3a5-3ccaf491fe10",
    f"{TARGET_ACCOUNT_ID}|788db4d2-54aa-409d-8190-54db3ff9bfa4|{CAR_URL}|H2": "129e6424-1d97-47b6-8838-9652f26c5cb3",
    f"{TARGET_ACCOUNT_ID}|b7751519-8006-478c-81ec-4223d51be3de|{CAR_URL}|H1": "d2cd9a0a-6ea4-400c-9be9-98b33b960afd",
    f"{TARGET_ACCOUNT_ID}|b7751519-8006-478c-81ec-4223d51be3de|{CAR_URL}|H2": "10d7e44d-3a4f-46a8-a950-e5d1c6a484d7",
    f"{TARGET_ACCOUNT_ID}|2e23fe3a-8089-42dc-9fa7-7dc462a3ac33|{CAR_URL}|H1": "d074a8c4-0509-4161-bc04-5da58b9126d7",
    f"{TARGET_ACCOUNT_ID}|2e23fe3a-8089-42dc-9fa7-7dc462a3ac33|{CAR_URL}|H2": "e4621b61-f22c-4388-b9da-d2232d3e12bb",
    f"{TARGET_ACCOUNT_ID}|fd8dd57b-ac7d-43e8-8e54-e9f8c621b460|{CAR_URL}|H1": "04aaad8b-1f31-4de3-9da8-fcd7c7040904",
    f"{TARGET_ACCOUNT_ID}|fd8dd57b-ac7d-43e8-8e54-e9f8c621b460|{CAR_URL}|H2": "f3dc13fe-c587-4ece-ad8c-9098cbe8a4d9",
    f"{TARGET_ACCOUNT_ID}|7ad455cb-e919-4abd-9eb9-66260c7dce09|{CAR_URL}|H1": "82c66cb4-26ed-4817-9e29-7d807a11e157",
    f"{TARGET_ACCOUNT_ID}|7ad455cb-e919-4abd-9eb9-66260c7dce09|{CAR_URL}|H2": "3b03b6c1-48db-4130-a765-92b446c5ccc3",
    f"{TARGET_ACCOUNT_ID}|207c2a54-97a8-4445-b7b2-1e8cc46f9b14|{PB_URL}|H1": "eeade141-1ef8-4da0-bb82-2341c07d4cf3",
    f"{TARGET_ACCOUNT_ID}|207c2a54-97a8-4445-b7b2-1e8cc46f9b14|{PB_URL}|H2": "db208dcc-ed3f-448e-a005-56e88b4eb1ef",
    f"{TARGET_ACCOUNT_ID}|89aba96a-6aa7-4a63-9819-0daca54a74e4|{PB_URL}|H1": "f4ddb314-dda7-4607-b914-deeaaf9556d7",
    f"{TARGET_ACCOUNT_ID}|89aba96a-6aa7-4a63-9819-0daca54a74e4|{PB_URL}|H2": "0e4b382c-a17f-4e9b-8c9e-b8e7d2175acb",
    f"{TARGET_ACCOUNT_ID}|f03b4395-e1da-4ed3-8606-7e5e3d86d3e4|{PB_URL}|H1": "aab120d1-f938-45f0-9070-b7b9bf6bf206",
    f"{TARGET_ACCOUNT_ID}|f03b4395-e1da-4ed3-8606-7e5e3d86d3e4|{PB_URL}|H2": "56a06f00-7ac9-4711-a3ff-16bab3439cbb",
    f"{TARGET_ACCOUNT_ID}|04d7ae2c-e2c4-41ee-a5c9-1a6d3ace7f38|{PB_URL}|H1": "0c047d59-e41b-4287-b893-259d9ae237b5",
    f"{TARGET_ACCOUNT_ID}|04d7ae2c-e2c4-41ee-a5c9-1a6d3ace7f38|{PB_URL}|H2": "2e6619be-3f4c-4dda-bbd3-6f3e3256ab09",
    f"{TARGET_ACCOUNT_ID}|7f9ad9fb-aa50-4c59-b485-c058ad5666a0|{PB_URL}|H1": "76035b26-907d-4d49-aed9-d09d2236d1ab",
    f"{TARGET_ACCOUNT_ID}|7f9ad9fb-aa50-4c59-b485-c058ad5666a0|{PB_URL}|H2": "4a6f0a35-b65f-42e5-a2c7-07570814781a",
    f"{TARGET_ACCOUNT_ID}|5f5742e1-5944-43ee-b9aa-a6a2ea85f211|{PB_URL}|H1": "8fb617bf-f72e-4249-b235-2fd02491df63",
    f"{TARGET_ACCOUNT_ID}|5f5742e1-5944-43ee-b9aa-a6a2ea85f211|{PB_URL}|H2": "ba7a9ad4-a743-4e42-a28b-6be601011bae",
    f"{TARGET_ACCOUNT_ID}|08e86a33-6d7c-4f4a-8e4f-23b7b49072ff|{PB_URL}|H1": "72b18f81-86bd-4d54-a3bf-29506dacbcae",
    f"{TARGET_ACCOUNT_ID}|08e86a33-6d7c-4f4a-8e4f-23b7b49072ff|{PB_URL}|H2": "80660cd7-9cf4-4a46-8e5b-255a95183044",
    f"{TARGET_ACCOUNT_ID}|fb6c08bc-8d29-4d05-9735-6e435bcf54ab|{PB_URL}|H1": "cde5b2d3-b0be-4364-af15-3b9ad4d9034e",
    f"{TARGET_ACCOUNT_ID}|fb6c08bc-8d29-4d05-9735-6e435bcf54ab|{PB_URL}|H2": "a22b3d8b-3ec6-43de-a87c-5c997c51e329",
    f"{TARGET_ACCOUNT_ID}|0f6a2dd9-9955-41ac-a2e9-21cf7f5eebc6|{PB_URL}|H1": "5336a90e-81e2-43c0-9c3b-f3efb9e42d94",
    f"{TARGET_ACCOUNT_ID}|0f6a2dd9-9955-41ac-a2e9-21cf7f5eebc6|{PB_URL}|H2": "1482f0a5-81ae-4763-8c15-a36b084ce5c2",
    f"{TARGET_ACCOUNT_ID}|5d315093-a5af-4ccb-992e-ebb1cce47cd1|{PB_URL}|H1": "7f849f3a-2d79-43cc-8167-69adfbb0589d",
    f"{TARGET_ACCOUNT_ID}|5d315093-a5af-4ccb-992e-ebb1cce47cd1|{PB_URL}|H2": "394728a0-f247-4ad8-b7b7-2a3ee6b306df",
    f"{SECOND_ACCOUNT_ID}|42c43ab7-ddaf-45a4-a044-cf5466d11bbb|{CAR_URL}|H1": "a1894193-9f76-4609-a816-e8057b8a1e61",
    f"{SECOND_ACCOUNT_ID}|42c43ab7-ddaf-45a4-a044-cf5466d11bbb|{CAR_URL}|H2": "4a101722-e4be-45f7-a9c0-a62b93e864dc",
    f"{SECOND_ACCOUNT_ID}|f01fe69a-ec5b-4360-9897-7b64a42d0fe4|{CAR_URL}|H1": "e52b037d-864b-49a6-8807-4331331622df",
    f"{SECOND_ACCOUNT_ID}|f01fe69a-ec5b-4360-9897-7b64a42d0fe4|{CAR_URL}|H2": "3d5ed52e-f417-4c83-a904-7ba9ac7d8de7",
    f"{SECOND_ACCOUNT_ID}|0f2f0a11-671d-4b7a-89e2-9e2094869bbe|{CAR_URL}|H1": "c136e6fe-3848-43dd-a34a-f0c201ff5b99",
    f"{SECOND_ACCOUNT_ID}|0f2f0a11-671d-4b7a-89e2-9e2094869bbe|{CAR_URL}|H2": "99f0b62e-73e2-4a15-aae0-676680514079",
    f"{SECOND_ACCOUNT_ID}|0f93d640-5927-4cef-9467-5c735246fa11|{CAR_URL}|H1": "25952fec-14d4-48ac-9e2d-2dd8b30163cf",
    f"{SECOND_ACCOUNT_ID}|0f93d640-5927-4cef-9467-5c735246fa11|{CAR_URL}|H2": "79aeabd9-351e-4bdc-bff7-a0b704309358",
    f"{SECOND_ACCOUNT_ID}|f2aa3337-ecae-4ad3-a5a6-ce3e4bd7af92|{CAR_URL}|H1": "264619ee-529a-476a-9348-afd6ca26ae60",
    f"{SECOND_ACCOUNT_ID}|f2aa3337-ecae-4ad3-a5a6-ce3e4bd7af92|{CAR_URL}|H2": "b5d7f154-c292-4891-ba3f-249b5f9bc81f",
    f"{SECOND_ACCOUNT_ID}|bff898f4-dacc-44eb-ae2c-bed2f07aa374|{CAR_URL}|H1": "4de0b79f-1819-4a4c-a58e-bb5e9071af30",
    f"{SECOND_ACCOUNT_ID}|bff898f4-dacc-44eb-ae2c-bed2f07aa374|{CAR_URL}|H2": "09f42c3d-e58b-463a-8944-9bba22b04369",
    f"{SECOND_ACCOUNT_ID}|ac883791-5538-4bb5-b033-893c1d21c420|{CAR_URL}|H1": "57506795-9f0a-44e5-a017-547ee598ff51",
    f"{SECOND_ACCOUNT_ID}|ac883791-5538-4bb5-b033-893c1d21c420|{CAR_URL}|H2": "cfcbd63e-9628-47f0-bb97-253cefc2ab86",
    f"{SECOND_ACCOUNT_ID}|30213b6c-56f0-42a1-ad1f-203b850d1151|{CAR_URL}|H1": "4b66ce41-2b46-4f7d-a1e8-309574218cad",
    f"{SECOND_ACCOUNT_ID}|30213b6c-56f0-42a1-ad1f-203b850d1151|{CAR_URL}|H2": "669a2768-54ea-4ec3-bcac-128c8f187c63",
    f"{SECOND_ACCOUNT_ID}|89630bd3-a9fe-4698-a457-8aebe25e3361|{CAR_URL}|H1": "efe330ea-d47d-4e5c-9b4b-84524d0dad52",
    f"{SECOND_ACCOUNT_ID}|89630bd3-a9fe-4698-a457-8aebe25e3361|{CAR_URL}|H2": "a2996326-07cb-4de8-824b-7bd255338b66",
    f"{SECOND_ACCOUNT_ID}|617d9c71-666b-4440-8f07-284857d33d27|{PB_URL}|H1": "83b1b508-348d-491f-b1a3-04d98dcffeb3",
    f"{SECOND_ACCOUNT_ID}|617d9c71-666b-4440-8f07-284857d33d27|{PB_URL}|H2": "4eb48a5c-aa94-4a80-a2b9-c0633a76ba78",
    f"{SECOND_ACCOUNT_ID}|198c6ee8-d20a-483d-bf48-8b97fecd4697|{PB_URL}|H1": "330c0c04-34a2-4ed7-81a1-cf0693e882e9",
    f"{SECOND_ACCOUNT_ID}|198c6ee8-d20a-483d-bf48-8b97fecd4697|{PB_URL}|H2": "8337e4fc-b15b-4ca8-99ff-f0f17e521e15",
    f"{SECOND_ACCOUNT_ID}|4582d635-1313-487f-babe-2decd07d7119|{PB_URL}|H1": "403b84ae-6105-4517-979b-ac3f0dd282e2",
    f"{SECOND_ACCOUNT_ID}|4582d635-1313-487f-babe-2decd07d7119|{PB_URL}|H2": "b20b381a-b5ea-45d1-9716-bc32588f3da8",
    f"{SECOND_ACCOUNT_ID}|b7161f3e-7d5a-42e1-9861-5bda9b14488d|{PB_URL}|H1": "fd1a64d8-313f-486b-847c-1050bfda230f",
    f"{SECOND_ACCOUNT_ID}|b7161f3e-7d5a-42e1-9861-5bda9b14488d|{PB_URL}|H2": "af2de888-c474-4174-a489-66c18fa778de",
    f"{SECOND_ACCOUNT_ID}|773a093c-a918-4afa-b369-48c7dba00d6d|{PB_URL}|H1": "bb5f5d44-2296-437b-84cf-d7db7a57f68a",
    f"{SECOND_ACCOUNT_ID}|773a093c-a918-4afa-b369-48c7dba00d6d|{PB_URL}|H2": "155b1c6b-c72e-40fa-be89-62bc05397bd3",
    f"{SECOND_ACCOUNT_ID}|bd022732-08ce-43f3-854c-d66f1e472714|{PB_URL}|H1": "0ecbb845-1569-434e-9695-12f54583338f",
    f"{SECOND_ACCOUNT_ID}|bd022732-08ce-43f3-854c-d66f1e472714|{PB_URL}|H2": "cf3fdcac-9ee4-4b10-86bf-f74cd2d451e7",
    f"{SECOND_ACCOUNT_ID}|fae396e6-c536-43a5-a207-7af64945a4e5|{PB_URL}|H1": "3c829e04-3a29-45fd-8003-f8e4e3e3e5be",
    f"{SECOND_ACCOUNT_ID}|fae396e6-c536-43a5-a207-7af64945a4e5|{PB_URL}|H2": "4fec257b-b346-4b9d-bc96-86bd415b539f",
    f"{SECOND_ACCOUNT_ID}|8314f694-5f1b-402a-9060-cfae078f4728|{PB_URL}|H1": "6b598786-0d95-4679-8b60-56f8ccd150bf",
    f"{SECOND_ACCOUNT_ID}|8314f694-5f1b-402a-9060-cfae078f4728|{PB_URL}|H2": "b06adbe0-d1eb-46d6-b4a5-3122ee0439d8",
    f"{SECOND_ACCOUNT_ID}|dad71b63-5e86-43fb-bde9-7e53e2da77f3|{PB_URL}|H1": "b7bd4cc1-b39b-4ca0-b4af-33a6238fc4ad",
    f"{SECOND_ACCOUNT_ID}|dad71b63-5e86-43fb-bde9-7e53e2da77f3|{PB_URL}|H2": "4455f81f-51dd-4d93-8f96-7c1fbf82372d",
    f"{SECOND_ACCOUNT_ID}|bf3fa914-1816-4101-8cd7-e3a4ec14d060|{PB_URL}|H1": "eea31203-3f6d-4ccb-ba58-367bf23a7740",
    f"{SECOND_ACCOUNT_ID}|bf3fa914-1816-4101-8cd7-e3a4ec14d060|{PB_URL}|H2": "88b86ae6-0320-4509-83fa-13382ec37c13",
}


def make_session(token: str) -> requests.Session:
    s = requests.Session()
    retries = Retry(total=5, backoff_factor=0.8, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return s


def get_all_pages(s: requests.Session, url: str, key: str, max_pages: int = 40) -> list:
    out = []
    u = f"{API_BASE}{url}" if url.startswith("/") else url
    for _ in range(max_pages):
        if not u:
            break
        r = None
        for attempt in range(3):
            try:
                r = s.get(u, timeout=25)
                if r.ok:
                    break
            except Exception:
                if attempt == 2:
                    r = None
                else:
                    time.sleep(0.8)
        if not r or not r.ok:
            break
        d = r.json()
        for el in d.get(key, []):
            if key[:-1] in el:
                out.append(el[key[:-1]])
        u = d.get("paging", {}).get("next_link")
    return out


def build_30_slot_specs(
    prod_prefix: str,
    is_target_cost: bool,
    extra_bid_micro: int = 0,
    base_bid_micro: int | None = None,
    bid_str: str | None = None,
    force_hl_tag: str | None = None,
) -> list[dict]:
    mode_lbl = "TC" if is_target_cost else "Max"
    eff_base_bid = base_bid_micro if base_bid_micro is not None else (TC_BID_MICRO if is_target_cost else MAX_BID_MICRO)
    bid_micro = eff_base_bid + extra_bid_micro
    eff_bid_str = bid_str if bid_str is not None else ("3.00" if is_target_cost else "2.40")
    slots = []
    for idx in range(1, 31):
        hl_tag = force_hl_tag if force_hl_tag else ("H1" if idx <= 15 else "H2")
        sq_name = f"Copy - {prod_prefix} {eff_bid_str} {mode_lbl} [{hl_tag}] #{idx:02d}"
        slots.append({
            "index": idx,
            "name": sq_name,
            "bid_micro": bid_micro,
            "hl_tag": hl_tag,
            "bid_strategy": "TARGET_COST" if is_target_cost else "LOWEST_COST_WITH_MAX_BID",
            "target_bid": True if is_target_cost else False,
            "auto_bid": False,
        })
    return slots


def get_or_create_creative_id(
    s: requests.Session,
    acc_id: str,
    label: str,
    media_id: str,
    landing_url: str,
    hl_tag: str,
    hl_text: str,
    history: dict,
) -> str:
    key = f"{acc_id}|{media_id}|{landing_url}|{hl_tag}"
    if key in DEFAULT_CREATIVES:
        history.setdefault("creative_media_map", {})[DEFAULT_CREATIVES[key]] = media_id
        return DEFAULT_CREATIVES[key]
    cr_cache = history.setdefault("topup_creatives_cache", {})
    if key in cr_cache and cr_cache[key]:
        history.setdefault("creative_media_map", {})[cr_cache[key]] = media_id
        return cr_cache[key]

    cr_name = f"{label} [{hl_tag}]"[:95]
    payload = {
        "creatives": [
            {
                "ad_account_id": acc_id,
                "name": cr_name,
                "type": "WEB_VIEW",
                "brand_name": BRAND_NAME,
                "headline": hl_text[:34],
                "call_to_action": "ORDER_NOW",
                "shareable": True,
                "top_snap_media_id": media_id,
                "top_snap_crop_position": "MIDDLE",
                "forced_view_eligibility": "SIX_SECONDS",
                "ad_product": "SNAP_AD",
                "web_view_properties": {
                    "url": landing_url,
                    "allow_snap_javascript_sdk": False,
                    "use_immersive_mode": False,
                    "deep_link_urls": [],
                    "block_preload": True,
                },
            }
        ]
    }
    rc = s.post(f"{API_BASE}/adaccounts/{acc_id}/creatives", json=payload, timeout=25)
    if not rc.ok:
        raise RuntimeError(f"Create creative failed for {cr_name} in {acc_id}: {rc.status_code} {rc.text}")
    cid = rc.json()["creatives"][0]["creative"]["id"]
    cr_cache[key] = cid
    history.setdefault("creative_media_map", {})[cid] = media_id
    save_paused_history(history)
    return cid


def create_new_squads_with_filtered_ads(
    s: requests.Session,
    camp_id: str,
    slots_to_create: list[dict],
    ads_by_hl: dict[str, list[dict]],
    daily_budget_micro: int,
    start_time_iso: str | None = None,
) -> list[str]:
    if not slots_to_create:
        return []
    if start_time_iso:
        now_iso = start_time_iso
    else:
        now_ksa = datetime.now(ACCOUNT_TZ)
        if 0 <= now_ksa.hour < 7:
            sched_utc = now_ksa.replace(hour=7, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            now_iso = sched_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        else:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    targeting_cfg = {
        "regulated_content": False,
        "geos": [{"country_code": "sa", "operation": "INCLUDE"}],
        "enable_targeting_expansion": True,
        "auto_expansion_options": {
            "interest_expansion_option": {"enabled": True},
            "custom_audience_expansion_option": {"enabled": True},
            "auto_expansion_type": "SMART_TARGETING",
        },
    }
    created_ids = []
    for i in range(0, len(slots_to_create), 15):
        batch_slots = slots_to_create[i : i + 15]
        payloads = []
        for sl in batch_slots:
            payloads.append({
                "name": sl["name"],
                "status": "ACTIVE",
                "campaign_id": camp_id,
                "type": "SNAP_ADS",
                "targeting": targeting_cfg,
                "placement_v2": {"config": "AUTOMATIC"},
                "billing_event": "IMPRESSION",
                "bid_micro": sl["bid_micro"],
                "auto_bid": sl["auto_bid"],
                "target_bid": sl["target_bid"],
                "bid_strategy": sl["bid_strategy"],
                "daily_budget_micro": daily_budget_micro,
                "start_time": now_iso,
                "optimization_goal": "PIXEL_PURCHASE",
                "conversion_window": "SWIPE_28DAY_VIEW_1DAY",
                "pixel_id": PIXEL_ID,
                "pacing_type": "STANDARD",
                "child_ad_type": "REMOTE_WEBPAGE",
                "forced_view_setting": "NONE",
                "brand_safety_config": {"inventory_option": "FULL_INVENTORY"},
            })
        r_sq = None
        for attempt in range(3):
            try:
                r_sq = s.post(f"{API_BASE}/campaigns/{camp_id}/adsquads", json={"adsquads": payloads}, timeout=35)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"   ⚠️ استثناء مهلة إنشاء مجموعات بديلة في {camp_id}: {e}", flush=True)
                time.sleep(1.5 * (attempt + 1))
        if r_sq is None or not r_sq.ok:
            status_txt = f"{r_sq.status_code} {r_sq.text}" if r_sq else "Timeout/Error"
            print(f"   ⚠️ فشل إنشاء دفعة مجموعات بديلة في الحملة {camp_id}: {status_txt}", flush=True)
            continue
        new_sqs = [x["adsquad"] for x in r_sq.json().get("adsquads", []) if "adsquad" in x]
        for sq_obj, sl in zip(new_sqs, batch_slots):
            sq_id = sq_obj["id"]
            created_ids.append(sq_id)
            ad_items = ads_by_hl.get(sl["hl_tag"], [])
            if not ad_items:
                continue
            ad_payload = [
                {
                    "ad_squad_id": sq_id,
                    "creative_id": item["creative_id"],
                    "name": item["label"][:95],
                    "status": "ACTIVE",
                    "type": "REMOTE_WEBPAGE",
                }
                for item in ad_items
            ]
            ra = None
            for attempt in range(3):
                try:
                    ra = s.post(f"{API_BASE}/adsquads/{sq_id}/ads", json={"ads": ad_payload}, timeout=35)
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"   ⚠️ استثناء مهلة إنشاء إعلانات المجموعة {sq_id}: {e}", flush=True)
                    time.sleep(1.5 * (attempt + 1))
            if ra is None or not ra.ok:
                status_txt = f"{ra.status_code} {ra.text}" if ra else "Timeout/Error"
                print(f"   ⚠️ فشل إنشاء الإعلانات داخل المجموعة الجديدة {sq_id}: {status_txt}", flush=True)
            time.sleep(0.12)
    return created_ids


def main(dry_run: bool = False):
    print("🚫 [أمر المستخدم 2026-10-29] تم إيقاف الدوبليكيت (نسخ الكوبي 30x) في سناب شات نهائياً — نشتغل بأفضل 3 أدست فقط ببدجت $30 لكل أدست.", flush=True)
    return
    cfg = load_config()
    token = refresh_access_token(cfg)
    s = make_session(token)
    history = load_paused_history()
    now_str = datetime.now(ACCOUNT_TZ).strftime("%Y-%m-%d %H:%M GMT+3")

    # Populate creative -> media map from DEFAULT_CREATIVES so resolution is instant
    cmap = history.setdefault("creative_media_map", {})
    for k, cr_id in DEFAULT_CREATIVES.items():
        parts = k.split("|")
        if len(parts) >= 2:
            cmap[cr_id] = parts[1]

    sq_stats_1d = {}
    sq_stats_2d = {}
    sq_stats_3d = {}
    sq_stats_4d = {}
    sq_stats_5d = {}
    sq_stats_7d = {}
    ad_stats_1d = {}
    ad_stats_2d = {}
    ad_stats_3d = {}
    ad_stats_4d = {}
    ad_stats_5d = {}
    ad_stats_7d = {}

    for item in CAMPAIGNS_TO_MANAGE:
        acc_id = item["account_id"]
        if acc_id not in sq_stats_1d:
            sq_stats_1d[acc_id] = {x["id"]: x for x in fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=0)}
            sq_stats_2d[acc_id] = {x["id"]: x for x in fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=1)}
            sq_stats_3d[acc_id] = {x["id"]: x for x in fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=2)}
            sq_stats_4d[acc_id] = {x["id"]: x for x in (fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=3) or list(sq_stats_3d[acc_id].values()))}
            sq_stats_5d[acc_id] = {x["id"]: x for x in (fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=4) or list(sq_stats_4d[acc_id].values()))}
            sq_stats_7d[acc_id] = {x["id"]: x for x in (fetch_account_breakdown_stats(token, acc_id, "adsquad", days_back=6) or list(sq_stats_5d[acc_id].values()))}
            ad_stats_1d[acc_id] = {x["id"]: x for x in fetch_account_breakdown_stats(token, acc_id, "ad", days_back=0)}
            ad_stats_2d[acc_id] = {x["id"]: x for x in fetch_account_breakdown_stats(token, acc_id, "ad", days_back=1)}
            ad_stats_3d[acc_id] = {x["id"]: x for x in fetch_account_breakdown_stats(token, acc_id, "ad", days_back=2)}
            ad_stats_4d[acc_id] = {x["id"]: x for x in (fetch_account_breakdown_stats(token, acc_id, "ad", days_back=3) or list(ad_stats_3d[acc_id].values()))}
            ad_stats_5d[acc_id] = {x["id"]: x for x in (fetch_account_breakdown_stats(token, acc_id, "ad", days_back=4) or list(ad_stats_4d[acc_id].values()))}
            ad_stats_7d[acc_id] = {x["id"]: x for x in (fetch_account_breakdown_stats(token, acc_id, "ad", days_back=6) or list(ad_stats_5d[acc_id].values()))}

    for item in CAMPAIGNS_TO_MANAGE:
        acc_name = item["account_name"]
        acc_id = item["account_id"]
        camp_id = item["campaign_id"]
        prod = item["prod_prefix"]
        mode_lbl = item["mode_lbl"]
        is_tc = item["is_target_cost"]
        daily_budget_micro = item["daily_budget_micro"]
        landing_url = item["landing_url"]

        rc = s.get(f"{API_BASE}/campaigns/{camp_id}", timeout=20).json()
        camp_obj = (rc.get("campaigns") or [{}])[0].get("campaign", {})
        if not camp_obj:
            continue
        camp_name = camp_obj.get("name", camp_id)
        if camp_obj.get("status") != "ACTIVE":
            print(f"⏭️ [{acc_name} | {camp_name}] الحملة موقوفة (PAUSED) — تخطي وعدم إعادة تشغيلها نهائياً.", flush=True)
            continue

        squads = get_all_pages(s, f"/campaigns/{camp_id}/adsquads?limit=1000", "adsquads", max_pages=5)
        ads = get_all_pages(s, f"/campaigns/{camp_id}/ads?limit=1000", "ads", max_pages=4)
        ads_by_sq = {}
        ad_id_set = set()
        for a in ads:
            ad_id_set.add(a["id"])
            ads_by_sq.setdefault(a.get("ad_squad_id"), []).append(a)

        # Ensure every ACTIVE AdSquad has all its ads loaded via fast parallel threads
        missing_active_sqs = [
            sq for sq in squads
            if sq.get("status") == "ACTIVE" and len(ads_by_sq.get(sq["id"], [])) < 5
        ]
        if missing_active_sqs:
            import concurrent.futures
            def _fetch_sq_ads_topup(sq_id_val):
                try:
                    return sq_id_val, get_all_pages(s, f"/adsquads/{sq_id_val}/ads?limit=100", "ads", max_pages=2)
                except Exception:
                    return sq_id_val, []
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                for sq_id_val, direct_ads in ex.map(_fetch_sq_ads_topup, [sq_m["id"] for sq_m in missing_active_sqs]):
                    for da in direct_ads:
                        if da["id"] not in ad_id_set:
                            ad_id_set.add(da["id"])
                            ads.append(da)
                            ads_by_sq.setdefault(sq_id_val, []).append(da)

        st1_sq = sq_stats_1d.get(acc_id, {})
        st2_sq = sq_stats_2d.get(acc_id, {})
        st3_sq = sq_stats_3d.get(acc_id, {})
        st4_sq = sq_stats_4d.get(acc_id, {})
        st5_sq = sq_stats_5d.get(acc_id, {})
        st7_sq = sq_stats_7d.get(acc_id, {})
        st1_ad = ad_stats_1d.get(acc_id, {})
        st2_ad = ad_stats_2d.get(acc_id, {})
        st3_ad = ad_stats_3d.get(acc_id, {})
        st4_ad = ad_stats_4d.get(acc_id, {})
        st5_ad = ad_stats_5d.get(acc_id, {})
        st7_ad = ad_stats_7d.get(acc_id, {})

        # ----------------------------------------------------------------------
        # 1) فحص الفيديوهات التراكمي (Cumulative Video Audit by MEDIA:<media_id>)
        #    - أي فيديو يصرف >= $7.00 بـ 0 مبيعات أو >= $7.00 بـ CPA > $7.00 (في 1D/2D/3D/4D/5D/7D)
        #      يُوقف في جميع المجموعات النشطة، ويُسجل في disabled_videos، ويُمنع من النسخ في أي أدست جديدة!
        # ----------------------------------------------------------------------
        squad_status_map = {sq["id"]: sq.get("status") for sq in squads if sq.get("id")}
        video_cum = {}
        for a in ads:
            aid = a.get("id")
            sq_id = a.get("ad_squad_id")
            is_act = (a.get("status") == "ACTIVE" and squad_status_map.get(sq_id) == "ACTIVE")
            if (
                not is_act
                and aid not in st1_ad
                and aid not in st2_ad
                and aid not in st3_ad
                and aid not in st4_ad
                and aid not in st5_ad
                and aid not in st7_ad
            ):
                continue
            cr_id = a.get("creative_id")
            mid = cmap.get(cr_id) or (resolve_video_media_id(cr_id, token, history) if cr_id else None)
            if not mid:
                continue
            g = video_cum.setdefault(mid, {
                "active_ads": [],
                "all_ads": [],
                "sp1": 0.0, "pu1": 0,
                "sp2": 0.0, "pu2": 0,
                "sp3": 0.0, "pu3": 0,
                "sp4": 0.0, "pu4": 0,
                "sp5": 0.0, "pu5": 0,
                "sp7": 0.0, "pu7": 0,
                "sample_name": a.get("name", mid),
            })
            g["all_ads"].append(a)
            if is_act:
                g["active_ads"].append(a)
            if aid in st1_ad:
                g["sp1"] = round(g["sp1"] + st1_ad[aid]["spend"], 2)
                g["pu1"] += st1_ad[aid]["purchases"]
            if aid in st2_ad:
                g["sp2"] = round(g["sp2"] + st2_ad[aid]["spend"], 2)
                g["pu2"] += st2_ad[aid]["purchases"]
            if aid in st3_ad:
                g["sp3"] = round(g["sp3"] + st3_ad[aid]["spend"], 2)
                g["pu3"] += st3_ad[aid]["purchases"]
            if aid in st4_ad:
                g["sp4"] = round(g["sp4"] + st4_ad[aid]["spend"], 2)
                g["pu4"] += st4_ad[aid]["purchases"]
            if aid in st5_ad:
                g["sp5"] = round(g["sp5"] + st5_ad[aid]["spend"], 2)
                g["pu5"] += st5_ad[aid]["purchases"]
            if aid in st7_ad:
                g["sp7"] = round(g["sp7"] + st7_ad[aid]["spend"], 2)
                g["pu7"] += st7_ad[aid]["purchases"]

        banned_media_ids = set()
        disabled_vids = history.setdefault("disabled_videos", {})

        # تحديد المجموعات الرابحة مسبقاً لحماية إعلاناتها الرابحة من الإطفاء التراكمي (CPA < $6.00 في كل النوافذ 1D..3D)
        winning_squad_ids = set()
        for sq_w in squads:
            sid_w = sq_w.get("id")
            if not sid_w:
                continue
            sw1 = st1_sq.get(sid_w, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            sw2 = st2_sq.get(sid_w, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            sw3 = st3_sq.get(sid_w, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            has_6_cpa_any = any(
                wx["purchases"] > 0 and round(wx["spend"] / wx["purchases"], 2) >= 6.00
                for wx in (sw1, sw2, sw3)
            )
            has_4_nosale_12 = any(wx["purchases"] == 0 and wx["spend"] >= 4.00 for wx in (sw1, sw2))
            w1_ok = (not has_6_cpa_any) and (not has_4_nosale_12) and sw1["purchases"] >= 1 and round(sw1["spend"] / sw1["purchases"], 2) < 6.00
            w3_ok = (
                (not has_6_cpa_any)
                and (not has_4_nosale_12)
                and sw3["purchases"] >= 1
                and round(sw3["spend"] / sw3["purchases"], 2) < 6.00
                and not (sw1["purchases"] == 0 and sw1["spend"] >= 1.00 and (sw1["swipes"] == 0 or sw1["cpc"] > 0.28))
            )
            if w1_ok or w3_ok:
                winning_squad_ids.add(sid_w)

        def is_protected_individual_ad(a_obj: dict) -> bool:
            aid_p = a_obj.get("id")
            sq_id_p = a_obj.get("ad_squad_id")
            sa1 = st1_ad.get(aid_p, {"spend": 0.0, "purchases": 0})
            sa3 = st3_ad.get(aid_p, {"spend": 0.0, "purchases": 0})
            if sa1["purchases"] >= 1 and (sa1["spend"] / sa1["purchases"]) <= 7.00:
                return True
            if (
                sa3["purchases"] >= 1
                and (sa3["spend"] / sa3["purchases"]) <= 7.00
                and not (sa1["purchases"] == 0 and sa1["spend"] >= 4.00)
                and not (sa1["purchases"] > 0 and (sa1["spend"] / sa1["purchases"]) > 7.00)
            ):
                return True
            if sq_id_p in winning_squad_ids and sa1["spend"] < 4.00 and sa3["spend"] < 4.00:
                return True
            return False

        for mid, g in video_cum.items():
            sp1, pu1 = g["sp1"], g["pu1"]
            sp2, pu2 = g["sp2"], g["pu2"]
            sp3, pu3 = g["sp3"], g["pu3"]
            sp4, pu4 = g["sp4"], g["pu4"]
            sp5, pu5 = g["sp5"], g["pu5"]
            sp7, pu7 = g["sp7"], g["pu7"]
            cpa1 = round(sp1 / pu1, 2) if pu1 > 0 else None
            cpa2 = round(sp2 / pu2, 2) if pu2 > 0 else None
            cpa3 = round(sp3 / pu3, 2) if pu3 > 0 else None
            cpa4 = round(sp4 / pu4, 2) if pu4 > 0 else None
            cpa5 = round(sp5 / pu5, 2) if pu5 > 0 else None
            cpa7 = round(sp7 / pu7, 2) if pu7 > 0 else None
            cpas_v = [x for x in (cpa1, cpa2, cpa3, cpa4, cpa5, cpa7) if x is not None]
            g["best_cpa"] = min(cpas_v) if cpas_v else 9999.0

            is_1d_win_v = pu1 >= 1 and cpa1 is not None and cpa1 <= 7.00
            is_3d_win_v = (
                pu3 >= 1
                and cpa3 is not None
                and cpa3 <= 7.00
                and not (sp1 >= 7.00 and pu1 == 0)
                and not (sp1 >= 7.00 and pu1 > 0 and cpa1 is not None and cpa1 > 7.00)
                and not (sp2 >= 7.00 and pu2 == 0)
                and not (sp2 >= 7.00 and pu2 > 0 and cpa2 is not None and cpa2 > 7.00)
            )
            is_7d_win_v = pu7 >= 1 and cpa7 is not None and cpa7 <= 7.00 and sp3 < 7.00

            # فحص النوافذ الست (1D / 2D / 3D / 4D / 5D / 7D)
            kill_reason = ""
            for w_lbl, w_sp, w_pu, w_cpa in (
                ("1D", sp1, pu1, cpa1),
                ("2D", sp2, pu2, cpa2),
                ("3D", sp3, pu3, cpa3),
                ("4D", sp4, pu4, cpa4),
                ("5D", sp5, pu5, cpa5),
                ("7D", sp7, pu7, cpa7),
            ):
                if w_lbl not in ("1D",) and is_1d_win_v:
                    continue
                if w_lbl in ("4D", "5D", "7D") and (is_3d_win_v or is_7d_win_v):
                    continue
                if w_sp >= 7.00 and w_pu == 0:
                    kill_reason = f"[Cumulative {w_lbl} >= $7.00 No-Sale] ${w_sp:.2f} / 0 sales"
                    break
                if w_sp >= 7.00 and w_pu > 0 and w_cpa is not None and w_cpa > 7.00:
                    kill_reason = f"[Cumulative {w_lbl} CPA > $7.00] ${w_sp:.2f} / {w_pu} sales (CPA=${w_cpa:.2f})"
                    break
            g["kill_reason"] = kill_reason

        full_catalog = VIDEOS_CATALOG.get((acc_id, prod), [])
        catalog_mids_list = [mid for (_, mid) in full_catalog]

        for mid, g in video_cum.items():
            sp1, pu1 = g["sp1"], g["pu1"]
            sp2, pu2 = g["sp2"], g["pu2"]
            sp3, pu3 = g["sp3"], g["pu3"]
            sp4, pu4 = g["sp4"], g["pu4"]
            sp5, pu5 = g["sp5"], g["pu5"]
            sp7, pu7 = g["sp7"], g["pu7"]
            kill_reason = g.get("kill_reason", "")
            is_proven_win_v = (
                (pu1 >= 1 and (sp1 / pu1) <= 7.00)
                or (pu3 >= 1 and (sp3 / pu3) <= 7.00 and sp1 < 7.00)
                or (pu7 >= 1 and (sp7 / pu7) <= 7.00 and sp3 < 7.00)
            )
            if not kill_reason:
                if is_proven_win_v:
                    for stale_k in (f"{camp_id}:MEDIA:{mid}", f"{camp_id}:{mid}", f"{camp_id}:SA:MEDIA:{mid}"):
                        disabled_vids.pop(stale_k, None)
                elif any(k in disabled_vids for k in (f"{camp_id}:MEDIA:{mid}", f"{camp_id}:{mid}", f"{camp_id}:SA:MEDIA:{mid}")):
                    banned_media_ids.add(mid)
                continue

            banned_media_ids.add(mid)
            if not dry_run:
                disabled_vids[f"{camp_id}:MEDIA:{mid}"] = {
                    "campaign_id": camp_id,
                    "campaign_name": camp_name,
                    "video_media_id": mid,
                    "sample_ad_name": g["sample_name"],
                    "closed_instances": len(g["active_ads"]),
                    "total_spend": max(sp1, sp2, sp3, sp4, sp5, sp7),
                    "total_purchases": max(pu1, pu2, pu3, pu4, pu5, pu7),
                    "reason": kill_reason,
                    "disabled_at": now_str,
                }

        # Build allowed videos list for creating new replacement Copy AdSquads
        catalog_mids = set(catalog_mids_list)
        banned_media_ids = {mid for mid in banned_media_ids if mid in catalog_mids}
        allowed_videos = [(lbl, mid) for (lbl, mid) in full_catalog if mid not in banned_media_ids]
        if not allowed_videos and full_catalog:
            # فقط في حال استبعاد جميع فيديوهات الكتالوج بالكامل، نحتفظ بأفضل فيديو كبديل عند إنشاء أدست جديدة دون إلغاء إيقاف النسخ الخاسرة الحالية
            allowed_videos = [full_catalog[0]]
        allowed_mids_set = {mid for (_, mid) in allowed_videos}

        # Pause 100% of active copies of banned accumulated videos across all AdSquads
        for mid in list(banned_media_ids):
            g = video_cum.get(mid)
            if not g or not g["active_ads"] or dry_run:
                continue
            by_sq_pause = {}
            for a_act in g["active_ads"]:
                a_act["status"] = "PAUSED"
                by_sq_pause.setdefault(a_act["ad_squad_id"], []).append(a_act)
            paused_copies_cnt = 0
            for sq_p_id, sq_p_ads in by_sq_pause.items():
                r_p = s.put(f"{API_BASE}/adsquads/{sq_p_id}/ads", json={"ads": sq_p_ads}, timeout=25)
                if r_p.ok:
                    paused_copies_cnt += len(sq_p_ads)
            if paused_copies_cnt > 0:
                kill_r_str = (disabled_vids.get(f"{camp_id}:MEDIA:{mid}") or {}).get("reason", "[Cumulative > $7.00]")
                print(
                    f"   🗑️ [{acc_name} | {camp_name}] إيقاف {paused_copies_cnt} نسخة للفيديو التراكمي الخاسر '{g['sample_name']}' (MEDIA:{mid[:8]}) | {kill_r_str}",
                    flush=True,
                )

        save_paused_history(history)

        # ----------------------------------------------------------------------
        # 2) فحص وإيقاف أي مجموعة Copy نشطة خاسرة أو مخالفة للاستراتيجية
        # ----------------------------------------------------------------------
        expected_strat = "TARGET_COST" if is_tc else "LOWEST_COST_WITH_MAX_BID"
        expected_target_bid = True if is_tc else False

        active_copy_squads = []
        losing_or_invalid_squads = []
        healed_winning_squads = []
        now_utc_chk = datetime.now(timezone.utc)
        initial_active_copy_cnt = sum(
            1
            for sq_i in squads
            if sq_i.get("status") == "ACTIVE"
            and "copy" in (sq_i.get("name") or "").lower()
            and not is_non_copy_test_adsquad(sq_i.get("name") or "")
        )

        def _is_future_sched_sq(sq_obj: dict) -> bool:
            st_raw = str(sq_obj.get("start_time") or "").strip()
            if not st_raw:
                return False
            try:
                dt_s = datetime.fromisoformat(st_raw.replace("Z", "+00:00"))
                return now_utc_chk < dt_s
            except Exception:
                return False

        for sq in squads:
            nm = (sq.get("name") or "").strip()
            if nm.lower().startswith("base -") or is_non_copy_test_adsquad(nm):
                continue
            sid = sq["id"]
            s1 = st1_sq.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            s2 = st2_sq.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            s3 = st3_sq.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            s4 = st4_sq.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            s5 = st5_sq.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})
            s7 = st7_sq.get(sid, {"spend": 0.0, "purchases": 0, "cpa": None, "cpc": 0.0, "swipes": 0})

            has_6_cpa_123 = any(
                wx["purchases"] > 0 and round(wx["spend"] / wx["purchases"], 2) >= 6.00
                for wx in (s1, s2, s3)
            )
            has_6_cpa_any_win = any(
                wx["purchases"] > 0 and round(wx["spend"] / wx["purchases"], 2) >= 6.00
                for wx in (s1, s2, s3, s4, s5, s7)
            )
            has_4_nosale_12 = any(wx["purchases"] == 0 and wx["spend"] >= 4.00 for wx in (s1, s2))

            is_win_1d = (
                (not has_6_cpa_123)
                and s1["purchases"] >= 1
                and round(s1["spend"] / s1["purchases"], 2) < 6.00
            )
            is_win_3d = (
                (not has_6_cpa_123)
                and (not has_4_nosale_12)
                and s3["purchases"] >= 1
                and round(s3["spend"] / s3["purchases"], 2) < 6.00
                and not (s1["purchases"] == 0 and s1["spend"] >= 1.00 and (s1["swipes"] == 0 or s1["cpc"] > 0.28))
                and not (s2["purchases"] == 0 and s2["spend"] >= 1.00 and (s2["swipes"] == 0 or s2["cpc"] > 0.28))
            )

            if sq.get("status") != "ACTIVE":
                p_rec_sq = (history.get("adsquads") or {}).get(sid) or {}
                p_rsn_sq = str(p_rec_sq.get("reason") or "")
                was_paused_by_rule = any(
                    k in p_rsn_sq for k in ("CPC >", "Stop-Loss", "CPA >=", "CPA >", "6.00", "7.00", "Manual", "Max 30")
                )
                sq_matches_mode = (" tc " in nm.lower() or sq.get("bid_strategy") == "TARGET_COST" or bool(sq.get("target_bid"))) == is_tc
                can_heal_winner = (
                    (is_win_1d or is_win_3d)
                    and (not was_paused_by_rule)
                    and (not has_6_cpa_any_win)
                    and initial_active_copy_cnt < 30
                )
                # عين المراقب البشري: لا تعيد أبداً تشغيل أي أدست Copy أُوقفت بسبب CPA >= $6.00 أو Stop-Loss أو عند اكتمال 30/30 خانة!
                if camp_id != "9be45a26-06ac-4716-8e7b-2fb3a4d65e4c" and can_heal_winner:
                    sq["status"] = "ACTIVE"
                    healed_winning_squads.append(sq)
                    initial_active_copy_cnt += 1
                    history.get("adsquads", {}).pop(sid, None)
                    sq_ads_p = ads_by_sq.get(sid, [])
                    if not sq_ads_p:
                        sq_ads_p = get_all_pages(s, f"/adsquads/{sid}/ads?limit=100", "ads", max_pages=2)
                        ads_by_sq[sid] = sq_ads_p
                        for da_p in sq_ads_p:
                            cr_p = da_p.get("creative_id")
                            if cr_p and cr_p not in cmap:
                                resolve_video_media_id(cr_p, token, history)
                    if sq_ads_p and not any(a.get("status") == "ACTIVE" for a in sq_ads_p) and not dry_run:
                        ads_to_revive = [
                            a_o for a_o in sq_ads_p
                            if is_protected_individual_ad(a_o)
                            or (cmap.get(a_o.get("creative_id")) in allowed_mids_set and st1_ad.get(a_o["id"], {}).get("spend", 0.0) < 4.00)
                        ]
                        if not ads_to_revive:
                            ads_to_revive = [a_o for a_o in sq_ads_p if cmap.get(a_o.get("creative_id")) in allowed_mids_set] or list(sq_ads_p)
                        for a_obj in ads_to_revive:
                            a_obj["status"] = "ACTIVE"
                        try:
                            s.put(f"{API_BASE}/adsquads/{sid}/ads", json={"ads": ads_to_revive}, timeout=20)
                        except Exception:
                            pass
                    if "copy" in nm.lower():
                        active_copy_squads.append(sq)
                continue

            if "copy" not in nm.lower():
                # أي أدست لا تحتوي على كلمة Copy (ما فيها كوبي): ممنوع إطفاء الأدست نفسها أبداً، يتابع البوت الفيديوهات الفردية بداخلها فقط!
                continue

            if _is_future_sched_sq(sq):
                active_copy_squads.append(sq)
                continue

            sq_ads_curr = ads_by_sq.get(sid, [])
            active_ads_in_sq = [a for a in sq_ads_curr if a.get("status") == "ACTIVE"]
            has_zero_active_ads = False
            if len(sq_ads_curr) > 0 and len(active_ads_in_sq) == 0:
                direct_chk = get_all_pages(s, f"/adsquads/{sid}/ads?limit=100", "ads", max_pages=2)
                if direct_chk and not any(a.get("status") == "ACTIVE" for a in direct_chk):
                    # إذا كانت المجموعة رابحة أو سليمة وبداخلها فيديوهات مسموحة أُطفئت سابقاً بسبب قفل قديم، نعيد تفعيل الفيديوهات المسموحة/الرابحة بدلاً من إطفاء المجموعة!
                    revivable_ads = [
                        a_d for a_d in direct_chk
                        if is_protected_individual_ad(a_d)
                        or (
                            (cmap.get(a_d.get("creative_id")) in allowed_mids_set)
                            and st1_ad.get(a_d["id"], {}).get("spend", 0.0) < 4.00
                            and st3_ad.get(a_d["id"], {}).get("spend", 0.0) < 4.00
                        )
                    ]
                    if (is_win_1d or is_win_3d) and not revivable_ads:
                        revivable_ads = list(direct_chk)
                    if revivable_ads and not dry_run:
                        for a_r in revivable_ads:
                            a_r["status"] = "ACTIVE"
                        try:
                            s.put(f"{API_BASE}/adsquads/{sid}/ads", json={"ads": revivable_ads}, timeout=20)
                            has_zero_active_ads = False
                        except Exception:
                            has_zero_active_ads = True
                    else:
                        has_zero_active_ads = True

            pause_reason = ""
            for w_lbl, sw_st in [("1D", s1), ("2D", s2), ("3D", s3)]:
                if sw_st["purchases"] > 0 and round(sw_st["spend"] / sw_st["purchases"], 2) >= 6.00:
                    pause_reason = f"[{w_lbl} Copy CPA >= $6.00] Spend=${sw_st['spend']:.2f}, Sales={sw_st['purchases']}, CPA=${sw_st['spend']/sw_st['purchases']:.2f}"
                    break
            if not pause_reason and not is_win_1d:
                for w_lbl, sw_st in [("1D", s1), ("2D", s2)]:
                    if sw_st["purchases"] == 0 and sw_st["spend"] >= 4.00:
                        pause_reason = f"[{w_lbl} >= $4.00 Stop-Loss] Spend=${sw_st['spend']:.2f}, 0 sales"
                        break
            if not pause_reason:
                if is_win_1d or is_win_3d:
                    pause_reason = ""
                elif has_zero_active_ads:
                    pause_reason = "[Empty AdSquad: 0 Active Ads]"
                else:
                    windows_to_check = [("1D", s1), ("2D", s2), ("3D", s3), ("4D", s4), ("5D", s5), ("7D", s7)]
                    for w_lbl, sw_st in windows_to_check:
                        sp_w = sw_st["spend"]
                        p_w = sw_st["purchases"]
                        sw_w = sw_st["swipes"]
                        cpc_w = sw_st["cpc"]
                        if sp_w < 1.00:
                            continue
                        if p_w == 0 and sp_w >= 1.00 and (sw_w == 0 or cpc_w > 0.28):
                            pause_reason = f"[{w_lbl} CPC > $0.28] Spend=${sp_w:.2f}, 0 sales, Swipes={sw_w}, CPC=${cpc_w:.3f}"
                            break
                        if p_w == 0 and sp_w >= 4.00:
                            pause_reason = f"[{w_lbl} >= $4.00 Stop-Loss] Spend=${sp_w:.2f}, 0 sales"
                            break
                        if p_w > 0 and round(sp_w / p_w, 2) >= 6.00:
                            pause_reason = f"[{w_lbl} Copy CPA >= $6.00] Spend=${sp_w:.2f}, Sales={p_w}, CPA=${sp_w/p_w:.2f}"
                            break

            if pause_reason:
                losing_or_invalid_squads.append((sq, pause_reason))
            else:
                active_copy_squads.append(sq)

        if healed_winning_squads and not dry_run:
            for i in range(0, len(healed_winning_squads), 20):
                s.put(f"{API_BASE}/campaigns/{camp_id}/adsquads", json={"adsquads": healed_winning_squads[i : i + 20]}, timeout=25)
            save_paused_history(history)
            print(
                f"   👁️✅ [{acc_name} | {camp_name}] عين المراقب البشري أعادت تشغيل {len(healed_winning_squads)} أدست رابحة (CPA < $6.00) كانت مطفأة بالخطأ!",
                flush=True,
            )

        if losing_or_invalid_squads and not dry_run:
            to_pause_objs = []
            for sq_p, p_reason in losing_or_invalid_squads:
                sq_p["status"] = "PAUSED"
                to_pause_objs.append(sq_p)
                history["adsquads"][sq_p["id"]] = {
                    "name": sq_p.get("name", ""),
                    "campaign": camp_name,
                    "reason": p_reason,
                    "paused_at": now_str,
                }
            for i in range(0, len(to_pause_objs), 20):
                s.put(f"{API_BASE}/campaigns/{camp_id}/adsquads", json={"adsquads": to_pause_objs[i : i + 20]}, timeout=25)
            save_paused_history(history)
            print(
                f"   🛑 [{acc_name} | {camp_name}] تم إطفاء {len(to_pause_objs)} أدست خاسرة/مخالفة ليتم استبدالها بنسخ جديدة نظيفة.",
                flush=True,
            )

        # ----------------------------------------------------------------------
        # 3) مطابقة الخانات الـ 30 (#01..#30 ببيد $2.40 + $0.30 للحملات بدون صرف) وإنشاء نسخ جديدة لأي خانة ناقصة
        #    - ممنوع إعادة تشغيل الأدست الموقوفة الخاسرة؛ دائماً ننشئ أدست جديدة تماماً!
        # ----------------------------------------------------------------------
        camp_1d_spend = sum(st1_sq.get(sq_x["id"], {}).get("spend", 0.0) for sq_x in squads)
        no_zero_bump = bool(item.get("no_zero_spend_bump"))
        is_zero_spend_camp = (camp_1d_spend < 0.05) and (not no_zero_bump)
        extra_bid_micro = 300_000 if is_zero_spend_camp else 0
        custom_base_bid = item.get("base_bid_micro")
        custom_bid_str = item.get("bid_str")
        custom_force_hl = item.get("force_hl_tag")

        slots_30 = build_30_slot_specs(
            prod,
            is_target_cost=is_tc,
            extra_bid_micro=extra_bid_micro,
            base_bid_micro=custom_base_bid,
            bid_str=custom_bid_str,
            force_hl_tag=custom_force_hl,
        )
        slot_by_idx = {sl["index"]: sl for sl in slots_30}

        def _sq_rank(sq_item: dict):
            sid_k = sq_item["id"]
            s1_k = st1_sq.get(sid_k, {"spend": 0.0, "purchases": 0})
            s3_k = st3_sq.get(sid_k, {"spend": 0.0, "purchases": 0})
            p1_k = s1_k.get("purchases", 0)
            p3_k = s3_k.get("purchases", 0)
            w1 = 1 if (p1_k >= 1 and round(s1_k.get("spend", 0.0) / p1_k, 2) < 6.00) else 0
            w3 = 1 if (p3_k >= 1 and round(s3_k.get("spend", 0.0) / p3_k, 2) < 6.00) else 0
            f_sched = 1 if _is_future_sched_sq(sq_item) else 0
            return (w1, w3, f_sched, p3_k + p1_k, round(s1_k.get("spend", 0.0), 2), round(s3_k.get("spend", 0.0), 2))

        active_copy_squads.sort(key=_sq_rank, reverse=True)
        kept_top_30 = active_copy_squads[:30]
        excess_beyond_30 = active_copy_squads[30:]

        # حماية أي مجموعة رابحة (< $6.00) أو مجدولة للمستقبل حتى لو تجاوزت 30
        unassigned_to_pause = []
        for sq_ex in excess_beyond_30:
            sid_ex = sq_ex["id"]
            s1_ex = st1_sq.get(sid_ex, {"spend": 0.0, "purchases": 0})
            s3_ex = st3_sq.get(sid_ex, {"spend": 0.0, "purchases": 0})
            if (
                _is_future_sched_sq(sq_ex)
                or (s1_ex.get("purchases", 0) >= 1 and round(s1_ex["spend"] / s1_ex["purchases"], 2) < 6.00)
                or (s3_ex.get("purchases", 0) >= 1 and round(s3_ex["spend"] / s3_ex["purchases"], 2) < 6.00)
            ):
                kept_top_30.append(sq_ex)
            else:
                unassigned_to_pause.append(sq_ex)

        assigned_by_idx = {}
        unassigned_active = []
        for sq in kept_top_30[:30]:
            m = re.search(r"#(\d+)", sq.get("name") or "")
            idx = int(m.group(1)) if m else None
            if idx and 1 <= idx <= 30 and idx not in assigned_by_idx:
                assigned_by_idx[idx] = sq
            else:
                unassigned_active.append(sq)

        for idx in range(1, 31):
            if idx not in assigned_by_idx and unassigned_active:
                assigned_by_idx[idx] = unassigned_active.pop(0)

        # If any non-winning Copy AdSquads remain beyond 30, pause the excess
        if unassigned_to_pause and not dry_run:
            for sq_ex in unassigned_to_pause:
                sq_ex["status"] = "PAUSED"
            for i in range(0, len(unassigned_to_pause), 20):
                s.put(f"{API_BASE}/campaigns/{camp_id}/adsquads", json={"adsquads": unassigned_to_pause[i : i + 20]}, timeout=25)
            print(f"   🛑 [{acc_name} | {camp_name}] إيقاف {len(unassigned_to_pause)} أدست غير رابحة زائدة فوق سقف الـ 30.", flush=True)

        # Normalize name, bid ($2.40..$2.80 for Max / $3.00..$3.40 for TC with up to 4 low-spend +$0.10 bumps, and +$0.30 for 0-spend campaigns), and bid_strategy on existing healthy active squads
        expected_bid_micro = custom_base_bid if custom_base_bid is not None else (TC_BID_MICRO if is_tc else MAX_BID_MICRO)
        expected_bid_lbl = (
            f"${(expected_bid_micro + extra_bid_micro) / 1_000_000:.2f}"
            if custom_base_bid is not None
            else (("$3.30" if is_zero_spend_camp else "$3.00") if is_tc else ("$2.70" if is_zero_spend_camp else "$2.40"))
        )
        squads_to_normalize = []
        for idx, sq in assigned_by_idx.items():
            sl = slot_by_idx[idx]
            cur_b = int(sq.get("bid_micro") or 0)
            b_rec = (history.get("bid_bumps") or {}).get(sq["id"]) or {}
            rec_bumps = (
                int(b_rec.get("bumps_count", 0))
                if (isinstance(b_rec, dict) and b_rec.get("schema_v") == 2 and b_rec.get("base_bid_micro") == expected_bid_micro)
                else 0
            )
            sq_is_actually_tc = sq.get("bid_strategy") == "TARGET_COST" or bool(sq.get("target_bid"))
            sq_base_bid = custom_base_bid if custom_base_bid is not None else (TC_BID_MICRO if sq_is_actually_tc else (TC_BID_MICRO if is_tc else MAX_BID_MICRO))
            diff_b = cur_b - sq_base_bid
            api_bumps = (diff_b // 100_000) if (0 <= diff_b <= 400_000 and diff_b % 100_000 == 0) else 0
            min_zero_bumps = 3 if (is_zero_spend_camp and not _is_future_sched_sq(sq)) else 0
            valid_bumps = min(4, max(min_zero_bumps, rec_bumps, api_bumps))
            expected_sq_bid_micro = sq_base_bid + (valid_bumps * 100_000)

            if sq.get("name") != sl["name"] or cur_b != expected_sq_bid_micro:
                sq["name"] = sl["name"]
                sq["bid_micro"] = expected_sq_bid_micro
                squads_to_normalize.append(sq)

            # Ensure banned videos are PAUSED inside this healthy squad (and NEVER re-enable paused ads inside existing squads!)
            sq_ads_list = ads_by_sq.get(sq["id"], [])
            ads_to_upd_in_sq = []
            for a_sq in sq_ads_list:
                cr_id_a = a_sq.get("creative_id")
                mid_a = cmap.get(cr_id_a)
                if a_sq.get("status") == "ACTIVE":
                    if mid_a and mid_a in banned_media_ids:
                        a_sq["status"] = "PAUSED"
                        ads_to_upd_in_sq.append(a_sq)
            if ads_to_upd_in_sq and not dry_run:
                s.put(f"{API_BASE}/adsquads/{sq['id']}/ads", json={"ads": ads_to_upd_in_sq}, timeout=20)

        if squads_to_normalize and not dry_run:
            for i in range(0, len(squads_to_normalize), 10):
                try:
                    s.put(f"{API_BASE}/campaigns/{camp_id}/adsquads", json={"adsquads": squads_to_normalize[i : i + 10]}, timeout=35)
                except Exception as e_norm:
                    print(f"   ⚠️ [{acc_name} | {camp_name}] تحذير أثناء تحديث أسماء/بيدات الأدست: {e_norm}", flush=True)

        missing_slots = [slot_by_idx[idx] for idx in range(1, 31) if idx not in assigned_by_idx]

        if missing_slots:
            if dry_run:
                print(
                    f"🔍 [Dry-Run | {acc_name} | {camp_name}] النشط حالياً: {len(assigned_by_idx)}/30 | سيتم إنشاء {len(missing_slots)} أدست Copy جديدة ببيد {expected_bid_lbl} ({expected_strat}) مع {len(allowed_videos)} فيديو مسموح (استبعاد {len(banned_media_ids)} فيديو خاسر تراكمياً).",
                    flush=True,
                )
            else:
                hl_h1 = HEADLINES[prod]["H1"]
                hl_h2 = HEADLINES[prod]["H2"]
                ads_by_hl = {"H1": [], "H2": []}
                for v_label, mid in allowed_videos:
                    cid_h1 = get_or_create_creative_id(s, acc_id, v_label, mid, landing_url, "H1", hl_h1, history)
                    cid_h2 = get_or_create_creative_id(s, acc_id, v_label, mid, landing_url, "H2", hl_h2, history)
                    ads_by_hl["H1"].append({"label": f"{v_label}_m{mid[:6]} [H1]", "creative_id": cid_h1, "media_id": mid})
                    ads_by_hl["H2"].append({"label": f"{v_label}_m{mid[:6]} [H2]", "creative_id": cid_h2, "media_id": mid})

                created_sq_ids = create_new_squads_with_filtered_ads(
                    s=s,
                    camp_id=camp_id,
                    slots_to_create=missing_slots,
                    ads_by_hl=ads_by_hl,
                    daily_budget_micro=daily_budget_micro,
                )
                print(
                    f"♻️✅ [{acc_name} | {camp_name}] تم إنشاء {len(created_sq_ids)} أدست Copy بديلة جديدة ببيد {expected_bid_lbl} ({expected_strat}) "
                    f"| الإجمالي النشط الآن: {len(assigned_by_idx) + len(created_sq_ids)}/30 | الفيديوهات المسموحة: {len(allowed_videos)}/{len(full_catalog)} (المستبعدة تراكمياً: {len(banned_media_ids)})",
                    flush=True,
                )
        else:
            print(
                f"✅ [{acc_name} | {camp_name}] مكتملة 30/30 أدست Copy نشطة ببيد {expected_bid_lbl} ({expected_strat}) | الفيديوهات المسموحة: {len(allowed_videos)}/{len(full_catalog)} (المستبعدة تراكمياً: {len(banned_media_ids)})",
                flush=True,
            )


def enforce_exact_15_per_cohort(target_start_iso: str = "2026-10-24T03:00:00.000Z"):
    main(dry_run=False)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    main(dry_run=dry)
