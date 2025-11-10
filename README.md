# Maizone (QZone) 插件 — Nerko Agent 版本

Maizone 是一个为 Nerko Agent 设计的 QQ 空间自动化插件，支持自动发说说、读取好友动态、点赞评论以及定时运营。该版本基于 Nerko Agent 插件开发框架重构，兼容原 MaiBot 插件的核心能力，并提供 Nerko Agent 原生的沙箱能力与模型调用方式。

## 功能概览

- **发送说说**：通过沙箱工具或流程自动撰写并发布说说，可选择附带 AI 图片或表情包。
- **读取说说**：读取指定 QQ 号的最近动态，并根据配置执行点赞、评论等互动。
- **自动监控**：定时检查好友动态，支持自动评论、回复与点赞。
- **定时发送**：按照设定的时间表生成并发布说说，支持随机波动与固定主题。
- **AI 能力集成**：接入 Nerko Agent 模型路由，可结合模型组配置文本与图片生成。

## 环境要求

- 已安装并运行的 Nerko Agent（>= 0.8 版本，支持插件系统）。
- 可访问 QQ 空间的 Napcat HTTP 服务（或已获取的持久 Cookie）。
- 如需 AI 生图，请准备 SiliconFlow 或 ModelScope 的 API Key。

## 安装与启用

1. 将插件代码放置在 Nerko Agent 的插件目录中：
   ```bash
   cd /path/to/nerko-agent/plugins
   git clone https://github.com/internetsb/Maizone.git nerkoqzone
   ```
   目录名可自定义，但需保证 `plugin.py` 位于插件根目录。

2. 重新启动 Nerko Agent。启动日志应显示注册了 `Maizone (QZone)` 插件。

