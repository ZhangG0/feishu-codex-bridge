import tempfile, threading, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from bridge.codex import DesktopQueue, TurnEnded
from bridge.store import Store
from bridge.service import Service

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(str(Path(self.tmp.name)/'state.db'))
        self.b={'thread_id':'t','chat_id':'c','alias':'test'}
        self.s.enqueue('m','c','ou_sender',self.b,'test',30);self.j=self.s.claim()
        self.s.attach_turn('m','turn');self.s.finish(self.j,'uncertain','needs_review')
        self.s.enqueue('next','c','ou_sender',self.b,'next',30)
    def tearDown(self):self.s.db.close();self.tmp.cleanup()
    def text(self):return '\n'.join(x[0] for x in self.s.db.execute('SELECT text FROM outbox'))
    def service(self,turn):
        c=SimpleNamespace(inspect_job=lambda j:turn)
        return Service({},self.s,c,None)
    def test_uncertainty_blocks_and_ack_explains(self):
        self.assertIsNone(self.s.claim());self.assertIn('暂停',self.text())
        self.service(None).review_once();self.assertIsNone(self.s.claim())
    def test_success_recovered_once_without_resubmission(self):
        turn={'id':'turn','status':'completed','items':[{'type':'agentMessage','phase':'final_answer','text':'实际结果'}]}
        service=self.service(turn);service.review_once();service.review_once()
        self.assertEqual(self.text().count('实际结果'),1)
        self.assertEqual(self.s.claim()['message_id'],'next')
    def test_failed_snapshot_requires_end_time_and_stability(self):
        turn={'id':'turn','status':'interrupted','completedAt':None}
        service=self.service(turn)
        with patch('bridge.service.time.monotonic',return_value=100):service.review_once()
        self.assertIsNone(self.s.claim())
        turn['completedAt']=10
        with patch('bridge.service.time.monotonic',return_value=100):service.review_once()
        with patch('bridge.service.time.monotonic',return_value=110):service.review_once()
        self.assertIsNone(self.s.claim())
        with patch('bridge.service.time.monotonic',return_value=116):service.review_once()
        self.assertEqual(self.s.claim()['message_id'],'next');self.assertIn('可能有部分操作完成',self.text())
    def test_active_or_reader_error_resets_terminal_evidence(self):
        turn={'id':'turn','status':'failed','completedAt':1};service=self.service(turn)
        with patch('bridge.service.time.monotonic',return_value=100):service.review_once()
        service.codex.inspect_job=lambda j:(_ for _ in ()).throw(RuntimeError('offline'))
        service.review_once()
        service.codex.inspect_job=lambda j:turn
        with patch('bridge.service.time.monotonic',return_value=200):service.review_once()
        self.assertIsNone(self.s.claim())
    def test_confirmed_interruption_does_not_block_next_job(self):
        self.s.resolve('m');job=self.s.claim();self.s.finish(job,'interrupted','interrupted')
        self.s.enqueue('third','c','ou_sender',self.b,'third',30)
        self.assertEqual(self.s.claim()['message_id'],'third')
    def test_desktop_typed_interruption_after_stable_end(self):
        a=DesktopQueue.__new__(DesktopQueue);a.queue_command=['codex','queue']
        t={'id':'turn','status':'interrupted','completedAt':1,'items':[{'type':'userMessage','content':[{'type':'text','text':'[FEISHU_BRIDGE_MESSAGE_ID:m]'}]}]}
        pages=iter([{'data':[]},{'data':[t]},{'data':[t]}]);a.turns=lambda *args:next(pages)
        stop=SimpleNamespace(is_set=lambda:False,wait=lambda _:None)
        with patch('bridge.codex.subprocess.run',return_value=SimpleNamespace(returncode=0)),patch('bridge.codex.time.monotonic',side_effect=[0,0,0,20,20]):
            with self.assertRaises(TurnEnded) as e:a.run({'message_id':'m','thread_id':'t','prompt':'hello'},lambda t:None,stop)
        self.assertEqual(e.exception.status,'interrupted')

if __name__=='__main__':unittest.main()
