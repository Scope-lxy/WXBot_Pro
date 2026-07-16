import os
import tempfile
import time
import unittest

from PIL import Image

from core.media import (
    AI_COMPRESSED_IMAGE_DIRNAME,
    AI_IMAGE_MAX_LONG_SIDE,
    cleanup_media_cache,
    prepare_ai_image_path,
)


class MediaHelperTests(unittest.TestCase):
    def test_prepare_ai_image_path_writes_compressed_copy_under_wxbot_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            wxbot_save = os.path.join(tmp, "wxbot_save")
            os.makedirs(wxbot_save, exist_ok=True)
            original_path = os.path.join(wxbot_save, "poster.png")
            Image.new("RGB", (3000, 1800), (40, 120, 220)).save(original_path)

            prepared_path = prepare_ai_image_path(original_path)

            self.assertNotEqual(prepared_path, original_path)
            self.assertTrue(os.path.isfile(original_path))
            self.assertTrue(os.path.isfile(prepared_path))
            self.assertEqual(os.path.basename(os.path.dirname(prepared_path)), AI_COMPRESSED_IMAGE_DIRNAME)
            self.assertTrue(prepared_path.startswith(os.path.join(wxbot_save, AI_COMPRESSED_IMAGE_DIRNAME)))
            with Image.open(prepared_path) as prepared:
                self.assertLessEqual(max(prepared.size), AI_IMAGE_MAX_LONG_SIDE)
                self.assertEqual(prepared.format, "PNG")

    def test_prepare_ai_image_path_uses_jpeg_for_photo_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            wxbot_save = os.path.join(tmp, "wxbot_save")
            os.makedirs(wxbot_save, exist_ok=True)
            original_path = os.path.join(wxbot_save, "photo.jpg")
            Image.new("RGB", (3000, 1800), (40, 120, 220)).save(original_path, format="JPEG")

            prepared_path = prepare_ai_image_path(original_path)

            self.assertTrue(prepared_path.endswith(".jpg"))
            with Image.open(prepared_path) as prepared:
                self.assertLessEqual(max(prepared.size), AI_IMAGE_MAX_LONG_SIDE)
                self.assertEqual(prepared.format, "JPEG")

    def test_prepare_ai_image_path_uses_png_for_gif_first_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            wxbot_save = os.path.join(tmp, "wxbot_save")
            os.makedirs(wxbot_save, exist_ok=True)
            original_path = os.path.join(wxbot_save, "motion.gif")
            Image.new("RGB", (1200, 800), (255, 255, 255)).save(original_path, format="GIF")

            prepared_path = prepare_ai_image_path(original_path)

            self.assertTrue(prepared_path.endswith(".png"))
            with Image.open(prepared_path) as prepared:
                self.assertEqual(prepared.format, "PNG")

    def test_prepare_ai_image_path_keeps_transparent_images_lossless(self):
        with tempfile.TemporaryDirectory() as tmp:
            wxbot_save = os.path.join(tmp, "wxbot_save")
            os.makedirs(wxbot_save, exist_ok=True)
            original_path = os.path.join(wxbot_save, "transparent.png")
            Image.new("RGBA", (1200, 800), (255, 0, 0, 128)).save(original_path, format="PNG")

            prepared_path = prepare_ai_image_path(original_path)

            self.assertTrue(prepared_path.endswith(".png"))
            with Image.open(prepared_path) as prepared:
                self.assertEqual(prepared.format, "PNG")

    def test_cleanup_media_cache_deletes_old_files_recursively(self):
        with tempfile.TemporaryDirectory() as tmp:
            wxbot_save = os.path.join(tmp, "wxbot_save")
            nested_dir = os.path.join(wxbot_save, "compress_images", "nested")
            os.makedirs(nested_dir, exist_ok=True)
            old_file = os.path.join(nested_dir, "old.jpg")
            new_file = os.path.join(wxbot_save, "new.jpg")
            with open(old_file, "wb") as file:
                file.write(b"old")
            with open(new_file, "wb") as file:
                file.write(b"new")
            old_time = time.time() - 31 * 24 * 60 * 60
            os.utime(old_file, (old_time, old_time))

            stats = cleanup_media_cache(wxbot_save, retention_days=30)

            self.assertFalse(os.path.exists(old_file))
            self.assertTrue(os.path.isfile(new_file))
            self.assertEqual(stats["deleted_files"], 1)
            self.assertGreaterEqual(stats["deleted_dirs"], 1)
            self.assertFalse(stats["skipped"])

    def test_cleanup_media_cache_skips_non_wxbot_save_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = os.path.join(tmp, "cache")
            os.makedirs(cache_dir, exist_ok=True)
            old_file = os.path.join(cache_dir, "old.jpg")
            with open(old_file, "wb") as file:
                file.write(b"old")
            old_time = time.time() - 31 * 24 * 60 * 60
            os.utime(old_file, (old_time, old_time))

            stats = cleanup_media_cache(cache_dir, retention_days=30)

            self.assertTrue(os.path.isfile(old_file))
            self.assertTrue(stats["skipped"])
