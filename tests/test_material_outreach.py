import unittest
from types import SimpleNamespace

from feature.material_outreach import (
    collect_material_source_message,
    rebuild_material_pool_for_source,
)


def msg(msg_type, content):
    return SimpleNamespace(type=msg_type, content=content)


class MaterialOutreachPoolTests(unittest.TestCase):
    def test_collect_refreshes_same_source_and_stable_signature(self):
        existing = {
            "id": "mat_old",
            "source": "文件传输助手",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]相同标题",
            "stable_signature": "link|[链接]相同标题",
            "created_at": "2026-06-01T10:00:00",
            "status": "disabled",
            "ownership": "第三方作品",
            "copy_note": "旧备注",
            "forward_test_status": "failed",
            "last_error": "旧错误",
        }

        pool, entry, material_id = collect_material_source_message(
            [existing],
            "文件传输助手",
            msg("link", "[链接]相同标题"),
            material_id_factory=lambda: "mat_new",
            limit_map={"文件传输助手": 10},
        )

        self.assertEqual(material_id, "mat_old")
        self.assertEqual(len(pool), 1)
        self.assertEqual(entry["id"], "mat_old")
        self.assertEqual(entry["status"], "disabled")
        self.assertEqual(entry["ownership"], "第三方作品")
        self.assertEqual(entry["copy_note"], "旧备注")
        self.assertEqual(entry["forward_test_status"], "failed")
        self.assertEqual(entry["last_error"], "旧错误")

    def test_collect_keeps_same_signature_from_different_source(self):
        existing = {
            "id": "mat_other",
            "source": "其他来源",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]相同标题",
            "stable_signature": "link|[链接]相同标题",
            "created_at": "2026-06-01T10:00:00",
            "status": "active",
        }

        pool, entry, material_id = collect_material_source_message(
            [existing],
            "文件传输助手",
            msg("link", "[链接]相同标题"),
            material_id_factory=lambda: "mat_new",
            limit_map={"文件传输助手": 10, "其他来源": 10},
        )

        self.assertEqual(material_id, "mat_new")
        self.assertEqual(len(pool), 2)
        self.assertEqual(entry["id"], "mat_new")

    def test_stable_signature_refresh_reuses_existing_material_identity(self):
        existing = {
            "id": "mat_old",
            "source": "文件传输助手",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]相同标题",
            "stable_signature": "link|[链接]相同标题",
            "created_at": "2026-06-01T10:00:00",
            "status": "active",
            "ownership": "我的作品",
            "copy_note": "保留这条转发备注",
        }

        pool, entry, material_id = collect_material_source_message(
            [existing],
            "文件传输助手",
            msg("link", "[链接]相同标题"),
            material_id_factory=lambda: "mat_new",
            limit_map={"文件传输助手": 10},
        )

        self.assertEqual(material_id, "mat_old")
        self.assertEqual(entry["id"], "mat_old")
        self.assertEqual(entry["copy_note"], "保留这条转发备注")
        self.assertEqual([item["id"] for item in pool], ["mat_old"])

    def test_rebuild_keeps_latest_duplicate_message_and_old_material_id(self):
        first = msg("link", "[链接]相同标题")
        other = msg("miniapp", "小程序冷亦文集相同标题")
        latest = msg("link", "[链接]相同标题")
        existing = {
            "id": "mat_old",
            "source": "文件传输助手",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]相同标题",
            "stable_signature": "link|[链接]相同标题",
            "created_at": "2026-06-01T10:00:00",
            "status": "active",
            "copy_note": "沿用备注",
        }

        pool, runtime_messages, rebuilt = rebuild_material_pool_for_source(
            [existing],
            "文件传输助手",
            [first, other, latest],
            limit=10,
            limit_map={"文件传输助手": 10},
            material_id_factory=iter(["mat_a", "mat_b", "mat_c"]).__next__,
        )

        self.assertEqual(len(rebuilt), 2)
        self.assertEqual([item["type"] for item in rebuilt], ["miniapp", "link"])
        self.assertEqual(rebuilt[-1]["id"], "mat_old")
        self.assertEqual(rebuilt[-1]["copy_note"], "沿用备注")
        self.assertIs(runtime_messages["mat_old"], latest)
        self.assertEqual([item["id"] for item in pool], ["mat_b", "mat_old"])

    def test_rebuild_uses_latest_existing_duplicate_metadata(self):
        older = {
            "id": "mat_older",
            "source": "文件传输助手",
            "type": "link",
            "type_bucket": "link",
            "content_preview": "[链接]相同标题",
            "stable_signature": "link|[链接]相同标题",
            "created_at": "2026-06-01T10:00:00",
            "status": "active",
            "copy_note": "旧卡片",
        }
        newer = {
            **older,
            "id": "mat_newer",
            "created_at": "2026-06-02T10:00:00",
            "status": "disabled",
            "copy_note": "新卡片",
        }

        _pool, runtime_messages, rebuilt = rebuild_material_pool_for_source(
            [older, newer],
            "文件传输助手",
            [msg("link", "[链接]相同标题")],
            limit=10,
            limit_map={"文件传输助手": 10},
            material_id_factory=lambda: "mat_fresh",
        )

        self.assertEqual(len(rebuilt), 1)
        self.assertEqual(rebuilt[0]["id"], "mat_newer")
        self.assertEqual(rebuilt[0]["status"], "disabled")
        self.assertEqual(rebuilt[0]["copy_note"], "新卡片")
        self.assertIn("mat_newer", runtime_messages)


if __name__ == "__main__":
    unittest.main()
