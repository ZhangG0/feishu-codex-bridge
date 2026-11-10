import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from bridge.codex import final_text
from bridge.service import Service, route
from bridge.store import Store


BINDING = {'chat_id': 'oc_test', 'alias': '项目A', 'thread_id': 'thread_a'}
CONFIG = {'bot_open_id': 'ou_bot', 'bindings': [BINDING], 'send_enabled': True}


def event(sender='ou_any_member'):
    return {'sender': {'sender_type': 'user', 'sender_id': {'open_id': sender}},
            'message': {'message_id': 'om_test', 'chat_id': 'oc_test', 'chat_type': 'group',
                        'message_type': 'text', 'content': json.dumps({'text': '@_user_1 项目A 检查测试'}),
                        'mentions': [{'key': '@_user_1', 'id': {'open_id': 'ou_bot'}}]}}


class RoutingTests(unittest.TestCase):
    def test_all_group_members_are_allowed(self):
        for sender in ('ou_owner', 'ou_other'):
            self.assertEqual(route(event(sender), CONFIG), (BINDING, '检查测试'))

    def test_other_mentions_and_bot_messages_ignored(self):
        e = event()
        e['message']['mentions'][0]['id']['open_id'] = 'ou_other_bot'
        self.assertIsNone(route(e, CONFIG))
        e = event()
        e['sender']['sender_type'] = 'app'
        self.assertIsNone(route(e, CONFIG))

    def test_unbound_group_and_private_message_ignored(self):
        for key, value in [('chat_id', 'other'), ('chat_type', 'p2p')]:
            e = event()
            e['message'][key] = value
            self.assertIsNone(route(e, CONFIG))

    def test_multi_thread_requires_alias(self):
        config = dict(CONFIG, bindings=[BINDING, dict(BINDING, alias='B', thread_id='b')])
        e = event()
        e['message']['content'] = json.dumps({'text': '@_user_1 没有指定项目'})
        self.assertIsNone(route(e, config))

    def test_only_final_answer(self):
        turn = {'items': [{'type': 'agentMessage', 'phase': 'commentary', 'text': '处理中'},
                          {'type': 'agentMessage', 'phase': 'final_answer', 'text': '完成'}]}
        self.assertEqual(final_text(turn), '完成')


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'state.db')
        self.store = Store(self.path)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def enqueue(self, mid='m1', binding=BINDING, limit=30):
        return self.store.enqueue(mid, binding['chat_id'], 'any_member', binding, '测试', limit)

    def test_duplicate_durable_after_reopen(self):
        self.assertTrue(self.enqueue())
        reopened = Store(self.path)
        try:
            self.assertFalse(reopened.enqueue('m1', 'oc_test', 'any', BINDING, '测试', 30))
            self.assertIsNotNone(reopened.claim())
        finally:
            reopened.db.close()

    def test_crash_does_not_reexecute_running_task(self):
        self.enqueue()
        job = self.store.claim()
        self.enqueue('m2')
        self.store.attach_turn(job['message_id'], 'turn1')
        self.store.recover()
        self.assertIsNone(self.store.claim())
        self.store.resolve('m1')
        self.assertEqual(self.store.claim()['message_id'], 'm2')

    def test_thread_block_does_not_block_other_thread(self):
        self.enqueue()
        self.store.claim()
        self.store.recover()
        self.enqueue('m2', dict(BINDING, thread_id='thread_b'))
        self.assertEqual(self.store.claim()['thread_id'], 'thread_b')

    def test_bounded_queue_and_duplicate_rejection(self):
        self.assertTrue(self.enqueue(limit=1))
        self.assertFalse(self.enqueue('m2', limit=1))
        self.assertFalse(self.enqueue('m2', limit=1))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)

    def test_finish_and_outbox_survive_restart(self):
        self.enqueue()
        job = self.store.claim()
        self.store.finish(job, '结果')
        reopened = Store(self.path)
        try:
            self.assertEqual(reopened.summary()['jobs'], [{'status': 'done', 'count': 1}])
            self.assertEqual(reopened.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)
        finally:
            reopened.db.close()

    def test_outbox_retry_stable_id_and_backoff(self):
        self.store.output('key', 'chat', 'text')
        row = self.store.due()
        self.store.retry(row, 'network')
        self.assertIsNone(self.store.due())
        self.assertEqual(self.store.db.execute('SELECT id FROM outbox').fetchone()[0], row['id'])
        self.store.output('key', 'chat', 'text')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)

    def test_monitor_avoids_own_reply_and_repeated_delivery(self):
        self.enqueue()
        self.store.attach_turn('m1', 'turn1')
        self.store.forward('thread_a', 'turn1', [BINDING], 'own')
        self.store.forward('thread_a', 'turn2', [BINDING], 'desktop')
        self.store.forward('thread_a', 'turn2', [BINDING], 'desktop')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 2)

    def test_long_message_split(self):
        self.store.output('key', 'chat', '中' * 6001)
        rows = self.store.db.execute('SELECT text FROM outbox ORDER BY rowid').fetchall()
        self.assertEqual([len(r[0]) for r in rows], [2500, 2500, 1001])

    def test_monitor_does_not_duplicate_fast_queue_completion(self):
        self.enqueue()
        # Monitor can observe desktop completion before the worker stores turn_id.
        self.store.forward('thread_a', 'turn_fast', [BINDING], 'own', source_mid='m1')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 0)

    def test_mock_receive_execution_delivery(self):
        class FakeCodex:
            def run(self, job, attached, stop):
                attached('t_mock')
                return '模拟回答'

        class FakeJSON:
            marshal = staticmethod(json.dumps)

        class FakeLark:
            JSON = FakeJSON

        class FakeFeishu:
            lark = FakeLark

            def __init__(self):
                self.sent = []

            def send(self, row):
                self.sent.append(row['text'])

        feishu = FakeFeishu()
        service = Service(CONFIG, self.store, FakeCodex(), feishu)
        service.receive({'event': event()})
        service.receive({'event': event()})
        threads = [threading.Thread(target=target) for target in (service.worker, service.sender)]
        for thread in threads:
            thread.start()
        try:
            end = time.monotonic() + 5
            while time.monotonic() < end and len(feishu.sent) < 2:
                time.sleep(.02)
            self.assertEqual(len(feishu.sent), 2)
            self.assertIn('Codex 已接收', feishu.sent[0])
            self.assertIn('模拟回答', feishu.sent[-1])
        finally:
            service.stop.set()
            for thread in threads:
                thread.join(2)


if __name__ == '__main__':
    unittest.main()
