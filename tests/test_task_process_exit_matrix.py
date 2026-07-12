import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from feature import friend_request
from feature.material_outreach_storage import MaterialOutreachStorage
from feature.moments_tasks import merge_moments_task_storage, recover_interrupted_moments_task
from feature.scheduled_message_tasks import (
    merge_scheduled_message_task_storage,
    recover_interrupted_scheduled_message_task,
)
from feature.task_workbench_storage import TaskWorkbenchStorage
from core.wechat_ui_runtime import WeChatUIRuntime


ROOT = Path(__file__).resolve().parents[1]


class TaskProcessExitMatrixTests(unittest.TestCase):
    def _crash(self, script, temp_dir, phase):
        env = dict(os.environ)
        env.update({
            "PYTHONPATH": str(ROOT),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "WXBOT_FAULT_PHASE": phase,
        })
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script],
            cwd=temp_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(result.returncode, 91, result.stdout + result.stderr)

    def test_scheduled_message_process_exit_matrix(self):
        script = """
import os
from feature.scheduled_message_tasks import (
    finish_scheduled_message_run,
    mark_scheduled_message_running,
    queue_scheduled_message_task,
    split_scheduled_message_task_storage,
)
from feature.task_workbench_storage import TaskWorkbenchStorage

phase = os.environ['WXBOT_FAULT_PHASE']
task = queue_scheduled_message_task({
    'id': 'task-1', 'enabled': True, 'targets': ['fault-target'], 'msgs': ['hello'],
}, next_run_at='2026-07-11T12:00:00')
if phase != 'before_call':
    task = mark_scheduled_message_running(task, run_id='run-1', started_at='2026-07-11T11:00:00')
    task['pending_snapshot'] = {
        'run_id': 'run-1',
        'delivery_records': [{'key': '0:0', 'target': 'fault-target', 'message_index': 0, 'status': 'inflight', 'error': ''}],
    }
if phase == 'after_done':
    task = finish_scheduled_message_run(
        task, result_type='success', success_count=1, failed_count=0, skipped_count=0,
        finished_at='2026-07-11T11:01:00', recurring=False, next_run_at='',
        execution_snapshot={
            'run_id': 'run-1',
            'delivery_records': [{'key': '0:0', 'target': 'fault-target', 'message_index': 0, 'status': 'done', 'error': ''}],
        },
    )
definition, runtime, history = split_scheduled_message_task_storage(task)
storage = TaskWorkbenchStorage('.', 'wxid_test', 'scheduled_message')
storage.save_tasks([definition])
storage.save_runtime({'task-1': runtime})
storage.save_history({'task-1': history})
os._exit(91)
"""
        expected = {
            "before_call": ("pending", None),
            "inside_call": ("pending_confirm", "uncertain"),
            "after_done": ("executed", "done"),
        }
        for phase, (status, delivery_status) in expected.items():
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                self._crash(script, tmp, phase)
                storage = TaskWorkbenchStorage(tmp, "wxid_test", "scheduled_message")
                task = merge_scheduled_message_task_storage(
                    storage.load_tasks()[0],
                    storage.load_runtime().get("task-1"),
                    storage.load_history().get("task-1"),
                )
                task = recover_interrupted_scheduled_message_task(task)
                self.assertEqual(task["status"], status)
                if delivery_status:
                    records = task["last_result"]["delivery_records"]
                    self.assertEqual(records[0]["status"], delivery_status)

    def test_moments_process_exit_matrix(self):
        script = """
import os
from datetime import datetime
from feature.moments_tasks import (
    mark_moments_task_running,
    normalize_moments_task,
    queue_moments_task,
    split_moments_task_storage,
)
from feature.task_workbench_storage import TaskWorkbenchStorage

phase = os.environ['WXBOT_FAULT_PHASE']
now = datetime(2026, 7, 11, 11, 0, 0)
task = queue_moments_task({'id': 'moment-1', 'enabled': True, 'raw_text': 'fault-test'}, mode='immediate', now=now)
if phase != 'before_call':
    task = mark_moments_task_running(task, now=now)
    task['execution_snapshot'] = {'run_id': 'run-1', 'content_summary': 'fault-test'}
if phase == 'after_done':
    task = normalize_moments_task({
        **task,
        'status': 'executed',
        'enabled': False,
        'execute_after': '',
        'executed_at': '2026-07-11T11:01:00',
        'execution_result': 'success',
        'execution_message': '朋友圈已执行',
    }, now=now)
definition, runtime, history = split_moments_task_storage(task, now=now)
storage = TaskWorkbenchStorage('.', 'wxid_test', 'moments')
storage.save_tasks([definition])
storage.save_runtime({'moment-1': runtime})
storage.save_history({'moment-1': history})
os._exit(91)
"""
        expected = {
            "before_call": ("pending", ""),
            "inside_call": ("pending_confirm", "uncertain"),
            "after_done": ("executed", "success"),
        }
        for phase, (status, result_type) in expected.items():
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                self._crash(script, tmp, phase)
                storage = TaskWorkbenchStorage(tmp, "wxid_test", "moments")
                task = merge_moments_task_storage(
                    storage.load_tasks()[0],
                    storage.load_runtime().get("moment-1"),
                    storage.load_history().get("moment-1"),
                )
                task = recover_interrupted_moments_task(task)
                self.assertEqual(task["status"], status)
                self.assertEqual(task["execution_result"], result_type)

    def test_material_outreach_process_exit_matrix(self):
        script = """
import os
from feature.material_outreach_storage import MaterialOutreachStorage

phase = os.environ['WXBOT_FAULT_PHASE']
storage = MaterialOutreachStorage('.', 'wxid_test')
base = {
    'run_id': 'run-1', 'task_id': 'task-1', 'contact_key': 'contact-1',
    'send_name': 'fault-target', 'display_name': 'fault-target', 'created_at': '2026-07-11T11:00:00',
}
if phase != 'before_call':
    storage.append_progress_records([{**base, 'status': 'inflight', 'status_label': '发送中'}])
if phase == 'after_done':
    storage.append_progress_records([{**base, 'status': 'success', 'status_label': '成功'}])
os._exit(91)
"""
        expected = {
            "before_call": None,
            "inside_call": "uncertain",
            "after_done": "success",
        }
        for phase, status in expected.items():
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                self._crash(script, tmp, phase)
                storage = MaterialOutreachStorage(tmp, "wxid_test")
                storage.freeze_all_interrupted_sends()
                records = storage.load_progress_records()
                if status is None:
                    self.assertEqual(records, [])
                else:
                    self.assertEqual(records[-1]["status"], status)

    def test_friend_request_process_exit_matrix(self):
        script = """
import os
from datetime import datetime
from feature.friend_request import default_state, record_execution, save_state

phase = os.environ['WXBOT_FAULT_PHASE']
state = default_state('wxid_test')
candidate = {
    'candidate_id': 'candidate-1', 'contact_key': 'contact-1', 'name': 'fault-target',
    'send_target': 'fault-target', 'conversation_keyword': 'fault-target', 'status': 'pending',
}
state['candidates'] = [candidate]
if phase != 'before_call':
    candidate['status'] = 'uncertain'
    candidate['claim_token'] = 'claim-1'
    candidate['last_result'] = '好友申请正在提交；若进程中断需人工核实'
if phase == 'after_done':
    state = record_execution(
        state,
        candidate,
        {'status': 'sent', 'message': '已发送'},
        addmsg='',
        now=datetime(2026, 7, 11, 11, 1, 0),
    )
save_state('.', state)
os._exit(91)
"""
        expected = {
            "before_call": "pending",
            "inside_call": "uncertain",
            "after_done": "sent",
        }
        for phase, status in expected.items():
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                self._crash(script, tmp, phase)
                state = friend_request.load_state(tmp, "wxid_test")
                self.assertEqual(state["candidates"][0]["status"], status)

    def test_new_friend_accept_process_exit_reloads_wechat_truth(self):
        for phase in ("before_call", "inside_call", "after_done"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                marker = Path(tmp) / "accepted.txt"
                script = f"""
import os
from pathlib import Path
from core.wechat_ui_actions import UIIntent, UIIntentKind, WeChatUIOwner
from core.wechat_ui_runtime import WeChatUIRuntime

phase = os.environ['WXBOT_FAULT_PHASE']
marker = Path({str(marker)!r})
class Candidate:
    name = 'fault-target'
    content = 'request'
    acceptable = True
    def accept(self, **_kwargs):
        marker.write_text('accepted', encoding='utf-8')
        if phase == 'inside_call':
            os._exit(91)
class Client:
    nickname = 'test'
    def GetMyInfo(self): return {{'id': 'wxid_test'}}
    def StopListening(self): return True
    def StartListening(self): return True
    def GetNewFriends(self, acceptable=True): return [] if marker.exists() else [Candidate()]
runtime = WeChatUIRuntime(lambda *_args: None, client_factory=lambda _version: Client())
owner = WeChatUIOwner(runtime.handlers())
owner.start()
owner.call(UIIntent(UIIntentKind.BOOTSTRAP), 2)
if phase == 'before_call':
    os._exit(91)
owner.call(UIIntent(UIIntentKind.NEW_FRIEND, {{}}), 2)
os._exit(91)
"""
                self._crash(script, tmp, phase)

                accept_calls = []

                class RecoveryCandidate:
                    name = "fault-target"
                    content = "request"
                    acceptable = True

                    def accept(self, **_kwargs):
                        marker.write_text("accepted", encoding="utf-8")
                        accept_calls.append(True)

                class RecoveryClient:
                    def GetNewFriends(self, acceptable=True):
                        return [] if marker.exists() else [RecoveryCandidate()]

                runtime = WeChatUIRuntime(lambda *_args: None)
                runtime._client = RecoveryClient()
                accepted = runtime.process_new_friends({})
                if phase == "before_call":
                    self.assertEqual(len(accepted), 1)
                    self.assertEqual(len(accept_calls), 1)
                else:
                    self.assertEqual(accepted, [])
                    self.assertEqual(accept_calls, [])

    def test_contact_edit_process_exit_converges_to_noop(self):
        desired = "fault-remark"
        for phase in ("before_call", "inside_call", "after_done"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                marker = Path(tmp) / "remark.txt"
                script = f"""
import os
from pathlib import Path
from core.wechat_ui_runtime import WeChatUIRuntime

phase = os.environ['WXBOT_FAULT_PHASE']
marker = Path({str(marker)!r})
class Client:
    def ChatWith(self, who=None, exact=True): return True
    def ChatInfo(self): return {{'chat_type': 'friend', 'chat_name': 'fault-target'}}
    def EditFriendInfo(self, **kwargs):
        marker.write_text(str(kwargs.get('remark') or ''), encoding='utf-8')
        if phase == 'inside_call':
            os._exit(91)
        return {{'status': '成功'}}
runtime = WeChatUIRuntime(lambda *_args: None)
runtime._client = Client()
if phase == 'before_call':
    os._exit(91)
runtime.edit_contact({{'target': 'fault-target', 'remark': {desired!r}}})
os._exit(91)
"""
                self._crash(script, tmp, phase)

                class RecoveryClient:
                    def ChatWith(self, who=None, exact=True):
                        return True

                    def ChatInfo(self):
                        return {"chat_type": "friend", "chat_name": "fault-target"}

                    def EditFriendInfo(self, **kwargs):
                        if marker.exists() and marker.read_text(encoding="utf-8") == kwargs.get("remark"):
                            return {"status": "成功", "message": "目标值已满足，未进行任何修改"}
                        marker.write_text(str(kwargs.get("remark") or ""), encoding="utf-8")
                        return {"status": "成功"}

                runtime = WeChatUIRuntime(lambda *_args: None)
                runtime._client = RecoveryClient()
                result = runtime.edit_contact({"target": "fault-target", "remark": desired})
                self.assertEqual(result["status"], "成功")
                self.assertEqual(bool(result.get("noop")), phase != "before_call")


if __name__ == "__main__":
    unittest.main()
