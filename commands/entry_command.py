"""trigger_counter 条目管理命令。

支持的用法（仅主人可用）：

    /条目                         查看所有启用条目的值与状态
    /条目 调整 <名称> <增量>       手动调整某个条目的值（增量可为负）
    /条目 状态 <名称> "<文本>"     手动设置某个条目的状态（含空格时用引号包裹）
    /条目 重置 [名称]              重置为初始值与初始状态，省略名称则重置全部
    /entry ...                    英文别名
"""

from __future__ import annotations

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

from ..service import TriggerCounterService, format_value

logger = get_logger("trigger_counter.command")


class EntryCommand(BaseCommand):
    """条目查询与手动维护命令（仅主人可用）。"""

    name: str = "条目"
    description: str = "查看和维护 trigger_counter 条目的值与状态（仅主人可用）"
    permission_level: PermissionLevel = PermissionLevel.OWNER

    @classmethod
    def match(cls, parts: list[str]) -> int:
        """匹配命令名，同时支持 条目 与 entry 两种触发词。

        Args:
            parts: 命令片段列表。

        Returns:
            匹配长度，不匹配返回 0。
        """
        if not parts:
            return 0
        if parts[0] in ("条目", "entry"):
            return 1
        return 0

    def _service(self) -> TriggerCounterService:
        """构造服务实例。

        Returns:
            TriggerCounterService 实例。
        """
        return TriggerCounterService(self.plugin)

    async def _reply(self, text: str) -> None:
        """向当前聊天流发送文本回复。

        Args:
            text: 要发送的文本内容。
        """
        await send_text(text, stream_id=self.stream_id)

    async def execute(self, message_text: str) -> tuple[bool, str]:
        """执行命令（委托基类完成路由分发）。"""
        return await super().execute(message_text)

    @cmd_route()
    async def handle_list(self) -> tuple[bool, str]:
        """列出所有启用条目的当前值与状态。"""
        service = self._service()
        entries = service.enabled_entries()
        if not entries:
            await self._reply("当前没有启用中的条目。")
            return True, "empty"

        records = await service.load_records(self.stream_id)
        default_target = (service.config().target.actor_request_name or "").strip()
        lines = ["当前条目："]
        for entry in entries:
            entry_name = entry.name.strip()
            record = records.get(entry_name, {})
            value = format_value(float(record.get("value", 0.0)))
            state = str(record.get("state") or "").strip() or "暂无"
            target = (entry.listen_target or "").strip() or default_target
            target_text = target or "全局默认"
            lines.append(
                f"- {entry_name}：值 {value} ｜ 状态 {state} ｜ 监听 {target_text}"
            )

        await self._reply("\n".join(lines))
        return True, "listed"

    @cmd_route("调整")
    async def handle_adjust(
        self, entry_name: str = "", delta: float = 0.0
    ) -> tuple[bool, str]:
        """手动调整某个条目的值。

        Args:
            entry_name: 条目名称。
            delta: 增量，可为负数。
        """
        if not entry_name:
            await self._reply("用法：/条目 调整 <名称> <增量>")
            return False, "missing args"

        result = await self._service().adjust(entry_name, delta, self.stream_id)
        if result is None:
            await self._reply(f"没有找到启用中的条目：{entry_name}")
            return False, "not found"

        await self._reply(f"✓ {entry_name} 的值已调整为 {format_value(result)}。")
        logger.info(f"手动调整条目 {entry_name} 增量 {delta} 结果 {result}")
        return True, "adjusted"

    @cmd_route("状态")
    async def handle_set_state(
        self, entry_name: str = "", state: str = ""
    ) -> tuple[bool, str]:
        """手动设置某个条目的状态文本。

        Args:
            entry_name: 条目名称。
            state: 状态文本，含空格时需要用引号包裹。
        """
        state_text = state.strip()
        if not entry_name or not state_text:
            await self._reply('用法：/条目 状态 <名称> "<状态文本>"')
            return False, "missing args"

        ok = await self._service().set_state(entry_name, state_text, self.stream_id)
        if not ok:
            await self._reply(f"没有找到启用中的条目：{entry_name}")
            return False, "not found"

        await self._reply(f"✓ {entry_name} 的状态已更新。")
        return True, "state updated"

    @cmd_route("重置")
    async def handle_reset(self, entry_name: str = "") -> tuple[bool, str]:
        """重置条目的值与状态为初始设定。

        Args:
            entry_name: 条目名称；留空则重置全部启用条目。
        """
        count = await self._service().reset(entry_name or None, self.stream_id)
        if count == 0:
            await self._reply(f"没有找到启用中的条目：{entry_name}")
            return False, "not found"

        if entry_name:
            await self._reply(f"✓ {entry_name} 已重置为初始值与初始状态。")
        else:
            await self._reply(f"✓ 已重置 {count} 个条目为初始值与初始状态。")
        return True, f"reset {count}"
