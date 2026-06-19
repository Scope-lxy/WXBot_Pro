你刚才输出的会话记忆提案 JSON 不符合规范。

请只修正格式，不要新增不存在的信息，不要删除原本有效的信息，不要解释，不要输出代码块。

# 合法JSON结构
{
  "add": [
    {"importance": "高", "type": "边界", "content": "不喜欢早上被催。"}
  ],
  "update": [
    {"id": "existing_memory_id", "importance": "中", "type": "偏好", "content": "更喜欢短句和自然聊天。"}
  ],
  "delete": [
    {"id": "existing_memory_id", "reason": "已过时，被更新信息替代"}
  ]
}

# 修正规则
1. 顶层字段只能有：add、update、delete。
2. 三个字段都必须存在；没有内容时输出空数组 []。
3. add 条目必须包含：importance、type、content。
4. update 条目必须包含：id、importance、type、content。
5. delete 条目必须包含：id、reason。
6. importance 只能是：高、中、低。
7. 不要输出 add、update、delete 之外的任何字段。
8. 不要输出空字段，不要输出注释，不要输出额外说明文字。

# 以下是需要修正的提案
{{bad_output}}
