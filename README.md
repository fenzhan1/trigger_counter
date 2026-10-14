# trigger_counter（条目注入与值追踪）

自定义条目注入插件：支持启用开关、白名单 / 黑名单、注入位置、每条目独立监听目标，并在回复结束后由判定模型更新条目的值与状态。

## 版本与核心兼容性

当前版本 **1.2.2**，适配 neo-mofox **1.3.0-alpha.0** 及以上。

核心 1.3.0 把若干插件 API 的主版本号提升了（`stream_api` 1.0.0 → **2.0.0**，
`action_api` / `agent_api` → 2.0.0）。加载器规则为「主版本不一致即拒绝注册」，
因此旧声明会表现为**插件不加载、WebUI 页面打不开**且不报明显错误。

`manifest.json` 中对应声明为：

```json
"api_version": {
    "event_api": "1.0.0",
    "llm_api": "1.0.0",
    "log_api": "1.0.0",
    "prompt_api": "1.0.0",
    "send_api": "1.0.0",
    "storage_api": "1.0.0",
    "stream_api": "2.0.0"
}
```

升级核心后若插件不再加载，请先核对这里的 `stream_api` 是否为 `2.0.0`。

## 安装

将 `trigger_counter-1.2.2.mfp` 放入机器人的 `plugins/` 目录，然后执行 `/reload trigger_counter`，或把 `trigger_counter/` 源码目录放入 `plugins/` 后重启。

## 目录说明

- 根目录：插件源码（与 plugins/ 目录布局一致）
- `test/`：插件核心逻辑测试
- `dist/`：构建产物（`trigger_counter-1.2.2.mfp` / `.zip`）

## 配置

首次加载自动生成 `config/plugins/trigger_counter/config.toml`。条目通过 `[[entries]]` 数组表定义，每个条目可独立设置名称、描述、白名单 / 黑名单、注入位置、监听目标、初始值、上下限与单轮最大变化。

## 测试

```bash
python -m pytest test/plugins/trigger_counter -p no:randomly -q -o addopts=""
```

## 许可证

AGPL-3.0。
