"""回复结束后的条目判定逻辑。

流程：

1. 把命中条目的名称 / 描述 / 当前值 / 当前状态，连同本轮回复内容交给判定模型
2. 判定模型按条目返回操作（增加 / 减少 / 替换 / 不变）、幅度与新的条目状态
3. 结果交给服务层写入记录
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.app.plugin_system.api import llm_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import ROLE, LLMContextManager, LLMPayload, Text

from .service import format_value

if TYPE_CHECKING:
    from .config import EntrySection, TriggerCounterConfig
    from .service import TriggerCounterService

logger = get_logger("trigger_counter.judge")

_VALID_OPS = ("increase", "decrease", "replace", "none")


def extract_json_object(raw: str) -> dict[str, Any] | None:
    """从模型输出中提取第一个 JSON 对象。

    Args:
        raw: 模型原始输出文本。

    Returns:
        解析出的字典；解析失败时返回 ``None``。
    """
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def resolve_model_set(config: TriggerCounterConfig) -> Any | None:
    """按配置解析判定模型集，优先使用自选模型名。

    Args:
        config: 插件配置。

    Returns:
        可用的 ModelSet；都不可用时返回 ``None``。
    """
    judge = config.judge
    model_name = (judge.model_name or "").strip()
    if model_name:
        try:
            return llm_api.get_model_set_by_name(
                model_name,
                temperature=judge.temperature,
                max_tokens=judge.max_tokens,
            )
        except Exception as exc:
            logger.warning(f"判定模型 {model_name!r} 不可用，回退任务模型集: {exc}")

    fallback = (judge.fallback_task or "").strip()
    if not fallback:
        return None
    try:
        return llm_api.get_model_set_by_task(fallback)
    except Exception as exc:
        logger.warning(f"任务模型集 {fallback!r} 不可用，跳过本轮判定: {exc}")
        return None


def build_judge_prompt(
    entries: list[EntrySection],
    records: dict[str, dict[str, Any]],
    reply_text: str,
    tool_summary: str = "",
    request_name: str = "",
) -> str:
    """构建判定模型的提示词。

    Args:
        entries: 参与判定的条目列表。
        records: 条目名称到 ``{"value", "state"}`` 的当前记录。
        reply_text: 本轮回复内容。
        tool_summary: 本轮工具调用摘要，可为空。
        request_name: 当前被监听的 LLM 请求名称，可为空。

    Returns:
        完整提示词文本。
    """
    lines: list[str] = [
        (
            "你是一个严格的记录员。请根据本轮回复内容，判断每个条目的值应该如何变化，"
            "并更新该条目的状态描述。"
        ),
        "",
        "条目清单：",
    ]
    if request_name:
        lines.append(f"本轮监听目标：{request_name}")
        lines.append("")

    for index, entry in enumerate(entries, start=1):
        entry_name = entry.name.strip()
        record = records.get(entry_name, {})
        value = record.get("value", entry.initial_value)
        state = str(record.get("state") or "").strip() or "暂无"
        lines.append(f"{index}. 名称：{entry_name}")
        lines.append(f"   描述：{(entry.description or '').strip()}")
        lines.append(f"   当前值：{format_value(float(value))}")
        lines.append(f"   当前状态：{state}")
        lines.append(
            f"   取值范围：{format_value(float(entry.value_min))} ~ "
            f"{format_value(float(entry.value_max))}，"
            f"单轮最大变化：{format_value(float(entry.max_delta_per_round))}"
        )

    lines.extend(
        [
            "",
            "本轮回复内容：",
            reply_text.strip(),
        ]
    )

    if tool_summary.strip():
        lines.extend(["", "本轮工具调用：", tool_summary.strip()])

    lines.extend(
        [
            "",
            "判定规则：",
            "- op=increase 表示值增加 amount；op=decrease 表示值减少 amount。",
            "- op=replace 表示把值直接替换为 amount。",
            "- op=none 表示本轮不变化，此时 amount 填 0。",
            "- amount 必须是非负数字（replace 时为目标数值），且不超过该条目的单轮最大变化。",
            (
                "- state 是更新后的条目状态，一句话以内，写清楚当前情况或最近一次变化的原因；"
                "没有变化时可沿用原状态。"
            ),
            "- 依据必须来自回复内容与条目描述，不要凭空推测。",
            "",
            "只输出如下 JSON，不要输出任何解释或代码块标记：",
            '{"entries": [{"name": "条目名称", "op": "increase", "amount": 1, "state": "状态文本"}]}',
        ]
    )

    return "\n".join(lines)


def parse_judge_result(
    payload: dict[str, Any],
    valid_names: set[str],
) -> list[dict[str, Any]]:
    """解析判定模型输出，过滤非法条目与操作。

    Args:
        payload: 模型输出解析出的字典。
        valid_names: 允许出现的条目名称集合。

    Returns:
        规范化后的更新列表。
    """
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        return []

    updates: list[dict[str, Any]] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        entry_name = str(item.get("name") or "").strip()
        if entry_name not in valid_names:
            continue

        op = str(item.get("op") or "none").strip().lower()
        if op not in _VALID_OPS:
            op = "none"

        try:
            amount = float(item.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0

        state = item.get("state")
        updates.append(
            {
                "name": entry_name,
                "op": op,
                "amount": amount,
                "state": state if isinstance(state, str) else "",
            }
        )
    return updates


async def run_judge(
    service: TriggerCounterService,
    entries: list[EntrySection],
    reply_text: str,
    tool_summary: str = "",
    stream_id: str = "",
    request_name: str = "",
) -> list[dict[str, Any]]:
    """调用判定模型并把结果写入记录。

    Args:
        service: 服务实例，用于读写记录。
        entries: 参与判定的条目列表。
        reply_text: 本轮回复内容。
        tool_summary: 本轮工具调用摘要，可为空。
        stream_id: 聊天流 ID。
        request_name: 当前被监听的 LLM 请求名称。

    Returns:
        实际发生变化的条目摘要列表。
    """
    if not entries:
        return []

    config = service.config()
    model_set = resolve_model_set(config)
    if not model_set:
        return []

    records = await service.load_records(stream_id)
    prompt = build_judge_prompt(
        entries, records, reply_text, tool_summary, request_name
    )

    request = llm_api.create_llm_request(
        model_set=model_set,
        request_name=config.judge.request_name,
        context_manager=LLMContextManager(),
    )
    request.add_payload(LLMPayload(ROLE.USER, Text(prompt)))

    try:
        response = await request.send(stream=False)
        await response
    except Exception as exc:
        logger.warning(f"判定模型请求失败: {exc}")
        return []

    raw = (response.message or "").strip()
    payload = extract_json_object(raw)
    if payload is None:
        logger.warning("判定模型输出无法解析为 JSON，跳过本轮更新")
        return []

    valid_names = {entry.name.strip() for entry in entries}
    updates = parse_judge_result(payload, valid_names)
    if not updates:
        return []

    changed = await service.apply_updates(updates, stream_id)
    if config.plugin.debug_log and changed:
        summary = "; ".join(
            f"{item['name']}: {format_value(item['before'])} -> "
            f"{format_value(item['after'])} ({item['op']})"
            for item in changed
        )
        logger.info(f"[trigger_counter] 判定结果: {summary}")
    return changed
