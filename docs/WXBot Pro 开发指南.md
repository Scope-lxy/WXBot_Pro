# WXBot Pro 开发指南

这份文档面向本地维护者和后续 AI Agent：只记录“代码现在怎么跑、哪些边界不能乱动、改完怎么验证”。面板操作请看 `docs/WXBot Pro 使用说明.md`。

## 文档分工

- `README.md`：项目总览、启动方式、目录边界和交付方式。
- `docs/WXBot Pro 使用说明.md`：面板怎么配、功能怎么用、常见问题。
- `docs/WXBot Pro 开发指南.md`：代码结构、运行链路、数据真源和改动边界。
- `docs/WXBot Pro 设计规范.md`：面板 UI 排版规范。
- `docs/微信 UI 并发实测结果.md`：微信主窗口 / 子窗口并发实测结果。
- `docs/wechat-cli 本地数据集成探测.md`：本地只读数据源的安装、账号校验和集成边界。
- `docs/人设编写规范模板.md`：基础人设和人设近况模板。
- `docs/伪BUG记录.md`：已接受机制与排查时不要重复误报的点。
- `AGENTS.md`：AI 协作规则、Windows 编码注意事项和项目红线。

## 维护原则

- 本项目是个人自用 fork，优先当前实际体验和代码可维护性，不为旧任务、旧实例、旧记录额外保留兼容层。
- 能收敛成单一真源时，不并行维护两套字段、两套流程或两套 UI 心智。
- 不提交本地私密配置和账号数据，尤其是 `data/config/`、`data/prompt/`、`data/accounts/`。
- 高风险链路改动优先补测试：登录 / session、消息路由、图片链路、发送清洗、会话记忆、管理员接管、任务调度、素材转发和发圈。
- 模块拆分只整理本项目业务层，不顺手改 `wxautox4`、wxauto 消息类、监听实现、下载实现或微信窗口控制底层。
- Windows PowerShell 看到中文乱码时，先按终端显示问题处理，不直接重写中文内容。

## 代码结构

- `打开软件.bat`：启动入口。创建或复用 Python 3.12 `venv`，安装依赖后运行 `web_server.py`。
- `web_server.py`：Flask 面板和管理 API，负责登录、配置读写、接口测试、备份、页面 API，以及启动 / 停止机器人。
- `templates/dashboard.html`：面板主体。
- `wxbot_core.py`：机器人运行时总编排，负责 wxautox4、微信监听、AI 回复、统一任务扫描和真实微信发送。
- `core/`：底座能力，例如配置、Prompt、媒体、记忆、发送、通讯录身份校准和调度。
- `feature/`：业务规则，例如监听维护、管理员工作台、素材转发、定时任务和发圈任务。
- `extension/`：外部增强，例如邮件通知、Webhook、SiverPanel 远程访问。
- `tools/`：本地复现脚本、备份脚本和专项测试辅助，只放临时、测试性质工具。
- `tests/`：行为保护测试。

## 运行链路

### 启动层

```text
打开软件.bat
-> 创建 / 复用 venv
-> 安装脚本内置依赖清单
-> 运行 web_server.py
-> 面板优先开放在 http://127.0.0.1:10001（被占用时顺延）
```

`打开软件.bat` 自己维护 Python 依赖清单，不读取外部 `requirements.txt`。它会兜底安装 `Pillow`，并在系统缺少 `ffmpeg` / `ffprobe` 时下载到 `venv/tools/ffmpeg/`。

`wechat-cli` 是项目级外部工具，但默认禁用。只有 `config.json` 里 `wechat_cli_enabled=true` 且未设置 `WXBOT_DISABLE_WECHAT_CLI=1` 时，启动脚本才会准备 `venv/tools/wechat-cli/`、创建隔离 `pyenv` 并检测初始化。多账号候选不会自动猜测绑定。

### 面板层

`web_server.py` 负责：

- 登录态、session、首页和日志拉取。
- `config.json`、`admin.json`、`email.json`、`webhook.json` 等配置读写。
- 接口连通性测试和视觉能力测试，并写入 `api_capability_map`。
- Prompt、人设近况、Prompt 预览、会话记忆、聊天记录、通讯录、素材转发、发圈任务、备份等页面 API。
- 统一任务工作台 API：`/api/task-workbench/<module>` 与 `/api/task-workbench/<module>/runtime`。
- 启动 / 停止机器人：`/start_bot` 后台创建 `WXBot` 并调用 `run()`；`/get_startup_status` 轮询真实启动结果；`/stop_bot` 等待线程退出。
- `wechat-cli` 状态卡：关闭态所有检测、更新和读取入口都短路为 `disabled`；开启前必须弹出风控确认；`检查更新` 只提示 Git HEAD，不自动更新。

### 机器人层

