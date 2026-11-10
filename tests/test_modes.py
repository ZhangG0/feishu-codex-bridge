import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from bridge.service import Service
from bridge.store import Store

class ModeRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = self.root / 'workflow.sqlite3'
        with sqlite3.connect(self.ledger) as db:
            db.executescript('CREATE TABLE scheduler_controls(scope TEXT PRIMARY KEY,enabled INTEGER); CREATE TABLE messages(source TEXT,conversation_id TEXT,source_message_id TEXT); INSERT INTO scheduler_controls VALUES("workflow:demo",0);')
        self.store = Store(self.root / 'bridge.sqlite3')
        self.binding = {'chat_id':'chat', 'alias':'demo', 'thread_id':'thread', 'project_key':'demo'}
        self.config = {'bot_open_id':'bot','bindings':[self.binding], 'send_enabled':True,
                       'workflow':{'enabled':True,'url':'http://127.0.0.1:4317','token_file':str(self.root/'absent-token'),
                                   'spool_directory':str(self.root/'spool'),'mode_database':str(self.ledger),'project_key':'demo'}}
        self.feishu = Mock()
        self.feishu.lark.JSON.marshal = json.dumps
        self.service = Service(self.config,self.store,Mock(),self.feishu)
        self.event = {'event':{'sender':{'sender_type':'user','sender_id':{'open_id':'sender'}},
                              'message':{'message_id':'mid','chat_id':'chat','chat_type':'group','message_type':'text',
                                         'content':json.dumps({'text':'@bot hello'}),'mentions':[{'key':'@bot','id':{'open_id':'bot'}}]}}}
    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()
    def test_disabled_workflow_queues_and_receipts_without_http_or_token(self):
        with patch('bridge.workflow.urlopen',side_effect=AssertionError('HTTP must not be needed')):
            self.service.receive(self.event)
        self.assertTrue(self.store.job_exists('mid'))
        self.assertEqual(list((self.root/'spool').glob('*.json')),[])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
    def test_registered_workflow_retry_never_enters_direct_queue(self):
        with sqlite3.connect(self.ledger) as db:
            db.execute('INSERT INTO messages VALUES(?,?,?)',('feishu','chat','mid'))
        self.service.receive(self.event)
        self.assertFalse(self.store.job_exists('mid'))
        self.assertEqual(len(list((self.root/'spool').glob('*.json'))),1)
    def test_direct_retry_after_workflow_reenabled_keeps_original_job(self):
        self.service.receive(self.event)
        with sqlite3.connect(self.ledger) as db:
            db.execute('UPDATE scheduler_controls SET enabled=1')
        self.service.receive(self.event)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
        self.assertEqual(list((self.root/'spool').glob('*.json')),[])
    def test_unreadable_mode_ledger_buffers_instead_of_bypassing(self):
        self.config['workflow']['mode_database']=str(self.root/'missing.sqlite3')
        self.service.receive(self.event)
        self.assertFalse(self.store.job_exists('mid'))
        self.assertEqual(len(list((self.root/'spool').glob('*.json'))),1)
    def test_disabled_bridge_start_does_not_require_workflow_http(self):
        with patch('bridge.service.threading.Thread') as thread, patch('bridge.workflow.urlopen',side_effect=AssertionError('HTTP must not be needed')):
            self.service.start()
        self.feishu.start.assert_called_once()
        self.assertEqual(thread.call_count,5)
