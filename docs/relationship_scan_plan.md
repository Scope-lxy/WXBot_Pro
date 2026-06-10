# 关系扫描开发方案

## 目标

在通讯录页面新增“关系扫描”卡片，用微信主窗口会话列表预览识别好友关系变化，并自动维护本地通讯录状态和微信实际标签。

这次不再使用头像、默认占位图、头像文件大小等特征判断好友关系。通讯录维护只负责读取好友资料和维护本地联系人，不负责判断拉黑/删除。

## 识别规则

- 会话预览包含 `消息已发出，但被对方拒收了。`：标记为 `blocked`，同步微信标签 `拉黑我的人`
- 会话预览包含 `开启了朋友验证` 且包含 `你还不是他` 或 `你还不是她`：标记为 `deleted`，同步微信标签 `删除我的人`
- 当前扫描中明确读到该联系人，且预览不再命中上述异常文本：标记为 `normal`，移除 `拉黑我的人` 和 `删除我的人`
- 没有出现在本轮 `GetSession()` 结果里的联系人：不更新关系状态

正常预览被视为恢复证据；未扫描到不视为恢复证据。

## 扫描模式

### 自动扫描

- 默认开启
- 每 10 秒执行一次，可配置 5-20 秒
- 只调用 `wx.GetSession()`，读取当前已渲染/预渲染的会话列表
- 不滚动、不切窗口、不占用长时间 UI
- 适合后台持续运行

### 手动扫描

- 按钮文案：`立即扫描`
- 执行一次当前会话列表扫描
- 不滚动微信主窗口

### 全量扫描

- 手动触发
- 进入 `wechat_action_lock`
- 使用 `wx.SessionBox.go_top()` 后循环 `GetSession()` + `roll_down()`
- 去重联系人名；连续无新增或达到最大滚动次数后停止
- 会滚动微信主窗口会话列表，运行时应提示用户不要手动操作微信

## 数据设计

建议新增账号级文件：

`data/accounts/<wx_id>/relationship_scan/relationships.json`

结构：

```json
{
  "schema_version": 1,
  "wx_id": "wxid",
  "updated_at": "2026-06-11T02:00:00",
  "settings": {
    "auto_write_contact_directory": true,
    "auto_sync_wechat_tags": true,
    "sync_batch_size": 5,
    "scan_interval_seconds": 10
  },
  "records": [
    {
      "name": "B-成和香",
      "status": "blocked",
      "previous_status": "normal",
      "evidence": "消息已发出，但被对方拒收了。",
      "source": "session_preview",
      "first_seen_at": "2026-06-11T00:42:18",
      "last_seen_at": "2026-06-11T00:42:18",
      "changed_at": "2026-06-11T00:42:18",
      "contact_key": "",
      "contact_matched": false,
      "wechat_sync_status": "pending",
      "wechat_sync_error": ""
    }
  ],
  "events": []
}
```

`events` 用于当天统计，记录关系变化和同步结果：

- `blocked`
- `deleted`
- `recovered`
- `wechat_synced`
- `wechat_sync_failed`

## 本地通讯录同步

配置：`自动写入通讯录`，默认开启。

扫描到关系变化后：

- 如果本地通讯录里能按 `remark / nickname / display_name / send_name` 匹配到联系人，实时更新该联系人的关系字段和标签
- 如果匹配不到，保留在关系扫描记录里，等待后续通讯录维护补全联系人后再合并
- 本地通讯录标签与微信标签保持同一语义：
  - `blocked`：包含 `拉黑我的人`，移除 `删除我的人`
  - `deleted`：包含 `删除我的人`，移除 `拉黑我的人`
  - `normal`：移除两类异常标签

## 微信标签同步

配置：`自动同步微信标签`，默认开启。

同步不在扫描线程里直接执行，避免 `GetSession()` 这种轻操作被真实 UI 修改拖慢。同步器在机器人空闲时运行：

- 每轮最多同步标签数：默认 5，可配置 1-10
- 所有真实标签修改必须进入 `wechat_action_lock`
- 复用已验证的 `modify_friend_tags_via_chat_profile`
- 添加/移除标签统一走“聊天资料页 -> EditFriendInfo”路径
- 信任内核 `EditFriendInfo` 返回的成功结果，不再额外读回验证

同步动作：

- `blocked`：添加 `拉黑我的人`，移除 `删除我的人`
- `deleted`：添加 `删除我的人`，移除 `拉黑我的人`
- `normal`：移除 `拉黑我的人` 和 `删除我的人`

失败时保留 `pending`，记录错误，下轮重试。

## UI 原型

原型文件：

`prototypes/contact-relationship-scan.html`

页面位置：

- 放在“通讯录”页面
- 作为“通讯录维护”下面的第二个卡片

顶部胶囊：

- 自动扫描
- 今天拉黑
- 今天删除
- 今天恢复
- 已同步微信
- 待同步微信

左侧结果区：

- 标题：扫描结果
- 操作按钮：立即扫描、全量扫描、停止扫描
- 全量扫描风险提示
- 结果表：联系人、状态、会话预览、变化时间、自动同步
- 不做 `全部 / 拉黑 / 删除 / 恢复 / 待同步` 筛选

右侧设置区：

- 自动写入通讯录：默认开
- 自动同步微信标签：默认开
- 每轮同步标签：默认 5，可编辑 1-10
- 每次查询间隔：默认 10 秒，可编辑 5-20

## 后端开发落点

建议新增：

- `feature/relationship_scan.py`
  - 纯规则：从 session preview 判断 `blocked/deleted/normal`
  - 存储读写：账号级 relationships.json
  - 本地通讯录合并
  - 待同步队列生成
  - 全量扫描滚动逻辑

- `tests/test_relationship_scan.py`
  - 文案识别规则
  - 正常恢复规则
  - 未出现在扫描结果时不恢复
  - 每联系人保留最新状态
  - 待同步队列去重

需要接入：

- `wxbot_core.py`
  - 主循环每隔配置秒数调用自动扫描
  - 空闲时执行微信标签同步
  - 暴露 bot 方法给 web_server 调用

- `web_server.py`
  - 读取状态接口
  - 保存设置接口
  - 立即扫描接口
  - 全量扫描接口
  - 停止扫描接口

- `templates/dashboard.html`
  - 通讯录页新增第二个卡片
  - 接入状态刷新、按钮事件、设置保存

- `templates/static/dashboard.css`
  - 复用通讯录维护卡片结构
  - 补充结果表和胶囊状态样式

## 清理要求

已经移除通讯录维护里的旧头像判断链路：

- 不再根据头像占位图判断 `拉黑我的人`
- 不再保存好友头像用于关系识别
- 不再保留 `is_default_placeholder_avatar`
- 不再保留头像检测相关单测
- 通讯录维护结果不再返回 `blocked_tag_result`

后续实现时不要重新引入头像识别、头像文件大小判断、PIL 图片识别或占位图阈值。

## 验证计划

代码级：

- `python -X utf8 -m py_compile core/contact_profiles.py feature/contacts.py feature/relationship_scan.py web_server.py`
- `python -X utf8 -m unittest discover -s tests -q`

实机级：

- 当前会话列表扫描能识别已渲染的拉黑/删除联系人
- 普通预览能把异常关系恢复为正常
- 未出现在当前扫描结果的联系人不发生状态变化
- 全量扫描会滚动会话列表并停止在无新增处
- 微信标签同步能自动添加 `拉黑我的人` / `删除我的人`
- 恢复正常后能自动移除这两个标签
- 标签同步运行时不打断已有监听子窗口收发
