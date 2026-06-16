# WXBot Pro 开发维护说明

本文档给本地维护者和后续 AI Agent 使用，目标是用“当前代码真的怎么跑”的视角，说明这套 fork 的结构、运行链路、数据真源、风险边界和验证方式。

## 先看哪份文档

- `README.md`：项目总览、启动方式、目录边界和交付方式。
- `docs/WXBot Pro 使用说明.md`：面板怎么配、功能怎么用、常见问题。
- `docs/WXBot Pro 开发指南.md`：代码结构、运行链路、数据真源和改动边界。
- `docs/WXBot Pro 设计规范.md`：面板 UI 排版规范。
- `docs/人设编写规范模板.md`：基础人设和人设近况模板。
- `docs/伪BUG记录.md`：已接受机制与排查时不要重复误报的点。
- `AGENTS.md`：AI 协作规则、Windows 编码注意事项和文档同步红线。

## 维护原则

- 这是个人自用 fork。优先当前实际使用体验和代码可维护性，默认不为旧任务、旧实例、旧记录保留兼容层。
- 能收敛成单一真源时，就不要继续并行维护两套字段、两套流程或两套旧 UI 心智。
- 不要提交本地私密配置和账号数据，尤其是 `data/config/`、`data/prompt/`、`data/accounts/`。
- 高风险链路改动优先补测试，尤其是登录 / session、消息路由、图片链路、发送清洗、会话记忆、管理员接管、定时任务、素材转发和发圈。
- 模块拆分只整理本项目业务层，不要顺手改 `wxautox4`、wxauto 消息类、监听实现、下载实现或微信窗口控制底层。
- Windows PowerShell 看到中文乱码时，先当成显示问题，不要直接重写中文源码。

## 当前代码结构

- `打开软件.bat`：启动入口。优先创建或复用 Python 3.12 `venv`，安装依赖后运行 `web_server.py`。
- `web_server.py`：Flask 面板和管理 API，负责登录、配置读写、接口测试、备份、页面 API，以及启动 / 停止机器人。
- `templates/dashboard.html`：面板主体。
- `wxbot_core.py`：机器人运行时总编排，负责 wxautox4、微信监听、AI 回复、统一时间任务扫描和真实微信发送。
- `core/`：底座能力，例如配置、Prompt、媒体、记忆、发送、身份索引和调度。
- `feature/`：机器人业务规则，例如监听维护、管理员工作台、素材转发、定时任务和发圈任务。
- `extension/`：外部增强，例如邮件通知、Webhook、SiverPanel 远程访问。
- `tools/`：本地复现脚本、备份脚本和专项测试辅助。
- `tests/`：行为保护测试。

## 当前运行链路

### 1. 启动层

```text
打开软件.bat
-> 创建 / 复用 venv
-> 安装脚本内置依赖清单
-> 运行 web_server.py
-> 面板优先开放在 http://127.0.0.1:10001（被占用时顺延）
```

