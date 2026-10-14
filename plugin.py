"""trigger_counter 插件入口。

在 prompt_injector 的注入能力之上，增加「值」与「条目状态」两项可追踪数据：

- 条目定义写在配置的 ``[[entries]]`` 中，可自由增加数量
- 命中白名单 / 黑名单的条目按各自的注入位置写入 SystemReminder，实现流隔离注入
- 回复结束后由可自选的判定模型根据条目描述、当前状态与回复内容，
  对值执行增加 / 减少 / 替换，并记录更新后的条目状态
"""

from __future__ import annotations

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin

from .commands.entry_command import EntryCommand
from .config import TriggerCounterConfig
from .event_handler import (
    REMINDER_NAMES,
    TriggerCounterInjectHandler,
    TriggerCounterReviewHandler,
)
from .service import TriggerCounterService

logger = get_logger("trigger_counter")


@register_plugin
class TriggerCounterPlugin(BasePlugin):
    """条目注入与值追踪插件。"""

    plugin_name: str = "trigger_counter"
    plugin_description: str = (
        "自定义条目注入插件：支持启用开关、白名单 / 黑名单、注入位置、"
        "每条目独立监听目标，并在回复结束后由判定模型更新条目的值与状态"
    )
    plugin_version: str = "1.2.2"

    configs = [TriggerCounterConfig]

    def get_components(self) -> list[type]:
        """返回插件包含的组件。

        Returns:
            组件类列表；插件总开关关闭时返回空列表。
        """
        config = self.config
        if isinstance(config, TriggerCounterConfig) and not config.plugin.enabled:
            return []
        return [
            TriggerCounterService,
            TriggerCounterInjectHandler,
            TriggerCounterReviewHandler,
            EntryCommand,
        ]

    async def on_plugin_unloaded(self) -> None:
        """插件卸载时清理全局命名空间下写入的 reminder，避免残留。

        流私有的 reminder 会在聊天流销毁时由框架回收，这里只做全局兜底清理。
        """
        try:
            from src.core.prompt import SystemReminderBucket, get_system_reminder_store

            store = get_system_reminder_store()
            for reminder_name in REMINDER_NAMES.values():
                store.delete(SystemReminderBucket.ACTOR, reminder_name)
        except Exception as exc:
            logger.debug(f"trigger_counter: 卸载清理 reminder 时出错（忽略）: {exc}")
