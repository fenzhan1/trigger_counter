"""trigger_counter 服务组件。

职责：

- 读写各条目的「值」与「条目状态」（持久化在
  ``data/json_storage/trigger_counter/records.json``）
- 构建注入提示词的条目说明块

注意：``service_api.get_service()`` 每次调用都会创建新实例，因此所有共享状态
（并发锁、内存缓存）都放在模块级，而不是实例属性上。
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.app.plugin_system.api import storage_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseService

from .config import EntrySection, TriggerCounterConfig

logger = get_logger("trigger_counter.service")

_STORE_NAME = "trigger_counter"
_RECORD_KEY = "records"
_GLOBAL_SCOPE = "__global__"

# 服务实例非单例，共享状态必须放在模块级
_LOCK = asyncio.Lock()
_CACHE: dict[str, dict[str, dict[str, Any]]] = {}
_CACHE_LOADED = False


def _clamp(value: float, low: float, high: float) -> float:
    """把数值裁剪到给定区间。

    Args:
        value: 待裁剪的数值。
        low: 下限。
        high: 上限。

    Returns:
        裁剪后的数值。
    """
    if high < low:
        low, high = high, low
    return max(low, min(high, value))


def _normalize_number(raw: Any, default: float = 0.0) -> float:
    """把任意输入转换为浮点数。

    Args:
        raw: 原始输入。
        default: 转换失败时的回退值。

    Returns:
        转换后的浮点数。
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def format_value(value: float) -> str:
    """把值格式化为便于阅读的字符串（整数不带小数点）。

    Args:
        value: 数值。

    Returns:
        格式化后的字符串。
    """
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


