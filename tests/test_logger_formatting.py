import unittest

from core.logger import format_log_message


class LogFormattingTest(unittest.TestCase):
    def test_strips_duplicate_leading_timestamp_from_message_body(self):
        self.assertEqual(
            format_log_message(
                '2026/06/12 16:44:41 类型：voice 属性：friend 窗口：B-天仙妹妹 发送人：B-天仙妹妹 - 消息：语音7"秒'
            ),
            '私聊 B-天仙妹妹：收到语音消息，内容：语音7"秒',
        )

    def test_formats_module_prefix(self):
        self.assertEqual(
            format_log_message("[通讯录维护] 建档失败：窗口不存在"),
            "通讯录维护：建档失败：窗口不存在",
        )
        self.assertEqual(
            format_log_message("【防锁屏】已恢复 Windows 原有锁屏/黑屏/睡眠策略"),
            "防锁屏：已恢复 Windows 原有锁屏/黑屏/睡眠策略",
        )

    def test_keeps_source_normalized_messages_unchanged(self):
        message = "API返回成功（Chat Completions，非流式），内容：嗯嗯，姐你懂这些👍..."
        self.assertEqual(format_log_message(message), message)

    def test_formats_legacy_listener_delete_result(self):
        self.assertEqual(
            format_log_message("张三 删除监听返回: ok"),
            "监听管理 张三：删除监听完成",
        )
        self.assertEqual(
            format_log_message("张三 删除监听返回: 未找到监听对象"),
            "监听管理 张三：删除监听结果：未找到监听对象",
        )


if __name__ == "__main__":
    unittest.main()