`打开软件.bat` 现在自己维护 Python 依赖清单，不再读取外部 `requirements.txt`。它还会兜底 `ffmpeg` / `ffprobe`：优先复用系统 PATH 中已有工具；系统缺失时，自动下载到 `venv\tools\ffmpeg\`。开发和打包都以 Python 3.12 为准。

### 2. 面板层

`web_server.py` 负责：

- 登录态、session、首页和日志拉取。
- 配置读写：`config.json`、`admin.json`、`email.json`、`webhook.json`。
- 接口连通性测试和视觉能力测试，并把能力写入 `api_capability_map`。
- Prompt、人设近况、Prompt 预览、会话记忆、聊天记录、通讯录、素材转发、发圈任务、备份等页面 API。
- 统一任务工作台 API：`/api/task-workbench/<module>` 与 `/api/task-workbench/<module>/runtime`。
- 启动 / 停止机器人：`/start_bot` 在后台线程里创建 `WXBot` 并调用 `run()`；前端通过 `/get_startup_status` 轮询真实启动结果。`/stop_bot` 和更新 `wxautox4` 前的自动停机都会等待线程真正退出。

### 3. 机器人启动层

`WXBot.__init__()` 会完成：

- 读取 `WXBotConfig`
- 初始化默认聊天接口和主备切换状态
- 初始化回复轮数存储 `ReplyCountStore`
- 初始化 Prompt 系统
- 初始化素材转发、发圈草稿、监听缓存、消息合并缓存、发送锁等运行态

真正接入微信发生在 `init_wx_listeners()`：创建 `WeChat` 对象、读取当前 `wx_id`、初始化按微信号隔离的数据目录、启动监听器，并注册管理员监听、白名单监听、群聊监听、自定义转发来源监听和素材来源监听。

### 4. 主循环

`WXBot.main()` 每轮主要负责：

- 微信在线 / 窗口状态检测
- 新好友随机间隔检测
- 全局监听模式下的会话刷新
- 运行中任务热更新请求处理
- 统一时间任务扫描：定时消息、素材转发、发圈任务、朋友圈点赞
- AI 自动转发待发送队列处理
- 管理员发圈草稿自动预览
- 通讯录自动维护检查

当前核心调度真相已经是 `core/scheduled_tasks.py` + `wxbot_core.py` 主循环扫描，不再依赖多套 `schedule.run_pending()`。动态监听子窗口不再由主循环高频巡检维护，而是在收到消息时按需复用或补一次窗口。

动态补窗的当前边界：

- `AddListenChat` 如果直接返回目标 `Chat`，立即缓存并用于处理当前批消息。
- 返回值不是可用 `Chat` 时，只短暂 `GetSubWindow` 验证几次，不重复 `AddListenChat`。
- 只有明确命中“已监听但拿不到子窗口”的残留登记状态，才进入“轻量延后监听”：内存暂存当前批消息，10 秒后最多尝试一次恢复。
- 轻量延后监听到期后先复用现有子窗口；没有可用窗口时，才在微信操作锁内执行一次 `RemoveListenChat(close_window=True) -> AddListenChat`。
- 同一好友强制重建冷却为 600 秒；任务超过 60 秒、期间已有更新消息处理、或重建失败时，旧批次直接放弃并写日志。

### 5. 收到消息后的处理链路

```text
微信监听回调
-> 基础预处理（消息属性、媒体、去重、必要时语音转文字）
-> 全局监听按需复用 / 轻量补一次动态子窗口
-> message_routing 第一轮入口分流
-> 管理员发圈输入 / 素材来源投喂 / 自定义转发人工接管优先链路
-> 普通消息处理
-> 发送前清洗、按需拆分多条、人工延迟
-> 真正 SendMsg / SendFiles / message.forward(...)
-> 写入聊天记录
-> 视情况更新会话记忆
```

### 6. 保存配置后的运行中同步

`/save_config` 在机器人运行中会做两类同步：

- API 相关字段变更时，调用 `apply_runtime_api_config_update()`，立即刷新聊天接口实例，并把聊天主备状态重置回主接口。
- 定时消息、素材转发、发圈任务等任务类配置变更时，调用 `request_runtime_task_reload()`，由主循环在安全时机重新载入后续任务表。

已经开始执行的一轮不会被强行打断；这不是热重启整个机器人，而是刷新运行中配置和后续任务计划。

## Prompt 规则

- 需要基础人设、人设近况、会话记忆、当前时间或会话名的 Prompt，优先走 `PromptSystem`。
- 普通私聊回复、群聊回复、图片最终回复、素材转发判定、素材附加文案、管理员发圈文案生成和面板 Prompt 预览，当前都已接到 `PromptSystem`。
- 辅助视觉分析、结构化视觉笔记、会话记忆提取 / 修复这类“固定协议型 Prompt”继续独立维护。
- 不要再在 `wxbot_core.py` 或 `web_server.py` 里平行拼一套 `base_prompt / persona_status_block` 逻辑。

## 模块边界

### 落位原则

- 去掉“这是微信机器人”这个前提后仍成立的，优先放 `core/`。
- 明显依赖当前业务规则和流程判断的，优先放 `feature/`。
- 面向外部系统的接入和增强，放 `extension/`。

### 当前分工

- `core/`：配置、Prompt、媒体、记忆、发送、身份索引、统一调度、通讯录持久化、运行态缓存。
- `feature/`：管理员工作台、监听维护、消息路由、关键词回复、自定义转发、通讯录建档、新好友、关系扫描、好友申请、素材转发、AI 自动转发、定时消息、发圈任务、语音回复、任务工作台。
- `extension/`：报错邮件、Webhook、SiverPanel 远程访问。

### 仍应留在 `wxbot_core.py` 的内容

- wxautox4 对象创建和窗口控制
- 监听注册、主循环、线程编排
- 真实 `SendMsg`、`SendFiles`、`message.forward(...)`、`new.accept(...)`、朋友圈打开 / 发布等微信动作
- 聊天主备切换、发送锁、主循环健康检查、运行中任务热更新入口

## 当前数据真源

- `data/config/config.json`：主配置
- `data/config/admin.json`：面板账号密码
- `data/config/email.json`：邮件通知配置
- `data/config/webhook.json`：Webhook 配置
- `data/config/reply_count.json`：私聊回复轮数限制计数
- `data/config/daily_runtime_stats.json`：状态面板、回复行为胶囊和管理员 `/状态` 使用的当天统计，包含收发消息、API 请求、语音回复、拆分回复、私聊合并、任务发送和发圈发布计数
- `data/prompt/`：人格模板和人格近况文件
- `data/system_prompts/`：系统 Prompt 片段及其备份
- `data/accounts/<wx_id>/memory/`：聊天记录
- `data/accounts/<wx_id>/conversation_memory/`：会话记忆 JSON 真源
- `data/accounts/<wx_id>/contact_profiles/contacts.json`：通讯录档案真源
- `data/accounts/<wx_id>/identity_index/contacts.json`：联系人身份索引和等待校准项
- `data/accounts/<wx_id>/identity_backups/`：身份合并前的账号级保险备份
- `data/accounts/<wx_id>/tasks/keyword_reply/`、`custom_forward/`、`scheduled_message/`、`material_outreach/`、`moments/`：各任务模块的规则、运行态和历史记录
- `data/accounts/<wx_id>/relationship_scan/relationships.json`：关系扫描结果
- `data/accounts/<wx_id>/friend_request/state.json`：好友申请设置、候选人和执行记录
- `data/accounts/<wx_id>/config/voice_reply_state.json`：语音回复运行态
- `data/accounts/<wx_id>/moments_drafts/active_draft.json`：管理员发圈草稿运行态
- `data/accounts/default/`：只有没有运行中微信号、没有 `last_wx_id`、也没有历史账号数据时才使用
- `wxbot_logs/`：面板运行日志
- `backups/data_时间戳/`：面板一键备份产物；`data/backups/` 下的身份迁移报告不是运行时真源

## 当前关键行为边界

- 会话记忆页里的“带入最近聊天”是组合控件：`好友 N 条 + AI M 条`。`memory_context_count` 最小值是 `1`，不要恢复成 `0=关闭上下文`。
- 图片回复按“最终回复接口是否支持视觉”分两条路径：支持视觉时直接传图；不支持视觉时先走 `core/vision_bridge.py` 生成结构化视觉笔记，再交给主回复模型。
- 私聊和群聊共用最近图片上下文缓存，TTL 为 10 分钟。私聊图片会立即识图并缓存；群聊图片消息本身只缓存到群级 pending visual context，不直接触发视觉模型。后续群聊文本触发 AI 且命中问图意图时，才消费该群 pending 图片；A 发图、B 问图也应命中。成功发送图片相关回复后清理 pending 图片，新图片覆盖旧图片。
- 群聊图片消息即使在 `group_reply_at=False` 时也不能因为“非仅 @”而直接进 `_reply_group_image_message()`；路由层应先缓存后 `skip`，后续问图文本再进入图片回复管线。引用图片或文本里显式携带图片路径的场景可以按显式图片请求处理。
- 所有 AI 可见 `history` 都应统一走 `core/chat_history_format.py::build_model_visible_history(...)`；图片、视频、文件不会把本地绝对路径直接喂给 AI，且媒体上下文合计最多保留最近 `3` 条。
- 默认聊天接口支持主备切换；白名单好友的专属聊天接口不参与全局主备切换。
- 私聊 `chat_listen_only` 优先级高于关键词回复、轮数超限结束语和普通 AI 回复；命中后会直接结束私聊回复链路，自定义转发除外。
- 私聊连续消息使用每好友批次流水线：`chat_message_merge_delay` 是最后一条消息后的静默等待，内部最大等待为 `delay * 3` 且限制在 9-30 秒；同一好友只跑一个 AI 回复流程，回复运行中新消息进入下一批，不再用新消息直接取消当前回复。
- 私聊白名单是私聊专属配置的单一真源：白名单里的好友优先命中自己的 `chat_prompt_map / chat_api_map / chat_tts_map`，未进白名单的私聊走全局配置。
- 私聊轮数超限结束语如果在 AI 生成阶段报错，会直接回退到 `api_error_reply`；留空则静默。
- `api_error_reply`、语音转文字失败固定提示、命中元话术固定回复和轮数超限固定回复的“只回复一次”状态都复用 `ReplyCountStore` 的回复循环窗口。新增同类提醒时，不要另起重置时间或独立状态文件。
- 语音回复属于 AI 自动回复的发送层能力；私聊勾选“收到语音触发”时，会自动带起该账号的私聊语音转文字开关。
- 语音转文字失败固定提示允许空字符串，空字符串表示静默；不要再用默认文案兜底覆盖用户主动清空。
- 定时消息内容模型已经统一成“可选 `1` 条文案 + 最多 `9` 个本地文件”；发送顺序固定为先文案、后文件。
- AI 自动转发只处理有真实新消息的活跃私聊，触发条件是 `判定周期` 和 `判定门槛` 在同一检测窗口内同时满足；每天上限只统计 AI 自动转发当天成功发送次数。
- 素材转发里的 AI 文案已经统一成同一层：无论入口是普通任务还是 `AI自动转发`，最终都走同一套“统一文案”生成逻辑。
- `AI自动转发` 仍是“两段式”链路：先判断当前最适合发哪条素材，再生成最终附加文案；不要把判断和文案生成混成一次模型调用。
- 【素材转发】页固定是 `任务列表 / 素材来源 / 素材管理` 三个二级 tab；`单次任务` 与 `循环任务` 的 `素材来源 / 素材类型` 语义不同，前者是筛选器，后者是真正的随机范围限制。
- 定时消息、素材转发、发圈任务、朋友圈点赞都走统一时间模型；新增任务类面板时优先复用现有任务工作台 contract / storage / service。
- `scheduled_message` 和 `material_outreach` 的任务卡片表达的是“生成运行时实例的规则”；`moments` 表达的是“一次明确的发布动作”，只有确认后的任务才进入待执行区。
- 面板里的发圈任务创建当前拆成两段：先建任务，再异步生成候选文案。
- 发圈文案生成有图片时统一走 `api.chat(..., image_path/image_paths=...)` 图片直传，不走辅助视觉转述路径；管理员 `/发圈` 和面板发圈任务不要再分叉维护专用多图 HTTP 请求。
- 普通定时消息和随机消息优先走已监听的聊天子窗口，找不到时再回退主窗口。
- 动态监听采用轻量按需补窗：普通增删监听不触发微信客户端重绑，不做主循环高频巡检；残留监听登记只允许走轻量延后监听的一次性受控重建。启动初始化和整体监听恢复仍可重建监听器。
- 通讯录页和会话记忆页都已经是可操作的数据管理页，不要再按纯查看器心智改。
- 身份校准跟随通讯录维护、手动建档和备注修复成功触发，不在每条消息热路径里刷新通讯录或扫描目录。
- 身份索引必须按微信账号隔离；聊天记录、会话记忆、关系扫描、任务引用和回复计数的改名 / 合并都通过账号级身份合并链路处理。
- 身份判定不做模糊匹配，不维护历史别名：同 wxid、唯一备注、无备注时 `nickname + source + added_at` 唯一且只变 wxid 才自动合并，其余有意义冲突进入等待校准。
- 群聊页勾选 `group_listen_only` 后，前端会自动保持 `group_switch` 为开启，并临时禁用依赖自动回复的相关选项。

## 修改时的高风险点

- 登录 / session
- 微信监听注册、动态子窗口缓存和全局监听按需补窗
- 消息回调分流顺序
- 管理员接管态、发圈态和素材来源静默规则的优先级
- 发送前清洗、拆分多条和延迟策略
- 回复轮数计数和超限结束语
- 会话记忆提取、提案合并和保护规则
- 定时任务 `next_fire_at` 推进、任务热更新和状态回写
- 素材转发记录、进度记录和 AI pending 队列状态
- 通讯录档案、身份索引、备注修复和手动目标名解析

## 推荐验证命令

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

文档不要写成阶段日志。开发说明要写“现在是什么”，不是“上次做到了哪一步、下次准备做什么”。
