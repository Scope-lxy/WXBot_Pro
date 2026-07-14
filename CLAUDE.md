## 项目速记

- 默认用中文沟通。先讲用户能感知到的变化、风险和结果，再补必要技术细节。
- 启动入口是 `打开软件.bat`；面板入口是 `web_server.py`；机器人主编排是 `wxbot_core.py`。
- 文档分工：`README.md` 写总览，`docs/WXBot Pro 使用说明.md` 写面板操作，`docs/WXBot Pro 开发指南.md` 写代码真相，`docs/WXBot Pro 设计规范.md` 写 UI 约束。
- 本项目是个人自用 fork，本地数据可以清洗迁移；不要为了旧版本、旧配置、旧机制加没意义的兼容层。

## 微信自动化

- 微信主窗口需要保持可见。做 wxautox4 实机测试前，先确保 Python 使用 UTF-8，否则中文昵称可能变成 `??????`，测试结果不可信。
- 推荐命令：`$env:PYTHONUTF8='1'; .\venv\Scripts\python.exe -X utf8 ...`
- 测试好友可用：阿英2、阿英3、阿英4、炳3、炳4。
- 动态监听按需补窗：失败先按 30s/60s、之后按 60s 重试，持续 600s 后标记降级并改为每 300s 低频恢复；窗口 supervisor 只保存会话和错误状态，不持有消息。同一会话只保留一个恢复任务；只有“已监听但无子窗口”残留状态允许受控关闭重建。固定监听可低频巡检补回，但不要恢复主循环高频巡检、激进残留窗口强清或普通动态监听增删时重绑微信客户端。
- 普通业务失败不得重绑整个微信客户端。控件超时、目标不匹配、单个资料页失败只结束或延后当前任务；监听器异常先探活现有客户端并只重建监听器。只有明确的客户端级失效证据（例如 Windows 1400 无效窗口句柄、COM/RPC 客户端已断开），或专用 watchdog 确认后续任务无法继续时，才允许重绑。
- 备注和标签编辑沿用实机验证过的 `ChatWith -> ChatInfo -> EditFriendInfo`，但必须在 owner 内保留旧版关键稳定步骤：操作前和搜索后把微信主窗口置前，搜索后及编辑前把鼠标移到主窗口中央；不要只搬三个 wxautox 调用而删掉焦点管理。
- 通讯录自动维护的完整资料采集必须走 `feature/contact_auto_collector_worker.py` 最小子进程；主进程负责 UI owner 全屏障、300s 硬超时、PID 级 kill/taskkill 兜底和 `SwitchToChat` 恢复，不要把 `GetFriendDetails` 搬回主进程；`tools/` 只放临时、测试性质辅助。
- 通讯录档案 `contacts.json` 只存联系人真源字段；`display_name` / `send_name` 只允许作为任务运行记录的历史快照，不要写回通讯录档案。
- 生产环境不安装、初始化、检测或读取 `wechat-cli`，不要恢复本地数据库旁路。生产路径禁止 `msg.to_text()`；只有时长的语音在约 5 秒、10 秒用普通 `GetAllMessage()` 新快照重读。
- 全部真实微信动作归唯一 UI owner；wxautox `Chat` / `Message` / `NewFriend` / UIA 对象不得跨线程。手动关系全量扫描是连续、不可抢占的独占事务，不提供中途停止；1000 次滚动只作失控保险。
- 正确回复、可信 history/记忆、完整运营任务和故障可恢复优先于回复速度。通讯录批次从启动到 `SwitchToChat` 恢复期间阻塞全部聊天业务和微信 UI 意图；新消息只以纯数据 FIFO 排队，批次完成或 300 秒硬超时恢复后再处理，不恢复“通讯录期间轻量聊天穿插”。
- 私聊 AI 拆分后的每个气泡都是独立可取消意图，不得用 `SendMsgBatch` 合成不可中断批次。对方新消息或人工 `self` 在纯数据入队时立即推进会话版本；已开始的单个气泡发完，尚未开始的剩余气泡取消。机器人 outbound echo 不推进版本。首条气泡不加额外延迟；私聊和群聊分别控制第二条及后续气泡是否使用固定标准间隔。
- 好友申请、发送和素材转发等非幂等动作一旦进入提交且结果未知，必须标记 `uncertain`，禁止自动重发。通讯录 `contacts.json` 的读改写必须使用路径锁和原子替换，避免关系扫描与面板操作互相覆盖。
- wxautox4 的私聊 `chat_type='friend'` 只允许在 `ConversationRef` 入口转换为内部 `private`；内部和 `message_store.sqlite3` 只接受 `private / group`。消息 `attr='friend'`、入站 `direction='friend'` 仍是方向语义，禁止全局替换。
- 消息事实、聊天记录、会话版本、回复任务和投递边界只写账号级 `message_store.sqlite3`。未完成回复固定 15 分钟有效期；启动只恢复仍有效且未 claim 的任务，遗留 `inflight` 转为 `uncertain` 并取消同轮剩余气泡，正常停止取消全部未 claim 工作。claim 前只有 SQLite `BUSY / LOCKED` 可自动重试，其他代码 / 数据 / 存储错误只失败一次；claim 后结果未知一律 `uncertain`。不要恢复待答 JSON、旧发送队列或旧 echo 列表。

## Windows 编码

- PowerShell 里看到 `鑾峰彇` 这类中文乱码时，先按终端显示问题处理，不要直接重写中文内容。
- 读中文文件优先用 `Get-Content -Encoding UTF8` 或 `venv\Scripts\python.exe -X utf8`。

## 文档同步

- 改启动方式、打包方式、主要页面入口、任务工作台、通讯录/身份机制后，同步更新 `README.md` 和对应 `docs/`。
- `AGENTS.md` 只放会反复踩坑的项目事实，不写开发流水账，不要写具体的功能性备注
