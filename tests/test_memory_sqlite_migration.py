import json
import tempfile
import unittest
from pathlib import Path
from core.memory import MemoryManager


class MemorySQLiteMigrationTests(unittest.TestCase):
    @staticmethod
    def _legacy_file(base, conversation="张三"):
        directory = Path(base) / "accounts" / "wxid" / "memory" / conversation
        directory.mkdir(parents=True)
        (directory / "name.json").write_text(
            json.dumps({"name": conversation}, ensure_ascii=False),
            encoding="utf-8",
        )
        return directory / f"{conversation}_memory.json"

    def test_legacy_json_is_not_imported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._legacy_file(tmp)
            path.write_text(
                json.dumps(
                    [
                        {
                            "time": "2026/07/03 05:00:00",
                            "type": "text",
                            "attr": "friend",
                            "sender": "张三",
                            "content": "好",
                        },
                        {
                            "time": "2026/07/03 05:00:00",
                            "type": "text",
                            "attr": "friend",
                            "sender": "张三",
                            "content": "好",
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            first = MemoryManager("wxid", tmp)
            self.assertEqual(first.get_messages("张三", 10), [])
            self.assertTrue(path.exists())

    def test_new_history_and_visual_notes_only_use_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = MemoryManager("wxid", tmp)
            image_path = r"C:\tmp\photo.png"

            manager.message_store.append_history([{
                "event_id": "image-1",
                "conversation": "张三",
                "chat_type": "private",
                "direction": "friend",
                "sender": "张三",
                "content": "[图片]",
                "original_content": image_path,
                "message_type": "image",
                "native_attr": "friend",
                "received_at": 1.0,
                "metadata": {"image_paths": [image_path]},
            }])
            self.assertTrue(
                manager.attach_visual_notes("张三", [image_path], ["一张照片"])
            )

            history = manager.get_messages("张三", 10)
            self.assertEqual(history[0]["content"], "[图片]")
            self.assertEqual(history[0]["visual_note"], "一张照片")
            self.assertFalse((Path(tmp) / "accounts" / "wxid" / "memory").exists())


if __name__ == "__main__":
    unittest.main()
