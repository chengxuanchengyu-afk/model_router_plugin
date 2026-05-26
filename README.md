# 模型路由插件

这个插件用于按 QQ 群号或私聊 QQ 号为 MaiBot 的 Planner / Replyer 设置模型路由。

## 功能

- 每个 `[[routes]]` 是一条完整路由。
- Planner 和 Replyer 放在同一条路由里，不拆成两个配置区。
- WebUI 中同一条路由会显示：
  - 启用
  - 路由名称
  - 会话类型
  - QQ号或QQ群号
  - Planner 模型
  - Replyer 模型
- `planner_models` / `replyer_models` 里直接填写模型名称即可，不需要手写完整规则字符串。
- 模型参数仍然全部来自 `model_config`，插件不配置 API Key、base_url、temperature 等参数。
- 某个阶段不需要路由时，把对应模型列表留空 `[]`。
- 提供中文日志开关，便于确认路由是否命中。

## 配置格式

```toml
[plugin]
enabled = false
config_version = "1.0.0"

[logging]
enabled = true

[[routes]]
enabled = true
name = "示例群聊路由"
target_type = "group"
target_id = "123456789"
planner_models = ["模型2", "模型3"]
replyer_models = ["模型2", "模型3"]

[[routes]]
enabled = true
name = "示例私聊路由"
target_type = "private"
target_id = "987654321"
planner_models = ["模型C"]
replyer_models = ["模型D", "模型E"]
```

##已知问题：planner在填写模型后，有无法进行正确路由的问题，正在检查问题

## 字段说明

### `enabled`

是否启用这一条路由。

### `name`

路由名称，只用于日志显示，不影响匹配。

### `target_type`

会话类型：

- `group`：QQ群聊，`target_id` 填 QQ群号。
- `private`：QQ私聊，`target_id` 填对方 QQ 号。

### `target_id`

QQ 群号或私聊 QQ 号。

### `planner_models`

Planner 使用的模型名称列表，直接填写 `model_config` 中已有模型名。

示例：

```toml
planner_models = ["模型2", "模型3"]
```

### `replyer_models`

Replyer 使用的模型名称列表，直接填写 `model_config` 中已有模型名。

示例：

```toml
replyer_models = ["模型D", "模型E"]
```

## WebUI 显示方式

插件 Schema 已改为数组表结构：`routes` 是一个路由列表，每个路由卡片里都有 Planner 和 Replyer 模型列表输入框。

这样用户不需要写：

```text
123456789|planner=model-b|replyer=model-c
```

而是直接在同一条路由里分别填写：

```toml
target_id = "123456789"
planner_models = ["模型2", "模型3"]
replyer_models = ["模型2", "模型3"]
```

## 兼容说明

插件仍兼容之前的两种旧配置：

1. 字符串列表格式：

```toml
[routes]
group_routes = [
  "123456789|planner=model-b|replyer=model-c",
]
```

2. Planner / Replyer 分开配置格式：

```toml
[planner]
group_routes = ["123456789:model-b"]

[replyer]
group_routes = ["123456789:model-c"]
```

加载后会归一化为新的 `[[routes]]` 格式。

## 当前 MaiBot 本体 Hook 兼容说明

插件会返回以下兼容字段：

- `model_name`
- `requested_model_name`
- `model_list`
- `fallback_task_name`
- `fallback_model_name`
- `model_router_models`

当前 MaiBot 本体中：

1. `maisaka.replyer.before_request` 已原生消费 `model_name`，Replyer 可以切换到路由模型列表中的第一个模型。
2. `maisaka.planner.before_request` 当前主要消费 `messages` 和 `tool_definitions`，部分版本不会直接消费 `model_name` / `model_list`。
3. 为了在不修改 MaiBot 本体的前提下让 Planner 也能稳定路由，插件在加载时会安装运行时兼容层，在 LLMService / ChatLoop / LLMOrchestrator 调用链上补充模型选择逻辑。
4. 当前显式指定单个 `model_name` 时通常只尝试该模型，不会自动继续尝试插件列表中的后续模型。

因此，命中路由后：

- Replyer 优先通过原生 Hook 字段生效。
- Planner 会通过插件的运行时兼容层在最终模型请求前强制使用路由模型。
- 模型的 API Provider、base_url、API Key、temperature、max_tokens 等参数仍然完全来自 MaiBot 的 `model_config`。

## 与原插件的关系

本插件的功能方向受到 A_Dawn 开发的早期 MaiBot 模型路由插件启发，在此表示感谢。

A_Dawn 的插件为 MaiBot 早期版本提供了按会话切换模型的思路，但由于 MaiBot 当前版本的插件系统、Hook 机制以及 Planner / Replyer 调用链已经发生变化，旧插件已无法直接适配当前版本，且目前不再维护。

因此，本插件是在当前 MaiBot 版本基础上重新开发的独立实现，目标是在不修改 MaiBot 本体的前提下，实现按 QQ 群号或私聊 QQ 号为 Planner / Replyer 指定模型。插件只负责模型名称路由，不额外保存 API Key、base_url、temperature 等模型参数，这些配置仍由 MaiBot 的 `model_config` 统一管理。

本插件不是原插件的官方续作，也不是对原插件的直接复制或维护分支。如 A_Dawn 后续恢复维护或发布新版模型路由插件，用户可以根据自身需求自行选择使用。

## 致谢

感谢 A_Dawn 对 MaiBot 模型路由能力的早期探索与贡献。这个插件的开发思路受到了其早期工作的启发。

如果本项目中存在表述不当、遗漏致谢或其他不合适的地方，欢迎通过 issue 或 pull request 提出修正。
