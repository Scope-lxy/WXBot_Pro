## 项目约定

- 默认用中文沟通；先说明用户能感知到的变化、风险和结果。
- 启动入口是 `打开软件.bat`；面板入口是 `web_server.py`；机器人主编排是 `wxbot_core.py`。
- `README.md` 写总览，使用说明写面板操作，开发指南写实现与验证，设计规范写 UI 约束。
- 这是个人自用 fork：优先当前体验和代码真相，不为旧配置或旧机制增加兼容层。

## 不可破坏的边界

- 所有真实微信动作只能经唯一 UI owner；wxautox `Chat` / `Message` / `NewFriend` / UIA 对象不得跨线程。
- 控件超时或单任务失败只结束 / 延后该任务；只有 Windows `1400`、COM/RPC 断开等客户端级证据才允许重绑微信客户端。
- 通讯录完整资料采集只能由 `feature/contact_auto_collector_worker.py` 子进程执行；主进程负责 owner 屏障、300 秒硬超时、PID 清理和 `SwitchToChat` 恢复。
- `message_store.sqlite3` 是消息事实、会话版本、回复和投递边界的唯一真源；发送、好友申请、素材转发一旦结果未知必须标记 `uncertain`，禁止自动重发。
- 通讯录档案只保存联系人真源字段；`display_name` / `send_name` 只能作为运行记录快照。
- 内部会话类型只能是 `private / group`；wxautox 的 `friend` 只在 `ConversationRef` 入口转换，不能混同消息方向语义。
- 全局扫描无消息时必须返回正常空批次并进入空闲轮询；不得因空结果的 `chat_type=None` 记录或跳过“不支持会话”，只有携带实际消息的非 `private / group` 会话才跳过。
- 素材来源只配置会话名。启动和恢复时必须按微信实际窗口自动识别 `private / group`，不得默认私聊或要求重复加入普通监听名单。
- 生产环境不使用 `wechat-cli` 或本地数据库旁路，也不调用 `msg.to_text()`。

## 开发与文档

- PowerShell 中文乱码先按显示编码处理；读中文文件和 wxautox4 实测使用 UTF-8。
- 改运行链路、数据边界、面板流程或 UI 时，同步对应 README / `docs/`；不要把实现流水账塞进本文件。
- 监听、扫描、补窗、消息恢复和实机验证细节以 `docs/WXBot Pro 开发指南.md` 为准。
