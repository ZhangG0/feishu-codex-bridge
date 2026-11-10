import sys
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from bridge.codex import Codex, DesktopQueue, RpcError, source_message_id


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.codex = Codex([sys.executable, str(Path(__file__).with_name('fake_rpc.py'))])

    def tearDown(self):
        self.codex.close()

    def test_completion_before_start_response_is_retained(self):
        turns = []
        text = self.codex.run({'thread_id': 'thread_a', 'sender_id': 'user',
                               'message_id': 'msg', 'prompt': '测试'}, turns.append, threading.Event())
        self.assertEqual(text, '协议测试回答')
        self.assertEqual(turns, ['turn_a'])

    def test_rpc_error_is_not_success(self):
        with self.assertRaises(RpcError):
            self.codex.call('unknown', {})

    def test_remote_approval_is_not_automatically_granted(self):
        self.codex.call('test/approval', {})
        self.assertEqual(self.codex.events.get(timeout=2)['method'], 'bridge/approvalRequired')


class DesktopQueueTests(unittest.TestCase):
    def test_queue_correlates_reply_without_resuming_desktop_writer(self):
        adapter = DesktopQueue.__new__(DesktopQueue)
        adapter.queue_command = ['codex', 'queue']
        completed = {'id': 'new_turn', 'status': 'completed', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': '[FEISHU_BRIDGE_MESSAGE_ID:om_1]\n测试'}]},
            {'type': 'agentMessage', 'phase': 'final_answer', 'text': 'OK'}]}
        pages = iter([{'data': [{'id': 'old_turn'}]}, {'data': [completed]}])
        adapter.turns = lambda tid, cursor=None: next(pages)
        attached = []
        with patch('bridge.codex.subprocess.run', return_value=SimpleNamespace(returncode=0)) as submit:
            text = adapter.run({'thread_id': 'tid', 'message_id': 'om_1', 'sender_id': 'any', 'prompt': '测试'},
                               attached.append, threading.Event())
        self.assertEqual(text, 'OK')
        self.assertEqual(attached, ['new_turn'])
        args = submit.call_args.args[0]
        self.assertEqual(args[:4], ['codex', 'queue', '--thread', 'tid'])
        self.assertIn('[FEISHU_BRIDGE_MESSAGE_ID:om_1]', args[-1])

    def test_submission_error_does_not_wait_or_claim_success(self):
        adapter = DesktopQueue.__new__(DesktopQueue)
        adapter.queue_command = ['codex', 'queue']
        adapter.turns = lambda tid: {'data': []}
        with patch('bridge.codex.subprocess.run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(RpcError):
                adapter.run({'thread_id': 'tid', 'message_id': 'om_1', 'sender_id': 'any', 'prompt': '测试'},
                            lambda t: None, threading.Event())

    def test_resume_warning_is_uncertain_even_when_queue_command_succeeds(self):
        adapter = DesktopQueue.__new__(DesktopQueue)
        adapter.queue_command = ['codex', 'queue', '--resume']
        adapter.turns = lambda tid: {'data': []}
        with patch('bridge.codex.subprocess.run', return_value=SimpleNamespace(returncode=0, stderr='warning: failed to resume session')):
            with self.assertRaisesRegex(RpcError, 'inspect original queue'):
                adapter.run({'thread_id': 'tid', 'message_id': 'om_1', 'prompt': 'test'}, lambda _: None, threading.Event())

    def test_marker_only_matches_user_items(self):
        self.assertIsNone(source_message_id({'items': [{'type': 'agentMessage', 'text': '[FEISHU_BRIDGE_MESSAGE_ID:fake]'}]}))

    def test_unfinished_interrupted_snapshot_waits_for_completion(self):
        adapter = DesktopQueue.__new__(DesktopQueue)
        adapter.queue_command = ['codex', 'queue']
        items = [{'type': 'userMessage', 'content': [{'type': 'text', 'text': '[FEISHU_BRIDGE_MESSAGE_ID:om_1]'}]}]
        pages = iter([{'data': []},
                      {'data': [{'id': 't', 'status': 'interrupted', 'completedAt': None, 'items': items}]},
                      {'data': [{'id': 't', 'status': 'completed', 'items': items + [
                          {'type': 'agentMessage', 'phase': 'final_answer', 'text': '完成'}]}]}])
        adapter.turns = lambda tid, cursor=None: next(pages)
        stop = SimpleNamespace(is_set=lambda: False, wait=lambda delay: None)
        with patch('bridge.codex.subprocess.run', return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(adapter.run({'thread_id': 'tid', 'message_id': 'om_1', 'sender_id': 'any', 'prompt': '测试'},
                                         lambda t: None, stop), '完成')


if __name__ == '__main__':
    unittest.main()
