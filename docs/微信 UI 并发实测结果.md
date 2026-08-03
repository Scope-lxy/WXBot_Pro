# 微信 UI 调度实测结论

本文保留会影响生产设计的 wxautox4 实测结论和复验入口。当前运行规则以《WXBot Pro 开发指南》和 `AGENTS.md` 为准；这里不是并发能力说明书。

## 生产结论

- 全部真实微信动作只经一个 UI owner。业务线程只传纯数据，不能跨线程缓存或复用 `WeChat`、`Chat`、`Message`、`NewFriend` 或 UIA 对象。
- 通讯录完整资料采集只能在 `feature/contact_auto_collector_worker.py` 子进程执行。采集开始到 `SwitchToChat` 恢复完成前，owner 和聊天业务都等待；父进程以 300 秒硬超时和 PID 清理兜底。
- 普通控件超时、目标不匹配或单任务失败只结束或延后该任务。只有 Windows `1400`、COM/RPC 断开等客户端级证据才允许重绑微信。
- 手动关系全量扫描是连续、不可抢占的 owner 独占事务；自动关系扫描只读取当前会话列表。

## 实测依据与边界

2026-07 的 wxautox4 实测说明：不同子窗口的轻量读取或文字发送有时能并行完成，但总耗时没有稳定优势；三路读取明显退化。通讯录与文件、语音或主窗口历史读取叠加时出现长期不返回或控件超时。

| 场景 | 可依赖的结论 |
| --- | --- |
| `GetFriendDetails` | 内部 `timeout` 不能作为进程级截止线；必须使用独立 worker 和父进程硬超时。 |
| 通讯录 worker 强制终止 | 按 PID 终止后没有半成品合并机会，恢复聊天页后可以重新读取和发送。 |
| 多子窗口并发 | 仅证明底层偶尔可执行，不证明适合生产；生产不以这项能力换取更快回复。 |
| 通讯录 callback | 返回 `False` 继续寻找起点；第一次返回 `True` 停止 callback，并从命中项读取，命中项计入数量。名称只能定位，不能证明身份。 |
| 关系全量扫描 | 连续 owner 事务比旧分片滚动更可靠；开始和结束回顶保留稳定等待，中间不插入固定等待。 |

这些结论不外推到未验证的回调瞬间、图片下载、引用发送或长时间高压场景。未验证组合默认串行。

## 更新内核后的复验

升级微信或 wxautox4 后，先确认基础启动和授权，再在微信主窗口可见、UTF-8 环境下运行：

```powershell
$env:PYTHONUTF8='1'
.\venv\Scripts\python.exe -X utf8 tools\probe_wx_ui_lane_matrix_v2.py
.\venv\Scripts\python.exe -X utf8 tools\probe_contact_callback_contract.py
.\venv\Scripts\python.exe -X utf8 tools\probe_contact_worker_preemption.py
.\venv\Scripts\python.exe -X utf8 tools\probe_relationship_scan_timing.py
```

随后人工确认：通讯录批次可恢复聊天页、关系全量扫描能自然结束、监听和发送正常、停止后没有残留 worker。任何一项失败时，保持单 owner 和通讯录全屏障，不根据单次成功放宽并发规则。
