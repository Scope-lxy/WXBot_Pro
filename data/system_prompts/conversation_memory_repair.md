你刚才输出的会话记忆提案 JSON 不符合规范。

请只修正格式，不要新增不存在的信息，不要删除原本有效的信息，不要解释，不要输出 Markdown，不要输出代码块。你必须只输出 1 个 JSON 对象。

# 合法JSON结构
{
  "profile": [
    {"id": "B01", "type": "称呼", "content": "阿眠"}
  ],
  "add": [
    {"importance": "高", "type": "边界", "content": "不喜欢早上被催。"}
  ],
  "update": [
    {"id": "M02", "importance": "中", "type": "偏好", "content": "更喜欢短句和自然聊天。"}
  ],
  "delete": [
    {"id": "M05", "reason": "已过时，被更新信息替代"}
  ]
}

# 修正规则
1. 顶层字段只能有：profile、add、update、delete。
2. 这四个字段都必须存在；没有内容时输出空数组 []。
   其中 profile 输出 [] 表示“不改现有基础信息”，不是清空。
3. profile 条目必须包含：id、type、content。
4. add 条目必须包含：importance、type、content。
5. update 条目必须包含：id、importance、type、content。
6. delete 条目必须包含：id、reason。
7. importance 只能是：高、中、低。
8. 不要输出空字段，不要输出注释，不要输出额外说明文字。

# 以下是需要修正格式的内容
{{bad_output}}