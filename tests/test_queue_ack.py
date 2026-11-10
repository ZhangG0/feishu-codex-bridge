import tempfile
import unittest
from pathlib import Path

from bridge.store import Store


class QueueAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'state.db')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def enqueue(self, mid, thread='a', limit=30):
        return self.store.enqueue(mid, 'chat', 'sender',
                                  {'thread_id': thread, 'alias': thread}, '测试', limit)

    def messages(self, mid):
        return [r[0] for r in self.store.db.execute(
            'SELECT text FROM outbox WHERE parent_mid=? ORDER BY rowid', (mid,))]

    def test_idle_first_job_is_saved_silently_and_keeps_lifecycle_messages(self):
        self.assertTrue(self.enqueue('first'))
        self.assertEqual(self.messages('first'), [])
        self.assertFalse(self.enqueue('first'))
        job = self.store.claim()
        self.assertEqual(job['message_id'], 'first')
        self.store.attach_turn('first', 'turn')
        self.store.attach_turn('first', 'turn')
        self.store.finish(job, '完成结果')
        messages = self.messages('first')
        self.assertEqual(len(messages), 2)
        self.assertIn('Codex 已接收', messages[0])
        self.assertIn('完成结果', messages[1])
        self.assertTrue(all('#0001｜测试' in m for m in messages))

    def test_queued_or_running_work_in_any_thread_requires_wait_notice(self):
        for running in (False, True):
            for thread in ('a', 'b'):
                with self.subTest(running=running, thread=thread):
                    self.enqueue('first')
                    if running:
                        self.store.claim()
                    self.assertTrue(self.enqueue('second', thread))
                    messages = self.messages('second')
                    self.assertEqual(len(messages), 1)
                    self.assertIn('前面有 1 条任务', messages[0])
                    self.assertIn('尚未提交给 Codex', messages[0])
                    with self.store.db:
                        self.store.db.execute('DELETE FROM jobs')
                        self.store.db.execute('DELETE FROM outbox')

    def test_review_pauses_own_thread_but_does_not_count_against_others(self):
        self.enqueue('uncertain')
        job = self.store.claim()
        self.store.finish(job, '状态不明', 'needs_review')
        self.assertTrue(self.enqueue('paused'))
        self.assertIn('队列已暂停', self.messages('paused')[0])
        self.assertIn('当前待处理 2 条', self.messages('paused')[0])
        self.assertIsNone(self.store.claim())
        self.assertTrue(self.enqueue('other', 'b'))
        self.assertEqual(self.messages('other'), [])
        self.assertEqual(self.store.claim()['message_id'], 'other')

    def test_full_queue_still_rejects_and_duplicate_does_not_repeat_notice(self):
        self.enqueue('first', limit=1)
        self.assertFalse(self.enqueue('full', limit=1))
        self.assertFalse(self.enqueue('full', limit=1))
        self.assertEqual(len(self.messages('full')), 1)
        self.assertIn('队列已满，本条未提交', self.messages('full')[0])
        self.assertEqual(self.store.claim()['message_id'], 'first')
        self.assertIsNone(self.store.claim())

    def test_silent_enqueue_keeps_error_notification(self):
        self.enqueue('first')
        self.store.finish(self.store.claim(), '任务失败', 'failed')
        self.assertEqual(len(self.messages('first')), 1)
        self.assertIn('任务失败', self.messages('first')[0])


if __name__ == '__main__':
    unittest.main()
