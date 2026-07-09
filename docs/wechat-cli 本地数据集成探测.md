# wechat-cli 本地数据集成

`wechat-cli` 是 WXBot 当前集成的本地微信数据库只读数据源，主要用于加速通讯录维护、私聊 / 群聊聊天记录自动补全和关系扫描。`tools/wechat_cli_probe.py` 仍可用于只读探测工具是否可执行，但日常运行优先依赖启动脚本和状态面板自动检测。

本地 CLI 读取默认禁用。需要加速相关功能时，可以在状态面板 `wechat-cli 当前状态` 卡片确认风控风险后点击绿色 `开启 CLI` 按钮；也可以通过配置 `wechat_cli_enabled=true` 显式启用。禁用后，启动脚本不会安装、初始化或检测 `wechat-cli`，Python 读取层也会短路所有 contacts / history / sessions / status / update 调用。`WXBOT_DISABLE_WECHAT_CLI=1` 可作为更高优先级的强制禁用环境变量。

推荐把 `wechat-cli` 可执行文件放在项目现有外部工具目录：

- `venv/tools/wechat-cli/wechat-cli.exe`
- 或 `venv/tools/wechat-cli/bin/wechat-cli.exe`
- 或隔离 Python 工具环境：`venv/tools/wechat-cli/pyenv/Scripts/wechat-cli.exe`

这个位置和 `venv/tools/ffmpeg/` 的管理方式一致。CLI 开关启用时，探测脚本会优先搜索这些位置，然后再查环境变量和系统 `PATH`；禁用时探测脚本会直接退出，不执行任何 `wechat-cli` 命令。

## 探测脚本安全边界

- 只执行 `wechat-cli --help` 和 `wechat-cli --version`。
- 不执行 `wechat-cli init`。
- 不读取聊天记录、通讯录、会话、未读消息或新增消息。
- 不访问微信进程内存，不解密数据库，不写入 WXBot 主配置。

## 使用方式

当前推荐隔离安装方式：

```powershell
.\venv\Scripts\python.exe -X utf8 -m venv .\venv\tools\wechat-cli\pyenv
.\venv\tools\wechat-cli\pyenv\Scripts\python.exe -m pip install "git+https://github.com/huohuoer/wechat-cli.git"
```

