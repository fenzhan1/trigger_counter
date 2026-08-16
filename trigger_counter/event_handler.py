"""trigger_counter 事件处理器。

包含两个处理器：

- ``TriggerCounterInjectHandler``：订阅 ``on_prompt_build``，把命中作用域的条目
  （名称 / 描述 / 值 / 条目状态）通过 SystemReminder 注入提示词，实现流隔离。
- ``TriggerCounterReviewHandler``：订阅 ``after_llm_request``，在回复生成结束后
  交由判定模型更新各条目的值与状态，判定过程放到后台任务，不阻塞回复流程。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api.event_api import EventDecision
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.prompt_api import add_stream_reminder, delete_stream_reminder
from src.app.plugin_system.api.stream_api import get_stream_info
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import SystemReminderBucket
from src.core.prompt import (
    SystemReminderConsumeType,
    SystemReminderInsertType,
)

from .config import EntrySection, TriggerCounterConfig
from .judge import run_judge
from .scope import entry_listens_to, entry_matches
from .service import TriggerCounterService

logger = get_logger("trigger_counter")

# 四种「注入位置 + 消费模式」组合各自使用独立的 reminder name，
# 因为 insert_type 与 consume 都是 per-item 属性，不能合并成一条。
REMINDER_NAMES: dict[tuple[str, str], str] = {
    ("dynamic", "forever"): "trigger_counter_dynamic_forever",
    ("dynamic", "once"): "trigger_counter_dynamic_once",
    ("fixed", "forever"): "trigger_counter_fixed_forever",
    ("fixed", "once"): "trigger_counter_fixed_once",
}

_INSERT_TYPES = {
    "dynamic": SystemReminderInsertType.DYNAMIC,
    "fixed": SystemReminderInsertType.FIXED,
}
_CONSUME_TYPES = {
    "forever": SystemReminderConsumeType.FOREVER,
    "once": SystemReminderConsumeType.ONCE,
}


def _get_config(plugin: Any) -> TriggerCounterConfig:
    """获取插件配置，缺失时回退到默认配置。

    Args:
        plugin: 插件实例。

    Returns:
        TriggerCounterConfig 实例。
    """
    config = getattr(plugin, "config", None)
    if isinstance(config, TriggerCounterConfig):
        return config
    return TriggerCounterConfig()


async def _matched_entries(
    service: TriggerCounterService,
    stream_id: str,
    request_name: str = "",
    default_target: str = "",
) -> list[EntrySection]:
    """筛选出当前聊天流命中白名单 / 黑名单、并匹配目标的启用条目。

    Args:
        service: 服务实例。
        stream_id: 聊天流 ID。
        request_name: 当前 LLM 请求名；为空时不按目标过滤。
        default_target: 全局默认目标，条目 listen_target
            留空时跟随该目标。

    Returns:
        命中的条目列表；无法获取流信息时返回空列表。
    """
    entries = service.enabled_entries()
    if not entries:
        return []

    try:
        stream_meta = await get_stream_info(stream_id)
    except Exception as exc:
        logger.error(f"trigger_counter: 查询 stream_info 失败 stream_id={stream_id!r}: {exc}")
        return []

    if stream_meta is None:
        logger.debug(f"trigger_counter: 无法获取 stream_info，跳过 stream_id={stream_id!r}")
        return []

    matched = [entry for entry in entries if entry_matches(entry, stream_meta)]
    if request_name:
        matched = [
            entry
            for entry in matched
            if entry_listens_to(entry, request_name, default_target)
        ]
    return matched


class TriggerCounterInjectHandler(BaseEventHandler):
    """条目注入处理器。

    订阅 ``on_prompt_build``，把命中条目写入 SystemReminderStore 的流私有命名空间，
    由 chatter 的 LLMContextManager 自动拾取，避免直接修改模板导致内容累积。
    """

    name: str = "trigger_counter_inject_handler"
    description: str = "把自定义条目的名称 / 描述 / 值 / 状态注入提示词"
    weight: int = 15
    intercept_message: bool = False
    init_subscribe: list[str] = ["on_prompt_build"]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 ``on_prompt_build``，向 reminder store 写入条目说明块。

        Args:
            event_name: 事件名称。
            params: 事件参数，包含模板名称与 values。

        Returns:
            决策与原样返回的参数（不修改 params 的键集合）。
        """
        config = _get_config(self.plugin)
        if not config.plugin.enabled or not config.plugin.inject_prompt:
            return EventDecision.SUCCESS, params

        values = params.get("values", {})
        stream_id = str(values.get("stream_id", "")) if isinstance(values, dict) else ""
        if not stream_id:
            return EventDecision.SUCCESS, params

        service = TriggerCounterService(self.plugin)
        matched = await _matched_entries(
            service,
            stream_id,
            request_name=config.target.actor_request_name,
            default_target=config.target.actor_request_name,
        )

        grouped: dict[tuple[str, str], list[EntrySection]] = {}
        for entry in matched:
            key = (entry.insert_type, entry.consume)
            grouped.setdefault(key, []).append(entry)

        for key, reminder_name in REMINDER_NAMES.items():
            bucket_entries = grouped.get(key, [])
            if not bucket_entries:
                # 本轮该组合没有命中条目，清除上一轮残留
                delete_stream_reminder(
                    stream_id=stream_id,
                    bucket=SystemReminderBucket.ACTOR.value,
                    name=reminder_name,
                )
                continue

            content = await service.build_prompt_block(bucket_entries, stream_id)
            if not content.strip():
                delete_stream_reminder(
                    stream_id=stream_id,
                    bucket=SystemReminderBucket.ACTOR.value,
                    name=reminder_name,
                )
                continue

            add_stream_reminder(
                stream_id=stream_id,
                bucket=SystemReminderBucket.ACTOR.value,
                name=reminder_name,
                content=content,
                insert_type=_INSERT_TYPES[key[0]],
                consume=_CONSUME_TYPES[key[1]],
            )

            if config.plugin.debug_log:
                logger.info(
                    f"[trigger_counter] 注入 {reminder_name} "
                    f"(stream_id={stream_id[:8]}...): {content!r}"
                )

        return EventDecision.SUCCESS, params


