"""条目作用域（白名单 / 黑名单）匹配工具。

作用域字符串格式：

- ``"group:*"``   全部群聊
- ``"group:123"`` 群号为 123 的群聊
- ``"user:*"``    全部私聊
- ``"user:456"``  QQ 号为 456 的私聊
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import EntrySection


def scope_matches(spec: str, chat_type: str, chat_id: str, platform: str = "") -> bool:
    """判断单条作用域字符串是否命中当前聊天流。

    Args:
        spec: 作用域字符串，如 ``"group:*"`` / ``"user:456"``。
        chat_type: 聊天类型，``"group"`` 或 ``"private"``。
        chat_id: 群号（群聊）或哈希后的 person_id（私聊）。
        platform: 平台标识，用于计算私聊用户哈希。

    Returns:
        是否命中。
    """
    if ":" not in spec:
        return False
    kind, value = spec.split(":", 1)
    kind = kind.strip().lower()
    value = value.strip()

    if kind == "group":
        if chat_type != "group":
            return False
        return value in ("*", chat_id)
    if kind == "user":
        if chat_type != "private":
            return False
        if value == "*":
            return True
        if platform:
            expected = hashlib.sha256(f"{platform}_{value}".encode()).hexdigest()
            return expected == chat_id
        return value == chat_id
    return False


def entry_matches(entry: EntrySection, stream_info: dict[str, Any]) -> bool:
    """判断条目的白名单 / 黑名单是否对当前聊天流生效。

    规则：

    - include 与 exclude 均为空 -> 全局生效
    - include 非空 -> 至少命中一条才生效
    - exclude 非空 -> 命中任意一条即排除

    Args:
        entry: 条目配置。
        stream_info: ``stream_api.get_stream_info`` 返回的聊天流元数据。

    Returns:
        该条目是否对当前聊天流生效。
    """
    chat_type = str(stream_info.get("chat_type", ""))
    platform = str(stream_info.get("platform", ""))
    if chat_type == "group":
        chat_id = str(stream_info.get("group_id") or "")
    else:
        chat_id = str(stream_info.get("person_id") or "")

    if entry.include and not any(
        scope_matches(spec, chat_type, chat_id, platform) for spec in entry.include
    ):
        return False

    if entry.exclude and any(
        scope_matches(spec, chat_type, chat_id, platform) for spec in entry.exclude
    ):
        return False

    return True


def entry_listens_to(
    entry: EntrySection, request_name: str, default_target: str
) -> bool:
    """判断条目的监听目标是否匹配当前 LLM 请求。

    条目 ``listen_target`` 留空时跟随全局默认目标；填写后只匹配指定
    ``request_name``（after_llm_request 事件中的 request_name）。

    Args:
        entry: 条目配置。
        request_name: 当前请求名。
        default_target: 全局默认监听目标。

    Returns:
        该条目是否监听当前请求。
    """
    target = (entry.listen_target or "").strip()
    if not target:
        target = (default_target or "").strip()
    return bool(target) and target == (request_name or "").strip()
