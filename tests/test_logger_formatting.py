import unittest

from core.logger import format_log_message


class LogFormattingTest(unittest.TestCase):
    def test_strips_duplicate_leading_timestamp_from_message_body(self):
        self.assertEqual(
            format_log_message(
                '2026/06/12 16:44:41 类型：voice 属性：friend 窗口：B-天仙妹妹 发送人：B-天仙妹妹 - 消息：语音7"秒'
            ),
            '私聊 B-天仙妹妹：收到语音消息，发送人：B-天仙妹妹，内容：语音7"秒',
        )

    def test_formats_chat_completions_success_with_result_first(self):
        self.assertEqual(
            format_log_message("Chat Completions 非流式返回成功：嗯嗯，姐你懂这些👍..."),
            "API返回成功（Chat Completions，非流式），内容：嗯嗯，姐你懂这些👍...",
        )

    def test_formats_api_failure_with_colon_variants(self):
        self.assertEqual(
            format_log_message("Chat Completions API 调用失败 [RateLimitError]: too many requests"),
            "API调用失败（Chat Completions），错误类型：RateLimitError，详情：too many requests",
        )
        self.assertEqual(
            format_log_message("Responses API 调用失败 [APIError]：bad response"),
            "API调用失败（Responses API），错误类型：APIError，详情：bad response",
        )

    def test_formats_reply_split_outcome(self):
        self.assertEqual(
            format_log_message("私聊 B-天仙妹妹 命中句末标点自动拆分，共 2 条"),
            "私聊 B-天仙妹妹：命中句末标点，已自动拆分成 2 条",
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

    def test_formats_keyword_message(self):
        self.assertEqual(
            format_log_message("群组 测试群 关键字消息：下午好"),
            "群组 测试群：命中关键词回复，内容：下午好",
        )

    def test_formats_listener_management_message(self):
        self.assertEqual(
            format_log_message("张三 删除监听返回: ok"),
            "监听管理 张三：删除监听完成",
        )
        self.assertEqual(
            format_log_message("张三 删除监听返回: 未找到监听对象"),
            "监听管理 张三：删除监听结果：未找到监听对象",
        )
        self.assertEqual(
            format_log_message("张三 残留监听子窗口已关闭"),
            "监听管理 张三：残留监听子窗口已关闭",
        )


if __name__ == "__main__":
    unittest.main()