`WXBot.__init__()` 初始化配置、聊天接口主备状态、回复轮数存储、Prompt 系统、素材转发、发圈草稿、监听缓存、消息合并缓存和发送锁。

真正接入微信发生在 `init_wx_listeners()`：创建 `WeChat` 对象、读取当前 `wx_id`、初始化账号隔离数据目录、启动监听器，并注册管理员、白名单、群聊、自定义转发来源和素材来源监听。

`WXBot.main()` 每轮负责微信在线 / 窗口状态检测、新好友检测、全局监听刷新、运行中任务热更新、统一时间任务扫描、AI 自动转发待发送队列、管理员发圈草稿预览和通讯录自动维护检查。

时间任务统一由 `core/scheduled_tasks.py` + `wxbot_core.py` 主循环扫描，不再依赖多套 `schedule.run_pending()`。

## 消息处理链路

```text
微信监听回调
-> 基础预处理（消息属性、媒体、去重、语音转文字）
-> 全局监听按需复用 / 轻量补一次动态子窗口
-> message_routing 第一轮入口分流
-> 管理员发圈输入 / 素材来源投喂 / 自定义转发人工接管
-> 普通消息处理
-> 私聊好友消息先写聊天记录，再进入连续消息合并队列
-> AI 回复前按需补齐最近上下文，再读取 history 组装 Prompt
-> 回复预处理（清洗、准入、必要时重写 / 兜底）
-> 拆分多条、人工延迟
-> SendMsg / SendFiles / message.forward(...)
-> 成功发送后写入输出记录
-> 按需更新会话记忆
```

去重口径按“同一会话 + 同一发送者 + 同一类型 + 同一内容”的短时间重复回调处理，不按回调来源区分放行。

私聊 `self` 消息会先排除机器人自己回复的回显；确认是手动回复后，写入本地聊天记录、推进该好友消息序号、清理旧 AI 回复，不主动触发 AI。下一条好友消息会重新进入 AI 流程，并在 history 中带上这条手动回复。

机器人自己发出的私聊消息用 outbound echo 账本过滤微信 `self` 回调。覆盖入口包括 AI 文本 / 语音回复、轻量延后发送队列、运行时发送、关键词回复、定时消息、自定义转发、普通素材转发和 AI 自动素材转发。通讯录维护、关系扫描和标签同步等低优先级 UI 任务启动前要避让未消费 echo。

## 运行中同步

`/save_config` 在机器人运行中做两类同步：

- API 相关字段变更时，调用 `apply_runtime_api_config_update()`，刷新聊天接口实例，并把聊天主备状态重置回主接口。
- 定时消息、素材转发、发圈任务等任务配置变更时，调用 `request_runtime_task_reload()`，由主循环在安全时机重新载入后续任务表。

已经开始执行的一轮不会被强行打断；这不是热重启整个机器人。

## Prompt 规则

- 普通私聊回复、群聊回复、图片最终回复、素材转发判定、素材附加文案、管理员发圈文案生成和面板 Prompt 预览，优先走 `PromptSystem`。
- 辅助视觉分析、结构化视觉笔记、会话记忆提取 / 修复这类固定协议型 Prompt 独立维护。
- 不要在 `wxbot_core.py` 或 `web_server.py` 里平行拼 `base_prompt / persona_status_block`。

## 模块边界

落位原则：

- 去掉“这是微信机器人”前提后仍成立的能力，优先放 `core/`。
- 明显依赖当前业务规则和流程判断的能力，优先放 `feature/`。
- 面向外部系统的接入和增强，放 `extension/`。

仍应留在 `wxbot_core.py`：

- wxautox4 对象创建和窗口控制。
- 监听注册、主循环、线程编排。
- 真实 `SendMsg`、`SendFiles`、`message.forward(...)`、`new.accept(...)`、朋友圈打开 / 发布等微信动作。
- 聊天主备切换、发送锁、主循环健康检查、运行中任务热更新入口。

## 数据真源

