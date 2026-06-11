# WXBot Pro 开发维护说明

本文档给本地维护者和后续 AI Agent 使用，目标是用“当前代码真的怎么跑”的视角，说明这套 fork 的结构、运行链路、数据真源、风险边界和验证方式。

## 先看哪份文档

- `README.md`：给使用者看的功能说明、目录说明、配置/打包边界。
- `docs/本地项目开发指南.md`：给开发维护者看的当前代码结构、运行链路和改动边界。
- `docs/人设编写规范模板.md`：给维护者写基础人设与人设近况时用的辅助模板。
- `docs/产品UI排版规范.md`：给后续改管理面板时用的统一排版约束。
- `docs/伪BUG记录.md`：给排查和回归时用，避免重复把已接受机制当成 BUG。
- `AGENTS.md`：给 AI 协作时用，约束项目边界、红线和推荐检查命令。

## 维护原则

- 这是个人自用 fork。开发、修复和重构时，优先保证当前实际使用体验和代码可维护性；默认不为旧任务、旧实例、旧记录保留兼容层，除非这次改动明确要求。
- 不保留旧字段兼容层、迁移壳或“顺手清理历史配置”的旁路逻辑；能收敛成单一真源时，就不要继续并行维持两套字段或流程。
- 不要提交本地私密配置和人格模板，尤其是 `data/config/config.json`、`data/prompt/`、`data/accounts/` 下的任何账号数据。
- 高风险链路的行为变更优先补测试，尤其是登录 / session、消息路由、图片链路、发送清洗、会话记忆、管理员接管、定时任务、素材转发、发圈和状态持久化。
- 模块拆分只整理本项目业务层，不要顺手改 `wxautox4`、wxauto 消息类、监听实现、下载实现或微信窗口控制底层。
- Windows PowerShell 可能把 UTF-8 中文显示成乱码。看到 `鑾峰彇` 这类字符时，先当成终端显示问题，不要直接重写中文源码。

## 当前代码结构

当前实际入口和分层如下：

- `打开软件.bat`：启动入口。优先创建 / 复用 Python 3.12 `venv`，安装依赖后运行 `web_server.py`。
- `web_server.py`：Flask 面板和管理 API。负责登录、配置读写、接口测试、备份、Prompt/记忆/通讯录/发圈相关接口，以及启动 / 停止机器人。
- `templates/dashboard.html`：面板 UI。
- `wxbot_core.py`：机器人运行时总编排。负责 wxautox4、微信监听、消息处理、AI 回复、统一时间任务扫描、真实微信发送和运行状态。
- `core/`：可复用底座能力。
- `feature/`：机器人业务规则。
- `extension/`：外部增强通道。
- `tools/`：本地复现脚本、备份脚本和专项测试辅助。
- `tests/`：行为保护测试。

## 当前运行链路

### 1. 启动层

```text
打开软件.bat
-> 创建 / 复用 venv
-> 安装 requirements.txt
-> 运行 web_server.py
-> 面板优先开放在 http://127.0.0.1:10001（被占用时顺延）
```

当前 `打开软件.bat` 会优先寻找 Python 3.12，包括本地 `runtime\python`、系统 `py -3.12`、`uv python find 3.12` 等来源。开发和打包都以 Python 3.12 为准。

