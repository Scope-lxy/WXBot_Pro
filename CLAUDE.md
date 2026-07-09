## 项目速记

- 默认用中文沟通。先讲用户能感知到的变化、风险和结果，再补必要技术细节。
- 启动入口是 `打开软件.bat`；面板入口是 `web_server.py`；机器人主编排是 `wxbot_core.py`。
- 文档分工：`README.md` 写总览，`docs/WXBot Pro 使用说明.md` 写面板操作，`docs/WXBot Pro 开发指南.md` 写代码真相，`docs/WXBot Pro 设计规范.md` 写 UI 约束。
- 本项目是个人自用 fork，本地数据可以清洗迁移；不要为了旧版本、旧配置、旧机制加没意义的兼容层。

## 微信自动化

- 微信主窗口需要保持可见。做 wxautox4 实机测试前，先确保 Python 使用 UTF-8，否则中文昵称可能变成 `??????`，测试结果不可信。
- 推荐命令：`$env:PYTHONUTF8='1'; .\venv\Scripts\python.exe -X utf8 ...`
- 测试好友可用：阿英2、阿英3、阿英4、炳3、炳4。
- 动态监听是轻量按需补窗：收到消息时复用或补一次子窗口；补窗失败先 30s/60s 轻量延后，之后在 600s 待处理窗口内继续轻量重试；同一好友已有延后任务时只合并消息不重复补窗；只有“已监听但无子窗口”残留状态允许受控关闭重建。固定监听可低频巡检补回，但不要恢复主循环高频巡检、激进残留窗口强清或普通动态监听增删时重绑微信客户端。
- 通讯录自动维护的完整资料采集必须走 `feature/contact_auto_collector_worker.py` 最小子进程；主进程负责微信 UI 锁、300s 硬超时、PID 级 kill/taskkill 兜底和 `SwitchToChat` 恢复，不要把 `GetFriendDetails` 搬回主进程；`tools/` 只放临时、测试性质辅助。
- 通讯录档案 `contacts.json` 只存联系人真源字段；`display_name` / `send_name` 只允许作为任务运行记录的历史快照，不要写回通讯录档案。
- `wechat-cli` 默认禁用，只有 `wechat_cli_enabled=true` 且未设置 `WXBOT_DISABLE_WECHAT_CLI` 时才能安装、初始化、检测或读取；它是 `venv/tools/wechat-cli/` 下的本地只读高速源，只能在账号绑定/活体校验通过后用于通讯录、私聊/群聊补洞和关系扫描；私聊补洞只在启动/恢复/本地尾部缺口等强理由触发，不做周期巡检；同名补洞必须满足 4 条锚点消歧，不能猜 wxid 或把 CLI 额外字段塞进现有 JSON；私聊锚点忽略 self/me 差异，群聊锚点保留发送人；补洞写入前要过滤未识别语音占位，并按同方向/类型/内容 10 分钟近重复去重。

## Windows 编码

- PowerShell 里看到 `鑾峰彇` 这类中文乱码时，先按终端显示问题处理，不要直接重写中文内容。
- 读中文文件优先用 `Get-Content -Encoding UTF8` 或 `venv\Scripts\python.exe -X utf8`。

## 文档同步

- 改启动方式、打包方式、主要页面入口、任务工作台、通讯录/身份机制后，同步更新 `README.md` 和对应 `docs/`。
- `AGENTS.md` 只放会反复踩坑的项目事实，不写开发流水账，不要写具体的功能性备注
