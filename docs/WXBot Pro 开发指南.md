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

`打开软件.bat` 现在自己维护 Python 依赖清单，不再读取外部 `requirements.txt`。它还会兜底 `ffmpeg` / `ffprobe`：优先复用系统 PATH 中已有工具；系统缺失时，自动下载到 `venv\tools\ffmpeg\`。图片压缩依赖 `Pillow`，脚本会在创建新虚拟环境时安装，并在复用旧虚拟环境时用 `import PIL` 补检。开发和打包都以 Python 3.12 为准。

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
- 动态接管窗口不可用时，当前批消息会进入“轻量延后监听”：30 秒尝试一次，失败后 60 秒再尝试一次；TTL 为 90 秒，用来给第二次调度留余量。
- 同一好友已有轻量延后监听任务时，新批次消息只合并进原任务，不再立刻补窗，避免微信列表围绕同一联系人反复滚动。
- 轻量延后监听到期后先复用现有子窗口；普通补窗失败只再次尝试普通 `AddListenChat`，只有明确命中“已监听但拿不到子窗口”的残留登记状态，才允许在 600 秒同好友冷却内执行一次 `RemoveListenChat(close_window=True) -> AddListenChat`。
- 管理员、白名单固定监听、群监听、自定义转发来源和素材投喂来源属于固定监听，不参与动态监听 600 秒超时清理。主循环会低频巡检固定监听；巡检受 30 秒间隔和非阻塞微信操作锁限制，在全局监听已有轻量延后任务时主动让路。
- 轻量延后期间同一好友已有新消息被处理、第一次恢复未成功、或首次补窗失败但已进入延后重试时，日志使用 `INFO`；两次恢复失败、重建异常或最终放弃时使用 `WARNING`；恢复成功使用 `SUCCESS`。

### 5. 收到消息后的处理链路

```text
微信监听回调
-> 基础预处理（消息属性、媒体、去重、只读取微信已生成的语音识别结果）
-> 全局监听按需复用 / 轻量补一次动态子窗口
-> message_routing 第一轮入口分流
-> 管理员发圈输入 / 素材来源投喂 / 自定义转发人工接管优先链路
-> 普通消息处理
-> 私聊好友消息去重后先写入聊天记录，再进入连续消息合并队列
-> 私聊普通 AI 回复前按需补齐最近上下文，再读取 history 组装 Prompt
-> 发送前清洗、按需拆分多条、人工延迟
-> 真正 SendMsg / SendFiles / message.forward(...)
-> 成功发送后写入 AI 回复等输出记录
-> 视情况更新会话记忆
```

当前去重口径按“同一会话 + 同一发送者 + 同一类型 + 同一内容”的短时间重复回调处理，不再按回调来源区分放行；这样能避免同一条真实消息从不同监听入口进来时被重复处理。

私聊好友输入在 `_enqueue_private_message_for_ai()` 里会先调用 `_save_private_incoming_memory_message()` 落到本地聊天记录，再进入连续消息合并队列；这只是对方输入的预写，不是提前写 AI 输出。随后 `wx_send_ai()` 在 `_get_model_context_history()` 之前调用 `_repair_private_context_before_ai()`，让模型拿到的 history 尽量贴近微信窗口真实尾部。

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
- `data/config/runtime_metrics_v1.json`：状态面板、数据图表和管理员 `/状态` 使用的小时级运行统计，保留最近 365 天；统计失败只影响展示，不阻断业务流程
- `data/prompt/`：人格模板和人格近况文件
- `data/system_prompts/`：系统 Prompt 片段及其备份
- `data/accounts/<wx_id>/memory/`：聊天记录
- `data/accounts/<wx_id>/chat_memory/`：会话记忆 JSON 真源
- `data/accounts/<wx_id>/contact_profiles/contacts.json`：通讯录档案真源
- `data/accounts/<wx_id>/identity_index/contacts.json`：联系人身份索引和等待校准项
- `data/accounts/<wx_id>/identity_backups/`：身份合并前的账号级保险备份
- `data/accounts/<wx_id>/tasks/keyword_reply/`、`custom_forward/`、`scheduled_message/`、`material_outreach/`、`moments/`：各任务模块的规则、运行态和历史记录
- `data/accounts/<wx_id>/relationship_scan/relationships.json`：关系扫描结果
- `data/accounts/<wx_id>/friend_request/state.json`：好友申请设置、候选人和执行记录
- `data/accounts/<wx_id>/config/voice_reply_state.json`：语音回复运行态
- `data/accounts/<wx_id>/moments_drafts/active_draft.json`：管理员发圈草稿运行态
- `data/accounts/default/`：只有没有运行中微信号、没有 `last_wx_id`、也没有历史账号数据时才使用
- `wxauto_save/`：wxautox 下载原件和 AI 图片压缩副本缓存，不是长期数据真源；启动时会按 `wxauto_save_cache_retention_days` 配置后台清理，默认 30 天，`0` 表示不清理
- `wxbot_logs/`：面板运行日志
- `backups/data_时间戳/`：面板一键备份产物；`data/backups/` 下的身份迁移报告不是运行时真源

## 当前关键行为边界

- 会话记忆页里的“带入最近聊天”只配置 `最近 N 条`。`memory_context_count` 最小值是 `1`，不要恢复成 `0=关闭上下文`，也不要恢复成按好友消息和机器人消息分开计数。
- 私聊上下文补洞只由聊天记录配置里的两个开关控制：`memory_context_repair_low_risk_switch` 默认开，`memory_context_repair_high_risk_switch` 默认关；不要把冷却、锚点数量、可见读取上限等内部参数重新暴露到 UI。
- 当前自动回复前优先走低风险补洞：读取当前私聊子窗口 `GetAllMessage()` 的最近可见消息，内部上限 `30`，同私聊冷却 `60` 秒；没有启动 / 恢复 / 尾部不匹配这类强原因时，冷却到期会以 `scheduled_low_risk_check` 做一次计划性低风险巡检。
- 锚点只用来判断当前读取结果是否能和本地历史对齐；实际补入按全量去重处理当前读取到的缺失消息，锚点前后的可见缺口都会按时间排序补入 memory。无锚点也会补入，因为更常见原因是本地记录落后太多。该机制必须排除群聊，包括 `chat_type == "group"` 和已配置在 `config.group` 的对象。
- 高风险补洞机制尚未完善，默认关闭；只允许在 `_repair_private_context_before_ai()` 中由低风险无锚点、`memory_context_repair_high_risk_switch=True`、高风险冷却允许、微信操作锁可用共同触发。读取内部上限 `50`，同私聊冷却 `3600` 秒；失败时退回低风险可见结果，不阻断 AI 回复。
- 图片回复按“最终回复接口是否支持视觉”分两条路径：支持视觉时直接把 `image_path/image_paths` 传给回复接口；不支持视觉时先走 `core/vision_bridge.py` 生成结构化视觉笔记，再把视觉笔记贴近当前 user 消息交给主回复模型。`image_parse.md` 只放图片处理规则，不再塞具体图片解析结果。
- 图片消息本地记忆使用 `[图片]` + `image_paths` + `visual_notes`。AI 可见 `history` 统一走 `core/chat_history_format.py::build_model_visible_history(...)` 渲染，不把本地绝对路径喂给 AI；历史图片等媒体消息作为最近 N 条真实消息的一部分保留，不再按媒体条数单独裁剪。语音进入 history 时保留 `[语音]文本`，不带语音时长；语音转文字失败或空内容不作为普通聊天内容污染 history。链接、小程序、视频、名片等保留必要类型壳和语义内容，视频去掉“下载”按钮字样但保留时长。
- `wxauto_save/` 是微信下载原件和 AI 图片压缩副本的统一缓存区。AI 图片副本由 `core/media.py::prepare_ai_image_path(...)` 生成，最长边限制为 `2048`，照片类优先 JPEG，透明图、PNG、BMP、GIF 首帧保留 PNG。机器人启动时会按 `wxauto_save_cache_retention_days` 清理旧文件，并顺手移除变空的子目录；可选值为 `0/7/30/90/180/360`，其中 `0` 表示不清理，不要为 `compress_images/` 等子目录另起独立保留策略。
- 私聊图片、图片+文字和多张图片都进入连续消息合并队列。普通文本静默等待为 `chat_message_merge_delay`，含图批次静默等待为基础值 `* 2`，最大等待仍按基础值 `* 3` 且限制在 9-30 秒；单批最多合并 `9` 张图片。私聊 pending visual context 是 600 秒短期桥接，不靠关键词判断；成功发送图片相关回复后清理，之后围绕同一图片继续聊主要依赖聊天记录 history。
- 群聊图片消息本身只缓存到群级 pending visual context，不直接触发视觉模型。后续群聊文本触发 AI 且命中问图意图时，才消费该群 pending 图片；A 发图、B 问图也应命中。成功发送图片相关回复后清理 pending 图片，新图片覆盖旧图片。
- 群聊图片消息即使在 `group_reply_at=False` 时也不能因为“非仅 @”而直接进 `_reply_group_image_message()`；路由层应先缓存后 `skip`，后续问图文本再进入图片回复管线。引用图片或文本里显式携带图片路径的场景可以按显式图片请求处理。
- 默认聊天接口支持主备切换；白名单好友的专属聊天接口不参与全局主备切换。
- 私聊 `chat_listen_only` 优先级高于关键词回复、轮数超限结束语和普通 AI 回复；命中后会直接结束私聊回复链路，自定义转发除外。
- 运行统计口径：`API请求/API 调用次数` 统计机器人向 AI 大模型和正式 TTS 模型发出的真实请求；`聊天请求` 统计为了回复当前聊天而发起的聊天模型请求，包含 `final_reply.md`、`closing_reply.md` 和图片转文字辅助识别，不包含素材判定、素材附加文案和面板测试/预览。
- 私聊连续消息使用每好友批次流水线；同一好友只跑一个 AI 回复流程，新消息进入下一批，不会取消正在生成的批次。普通私聊 AI 回复进入发送层后会记录消息序号，拆分段发送前若同一好友又来新消息，就停止上一轮剩余气泡；如果旧回复因子窗口不可用进入轻量延后发送队列，flush 前发现消息序号已变化也会丢弃这轮过期回复。轮数超限结束语和 API 错误兜底尽量保证送达，不按这条中断规则处理。
- 私聊白名单是私聊专属配置的单一真源：白名单里的好友优先命中自己的 `chat_prompt_map / chat_api_map / chat_tts_map`，未进白名单的私聊走全局配置。
- 私聊轮数超限结束语如果在 AI 生成阶段报错，会直接回退到 `api_error_reply`；留空则静默。
- `api_error_reply`、语音转文字失败固定提示、命中元话术固定回复和轮数超限固定回复的“只回复一次”状态都复用 `ReplyCountStore` 的回复循环窗口。新增同类提醒时，不要另起重置时间或独立状态文件。
- 语音消息处理依赖微信自己的「语音自动转文字」结果，运行时不要调用 `msg.to_text()` 主动右键识别；私聊只有时长、暂无文字时只延后 5 秒重读当前可见消息一次，群聊不做延后重读。
- 语音回复属于 AI 自动回复的发送层能力；私聊勾选“收到语音触发”时，会自动带起该账号的私聊处理语音消息开关。
- TTS 模型下拉框的选项由 `core/tts.py::TTS_SDK_REGISTRY` 驱动。豆包语音合成 2.0 标准版和高表现力版都使用 `X-Api-Resource-Id: seed-tts-2.0`，并通过请求体 `req_params.model` 区分 `seed-tts-2.0-standard` / `seed-tts-2.0-expressive`；豆包语音合成 1.0 使用 `seed-tts-1.0`；声音复刻和声音设计音色统一使用 `seed-icl-2.0`。只有 `seed-tts-2.0-expressive` 发送 `context_texts` / `section_id` 上下文扩展字段，其他 TTS 模型不要发送这些字段。
- 语音识别失败固定提示允许空字符串，空字符串表示静默；不要再用默认文案兜底覆盖用户主动清空。
- 运行日志等级按用户可处理程度分层：普通消息处理、会话记忆更新、允许的静默策略和已兜住的失败使用 `INFO`；异常状态恢复或主动操作完成使用 `SUCCESS`；局部失败但机器人仍可继续运行、回复 / 语音回复触发上限使用 `WARNING`；监听器、微信客户端、主线程或核心初始化受损时使用 `ERROR`。
- 定时消息内容模型已经统一成“可选 `1` 条文案 + 最多 `9` 个本地文件”；发送顺序固定为先文案、后文件。
- AI 自动转发只处理有真实新消息的活跃私聊，触发条件是 `判定周期` 和 `判定门槛` 在同一检测窗口内同时满足；每天上限只统计 AI 自动转发当天成功发送次数。
- 素材转发里的 AI 文案已经统一成同一层：无论入口是普通任务还是 `AI自动转发`，最终都走同一套“统一文案”生成逻辑。
- `AI自动转发` 仍是“两段式”链路：先判断当前最适合发哪条素材，再生成最终附加文案；不要把判断和文案生成混成一次模型调用。
- 【素材转发】页固定是 `任务列表 / 素材来源 / 素材管理` 三个二级 tab；`单次任务` 与 `循环任务` 的 `素材来源 / 素材类型` 语义不同，前者是筛选器，后者是真正的随机范围限制。
- 定时消息、素材转发、发圈任务、朋友圈点赞都走统一时间模型；新增任务类面板时优先复用现有任务工作台 contract / storage / service。
- `scheduled_message` 和 `material_outreach` 的内部模型仍是“规则生成运行时实例”；面板对用户展示为 `等待发送 / 等待转发`。`moments` 表达的是“一次明确的发布动作”，面板展示为 `等待发布 / 发布记录`。
- 面板里的发圈任务创建当前拆成两段：先建任务，再异步生成候选文案。
- 发圈文案生成有图片时统一走 `api.chat(..., image_path/image_paths=...)` 图片直传，不走辅助视觉转述路径；管理员 `/发圈` 和面板发圈任务不要再分叉维护专用多图 HTTP 请求。
- 普通定时消息和随机消息优先走已监听的聊天子窗口，找不到时再回退主窗口。
- 动态监听采用轻量按需补窗：普通增删监听不触发微信客户端重绑，不做主循环高频巡检；普通补窗失败只进入 30s/60s 延后重试，同一好友已有延后任务时只合并消息不重复补窗，残留监听登记才允许走轻量延后监听的一次性受控重建。固定监听可低频巡检补回，但不能和全局监听延后补窗抢微信 UI。启动初始化和整体监听恢复仍可重建监听器。
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
- 私聊上下文补洞、history 组装顺序和微信操作锁占用
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