另外，`打开软件.bat` 现在还会负责 `ffmpeg` / `ffprobe` 的运行依赖兜底：优先复用系统 PATH 中已有的工具；系统缺失时，自动从 `BtbN/FFmpeg-Builds` 下载 Windows 预编译包，并解到 `venv\tools\ffmpeg\`，只对当前项目生效。
当前仓库不再把旧会话记忆迁移脚本作为常规维护入口；会话记忆以当前 JSON 结构和面板维护链路为准。

### 2. 面板层

`web_server.py` 当前负责：

- 登录态、session、面板首页和日志拉取。
- 配置读写：`config.json`、`admin.json`、`email.json`、`webhook.json`。
- API 连通性测试和视觉能力测试，并把接口能力写入 `api_capability_map`。
- Prompt、人格近况、Prompt 预览、会话记忆、聊天记录、通讯录、素材转发、发圈任务、备份等页面 API。
- 统一任务工作台 API：`/api/task-workbench/<module>`、`/api/task-workbench/<module>/runtime`，以及对应的发圈确认 / 取消、运行时实例取消等操作接口。
- 启动 / 停止机器人：`/start_bot` 会在后台线程里创建 `WXBot` 并调用 `run()`，并立即返回“启动中”状态；前端再通过 `/get_startup_status` 轮询真实启动结果。`/stop_bot` 和更新 `wxautox4` 前的自动停机都会等待线程真正退出后才回报成功。

当前面板层已经不只是“静态配置页”，它本身就是运行时控制台的一部分。

### 3. 机器人启动层

`WXBot.__init__()` 当前会完成这些事情：

- 读取 `WXBotConfig`。
- 初始化默认聊天接口和聊天主备切换状态。
- 初始化回复轮数存储 `ReplyCountStore`。
- 初始化 Prompt 系统。
- 初始化素材转发、发圈草稿、监听缓存、去重缓存、消息合并缓存、发送锁等运行态。

真正接入微信发生在 `init_wx_listeners()`：

- 创建 `WeChat` 客户端对象。
- 读取当前微信号 `wx_id`。
- 初始化聊天记录、会话记忆、素材转发、发圈草稿等按微信号隔离的数据命名空间。
- 启动 wxautox 监听器。
- 注册管理员监听、白名单监听、群聊监听、自定义转发来源监听、素材来源监听。
- 把监听子窗口写入运行时缓存。
- 注册统一时间扫描器占位。当前实现不会再向 `schedule.every()` 注册多套任务。

### 4. 主循环

`WXBot.main()` 的主循环当前每轮会做这些事情：

- 微信在线 / 窗口状态检测。
- 新好友随机间隔检测。
- 全局监听模式（黑名单模式）下的会话刷新。
- 处理“保存配置后”的运行中任务热更新请求。
- 统一时间任务扫描：
  - 定时 / 随机定时消息
  - 固定 / 随机素材转发
  - 发圈任务
  - 随机朋友圈点赞
- AI 自动转发待发送队列处理。
- 管理员发圈草稿自动预览。
- 通讯录自动维护检查。

当前统一时间任务扫描已经替代老式的 `schedule.run_pending()` 多套驱动思路。`schedule` 依赖还在，但核心调度真相已经是 `core/scheduled_tasks.py` + `wxbot_core.py` 主循环扫描。

### 5. 收到消息后的处理链路

当前消息入口是 `message_handle_callback()`。真实处理顺序可以这样理解：

```text
微信监听回调
-> 基础预处理（消息属性、媒体、去重、必要时语音转文字）
-> message_routing 第一轮入口分流
-> 管理员发圈输入 / 素材来源投喂 / 自定义转发人工接管优先链路
-> 普通消息处理
   -> message_routing 过滤与路由判断
   -> 私聊或群聊 AI 回复
   -> 图片直传回复或“识图后再回复”的两段式链路
