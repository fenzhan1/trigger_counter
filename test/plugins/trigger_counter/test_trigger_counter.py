"""trigger_counter 插件核心逻辑测试。"""

from __future__ import annotations

from typing import Any

import pytest

from plugins.trigger_counter.config import EntrySection, TriggerCounterConfig
from plugins.trigger_counter.event_handler import TriggerCounterInjectHandler
from plugins.trigger_counter.judge import build_judge_prompt, parse_judge_result
from plugins.trigger_counter.scope import entry_listens_to, entry_matches, scope_matches
from plugins.trigger_counter.service import TriggerCounterService

GROUP_INFO = {
    "chat_type": "group",
    "group_id": "123456",
    "person_id": "",
    "platform": "qq",
}


class FakePlugin:
    """只提供 config 的最小插件替身。"""

    def __init__(self, config: TriggerCounterConfig) -> None:
        self.config = config


class FakeStorage:
    """内存版 storage_api，避免测试写入磁盘。"""

    def __init__(self) -> None:
        self.saved: list[dict[str, Any]] = []

    async def load_json(self, store_name: str, key: str) -> dict[str, Any]:
        return {}

    async def save_json(self, store_name: str, key: str, value: dict[str, Any]) -> None:
        self.saved.append(value)


@pytest.fixture(autouse=True)
def reset_service_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """清理服务模块级缓存，并注入内存存储。"""
    from plugins.trigger_counter import service as service_module

    service_module._CACHE.clear()
    service_module._CACHE_LOADED = False
    monkeypatch.setattr(service_module, "storage_api", FakeStorage())


def test_config_default_contains_listen_target() -> None:
    """默认配置包含条目监听目标字段。"""
    entry = TriggerCounterConfig().entries[0]
    assert isinstance(entry.listen_target, str)
    assert entry.listen_target == ""


def test_scope_matches() -> None:
    """作用域匹配规则：群聊、私聊、通配符与平台哈希。"""
    assert scope_matches("group:*", "group", "123")
    assert scope_matches("group:123", "group", "123")
    assert not scope_matches("group:456", "group", "123")
    assert not scope_matches("group:123", "private", "123")
    assert scope_matches("user:*", "private", "abc")
    assert scope_matches("user:456", "private", "456", platform="")


def test_entry_matches_include_exclude() -> None:
    """条目的白名单 / 黑名单过滤。"""
    entry = EntrySection(name="A", include=["group:123456"])
    assert entry_matches(entry, GROUP_INFO)

    entry = EntrySection(name="A", include=["group:999"])
    assert not entry_matches(entry, GROUP_INFO)

    entry = EntrySection(name="A", exclude=["group:123456"])
    assert not entry_matches(entry, GROUP_INFO)

    entry = EntrySection(name="A")
    assert entry_matches(entry, GROUP_INFO)


def test_entry_listens_to() -> None:
    """条目监听目标：留空跟随默认，填写则精确匹配。"""
    entry = EntrySection(name="A")
    assert entry_listens_to(entry, "default_chatter", "default_chatter")
    assert not entry_listens_to(entry, "other_task", "default_chatter")

    custom = EntrySection(name="A", listen_target="special_task")
    assert entry_listens_to(custom, "special_task", "default_chatter")
    assert not entry_listens_to(custom, "default_chatter", "default_chatter")


def test_judge_prompt_and_parse() -> None:
    """判定提示词包含监听目标与条目信息，解析结果过滤非法条目。"""
    entry = EntrySection(name="好感度", description="对方友善时增加")
    prompt = build_judge_prompt(
        entries=[entry],
        records={"好感度": {"value": 50.0, "state": "普通"}},
        reply_text="今天聊得很开心",
        request_name="default_chatter",
    )
    assert "default_chatter" in prompt
    assert "好感度" in prompt
    assert "对方友善时增加" in prompt

    updates = parse_judge_result(
        {"entries": [{"name": "好感度", "op": "increase", "amount": 1, "state": "更开心"}]},
        valid_names={"好感度"},
    )
    assert updates == [{"name": "好感度", "op": "increase", "amount": 1.0, "state": "更开心"}]

    updates = parse_judge_result(
        {"entries": [{"name": "不存在", "op": "increase", "amount": 1, "state": ""}]},
        valid_names={"好感度"},
    )
    assert updates == []


async def test_service_apply_updates() -> None:
    """值更新遵循上下限与单轮最大变化，状态按上限截断。"""
    config = TriggerCounterConfig()
    config.entries = [
        EntrySection(
            name="好感度",
            initial_value=50.0,
            value_min=0.0,
            value_max=100.0,
            max_delta_per_round=5.0,
        )
    ]
    service = TriggerCounterService(FakePlugin(config))

    records = await service.load_records()
    assert records["好感度"]["value"] == 50.0

    changed = await service.apply_updates(
        [
            {
                "name": "好感度",
                "op": "increase",
                "amount": 99.0,
                "state": "x" * 200,
            }
        ]
    )
    assert changed[0]["after"] == 55.0
    assert len(changed[0]["state"]) == config.judge.state_max_length

    changed = await service.apply_updates(
        [{"name": "好感度", "op": "replace", "amount": 200.0, "state": "封顶"}]
    )
    assert changed[0]["after"] == 100.0


async def test_inject_handler_filters_listen_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入时只写入监听当前目标的条目。"""
    config = TriggerCounterConfig()
    config.entries = [
        EntrySection(name="全局条目", enabled=True, listen_target=""),
        EntrySection(name="其他目标", enabled=True, listen_target="other_task"),
    ]
    plugin = FakePlugin(config)

    async def fake_stream_info(stream_id: str) -> dict[str, Any]:
        return GROUP_INFO

    written: list[tuple[str, str, str]] = []

    def fake_add(stream_id: str, bucket: str, name: str, content: str, insert_type: Any, consume: Any) -> None:
        written.append((stream_id, name, content))

    monkeypatch.setattr("plugins.trigger_counter.event_handler.get_stream_info", fake_stream_info)
    monkeypatch.setattr("plugins.trigger_counter.event_handler.add_stream_reminder", fake_add)
    monkeypatch.setattr("plugins.trigger_counter.event_handler.delete_stream_reminder", lambda *a, **kw: None)

    handler = TriggerCounterInjectHandler(plugin)
    await handler.execute("on_prompt_build", {"values": {"stream_id": "s1"}})

    assert written, "应至少写入一条 reminder"
    content = written[0][2]
    assert "全局条目" in content
    assert "其他目标" not in content
