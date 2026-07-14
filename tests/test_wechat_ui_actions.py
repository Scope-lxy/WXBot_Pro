import unittest
import threading
import time
import json
import tempfile
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from core import wechat_ui_actions
from wxbot_core import WXBot


class WechatUiActionsTests(unittest.TestCase):
    def test_config_task_versions_read_account_scoped_rule_truth(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_file = root / "config.json"
            keyword_file = root / "keyword.json"
            config_file.write_text(json.dumps({
                "chat_keyword_switch": True,
                "group_keyword_switch": True,
                "group_keyword_at_only": False,
            }), encoding="utf-8")
            keyword_file.write_text(json.dumps({"旧词": {"text": "旧回复"}}, ensure_ascii=False), encoding="utf-8")
            bot = WXBot.__new__(WXBot)
            bot.config = SimpleNamespace(
                CONFIG_FILE=str(config_file),
                config={},
                _keyword_rules_file=lambda: keyword_file,
            )

            keyword_key, keyword_version = bot._config_ui_task_guard("keyword")
            keyword_file.write_text(json.dumps({"新词": {"text": "新回复"}}, ensure_ascii=False), encoding="utf-8")

            self.assertNotEqual(bot._current_ui_task_version(keyword_key), keyword_version)

    def test_ui_intent_rejects_non_data_objects(self):
        with self.assertRaises(TypeError):
            wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT,
                {"chat": SimpleNamespace(who="张三")},
            )

    def test_owner_executes_all_handlers_on_one_thread(self):
        thread_ids = []
        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda payload: thread_ids.append(threading.get_ident()),
            wechat_ui_actions.UIIntentKind.SEND_FILE: lambda payload: thread_ids.append(threading.get_ident()),
        })
        owner.start()
        try:
            owner.call(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_TEXT, {"text": "a"}), 1)
            owner.call(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_FILE, {"path": "a.txt"}), 1)
        finally:
            owner.stop()

        self.assertEqual(len(set(thread_ids)), 1)
        self.assertEqual(thread_ids[0], owner.owner_thread_id)
        self.assertNotEqual(thread_ids[0], threading.get_ident())

    def test_started_action_is_not_preempted_by_later_action(self):
        started = threading.Event()
        release = threading.Event()
        events = []

        def exclusive(_payload):
            events.append("exclusive-start")
            started.set()
            release.wait(1)
            events.append("exclusive-end")

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.SEND_FILE: exclusive,
            wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda payload: events.append("text"),
        })
        owner.start()
        try:
            first = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_FILE))
            self.assertTrue(started.wait(1))
            second = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_TEXT))
            time.sleep(0.03)
            self.assertEqual(events, ["exclusive-start"])
            release.set()
            first.result(1)
            second.result(1)
        finally:
            owner.stop()

        self.assertEqual(events, ["exclusive-start", "exclusive-end", "text"])

    def test_owner_rejects_reply_that_expires_while_queued(self):
        started = threading.Event()
        release = threading.Event()
        sends = []

        def blocker(_payload):
            started.set()
            release.wait(1)
            return True

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.SEND_FILE: blocker,
            wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda payload: sends.append(payload["text"]),
        })
        owner.start()
        try:
            first = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_FILE))
            self.assertTrue(started.wait(1))
            reply = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT,
                {"conversation": "Alice", "text": "late answer"},
                expires_at=time.time() + 0.02,
            ))
            time.sleep(0.03)
            release.set()
            first.result(1)
            with self.assertRaises(wechat_ui_actions.IntentCancelled):
                reply.result(1)
        finally:
            release.set()
            owner.stop()

        self.assertEqual(sends, [])

    def test_owner_checks_reply_expiry_after_payload_preparation(self):
        sends = []

        def prepare(intent):
            time.sleep(0.03)
            return dict(intent.payload)

        owner = wechat_ui_actions.WeChatUIOwner(
            {wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda payload: sends.append(payload["text"])},
            payload_preparer=prepare,
        )
        owner.start()
        try:
            with self.assertRaises(wechat_ui_actions.IntentCancelled):
                owner.call(wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.SEND_TEXT,
                    {"conversation": "Alice", "text": "late answer"},
                    expires_at=time.time() + 0.02,
                ), 1)
        finally:
            owner.stop()

        self.assertEqual(sends, [])

    def test_callback_bound_action_uses_owner_fifo_but_stays_on_callback_thread(self):
        current_started = threading.Event()
        release_current = threading.Event()
        callback_done = threading.Event()
        events = []
        callback_thread_ids = []

        def current(_payload):
            events.append("current-start")
            current_started.set()
            release_current.wait(1)
            events.append("current-end")

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.SEND_FILE: current,
        })
        owner.start()
        first = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_FILE))
        self.assertTrue(current_started.wait(1))

        def run_callback():
            owner.run_callback_action(
                wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.DOWNLOAD_MEDIA,
                    {"conversation": "张三", "callback_bound_message": True},
                ),
                lambda: (
                    callback_thread_ids.append(threading.get_ident()),
                    events.append("callback-download"),
                ),
            )
            callback_done.set()

        callback_thread = threading.Thread(target=run_callback)
        callback_thread.start()
        try:
            time.sleep(0.03)
            self.assertEqual(events, ["current-start"])
            release_current.set()
            first.result(1)
            self.assertTrue(callback_done.wait(1))
        finally:
            callback_thread.join(1)
            owner.stop()

        self.assertEqual(events, ["current-start", "current-end", "callback-download"])
        self.assertEqual(callback_thread_ids, [callback_thread.ident])
        self.assertNotEqual(callback_thread_ids[0], owner.owner_thread_id)

    def test_callback_bound_action_runs_inline_when_callback_is_already_on_owner_thread(self):
        events = []
        owner = None

        def poll(_payload):
            return owner.run_callback_action(
                wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.DOWNLOAD_MEDIA,
                    {"conversation": "张三", "callback_bound_message": True},
                ),
                lambda: events.append(("download", threading.get_ident())),
            )

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.POLL_MESSAGES: poll,
        })
        owner.start()
        try:
            owner.call(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.POLL_MESSAGES), 1)
        finally:
            owner.stop()

        self.assertEqual(events, [("download", owner.owner_thread_id)])

    def test_full_relationship_scan_holds_owner_until_complete(self):
        scan_started = threading.Event()
        scan_finished = threading.Event()
        events = []

        def full_scan(_payload):
            events.append("scan-start")
            scan_started.set()
            self.assertTrue(scan_finished.wait(1))
            events.append("scan-end")

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.RELATIONSHIP_SCAN: full_scan,
            wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda _payload: events.append("reply"),
        })
        owner.start()
        try:
            scan = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.RELATIONSHIP_SCAN,
                {"mode": "full"},
            ))
            self.assertTrue(scan_started.wait(1))
            reply = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT,
                {"conversation": "阿英2", "text": "稍后回复"},
            ))
            time.sleep(0.03)
            self.assertEqual(events, ["scan-start"])
            self.assertFalse(reply.done)
            scan_finished.set()
            scan.result(1)
            reply.result(1)
        finally:
            owner.stop()

        self.assertEqual(events, ["scan-start", "scan-end", "reply"])

    def test_contact_barrier_holds_auto_relationship_scan_until_recovery(self):
        contact_done = threading.Event()
        events = []
        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.CONTACT_START: lambda _payload: wechat_ui_actions.ContactBatchHandle(
                poll=lambda: (contact_done.is_set(), "contact-result"),
            ),
            wechat_ui_actions.UIIntentKind.CONTACT_RECOVER: lambda _payload: events.append("recover"),
            wechat_ui_actions.UIIntentKind.RELATIONSHIP_SCAN: lambda _payload: events.append("scan") or [],
        }, poll_interval=0.01)
        owner.start()
        try:
            contact = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_START))
            deadline = time.time() + 1
            while not owner.contact_active and time.time() < deadline:
                time.sleep(0.01)
            scan = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.RELATIONSHIP_SCAN,
                {"mode": "current"},
            ))

            time.sleep(0.03)
            self.assertEqual(events, [])
            self.assertFalse(scan.done)

            contact_done.set()
            self.assertEqual(contact.result(1), "contact-result")
            self.assertEqual(scan.result(1), [])
        finally:
            contact_done.set()
            owner.stop()

        self.assertEqual(events, ["recover", "scan"])

    def test_cancel_pending_keeps_current_action_but_drops_queued_send_before_shutdown(self):
        started = threading.Event()
        release = threading.Event()
        events = []

        def current(_payload):
            events.append("current-start")
            started.set()
            release.wait(1)
            events.append("current-end")

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.SEND_FILE: current,
            wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda _payload: events.append("queued-send"),
            wechat_ui_actions.UIIntentKind.SHUTDOWN: lambda _payload: events.append("shutdown"),
        })
        owner.start()
        first = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_FILE))
        self.assertTrue(started.wait(1))
        queued = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_TEXT))
        owner.cancel_pending()
        release.set()
        first.result(1)
        with self.assertRaises(wechat_ui_actions.IntentCancelled):
            queued.result(1)
        owner.call_shutdown(1)
        owner.stop()

        self.assertEqual(events, ["current-start", "current-end", "shutdown"])

    def test_cancel_pending_terminates_contact_start_that_finishes_during_stop(self):
        started = threading.Event()
        release = threading.Event()
        terminated = threading.Event()

        def start_contact(_payload):
            started.set()
            release.wait(1)
            return wechat_ui_actions.ContactBatchHandle(
                poll=lambda: (False, None),
                terminate=lambda: terminated.set(),
            )

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.CONTACT_START: start_contact,
            wechat_ui_actions.UIIntentKind.SHUTDOWN: lambda _payload: True,
        })
        owner.start()
        contact = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_START))
        self.assertTrue(started.wait(1))
        owner.cancel_pending()
        release.set()
        with self.assertRaises(wechat_ui_actions.IntentCancelled):
            contact.result(1)
        self.assertTrue(terminated.wait(1))
        owner.call_shutdown(1)
        owner.stop()

    def test_contact_batch_holds_all_ui_actions_until_recovery(self):
        contact_done = threading.Event()
        events = []

        def start_contact(_payload):
            events.append("contact-start")
            return wechat_ui_actions.ContactBatchHandle(
                poll=lambda: (contact_done.is_set(), "contact-result"),
            )

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.CONTACT_START: start_contact,
            wechat_ui_actions.UIIntentKind.CONTACT_RECOVER: lambda payload: events.append("contact-recover"),
            wechat_ui_actions.UIIntentKind.SEND_FILE: lambda payload: events.append("file"),
            wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda payload: events.append("text"),
        }, poll_interval=0.01)
        owner.start()
        try:
            contact = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_START))
            deadline = time.time() + 1
            while not owner.contact_active and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(owner.contact_active)
            exclusive = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_FILE))
            light = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_TEXT))
            self.assertFalse(exclusive.done)
            self.assertFalse(light.done)
            contact_done.set()
            self.assertEqual(contact.result(1), "contact-result")
            exclusive.result(1)
            light.result(1)
        finally:
            owner.stop()

        self.assertEqual(events, ["contact-start", "contact-recover", "file", "text"])

    def test_contact_batch_logs_only_sanitized_exclusive_chat_queue_event(self):
        contact_done = threading.Event()
        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.CONTACT_START: lambda _payload: wechat_ui_actions.ContactBatchHandle(
                poll=lambda: (contact_done.is_set(), True),
            ),
            wechat_ui_actions.UIIntentKind.CONTACT_RECOVER: lambda _payload: None,
            wechat_ui_actions.UIIntentKind.SEND_AUDIO: lambda _payload: True,
        }, poll_interval=0.01, runtime_id="a" * 32)
        owner.start()
        try:
            contact = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_START))
            deadline = time.time() + 1
            while not owner.contact_active and time.time() < deadline:
                time.sleep(0.01)
            with patch.object(wechat_ui_actions, "log") as log_mock:
                audio = owner.submit(wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.SEND_AUDIO,
                    {"conversation": "敏感昵称", "path": "敏感路径.wav"},
                ))
            logged = str(log_mock.call_args)
            self.assertIn("运行事件：通讯录期间微信 UI 任务已排队", logged)
            self.assertNotIn("敏感昵称", logged)
            self.assertNotIn("敏感路径", logged)
            contact_done.set()
            contact.result(1)
            audio.result(1)
        finally:
            owner.stop()

    def test_contact_queue_telemetry_failure_does_not_lose_ticket(self):
        contact_done = threading.Event()
        sent = []
        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.CONTACT_START: lambda _payload: wechat_ui_actions.ContactBatchHandle(
                poll=lambda: (contact_done.is_set(), True),
            ),
            wechat_ui_actions.UIIntentKind.CONTACT_RECOVER: lambda _payload: None,
            wechat_ui_actions.UIIntentKind.SEND_AUDIO: lambda payload: sent.append(payload["path"]),
        }, poll_interval=0.01, runtime_id="a" * 32)
        owner.start()
        try:
            contact = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_START))
            deadline = time.time() + 1
            while not owner.contact_active and time.time() < deadline:
                time.sleep(0.01)
            with patch.object(wechat_ui_actions, "log", side_effect=OSError("log unavailable")):
                audio = owner.submit(wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.SEND_AUDIO,
                    {"path": "voice.wav"},
                ))
            contact_done.set()
            contact.result(1)
            audio.result(1)
        finally:
            owner.stop()

        self.assertEqual(sent, ["voice.wav"])

    def test_production_call_waits_for_queued_exclusive_without_late_execution(self):
        contact_done = threading.Event()
        sent = []
        call_result = []
        call_error = []
        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.CONTACT_START: lambda _payload: wechat_ui_actions.ContactBatchHandle(
                poll=lambda: (contact_done.is_set(), "contact-result"),
            ),
            wechat_ui_actions.UIIntentKind.CONTACT_RECOVER: lambda _payload: None,
            wechat_ui_actions.UIIntentKind.SEND_AUDIO: lambda payload: sent.append(payload["path"]) or "sent",
        }, poll_interval=0.01)
        owner.start()
        contact = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_START))
        deadline = time.time() + 1
        while not owner.contact_active and time.time() < deadline:
            time.sleep(0.01)

        def call_audio():
            try:
                call_result.append(owner.call(
                    wechat_ui_actions.UIIntent(
                        wechat_ui_actions.UIIntentKind.SEND_AUDIO,
                        {"conversation": "张三", "path": "voice.wav"},
                    ),
                    wechat_ui_actions.UI_CALL_WAIT_TIMEOUT,
                ))
            except BaseException as exc:
                call_error.append(exc)

        caller = threading.Thread(target=call_audio)
        caller.start()
        try:
            time.sleep(0.05)
            self.assertTrue(caller.is_alive())
            self.assertEqual(sent, [])
            contact_done.set()
            self.assertEqual(contact.result(1), "contact-result")
            caller.join(1)
        finally:
            contact_done.set()
            owner.stop()
            caller.join(1)

        self.assertFalse(caller.is_alive())
        self.assertEqual(call_error, [])
        self.assertEqual(call_result, ["sent"])
        self.assertEqual(sent, ["voice.wav"])

    def test_contact_batch_coalesces_message_poll_and_runs_it_after_recovery(self):
        contact_done = threading.Event()
        events = []
        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.CONTACT_START: lambda payload: wechat_ui_actions.ContactBatchHandle(
                poll=lambda: (contact_done.is_set(), None),
            ),
            wechat_ui_actions.UIIntentKind.CONTACT_RECOVER: lambda payload: events.append("recover"),
            wechat_ui_actions.UIIntentKind.POLL_MESSAGES: lambda payload: events.append("poll"),
            wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda payload: events.append("text"),
        }, poll_interval=0.01)
        owner.start()
        try:
            contact = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_START))
            deadline = time.time() + 1
            while not owner.contact_active and time.time() < deadline:
                time.sleep(0.01)
            text = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.SEND_TEXT))
            poll_a = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.POLL_MESSAGES))
            poll_b = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.POLL_MESSAGES))
            self.assertIs(poll_a, poll_b)
            contact_done.set()
            contact.result(1)
            text.result(1)
            poll_a.result(1)
        finally:
            owner.stop()

        self.assertEqual(events, ["recover", "text", "poll"])

    def test_contact_start_failure_releases_business_barrier(self):
        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.CONTACT_START: lambda _payload: (_ for _ in ()).throw(
                RuntimeError("contact start failed")
            ),
        }, poll_interval=0.01)
        owner.start()
        try:
            contact = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_START))
            with self.assertRaisesRegex(RuntimeError, "contact start failed"):
                contact.result(1)
            self.assertFalse(owner.contact_active)
            self.assertTrue(owner.wait_for_contact_idle())
        finally:
            owner.stop()

    def test_contact_batch_preserves_chat_intent_fifo_across_conversations(self):
        contact_done = threading.Event()
        attempts = []

        def send_text(payload):
            attempts.append(payload["conversation"])

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.CONTACT_START: lambda payload: wechat_ui_actions.ContactBatchHandle(
                poll=lambda: (contact_done.is_set(), None),
            ),
            wechat_ui_actions.UIIntentKind.SEND_TEXT: send_text,
        }, poll_interval=0.01)
        owner.start()
        try:
            contact = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_START))
            deadline = time.time() + 1
            while not owner.contact_active and time.time() < deadline:
                time.sleep(0.01)
            first_a = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT, {"conversation": "A", "text": "first"}
            ))
            second_a = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT, {"conversation": "A", "text": "second"}
            ))
            chat_b = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT, {"conversation": "B", "text": "other"}
            ))
            time.sleep(0.05)
            self.assertFalse(first_a.done)
            self.assertFalse(second_a.done)
            self.assertFalse(chat_b.done)
            self.assertEqual(attempts, [])
            contact_done.set()
            contact.result(1)
            first_a.result(1)
            second_a.result(1)
            chat_b.result(1)
        finally:
            owner.stop()

        self.assertEqual(attempts, ["A", "A", "B"])

    def test_watchdog_triggers_only_after_current_action_deadline(self):
        timed_out = threading.Event()
        snapshot = wechat_ui_actions.CurrentActionSnapshot(
            kind="send_text",
            started_at=time.monotonic() - 2,
            deadline_at=time.monotonic() - 1,
        )
        watchdog = wechat_ui_actions.UIWatchdog(
            lambda: snapshot,
            lambda current: timed_out.set(),
            poll_interval=0.01,
        )

        watchdog.start()
        try:
            self.assertTrue(timed_out.wait(1))
        finally:
            watchdog.stop()

    def test_owner_can_terminate_active_contact_job_before_process_exit(self):
        contact_started = threading.Event()
        contact_terminated = threading.Event()

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.CONTACT_START: lambda _payload: wechat_ui_actions.ContactBatchHandle(
                poll=lambda: (False, None),
                terminate=contact_terminated.set,
            ),
        })
        owner.start()
        try:
            ticket = owner.submit(wechat_ui_actions.UIIntent(wechat_ui_actions.UIIntentKind.CONTACT_START))
            deadline = time.monotonic() + 1
            while not owner.contact_active and time.monotonic() < deadline:
                time.sleep(0.01)
            contact_started.set()

            self.assertTrue(owner.terminate_active_contact_job())
            self.assertTrue(contact_terminated.wait(1))
            with self.assertRaises(wechat_ui_actions.IntentCancelled):
                ticket.result(1)
        finally:
            owner.stop()

    def test_hold_acquires_and_releases_threading_style_lock(self):
        events = []

        class Lock:
            def acquire(self, blocking=True):
                events.append(("acquire", blocking))
                return True

            def release(self):
                events.append(("release",))

        bot = SimpleNamespace(_get_wechat_action_lock=lambda: Lock())

        with wechat_ui_actions.hold(bot):
            events.append(("inside",))

        self.assertEqual(events, [("acquire", True), ("inside",), ("release",)])

    def test_try_acquire_returns_none_when_lock_is_busy(self):
        released = []
        test_case = self

        class BusyLock:
            def acquire(self, blocking=True):
                test_case.assertFalse(blocking)
                return False

            def release(self):
                released.append(True)

        bot = SimpleNamespace(_get_wechat_action_lock=lambda: BusyLock())

        self.assertIsNone(wechat_ui_actions.try_acquire(bot))
        self.assertEqual(released, [])
        self.assertTrue(wechat_ui_actions.is_busy(bot))

    def test_context_manager_lock_is_supported_for_test_doubles(self):
        events = []

        class ContextLock:
            def __enter__(self):
                events.append(("enter",))
                return self

            def __exit__(self, exc_type, exc, tb):
                events.append(("exit", exc_type))
                return False

        bot = SimpleNamespace(_get_wechat_action_lock=lambda: ContextLock())

        release = wechat_ui_actions.acquire(bot)
        self.assertIsNotNone(release)
        events.append(("inside",))
        release()

        self.assertEqual(events, [("enter",), ("inside",), ("exit", None)])

    def test_missing_lock_getter_is_treated_as_noop_for_pure_test_doubles(self):
        bot = SimpleNamespace()

        release = wechat_ui_actions.try_acquire(bot)
        self.assertIsNotNone(release)
        release()
        self.assertFalse(wechat_ui_actions.is_busy(bot))

    def test_invalid_configured_lock_fails_loudly(self):
        bot = SimpleNamespace(_get_wechat_action_lock=lambda: object())

        with self.assertRaises(RuntimeError):
            wechat_ui_actions.acquire(bot)

    def test_owner_cancels_queued_stale_conversation_version(self):
        started = threading.Event()
        release = threading.Event()
        versions = {"张三": 1}
        sent = []

        def handler(payload):
            sent.append(payload["text"])
            if payload["text"] == "占位":
                started.set()
                release.wait(1)

        owner = wechat_ui_actions.WeChatUIOwner(
            {wechat_ui_actions.UIIntentKind.SEND_TEXT: handler},
            conversation_version_provider=lambda conversation, _chat_type: versions.get(conversation, 0),
        )
        owner.start()
        try:
            first = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT,
                {"conversation": "张三", "text": "占位"},
                conversation_version=1,
            ))
            self.assertTrue(started.wait(1))
            stale = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT,
                {"conversation": "张三", "text": "过期回复"},
                conversation_version=1,
            ))
            versions["张三"] = 2
            release.set()
            first.result(1)
            with self.assertRaises(wechat_ui_actions.IntentCancelled):
                stale.result(1)
        finally:
            release.set()
            owner.stop()

        self.assertEqual(sent, ["占位"])

    def test_owner_checks_zero_group_version_with_group_scope(self):
        versions = {("测试群", "group"): 1}
        sent = []
        owner = wechat_ui_actions.WeChatUIOwner(
            {wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda payload: sent.append(payload["text"])},
            conversation_version_provider=lambda conversation, chat_type: versions.get(
                (conversation, chat_type),
                0,
            ),
        )
        owner.start()
        try:
            ticket = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT,
                {
                    "conversation": "测试群",
                    "chat_type": "group",
                    "text": "过期群回复",
                },
                conversation_version=0,
            ))
            with self.assertRaises(wechat_ui_actions.IntentCancelled):
                ticket.result(1)
        finally:
            owner.stop()

        self.assertEqual(sent, [])

    def test_owner_rechecks_task_version_before_execution(self):
        versions = {"task-1": 2}
        owner = wechat_ui_actions.WeChatUIOwner(
            {wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda payload: payload["text"]},
            task_version_provider=lambda task_key: versions.get(task_key, 0),
        )
        owner.start()
        try:
            ticket = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT,
                {"conversation": "张三", "text": "旧任务", "task_key": "task-1"},
                task_version=1,
            ))
            with self.assertRaises(wechat_ui_actions.IntentCancelled):
                ticket.result(1)
        finally:
            owner.stop()

    def test_owner_prepares_latest_contact_name_at_execution(self):
        names = {"contact-1": "新备注"}
        seen = []

        def prepare(intent):
            payload = dict(intent.payload)
            payload["conversation"] = names[payload["contact_key"]]
            return payload

        owner = wechat_ui_actions.WeChatUIOwner(
            {wechat_ui_actions.UIIntentKind.SEND_TEXT: lambda payload: seen.append(payload["conversation"])},
            payload_preparer=prepare,
        )
        owner.start()
        try:
            owner.call(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_TEXT,
                {"conversation": "旧备注", "contact_key": "contact-1", "text": "你好"},
            ), 1)
        finally:
            owner.stop()

        self.assertEqual(seen, ["新备注"])

    def test_owner_marks_non_idempotent_delivery_uncertain_on_handler_error(self):
        events = []

        class Journal:
            def begin(self, delivery_id, kind, payload):
                events.append(("begin", delivery_id, kind, payload["conversation"]))
                return True

            def finish(self, delivery_id, status, error=""):
                events.append(("finish", delivery_id, status, error))

        def fail(_payload):
            raise RuntimeError("injected")

        owner = wechat_ui_actions.WeChatUIOwner({wechat_ui_actions.UIIntentKind.SEND_FILE: fail})
        owner.set_delivery_journal(Journal())
        owner.start()
        try:
            ticket = owner.submit(wechat_ui_actions.UIIntent(
                wechat_ui_actions.UIIntentKind.SEND_FILE,
                {"conversation": "张三", "path": "a.pdf", "delivery_id": "delivery-1"},
            ))
            with self.assertRaisesRegex(RuntimeError, "injected"):
                ticket.result(1)
        finally:
            owner.stop()

        self.assertEqual(events[0], ("begin", "delivery-1", "send_file", "张三"))
        self.assertEqual(events[1][:3], ("finish", "delivery-1", "uncertain"))

    def test_owner_marks_false_non_idempotent_result_uncertain(self):
        events = []

        class Journal:
            def begin(self, delivery_id, kind, payload):
                events.append(("begin", delivery_id, kind, payload["conversation"]))
                return True

            def finish(self, delivery_id, status, error=""):
                events.append(("finish", delivery_id, status, error))

        owner = wechat_ui_actions.WeChatUIOwner({
            wechat_ui_actions.UIIntentKind.SEND_AUDIO: lambda _payload: False,
        })
        owner.set_delivery_journal(Journal())
        owner.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "unsuccessful result"):
                owner.call(wechat_ui_actions.UIIntent(
                    wechat_ui_actions.UIIntentKind.SEND_AUDIO,
                    {"conversation": "张三", "path": "voice.wav", "delivery_id": "delivery-2"},
                ), 1)
        finally:
            owner.stop()

        self.assertEqual(events[0], ("begin", "delivery-2", "send_audio", "张三"))
        self.assertEqual(events[1][:3], ("finish", "delivery-2", "uncertain"))


if __name__ == "__main__":
    unittest.main()
