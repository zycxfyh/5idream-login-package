#!/usr/bin/env python3
"""到梦空间扫码登录并导出个人活动/部落信息。

登录成功后只输出数据并退出，不打开浏览器。Token 只保存在内存中。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


BASE_URL = "https://www.5idream.net"
DEFAULT_ACTIVITY_IMAGE = "https://5idream.oss-cn-beijing.aliyuncs.com/horde-default-avatar.png"
REQUEST_DELAY_RANGE = (1.0, 2.0)
QR_ENDPOINT = f"{BASE_URL}/scan_login/getSecurityId"
POLL_ENDPOINT = f"{BASE_URL}/scan_login/rollPoling"
CHECK_TOKEN_ENDPOINT = f"{BASE_URL}/token/checkToken"

PROFILE_KEY = "\u4e2a\u4eba\u4fe1\u606f"
ACTIVITIES_KEY = "\u6d3b\u52a8"
TRIBES_KEY = "\u6211\u7684\u90e8\u843d"

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


# ------------------------------ 网络与登录 ------------------------------


def wait_before_request() -> None:
    """统一控制请求间隔，避免不同接口使用不同节奏。"""
    time.sleep(random.uniform(*REQUEST_DELAY_RANGE))


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


def parse_response_json(response: requests.Response) -> dict[str, Any]:
    """兼容普通 JSON、JSONP、BOM 和空格分隔 ASCII 响应。"""
    text = decode_response(response.text).lstrip("\ufeff").strip()
    match = re.search(r"\((\s*\{.*\}\s*)\)\s*;?\s*$", text, re.S)
    if match:
        text = match.group(1)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        preview = re.sub(r"\s+", " ", text[:160])
        raise RuntimeError(
            f"接口返回格式异常，HTTP {response.status_code}，响应摘要：{preview}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("接口响应不是对象")
    return value


def get_qr(session: requests.Session) -> tuple[str, str]:
    wait_before_request()
    response = session.get(QR_ENDPOINT, timeout=20)
    response.raise_for_status()
    data = parse_response_json(response)
    if not data.get("success"):
        raise RuntimeError(f"获取二维码失败: {data}")
    result = data["result"]
    security_id = str(result["securityId"])
    qr_url = f"{result['url']}?securityId={security_id}"
    payload = json.dumps(
        {"qrCode": qr_url, "qrType": str(result["qrType"])},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return security_id, payload


def write_qr(payload: str, output: Path) -> None:
    try:
        import qrcode
    except ImportError as exc:
        raise RuntimeError(
            "缺少 qrcode 依赖，请运行: python -m pip install -r requirements-5idream-login.txt"
        ) from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    qrcode.make(payload).save(output)


def poll_login(session: requests.Session, security_id: str, timeout: int) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wait_before_request()
        callback = f"cb{int(time.time() * 1000)}"
        response = session.post(
            POLL_ENDPOINT,
            params={"securityId": security_id, "callback": callback},
            headers={"Referer": f"{BASE_URL}/", "User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()
        data = parse_response_json(response)
        result = data.get("result")
        code = str(data.get("code", ""))
        if result and result != "dummy" and code not in {"500", "90000017"}:
            return str(result)
        print("等待扫码确认...", flush=True)
    raise TimeoutError("二维码登录超时，请重新运行脚本")


def check_token(session: requests.Session, token: str) -> dict[str, Any]:
    wait_before_request()
    response = session.get(CHECK_TOKEN_ENDPOINT, params={"token": token}, timeout=20)
    response.raise_for_status()
    data = parse_response_json(response)
    if str(data.get("code")) != "100":
        raise RuntimeError(f"Token 验证失败: {data}")
    result = data.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("Token 验证响应缺少用户信息")
    return result


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
        image_key = any(word in key.lower() for word in ("img", "logo", "pic", "photo", "cover", "path"))
        image_ext = re.search(r"\.(jpg|jpeg|png|gif|webp|bmp)(?:\?|$)", value, re.I)
        if image_key or image_ext:
            if isinstance(candidate, str) and candidate not in links:
                links.append(candidate)
    return links


def fetch_page(session: requests.Session, endpoint: str, user_id: Any, rows: int) -> list[dict[str, Any]]:
    """按站点分页接口取得全部记录，保留每条记录的全部字段。"""
    # 列表接口统一使用 userid、rows、page；直到返回页数不足时停止。
    page = 1
    records: list[dict[str, Any]] = []
    while True:
        wait_before_request()
        response = session.post(
            urljoin(BASE_URL, endpoint),
            data={"userid": user_id, "rows": rows, "page": page},
            timeout=30,
        )
        response.raise_for_status()
        data = parse_response_json(response)
        page_rows = data.get("rows") or []
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


def collect_account_data(session: requests.Session, user: dict[str, Any], rows: int) -> dict[str, Any]:
    profile = dict(user)
    if profile.get("logopath"):
        profile["avatar"] = absolute_url(profile["logopath"])
    else:
        profile["avatar"] = None

    activities = {
        label: fetch_page(session, endpoint, user["id"], rows)
        for label, endpoint in ACTIVITY_ENDPOINTS.items()
    }
    tribes = {
        label: fetch_page(session, endpoint, user["id"], rows)
        for label, endpoint in TRIBE_ENDPOINTS.items()
    }
    return {PROFILE_KEY: profile, ACTIVITIES_KEY: activities, TRIBES_KEY: tribes}


# ------------------------------ 字段与通用格式化 ------------------------------


FIELD_LABELS = {
    "id": "\u7f16\u53f7",
    "name": "\u540d\u79f0",
    "title": "\u6807\u9898",
    "activityname": "\u6d3b\u52a8\u540d\u79f0",
    "activityName": "\u6d3b\u52a8\u540d\u79f0",
    "tribename": "\u90e8\u843d\u540d\u79f0",
    "tribeName": "\u90e8\u843d\u540d\u79f0",
    "logopath": "\u5934\u50cf\u5730\u5740",
    "avatar": "\u5934\u50cf\u5730\u5740",
    "schoolname": "\u5b66\u6821",
    "schoolName": "\u5b66\u6821",
    "schoolid": "\u5b66\u6821\u7f16\u53f7",
    "startdate": "\u5f00\u59cb\u65f6\u95f4",
    "enddate": "\u7ed3\u675f\u65f6\u95f4",
    "joinstartdate": "\u62a5\u540d\u5f00\u59cb",
    "joinenddate": "\u62a5\u540d\u7ed3\u675f",
    "address": "\u5730\u70b9",
    "place": "\u5730\u70b9",
    "content": "\u5185\u5bb9",
    "description": "\u63cf\u8ff0",
    "status": "\u72b6\u6001",
    "state": "\u72b6\u6001",
    "type": "\u7c7b\u578b",
    "organizer": "\u7ec4\u7ec7\u8005",
    "orgname": "\u7ec4\u7ec7\u540d\u79f0",
    "createuser": "\u521b\u5efa\u4eba",
    "createtime": "\u521b\u5efa\u65f6\u95f4",
    "image_links": "\u56fe\u7247\u94fe\u63a5",
}


KEY_WORDS = {
    "activitypic": "\u6d3b\u52a8\u56fe\u7247",
    "activitylevel": "\u6d3b\u52a8\u7b49\u7ea7",
    "activityid": "\u6d3b\u52a8\u7f16\u53f7",
    "activityname": "\u6d3b\u52a8\u540d\u79f0",
    "activitytype": "\u6d3b\u52a8\u7c7b\u578b",
    "activitystatus": "\u6d3b\u52a8\u72b6\u6001",
    "activity": "\u6d3b\u52a8",
    "pic": "\u56fe\u7247",
    "picture": "\u56fe\u7247",
    "image": "\u56fe\u7247",
    "img": "\u56fe\u7247",
    "level": "\u7b49\u7ea7",
    "id": "\u7f16\u53f7",
    "name": "\u540d\u79f0",
    "title": "\u6807\u9898",
    "type": "\u7c7b\u578b",
    "status": "\u72b6\u6001",
    "state": "\u72b6\u6001",
    "start": "\u5f00\u59cb",
    "end": "\u7ed3\u675f",
    "date": "\u65e5\u671f",
    "time": "\u65f6\u95f4",
    "join": "\u62a5\u540d",
    "checkin": "\u7b7e\u5230",
    "check": "\u7b7e\u5230",
    "manage": "\u7ba1\u7406",
    "create": "\u521b\u5efa",
    "focus": "\u5173\u6ce8",
    "tribe": "\u90e8\u843d",
    "user": "\u7528\u6237",
    "school": "\u5b66\u6821",
    "logo": "\u6807\u5fd7",
    "path": "\u5730\u5740",
    "url": "\u94fe\u63a5",
    "cover": "\u5c01\u9762",
    "address": "\u5730\u70b9",
    "place": "\u5730\u70b9",
    "content": "\u5185\u5bb9",
    "description": "\u63cf\u8ff0",
    "org": "\u7ec4\u7ec7",
    "organizer": "\u7ec4\u7ec7\u8005",
    "score": "\u5206\u6570",
    "credit": "\u5b66\u5206",
    "hour": "\u5b66\u65f6",
    "count": "\u6570\u91cf",
    "number": "\u7f16\u53f7",
    "limit": "\u9650\u5236",
    "max": "\u6700\u5927",
    "min": "\u6700\u5c0f",
    "audit": "\u5ba1\u6838",
    "real": "\u771f\u5b9e",
    "account": "\u8d26\u53f7",
    "phone": "\u624b\u673a",
    "mobile": "\u624b\u673a",
    "remark": "\u5907\u6ce8",
    "reason": "\u539f\u56e0",
    "time": "\u65f6\u95f4",
}


def translate_key(key: str) -> str:
    if key in FIELD_LABELS:
        return FIELD_LABELS[key]
    normalized = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(key)).replace("_", " ").replace("-", " ")
    words = [word.lower() for word in normalized.split() if word]
    translated_parts: list[str] = []
    known_words = sorted(KEY_WORDS, key=len, reverse=True)
    for word in words:
        remaining = word
        while remaining:
            match = next((candidate for candidate in known_words if remaining.startswith(candidate)), None)
            if match:
                translated_parts.append(KEY_WORDS[match])
                remaining = remaining[len(match):]
            else:
                translated_parts.append("\u5176\u4ed6")
                break
    translated = "".join(translated_parts)
    return translated or "\u5176\u4ed6\u5b57\u6bb5"


def field_label(key: str) -> str:
    return translate_key(key)


def translate_object_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {translate_key(str(key)): translate_object_keys(child) for key, child in value.items()}
    if isinstance(value, list):
        return [translate_object_keys(child) for child in value]
    return value


def format_value(key: str, value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        if any(word in key.lower() for word in ("date", "time")):
            try:
                number = int(value)
                if number > 10_000_000_000:
                    number //= 1000
                if number > 1_000_000_000:
                    return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, OSError, OverflowError):
                pass
    if isinstance(value, (dict, list)):
        return json.dumps(translate_object_keys(value), ensure_ascii=False, indent=2)
    return str(value)


def value_from(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if record.get(key) not in (None, "", []):
            return record[key]
    return None


def render_activity_summary(label: str, records: list[dict[str, Any]]) -> str:
    lines = [f"{label}（{len(records)} 条）", ""]
    if not records:
        return "\n".join(lines + ["暂无记录。", ""])
    for index, record in enumerate(records, 1):
        name = value_from(record, "name", "activityname", "activityName", "title") or "未命名活动"
        image = value_from(record, "firstImg", "firstimg", "activitypic", "activityPic", "pic", "img", "image")
        if not image and record.get("image_links"):
            image = record["image_links"][0]
        activity_type = value_from(record, "catalog2name", "catalogname", "activitytype", "activityType", "type") or "未提供"
        activity_place = value_from(record, "address", "place", "activityaddress", "activityAddress") or "未提供"
        start = value_from(record, "startdate", "startDate", "joinstartdate", "joinStartDate")
        end = value_from(record, "enddate", "endDate", "joinenddate", "joinEndDate")
        activity_time = "至".join(item for item in (format_value("startdate", start), format_value("enddate", end)) if item) or "未提供"
        status = list_status(record)
        lines.extend(
            [
                f"{index}. 活动名称：{name}",
                f"   活动类型：{activity_type}",
                f"   活动地点：{activity_place}",
                f"   活动时间：{activity_time}",
                f"   活动图片：{absolute_url(image) if image else DEFAULT_ACTIVITY_IMAGE}",
                f"   部落名称：{value_from(record, 'tribename', 'tribeName', 'groupname', 'groupName') or '未提供'}",
                f"   报名状态：{status}",
                "",
            ]
        )
    return "\n".join(lines)


def list_status(record: dict[str, Any]) -> str:
    status = str(record.get("status", ""))
    review = str(record.get("reviewstatus", ""))
    if status == "1":
        return {
            "1": "审核中",
            "2": "审核成功",
            "3": "审核驳回",
            "4": "已取消",
            "5": "待部落审核",
            "6": "待院级审核",
            "7": "待校级审核",
        }.get(review, "不需要审核")
    return {"2": "规划中", "3": "报名中", "4": "等待中", "5": "进行中", "6": "已结束", "7": "已取消"}.get(status, "未提供")


def render_tribe_summary(label: str, records: list[dict[str, Any]]) -> str:
    lines = [f"{label}（{len(records)} 条）", ""]
    if not records:
        return "\n".join(lines + ["暂无记录。", ""])
    for index, record in enumerate(records, 1):
        tribe_id = value_from(record, "id", "tribeid", "tribeId") or "未提供"
        name = value_from(record, "name", "tribename", "tribeName") or "未命名部落"
        avatar = value_from(record, "logopath", "logoPath", "avatar", "image", "pic") or DEFAULT_ACTIVITY_IMAGE
        description = value_from(record, "description", "introduce", "content") or "未提供"
        lines.extend(
            [
                f"{index}. 部落名称：{name}",
                f"   部落编号：{tribe_id}",
                f"   部落头像：{absolute_url(avatar)}",
                f"   部落描述：{description}",
                "",
            ]
        )
    return "\n".join(lines)


def build_processed_data(data: dict[str, Any]) -> dict[str, Any]:
    """生成精简 JSON，只保留整理后的个人、活动和部落字段。"""
    profile = data[PROFILE_KEY]
    result: dict[str, Any] = {
        "个人信息": {
            "姓名": profile.get("name") or profile.get("realname") or "未提供",
            "头像": profile.get("avatar") or "未提供",
        },
        "活动": {},
        "我的部落": {},
    }
    for label, records in data[ACTIVITIES_KEY].items():
        result["活动"][label] = []
        for record in records:
            image = value_from(record, "firstImg", "firstimg", "activitypic", "activityPic", "pic", "img", "image")
            if not image and record.get("image_links"):
                image = record["image_links"][0]
            start = value_from(record, "startdate", "startDate", "joinstartdate", "joinStartDate")
            end = value_from(record, "enddate", "endDate", "joinenddate", "joinEndDate")
            result["活动"][label].append(
                {
                    "活动编号": value_from(record, "id", "activityid", "activityId"),
                    "活动名称": value_from(record, "name", "activityname", "activityName", "title") or "未命名活动",
                    "活动类型": value_from(record, "catalog2name", "catalogname", "activitytype", "activityType", "type") or "未提供",
                    "活动地点": value_from(record, "address", "place", "activityaddress", "activityAddress") or "未提供",
                    "活动时间": "至".join(item for item in (format_value("startdate", start), format_value("enddate", end)) if item) or "未提供",
                    "活动图片": absolute_url(image) if image else DEFAULT_ACTIVITY_IMAGE,
                    "部落名称": value_from(record, "tribename", "tribeName", "groupname", "groupName") or "未提供",
                    "报名状态": list_status(record),
                }
            )
    for label, records in data[TRIBES_KEY].items():
        result["我的部落"][label] = []
        for record in records:
            avatar = value_from(record, "logopath", "logoPath", "avatar", "image", "pic") or DEFAULT_ACTIVITY_IMAGE
            result["我的部落"][label].append(
                {
                    "部落编号": value_from(record, "id", "tribeid", "tribeId"),
                    "部落名称": value_from(record, "name", "tribename", "tribeName") or "未命名部落",
                    "部落头像": absolute_url(avatar),
                    "部落描述": value_from(record, "description", "introduce", "content") or "未提供",
                }
            )
    return result


# ------------------------------ 活动详情 ------------------------------


def fetch_activity_detail(session: requests.Session, activity_id: Any, user_id: Any) -> dict[str, Any]:
    """按详情页模板对应的接口取得活动详情和附件。"""
    wait_before_request()
    response = session.post(
        urljoin(BASE_URL, "/activity/activity/activityrepublish"),
        data={"activityId": activity_id, "userid": user_id, "rows": 10},
        timeout=30,
    )
    response.raise_for_status()
    data = parse_response_json(response)
    result = data.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("未取得活动详情")
    wait_before_request()
    attachment_response = session.post(
        urljoin(BASE_URL, "/activity/activity/activityattachment"),
        data={"activityId": activity_id, "userid": user_id, "rows": 100, "page": 1},
        timeout=30,
    )
    if attachment_response.ok:
        try:
            attachment_data = parse_response_json(attachment_response)
            result["activityattachment"] = attachment_data.get("rows") or []
        except RuntimeError:
            result["activityattachment"] = []
    return result


def detail_value(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, "", []):
            return value
    return None


def format_activity_level(value: Any) -> str:
    return {
        0: "院系级",
        1: "校级",
        2: "市级",
        3: "省级",
        4: "国家级",
        "0": "院系级",
        "1": "校级",
        "2": "市级",
        "3": "省级",
        "4": "国家级",
    }.get(value, "未提供")


def format_join_way(value: Any) -> str:
    return {1: "中签制", 2: "报名制", 3: "评审制", "1": "中签制", "2": "报名制", "3": "评审制"}.get(value, "未提供")


def format_join_scope(value: Any) -> str:
    return {1: "部落内", 2: "学院内", 3: "学校内", 4: "不限", "1": "部落内", "2": "学院内", "3": "学校内", "4": "不限"}.get(value, "不限")


def format_status(detail: dict[str, Any]) -> str:
    status = str(detail.get("status", ""))
    review = str(detail.get("reviewstatus", ""))
    review_labels = {"1": "审核中", "2": "审核成功", "3": "审核驳回", "4": "已取消", "5": "待部落审核", "6": "待院级审核", "7": "待校级审核"}
    if status == "1":
        return review_labels.get(review, "不需要审核")
    labels = {"2": "规划中", "3": "报名中", "4": "等待中", "5": "进行中", "6": "已结束", "7": "已取消"}
    label = labels.get(status, "未提供")
    if status == "2":
        return label + "，报名开始倒计时：" + countdown_text(detail.get("joinstartdate"))
    if status == "3":
        return label + "，报名结束倒计时：" + countdown_text(detail.get("joinenddate"))
    if status == "4":
        return label + "，活动开始倒计时：" + countdown_text(detail.get("startdate"))
    if status == "5":
        return label + "，活动结束倒计时：" + countdown_text(detail.get("enddate"))
    return label


def format_people(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("username") or "未提供")
    if isinstance(value, list):
        names = [format_people(item) for item in value]
        return "、".join(name for name in names if name and name != "未提供") or "未提供"
    return str(value) if value not in (None, "") else "未提供"


def format_activity_time(detail: dict[str, Any]) -> str:
    start = format_value("startdate", detail.get("startdate"))
    end = format_value("enddate", detail.get("enddate"))
    return "至".join(item for item in (start, end) if item) or "未提供"


def countdown_text(value: Any) -> str:
    try:
        timestamp = int(value)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        remaining = max(0, timestamp - int(time.time()))
        days, remaining = divmod(remaining, 86400)
        hours, remaining = divmod(remaining, 3600)
        minutes, seconds = divmod(remaining, 60)
        return f"{days}天{hours}时{minutes}分{seconds}秒"
    except (TypeError, ValueError):
        return "暂无"


def render_activity_detail(detail: dict[str, Any]) -> str:
    """按照详情页 DOM 的固定模块顺序生成可读文本。"""
    activity_id = detail_value(detail, ("id", "activityid", "activityId")) or "未提供"
    lines = ["活动详情", ""]
    lines.extend(
        [
            f"活动标题：{detail.get('name') or detail.get('title') or '未提供'}",
            f"活动ID：{activity_id}",
            f"活动级别：{format_activity_level(detail.get('level'))}",
            f"活动时间：{format_activity_time(detail)}",
            f"活动地点：{detail.get('address') or '未提供'}",
            f"类型：{detail.get('catalog1name') or '未提供'} — {detail.get('catalog2name') or '未提供'}",
            f"发布者：{detail.get('studentname') or '未提供'}",
            f"报名制：{format_join_way(detail.get('joinway'))}",
            f"报名状态：{format_status(detail)}",
            "",
            "具体规则：",
            f"  参与范围：{format_join_scope(detail.get('joinrange'))} {detail.get('schoolname') or ''}".rstrip(),
            f"  报名时间：{format_value('joinstartdate', detail.get('joinstartdate')) or '未提供'} 至 {format_value('joinenddate', detail.get('joinenddate')) or '未提供'}",
            f"  报名方式：{format_join_way(detail.get('joinway'))}",
            f"  报名人数：{'不限人数' if str(detail.get('joinmaxnum')) == '-1' else detail.get('joinmaxnum') or '0'}",
            "",
            "负责人：",
            f"  负责人：{format_people(detail.get('activityManagerVo'))}",
            f"  组织者：{format_people(detail.get('activityManagerList'))}",
            f"  指导老师：{format_people(detail.get('teacherDtoList'))}",
            "",
            f"活动介绍：{detail.get('content') or '暂无'}",
            f"参与须知：{detail.get('jointip') or '暂无'}",
            "",
            "奖项设置：",
        ]
    )
    achievements = detail.get("listAchievement") or []
    if achievements:
        for item in achievements:
            if isinstance(item, dict):
                lines.append(f"  {item.get('item') or '奖项'}：{item.get('reward') or '未提供'}，人数：{item.get('num') or '未提供'}")
            else:
                lines.append(f"  {item}")
    else:
        lines.append("  暂无")
    lines.append("学分设置：")
    scores = detail.get("listScore") or []
    if scores:
        for item in scores:
            if isinstance(item, dict):
                lines.append(f"  {item.get('name') or '学分'}：{item.get('unitcount') or ''}{item.get('unit') or ''}/人，人数：{item.get('maxprovidecount') or '未提供'}")
            else:
                lines.append(f"  {item}")
    else:
        lines.append("  暂无")
    labels = detail.get("activityLabelList") or []
    lines.append("活动标签：" + ("、".join(str(item.get("labelname")) for item in labels if isinstance(item, dict)) or "暂无"))
    lines.extend([f"活动详情：{detail.get('content') or '暂无'}", "", "相关附件："])
    attachments = detail.get("listAttachment") or detail.get("activityattachment") or []
    if not attachments:
        lines.append("  暂无")
    else:
        for item in attachments:
            if isinstance(item, dict):
                link = item.get("url") or item.get("downloadUrl")
                name = item.get("filename") or item.get("fileName") or "附件"
                lines.append(f"  {name}：{absolute_url(link) if link else '未提供链接'}")
            else:
                lines.append(f"  {absolute_url(item)}")
    lines.extend(["", f"活动详情地址：{BASE_URL}/activity/activitydetail.html?activityid={activity_id}"])
    return "\n".join(lines) + "\n"


# ------------------------------ 交互菜单与报告 ------------------------------


def interactive_activity_menu(session: requests.Session, data: dict[str, Any], user: dict[str, Any]) -> None:
    """保持登录会话，循环处理活动分类、部落分类和活动详情选择。"""
    categories = [("activity", label, records) for label, records in data[ACTIVITIES_KEY].items()]
    categories.extend(("tribe", f"我的部落（{label}）", records) for label, records in data[TRIBES_KEY].items())
    while True:
        print("\n请选择活动类型：")
        for index, (_, label, _) in enumerate(categories, 1):
            print(f"{index}. {label}")
        print("0. 退出")
        choice = input("请输入编号：").strip()
        if choice == "0":
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(categories):
            print("选择无效，请重试。")
            continue
        kind, label, records = categories[int(choice) - 1]
        if kind == "tribe":
            print("\n" + render_tribe_summary(label, records))
            continue
        print("\n" + render_activity_summary(label, records))
        if not records:
            continue
        selected = input("请输入要查看详情的活动编号，输入 0 返回类型选择：").strip()
        if selected == "0":
            continue
        if not selected.isdigit() or not 1 <= int(selected) <= len(records):
            print("选择无效，请重试。")
            continue
        record = records[int(selected) - 1]
        activity_id = value_from(record, "activityid", "activityId", "id")
        if not activity_id:
            print("该记录没有活动编号，无法请求详情。")
            continue
        try:
            print("正在请求活动详情，请稍候……")
            detail = fetch_activity_detail(session, activity_id, user["id"])
            print("\n" + render_activity_detail(detail))
        except Exception as exc:
            print(f"活动详情请求失败：{exc}")


def render_report(data: dict[str, Any]) -> str:
    profile = data[PROFILE_KEY]
    lines = ["# \u5230\u68a6\u7a7a\u95f4\u4e2a\u4eba\u4fe1\u606f\u62a5\u544a", "", "## 1. \u4e2a\u4eba\u4fe1\u606f", ""]
    lines.append(f"- **\u59d3\u540d：** {profile.get('name') or profile.get('realname') or '\u672a\u63d0\u4f9b'}")
    lines.append(f"- **\u5934\u50cf：** {profile.get('avatar') or '\u672a\u63d0\u4f9b'}")
    for key, value in profile.items():
        if key in {"name", "realname", "avatar", "logopath"}:
            continue
        formatted = format_value(key, value)
        if formatted:
            lines.append(f"- **{field_label(key)}：** {formatted}")

    lines.extend(["", "## 2. \u6211\u7684\u6d3b\u52a8", ""])
    for index, (label, records) in enumerate(data[ACTIVITIES_KEY].items(), 1):
        lines.extend([f"### 2.{index} {label}\uff08{len(records)} \u6761\uff09", ""])
        lines.append(render_activity_summary(label, records))

    lines.extend(["## 3. \u6211\u7684\u90e8\u843d", ""])
    for index, (label, records) in enumerate(data[TRIBES_KEY].items(), 1):
        lines.extend([f"### 3.{index} {label}\uff08{len(records)} \u6761\uff09", ""])
        lines.append(render_tribe_summary(label, records))
    return "\n".join(lines).rstrip() + "\n"


def render_console(report: str) -> str:
    """控制台使用纯文本，避免显示 Markdown 星号。"""
    plain = report.replace("**", "").replace("*", "")
    plain = re.sub(r"^#{1,6}\s*", "", plain, flags=re.MULTILINE)
    return plain


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="到梦空间扫码登录并导出个人数据")
    parser.add_argument("--qr", type=Path, default=Path("5idream-login-qr.png"))
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--rows", type=int, default=100)
    parser.add_argument(
        "--json-out",
        type=Path,
        default=Path(
            r"C:\Users\31058\Documents\Codex\2026-07-27\reverse-flow-https-www-5idream-net\outputs\5idream-data.json"
        ),
        help="整理后的 JSON 数据路径",
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=Path(
            r"C:\Users\31058\Documents\Codex\2026-07-27\reverse-flow-https-www-5idream-net\outputs\5idream-report.md"
        ),
        help="整理后的 Markdown 报告路径",
    )
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Referer": f"{BASE_URL}/"})
    try:
        security_id, payload = get_qr(session)
        write_qr(payload, args.qr)
        print(f"二维码已保存: {args.qr.resolve()}")
        print("请使用到梦空间 APP 扫码并确认登录。")
        token = poll_login(session, security_id, args.timeout)
        session.cookies.set("dmkj_web_token", token, domain="www.5idream.net", path="/")
        user = check_token(session, token)
        output = collect_account_data(session, user, max(1, args.rows))
        rendered = render_report(output)
        print(render_console(rendered))
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(rendered, encoding="utf-8")
        print(f"整理后的报告已保存: {args.report_out.resolve()}")
        processed = build_processed_data(output)
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(processed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"整理后的 JSON 已保存: {args.json_out.resolve()}")
        interactive_activity_menu(session, output, user)
        return 0
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
