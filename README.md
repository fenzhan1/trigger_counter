# trigger_counter（条目注入与值追踪）

自定义条目注入插件：支持启用开关、白名单 / 黑名单、注入位置、每条目独立监听目标，并在回复结束后由判定模型更新条目的值与状态。

## 安装

将 `trigger_counter-1.2.0.mfp` 放入机器人的 `plugins/` 目录，然后执行 `/reload trigger_counter`，或把 `trigger_counter/` 源码目录放入 `plugins/` 后重启。

## 目录说明

- `trigger_counter/`：插件源码（与 plugins/ 目录布局一致）
- `test/`：插件核心逻辑测试
- `trigger_counter-1.2.0.mfp` / `.zip`：发布包（内容相同，任选其一）

## 配置

首次加载自动生成 `config/plugins/trigger_counter/config.toml`。条目通过 `[[entries]]` 数组表定义，每个条目可独立设置名称、描述、白名单 / 黑名单、注入位置、监听目标、初始值、上下限与单轮最大变化。

## 测试

```bash
python -m pytest test/plugins/trigger_counter -p no:randomly -q -o addopts=""
```

## 许可证

AGPL-3.0。