3. 在 Nerko Agent 控制台或 Web 配置界面中启用插件，并打开配置面板。Nerko Agent 的插件管理与配置界面可参考官方文档：
   - [插件介绍](https://doc.nekro.ai/docs/04_plugin_dev/00_introduction.html)
   - [插件快速上手](https://doc.nekro.ai/docs/04_plugin_dev/01_quick_start.html)

4. 首次启用后，进入插件配置页填写 Napcat Host、Port 以及 Napcat Token 等必要信息。

## 配置说明

所有配置均通过 Nerko Agent 的插件配置 UI（或 `config.json` 导出文件）管理。主要字段如下：

### Napcat / QZone 访问

| 字段 | 说明 |
| ---- | ---- |
| `plugin_http_host` | Napcat HTTP 服务 Host（默认 `127.0.0.1`）。 |
| `plugin_http_port` | Napcat HTTP 服务端口（默认 `9999`）。 |
| `plugin_napcat_token` | 若 Napcat 启用了 Token 验证，请填写对应口令。 |

当 Napcat 无法访问时，可在插件目录下的 `cookies-*.json` 填入手工抓取的 Cookie，或在 UI 中关闭自动刷新，由插件读取本地 Cookie。

### 模型与 AI 设置

| 字段 | 说明 |
| ---- | ---- |
| `TEXT_MODEL_GROUP` | Nerko Agent 模型组（chat 类型）。会用于列出可选的文本模型。 |
| `models_text_model` | 具体的文本模型名称，默认 `replyer`。 |
| `models_api_key` | 外部服务（如 SiliconFlow）所需的 API Key。 |
| `models_show_prompt` | 是否在日志中输出生成提示词。 |
| `models_image_provider` | 生图服务提供商（`SiliconFlow` 或 `ModelScope`）。 |
| `models_image_model` | 生图模型名称。 |
| `models_image_ref` | 是否附带参考图（将 `images/done_ref.xxx` 作为参考）。 |

> **提示**：插件会优先通过 `TEXT_MODEL_GROUP` 获取 Nerko Agent 的模型列表。如未配置模型组，将回退到 `models_text_model` 提供的名称。

### 监控与定时任务

| 字段 | 说明 |
| ---- | ---- |
| `monitor_enable_auto_monitor` | 启用自动监控说说。 |
| `monitor_enable_auto_reply` | 是否自动回复自己说说下的评论。 |
| `monitor_interval_minutes` | 监控轮询间隔（分钟）。 |
| `monitor_self_readnum` | 自身说说读取条数，用于判断是否有新评论。 |
| `schedule_enable_schedule` | 启用定时发说说。 |
| `schedule_schedule_times` | 每日计划发送时间（HH:MM）。 |
| `schedule_fluctuation_minutes` | 随机波动范围（分钟）。 |
| `schedule_random_topic` | 定时发送时是否随机主题。 |
| `schedule_fixed_topics` | 备用主题列表。 |

其他历史配置（权限名单、阅读数量、概率设置等）保持与旧版一致，仅字段命名由 `section.field` 自动映射到 Nerko Agent 配置。

## 在 Agent 中使用

### 沙箱工具

插件提供以下沙箱工具/测试方法，可在对话流程、自动化任务或手动调试中调用：

- `发送说说`（TOOL）：输入文本后，插件会根据配置自动选择图片并发布说说。
- `读取说说`（TOOL）：输入目标 QQ 号和读取条数，返回最近动态列表。
- `测试Napcat连接`（TEST）：刷新 Napcat Cookie 并尝试创建 QzoneAPI，用于验证 Napcat Host/Port/Token 是否可用。
- `测试模型调用`（TEST）：调用 Nerko Agent 中配置的文本模型，返回简短生成结果，便于检查模型组/模型名称是否正确。

这些方法的签名符合 Nerko Agent 的 `SandboxMethodType.TOOL/TEST` 规范，可在工作流或测试面板中直接引用。
插件提供两个沙箱工具，可在对话流程、自动化任务或手动测试中调用：

- `发送说说`：输入文本后，插件会根据配置自动选择图片并发布说说。
- `读取说说`：输入目标 QQ 号和读取条数，返回最近动态列表。

这些方法的签名符合 Nerko Agent 的 `SandboxMethodType.TOOL` 规范，可在工作流中直接引用。

### 自动任务

插件在初始化时根据配置自动启动两个后台任务：

1. **FeedMonitor**：周期性调用 `monitor_read_feed`，自动处理点赞、评论以及回复逻辑。
2. **ScheduleSender**：根据时间表自动生成并发布说说。

在禁用插件或关闭对应配置后，后台任务会自动停止。

## 调试与 `/exec` 用法

Nerko Agent 在聊天窗口或控制台中提供了 `/exec` 命令，便于直接触发插件暴露的沙箱方法。Maizone 插件注册了 `发送说说`、`读取说说` 两个工具方法以及 `测试Napcat连接`、`测试模型调用` 两个测试方法，可以按照以下格式调试：

```text
/exec plugin="Maizone (QZone)" method="发送说说" args='{"message": "今天也要记得打卡！"}'
```

- `plugin`：填写插件的展示名称 `Maizone (QZone)`（或插件管理页面中显示的名称）。
- `method`：使用沙箱工具名称（如 `发送说说` 或 `读取说说`）。
- `args`：JSON 格式传参，对应工具函数的参数。示例中 `message` 会传递给 `send_feed_tool`。

调试读取接口时，可以这样调用：

```text
/exec plugin="Maizone (QZone)" method="读取说说" args='{"target_qq": "123456", "num": 3}'
```

> 提示：在 `/exec` 命令中，JSON 字符串需要使用单引号包裹，避免与聊天窗口的双引号冲突。如需查看插件日志，可同时在 Agent 的控制台查看 `Maizone` 相关输出，便于定位请求或配置问题。

## AI 工作流说明

- 文本生成：插件通过 Nerko Agent 的沙箱模型接口调用所选模型。若模型组为空或模型名称无效，将记录错误日志并跳过该步骤。
- 图片生成：启用 AI 配图后，插件会根据 `models_image_provider` 调用对应 API，并支持参考图能力。失败时会回退到表情包模式。
- 所有模型调用均运行在沙箱内，遵循 Nerko Agent 的上下文与审计机制，详见 [系统 API 参考](https://doc.nekro.ai/docs/04_plugin_dev/04_system_api_reference.html)。

## 常见问题

- **无法刷新 Cookie**：确认 Napcat HTTP 端口可访问、Token 正确且 QQ 客户端在线。必要时将 `cookies-*.json` 替换为人工抓取的 Cookie。
- **模型调用失败**：检查是否在 Nerko Agent 中配置了对应模型组/模型，并确认已为外部服务填入 API Key。
- **AI 生图失败**：确认所选模型支持当前请求（例如 `Kwai-Kolors/Kolors` 支持多图），并确保图片目录具有写入权限。

## 致谢

- 原 MaiBot 插件作者及贡献者。
- [qzone-toolkit](https://github.com/gfhdhytghd/qzone-toolkit) 项目提供的部分 API 实现。

如有问题或建议，欢迎通过 Issue 提交反馈。
