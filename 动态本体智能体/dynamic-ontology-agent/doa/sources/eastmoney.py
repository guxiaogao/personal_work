"""东财财报接口。零额外依赖，只用 urllib。

接口一次调用同时返回 REPORT_DATE（报告期）与 NOTICE_DATE（披露日），
双时间戳所需字段齐备。

已知限制（见项目计划 §6.3）
--------------------------
* 只返回每个报告期的**最新值**，不给逐字段修正历史
* 正式报表的 NOTICE_DATE 记的是"最近一次涉及该报告期的披露"而非首次披露
* 修正历史需靠三级披露链（业绩预告 → 业绩快报 → 正式报表）重建
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

BASE = "https://datacenter.eastmoney.com/securities/api/data/v1/get"

# 报表名 → (reportName, source)
REPORTS = {
    "balance": ("RPT_DMSK_FN_BALANCE", "HSF10"),
    "income": ("RPT_DMSK_FN_INCOME", "HSF10"),
    "cashflow": ("RPT_DMSK_FN_CASHFLOW", "HSF10"),
    "express": ("RPT_FCI_PERFORMANCEE", "DataCenter"),   # 业绩快报
    "forecast": ("RPT_PUBLIC_OP_NEWPREDICT", "DataCenter"),  # 业绩预告
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://emweb.securities.eastmoney.com/",
}


def fetch(
    report: str,
    security_code: str,
    *,
    columns: str = "ALL",
    page_size: int = 20,
    report_date: str | None = None,
    timeout: float = 25.0,
) -> list[dict[str, Any]]:
    """取某只票的某张报表。report 见 REPORTS 的键。"""
    if report not in REPORTS:
        raise ValueError(f"未知报表 {report!r}，可用: {sorted(REPORTS)}")
    report_name, source = REPORTS[report]

    filters = f'(SECURITY_CODE="{security_code}")'
    if report_date:
        filters += f"(REPORT_DATE='{report_date}')"

    params = {
        "reportName": report_name,
        "columns": columns,
        "filter": filters,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
        "source": source,
        "client": "PC",
    }
    url = f"{BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=_HEADERS)

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    if not payload.get("success"):
        # code 9201 = 返回数据为空，属正常情况（该票没有这张报表）
        if payload.get("code") == 9201:
            return []
        raise RuntimeError(f"接口返回失败: {payload.get('message')}")

    return (payload.get("result") or {}).get("data") or []


def nonnull_fields(row: dict[str, Any]) -> set[str]:
    """非空字段集。不同行业返回的字段集差异是 schema 漂移的横截面来源。"""
    return {k for k, v in row.items() if v not in (None, "")}
