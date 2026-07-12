import os
import tempfile
import unittest
from unittest import mock

from core import logger
from core.logger import format_log_message, format_panel_log_message


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

    def test_panel_keeps_only_error_summary_for_multiline_traceback(self):
        self.assertEqual(
            format_panel_log_message(
                "后台线程异常：监听线程退出\nTraceback (most recent call last):\nRuntimeError: boom",
                level="ERROR",
            ),
            "后台线程异常：监听线程退出（详情见本地日志）",
        )

    def test_panel_summary_is_limited_to_one_hundred_characters(self):
        summary = format_panel_log_message("回" * 120)

        self.assertEqual(len(summary), 100)
        self.assertEqual(summary, "回" * 99 + "…")

    def test_panel_truncation_does_not_truncate_local_log_file(self):
        full_message = "回复" * 80
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(logger, "LOG_PATH", temp_dir), mock.patch.object(
                logger, "_is_test_process", return_value=True
            ):
                with logger._log_lock:
                    original = list(logger.log_messages)
                    logger.log_messages.clear()
                try:
                    logger.log(message=full_message)
                    self.assertEqual(len(logger.get_recent_logs(limit=1)[0]["message"]), 100)
                    path = os.path.join(temp_dir, "tests", "log_" + logger.datetime.now().strftime("%y%m%d") + ".txt")
                    with open(path, encoding="utf-8-sig") as log_file:
                        self.assertIn(full_message, log_file.read())
                finally:
                    with logger._log_lock:
                        logger.log_messages[:] = original

    def test_debug_is_file_only_and_test_logs_use_separate_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(logger, "LOG_PATH", temp_dir), mock.patch.object(
                logger, "_is_test_process", return_value=True
            ):
                with logger._log_lock:
                    original = list(logger.log_messages)
                    logger.log_messages.clear()
                try:
                    logger.log(level="DEBUG", message="内部诊断")
                    self.assertEqual(logger.get_recent_logs(), [])
                    path = os.path.join(temp_dir, "tests", "log_" + logger.datetime.now().strftime("%y%m%d") + ".txt")
                    self.assertTrue(os.path.isfile(path))
                    with open(path, encoding="utf-8-sig") as log_file:
                        self.assertIn("[DEBUG]: 内部诊断", log_file.read())
                finally:
                    with logger._log_lock:
                        logger.log_messages[:] = original

    def test_panel_log_uses_short_time_and_chinese_ui_keeps_internal_level(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.object(logger, "LOG_PATH", temp_dir), mock.patch.object(
                logger, "_is_test_process", return_value=True
            ):
                with logger._log_lock:
                    original = list(logger.log_messages)
                    logger.log_messages.clear()
                try:
                    logger.log(level="SUCCESS", message="监听器已就绪")
                    item = logger.get_recent_logs(limit=1)[0]
                    self.assertRegex(item["time"], r"^\d{2}:\d{2}:\d{2}$")
                    self.assertEqual(item["level"], "SUCCESS")
                    self.assertEqual(item["message"], "监听器已就绪")
                finally:
                    with logger._log_lock:
                        logger.log_messages[:] = original


if __name__ == "__main__":
    unittest.main()
