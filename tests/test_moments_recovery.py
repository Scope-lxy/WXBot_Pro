import unittest
from datetime import datetime

from feature.moments_tasks import (
    mark_moments_task_running,
    recover_interrupted_moments_task,
)


class MomentsRecoveryTests(unittest.TestCase):
    def test_interrupted_publish_is_uncertain_and_requires_confirmation(self):
        running = mark_moments_task_running({
            "id": "moment-1",
            "enabled": True,
            "status": "pending",
            "text": "今天的内容",
            "execution_snapshot": {
                "run_id": "run-1",
                "content_summary": "今天的内容",
            },
        }, now=datetime(2026, 7, 11, 10, 0))

        recovered = recover_interrupted_moments_task(
            running,
            now=datetime(2026, 7, 11, 10, 1),
        )

        self.assertEqual(recovered["status"], "pending_confirm")
        self.assertEqual(recovered["execution_result"], "uncertain")
        self.assertTrue(recovered["enabled"])
        self.assertEqual(recovered["execute_after"], "")
        self.assertIn("不会自动重发", recovered["execution_message"])


if __name__ == "__main__":
    unittest.main()
