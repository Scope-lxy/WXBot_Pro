from types import SimpleNamespace
from unittest.mock import patch

from core.message_pipeline import ConversationRef
from core.wechat_recovery import UI_STUCK_EXIT_CODE
from feature import listening
from wxbot_core import WXBot


def _bot():
    return SimpleNamespace()


def _state(bot):
    return listening.listener_recovery_coordinator(bot).state_snapshot()


def test_first_exhaustion_outside_observation_queues_one_listener_rebuild():
    bot = _bot()
    with patch("feature.listening._bot_log") as recovery_log:
        assert listening.record_listener_recovery_exhausted(
            bot, ConversationRef("甲", "private"), now=100,
        ) == "rebuild"

    assert _state(bot).active
    assert not _state(bot).force_rebind
    assert "自恢复【监听窗口重建】开始" in recovery_log.call_args.kwargs["message"]


def test_rebuild_and_rebind_success_logs_are_distinct():
    rebuild_bot = _bot()
    with patch("feature.listening._bot_log") as rebuild_log:
        assert listening.record_listener_recovery_exhausted(
            rebuild_bot, ConversationRef("甲", "private"), now=100,
        ) == "rebuild"
        with patch(
            "feature.listening.probe_listener_recovery_client", return_value=object(),
        ), patch("feature.listening.rebuild_listener_runtime", return_value=True):
            assert listening.process_listener_auto_recovery(rebuild_bot) == "recovered"

    rebuild_messages = [call.kwargs["message"] for call in rebuild_log.call_args_list]
    assert any("自恢复【监听窗口重建】成功" in message for message in rebuild_messages)
    assert not any("自恢复【微信客户端重绑】成功" in message for message in rebuild_messages)

    rebind_bot = _bot()
    listening._begin_listener_recovery_observation(rebind_bot, after_rebind=False, now=100)
    with patch("feature.listening._bot_log") as rebind_log:
        assert listening.record_listener_recovery_exhausted(
            rebind_bot, ConversationRef("甲", "private"), now=101,
        ) == "observe"
        assert listening.record_listener_recovery_exhausted(
            rebind_bot, ConversationRef("乙", "group"), now=102,
        ) == "rebind"
        with patch(
            "feature.listening.probe_listener_recovery_client", return_value=object(),
        ) as probe, patch("feature.listening.rebuild_listener_runtime", return_value=True):
            assert listening.process_listener_auto_recovery(rebind_bot) == "recovered"

    rebind_messages = [call.kwargs["message"] for call in rebind_log.call_args_list]
    assert any("自恢复【微信客户端重绑】开始" in message for message in rebind_messages)
    assert any("自恢复【微信客户端重绑】成功" in message for message in rebind_messages)
    assert probe.call_args.kwargs == {"force_rebind": True}
    assert _state(rebind_bot).after_rebind


def test_client_rebind_evidence_is_not_downgraded_by_a_later_desktop_error():
    bot = _bot()

    assert listening.arm_listener_auto_recovery(
        bot,
        OSError(1400, "GetWindowRect", "无效的窗口句柄。"),
    )
    assert _state(bot).force_rebind
    assert listening.arm_listener_auto_recovery(
        bot,
        RuntimeError("Find Control Timeout: ListItemControl"),
    )
    assert _state(bot).force_rebind


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

    assert _state(bot).observation_started_at == 0.0
    assert _state(bot).failed_conversations == frozenset()


def test_successful_stable_window_decays_before_the_next_failure():
    bot = _bot()
    listening._begin_listener_recovery_observation(bot, after_rebind=False, now=100)
    listening.note_listener_subwindow_operation(bot, ConversationRef("甲", "private"), now=101)

    assert listening.record_move_window_local_recovery_failure(
        bot, ConversationRef("乙", "group"), now=702,
    ) == "rebuild"
    assert _state(bot).observation_started_at == 0.0


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
        with patch("wxbot_core.log") as recovery_log:
            assert bot._trigger_controlled_listener_recovery()
        exit_process.assert_called_once_with(UI_STUCK_EXIT_CODE)
        assert "自恢复【机器人重启】执行" in recovery_log.call_args.kwargs["message"]