class TriggerCounterService(BaseService):
    """条目值与状态记录服务。"""

    name: str = "trigger_counter"
    description: str = "维护自定义条目的值与条目状态，并构建注入提示词的说明块"
    version: str = "1.2.0"

    # ------------------------------------------------------------------
    # 配置与条目
    # ------------------------------------------------------------------

    def config(self) -> TriggerCounterConfig:
        """获取插件配置，缺失时回退到默认配置。

        Returns:
            TriggerCounterConfig 实例。
        """
        config = getattr(self.plugin, "config", None)
        if isinstance(config, TriggerCounterConfig):
            return config
        return TriggerCounterConfig()

    def enabled_entries(self) -> list[EntrySection]:
        """获取启用中的条目列表（按名称去重）。

        Returns:
            启用且名称非空的条目列表。
        """
        result: list[EntrySection] = []
        seen: set[str] = set()
        for entry in self.config().entries:
            entry_name = (entry.name or "").strip()
            if not entry.enabled or not entry_name or entry_name in seen:
                continue
            seen.add(entry_name)
            result.append(entry)
        return result

    def find_entry(self, entry_name: str) -> EntrySection | None:
        """按名称查找启用中的条目。

        Args:
            entry_name: 条目名称。

        Returns:
            命中的条目；不存在时返回 ``None``。
        """
        target = (entry_name or "").strip()
        for entry in self.enabled_entries():
            if entry.name.strip() == target:
                return entry
        return None

    def scope_key(self, stream_id: str = "") -> str:
        """计算记录使用的作用域键。

        Args:
            stream_id: 聊天流 ID。

        Returns:
            按流隔离时返回 ``stream:<id>``，否则返回全局键。
        """
        if self.config().plugin.scope_by_stream and stream_id:
            return f"stream:{stream_id}"
        return _GLOBAL_SCOPE

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    async def _ensure_loaded(self) -> None:
        """确保记录缓存已从磁盘加载（调用方需持有锁）。"""
        global _CACHE_LOADED

        if _CACHE_LOADED:
            return

        stored = await storage_api.load_json(_STORE_NAME, _RECORD_KEY) or {}
        _CACHE.clear()
        for scope, entries in stored.items():
            if not isinstance(entries, dict):
                continue
            bucket: dict[str, dict[str, Any]] = {}
            for entry_name, record in entries.items():
                if not isinstance(record, dict):
                    continue
                bucket[str(entry_name)] = {
                    "value": _normalize_number(record.get("value")),
                    "state": str(record.get("state") or ""),
                }
            _CACHE[str(scope)] = bucket
        _CACHE_LOADED = True

    async def _flush(self) -> None:
        """把记录缓存写回磁盘（调用方需持有锁）。"""
        await storage_api.save_json(_STORE_NAME, _RECORD_KEY, {
            scope: {name: dict(record) for name, record in entries.items()}
            for scope, entries in _CACHE.items()
        })

    async def load_records(self, stream_id: str = "") -> dict[str, dict[str, Any]]:
        """加载当前作用域下所有启用条目的值与状态，为新条目补齐初始数据。

        Args:
            stream_id: 聊天流 ID，仅在按流隔离时生效。

        Returns:
            条目名称到 ``{"value": float, "state": str}`` 的映射。
        """
        scope = self.scope_key(stream_id)

        async with _LOCK:
            await self._ensure_loaded()
            bucket = _CACHE.setdefault(scope, {})

            dirty = False
            for entry in self.enabled_entries():
                entry_name = entry.name.strip()
                if entry_name not in bucket:
                    bucket[entry_name] = {
                        "value": _clamp(
                            float(entry.initial_value),
                            float(entry.value_min),
                            float(entry.value_max),
                        ),
                        "state": (entry.initial_state or "").strip(),
                    }
                    dirty = True

            if dirty:
                await self._flush()

            return {
                entry.name.strip(): dict(bucket[entry.name.strip()])
                for entry in self.enabled_entries()
                if entry.name.strip() in bucket
            }

    async def apply_updates(
        self,
        updates: list[dict[str, Any]],
        stream_id: str = "",
    ) -> list[dict[str, Any]]:
        """把判定结果写入记录。

        Args:
            updates: 判定结果列表，每项形如
                ``{"name": str, "op": "increase|decrease|replace|none",
                "amount": float, "state": str}``。
            stream_id: 聊天流 ID，仅在按流隔离时生效。

        Returns:
            实际发生变化的条目摘要列表，每项包含 ``name`` / ``op`` /
            ``before`` / ``after`` / ``state``。
        """
        if not updates:
            return []

        config = self.config()
        state_limit = int(config.judge.state_max_length)
        scope = self.scope_key(stream_id)
        changed: list[dict[str, Any]] = []

        async with _LOCK:
            await self._ensure_loaded()
            bucket = _CACHE.setdefault(scope, {})

            for update in updates:
                entry_name = str(update.get("name") or "").strip()
                entry = self.find_entry(entry_name)
                if entry is None:
                    continue

                record = bucket.setdefault(
                    entry_name,
                    {
                        "value": float(entry.initial_value),
                        "state": (entry.initial_state or "").strip(),
                    },
                )
                before = _normalize_number(record.get("value"))
                op = str(update.get("op") or "none").strip().lower()
                amount = _normalize_number(update.get("amount"))
                limit = float(entry.max_delta_per_round)

                if op == "increase":
                    delta = _clamp(abs(amount), 0.0, limit)
                    after = before + delta
                elif op == "decrease":
                    delta = _clamp(abs(amount), 0.0, limit)
                    after = before - delta
                elif op == "replace":
                    after = amount
                else:
                    after = before

                after = _clamp(after, float(entry.value_min), float(entry.value_max))

                new_state = update.get("state")
                state_text = record.get("state", "")
                if isinstance(new_state, str) and new_state.strip():
                    state_text = new_state.strip()[:state_limit]

                value_changed = abs(after - before) > 1e-9
                state_changed = state_text != record.get("state", "")
                if not value_changed and not state_changed:
                    continue

                record["value"] = after
                record["state"] = state_text
                changed.append(
                    {
                        "name": entry_name,
                        "op": op,
                        "before": before,
                        "after": after,
                        "state": state_text,
                    }
                )

            if changed:
                await self._flush()

        return changed

    async def adjust(
        self,
        entry_name: str,
        delta: float,
        stream_id: str = "",
    ) -> float | None:
        """手动调整某个条目的值。

        Args:
            entry_name: 条目名称。
            delta: 增量，可为负数。
            stream_id: 聊天流 ID，仅在按流隔离时生效。

        Returns:
            调整后的值；条目不存在时返回 ``None``。
        """
        entry = self.find_entry(entry_name)
        if entry is None:
            return None

        await self.load_records(stream_id)
        scope = self.scope_key(stream_id)

        async with _LOCK:
            await self._ensure_loaded()
            bucket = _CACHE.setdefault(scope, {})
            record = bucket.setdefault(
                entry.name.strip(),
                {"value": float(entry.initial_value), "state": ""},
            )
            record["value"] = _clamp(
                _normalize_number(record.get("value")) + delta,
                float(entry.value_min),
                float(entry.value_max),
            )
            await self._flush()
            return float(record["value"])

    async def set_state(
        self,
        entry_name: str,
        state: str,
        stream_id: str = "",
    ) -> bool:
        """手动设置某个条目的状态文本。

        Args:
            entry_name: 条目名称。
            state: 新的状态文本。
            stream_id: 聊天流 ID，仅在按流隔离时生效。

        Returns:
            设置成功返回 ``True``；条目不存在返回 ``False``。
        """
        entry = self.find_entry(entry_name)
        if entry is None:
            return False

        await self.load_records(stream_id)
        limit = int(self.config().judge.state_max_length)
        scope = self.scope_key(stream_id)

        async with _LOCK:
            await self._ensure_loaded()
            bucket = _CACHE.setdefault(scope, {})
            record = bucket.setdefault(
                entry.name.strip(),
                {"value": float(entry.initial_value), "state": ""},
            )
            record["state"] = state.strip()[:limit]
            await self._flush()
            return True

    async def reset(self, entry_name: str | None = None, stream_id: str = "") -> int:
        """把值与状态重置为条目的初始设定。

        Args:
            entry_name: 要重置的条目名称；为 ``None`` 时重置全部启用条目。
            stream_id: 聊天流 ID，仅在按流隔离时生效。

        Returns:
            被重置的条目数量。
        """
        entries = self.enabled_entries()
        if entry_name is not None:
            entries = [e for e in entries if e.name.strip() == entry_name.strip()]
            if not entries:
                return 0

        scope = self.scope_key(stream_id)

        async with _LOCK:
            await self._ensure_loaded()
            bucket = _CACHE.setdefault(scope, {})
            for entry in entries:
                bucket[entry.name.strip()] = {
                    "value": _clamp(
                        float(entry.initial_value),
                        float(entry.value_min),
                        float(entry.value_max),
                    ),
                    "state": (entry.initial_state or "").strip(),
                }
            await self._flush()
            return len(entries)

    # ------------------------------------------------------------------
    # 提示词注入
    # ------------------------------------------------------------------

    async def build_prompt_block(
        self,
        entries: list[EntrySection],
        stream_id: str = "",
    ) -> str:
        """构建注入提示词的条目说明块。

        Args:
            entries: 本轮命中作用域的条目列表。
            stream_id: 聊天流 ID，仅在按流隔离时生效。

        Returns:
            注入文本；无可用条目时返回空字符串。
        """
        if not entries:
            return ""

        config = self.config()
        records = await self.load_records(stream_id)

        lines: list[str] = [f"## {config.prompt.block_title}"]
        header_note = config.prompt.header_note.strip()
        if header_note:
            lines.append(header_note)

        for index, entry in enumerate(entries, start=1):
            entry_name = entry.name.strip()
            record = records.get(entry_name, {})
            lines.append(f"{index}. 名称：{entry_name}")
            description = (entry.description or "").strip()
            if description:
                lines.append(f"   描述：{description}")
            if config.prompt.show_value:
                value = _normalize_number(record.get("value"))
                lines.append(f"   当前值：{format_value(value)}")
            if config.prompt.show_state:
                state = str(record.get("state") or "").strip() or "暂无"
                lines.append(f"   当前状态：{state}")

        return "\n".join(lines)