-> 发送前清洗、按需拆分多条、人工延迟
-> 真正 SendMsg / SendFiles / message.forward(...)
-> 写入聊天记录
-> 视情况更新会话记忆
```

### 6. 保存配置后的运行中同步

当前配置保存不再只有“写文件后等下次重启”一种结果。

`/save_config` 在机器人运行中会做两类同步：

- API 相关字段变更时，调用 `apply_runtime_api_config_update()`，立即刷新聊天接口实例，并把聊天主备状态重置回主接口。
- 定时消息、素材转发、发圈任务等任务类配置变更时，调用 `request_runtime_task_reload()`，由主循环在安全时机重新载入后续任务表。

注意：

- 已经开始执行的一轮任务不会被强行打断。
- 这不是“热重启整个机器人”，只是刷新运行中配置和后续任务计划。

### 7. 当前 Prompt 真相

当前项目里的 Prompt 现在按“上下文型”与“技术型”两类维护，不要再把它们混成一套。

- 上下文型 Prompt：需要结合基础人设、人设近况、会话记忆、当前会话名、当前时间等上下文后，才能发给模型。
- 技术型 Prompt：只负责输出协议、结构或固定格式，不依赖人格上下文。

当前已经统一到 `PromptSystem` 的链路：

- 普通私聊回复：`final_reply.md`
- 群聊回复：仍走 `PromptSystem.build_prompt(...)`，但不会注入会话记忆
- 图片最终回复：图片链路不再单独传 `base_prompt`，最终回复统一回到 `PromptSystem.build_prompt(...)`
- 素材转发判定：`material_decision.md`
- 素材转发附加文案：`material_preface.md`
- 管理员发圈文案生成：`moments_caption.md`
- 面板里的 Prompt 预览：和运行时共用 `PromptSystem`

这里的“统一”指的是：

- 基础人设正文由 `PromptSystem.base_prompt_for(...)` 决定
- 当前使用哪份人设由 `PromptSystem.prompt_name_for(...)` 决定
- `base_prompt / persona_status_block / conversation_memory_section / now / chat_name` 这几个上下文字段由 `PromptSystem.context_values_for(...)` 统一产出
- 需要渲染任务型模板时，优先走 `PromptSystem.render_template_prompt(...)`
- `render_template_prompt(...)` 会统一提供上述上下文字段，但只会强校验 `base_prompt / persona_status_block / conversation_memory_section` 以及当前模板显式声明的必填占位符；`now / chat_name` 只有模板自己真的用了时，才需要出现在模板里

当前故意保留为独立技术型 Prompt 的链路：

- `core/prompting.py` 里的辅助视觉分析器 system prompt 与图片描述 prompt
- `image_parse.md`：把结构化视觉笔记转成最终回复可消费的上下文块
- 会话记忆提取 / 修复 prompt：由 `ConversationMemoryExtractor` 单独维护
- `closing_reply.md`：轮数超限结束语，属于独立任务模板，但现在也接入 `PromptSystem` 的人设、近况和会话记忆上下文

维护时的判断规则：

- 如果一个 Prompt 需要“当前人设是谁、最近状态如何、会话记忆是什么”，就应优先并到 `PromptSystem`
- 如果一个 Prompt 只是要求模型“按固定结构输出 JSON / 视觉笔记 / 修复结果”，通常继续保留为技术型独立 Prompt
- 不要再在 `wxbot_core.py` 或 `web_server.py` 里手工拼一套和 `PromptSystem` 平行的 `base_prompt / persona_status_block` 逻辑

## 当前模块边界

### 模块落位判定规则

这套 fork 继续保留 `core / feature / extension` 三层，但不追求“学术上绝对纯”的边界。判断标准以维护成本和实际职责为准。

- `core/`：放相对稳定、可复用、少业务判断的基础能力。
- `feature/`：放这只机器人自己的业务规则、流程编排和决策逻辑。
- `extension/`：放面向外部系统的增强通道或集成适配。

落位时优先按下面这组问题判断：

- 去掉“这是一只微信机器人”这个前提后，模块仍然成立，优先放 `core/`。
- 去掉当前业务规则后，模块就失去意义，优先放 `feature/`。
- 模块主要在做“存取、格式化、校验、归一化、拼装、适配”，通常更像 `core/`。
- 模块里大量出现“如果群聊 / 如果命中接管 / 如果开关打开 / 如果命中某条规则”这类判断，通常更像 `feature/`。

处理灰区时遵循以下原则：

- 如果一个模块约 `70%` 是业务规则，就放 `feature/`。
- 如果一个模块约 `70%` 是通用支撑，就放 `core/`。
- 老模块不要为了“命名更纯”大规模搬家；只有在能明显减体积、降风险、提可读性时再迁移。
- 新增模块优先放对位置，比回头重洗历史落位更重要。

当前项目里，`message_routing.py`、`listening.py`、`contacts.py` 这类文件都按“机器人业务规则”处理，归入 `feature/`；`media.py`、`sending.py`、`reply_count_store.py` 这类通用支撑继续归入 `core/`。

### `core/` 底座

- `core/config.py`：配置默认值辅助、数值收敛、接口能力标记。
- `core/media.py`：图片路径判断、本地图片校验、图片内容 hash。
- `core/message_pipeline.py`：消息去重 ID、连续私聊消息合并。
- `core/prompting.py`：图片相关 Prompt 片段、结构化视觉提示和图片消息拼装。
- `core/prompt_system.py`：人格模板、人格近况、Prompt 构建、系统 Prompt、会话记忆提取和保存保护。
- `core/memory.py`：聊天记录命名、安全目录、历史读取、聊天记录管理器。
- `core/reply_pipeline.py`：图片直传回复与“两段式辅助视觉 -> 主回复模型”策略。
- `core/vision_bridge.py`：辅助视觉桥接层，负责识图结果结构化、同图缓存和失败兜底。
- `core/sending.py`：发送前清洗、元话术过滤、按需拆分多条、短多行自动拆分、待发送片段准备。拆分逻辑中，“最多条数”只做上限；没有显式换行且整段不超过单条最大字数时，默认保持单条发送。私聊 AI 回复在开启拆分多条后，额外允许“纯中文 / 中文标点 / 空格”的短回复按空格停顿拆成 2 ~ 3 个气泡；群聊和默认解析不启用这条空格规则。
- `core/scheduled_tasks.py`：统一时间模型和任务计划推进，包括 `fixed_at`、`random_in_date_window`、`interval_next` 三种计划。
- `core/runtime_chat_state.py`：运行时监听缓存、目标发送适配、单好友暂停回复状态。
- `core/contact_profiles.py`：通讯录档案持久化、标签解析、备注修复、手动目标解析。
- `core/logger.py`：面板日志缓存和本地日志文件写入。

### `feature/` 业务规则

- `feature/admin_commands.py`：管理员命令入口和分发。
- `feature/admin_status.py`：`/状态`、`/监听列表`、`/自动回复状态`、`/当前会话` 文案拼装。
- `feature/admin_control.py`：管理员命令对应的业务动作。
- `feature/admin_moments_flow.py`：管理员发圈前台的草稿收集、超时和确认流程。
- `feature/admin_forward_flow.py`：管理员素材转发前台的草稿收集、目标选择和确认流程。
- `feature/takeover_runtime.py`：管理员工作台模式、接管会话、消息镜像、管理员普通消息路由。
- `core/daily_runtime_stats.py`：管理员 `/状态` 使用的全局当天统计真源，不按 `wx_id` 隔离，也不受任务执行记录清空影响。
- `feature/message_routing.py`：新消息过滤、黑白名单/群私聊分流、关键词前置判断、接管与普通回复入口路由。
- `feature/keyword_reply.py`：私聊 / 群聊关键词回复规则，负责多关键词归一化、结构化规则清洗，以及“文案 + 本地文件”发送动作展开。
- `feature/custom_forward.py`：自定义转发规则匹配、转发动作计划、命中后人工接管决策。
- `feature/custom_forward_runtime.py`：自定义转发的真实执行和与接管态的联动。
- `feature/contacts.py`：通讯录建档、自动维护、批次分析、状态摘要。
- `feature/listening.py`：监听窗口维护、监听初始化、全局监听收消息、新好友通过、群欢迎语和监听超时移除。
- `feature/new_friends.py`：新好友通过、自动备注标签和状态文案。
- `feature/relationship_scan.py`：关系扫描结果、微信标签同步和“删除我的人 / 拉黑我的人”状态沉淀。
- `feature/friend_request.py`：好友申请设置、候选人、调度和执行记录。
- `feature/friend_request_senders.py`：好友申请发送器；当前真实发送方式是会话验证入口。
- `feature/material_outreach.py`：素材池、目标解析、批次规划、发送 / 跳过 / 进度记录、随机素材转发计划。
- `feature/ai_material_outreach.py`：AI 自动转发判断、pending 队列、节流和取消逻辑。
- `feature/material_outreach_storage.py`：素材任务、素材池和运行记录的存取适配。
- `feature/material_outreach_preface.py`：素材转发附加文案相关的结构化辅助。
- `feature/scheduled_messages.py`：定时消息真实执行适配。
- `feature/scheduled_message_tasks.py`：统一定时消息任务对象、运行态、执行历史和回退逻辑。
- `feature/runtime_task_runner.py`：统一时间任务执行入口和运行时推进。
- `feature/moments_tasks.py`：发圈草稿、候选文案、任务规范化、排队和退回状态。
- `feature/moments_publisher.py`：朋友圈真实发布动作封装。
- `feature/moments_like.py`：随机朋友圈点赞。
- `feature/voice_reply.py`：语音回复触发判定、限流和运行态持久化。
- `feature/task_display_titles.py`：任务工作台标题、摘要和展示文案归一化。
- `feature/task_workbench_contract.py`：统一任务卡片 / 队列 / 执行记录的数据契约。
- `feature/task_workbench_runtime_summary.py`：任务工作台运行时实例和执行记录摘要聚合。
- `feature/task_workbench_storage.py`：按模块读写 `tasks / runtime / history` 的存储适配层。
- `feature/task_workbench_service.py`：定时消息、素材转发、发圈任务共用的工作台服务层。

### `extension/` 外部增强

- `extension/email.py`：报错邮件 / 离线提醒 SMTP 通道。
- `extension/webhook.py`：Webhook 通知通道。
- `extension/siver_panel.py`：SiverPanel 远程访问连接和请求转发。

### 保持在 `wxbot_core.py` 的内容

下面这些逻辑目前仍应留在 `wxbot_core.py`，不要为了“拆得更漂亮”就随意下沉：

- wxautox4 对象创建和窗口控制。
- 监听注册、主循环、线程编排。
- 真实 `SendMsg`、`SendFiles`、`message.forward(...)`、`new.accept(...)`、朋友圈打开 / 发布等微信动作。
- 高风险运行态：聊天主备切换、发送锁、主循环健康检查、运行中任务热更新入口。

## 当前数据真源

当前运行目录真相如下：

- `data/config/config.json`：主配置。
- `data/config/admin.json`：面板账号密码。
- `data/config/email.json`：邮件通知配置。
- `data/config/webhook.json`：Webhook 配置。
- `data/config/reply_count.json`：私聊回复轮数限制计数，当前按 `chat.who` / 好友名分别记录。
- `data/prompt/`：人格模板和人格近况文件；当前目录里也保留可直接复制的模板文件 `模板.md` 与 `模板-人设近况.md`。
- `data/system_prompts/`：系统 Prompt 片段；`data/system_prompts/prompt_backup/` 统一保存系统 Prompt 的 `.md` 恢复文件。
- `data/accounts/<wx_id>/memory/<safe_chat_name>/`：聊天记录。
- `data/accounts/<wx_id>/conversation_memory/<safe_chat_name>.json`：会话记忆 JSON 真源。
- `data/accounts/<wx_id>/contact_profiles/contacts.json`：通讯录档案真源。
- `data/accounts/<wx_id>/tasks/keyword_reply/rules.json`：关键词回复规则；当前值结构是 `{"关键词A；关键词B": {"keywords": [...], "text": "...", "files": [...]}}`，其中 `files` 最多保留 `9` 个本地绝对路径。
- `data/accounts/<wx_id>/tasks/custom_forward/rules.json`：自定义转发规则。
- `data/accounts/<wx_id>/tasks/scheduled_message/{tasks,runtime,history}.json`：定时消息任务定义、运行态和执行记录；任务内容当前统一走 `msgs` 字段，解释为“可选 `1` 条文案 + 最多 `9` 个本地绝对路径文件”，发送顺序固定为先文案、后文件。
- `data/accounts/<wx_id>/tasks/material_outreach/{tasks,runtime,history,materials}.json`：素材转发任务、运行态、执行记录和素材池。
- `data/accounts/<wx_id>/tasks/moments/{tasks,runtime,history}.json`：发朋友圈任务定义、运行态和执行记录。
- `data/accounts/<wx_id>/tasks/moments/uploads/`：发圈上传图片。
- `data/accounts/<wx_id>/relationship_scan/relationships.json`：关系扫描结果、同步状态和事件记录。
- `data/accounts/<wx_id>/friend_request/state.json`：好友申请设置、候选人、执行记录和日统计。
- `data/accounts/<wx_id>/config/voice_reply_state.json`：语音回复限流和最近触发运行态。
- `data/accounts/<wx_id>/moments_drafts/active_draft.json`：管理员发圈草稿运行态。
- `data/config/daily_runtime_stats.json`：机器人级别的当天统计；当前记录已收消息、已回复消息、定时消息、素材转发、AI 转发和发朋友圈次数。
- `data/accounts/default/`：只有在没有运行中微信号、没有 `last_wx_id`、也没有任何已有账号数据时才使用的默认账号空间；不要把它理解成所有场景下的固定主目录。
- `panel_logs/`：面板运行日志。
- `backups/data_时间戳/`：面板一键备份产物。
- 任务工作台本次没有新增任何平行历史文件；摘要仍只消费各模块原有的 `runtime.json / history.json`。`scheduled_message` / `moments` / `material_outreach` 的运行态分别保存本轮快照、发布快照和批次上下文，用于工作台渲染。
- 工作台标题现在按内容驱动：`scheduled_message` 显示首条文案，纯附件任务回退到首个文件名；`material_outreach` 的单次任务显示素材 `content_preview`、循环任务显示 `素材来源 + 素材类型`；`moments` 显示最终选中的发布文案或 `无文案`。
- 这三类任务的待执行实例和执行记录也都沿用这套内容优先标题，不再依赖旧的时间型 `generated_title` 机制。
- 工作台第二行只放模块自己的补充信息，第三行固定先放时间，再放发送目标、标签、可见范围等执行维度，只有失败或需要补充说明时才继续拼失败原因或失败次数。`scheduled_message` 不再展示 `目标 N 人，发送 M 条`，`moments` 成功时不再重复显示 `发布成功`，`material_outreach` 第二行只保留实际附加文案。
- `material_outreach` 底部的 `运行时实例 / 执行记录` 统一只走 `build_task_workbench_runtime_payload(...) + renderTaskWorkbenchRuntimeCards(...)` 这条共享渲染链路，不再恢复旧的首屏 Jinja 队列卡片。
- 发圈任务对象还保留 `ai_generation_status`（`idle / pending / done`）和 `ai_generation_error` 两个面板相关字段；候选生成失败时仍维持等待态，由用户手动再次点 `重新生成`。

### 会话记忆约定

- 会话记忆正式真源是 JSON，不再把 Markdown 当正式保存入口。
- AI 只返回提案 JSON，由程序做合并、校验和拦截，避免整份重写误清空。
- 结构化会话记忆现在统一为单一 `memories` 真源；面板展示顺序和喂给 AI 的顺序一致，都会按重要度 `高 -> 中 -> 低`、组内按 `updated_at` 从新到旧自动排序。
- 会话记忆自动维护只会在有新增聊天记录时判断；累计新增达到阈值，或距离上次处理已超过配置间隔时才会触发，空窗口不会因为缺少 `last_processed_at` 就空跑。
- 面板手动“马上提取记忆”当前会把单次分析上限提到 `500` 条聊天记录，方便一次补齐较长历史。
- 私聊 AI 回复成功发送后，会写入 `sender=self` 的会话记忆；同文案的回声会在约 `60` 秒内去重，避免重复入记忆。
- `conversation_memory`、Prompt 预览、通讯录与任务工作台的账号选择现在共用一套语义：`运行中微信号 -> last_wx_id -> 有实际文件内容的历史账号 -> default`。
- 只有空目录、没有任何实际文件内容的账号壳层不再算有效账号；不要再把这类目录当成“已有历史账号”。
- 显式请求里的 `wx_id` 如果已经失效，后端应直接报错并提示重新选择；不要恢复“未知 `wx_id` 也自动建目录”的旧行为。

### 打包边界

当前仓库没有可直接使用的 `打包发布.ps1`，也没有默认产出的 `dist/WXBot_Pro.zip`。这份 fork 现在按“整目录复制”交付最稳：复制代码目录、`data/system_prompts/`，以及需要继承的 `data/config/`、`data/prompt/`、`data/accounts/` 等用户数据即可。新机器首次运行继续执行 `打开软件.bat`，让它按启动链路自动补齐 `venv`、依赖和缺失的 `ffmpeg` / `ffprobe`。

## 当前关键行为边界

- 会话记忆页里的“带入最近聊天”当前是组合控件，不再是两个独立数字框；心智统一为 `好友 N 条 + AI M 条`。`memory_context_count` 当前允许 `1 ~ 100`，`memory_context_assistant_count` 当前允许 `0 ~ 100`，默认值分别是 `50` 和 `10`；后者只限制模型可见上下文里的旧 AI 回复条数，不会额外裁掉用户消息。
- 当前不要再把 `memory_context_count=0` 当成合法配置；面板保存、配置加载和测试都以 `1` 为最小值。改这块时不要再恢复会把 `0` 当成“关闭上下文”的旧逻辑。
- 图片回复按“最终回复接口是否支持视觉”分成两条执行路径：支持视觉时直接传图；不支持视觉时先走 `core/vision_bridge.py` 生成结构化视觉笔记，再把笔记注入 `image_parse.md` 系统 Prompt，由主回复接口负责生成最终回复。
- 所有 AI 可见 `history` 当前都应统一走 `core/chat_history_format.py::build_model_visible_history(...)`，不要再各处手工拼 history。它会同时处理旧 AI 回复限流、媒体消息净化和模型可见格式化。
- 媒体消息净化的当前规则是：图片、视频、文件都不再把本地绝对路径直接喂给 AI；图片如果已经走过“两段式辅助视觉”，会优先带 `图片概览 / 可见文字 / 关键细节`，视频和普通文件当前只保留“发来一个视频 / 发来一个文件”的占位信息。
- 媒体上下文当前单独限流为“图片 / 视频 / 文件合计最多最近 `3` 条”，这条规则不仅作用于普通聊天回复，也作用于素材转发附加文案、AI 自动转发判断、会话记忆提取等所有复用 `history` 的 Prompt。
- 默认聊天接口支持主备切换；统计的是当前运行内的连续失败次数，不持久化到磁盘。切到备用接口后会按固定间隔自动探测主接口，探测成功后自动切回。
- 私聊 `chat_listen_only` 当前优先级高于关键词回复、轮数超限结束语和普通 AI 回复；命中后会直接结束私聊回复链路，自定义转发除外。
- 私聊白名单现在是私聊专属配置的单一真源：白名单里的好友在两种监听模式下都优先命中自己的 `chat_prompt_map / chat_api_map / chat_tts_map`；未进白名单的私聊统一走全局配置。
- 白名单专属聊天接口不参与全局主备切换；只有默认聊天接口链路会自动切到备用接口。
- OpenAI SDK 路径在主 `Chat Completions` 失败后会回退到 `Responses API`；当前备用路径也会继续透传 `history`，不要再把它当成“只吃 prompt + 当前消息”的无上下文兜底。
- 私聊轮数超限结束语如果在 AI 生成阶段遇到接口报错，当前会直接转入 `api_error_reply` 配置；留空则静默，不再把内部错误占位文案当正常回复发送。
- 管理员工作台当前有四种前台模式：空闲、接管、发圈、转发，互斥。
- 私聊 / 群聊语音回复属于 AI 自动回复的发送层能力；TTS 配置、试听缓存和语音会话状态分别落在 `tts_configs`、`data/cache/tts*/`、`data/accounts/<wx_id>/config/voice_reply_state.json`。其中私聊勾选“收到语音触发”时，会自动带起该账号的私聊语音转文字开关。
- 语音回复成功发送后，当前轮待跟进的图片上下文会立即清空，避免把上一张图串到后续无关私聊。`data/cache/tts/` 里的正式语音缓存现在也会在发送完成或失败后回收，不再长期堆积。
- 关键词回复当前已经不是“关键词 -> 单段文本”的旧结构；保存时会规范成多关键词结构化对象，发送顺序固定为文案先发、附件后发，空规则会在保存阶段直接拦截。
- 定时消息当前也不再区分“文案列表 + 图片列表”；详情区和运行态统一为“可选 `1` 条文案 + 最多 `9` 个本地文件”，图片也按文件发送。
- AI 自动转发现在只处理有真实新消息的活跃私聊；新消息 = 对方任意私聊消息，表情也算。
- AI 自动转发的触发条件是 `判定周期（分钟）` 与 `判定门槛（条消息）` 在同一检测窗口内同时满足；如果消息门槛先达到，就继续等到周期补齐，补齐后即使没有新消息也会补触发这 1 次判定。
- `AI自动转发` 系统卡片当前只保留 `判定周期（分钟）`、`判定门槛（条消息）`、`每天最多转发（次）` 这 3 个核心节奏控件；候选素材来源过滤只约束 AI 自动转发可挑选的素材范围，不要误当成普通素材任务的筛选条件。
- 素材转发里的 AI 文案现在已经统一成同一层：不管入口是任务转发，还是 `AI自动转发`，只要最终真的要发，都会走同一套“统一文案”生成层；这层会一起参考基础人设、人设近况、会话记忆、近期聊天记录、素材信息和转发目标。
- `AI自动转发` 现在仍是“两段式”链路：先按 `素材关联度` 和候选来源判断“当前最适合发哪条”；这一层会同时参考最近对话和本轮最新信号。即时触发时，最新信号就是对方刚发来的那条消息；主循环补触发时，最新信号会改成“过去 X 分钟新增了 Y 条私聊消息”的窗口摘要。只有全部候选都明显不合适时才不发，命中后再调用统一文案层生成最终附加文案；不要再把判断和最终文案混成同一次模型调用来理解。
- 发给模型的候选素材卡片当前固定只保留 `index`、`type`、`content_preview`、`ownership`、`copy_note`；`stable_signature` 只在程序内部用于去重和排重，不要再作为模型输入字段理解。
- 同一好友已经成功发送过的素材，后续 AI 判定时会直接从候选池里排除；`每天最多转发（次）` 也只统计 AI 自动转发当天成功发出的次数。
- AI 自动转发运行态现在只维护 `ai_detection_state` 与 `ai_pending_queue`，不再维护补检运行态或 miss 节流状态。
- 面板里的【素材转发】页现在分成 `任务列表 / 素材来源 / 素材管理` 三个二级 tab；不要再按旧的双 tab 心智改前端。
- 素材转发里的 `单次任务` 与 `循环任务` 语义不同：单次任务的 `素材来源 / 素材类型` 只用于筛选固定素材下拉；循环任务里的同名控件才是真正限制随机选材范围。
- `素材管理` 只允许改 `status`、`ownership`、`copy_note` 等转发元数据，不编辑微信原始素材内容，也不要再恢复 `ai_description` 那套素材分析字段。
- 定时消息、素材转发、发圈任务、朋友圈点赞都走统一时间模型，不要再新增一套平行调度器。
- `scheduled_message`、`material_outreach`、`moments` 三个模块已经统一接入任务工作台；新增任务类面板时，优先复用这套 contract / storage / service，而不是再造一套列表和 runtime 接口。
- `关键词回复`、`自定义转发`、`scheduled_message`、`material_outreach`、`moments` 这 5 个双栏页面现在统一复用 `ui-workbench-pane + ui-workbench-scroll-body` 这套前端外壳：桌面端左侧列表区独立限高滚动、右侧详情保持同屏；移动端取消内部滚动恢复整页展开。改这些页面时优先复用这套壳，不要再各自维护一套滚动规则。
- `scheduled_message` 和 `material_outreach` 的任务卡片表达的是“生成运行时实例的规则”；实例由系统自动生成，停用任务时会直接撤销待执行实例并停止后续生成。
- `moments` 任务卡片表达的是“一次明确的发布动作”；AI 候选生成属于任务内容准备态，不是独立运行时队列，只有确认后的任务才进入待执行。
- 面板里的发圈任务创建当前拆成两段：`/api/moments/tasks` 只负责立刻建任务并把 `ai_generation_status` 置为 `pending`；候选文案由前端随后调用 `/api/moments/tasks/<task_id>/generate` 异步生成。
- 普通定时消息和随机消息优先走已监听的聊天子窗口，找不到时再回退主窗口。
- 通讯录页和会话记忆页都已经是“可操作的数据管理页”，不是单纯的静态查看器。
- 群聊页勾选 `group_listen_only` 后，前端会自动保持 `group_switch` 为开启，并临时禁用 `group_reply_at` / `group_reply_at_msg` 这两个依赖自动回复的选项。

## 修改时的高风险点

- 登录 / session。
- 微信监听注册和子窗口缓存。
- 消息回调分流顺序。
- 管理员接管态、发圈态和素材来源静默规则的优先级。
- 发送前清洗、拆分多条和延迟策略。
- 回复轮数计数和超限结束语。
- 会话记忆提取、提案合并和保护规则。
- 定时任务 `next_fire_at` 推进、任务热更新和状态回写。
- 素材转发记录、进度记录和 AI pending 队列状态。
- 通讯录档案、备注修复和手动目标名解析。

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

以下情况改完代码后，文档要一起改：

- 面板新增 / 删除功能卡片、接口或运行入口：更新 `README.md`。
- 代码结构、模块职责、运行链路或打包边界变化：更新 `本地项目开发指南.md`。
- 对外完整使用说明变化：当前仓库默认以 `README.md` 为主；如果另外新增了对外文档，再一起同步更新。
- AI 协作边界、风险点、推荐检查命令变化：更新 `AGENTS.md`。

文档不要写成阶段日志。开发说明要写“现在是什么”，不是“上次做到了哪一步、下次准备做什么”。