- `data/config/config.json`：主配置。
- `data/config/admin.json`：面板账号密码。
- `data/config/email.json`：邮件通知配置。
- `data/config/webhook.json`：Webhook 配置。
- `data/config/reply_count.json`：私聊回复轮数限制计数。
- `data/config/runtime_metrics_v1.json`：状态面板、数据图表和管理员 `/状态` 使用的小时级运行统计。
- `data/config/wechat_cli_account_bindings.json`：wxautox4 账号命名空间到 `wechat-cli` 数据库目录的活体校验绑定。
- `data/prompt/`：人格模板和人格近况文件。
- `data/system_prompts/`：系统 Prompt 片段及其备份。
- `data/accounts/<wx_id>/memory/`：聊天记录。
- `data/accounts/<wx_id>/chat_memory/`：会话记忆 JSON 真源。
- `data/accounts/<wx_id>/contact_profiles/contacts.json`：通讯录档案真源，使用 v2 瘦身结构。
- `data/accounts/<wx_id>/contact_merge_backups/`：联系人合并前的账号级保险备份。
- `data/accounts/<wx_id>/tasks/`：关键词、自定义转发、定时消息、素材转发、发圈等任务模块。
- `data/accounts/<wx_id>/relationship_scan/relationships.json`：关系扫描结果。
- `data/accounts/<wx_id>/friend_request/state.json`：好友申请设置、候选人和执行记录。
- `data/accounts/<wx_id>/config/voice_reply_state.json`：语音回复运行态。
- `data/accounts/<wx_id>/moments_drafts/active_draft.json`：管理员发圈草稿运行态。
- `data/accounts/default/`：只有没有运行中微信号、没有 `last_wx_id`、也没有历史账号数据时才使用。
- `wxauto_save/`：微信下载原件和 AI 图片压缩副本缓存，不是长期数据真源。
- `wxbot_logs/`：面板运行日志。
- `backups/data_时间戳/`：面板一键备份产物。

通讯录 v2 只落盘联系人真源字段：`contact_key`、`wechat_id`、`wxid`、`nickname`、`remark`、`region`、`source`、`added_at`、`signature`、`tags`、`status`、`warnings` 和维护状态字段。不要把 `display_name`、`send_name`、`send_target`、`name` 当作联系人档案字段写进 `contacts.json`。

通讯录名称分三层：

- 档案层：只保存微信资料和本地维护事实。
- 接口 / 调用层：通过 `core.contact_profiles.contact_public_view()`、`contact_send_target()`、`contact_display_label()` 从档案层临时派生 `name` / `send_target`。
- 运行记录层：素材转发、定时任务、AI 前言队列等历史证据可以保存当时的 `send_name` / `display_name`，但这些字段不能反向污染通讯录档案。

如果传入对象同时包含事实字段和临时字段，通讯录派生函数必须优先使用事实字段。保存入口 `save_directory()` 会再次归一化，防止展示名、发送目标等临时字段回流。

## 关键行为边界

### 微信 UI 锁

微信 UI 锁保护一整轮真实微信动作，不只是主窗口切换。即使已有缓存子窗口，`SendMsg`、`SendFiles`、`SendAudio`、群聊 `quote`、素材源历史读取、备注 / 标签编辑仍可能改变微信焦点或可见列表。

运行时任务发送、轻量延后发送队列、私聊 / 群聊自动回复、管理员回复、素材源读取和联系人资料编辑必须拿到全局 UI 锁后连续完成。拿不到锁时，按各自策略排队、延后或跳过。

### 动态监听

动态监听采用轻量按需补窗。普通增删监听不触发微信客户端重绑，不恢复主循环高频巡检。普通补窗失败先进入 30s / 60s 延后重试，之后在 600s 待处理窗口内继续轻量重试；同一好友已有延后任务时只合并消息，不重复补窗。只有“已监听但无子窗口”残留状态允许受控关闭重建。

固定监听可低频巡检补回，但不能和全局监听延后补窗抢微信 UI。

### 上下文补洞

会话记忆页只暴露两个开关：

- `memory_context_repair_low_risk_switch`：显示为“自动补最近上下文”，默认开，统一控制 CLI 补洞和低风险 UI 当前渲染消息兜底。
- `memory_context_repair_high_risk_switch`：显示为“补上下文时允许滚动微信窗口”，默认关，只控制高风险滚动 UI 补洞。

不要把冷却、锚点数量、读取上限等内部参数重新暴露到 UI。

CLI 补洞必须通过账号绑定校验。真实 `wxid_...` 可按目录匹配；`scope_*` 命名空间必须经文件传输助手活体校验并保存绑定。私聊同名和群聊重名都必须用最近锚点消息消歧，不能猜 wxid 或 `@chatroom`。

补洞写入前过滤未识别语音占位，并按同方向 / 类型 / 内容的近时间重复去重。私聊无锚点时可按最近可见消息补入；群聊没有锚点、找不到 CLI 目标或目标不唯一时跳过补入，避免污染上下文。

### 通讯录与身份

通讯录自动维护的完整资料采集必须走 `feature/contact_auto_collector_worker.py` 最小子进程。主进程负责微信 UI 锁、300s 硬超时、PID 级 kill / `taskkill /T /F` 兜底和 `SwitchToChat` 恢复；不要把 `GetFriendDetails` 搬回主进程。

身份校准跟随通讯录维护、手动建档和备注修复成功触发，不在每条消息热路径刷新通讯录或扫描目录。聊天记录、会话记忆、关系扫描、任务引用和回复计数的改名 / 合并都通过账号级联系人合并链路处理，合并前写入 `contact_merge_backups/`。

