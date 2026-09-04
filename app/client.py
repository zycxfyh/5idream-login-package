"""5idream（到梦空间）异步客户端。

保持与原 CLI 工具相同的接口语义，只做三处适配：
1. 同步 requests -> 异步 httpx（天然支持高并发复用）；
2. time.sleep -> asyncio.sleep（不阻塞事件循环）；
3. 登录态通过每个请求显式携带 dmkj_web_token cookie，避免跨用户串号。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from .config import settings

BASE_URL = settings.base_url
DEFAULT_ACTIVITY_IMAGE = "https://5idream.oss-cn-beijing.aliyuncs.com/horde-default-avatar.png"

QR_ENDPOINT = f"{BASE_URL}/scan_login/getSecurityId"
POLL_ENDPOINT = f"{BASE_URL}/scan_login/rollPoling"
CHECK_TOKEN_ENDPOINT = f"{BASE_URL}/token/checkToken"

PROFILE_KEY = "\u4e2a\u4eba\u4fe1\u606f"      # 个人信息
ACTIVITIES_KEY = "\u6d3b\u52a8"             # 活动
TRIBES_KEY = "\u6211\u7684\u90e8\u843d"     # 我的部落

ACTIVITY_ENDPOINTS = {
    "\u6211\u62a5\u540d\u7684": "/activity/activity/myjoin",
    "\u6211\u7b7e\u5230\u7684": "/activity/activity/mycheckin",
    "\u6211\u7ba1\u7406\u7684": "/activity/activity/mymanage",
    "\u6211\u53d1\u8d77\u7684": "/activity/activity/mycreate",
    "\u6211\u5173\u6ce8\u7684": "/activity/activity/myfocuse",
}

TRIBE_ENDPOINTS = {
    "\u6211\u7ba1\u7406\u7684": "/tribe/tribe/mymanagelist",
    "\u6211\u52a0\u5165\u7684": "/tribe/tribe/myjoinlist",
}

# 分类目录（分库分页用）：(key, label, endpoint)
ACTIVITY_CATEGORIES = (
    ("join", "\u6211\u62a5\u540d\u7684", "/activity/activity/myjoin"),
    ("checkin", "\u6211\u7b7e\u5230\u7684", "/activity/activity/mycheckin"),
    ("manage", "\u6211\u7ba1\u7406\u7684", "/activity/activity/mymanage"),
    ("create", "\u6211\u53d1\u8d77\u7684", "/activity/activity/mycreate"),
    ("focus", "\u6211\u5173\u6ce8\u7684", "/activity/activity/myfocuse"),
)

TRIBE_CATEGORIES = (
    ("manage", "\u6211\u7ba1\u7406\u7684", "/tribe/tribe/mymanagelist"),
    ("join", "\u6211\u52a0\u5165\u7684", "/tribe/tribe/myjoinlist"),
)

ACTIVITY_CATEGORY_MAP = {key: (label, endpoint) for key, label, endpoint in ACTIVITY_CATEGORIES}
TRIBE_CATEGORY_MAP = {key: (label, endpoint) for key, label, endpoint in TRIBE_CATEGORIES}

USER_AGENT = "Mozilla/5.0"


class DreamAPIError(RuntimeError):
    """5idream 接口异常。"""


# ------------------------------ 响应解析 ------------------------------


def decode_response(text: str) -> str:
    """兼容站点返回的空格分隔 ASCII 数字响应。"""
    stripped = text.strip()
    parts = stripped.split()
    if parts and all(part.isdigit() for part in parts):
        try:
            return "".join(chr(int(part)) for part in parts)
        except ValueError:
            pass
    return stripped


def parse_response_json(response: httpx.Response) -> dict[str, Any]:
    """兼容普通 JSON、JSONP、BOM 和空格分隔 ASCII 响应。"""
    text = decode_response(response.text).lstrip("\ufeff").strip()
    match = re.search(r"\((\s*\{.*\}\s*)\)\s*;?\s*$", text, re.S)
    if match:
        text = match.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = re.sub(r"\s+", " ", text[:160])
        raise DreamAPIError(
            f"接口返回格式异常，HTTP {response.status_code}，响应摘要：{preview}"
        ) from exc
    if not isinstance(value, dict):
        raise DreamAPIError("接口响应不是对象")
    return value


def _page_rows(data: dict[str, Any]) -> list[Any]:
    """读取分页 rows；缺失/null 表示空页，其它非数组 shape 视为协议异常。"""
    rows = data.get("rows")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise DreamAPIError("分页响应 rows 不是数组")
    return rows


# ------------------------------ 登录（未登录阶段） ------------------------------


async def get_qr(client: httpx.AsyncClient) -> tuple[str, str]:
    """获取登录二维码，返回 (security_id, qr_payload)。"""
    await asyncio.sleep(random.uniform(*settings.request_delay_range))
    response = await client.get(QR_ENDPOINT, timeout=settings.request_timeout)
    response.raise_for_status()
    data = parse_response_json(response)
    if not data.get("success"):
        raise DreamAPIError(f"获取二维码失败: {data}")
    result = data["result"]
    security_id = str(result["securityId"])
    qr_url = f"{result['url']}?securityId={security_id}"
    payload = json.dumps(
        {"qrCode": qr_url, "qrType": str(result["qrType"])},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return security_id, payload


async def poll_login_once(client: httpx.AsyncClient, security_id: str) -> str | None:
    """轮询一次扫码结果；用户确认后返回 token，否则返回 None。"""
    await asyncio.sleep(random.uniform(*settings.request_delay_range))
    callback = f"cb{int(time.time() * 1000)}"
    response = await client.post(
        POLL_ENDPOINT,
        params={"securityId": security_id, "callback": callback},
        headers={"Referer": f"{BASE_URL}/", "User-Agent": USER_AGENT},
        timeout=settings.request_timeout,
    )
    response.raise_for_status()
    data = parse_response_json(response)
    result = data.get("result")
    code = str(data.get("code", ""))
    if result and result != "dummy" and code not in {"500", "90000017"}:
        return str(result)
    return None


# ------------------------------ 已登录客户端 ------------------------------


def absolute_url(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value.startswith(("http://", "https://", "data:", "mailto:", "javascript:")):
        return value
    if value.startswith(("/", "//")):
        return urljoin(BASE_URL, value)
    return value


def collect_image_links(value: Any, key: str = "") -> list[str]:
    links: list[str] = []
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            links.extend(collect_image_links(child_value, child_key))
    elif isinstance(value, list):
        for child in value:
            links.extend(collect_image_links(child, key))
    elif isinstance(value, str):
        candidate = absolute_url(value)
        image_key = any(
            word in key.lower() for word in ("img", "logo", "pic", "photo", "cover", "path")
        )
        image_ext = re.search(r"\.(jpg|jpeg|png|gif|webp|bmp)(?:\?|$)", value, re.I)
        if image_key or image_ext:
            if isinstance(candidate, str) and candidate not in links:
                links.append(candidate)
    return links


class DreamClient:
    """携带某用户 token 的 5idream 异步客户端。

    每个请求显式携带 dmkj_web_token cookie，天然隔离不同用户的会话，
    可在高并发下安全并发创建/复用。
    """

    def __init__(self, token: str):
        self.token = token
        self._client = httpx.AsyncClient(
            timeout=settings.request_timeout,
            headers={"User-Agent": USER_AGENT, "Referer": f"{BASE_URL}/"},
            follow_redirects=True,
        )

    async def __aenter__(self) -> "DreamClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        kwargs.setdefault("cookies", {"dmkj_web_token": self.token})
        # 请求前随机等待，控制频率、避免触发风控
        await asyncio.sleep(random.uniform(*settings.request_delay_range))
        return await self._client.request(method, url, **kwargs)

    async def check_token(self) -> dict[str, Any]:
        """校验 token 并返回用户信息。"""
        response = await self._request(
            "GET", CHECK_TOKEN_ENDPOINT, params={"token": self.token}
        )
        response.raise_for_status()
        data = parse_response_json(response)
        if str(data.get("code")) != "100":
            raise DreamAPIError(f"Token 验证失败: {data}")
        result = data.get("result")
        if not isinstance(result, dict):
            raise DreamAPIError("Token 验证响应缺少用户信息")
        return result

    async def fetch_page(
        self, endpoint: str, user_id: Any, rows: int
    ) -> list[dict[str, Any]]:
        """按站点分页接口取得全部记录，保留每条记录的全部字段。"""
        page = 1
        records: list[dict[str, Any]] = []
        while True:
            response = await self._request(
                "POST",
                urljoin(BASE_URL, endpoint),
                data={"userid": user_id, "rows": rows, "page": page},
            )
            response.raise_for_status()
            data = parse_response_json(response)
            page_rows = _page_rows(data)
            for item in page_rows:
                if isinstance(item, dict):
                    copied = {key: absolute_url(value) for key, value in item.items()}
                    copied["image_links"] = collect_image_links(item)
                    records.append(copied)
            total = int(data.get("records") or 0)
            if not page_rows or len(records) >= total or len(page_rows) < rows:
                break
            page += 1
        return records

    async def fetch_page_one(
        self, endpoint: str, user_id: Any, page: int, rows: int
    ) -> tuple[list[dict[str, Any]], int]:
        """只请求指定分类的指定一页，返回 (records, total)。

        page 从 1 开始；rows 即 page_size。用于「分库分页」查询，
        避免一次把某个分类的全部记录拉完。
        """
        response = await self._request(
            "POST",
            urljoin(BASE_URL, endpoint),
            data={"userid": user_id, "rows": rows, "page": page},
        )
        response.raise_for_status()
        data = parse_response_json(response)
        page_rows = _page_rows(data)
        records: list[dict[str, Any]] = []
        for item in page_rows:
            if isinstance(item, dict):
                copied = {key: absolute_url(value) for key, value in item.items()}
                copied["image_links"] = collect_image_links(item)
                records.append(copied)
        total = int(data.get("records") or 0)
        return records, total

    async def collect_account_data(
        self, user: dict[str, Any], rows: int
    ) -> dict[str, Any]:
        """拉取个人资料 + 全部活动分类 + 全部部落分类。"""
        profile = dict(user)
        if profile.get("logopath"):
            profile["avatar"] = absolute_url(profile["logopath"])
        else:
            profile["avatar"] = None

        # 串行请求，避免对站点造成并发冲击（配合随机等待控制频率）
        activities: dict[str, Any] = {}
        for label, endpoint in ACTIVITY_ENDPOINTS.items():
            activities[label] = await self.fetch_page(endpoint, user["id"], rows)

        tribes: dict[str, Any] = {}
        for label, endpoint in TRIBE_ENDPOINTS.items():
            tribes[label] = await self.fetch_page(endpoint, user["id"], rows)

        return {PROFILE_KEY: profile, ACTIVITIES_KEY: activities, TRIBES_KEY: tribes}

    async def fetch_activity_detail(
        self, activity_id: Any, user_id: Any
    ) -> dict[str, Any]:
        """按详情页模板对应的接口取得活动详情和附件。"""
        response = await self._request(
            "POST",
            urljoin(BASE_URL, "/activity/activity/activityrepublish"),
            data={"activityId": activity_id, "userid": user_id, "rows": 10},
        )
        response.raise_for_status()
        data = parse_response_json(response)
        result = data.get("result")
        if not isinstance(result, dict):
            raise DreamAPIError("未取得活动详情")

        attachment_response = await self._request(
            "POST",
            urljoin(BASE_URL, "/activity/activity/activityattachment"),
            data={"activityId": activity_id, "userid": user_id, "rows": 100, "page": 1},
        )
        if attachment_response.is_success:
            try:
                attachment_data = parse_response_json(attachment_response)
                result["activityattachment"] = attachment_data.get("rows") or []
            except DreamAPIError:
                result["activityattachment"] = []
        return result
