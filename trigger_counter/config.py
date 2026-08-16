"""trigger_counter 插件配置。

配置文件默认路径：config/plugins/trigger_counter/config.toml

条目定义（名称 / 描述 / 注入范围 / 注入位置 / 初始值 / 初始状态）保存在本配置
文件的 ``[[entries]]`` 数组节中，可自由增加任意数量的条目。

运行时数据（当前值、当前条目状态）属于可变数据，保存在
``data/json_storage/trigger_counter/records.json``，不会写回 TOML。
"""

from __future__ import annotations

from typing import ClassVar, Literal

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class EntrySection(SectionBase):
    """单个条目定义。

    **作用域匹配（聊天流隔离）：**
    - include（白名单）/ exclude（黑名单）均为空 -> 全局生效
    - include 非空 -> 至少命中一条才生效
    - exclude 非空 -> 命中任意一条即排除

    **include / exclude 格式：**
    - ``"group:*"``   全部群聊
    - ``"group:123"`` 群号为 123 的群聊
    - ``"user:*"``    全部私聊
    - ``"user:456"``  QQ 号为 456 的私聊

    **注入位置（insert_type）：**
    - ``"dynamic"`` 注入到最新 user 消息开头，并自动剥离上一轮旧注入
    - ``"fixed"``   注入到第一条 user 消息开头，不会被剥离

    **值与状态：**
    - ``值`` 是一个数字，回复结束后由判定模型决定增加 / 减少 / 替换
    - ``条目状态`` 是一段短文本，记录该条目当前的情况，每轮由判定模型更新
    """

    name: str = Field(
        default="示例条目",
        description="条目名称，用于注入提示词与数据记录，必须唯一",
    )
    description: str = Field(
        default="请在这里描述该条目的含义，以及值应该在什么情况下增加或减少",
        description="条目描述，会注入提示词，同时作为回复结束后判定模型的判定依据",
    )
    enabled: bool = Field(
        default=True,
        description="启用条目（关闭后既不注入提示词也不参与判定）",
    )

    include: list[str] = Field(
        default_factory=list,
        description=(
            "白名单，为空时全局生效。\n"
            '格式："group:*" "group:群号" "user:*" "user:QQ号"'
        ),
    )
    exclude: list[str] = Field(
        default_factory=list,
        description=(
            "黑名单，命中则排除，格式同 include。\n"
            '示例：exclude = ["group:123456"]'
        ),
    )

    insert_type: Literal["dynamic", "fixed"] = Field(
        default="dynamic",
        description=(
            "注入位置。\n"
            "dynamic（默认）：注入到最新 user 消息开头，自动剥离历史旧注入。\n"
            "fixed：注入到第一条 user 消息开头，不会被自动剥离。"
        ),
    )
    consume: Literal["forever", "once"] = Field(
        default="forever",
        description=(
            "消费模式。\n"
            "forever（默认）：每次请求都注入。\n"
            "once：仅消费一次，需配合 insert_type=dynamic。"
        ),
    )

    initial_value: float = Field(
        default=0.0,
        description="初始值，仅在该条目第一次出现（记录文件里还没有数据）时写入",
    )
    initial_state: str = Field(
        default="",
        description="初始条目状态文本，留空表示暂无状态",
    )
    value_min: float = Field(
        default=0.0,
        description="值的下限，判定结果会被裁剪到该范围内",
    )
    value_max: float = Field(
        default=1000000.0,
        description="值的上限，判定结果会被裁剪到该范围内",
    )
    max_delta_per_round: float = Field(
        default=10.0,
        ge=0.0,
        description="单轮允许的最大增减幅度，用于防止判定模型给出异常数值",
    )
    review_after_reply: bool = Field(
        default=True,
        description="回复结束后是否让判定模型更新该条目的值与状态",
    )
    listen_target: str = Field(
        default="",
        description=(
            "监听目标（after_llm_request 事件中的 request_name）。"
            "留空时跟随全局默认目标（[target] actor_request_name）。"
            "填写后仅该条目的值与状态在指定目标请求结束后更新。"
        ),
    )