身份判定不做模糊匹配，不维护历史别名：同 wxid、唯一备注、无备注时 `nickname + source + added_at` 唯一且只变 wxid 才自动合并，其余有意义冲突进入等待校准。

### 图片、语音与历史渲染

图片回复先生成结构化视觉笔记，再按最终回复接口是否支持视觉选择直接传图或视觉笔记转述。`image_parse.md` 只放图片处理规则，不塞具体图片解析结果。

AI 可见 history 统一走 `core/chat_history_format.py::build_model_visible_history(...)`。本地绝对路径不能喂给模型；图片使用 `[图片]` 加视觉笔记，语音只传转写正文，链接、小程序、视频、名片保留必要类型提示。

私聊图片进入连续消息合并队列，最多 `9` 张。群聊图片本身只缓存到群级 pending visual context，后续文本触发 AI 且命中问图意图时才消费。

语音消息依赖微信自己的转文字结果，优先调用 wxauto 官方 `msg.to_text()`。私聊只有时长、暂无文字时，每 5 秒对原语音对象重试一次，最多 3 次；群聊不做延后重试。未识别占位、`voicemsg` XML 和失败状态不进入 AI / history，也不发送兜底提示。

### 发送、预处理与统计

AI 回复发送前统一走回复预处理：先按 `clean_ai_reply_switch` 清洗，再用 `evaluate_reply_preprocess_admission()` 做准入。不从脏输出里提取“最终回复”；不合格时只允许重写一次，重写仍不合格才走异常兜底或静默。

`api_error_reply`、回复预处理异常兜底和轮数超限固定回复的“只回复一次”状态都复用 `ReplyCountStore` 的回复循环窗口。新增同类提醒时，不另起重置时间或独立状态文件。

运行统计口径：`全部API调用` 统计机器人向 AI 大模型和正式 TTS 模型发出的真实请求；`聊天API调用` 统计为了回复当前聊天而发起的聊天模型请求，不包含素材判定、素材附加文案和面板测试 / 预览。

日志按用户可处理程度分层：正常流程用 `INFO`，明确成功用 `SUCCESS`，局部失败但可继续运行用 `WARNING`，监听器、微信客户端、主线程或核心初始化受损用 `ERROR`。日志降噪只处理预期内、已兜住、会高频重复的事件；真实故障必须逐次保留。

### 任务与素材

定时消息、素材转发、发圈任务、朋友圈点赞都走统一时间模型。新增任务类面板时优先复用现有任务工作台 contract / storage / service。

定时消息内容模型是可选 `1` 条文案 + 最多 `9` 个本地文件，发送顺序固定为先文案、后文件。

素材转发里的 AI 文案已经统一成同一层。`AI自动转发` 仍是两段式链路：先判断当前最适合发哪条素材，再生成最终附加文案；不要把判断和文案生成混成一次模型调用。

发圈文案生成有图片时统一走 `api.chat(..., image_path/image_paths=...)` 图片直传，不走辅助视觉转述路径；管理员 `/发圈` 和面板发圈任务不要分叉维护专用多图 HTTP 请求。

## 高风险修改点

- 登录 / session。
- 微信监听注册、动态子窗口缓存和全局监听按需补窗。
- 消息回调分流顺序。
- 管理员接管态、发圈态和素材来源静默规则优先级。
- 发送前清洗、拆分多条和延迟策略。
- outbound echo 过滤和私聊 `self` 手动接管判定。
- 回复轮数计数和超限结束语。
- 会话记忆提取、提案合并和保护规则。
- 私聊上下文补洞、history 组装顺序和微信 UI 锁占用。
- 定时任务 `next_fire_at` 推进、任务热更新和状态回写。
- 素材转发记录、进度记录和 AI pending 队列状态。
- 通讯录档案、身份校准、备注修复和手动目标名解析。

## 验证命令

```powershell
$files = @('wxbot_core.py','web_server.py') + (Get-ChildItem .\core\*.py | ForEach-Object { $_.FullName }) + (Get-ChildItem .\feature\*.py | ForEach-Object { $_.FullName }) + (Get-ChildItem .\extension\*.py | ForEach-Object { $_.FullName }); py -m py_compile @files
venv\Scripts\python.exe -m unittest discover tests
git diff --check
```

提交前补充检查：

```powershell
git status --short
git diff --cached --name-only
```

## 文档同步要求

- 面板新增 / 删除功能卡片、接口或运行入口：更新 `README.md`。
- 代码结构、模块职责、运行链路或打包边界变化：更新 `docs/WXBot Pro 开发指南.md`。
- 对外完整使用说明变化：更新 `docs/WXBot Pro 使用说明.md`。
- AI 协作边界、风险点、推荐检查命令变化：更新 `AGENTS.md`。

文档写“现在是什么”，不写阶段日志。
