# wechat-cli 本地数据集成探测

`tools/wechat_cli_probe.py` 用于评估 `wechat-cli` 是否适合作为 WXBot 的可选只读数据源。

推荐把 `wechat-cli` 可执行文件放在项目现有外部工具目录：

- `venv/tools/wechat-cli/wechat-cli.exe`
- 或 `venv/tools/wechat-cli/bin/wechat-cli.exe`
- 或隔离 Python 工具环境：`venv/tools/wechat-cli/pyenv/Scripts/wechat-cli.exe`

这个位置和 `venv/tools/ffmpeg/` 的管理方式一致。探测脚本会优先搜索这些位置，然后再查环境变量和系统 `PATH`。

## 安全边界

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

## 后续集成原则

- `wechat-cli` 作为可选外部工具，不放进 WXBot 核心依赖。
- 补洞、通讯录读取优先尝试本地只读数据源；失败立即回退 wxautox4。
- 多账号环境不能只相信 `wechat-cli` 自动目录检测：WXBot 会把 wxautox4 识别到的当前账号命名空间和 `wechat-cli` 当前 `db_dir` 做绑定。首次绑定或目录变化时，会向文件传输助手发送一条 `校验时间：YYYY-MM-DD HH:MM:SS` 的简短消息，并用 `wechat-cli search` 命中后才保存绑定。
- wxautox4 未授权、机器人未启动、无法切到文件传输助手或校验消息未被 `wechat-cli` 搜到时，不保存绑定；通讯录和补洞继续回退 wxautox4。
- 本地源只做数据搬运和格式适配：成功提取的数据必须优先按 WXBot 现有字段、标准和 JSON 格式合并，不因为 `wechat-cli` 能拿到更多信息就新增联系人或历史记录字段。
- 只记录可用性和耗时，不记录聊天内容、密钥、数据库内容。
- 发送消息、改备注、打标签等写操作仍由 wxautox4 负责。

## 当前探测结论

- `wechat-cli --help` / `--version` 可用，版本为 `0.2.4`。
- `init` 自动检测未覆盖当前机器的数据目录；手动传入 `db_storage` 后初始化成功。
- 启动脚本会自动安装 `venv/tools/wechat-cli/pyenv`；初始化时只有检测到唯一 `db_storage` 候选才会自动指定目录，多账号候选会跳过自动初始化，避免选错账号。
- 已验证 `contacts --limit` 和 `contacts --detail` 可用，单次耗时约 `300ms`，可作为通讯录读取候选数据源。
- 已验证 `sessions` 和 `unread` 可用，单次耗时约 `300ms`。
- 已验证 `history "私聊名" --limit 20` 可用，单次耗时约 `370ms`，返回结构包含 `chat`、`username`、`count`、`messages` 等字段。
- Windows 下运行真实读库命令时需要设置 `PYTHONIOENCODING=utf-8`，否则包含特殊字符的 JSON 可能被 GBK 控制台编码打断。

## 外部项目发现

- `wechat-cli` 基于 `wechat-decrypt`，当前 `history` 能读取普通消息，但语音消息仍主要显示为 `[语音]` 加 XML 元数据；未直接返回微信语音转写正文。
- `wechat-decrypt` 的语音链路更完整：从 `message/media_*.db` 的 `VoiceInfo.voice_data` 读取 SILK 数据，按 `Name2Id.user_name` 和 `local_id` 定位单条语音。
- `wechat-decrypt` 解码语音时会移除微信私有 `0x02` 前缀，把 SILK 解成 `24kHz/mono/16bit` WAV；另有 `voice_to_mp3.py` 可借助 `pilk`/`ffmpeg` 转 MP3。
- `wechat-decrypt` 的 MCP 工具包含 `get_voice_messages(chat_name, limit, offset, start_time, end_time)`、`decode_voice(chat_name, local_id)`、`transcribe_voice(chat_name, local_id)`，转录结果有本地缓存。
- `wechat-decrypt` 支持三种转录后端：本地 Whisper、OpenAI、whisper.cpp；但 whisper.cpp 的可执行文件名在新版本中可能是 `whisper-cli`，不是 `whisper-cpp`。
- `wechat-cli` 有一个未合并 PR 修复 Windows 4.x 自动目录检测和 GBK 输出崩溃；当前集成层需要自行设置 UTF-8 输出并允许手动指定 `db_storage`。
- `wechat-cli` 有未合并 PR 暴露链接 URL、展开合并转发聊天记录、增加 schema/fields/ndjson/has_more 等 AI 友好能力；后续如果要强化链接和聊天记录转发，可借鉴这些 PR 的实现思路。

## 对 WXBot 的集成启发

- 通讯录可先用 `wechat-cli contacts/detail` 接入，收益最大、风险最低。
- 通讯录在 WXBot 里应按“本地快照”理解，而不是模拟 wxautox4 分批滚动：手动建档一次性提取全部可读联系人；自动维护定期拉取最新快照校准本地资料，成功时不再走游标/分批/微信锁。
- 补洞可先接 `wechat-cli history` 的文本、图片、链接、名片、聊天记录转发占位；本地源不受 UI 可见区域限制，当前内部读取上限可高于 wxautox4 的 `30/50`，但仍要按 WXBot 现有记忆字段归一化，对原始 XML 做清洗后再写入 WXBot 记忆。
- 语音不要直接写 XML。短期保持 `[语音] 一条语音消息（未识别出文字）`；中期可增加 `VoiceInfo` 查询与转录缓存，但默认关闭。
- 后续可在 WXBot 自己的适配层实现一个 `get_voice_messages`/`decode_voice` 的极简版本，而不是直接引入整个 `wechat-decrypt` 运行时。