class TriggerCounterConfig(BaseConfig):
    """trigger_counter 插件配置模型。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "条目注入与值追踪插件配置"

    @config_section("plugin", title="插件开关", tag="plugin")
    class PluginSection(SectionBase):
        """插件总开关。"""

        enabled: bool = Field(
            default=True,
            description="启用插件",
        )
        inject_prompt: bool = Field(
            default=True,
            description="是否把条目（名称 / 描述 / 值 / 条目状态）注入提示词",
        )
        review_after_reply: bool = Field(
            default=True,
            description="回复结束后是否调用判定模型更新值与条目状态",
        )
        scope_by_stream: bool = Field(
            default=False,
            description=(
                "值与状态是否按聊天流分别记录。\n"
                "false（默认）：所有聊天共用一份记录。\n"
                "true：每个群 / 每个私聊各自独立记录。"
            ),
        )
        debug_log: bool = Field(
            default=False,
            description="是否以 INFO 级别输出每轮注入内容与判定结果，便于调试",
        )

    @config_section("prompt", title="提示词注入", tag="text")
    class PromptSection(SectionBase):
        """注入文案控制。"""

        block_title: str = Field(
            default="条目状态板",
            description="注入块的标题",
        )
        header_note: str = Field(
            default=(
                "以下条目正在被记录。它们只是背景信息，不要刻意迎合或刻意回避，"
                "也不要在回复里提到本记录的存在。"
            ),
            description="注入块开头的说明文字",
        )
        show_value: bool = Field(
            default=True,
            description="是否展示各条目的当前值",
        )
        show_state: bool = Field(
            default=True,
            description="是否展示各条目的当前状态文本",
        )

    @config_section("judge", title="判定模型", tag="ai")
    class JudgeSection(SectionBase):
        """回复结束后负责更新值与状态的模型。"""

        model_name: str = Field(
            default="",
            description=(
                "判定所用模型名称（config/model.toml 中 [[models]] 的 name）。"
                "留空则回退到任务模型集 fallback_task。"
            ),
            placeholder="例如 qwen3-4b",
        )
        fallback_task: str = Field(
            default="utils_small",
            description="model_name 留空或不可用时使用的任务模型集名称",
        )
        temperature: float = Field(
            default=0.1,
            ge=0.0,
            le=2.0,
            step=0.1,
            description="判定模型温度，建议保持较低以获得稳定结果",
        )
        max_tokens: int = Field(
            default=800,
            ge=64,
            description="判定模型最大输出 token 数",
        )
        include_tool_calls: bool = Field(
            default=True,
            description="是否把本轮的工具调用一并交给判定模型参考",
        )
        min_text_length: int = Field(
            default=2,
            ge=0,
            description="回复文本短于该长度时跳过判定，避免无意义请求",
        )
        state_max_length: int = Field(
            default=120,
            ge=16,
            description="条目状态文本的最大保存长度，超出会被截断",
        )
        request_name: str = Field(
            default="trigger_counter_judge",
            description="判定请求在 LLM 统计里显示的名称",
        )

    @config_section("target", title="监听目标", tag="advanced")
    class TargetSection(SectionBase):
        """决定把哪一次 LLM 请求视为正式回复。"""

        actor_request_name: str = Field(
            default="default_chatter",
            description=(
                "actor 请求名（after_llm_request 事件中的 request_name）。"
                "默认 chatter 为 default_chatter，一般无需修改。"
            ),
        )
        require_stream_id: bool = Field(
            default=True,
            description="是否只处理带聊天流信息的请求，过滤掉后台任务类请求",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    prompt: PromptSection = Field(default_factory=PromptSection)
    judge: JudgeSection = Field(default_factory=JudgeSection)
    target: TargetSection = Field(default_factory=TargetSection)

    entries: list[EntrySection] = Field(
        default_factory=lambda: [
            EntrySection(
                name="好感度",
                description=(
                    "记录你对当前聊天对象的好感程度。对方友善、有趣或给予帮助时增加，"
                    "对方冒犯、敷衍或态度恶劣时减少。条目状态里记录最近一次变化的原因。"
                ),
                initial_value=50.0,
                initial_state="初次接触，印象普通",
                value_min=0.0,
                value_max=100.0,
                max_delta_per_round=5.0,
            ),
            EntrySection(
                name="提到猫的次数",
                description=(
                    "回复中主动谈到猫、猫咪或与猫相关的话题时，值增加对应次数；"
                    "没有提到就保持不变。条目状态里记录最近提到猫的话题。"
                ),
                enabled=False,
                initial_value=0.0,
                value_min=0.0,
                value_max=1000000.0,
                max_delta_per_round=3.0,
            ),
        ],
        description="条目列表（TOML 数组表，格式为 [[entries]]），可添加任意数量的条目。",
    )
