from core.listener_window_supervisor import ListenerWindowSupervisor


def test_window_supervisor_retries_without_message_payloads():
    supervisor = ListenerWindowSupervisor(clock=lambda: 100.0)

    assert supervisor.request("张三", error="missing")
    claimed = supervisor.claim_due(now=100.0)

    assert len(claimed) == 1
    assert set(claimed[0]) == {
        "conversation",
        "chat_type",
        "first_failed_at",
        "next_retry_at",
        "attempts",
        "last_error",
        "allow_rebuild",
        "degraded",
        "inflight",
    }
    assert "messages" not in claimed[0]


def test_window_supervisor_keeps_retrying_after_degraded_state():
    supervisor = ListenerWindowSupervisor(
        retry_delays=(30, 60),
        retry_interval=60,
        degraded_after=600,
        degraded_interval=300,
        clock=lambda: 0.0,
    )
    supervisor.request("张三", now=0)
    supervisor.claim_due(now=0)

    state = supervisor.failed("张三", "still missing", now=601)

    assert state["degraded"] is True
    assert state["next_retry_at"] == 901
    assert supervisor.claim_due(now=900) == []
    assert supervisor.claim_due(now=901)[0]["conversation"] == "张三"


def test_window_supervisor_success_removes_retry_state():
    supervisor = ListenerWindowSupervisor(clock=lambda: 0.0)
    supervisor.request("张三")

    assert supervisor.succeeded("张三") is True
    assert supervisor.snapshot() == []


def test_window_supervisor_isolates_same_named_private_and_group_chats():
    supervisor = ListenerWindowSupervisor(clock=lambda: 0.0)
    supervisor.request("同名会话", chat_type="private")
    supervisor.request("同名会话", chat_type="group")

    claimed = supervisor.claim_due(limit=2, now=0)

    assert [(item["chat_type"], item["conversation"]) for item in claimed] == [
        ("group", "同名会话"),
        ("private", "同名会话"),
    ]
    assert supervisor.succeeded("同名会话", chat_type="group") is True
    assert supervisor.contains("同名会话", chat_type="private") is True
