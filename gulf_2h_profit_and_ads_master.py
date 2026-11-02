#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gulf Hourly Profit, Pricing, Funnel & Multi-Platform Ads Master Automation (0 * * * *)
(Qatar, Oman, Kuwait — Car Armrest Cushion $3.00 [QA & KW Only], Power Bank $3.70 [QA & OM], Flashlight $5.00 [QA & OM])

Gulf COD Partner Fee Structure (Official User Spec 2026-10-31):
- Call Center: Leads = $0.50 | Confirmed = $1.00 | Delivered = $2.00
- Shipping & COD: Shipped = $4.99 | Delivered = $0.50 | Returned = $0.00
- COD + Exchange Fees: 5.0% + 1.5% = 6.5% (Net collected = 93.5% = 0.935 * P_USD)
- Exact Break-Even CPA per Raw Lead:
  Break_Even_CPA = D_net * (0.935 * P_USD - N * u - 2.50) - 5.99 * C - 0.50
"""

import concurrent.futures
import datetime
import json
import socket
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")

# --- Mandatory Cloudflare 1.1.1.1 DoH DNS Fallback ---
_orig_getaddrinfo = socket.getaddrinfo
_dns_cache = {}
_STATIC_DNS_FALLBACK = {
    "api.cod.partners": "207.154.249.26",
    "services.lightfunnels.com": "172.67.74.93",
    "assets.lightfunnels.com": "104.26.2.51",
    "www.daralrimal.shop": "172.67.154.115",
    "www.daralrimal.org": "104.21.80.1",
    "adsapi.snapchat.com": "34.110.187.193",
    "accounts.snapchat.com": "34.149.46.130",
    "ads.tiktok.com": "23.48.204.205",
}


def _doh_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if not host or str(host).replace(".", "").isdigit() or ":" in str(host):
        return _orig_getaddrinfo(host, port, family, type, proto, flags)
    if host in _dns_cache:
        return _orig_getaddrinfo(_dns_cache[host], port, socket.AF_INET, type, proto, flags)
    try:
        req = urllib.request.Request(
            f"https://1.1.1.1/dns-query?name={host}&type=A",
            headers={"accept": "application/dns-json"},
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        ips = [a["data"] for a in r.get("Answer", []) if a.get("type") == 1]
        if ips:
            _dns_cache[host] = ips[0]
            return _orig_getaddrinfo(ips[0], port, socket.AF_INET, type, proto, flags)
    except Exception:
        pass
    if host in _STATIC_DNS_FALLBACK:
        _dns_cache[host] = _STATIC_DNS_FALLBACK[host]
        return _orig_getaddrinfo(_dns_cache[host], port, socket.AF_INET, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = _doh_getaddrinfo

BASE_DIR = Path(__file__).resolve().parent
REPORT_MD = BASE_DIR / "gulf_2h_master_latest_report.md"
REPORT_JSON = BASE_DIR / "gulf_2h_master_latest.json"
LEDGER_JSON = BASE_DIR / "gulf_2h_actions_ledger.json"

# --- Landed Unit Costs (USD) in Gulf ---
UNIT_COSTS_USD = {
    "cushion": 3.00,     # الوسادة واصلة لقطر والكويت فقط = $3.00 (عمان ما فيها وسادة)
    "powerbank": 3.70,   # الباور بانك واصل لقطر وعمان = $3.70
    "flashlight": 5.00,  # الكشاف واصل لعمان وقطر = $5.00
}

# --- Currency to USD Conversion Rates ---
FX_TO_USD = {
    "QAR": 1.0 / 3.64,   # 1 USD = 3.64 QAR
    "OMR": 2.60,         # 1 OMR = 2.60 USD
    "KWD": 3.26,         # 1 KWD = 3.26 USD
}

# --- Gulf Funnels Catalog (Oman = Power Bank + Flashlight ONLY; Qatar = Power Bank + Cushion + Flashlight; Kuwait = Cushion ONLY) ---
# 2026-11-01 Dynamic Price & AOV Optimization Ladders (3 Visible Bundles + 2 Hidden Special Post-Purchase Upsell Offers):
# - OM Power Bank (prod_1RHYwQSklxo1B6QsGmGDo): [16, 22, 27] OMR + Upsell [21 (+5 OMR), 26 (+4 OMR)]
# - OM Flashlight (prod_zvvyyUSTG7IJJ7ZcgmhNw): [15, 20, 24] OMR + Upsell [19 (+4 OMR), 23 (+3 OMR)]
# - QA Power Bank (prod_JEBZMDbvdF5-oQIPGC-yS): [159, 209, 259] QAR + Upsell [199 (+40 QAR), 249 (+40 QAR)]
# - QA Flashlight (prod_fq4HmhTp9MP4hsqJLwVCy): [159, 209, 259] QAR + Upsell [199 (+40 QAR), 249 (+40 QAR)]
# - QA Cushion    (prod_2x9ObKhQV1DtTPdcvBXrq): [149, 199, 249] QAR + Upsell [189 (+40 QAR), 239 (+40 QAR)]
# - KW Cushion    (prod_QBo2KWY0uMzKEGx93DqJk): [13, 18, 21] KWD  + Upsell [17 (+4 KWD), 20 (+2 KWD)]
GULF_FUNNELS = [
    {
        "country": "OM",
        "currency": "OMR",
        "product_type": "powerbank",
        "product_id": "prod_1RHYwQSklxo1B6QsGmGDo",
        "funnel_id": "fun_-NGM4CVYpVNR84_ee7U6v",
        "name": "باور بانك 4 في 1 oman",
        "slug": "UxRztVJfV",
        "prices_local": [16, 22, 27],
        "upsell_prices_local": [21, 26],
    },
    {
        "country": "OM",
        "currency": "OMR",
        "product_type": "flashlight",
        "product_id": "prod_zvvyyUSTG7IJJ7ZcgmhNw",
        "funnel_id": "fun_jh8_ymVwDKWGXnQcbumZW",
        "name": "led oman snap / tiktok",
        "slug": "UeLjFk4iq",
        "prices_local": [15, 20, 24],
        "upsell_prices_local": [19, 23],
    },
    {
        "country": "QA",
        "currency": "QAR",
        "product_type": "powerbank",
        "product_id": "prod_JEBZMDbvdF5-oQIPGC-yS",
        "funnel_id": "fun_3dcVXR9MxtsnHNURA3vBX",
        "name": "باور بانك 4 في 1 partner (Qatar)",
        "slug": "W0HzKNvvm",
        "prices_local": [159, 209, 259],
        "upsell_prices_local": [199, 249],
    },
    {
        "country": "QA",
        "currency": "QAR",
        "product_type": "powerbank",
        "product_id": "prod_JEBZMDbvdF5-oQIPGC-yS",
        "funnel_id": "fun_o4sTJl4Z3_VRMTzPEoJ6H",
        "name": "باور بانك 4 في 1 qatar tiktok network",
        "slug": "-sadZWthv",
        "prices_local": [159, 209, 259],
        "upsell_prices_local": [199, 249],
    },
    {
        "country": "QA",
        "currency": "QAR",
        "product_type": "powerbank",
        "product_id": "prod_JEBZMDbvdF5-oQIPGC-yS",
        "funnel_id": "fun_Wkdhy4N32Z6KcEZgk0qpx",
        "name": "باور بانك 4 في 1 qatar google",
        "slug": "mXYlz1bqb",
        "prices_local": [159, 209, 259],
        "upsell_prices_local": [199, 249],
    },
    {
        "country": "QA",
        "currency": "QAR",
        "product_type": "cushion",
        "product_id": "prod_2x9ObKhQV1DtTPdcvBXrq",
        "funnel_id": "fun_dYIpIU72dxoXircCvW31h",
        "name": "وسادة مسند السيارة qatar snap new",
        "slug": "Yg1vVQ4in",
        "prices_local": [149, 199, 249],
        "upsell_prices_local": [189, 239],
    },
    {
        "country": "QA",
        "currency": "QAR",
        "product_type": "cushion",
        "product_id": "prod_2x9ObKhQV1DtTPdcvBXrq",
        "funnel_id": "fun_C99HYNN4nTt5SlRTa8NiE",
        "name": "وسادة مسند السيارة qatar",
        "slug": "IRGXR83x7",
        "prices_local": [149, 199, 249],
        "upsell_prices_local": [189, 239],
    },
    {
        "country": "QA",
        "currency": "QAR",
        "product_type": "cushion",
        "product_id": "prod_pOsP9mDoYSUp9zNqr3Reb",
        "funnel_id": "fun_IXv9OvG0oRrIqb6hJOJnr",
        "name": "وسادة مسند السيارة qatar google",
        "slug": "a1PzoOGg6",
        "prices_local": [149, 199, 249],
        "upsell_prices_local": [189, 239],
    },
    {
        "country": "QA",
        "currency": "QAR",
        "product_type": "flashlight",
        "product_id": "prod_fq4HmhTp9MP4hsqJLwVCy",
        "funnel_id": "fun_ATzAQXgDf7g-RLzwYkWEF",
        "name": "led qatar tiktok",
        "slug": "SNQXs4ssJ",
        "prices_local": [159, 209, 259],
        "upsell_prices_local": [199, 249],
    },
    {
        "country": "QA",
        "currency": "QAR",
        "product_type": "flashlight",
        "product_id": "prod_leGjMquCtetyCHG8Mym1I",
        "funnel_id": "fun_wuwJdO8xLaFnGbiRzaRIN",
        "name": "led qat snap",
        "slug": "gHQY2MZ4t",
        "prices_local": [159, 209, 259],
        "upsell_prices_local": [199, 249],
    },
    {
        "country": "KW",
        "currency": "KWD",
        "product_type": "cushion",
        "product_id": "prod_IkbQSCNQ8s8D8tY3SXdY9",
        "funnel_id": "fun_gtu7vbwSNr3hXfUO2Hg_n",
        "name": "وسادة مسند السيارة kuwait",
        "slug": "sBpgVmRvQ",
        "prices_local": [13, 18, 21],
        "upsell_prices_local": [17, 20],
    },
    {
        "country": "KW",
        "currency": "KWD",
        "product_type": "cushion",
        "product_id": "prod_IkbQSCNQ8s8D8tY3SXdY9",
        "funnel_id": "fun_iawosU1ClAyYOnyGRIhOW",
        "name": "وسادة مسند السيارة kuwait tiktok",
        "slug": "OWhuIgTpF",
        "prices_local": [13, 18, 21],
        "upsell_prices_local": [17, 20],
    },
    {
        "country": "KW",
        "currency": "KWD",
        "product_type": "cushion",
        "product_id": "prod_IkbQSCNQ8s8D8tY3SXdY9",
        "funnel_id": "fun_apVgFB7wCSBb9dZv8EGd8",
        "name": "وسادة مسند السيارة kuwait google",
        "slug": "U4z-pcc7g",
        "prices_local": [13, 18, 21],
        "upsell_prices_local": [17, 20],
    },
]

# --- Exact COD Partner Per-Country Warehouse Inventory Map ---
GULF_WAREHOUSE_PIDS = {
    "KW": {
        "cushion": [
            {"pid": 9605, "color": "الأسود", "sku": "CopNourAbdallaRestBlack", "wh": "kwt_warehouse_4"},
            {"pid": 11443, "color": "البيج", "sku": "CopNourAbdallaRestBeige", "wh": "kwt_warehouse_4"},
            {"pid": 11483, "color": "الرمادي", "sku": "CopNourAbdallaRestGrey", "wh": "kwt_warehouse_4"},
        ],
    },
    "QA": {
        "cushion": [
            {"pid": 9605, "color": "الأسود", "sku": "CopNourAbdallaRestBlack", "wh": "QAR_warehouse_3"},
            {"pid": 11442, "color": "البيج", "sku": "CopNourAbdallaRestBeige", "wh": "QAR_warehouse_3"},
        ],
        "powerbank": [
            {"pid": 10369, "color": "الأسود", "sku": "CopNourAbdallaPowerBlack", "wh": "QAR_warehouse_3"},
            {"pid": 10370, "color": "الأبيض", "sku": "CopNourAbdallaPowerWhite", "wh": "QAR_warehouse_3"},
            {"pid": 10371, "color": "البنفسجي", "sku": "CopNourAbdallaPowerPurple", "wh": "QAR_warehouse_3"},
        ],
        "flashlight": [
            {"pid": 8516, "color": "الأسود", "sku": "CopNourAbdallaLight", "wh": "QAR_warehouse_3"},
        ],
    },
    "OM": {
        "powerbank": [
            {"pid": 11521, "color": "الأسود", "sku": "CopNourAbdallaPowerBlack", "wh": "OMN_Warehouse_2"},
            {"pid": 11522, "color": "الأبيض", "sku": "CopNourAbdallaPowerWhite", "wh": "OMN_Warehouse_2"},
            {"pid": 11524, "color": "البنفسجي", "sku": "CopNourAbdallaPowerPurple", "wh": "OMN_Warehouse_2"},
        ],
        "flashlight": [
            {"pid": 8516, "color": "الأسود", "sku": "CopNourAbdallaLight", "wh": "OMN_Warehouse_2"},
            {"pid": 11536, "color": "الفضي", "sku": "CopNourAbdallaLightSilver", "wh": "OMN_Warehouse_2"},
        ],
    },
}

# --- Snapchat Gulf Campaigns & Bid/Structure Experiments (CSP9860: 350cb560-ad77-4fb5-9bbf-4c090c9ad081) ---
# Strict Zero-Loss Simulation Cost Caps (2026-11-01 User Rule: Cost must NEVER exceed each country's COD Partner Simulation):
# - OM Flashlight: Sim Safe CPA = $6.69 (1u BE = $7.81) | Squad No-Sale Stop = $3.00 | Campaign Stop = $6.69
# - OM Power Bank: Sim Safe CPA = $7.87 (1u BE = $9.12) | Squad No-Sale Stop = $3.00 | Campaign Stop = $7.87
# - QA Cushion:    Sim Safe CPA = $10.00 (1u BE = $12.88) | Squad No-Sale Stop = $3.20 | Campaign Stop = $10.00
# - KW Cushion:    Sim Safe CPA = $11.48 (1u BE = $14.19) | Squad No-Sale Stop = $3.20 | Campaign Stop = $11.48
# - QA Power Bank: Sim Safe CPA = $11.50 (1u BE = $18.17) | Squad No-Sale Stop = $3.50 | Campaign Stop = $11.50
# - QA Flashlight: Sim Safe CPA = $11.50 (1u BE = $16.04) | Squad No-Sale Stop = $3.50 | Campaign Stop = $11.50
SNAP_GULF_CAMPAIGN_PLAN = [
    {
        "country": "QA",
        "product": "powerbank",
        "campaign_id": "67de1daa-7ffb-4194-9c44-00dcb7163d4d",
        "campaign_name": "باور بانك قطر 18-10",
        "max_safe_cpa": 11.50,
        "no_sale_stop": 3.50,
        "max_cpc": 0.26,
        "squads_to_run": [
            {"id": "430ea3c0-b721-402f-94b8-6c2c304ec15c", "bid_strategy": "LOWEST_COST_WITH_MAX_BID", "bid_micro": 2_750_000, "daily_micro": 20_000_000, "exp_tag": "Broad Max Bid"},
            {"id": "f7665412-6c51-4031-aea9-7276b02e8dff", "bid_strategy": "TARGET_COST", "bid_micro": 4_200_000, "daily_micro": 20_000_000, "exp_tag": "Men 20+ Target Cost"},
        ],
        "auto_clone_spec": None,
        "backup_squads": [],
    },
    {
        "country": "OM",
        "product": "powerbank",
        "campaign_id": "efccbe22-8951-49f4-b118-cd67f2cec64b",
        "campaign_name": "powe bank oman",
        "max_safe_cpa": 7.87,
        "no_sale_stop": 2.75,
        "max_cpc": 0.25,
        "squads_to_run": [
            {"id": "543cc1eb-b632-4e6c-b266-fbf3d38b0ff1", "bid_strategy": "LOWEST_COST_WITH_MAX_BID", "bid_micro": 2_400_000, "daily_micro": 20_000_000, "exp_tag": "Consolidated Max Bid"},
            {"id": "93546856-95dd-4d51-9e3b-00dae0da3785", "rename_to": "OM_PB Target Cost Broad", "bid_strategy": "TARGET_COST", "bid_micro": 4_400_000, "daily_micro": 20_000_000, "exp_tag": "Broad Target Cost"},
        ],
        "auto_clone_spec": None,
        "backup_squads": [],
    },
    {
        "country": "KW",
        "product": "cushion",
        "campaign_id": "3fb9b26f-7705-4286-b371-4efcd904670c",
        "campaign_name": "وسادة السيارة الكويت 18-10",
        "max_safe_cpa": 11.48,
        "no_sale_stop": 3.00,
        "max_cpc": 0.25,
        "squads_to_run": [
            {"id": "9496d93b-c72d-46e2-b1f7-b07cb2cc2afb", "bid_strategy": "TARGET_COST", "bid_micro": 4_300_000, "daily_micro": 20_000_000, "exp_tag": "Winner #1 Target Cost ($6.59/1p)"},
            {"id": "15fc0db8-57b7-49d8-b66b-79cf0783e6ae", "bid_strategy": "LOWEST_COST_WITH_MAX_BID", "bid_micro": 3_400_000, "daily_micro": 20_000_000, "exp_tag": "Winner #2 Broad H4 ($1.88/1p)"},
        ],
        "auto_clone_spec": None,
        "backup_squads": [],
    },
    {
        "country": "QA",
        "product": "cushion",
        "campaign_id": "f7f6b623-46d5-48af-9a21-fb2584106da8",
        "campaign_name": "qatar car scaling new 2 text",
        "max_safe_cpa": 10.00,
        "no_sale_stop": 3.20,
        "max_cpc": 0.26,
        "squads_to_run": [
            {"id": "335ab103-9212-4ae6-adbc-baee0e29eca2", "bid_strategy": "LOWEST_COST_WITH_MAX_BID", "bid_micro": 2_550_000, "daily_micro": 20_000_000, "exp_tag": "Broad #1 Max Bid"},
            {"id": "a2ff6521-3a1e-4240-b4fc-ca01c0e65074", "bid_strategy": "TARGET_COST", "bid_micro": 4_100_000, "daily_micro": 20_000_000, "exp_tag": "Scale Target Cost #2"},
        ],
        "auto_clone_spec": None,
        "backup_squads": [],
    },
    {
        "country": "OM",
        "product": "flashlight",
        "campaign_id": "2958697d-af14-4afc-a42a-1de78106b8a1",
        "campaign_name": "منتج الكشاف عمان",
        "max_safe_cpa": 6.69,
        "no_sale_stop": 2.80,
        "max_cpc": 0.25,
        "squads_to_run": [
            {"id": "24d07c16-140c-473b-9b81-92c0cf6a4bd4", "bid_strategy": "LOWEST_COST_WITH_MAX_BID", "bid_micro": 2_400_000, "daily_micro": 20_000_000, "exp_tag": "Consolidated Max Bid #1"},
        ],
        "auto_clone_spec": None,
        "backup_squads": [],
    },
    {
        "country": "QA",
        "product": "flashlight",
        "campaign_id": "0e6e5109-d336-44f6-8ce3-601bbe2d0262",
        "campaign_name": "Copy of kasahf smart+ qatar 3.5",
        "max_safe_cpa": 11.50,
        "no_sale_stop": 3.50,
        "max_cpc": 0.26,
        "squads_to_run": [
            {"id": "fa002a7f-b707-4660-b78d-cc7283d93727", "bid_strategy": "LOWEST_COST_WITH_MAX_BID", "bid_micro": 2_850_000, "daily_micro": 20_000_000, "exp_tag": "Consolidated Smart+ Max Bid"},
        ],
        "auto_clone_spec": None,
        "backup_squads": [],
    },
]

# --- TikTok Gulf 6 Clean Smart+ Cost-Cap Campaigns (Top 5 Proven Winners Each | TikTok In-Feed Only [3000] | Standard Pacing) ---
# Approved Distribution: KW = Cushion ONLY | QA = Cushion + Power Bank + Flashlight | OM = Power Bank + Flashlight ONLY (ZERO Cushion in Oman)
TIKTOK_GULF_CAMPAIGN_PLAN = [
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1880685110891601", "name": "kuwait car armrest 01-11 (5 Winners CC 8.5)", "country": "KW", "product": "cushion", "max_safe_cpa": 11.48, "ad_no_sale_stop": 3.20, "max_cpc": 0.26, "base_budget": 35.0, "cpa_bid": 8.50},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1880685124565442", "name": "qatar car armrest 01-11 (5 Winners CC 7.8)", "country": "QA", "product": "cushion", "max_safe_cpa": 10.00, "ad_no_sale_stop": 3.20, "max_cpc": 0.26, "base_budget": 35.0, "cpa_bid": 7.80},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1880685702354274", "name": "qatar power bank 01-11 (5 Winners CC 8.5)", "country": "QA", "product": "powerbank", "max_safe_cpa": 11.50, "ad_no_sale_stop": 3.50, "max_cpc": 0.26, "base_budget": 35.0, "cpa_bid": 8.50},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1880685160856705", "name": "oman power bank 01-11 (5 Winners CC 6.2)", "country": "OM", "product": "powerbank", "max_safe_cpa": 7.87, "ad_no_sale_stop": 2.75, "max_cpc": 0.25, "base_budget": 30.0, "cpa_bid": 6.20},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1880685952492818", "name": "qatar kashaf 01-11 (5 Winners CC 8.5)", "country": "QA", "product": "flashlight", "max_safe_cpa": 11.50, "ad_no_sale_stop": 3.50, "max_cpc": 0.26, "base_budget": 30.0, "cpa_bid": 8.50},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1880685181798465", "name": "oman kashaf 01-11 (5 Winners CC 5.5)", "country": "OM", "product": "flashlight", "max_safe_cpa": 6.69, "ad_no_sale_stop": 2.80, "max_cpc": 0.25, "base_budget": 30.0, "cpa_bid": 5.50},
]

# --- Banned Old/Faulty TikTok Gulf Campaigns (25-video bloat, OpenBid/Accelerated burn, wrong $11.50 Oman Cost Cap) ---
TIKTOK_GULF_BANNED_CAMPAIGNS = [
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879577400149345", "name": "qatar power bank 20-10 (Accel CC 11.5 - Old 25 Ads)", "country": "QA", "product": "powerbank"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879577375941873", "name": "oman power bank 20-10 (Accel CC 11.5 - Old 25 Ads)", "country": "OM", "product": "powerbank"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879660661658674", "name": "kuwait car armrest 21-10 (5 Winners CC 8.8 - Old)", "country": "KW", "product": "cushion"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879577430883393", "name": "qatar kashaf 20-10 (Accel CC 11.5 - Old 25 Ads)", "country": "QA", "product": "flashlight"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879577456037921", "name": "oman kashaf 20-10 (Accel CC 11.5 - Old 25 Ads)", "country": "OM", "product": "flashlight"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1880563740214402", "name": "qatar car armrest 31-10 (5 Winners CC 8.8 - Old)", "country": "QA", "product": "cushion"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879577185699906", "name": "oman power bank 20-10 (Turbo OpenBid)", "country": "OM", "product": "powerbank"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879577220262178", "name": "qatar power bank 20-10 (Turbo OpenBid)", "country": "QA", "product": "powerbank"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879577241078802", "name": "kuwait car armrest 20-10 (Turbo OpenBid)", "country": "KW", "product": "cushion"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879577265265762", "name": "qatar car armrest 20-10 (Turbo OpenBid)", "country": "QA", "product": "cushion"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879577289566305", "name": "qatar kashaf 20-10 (Turbo OpenBid)", "country": "QA", "product": "flashlight"},
    {"account_name": "ROELLINGER RECYCLING", "aadvid": "7631763556322148369", "campaign_id": "1879577320742993", "name": "oman kashaf 20-10 (Turbo OpenBid)", "country": "OM", "product": "flashlight"},
    {"account_name": "ROELLINGER RECYCLING2", "aadvid": "7631765070816509959", "campaign_id": "1874416653959746", "name": "kashaf leads oman (Old)", "country": "OM", "product": "flashlight"},
]


def compute_gulf_break_even_cpa(
    price_local: float,
    currency: str,
    units: int,
    product_type: str,
    conf_rate: float,
    deliv_rate_closed: float,
) -> dict:
    fx = FX_TO_USD[currency]
    p_usd = price_local * fx
    u_cost = UNIT_COSTS_USD[product_type]
    d_net = conf_rate * deliv_rate_closed
    net_per_deliv = (0.935 * p_usd) - (units * u_cost) - 2.50
    fixed_stage_cost = (5.99 * conf_rate) + 0.50
    be_cpa = (d_net * net_per_deliv) - fixed_stage_cost
    safe_target_cpa = round(max(3.50, be_cpa * 0.72), 2)
    return {
        "price_local": price_local,
        "currency": currency,
        "price_usd": round(p_usd, 2),
        "units": units,
        "unit_cost_usd": u_cost,
        "conf_rate": round(conf_rate * 100, 1),
        "deliv_rate_closed": round(deliv_rate_closed * 100, 1),
        "d_net_pct": round(d_net * 100, 1),
        "break_even_cpa_usd": round(be_cpa, 2),
        "safe_target_cpa_usd": safe_target_cpa,
    }


def fetch_cod_partner_gulf_metrics_and_stock() -> tuple[dict, dict]:
    mature_baseline = {
        "QA": {"conf_rate": 0.7895, "deliv_rate_closed": 0.7092, "not_available_hist": 3},
        "OM": {"conf_rate": 0.5966, "deliv_rate_closed": 0.5889, "not_available_hist": 0},
        "KW": {"conf_rate": 0.8205, "deliv_rate_closed": 0.7000, "not_available_hist": 13},
    }
    tok_path = BASE_DIR / "cod_active_token.json"
    if not tok_path.exists():
        return dict(mature_baseline), {}
    tok = json.loads(tok_path.read_text(encoding="utf-8"))
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.3, status_forcelist=[429, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16))
    s.headers.update({
        "Authorization": f"Bearer {tok['access_token']}",
        "x-device-uuid": tok["device_uuid"],
        "X-Client-Secret": "tlpKbcHZwwlYf8OZ1Ocw22q212mH5A5Omlt9sfwn",
        "Accept": "application/json",
        "Origin": "https://app.codpartner.com",
    })

    def _leads(p):
        try:
            r = s.get(
                f"https://api.cod.partners/api/seller/v2/leads?columns[0]=id&columns[1]=customer&columns[2]=status&columns[3]=createdAt&per_page=100&page={p}",
                timeout=8,
            )
            return r.json().get("data") or [] if r.status_code == 200 else []
        except Exception:
            return []

    def _orders(p):
        try:
            r = s.get(
                f"https://api.cod.partners/api/seller/v2/orders?columns[0]=id&columns[1]=customer&columns[2]=shipmentStatus&columns[3]=createdAt&per_page=100&page={p}",
                timeout=8,
            )
            return r.json().get("data") or [] if r.status_code == 200 else []
        except Exception:
            return []

    leads, orders = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(_leads, range(1, 20)):
            leads.extend(res)
        for res in ex.map(_orders, range(1, 15)):
            orders.extend(res)

    # Fetch live per-country warehouse stock
    stock_summary = {}
    unique_pids = {item["pid"] for country_map in GULF_WAREHOUSE_PIDS.values() for items in country_map.values() for item in items}
    pid_inv = {}
    for pid in sorted(unique_pids):
        try:
            r_inv = s.get(f"https://api.cod.partners/api/seller/v2/seller/products/{pid}/inventory", timeout=8).json()
            wh_dict = {}
            for country_entry in (r_inv.get("data") or []):
                for wh in (country_entry.get("warehouses") or []):
                    wh_code = wh.get("code")
                    qty = int(wh.get("expectedRemainingStock") if wh.get("expectedRemainingStock") is not None else (wh.get("inventoryTracker") or 0))
                    wh_dict[wh_code] = qty
            pid_inv[pid] = wh_dict
        except Exception:
            pid_inv[pid] = {}

    for cc, prod_map in GULF_WAREHOUSE_PIDS.items():
        stock_summary[cc] = {}
        for ptype, items in prod_map.items():
            stock_summary[cc][ptype] = [
                {
                    "pid": it["pid"],
                    "color": it["color"],
                    "sku": it["sku"],
                    "warehouse": it["wh"],
                    "qty": pid_inv.get(it["pid"], {}).get(it["wh"], 0),
                }
                for it in items
            ]

    l_map = {x["id"]: x for x in leads if "id" in x}
    o_map = {x["id"]: x for x in orders if "id" in x}

    out = {}
    mature_baseline = {
        "QA": {"conf_rate": 0.7895, "deliv_rate_closed": 0.7092, "not_available_hist": 3},
        "OM": {"conf_rate": 0.5966, "deliv_rate_closed": 0.5889, "not_available_hist": 0},
        "KW": {"conf_rate": 0.8205, "deliv_rate_closed": 0.7000, "not_available_hist": 13},
    }
    for cc in ["QA", "OM", "KW"]:
        cl = [x for x in l_map.values() if ((x.get("customer") or {}).get("shippingCountryCode") or "").upper() == cc]
        co = [x for x in o_map.values() if ((x.get("customer") or {}).get("shippingCountryCode") or "").upper() == cc]
        ls = Counter(str(x.get("status") or "") for x in cl)
        os_ = Counter(str(x.get("shipmentStatus") or "") for x in co)
        tot_l = len(cl)
        dup_l = ls.get("Duplicate", 0) + ls.get("Duplicated", 0)
        val_l = max(1, tot_l - dup_l)
        conf_l = ls.get("Confirmed", 0)
        del_o = os_.get("Delivered", 0)
        ret_o = os_.get("Returned", 0) + os_.get("Return", 0)
        rec_c = (conf_l / val_l) if tot_l >= 15 else mature_baseline[cc]["conf_rate"]
        rec_d = (del_o / (del_o + ret_o)) if (del_o + ret_o) >= 15 else mature_baseline[cc]["deliv_rate_closed"]
        c_rate = min(rec_c, mature_baseline[cc]["conf_rate"])
        d_rate = min(rec_d, mature_baseline[cc]["deliv_rate_closed"])
        out[cc] = {
            "recent_leads": tot_l,
            "recent_valid_leads": tot_l - dup_l,
            "recent_confirmed": conf_l,
            "recent_delivered": del_o,
            "recent_returned": ret_o,
            "not_available_hist": max(os_.get("Not available", 0), mature_baseline[cc]["not_available_hist"]),
            "recent_conf_rate": round(rec_c, 4),
            "recent_deliv_rate": round(rec_d, 4),
            "conf_rate": round(c_rate, 4),
            "deliv_rate_closed": round(d_rate, 4),
        }
    return out, stock_summary


def _load_gulf_ledger() -> dict:
    if LEDGER_JSON.exists():
        try:
            return json.loads(LEDGER_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"squads": {}, "cloned_squads": {}, "big_actions_log": []}


def _save_gulf_ledger(ledger: dict) -> None:
    try:
        LEDGER_JSON.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _snap_api_post(path: str, tok: str, body: dict) -> dict:
    try:
        r = requests.post(
            f"https://adsapi.snapchat.com/v1{path}",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            json=body,
            timeout=25,
        )
        return r.json() if r.status_code in (200, 201) else {}
    except Exception:
        return {}


def _populate_squad_ads_from_source( tok: str, target_sid: str, source_ads: list[dict]) -> int:
    ads_payload = []
    for ad in source_ads[:8]:
        if not ad or not ad.get("creative_id"):
            continue
        if ad.get("review_status") and ad.get("review_status") not in ("APPROVED", "PENDING_REVIEW"):
            continue
        ads_payload.append({
            "ad_squad_id": target_sid,
            "creative_id": ad["creative_id"],
            "name": ad.get("name", "Gulf_Winner"),
            "status": "ACTIVE",
            "type": ad.get("type", "REMOTE_WEBPAGE"),
        })
    if not ads_payload:
        return 0
    res = _snap_api_post(f"/adsquads/{target_sid}/ads", tok, {"ads": ads_payload})
    created_ads = [x.get("ad") for x in ((res or {}).get("ads") or []) if x.get("ad")]
    return len(created_ads)


def _clone_gulf_squad_with_ads(sm, tok: str, cid: str, source_sq: dict, source_ads: list[dict], spec: dict) -> dict | None:
    """Clones a Gulf AdSquad with its new bid/strategy and copies all healthy APPROVED ads into it."""
    new_sq = {
        "name": spec["new_name"],
        "campaign_id": cid,
        "status": "ACTIVE",
        "type": source_sq.get("type", "SNAP_ADS"),
        "placement_v2": source_sq.get("placement_v2") or {"config": "AUTOMATIC"},
        "optimization_goal": source_sq.get("optimization_goal", "PIXEL_PURCHASE"),
        "bid_strategy": spec["bid_strategy"],
        "target_bid": spec["bid_strategy"] == "TARGET_COST",
        "bid_micro": spec["bid_micro"],
        "daily_budget_micro": spec["daily_micro"],
        "billing_event": source_sq.get("billing_event", "IMPRESSION"),
        "targeting": source_sq.get("targeting"),
        "pixel_id": source_sq.get("pixel_id"),
        "start_time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    res = _snap_api_post(f"/campaigns/{cid}/adsquads", tok, {"adsquads": [new_sq]})
    created_list = (res or {}).get("adsquads") or []
    if not created_list or not created_list[0].get("adsquad"):
        return None
    created_sq = created_list[0]["adsquad"]
    new_sid = created_sq["id"]
    _populate_squad_ads_from_source(tok, new_sid, source_ads)
    return created_sq


def run_snap_gulf_campaigns(sim_cpa_lookup: dict | None = None) -> list[dict]:
    import snap_monitor as sm
    cfg = sm.load_config()
    tok = sm.refresh_access_token(cfg)
    acc_id = "350cb560-ad77-4fb5-9bbf-4c090c9ad081"
    st_camp_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(tok, acc_id, "campaign", days_back=0)}
    st_camp_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(tok, acc_id, "campaign", days_back=2)}
    st_sq_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(tok, acc_id, "adsquad", days_back=0)}
    st_sq_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(tok, acc_id, "adsquad", days_back=2)}
    st_ad_1d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(tok, acc_id, "ad", days_back=0)}
    st_ad_3d = {x["id"]: x for x in sm.fetch_account_breakdown_stats(tok, acc_id, "ad", days_back=2)}
    empty_st = {"spend": 0.0, "purchases": 0, "swipes": 0, "cpa": None, "cpc": None}

    riyadh_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    today_str = riyadh_now.strftime("%Y-%m-%d")
    riyadh_hour = riyadh_now.hour
    is_daytime = 6 <= riyadh_hour < 18  # أمر المستخدم الصارم: ممنوع رفع البدجت بعد الساعة 6 مساءً (18:00) أبداً!
    now_epoch = int(time.time())

    ledger = _load_gulf_ledger()
    ledger_squads = ledger.setdefault("squads", {})
    cloned_map = ledger.setdefault("cloned_squads", {})
    actions_log = ledger.setdefault("big_actions_log", [])

    camps = sm.api_get_paginated(f"/adaccounts/{acc_id}/campaigns?limit=200", tok, "campaigns", "campaign")
    camp_map = {c["id"]: c for c in camps}

    results = []
    for item in SNAP_GULF_CAMPAIGN_PLAN:
        cid = item["campaign_id"]
        c_obj = camp_map.get(cid)
        if not c_obj:
            continue

        # Live Simulation Safe CPA Ceiling for this Country + Product
        sim_key = (item["country"], item["product"])
        live_sim_cpa = (sim_cpa_lookup or {}).get(sim_key, item["max_safe_cpa"])
        sim_max_cpa = round(min(float(item["max_safe_cpa"]), float(live_sim_cpa)), 2)
        sq_no_sale_stop = round(min(float(item["no_sale_stop"]), sim_max_cpa * 0.45), 2)
        sq_max_cpc = float(item.get("max_cpc", 0.26))

        c1 = st_camp_1d.get(cid, empty_st)
        c3 = st_camp_3d.get(cid, empty_st)
        c1_sp, c1_pu, c1_cpa = float(c1.get("spend", 0.0)), int(c1.get("purchases", 0)), c1.get("cpa")
        c3_sp, c3_pu, c3_cpa = float(c3.get("spend", 0.0)), int(c3.get("purchases", 0)), c3.get("cpa")

        squads = sm.api_get_paginated(f"/campaigns/{cid}/adsquads?limit=100", tok, "adsquads", "adsquad")
        sq_map = {s["id"]: s for s in squads if s}

        run_list = [dict(x) for x in item["squads_to_run"]]
        allowed_sq_ids = {s_cfg["id"] for s_cfg in run_list}

        # Check if allowed converting squads inside this campaign are strongly profitable (combined CPA <= 75% of SimCap)
        conv_allowed_sp = 0.0
        conv_allowed_pu = 0
        for s_cfg in run_list:
            st_s = st_sq_1d.get(s_cfg["id"], empty_st)
            if int(st_s.get("purchases", 0)) >= 1 and (st_s.get("cpa") or 999) <= sim_max_cpa * 0.80:
                conv_allowed_sp += float(st_s.get("spend", 0.0))
                conv_allowed_pu += int(st_s.get("purchases", 0))
        has_profitable_winner_squads = (
            conv_allowed_pu >= 1
            and (conv_allowed_sp / conv_allowed_pu) <= sim_max_cpa * 0.75
            and (c1_cpa or 999) <= sim_max_cpa * 1.22  # Still strictly below 1-unit Break-Even ($14.00 < $14.19 in KW)
        )

        # 1) STRICT CAMPAIGN-LEVEL SIMULATION STOP-LOSS GATE (1D & 3D):
        # If Campaign total spend or CPA exceeds the Simulation Safe CPA (and no isolated super-profitable winner squad within BE headroom), PAUSE Campaign!
        c3_zero_stop = min(20.00, round(sim_max_cpa * 1.85, 2)) if c1_sp < 0.25 else min(20.00, round(sim_max_cpa + max(2.50, sq_no_sale_stop), 2))
        camp_bled_1d = (c1_pu == 0 and c1_sp >= sim_max_cpa) or (
            c1_pu > 0 and c1_cpa is not None and c1_cpa > sim_max_cpa and not has_profitable_winner_squads
        )
        camp_bled_3d = (
            c1_pu == 0
            and ((c3_pu == 0 and c3_sp >= c3_zero_stop) or (c3_pu > 0 and c3_cpa is not None and c3_cpa > sim_max_cpa * 1.15 and c1_sp >= 1.50))
        )
        if camp_bled_1d or camp_bled_3d:
            if c_obj.get("status") == "ACTIVE":
                c_obj["status"] = "PAUSED"
                sm.api_put(f"/adaccounts/{acc_id}/campaigns", tok, {"campaigns": [c_obj]})
                actions_log.append(
                    f"[{today_str} {riyadh_now.strftime('%H:%M')}] 🚨 CAMPAIGN SIMULATION STOP-LOSS: Paused {item['campaign_name']} ({cid}) | 1D: ${c1_sp:.2f}/{c1_pu}p | 3D: ${c3_sp:.2f}/{c3_pu}p (SimMax=${sim_max_cpa})"
                )
            to_pause_all = []
            for s_obj in sq_map.values():
                if s_obj.get("status") == "ACTIVE":
                    s_obj["status"] = "PAUSED"
                    to_pause_all.append(s_obj)
            if to_pause_all:
                for i in range(0, len(to_pause_all), 15):
                    sm.api_put(f"/campaigns/{cid}/adsquads", tok, {"adsquads": to_pause_all[i : i + 15]})
            results.append({
                "platform": "Snapchat (CSP9860)",
                "country": item["country"],
                "product": item["product"],
                "campaign_id": cid,
                "campaign_name": item["campaign_name"],
                "status": f"🚨 PAUSED (Campaign Simulation Stop-Loss <= ${sim_max_cpa})",
                "experiments": [
                    f"🛑 أُغلقت الحملة بالكامل لحماية رأس المال (1D: ${c1_sp:.2f}/{c1_pu}p | 3D: ${c3_sp:.2f}/{c3_pu}p | Simulation Max=${sim_max_cpa})"
                ],
            })
            continue

        # Pre-Emptive Campaign Simulation Protection:
        # If Campaign total spend is already >= 68% of allowed Simulation Budget ((max(1, c1_pu) * sim_max_cpa)),
        # isolate ONLY the converting squad (or single lowest-spend squad) and pause all zero-sale spending squads immediately!
        sim_total_budget_1d = max(1, c1_pu) * sim_max_cpa
        near_sim_ceiling = (c1_sp >= 0.68 * sim_total_budget_1d) or (
            c1_pu == 0 and c3_pu == 0 and c3_sp >= 0.68 * sim_max_cpa
        )

        to_update = []
        active_tags = []

        idx = 0
        while idx < len(run_list):
            s_cfg = run_list[idx]
            idx += 1
            sid = s_cfg["id"]
            s_obj = sq_map.get(sid)
            if not s_obj:
                continue
            s1 = st_sq_1d.get(sid, empty_st)
            s3 = st_sq_3d.get(sid, empty_st)
            s1_sp, s1_pu, s1_sw, s1_cpa, s1_cpc = (
                float(s1.get("spend", 0.0)),
                int(s1.get("purchases", 0)),
                int(s1.get("swipes", 0)),
                s1.get("cpa"),
                s1.get("cpc"),
            )
            s3_sp, s3_pu, s3_sw, s3_cpa, s3_cpc = (
                float(s3.get("spend", 0.0)),
                int(s3.get("purchases", 0)),
                int(s3.get("swipes", 0)),
                s3.get("cpa"),
                s3.get("cpc"),
            )

            # Calculate remaining Campaign Simulation Headroom after subtracting other squads' spend today
            other_sq_spend_1d = max(0.0, c1_sp - s1_sp)
            remaining_camp_headroom = max(0.80, round(sim_total_budget_1d - other_sq_spend_1d - 0.30, 2))
            eff_sq_no_sale_stop = round(min(sq_no_sale_stop, remaining_camp_headroom), 2)

            # 2) STRICT ADSQUAD-LEVEL SIMULATION, HEADROOM & EARLY CPC STOP-LOSS (1D & 3D):
            s3_zero_stop = round(eff_sq_no_sale_stop * 1.85, 2) if s1_sp < 0.20 else round(eff_sq_no_sale_stop * 1.35, 2)
            high_cpc_1d = s1_pu == 0 and s1_sp >= 0.75 and (s1_sw == 0 or (s1_cpc is not None and s1_cpc > sq_max_cpc))
            high_cpc_3d = s1_pu == 0 and s1_sp >= 0.25 and s3_pu == 0 and s3_sp >= 1.20 and (s3_sw == 0 or (s3_cpc is not None and s3_cpc > sq_max_cpc + 0.02))
            preemptive_zero_sale_cut = near_sim_ceiling and s1_pu == 0 and (s1_sp >= 0.50 or c1_pu >= 1)
            bled_1d = (
                (s1_pu == 0 and s1_sp >= eff_sq_no_sale_stop)
                or (s1_pu > 0 and s1_cpa is not None and s1_cpa > sim_max_cpa)
                or high_cpc_1d
                or preemptive_zero_sale_cut
            )
            bled_3d = (
                s1_pu == 0
                and (
                    (s3_pu == 0 and s3_sp >= s3_zero_stop)
                    or (s3_pu > 0 and s3_cpa is not None and s3_cpa > sim_max_cpa * 1.15 and s1_sp >= 0.50)
                    or high_cpc_3d
                )
            )
            if bled_1d or bled_3d:
                if s_obj.get("status") == "ACTIVE":
                    s_obj["status"] = "PAUSED"
                    to_update.append(s_obj)
                    actions_log.append(
                        f"[{today_str} {riyadh_now.strftime('%H:%M')}] Paused Squad {sid[:8]} in {item['campaign_name']} (1D: ${s1_sp:.2f}/{s1_pu}p CPC={s1_cpc} | 3D: ${s3_sp:.2f}/{s3_pu}p | SimCap=${sim_max_cpa})"
                    )
                stop_reason_str = (
                    f"High CPC ${s1_cpc or s3_cpc:.2f} > ${sq_max_cpc}"
                    if (high_cpc_1d or high_cpc_3d)
                    else (
                        f"Pre-Emptive Sim Protection (Camp ${c1_sp:.2f}/${sim_total_budget_1d:.2f})"
                        if preemptive_zero_sale_cut
                        else f"Stop-Loss <= ${eff_sq_no_sale_stop}"
                    )
                )
                active_tags.append(
                    f"🚨 PAUSED ({stop_reason_str}: 1D ${s1_sp:.2f}/{s1_pu}p | 3D ${s3_sp:.2f}/{s3_pu}p)"
                )
                continue

            # أمر المستخدم الصارم (2026-11-02): تجميد كامل للبيد (لا يطلع ولا ينزل تلقائياً) وإيقاف المتجاوزين فقط بدون إعادة تشغيل الموقوف!
            target_strategy = s_obj.get("bid_strategy") or s_cfg["bid_strategy"]
            target_bid_micro = int(s_obj.get("bid_micro") or s_cfg["bid_micro"])
            bumps_today = 0

            ledger_squads[sid] = {
                "date": today_str,
                "bid_strategy": target_strategy,
                "bid_micro": target_bid_micro,
                "bumps_today": bumps_today,
                "last_spend_1d": s1_sp,
                "last_check_epoch": now_epoch,
            }

            if s_obj.get("status") != "ACTIVE":
                active_tags.append(f"⏸️ PAUSED ({s_cfg['exp_tag']} | 1D: ${s1_sp:.2f}/{s1_pu}p)")
                continue

            is_stop_loss_only = (os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("STOP_LOSS_PAUSE_ONLY") == "1")
            cur_daily_micro = int(s_obj.get("daily_budget_micro") or s_cfg["daily_micro"])
            if is_stop_loss_only:
                target_daily_micro = cur_daily_micro
            elif riyadh_hour >= 22 or riyadh_hour < 6:
                target_daily_micro = s_cfg["daily_micro"]
            elif is_daytime and s1_pu >= 1 and (s1_cpa or 99) <= sim_max_cpa * 0.70:
                target_daily_micro = 30_000_000
            else:
                target_daily_micro = min(cur_daily_micro, 30_000_000) if s1_pu >= 1 else s_cfg["daily_micro"]
                if riyadh_hour >= 18 and target_daily_micro > cur_daily_micro:
                    target_daily_micro = cur_daily_micro

            changed = False
            if not is_stop_loss_only and int(s_obj.get("daily_budget_micro") or 0) != target_daily_micro:
                s_obj["daily_budget_micro"] = target_daily_micro
                changed = True
            if changed:
                to_update.append(s_obj)

            # تدقيق العين البشرية على مستوى كل إعلان داخل الأدست الخليجية (Micro-Ad Level Pruning in 1D & 3D):
            ads = sm.api_get_paginated(f"/adsquads/{sid}/ads?limit=100", tok, "ads", "ad")
            ads_mutations = []
            healthy_candidates = []
            pruned_ad_cnt = 0
            for a in ads:
                if not a:
                    continue
                if a.get("review_status") in ("REJECTED",):
                    continue
                aid_g = a["id"]
                a1 = st_ad_1d.get(aid_g, empty_st)
                a3 = st_ad_3d.get(aid_g, empty_st)
                a1_sp, a1_pu, a1_sw = float(a1.get("spend", 0.0)), int(a1.get("purchases", 0)), int(a1.get("swipes", 0))
                a3_sp, a3_pu, a3_sw = float(a3.get("spend", 0.0)), int(a3.get("purchases", 0)), int(a3.get("swipes", 0))
                a1_cpc = (a1_sp / a1_sw) if a1_sw > 0 else None
                a3_cpc = (a3_sp / a3_sw) if a3_sw > 0 else None
                a1_cpa = a1.get("cpa")
                a3_cpa = a3.get("cpa")
                is_bad_gulf_ad = (
                    (a1_pu == 0 and a1_sp >= 0.80 and (a1_sw == 0 or (a1_cpc is not None and a1_cpc > sq_max_cpc)))
                    or (a1_pu == 0 and a1_sp >= 0.20 and a3_pu == 0 and a3_sp >= 1.20 and (a3_sw == 0 or (a3_cpc is not None and a3_cpc > sq_max_cpc + 0.02)))
                    or (a1_pu == 0 and a3_pu == 0 and (a1_sp >= eff_sq_no_sale_stop or a3_sp >= round(eff_sq_no_sale_stop * 1.85, 2)))
                    or (s1_pu >= 1 and a1_pu == 0 and (a1_sp >= 1.50 or a1_sw >= 15))
                    or (a1_pu > 0 and a1_cpa is not None and a1_cpa > sim_max_cpa)
                    or (a1_pu == 0 and a1_sp >= 0.40 and a3_pu > 0 and a3_cpa is not None and a3_cpa > sim_max_cpa * 1.15)
                )
                if is_bad_gulf_ad:
                    if a.get("status") == "ACTIVE":
                        a["status"] = "PAUSED"
                        ads_mutations.append(a)
                        pruned_ad_cnt += 1
                else:
                    healthy_candidates.append(a)

            act_ads = [a for a in healthy_candidates if a.get("status") == "ACTIVE"]

            if ads_mutations:
                sm.api_put(f"/adsquads/{sid}/ads", tok, {"ads": ads_mutations})

            if len(act_ads) == 0:
                s_obj["status"] = "PAUSED"
                if s_obj not in to_update:
                    to_update.append(s_obj)
                active_tags.append(f"🧟 PAUSED (0 إعلانات سليمة متبقية بعد تقليم {pruned_ad_cnt} فيديو خاسر)")
            else:
                strat_lbl = "TC" if target_strategy == "TARGET_COST" else "MaxBid"
                bump_lbl = f" [+{bumps_today} bumps]" if bumps_today > 0 else ""
                active_tags.append(
                    f"✅ {s_cfg['exp_tag']} @ ${target_bid_micro/1e6:.2f} {strat_lbl}{bump_lbl} ({len(act_ads)} ads active, {pruned_ad_cnt} pruned | 1D: ${s1_sp:.2f}/{s1_pu}p | SimCap=${sim_max_cpa})"
                )

        # Pause any extra squads not in allowed_sq_ids (100% Bid Freeze)
        is_night_snap = (riyadh_hour >= 22 or riyadh_hour < 6) and not (os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("STOP_LOSS_PAUSE_ONLY") == "1")
        for s_id, s_obj in sq_map.items():
            if s_id not in allowed_sq_ids:
                sq_mut = False
                if s_obj.get("status") == "ACTIVE":
                    s_obj["status"] = "PAUSED"
                    sq_mut = True
                if is_night_snap and int(s_obj.get("daily_budget_micro") or 0) > 0 and int(s_obj.get("daily_budget_micro") or 0) != 20_000_000:
                    s_obj["daily_budget_micro"] = 20_000_000
                    sq_mut = True
                if sq_mut and s_obj not in to_update:
                    to_update.append(s_obj)

        if to_update:
            for i in range(0, len(to_update), 15):
                sm.api_put(f"/campaigns/{cid}/adsquads", tok, {"adsquads": to_update[i : i + 15]})

        # If all squads inside the campaign were paused, pause the campaign too (NEVER auto-re-activate a paused campaign)
        still_active_sq = [s for s in sq_map.values() if s.get("status") == "ACTIVE"]
        camp_final_status = f"{c_obj.get('status', 'ACTIVE')} (1D: ${c1_sp:.2f}/{c1_pu}p | SimCap=${sim_max_cpa})"
        if not still_active_sq:
            if c_obj.get("status") == "ACTIVE":
                c_obj["status"] = "PAUSED"
                sm.api_put(f"/adaccounts/{acc_id}/campaigns", tok, {"campaigns": [c_obj]})
                actions_log.append(f"[{today_str} {riyadh_now.strftime('%H:%M')}] Paused Gulf Campaign {item['campaign_name']} ({cid}) — All Squads Hit Stop-Loss")
            camp_final_status = f"🚨 PAUSED (All Squads Hit Stop-Loss <= ${sim_max_cpa} | 1D: ${c1_sp:.2f}/{c1_pu}p)"

        results.append({
            "platform": "Snapchat (CSP9860)",
            "country": item["country"],
            "product": item["product"],
            "campaign_id": cid,
            "campaign_name": item["campaign_name"],
            "status": camp_final_status,
            "experiments": active_tags,
        })

    # Enforce PAUSED on any rogue active Gulf Snapchat campaign NOT in SNAP_GULF_CAMPAIGN_PLAN (ZERO touch on Saudi campaigns)
    planned_snap_gulf_cids = {x["campaign_id"] for x in SNAP_GULF_CAMPAIGN_PLAN}
    for c_extra in camps:
        if not c_extra or c_extra.get("status") != "ACTIVE":
            continue
        ec_id = c_extra.get("id", "")
        ec_nm = c_extra.get("name", "")
        if ec_id not in planned_snap_gulf_cids and sm.is_gulf_034_campaign(ec_nm):
            c_extra["status"] = "PAUSED"
            sm.api_put(f"/adaccounts/{acc_id}/campaigns", tok, {"campaigns": [c_extra]})
            actions_log.append(f"[{today_str} {riyadh_now.strftime('%H:%M')}] Paused Unplanned/Old Gulf Snap Campaign {ec_nm} ({ec_id})")

    # Keep only last 60 action logs
    ledger["big_actions_log"] = actions_log[-60:]
    _save_gulf_ledger(ledger)
    return results


def run_tiktok_gulf_campaigns(sim_cpa_lookup: dict | None = None) -> list[dict]:
    import tiktok_monitor as tm
    cookies = tm.load_cookies()
    s, th = tm.build_session(cookies)
    today_d = datetime.datetime.now().strftime("%Y-%m-%d")
    two_d = (datetime.datetime.now() - datetime.timedelta(days=2)).strftime("%Y-%m-%d")
    riyadh_hour = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3))).hour
    is_daytime = 6 <= riyadh_hour <= 22

    # Fetch 1D & 3D campaign stats per account
    acc_camps_1d = {}
    acc_camps_3d = {}
    acc_ads_1d = {}
    acc_ads_3d = {}
    for aname, aid, _ in tm.ACCOUNTS:
        if aid in ("7631763556322148369", "7631765070816509959"):
            rows_1d = tm.fetch_campaigns(s, th, aid, today_d, today_d) or []
            rows_3d = tm.fetch_campaigns(s, th, aid, two_d, today_d) or []
            acc_camps_1d[aid] = {str(r.get("campaign_id")): r for r in rows_1d}
            acc_camps_3d[aid] = {str(r.get("campaign_id")): r for r in rows_3d}
            if aid == "7631763556322148369":
                ads_1d = tm.fetch_ads(s, th, aid, today_d, today_d) or []
                ads_3d = tm.fetch_ads(s, th, aid, two_d, today_d) or []
                acc_ads_1d[aid] = ads_1d
                acc_ads_3d[aid] = {str(r.get("ad_id")): r for r in ads_3d}

    results = []
    # 1) Permanently enforce PAUSED status on banned Cushion & Flashlight TikTok campaigns
    for b_item in TIKTOK_GULF_BANNED_CAMPAIGNS:
        aid = b_item["aadvid"]
        cid = b_item["campaign_id"]
        row1 = acc_camps_1d.get(aid, {}).get(cid, {})
        rd1 = row1.get("row_data") or row1
        spend_1d = float(rd1.get("stat_cost") or 0.0)
        conv_1d = int(float(rd1.get("time_attr_convert_cnt") or 0))
        opt_status = str(row1.get("opt_status", "1"))
        if opt_status == "0":
            tm.pause_campaign(s, th, aid, cid, operation="disable")
        results.append({
            "platform": f"TikTok ({b_item['account_name']})",
            "country": b_item["country"],
            "product": b_item["product"],
            "campaign_id": cid,
            "campaign_name": b_item["name"],
            "status": f"PAUSED (محظورة نهائياً في تيك توك | 1D: ${spend_1d:.2f}/{conv_1d}p)",
        })

    # 1B) Auto-pause any unplanned rogue Gulf TikTok campaign across all 6 TikTok accounts (ZERO touch on Saudi campaigns)
    planned_tt_cids = {x["campaign_id"] for x in TIKTOK_GULF_CAMPAIGN_PLAN}
    banned_tt_cids = {x["campaign_id"] for x in TIKTOK_GULF_BANNED_CAMPAIGNS}
    for aname_chk, aid_chk, _ in tm.ACCOUNTS:
        rows_chk = acc_camps_1d.get(aid_chk)
        if rows_chk is None:
            r_list = tm.fetch_campaigns(s, th, aid_chk, today_d, today_d) or []
            rows_chk = {str(r.get("campaign_id")): r for r in r_list}
            acc_camps_1d[aid_chk] = rows_chk
        for cid_chk, r_chk in rows_chk.items():
            if cid_chk in planned_tt_cids or cid_chk in banned_tt_cids:
                continue
            rd_chk = r_chk.get("row_data") or r_chk
            cnm_chk = str(r_chk.get("campaign_name") or rd_chk.get("campaign_name") or "")
            if tm.is_gulf_campaign(cnm_chk) and str(r_chk.get("opt_status", "1")) == "0":
                tm.pause_campaign(s, th, aid_chk, cid_chk, operation="disable")

    # 2) Actively manage, prune & optimize all 6 Gulf TikTok campaigns (KW Cushion, QA Cushion/PB/Flashlight, OM PB/Flashlight)
    ledger = _load_gulf_ledger()
    tt_scaled_map = ledger.setdefault("tiktok_gulf_scaled", {})
    riyadh_now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=3)))
    today_str = riyadh_now.strftime("%Y-%m-%d")
    allow_budget_scale = 6 <= riyadh_hour < 18

    for item in TIKTOK_GULF_CAMPAIGN_PLAN:
        aid = item["aadvid"]
        cid = item["campaign_id"]
        sim_key = (item["country"], item["product"])
        live_sim_cpa = (sim_cpa_lookup or {}).get(sim_key, item["max_safe_cpa"])
        sim_max_cpa = round(min(float(item["max_safe_cpa"]), float(live_sim_cpa)), 2)
        ad_no_sale_stop = float(item.get("ad_no_sale_stop", round(min(3.50, sim_max_cpa * 0.45), 2)))
        max_cpc = float(item.get("max_cpc", 0.25))
        base_budget = float(item.get("base_budget", 30.0))

        row1 = acc_camps_1d.get(aid, {}).get(cid, {})
        row3 = acc_camps_3d.get(aid, {}).get(cid, {})
        rd1 = row1.get("row_data") or row1
        rd3 = row3.get("row_data") or row3
        spend_1d = float(rd1.get("stat_cost") or 0.0)
        conv_1d = int(float(rd1.get("time_attr_convert_cnt") or 0))
        spend_3d = float(rd3.get("stat_cost") or 0.0)
        conv_3d = int(float(rd3.get("time_attr_convert_cnt") or 0))
        opt_status = str(row1.get("opt_status", "0"))
        cur_budget = float(row1.get("budget") or rd1.get("budget") or base_budget)

        # Campaign-level Simulation Stop-Loss (identical to Snapchat Gulf Simulation Gate)
        tt_c3_zero_stop = min(20.00, round(sim_max_cpa * 1.85, 2)) if spend_1d < 0.25 else min(20.00, round(sim_max_cpa + max(2.50, ad_no_sale_stop), 2))
        bled_1d = (conv_1d == 0 and spend_1d >= sim_max_cpa) or (conv_1d > 0 and (spend_1d / conv_1d) > sim_max_cpa)
        bled_3d = (conv_1d == 0 and conv_3d == 0 and spend_3d >= tt_c3_zero_stop) or (
            conv_1d == 0 and spend_1d >= 1.00 and conv_3d > 0 and (spend_3d / conv_3d) > sim_max_cpa * 1.15
        )
        # Pre-emptive Simulation Protection @ 68% of allowed Simulation ceiling when campaign has >=1 sale
        preemptive_68_prune = (conv_1d >= 1) and (spend_1d >= 0.68 * (conv_1d * sim_max_cpa))

        # Micro-Video Pruning inside each Gulf TikTok Smart+ campaign (via fetch_expanded_materials -> 5 individual videos)
        camp_ads = [a for a in acc_ads_1d.get(aid, []) if str(a.get("campaign_id") or (a.get("row_data") or {}).get("campaign_id")) == cid]
        creative_ids = [str(a.get("creative_id") or (a.get("row_data") or {}).get("creative_id") or "") for a in camp_ads if (a.get("creative_id") or (a.get("row_data") or {}).get("creative_id"))]
        pruned_tt_ads = 0
        active_tt_ads = 0
        if creative_ids:
            mats_1d = tm.fetch_expanded_materials(s, th, aid, creative_ids, today_d, today_d) or []
            mats_3d_list = tm.fetch_expanded_materials(s, th, aid, creative_ids, two_d, today_d) or []
            mats_1d_map = {str(m.get("main_entity_id") or (m.get("row_data") or {}).get("main_entity_id") or ""): (m.get("row_data") or m) for m in mats_1d}
            mats_3d_map = {str(m.get("main_entity_id") or (m.get("row_data") or {}).get("main_entity_id") or ""): (m.get("row_data") or m) for m in mats_3d_list}
            for m_row in (mats_3d_list or mats_1d):
                mrd_base = m_row.get("row_data") or m_row
                meid = str(m_row.get("main_entity_id") or mrd_base.get("main_entity_id") or "")
                ad_id = str(m_row.get("ad_id") or mrd_base.get("ad_id") or (camp_ads[0].get("ad_id") if camp_ads else ""))
                if not meid or not ad_id:
                    continue
                mrd1 = mats_1d_map.get(meid, {})
                mrd3 = mats_3d_map.get(meid, mrd_base)
                a_sp1 = float(mrd1.get("stat_cost") or 0.0)
                a_cv1 = int(float(mrd1.get("time_attr_convert_cnt") or 0))
                a_clk1 = int(float(mrd1.get("click_cnt") or 0))
                a_cpc1 = (a_sp1 / a_clk1) if a_clk1 > 0 else None
                a_sp3 = float(mrd3.get("stat_cost") or a_sp1)
                a_cv3 = int(float(mrd3.get("time_attr_convert_cnt") or a_cv1))
                a_clk3 = int(float(mrd3.get("click_cnt") or a_clk1))
                a_cpc3 = (a_sp3 / a_clk3) if a_clk3 > 0 else None
                aco_opt = str(mrd3.get("mix_material_aco_opt_status") or mrd1.get("mix_material_aco_opt_status") or m_row.get("mix_material_aco_opt_status") or "0")
                prim_st = str(mrd3.get("material_primary_status") or mrd1.get("material_primary_status") or "")
                bad_tt_vid = (
                    (a_cv1 == 0 and a_sp1 >= 1.00 and (a_clk1 == 0 or (a_cpc1 is not None and a_cpc1 > max_cpc)))
                    or (a_cv1 == 0 and a_cv3 == 0 and a_sp3 >= 1.00 and (a_clk3 == 0 or (a_cpc3 is not None and a_cpc3 > max_cpc)))
                    or (a_cv1 == 0 and a_cv3 == 0 and (a_sp1 >= ad_no_sale_stop or a_sp3 >= ad_no_sale_stop))
                    or (a_cv1 > 0 and (a_sp1 / a_cv1) > sim_max_cpa)
                    or (a_cv1 == 0 and a_cv3 > 0 and (a_sp3 / a_cv3) > sim_max_cpa)
                    or (preemptive_68_prune and a_cv1 == 0 and a_sp1 >= 0.60)
                )
                if bad_tt_vid:
                    if aco_opt == "0" and prim_st not in ("disable", "delete"):
                        tm.pause_smart_plus_video(s, th, aid, ad_id, mrd3, operation="disable")
                        pruned_tt_ads += 1
                else:
                    if aco_opt == "0" and prim_st not in ("disable", "delete"):
                        active_tt_ads += 1

        all_ads_pruned = len(creative_ids) > 0 and active_tt_ads == 0
        budget_note = ""
        is_stop_loss_only_tt = (os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("STOP_LOSS_PAUSE_ONLY") == "1")

        if bled_1d or bled_3d or all_ads_pruned:
            if opt_status == "0":
                tm.pause_campaign(s, th, aid, cid, operation="disable")
            reason_tag = "All 5 Ads Hit Stop-Loss" if all_ads_pruned and not (bled_1d or bled_3d) else f"Simulation Stop-Loss <= ${sim_max_cpa}"
            status_lbl = f"🚨 PAUSED ({reason_tag}: 1D ${spend_1d:.2f}/{conv_1d}p | 3D ${spend_3d:.2f}/{conv_3d}p)"
        elif opt_status != "0":
            status_lbl = f"⏸️ PAUSED (1D: ${spend_1d:.2f}/{conv_1d}p | 3D: ${spend_3d:.2f}/{conv_3d}p)"
        else:
            # Daytime profitable scaling ( strictly before 18:00 KSA — disabled when STOP_LOSS_PAUSE_ONLY / GitHub Actions )
            if not is_stop_loss_only_tt and allow_budget_scale and conv_1d >= 1 and (spend_1d / conv_1d) <= (sim_max_cpa * 0.80) and spend_1d >= (cur_budget * 0.45):
                last_sc = tt_scaled_map.get(cid, {})
                last_ts = float(last_sc.get("ts") or 0.0)
                last_pu = int(last_sc.get("pu") or 0)
                if (time.time() - last_ts) >= 10800 and conv_1d > last_pu:
                    new_b = min(100.0, round(cur_budget * 1.25, 2))
                    if new_b > cur_budget and hasattr(tm, "update_campaign_budget") and tm.update_campaign_budget(s, th, aid, cid, new_b):
                        tt_scaled_map[cid] = {"ts": time.time(), "pu": conv_1d, "old": cur_budget, "new": new_b}
                        cur_budget = new_b
                        budget_note = f" | 🚀 Scaled->${new_b:.0f}"

            status_lbl = (
                f"✅ ACTIVE (1D: ${spend_1d:.2f}/{conv_1d}p | 3D: ${spend_3d:.2f}/{conv_3d}p | "
                f"{active_tt_ads} videos, {pruned_tt_ads} pruned | CC=${item.get('cpa_bid')} | SimCap=${sim_max_cpa}{budget_note})"
            )

        results.append({
            "platform": f"TikTok ({item['account_name']})",
            "country": item["country"],
            "product": item["product"],
            "campaign_id": cid,
            "campaign_name": item["name"],
            "status": status_lbl,
        })
    _save_gulf_ledger(ledger)
    return results


def main():
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== GULF HOURLY PROFIT, PRICING, STOCK & ADS MASTER ({now_str}) ===")
    cod_metrics, stock_summary = fetch_cod_partner_gulf_metrics_and_stock()

    funnel_economics = []
    sim_cpa_lookup = {}
    for f in GULF_FUNNELS:
        cc = f["country"]
        m = cod_metrics.get(cc, {"conf_rate": 0.65, "deliv_rate_closed": 0.65})
        p1 = f["prices_local"][0]
        p2 = f["prices_local"][1]
        p3 = f["prices_local"][2] if len(f["prices_local"]) >= 3 else p2
        ups = f.get("upsell_prices_local") or [p2, p3]
        u2 = ups[0]
        u3 = ups[1] if len(ups) >= 2 else p3
        be1 = compute_gulf_break_even_cpa(p1, f["currency"], 1, f["product_type"], m["conf_rate"], m["deliv_rate_closed"])
        be2 = compute_gulf_break_even_cpa(p2, f["currency"], 2, f["product_type"], m["conf_rate"], m["deliv_rate_closed"])
        be2_up = compute_gulf_break_even_cpa(u2, f["currency"], 2, f["product_type"], m["conf_rate"], m["deliv_rate_closed"])
        be3 = compute_gulf_break_even_cpa(p3, f["currency"], 3, f["product_type"], m["conf_rate"], m["deliv_rate_closed"])
        # Blended Break-Even with 5-Offer AOV Ladder (65% 1u + 20% 2u Special Upsell + 10% 2u Bundle + 5% 3u Bundle)
        blended_be = round(
            0.65 * be1["break_even_cpa_usd"]
            + 0.20 * be2_up["break_even_cpa_usd"]
            + 0.10 * be2["break_even_cpa_usd"]
            + 0.05 * be3["break_even_cpa_usd"],
            2,
        )
        safe_cpa = round(max(3.50, blended_be * 0.75), 2)
        item_res = {
            "country": cc,
            "product_type": f["product_type"],
            "product_id": f.get("product_id", ""),
            "funnel_id": f["funnel_id"],
            "funnel_name": f["name"],
            "slug": f["slug"],
            "currency": f["currency"],
            "prices_local": f["prices_local"],
            "upsell_prices_local": ups,
            "be_1unit_usd": be1["break_even_cpa_usd"],
            "be_2unit_upsell_usd": be2_up["break_even_cpa_usd"],
            "be_2unit_usd": be2["break_even_cpa_usd"],
            "be_3unit_usd": be3["break_even_cpa_usd"],
            "blended_be_cpa_usd": blended_be,
            "max_safe_cpa_usd": safe_cpa,
            "d_net_pct": be1["d_net_pct"],
        }
        funnel_economics.append(item_res)
        # Skip blocked google funnel in Qatar (mXYlz1bqb) when building active simulation lookup
        if f["slug"] != "mXYlz1bqb":
            sim_key = (cc, f["product_type"])
            if sim_key not in sim_cpa_lookup or safe_cpa < sim_cpa_lookup[sim_key]:
                sim_cpa_lookup[sim_key] = safe_cpa
        print(
            f"  [{cc} - {f['product_type']}] {f['name']} ({f['prices_local']} + Upsell {ups} {f['currency']}) -> "
            f"1u BE=${be1['break_even_cpa_usd']} | 2u-Upsell BE=${be2_up['break_even_cpa_usd']} | 2u BE=${be2['break_even_cpa_usd']} | "
            f"Blended BE=${blended_be} | Max Safe CPA=${safe_cpa}"
        )

    snap_results = run_snap_gulf_campaigns(sim_cpa_lookup)
    for sr in snap_results:
        print(f"  [SNAP ACTIVE] {sr['country']} | {sr['campaign_name']} ({sr['campaign_id']}) -> {' ; '.join(sr['experiments'])}")

    tt_results = run_tiktok_gulf_campaigns(sim_cpa_lookup)
    for tr in tt_results:
        print(f"  [TIKTOK] {tr['country']} | {tr['campaign_name']} ({tr['campaign_id']}) -> {tr['status']}")

    payload = {
        "timestamp": now_str,
        "cod_partner_gulf_metrics": cod_metrics,
        "cod_partner_warehouse_stock": stock_summary,
        "funnel_economics": funnel_economics,
        "snapchat_gulf_campaigns": snap_results,
        "tiktok_gulf_campaigns": tt_results,
    }
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = [
        f"# تقرير أوتوميشن الخليج الموحد كل ساعة (قطر وعُمان والكويت) — {now_str}",
        "",
        "## 1. اقتصاديات الفانلز وسقف البيعة الآمن",
        "| الدولة | المنتج | الأسعار | التعادل المدمج | سقف CPA الآمن |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ]
    for r in funnel_economics:
        md_lines.append(
            f"| **{r['country']}** | `{r['product_type']}` | `{r['prices_local']} {r['currency']}` | `${r['blended_be_cpa_usd']}` | **`${r['max_safe_cpa_usd']}`** |"
        )

    md_lines.extend([
        "",
        "## 2. حملات سناب شات وتجارب البيدات والهيكلة في الخليج",
        "| الدولة | المنتج | الحملة | الحالة | المجموعات النشطة والبيدات |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    for sr in snap_results:
        md_lines.append(
            f"| **{sr['country']}** | `{sr['product']}` | `{sr['campaign_name']}` | `{sr['status']}` | {' <br> '.join(sr['experiments'])} |"
        )

    md_lines.extend([
        "",
        "## 3. حملات تيك توك في الخليج",
        "| الدولة | المنتج | الحملة | الحساب | الحالة |",
        "| :--- | :--- | :--- | :--- | :--- |",
    ])
    for tr in tt_results:
        md_lines.append(
            f"| **{tr['country']}** | `{tr['product']}` | `{tr['campaign_name']}` | `{tr['platform']}` | `{tr['status']}` |"
        )

    REPORT_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"Saved report to {REPORT_MD}")


if __name__ == "__main__":
    main()
