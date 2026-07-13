import errno
import unittest
from unittest.mock import patch

from core.atomic_storage import replace_with_retry


class AtomicStorageTests(unittest.TestCase):
    def test_transient_access_error_is_retried(self):
        error = PermissionError(errno.EACCES, "file busy")
        with (
            patch("core.atomic_storage.os.replace", side_effect=[error, None]) as replace,
            patch("core.atomic_storage.time.sleep") as sleep,
        ):
            replace_with_retry("source.tmp", "target.json", delays=(0.01,))

        self.assertEqual(replace.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_persistent_access_error_is_raised_after_bound(self):
        error = PermissionError(errno.EACCES, "file busy")
        with (
            patch("core.atomic_storage.os.replace", side_effect=error) as replace,
            patch("core.atomic_storage.time.sleep") as sleep,
            self.assertRaises(PermissionError),
        ):
            replace_with_retry("source.tmp", "target.json", delays=(0.01, 0.02))

        self.assertEqual(replace.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.02])

    def test_non_transient_error_is_not_retried(self):
        error = FileNotFoundError(errno.ENOENT, "missing")
        with (
            patch("core.atomic_storage.os.replace", side_effect=error) as replace,
            patch("core.atomic_storage.time.sleep") as sleep,
            self.assertRaises(FileNotFoundError),
        ):
            replace_with_retry("source.tmp", "target.json")

        replace.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
