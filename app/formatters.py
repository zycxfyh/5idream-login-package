"""字段翻译、报告渲染等纯函数（从原 CLI 原样移植，保持输出格式一致）。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Any

from .client import (
    BASE_URL,
    DEFAULT_ACTIVITY_IMAGE,
    ACTIVITIES_KEY,
    PROFILE_KEY,
    TRIBES_KEY,
    absolute_url,
)

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


def build_processed_activity(record: dict[str, Any]) -> dict[str, Any]:
    """整理单条活动记录为精简字段（分页查询用）。"""
    image = value_from(record, "firstImg", "firstimg", "activitypic", "activityPic", "pic", "img", "image")
    if not image and record.get("image_links"):
        image = record["image_links"][0]
    start = value_from(record, "startdate", "startDate", "joinstartdate", "joinStartDate")
    end = value_from(record, "enddate", "endDate", "joinenddate", "joinEndDate")
    return {
        "活动编号": value_from(record, "id", "activityid", "activityId"),
        "活动名称": value_from(record, "name", "activityname", "activityName", "title") or "未命名活动",
        "活动类型": value_from(record, "catalog2name", "catalogname", "activitytype", "activityType", "type") or "未提供",
        "活动地点": value_from(record, "address", "place", "activityaddress", "activityAddress") or "未提供",
        "活动时间": "至".join(item for item in (format_value("startdate", start), format_value("enddate", end)) if item) or "未提供",
        "活动图片": absolute_url(image) if image else DEFAULT_ACTIVITY_IMAGE,
        "部落名称": value_from(record, "tribename", "tribeName", "groupname", "groupName") or "未提供",
        "报名状态": list_status(record),
    }


def build_processed_tribe(record: dict[str, Any]) -> dict[str, Any]:
    """整理单条部落记录为精简字段（分页查询用）。"""
    avatar = value_from(record, "logopath", "logoPath", "avatar", "image", "pic") or DEFAULT_ACTIVITY_IMAGE
    return {
        "部落编号": value_from(record, "id", "tribeid", "tribeId"),
        "部落名称": value_from(record, "name", "tribename", "tribeName") or "未命名部落",
        "部落头像": absolute_url(avatar),
        "部落描述": value_from(record, "description", "introduce", "content") or "未提供",
    }


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
        result["活动"][label] = [build_processed_activity(record) for record in records]
    for label, records in data[TRIBES_KEY].items():
        result["我的部落"][label] = [build_processed_tribe(record) for record in records]
    return result


# ------------------------------ 活动详情 ------------------------------


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


# ------------------------------ 汇总报告 ------------------------------


def render_report(data: dict[str, Any]) -> str:
    profile = data[PROFILE_KEY]
    lines = ["# \u5230\u68a6\u7a7a\u95f4\u4e2a\u4eba\u4fe1\u606f\u62a5\u544a", "", "## 1. \u4e2a\u4eba\u4fe1\u606f", ""]
    lines.append(f"- **姓名：** {profile.get('name') or profile.get('realname') or '未提供'}")
    lines.append(f"- **头像：** {profile.get('avatar') or '未提供'}")
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
