from types import SimpleNamespace
from unittest.mock import patch

from core.message_pipeline import ConversationRef
from core.wechat_ui_actions import UI_STUCK_EXIT_CODE
from feature import listening
from wxbot_core import WXBot


def _bot():
    return SimpleNamespace()


def test_first_exhaustion_outside_observation_queues_one_listener_rebuild():
    bot = _bot()
    with patch("feature.listening.time.monotonic", return_value=100):
        assert listening.record_listener_recovery_exhausted(
            bot, ConversationRef("甲", "private"),
        ) == "rebuild"

    assert bot._listener_auto_recovery_active
    assert not bot._listener_auto_recovery_force_rebind


def test_client_rebind_evidence_is_not_downgraded_by_a_later_desktop_error():
    bot = _bot()

    assert listening.arm_listener_auto_recovery(
        bot,
        OSError(1400, "GetWindowRect", "无效的窗口句柄。"),
    )
    assert bot._listener_auto_recovery_force_rebind
    assert listening.arm_listener_auto_recovery(
        bot,
        RuntimeError("Find Control Timeout: ListItemControl"),
    )
    assert bot._listener_auto_recovery_force_rebind


def test_observation_only_escalates_after_two_distinct_exact_local_failures():
    bot = _bot()
    listening._begin_listener_recovery_observation(bot, after_rebind=False, now=100)

    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("甲", "private"), now=101,
    ) == "observe"
    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("甲", "private"), now=102,
    ) == "observe"
    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("乙", "group"), now=103,
    ) == "rebind"


def test_success_breaks_consecutive_failures_and_stable_window_decays():
    bot = _bot()
    listening._begin_listener_recovery_observation(bot, after_rebind=False, now=100)
    listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("甲", "private"), now=101,
    )
    listening.note_listener_subwindow_operation(bot, ConversationRef("甲", "private"), now=102)
    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("乙", "group"), now=103,
    ) == "observe"
    listening.note_listener_subwindow_operation(bot, ConversationRef("乙", "group"), now=104)
    listening.note_listener_subwindow_operation(bot, ConversationRef("乙", "group"), now=704)

    assert bot._listener_recovery_observation_started_at == 0.0
    assert bot._listener_recovery_failed_conversations == set()


def test_successful_stable_window_decays_before_the_next_failure():
    bot = _bot()
    listening._begin_listener_recovery_observation(bot, after_rebind=False, now=100)
    listening.note_listener_subwindow_operation(bot, ConversationRef("甲", "private"), now=101)

    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("乙", "group"), now=702,
    ) == "rebuild"
    assert bot._listener_recovery_observation_started_at == 0.0


def test_successful_rebuild_itself_starts_the_stable_window():
    bot = _bot()
    listening._begin_listener_recovery_observation(bot, after_rebind=False, now=100)

    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("乙", "group"), now=701,
    ) == "rebuild"


def test_failure_window_starts_at_first_failure_not_rebuild_time():
    bot = _bot()
    listening._begin_listener_recovery_observation(bot, after_rebind=False, now=100)

    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("甲", "private"), now=699,
    ) == "observe"
    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("乙", "group"), now=701,
    ) == "rebind"


def test_failures_more_than_ten_minutes_apart_start_a_new_sequence():
    bot = _bot()
    listening._begin_listener_recovery_observation(bot, after_rebind=False, now=100)

    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("甲", "private"), now=101,
    ) == "observe"
    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("乙", "group"), now=702,
    ) == "observe"


def test_failures_exactly_ten_minutes_apart_still_escalate():
    bot = _bot()
    listening._begin_listener_recovery_observation(bot, after_rebind=False, now=100)

    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("甲", "private"), now=101,
    ) == "observe"
    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("乙", "group"), now=701,
    ) == "rebind"


def test_post_rebind_second_distinct_failure_requests_bounded_process_recovery():
    bot = _bot()
    listening._begin_listener_recovery_observation(bot, after_rebind=True, now=100)

    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("甲", "private"), now=101,
    ) == "observe"
    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("乙", "group"), now=102,
    ) == "restart"
    assert listening.process_listener_auto_recovery(bot) == "restart"


def test_controlled_restart_waits_for_owner_idle_and_uses_launcher_exit_code():
    bot = WXBot.__new__(WXBot)
    bot._ui_owner = SimpleNamespace(is_idle=lambda: False)

    with patch("wxbot_core.os._exit") as exit_process:
        assert not bot._trigger_controlled_listener_recovery()
        exit_process.assert_not_called()

        bot._ui_owner = SimpleNamespace(is_idle=lambda: True)
        with patch("wxbot_core.log"):
            assert bot._trigger_controlled_listener_recovery()
        exit_process.assert_called_once_with(UI_STUCK_EXIT_CODE)
