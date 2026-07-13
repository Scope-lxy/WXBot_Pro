from datetime import datetime
import unittest

from tools.ui_conflict_test import (
    build_default_phases,
    build_material_outreach_task,
    build_scheduled_message_task,
)


class UIConflictTestScriptTests(unittest.TestCase):
    def test_build_scheduled_message_task_uses_direct_targets_and_future_fire_time(self):
        task = build_scheduled_message_task(
            contacts=["A", "B", "C", "D"],
            text="hello",
            delay_seconds=10,
            target_count=3,
            phase_label="任务互测",
        )
        self.assertEqual(task["targets_mode"], "direct")
        self.assertEqual(task["targets"], ["A", "B", "C"])
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["repeat_rule"], "custom_dates")
        self.assertGreaterEqual(datetime.fromisoformat(task["fire_at"]), datetime.now())

    def test_build_material_outreach_task_uses_direct_targets(self):
        task = build_material_outreach_task(
            contacts=["A", "B", "C"],
            delay_seconds=5,
            target_count=2,
            phase_label="任务互测",
        )
        self.assertEqual(task["targets"], [])
        self.assertEqual(task["manual_target_names"], ["A", "B"])
        self.assertEqual(task["batch_material_strategy"], "per_batch")
        self.assertEqual(task["preface_mode"], "none")
        self.assertEqual(task["target_selector"]["base"], "manual")
        self.assertGreaterEqual(datetime.fromisoformat(task["fire_at"]), datetime.now())

    def test_default_phases_include_task_only_and_contact_refresh(self):
        phases = build_default_phases(
            {
                "task_only_delay_seconds": 6,
                "maintenance_overlap_delay_seconds": 4,
            }
        )
        self.assertEqual([phase.key for phase in phases], ["tasks_only", "with_contact_refresh"])
        self.assertFalse(phases[0].include_contact_refresh)
        self.assertTrue(phases[1].include_contact_refresh)


if __name__ == "__main__":
    unittest.main()