备注：`npm install @canghe_ai/wechat-cli` 当前可装到 JS 包装器，但 Windows 预编译二进制子包不可用；本项目先使用隔离 Python 工具环境。

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
.\venv\Scripts\python.exe -X utf8 .\tools\wechat_cli_probe.py
```

如果 `wechat-cli` 不在 `PATH`，可显式指定：

```powershell
.\venv\Scripts\python.exe -X utf8 .\tools\wechat_cli_probe.py --exe "C:\path\to\wechat-cli.exe"
```

输出 JSON：

```powershell
.\venv\Scripts\python.exe -X utf8 .\tools\wechat_cli_probe.py --json
```

## 当前集成原则

- `wechat-cli` 作为项目级外部工具，安装在 `venv/tools/wechat-cli/`，不放进 WXBot 主 `venv` 的业务依赖清单。
- 只有 CLI 开关启用且账号校验通过时，通讯录维护、聊天记录自动补全和关系扫描才优先尝试本地只读数据源；失败时按具体场景处理，不能把偶发失败缓存成“永久不可用”。
- 多账号环境不能只相信 `wechat-cli` 自动目录检测：WXBot 会把 wxautox4 识别到的当前账号命名空间和 `wechat-cli` 当前 `db_dir` 做绑定。首次绑定或目录变化时，会向文件传输助手发送一条 `校验时间：YYYY-MM-DD HH:MM:SS` 的简短消息，并用 `wechat-cli search` 命中后才保存绑定。
- wxautox4 未授权、机器人未启动、无法切到文件传输助手或校验消息未被 `wechat-cli` 搜到时，不保存绑定；补洞和手动通讯录入口可按原 wxautox4 路径兜底，自动通讯录维护和自动关系扫描只告警并跳过本轮。
- 本地源只做数据搬运和格式适配：成功提取的数据必须优先按 WXBot 现有字段、标准和 JSON 格式合并，不因为 `wechat-cli` 能拿到更多信息就新增联系人或历史记录字段。
- 只记录可用性、失败原因和补洞结果，不记录聊天内容、密钥、数据库内容；正常 CLI 补洞成功不输出原始目标、耗时等完整诊断详情。
- 发送消息、改备注、打标签等写操作仍由 wxautox4 负责。

## 当前集成结论

- `wechat-cli --help` / `--version` 可用，版本为 `0.2.4`。
- `init` 自动检测未覆盖当前机器的数据目录；手动传入 `db_storage` 后初始化成功。
- 只有 CLI 开关显式启用后，启动脚本才会自动安装 `venv/tools/wechat-cli/pyenv`；初始化时只有检测到唯一 `db_storage` 候选才会自动指定目录，多账号候选会跳过自动初始化，避免选错账号。
- 已验证 `contacts --limit` 和 `contacts --detail` 可用，单次耗时约 `300ms`，可作为通讯录读取候选数据源。
- 已验证 `sessions` 和 `unread` 可用，单次耗时约 `300ms`。
- 已验证 `history "私聊名" --limit 20` 可用，单次耗时约 `370ms`，返回结构包含 `chat`、`username`、`count`、`messages` 等字段。
- Windows 下运行真实读库命令时需要设置 `PYTHONIOENCODING=utf-8`，否则包含特殊字符的 JSON 可能被 GBK 控制台编码打断。

## 当前运行参数

- 通讯录：WXBot 保存最多 `10000` 个好友；CLI 开启且账号校验通过时，底层会向 `wechat-cli contacts` 请求最多 `30000` 条原始行，用于过滤系统号、公众号后尽量拿满好友。CLI 基础资料会直接合并进通讯录档案，开启后按需检查，通讯录为空或距离上次 CLI 同步超过面板 `每轮维护间隔` 时自动执行；完整资料维护仍走微信界面自动维护，但 UI 读取隔离在最小采集子进程里，主进程负责超时终止和切回聊天页。
- 上下文补洞：聊天记录页 `自动补最近上下文` 开启且 CLI 开关启用时，私聊和群聊都会优先用 CLI 读取本地历史；读取条数按 `memory_context_count` 归一到最近 `60 ~ 210` 条，只把缺失记录补进 WXBot 现有聊天记录 JSON，不整套替换。私聊只在启动首次回复、恢复 / 身份迁移首次回复、当前消息缺失于本地尾部等强理由下补洞，不做单纯按冷却时间触发的周期巡检。补入前过滤未识别语音占位，并跳过 10 分钟内同方向、同类型、同内容的近时间重复。
- 补洞兜底：CLI 连续两次最终失败后，才读取当前微信窗口已渲染消息做低风险 UI 兜底；`补上下文时允许滚动微信窗口` 开启且低风险仍无法对齐时，才允许滚动微信窗口读取更多历史。私聊可用可见名和锚点消歧，群聊先走 `wechat-cli sessions` 解析到 `@chatroom`；找不到目标、目标不唯一或本地历史没有锚点时不猜测 CLI 目标。
- 补洞消歧：同名私聊联系人先查最多 `100` 个联系人候选，再用最近 `100` 个会话缩小范围；重名群聊从最近 `100` 个会话里找同名群候选。候选超过 `5` 个直接放弃 CLI；候选历史最多各读 `50` 条，必须连续命中最近 `4` 条锚点才允许定位到目标 wxid / `@chatroom`。
- 关系扫描：CLI 开启时，自动 CLI 扫描 `1000` 会话 / `6000` 秒；手动立即扫描 `1000` 会话；手动全量扫描最多 `10000` 会话。禁用 CLI 时手动扫描回退微信界面，后台自动扫描轻量读取当前会话列表，微信忙或无法取得操作锁时跳过本轮。删除 / 拉黑判断只看会话最后一条消息。
- 状态面板：`wechat-cli 当前状态` 提供总开关；默认关闭时红色提示，按钮为绿色 `开启 CLI`，启用前弹出风控确认；开启后可用状态为绿色提示，按钮为红色 `关闭 CLI`，关闭不需要确认。禁用时不检测、不修复、不检查更新。`检查更新` 只提示远端是否有新提交，不自动更新。

## 外部项目发现

- `wechat-cli` 基于 `wechat-decrypt`，当前 `history` 能读取普通消息，但语音消息仍主要显示为 `[语音]` 加 XML 元数据；未直接返回微信语音转写正文。
- `wechat-decrypt` 的语音链路更完整：从 `message/media_*.db` 的 `VoiceInfo.voice_data` 读取 SILK 数据，按 `Name2Id.user_name` 和 `local_id` 定位单条语音。
- `wechat-decrypt` 解码语音时会移除微信私有 `0x02` 前缀，把 SILK 解成 `24kHz/mono/16bit` WAV；另有 `voice_to_mp3.py` 可借助 `pilk`/`ffmpeg` 转 MP3。
- `wechat-decrypt` 的 MCP 工具包含 `get_voice_messages(chat_name, limit, offset, start_time, end_time)`、`decode_voice(chat_name, local_id)`、`transcribe_voice(chat_name, local_id)`，转录结果有本地缓存。
- `wechat-decrypt` 支持三种转录后端：本地 Whisper、OpenAI、whisper.cpp；但 whisper.cpp 的可执行文件名在新版本中可能是 `whisper-cli`，不是 `whisper-cpp`。
- `wechat-cli` 有一个未合并 PR 修复 Windows 4.x 自动目录检测和 GBK 输出崩溃；当前集成层需要自行设置 UTF-8 输出并允许手动指定 `db_storage`。
- `wechat-cli` 有未合并 PR 暴露链接 URL、展开合并转发聊天记录、增加 schema/fields/ndjson/has_more 等 AI 友好能力；后续如果要强化链接和聊天记录转发，可借鉴这些 PR 的实现思路。

## 后续增强方向

- 通讯录已经按“单一通讯录档案 + 来源分层字段”接入：CLI 开启时手动建档优先提取 CLI 可读联系人并直接合并进通讯录，自动 CLI 同步按 X 天维护昵称、备注、wxid；禁用时手动建档和 UI 自动维护都走微信界面，继续负责地区、来源、标签等完整资料。
- 补洞已经接入 `wechat-cli history` 的文本、图片、链接、名片、聊天记录转发占位；CLI 开启时，本地源不受 UI 可见区域限制，但仍按 WXBot 现有记忆字段归一化，对原始 XML 做清洗后再写入 WXBot 记忆。
- 语音不要直接写 XML；`wechat-cli history` 读到的未识别语音占位不写入补洞 memory。中期可增加 `VoiceInfo` 查询与转录缓存，但默认关闭。
- 后续可在 WXBot 自己的适配层实现一个 `get_voice_messages`/`decode_voice` 的极简版本，而不是直接引入整个 `wechat-decrypt` 运行时。