class TriggerCounterReviewHandler(BaseEventHandler):
    """回复结束后的条目判定处理器。

    订阅 ``after_llm_request``，筛选出 actor 的正式回复请求，然后在后台任务里
    调用判定模型，根据条目描述、当前状态与回复内容更新值并记录新状态。
    """

    name: str = "trigger_counter_review_handler"
    description: str = "回复结束后调用判定模型更新条目的值与状态"
    weight: int = 5
    intercept_message: bool = False
    init_subscribe: list[str] = ["after_llm_request"]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 ``after_llm_request``，异步触发条目判定。

        Args:
            event_name: 事件名称。
            params: 事件参数，包含 request_name / message / tool_calls / meta_data。

        Returns:
            决策与原样返回的参数（不修改 params 的键集合）。
        """
        config = _get_config(self.plugin)
        if not config.plugin.enabled or not config.plugin.review_after_reply:
            return EventDecision.SUCCESS, params

        if not params.get("success"):
            return EventDecision.SUCCESS, params

        request_name = str(params.get("request_name") or "")
        if not request_name:
            return EventDecision.SUCCESS, params

        meta_data = params.get("meta_data")
        stream_id = ""
        if isinstance(meta_data, dict):
            stream_id = str(meta_data.get("stream_id") or "")
        if config.target.require_stream_id and not stream_id:
            return EventDecision.SUCCESS, params

        reply_text = str(params.get("message") or "").strip()
        tool_summary = ""
        if config.judge.include_tool_calls:
            tool_summary = self._summarize_tool_calls(params.get("tool_calls"))

        if len(reply_text) < config.judge.min_text_length and not tool_summary:
            return EventDecision.SUCCESS, params

        try:
            from src.kernel.concurrency import get_task_manager

            get_task_manager().create_task(
                self._review(reply_text, tool_summary, stream_id, request_name),
                name="trigger_counter_review",
            )
        except Exception as exc:
            logger.warning(f"trigger_counter: 无法创建判定后台任务: {exc}")

        return EventDecision.SUCCESS, params

    @staticmethod
    def _summarize_tool_calls(tool_calls: Any) -> str:
        """把工具调用列表压缩成简短文本。

        Args:
            tool_calls: 事件参数里的工具调用列表。

        Returns:
            每行一个工具调用的摘要文本；无内容时返回空字符串。
        """
        if not isinstance(tool_calls, list) or not tool_calls:
            return ""

        lines: list[str] = []
        for call in tool_calls:
            func_name = getattr(call, "func_name", None) or getattr(call, "name", None)
            if func_name is None and isinstance(call, dict):
                func_name = call.get("func_name") or call.get("name")
            args = getattr(call, "args", None)
            if args is None and isinstance(call, dict):
                args = call.get("args")
            if func_name:
                lines.append(f"- {func_name}: {args}" if args else f"- {func_name}")
        return "\n".join(lines)

    async def _review(
        self,
        reply_text: str,
        tool_summary: str,
        stream_id: str,
        request_name: str = "",
    ) -> None:
        """在后台执行判定，异常仅记录日志不外抛。

        Args:
            reply_text: 本轮回复内容。
            tool_summary: 工具调用摘要。
            stream_id: 聊天流 ID。
        """
        try:
            service = TriggerCounterService(self.plugin)
            matched = await _matched_entries(
                service,
                stream_id,
                request_name=request_name,
                default_target=_get_config(self.plugin).target.actor_request_name,
            )
            entries = [entry for entry in matched if entry.review_after_reply]
            if not entries:
                return
            await run_judge(service, entries, reply_text, tool_summary, stream_id, request_name)
        except Exception as exc:
            logger.warning(f"trigger_counter: 条目判定失败: {exc}")
