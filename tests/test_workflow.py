import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from bridge.workflow import WorkflowIngress
from bridge.store import Store

class Response:
    def __init__(self, data): self.data = json.dumps(data).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return self.data

class WorkflowIngressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.token = root / 'token'
        self.token.write_text('test-token')
        self.spool = root / 'spool'
        self.config = {'url':'http://127.0.0.1:4317', 'token_file': str(self.token),
                       'spool_directory':str(self.spool), 'project_key':'demo'}
        self.ingress = WorkflowIngress(self.config)
        self.message = {'message_id':'mid','chat_id':'chat'}
        self.binding = {'alias':'project'}
    def tearDown(self): self.tmp.cleanup()
    def enqueue(self): self.ingress.enqueue(self.message, 'sender', self.binding, 'Build export')
    def test_spool_is_durable_and_deduplicated(self):
        self.enqueue(); self.enqueue()
        files = list(self.spool.glob('*.json'))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())['content'], 'Build export')
        with self.assertRaises(ValueError):
            self.ingress.enqueue(self.message, 'sender', self.binding, 'changed')
    def test_failed_delivery_survives_restart_and_success_removes_only_after_receipt(self):
        self.enqueue()
        with patch('bridge.workflow.urlopen', side_effect=OSError('offline')):
            with self.assertRaises(OSError): self.ingress.forward_once()
        recovered = WorkflowIngress(self.config)
        self.assertEqual(len(list(self.spool.glob('*.json'))), 1)
        with patch('bridge.workflow.urlopen', return_value=Response({'message': {'id':'M-1'},'duplicate':True})) as send:
            recovered.forward_once()
            self.assertEqual(send.call_args.args[0].get_header('Authorization'), 'Bearer test-token')
        self.assertEqual(list(self.spool.glob('*.json')), [])
    def test_no_receipt_does_not_remove_message_and_remote_host_is_rejected(self):
        self.enqueue()
        with patch('bridge.workflow.urlopen', return_value=Response({'error':'failed'})):
            with self.assertRaises(ValueError): self.ingress.forward_once()
        self.assertEqual(len(list(self.spool.glob('*.json'))), 1)
        with self.assertRaises(ValueError): WorkflowIngress(dict(self.config,url='https://remote.example'))
    def test_legacy_handoff_requires_original_message_and_paused_record(self):
        row={'message_id':'old','status':'needs_review','prompt':'Do not repeat'}
        good={'state':'paused','runs':[],'messages':[{'source':'feishu','source_message_id':'old','content':'Do not repeat'}]}
        with patch('bridge.workflow.urlopen', return_value=Response(good)):
            self.assertTrue(self.ingress.verify_legacy_handoff(row,{'old':'R-old'}))
        with patch('bridge.workflow.urlopen', return_value=Response(dict(good,state='running'))):
            self.assertFalse(self.ingress.verify_legacy_handoff(row,{'old':'R-old'}))
        with patch('bridge.workflow.urlopen', return_value=Response(dict(good,runs=[{'id':'run'}]))):
            self.assertFalse(self.ingress.verify_legacy_handoff(row,{'old':'R-old'}))
        self.assertFalse(self.ingress.verify_legacy_handoff(dict(row,status='running'),{'old':'R-old'}))
        self.assertFalse(self.ingress.verify_legacy_handoff(row,{}))

    def test_receipt_is_saved_after_spool_and_deduplicated_even_after_forwarding(self):
        store = Store(Path(self.tmp.name) / 'bridge.sqlite3')
        try:
            def receipt(payload):
                self.assertEqual(len(list(self.spool.glob('*.json'))), 1)
                store.workflow_received(payload)
            self.ingress = WorkflowIngress(self.config, on_received=receipt)
            self.enqueue(); self.enqueue()
            rows = store.db.execute('SELECT * FROM outbox').fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['parent_mid'], 'mid')
            self.assertIn('已收到并保存', rows[0]['text'])
            self.assertNotIn('Codex 已接收', rows[0]['text'])
            with patch('bridge.workflow.urlopen', return_value=Response({'message': {'id': 'M-1'}})):
                self.ingress.forward_once()
            self.enqueue()
            self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)
        finally:
            store.db.close()

    def test_receipt_failure_keeps_spool_and_restart_recovers_before_forwarding(self):
        with patch.object(self.ingress, 'on_received', side_effect=OSError('outbox unavailable')):
            with self.assertRaises(OSError): self.enqueue()
        self.assertEqual(len(list(self.spool.glob('*.json'))), 1)
        store = Store(Path(self.tmp.name) / 'bridge.sqlite3')
        try:
            recovered = WorkflowIngress(self.config, on_received=store.workflow_received)
            def forward(*args, **kwargs):
                self.assertEqual(store.db.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)
                return Response({'message': {'id': 'M-1'}})
            with patch('bridge.workflow.urlopen', side_effect=forward): recovered.forward_once()
            self.assertEqual(list(self.spool.glob('*.json')), [])
        finally: store.db.close()

    def test_failed_spool_save_never_sends_receipt(self):
        received = []
        self.ingress = WorkflowIngress(self.config, on_received=received.append)
        with patch('bridge.workflow.os.link', side_effect=OSError('disk error')):
            with self.assertRaises(OSError): self.enqueue()
        self.assertEqual(received, [])
