import json
import tempfile
import time
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from bridge.approvals import Approvals, card
from bridge.store import Store
from bridge.service import Service

class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'state.db'
        self.store=Store(self.path)
        self.binding={'chat_id':'oc_group','alias':'联调','thread_id':'thread'}
        self.store.enqueue('mid','oc_group','ou_sender',self.binding,'hello',30)
        self.job=self.store.claim()
        self.store.attach_turn('mid','turn')
        self.a=Approvals(self.path)
        self.event={'hook_event_name':'PermissionRequest','session_id':'thread','turn_id':'turn','tool_name':'Bash','tool_input':{'command':'echo hello'}}
        self.ident=self.a.create(self.event)
        self.a.rendered(self.ident,'pending','om_card')

    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def decide(self,actor='ou_sender',decision='allow',chat='oc_group',mid='om_card'):
        return self.a.decide(self.ident,actor,chat,mid,decision,['ou_admin'])

    def test_only_requester_or_admin(self):
        self.assertFalse(self.decide('ou_other')[0])
        self.assertTrue(self.decide('ou_admin')[0])
        self.assertEqual(self.a.consume(self.ident),'allow')

    def test_requester_can_deny_once(self):
        self.assertTrue(self.decide(decision='deny')[0])
        self.assertFalse(self.decide()[0])
        self.assertEqual(self.a.consume(self.ident),'deny')
        self.assertIsNone(self.a.consume(self.ident))

    def test_wrong_group_message_and_action(self):
        self.assertFalse(self.decide(chat='other')[0])
        self.assertFalse(self.decide(mid='other')[0])
        self.assertFalse(self.decide(decision='allowForSession')[0])
        self.assertEqual(self.a.get(self.ident)['state'],'pending')

    def test_no_mapping_to_another_desktop_turn(self):
        self.assertIsNone(self.a.create(dict(self.event,turn_id='other')))
        self.assertIsNone(self.a.create(dict(self.event,session_id='other')))

    def test_complete_payload_no_truncation(self):
        self.assertIsNone(self.a.create(dict(self.event,tool_input={'command':'中'*5000})))
        text=json.dumps(card(self.a.get(self.ident)),ensure_ascii=False)
        self.assertIn('echo hello',text)
        self.assertIn('批准本次',text)

    def test_expired_and_dead_hook_rejected(self):
        with self.a.connect() as d:
            d.execute('UPDATE approvals SET heartbeat=? WHERE id=?',(time.time()-10,self.ident))
        self.assertFalse(self.decide()[0])
        self.assertEqual(self.a.get(self.ident)['state'],'expired')

    def test_finished_job_cannot_approve(self):
        self.store.finish(self.job,'done')
        self.assertFalse(self.decide()[0])

    def test_task_finished_between_click_and_hook(self):
        self.assertTrue(self.decide()[0])
        self.store.finish(self.job,'done')
        self.assertIsNone(self.a.consume(self.ident))

    def test_simultaneous_decisions_only_one_wins(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda v:self.decide(decision=v)[0],['allow','deny']))
        self.assertEqual(sum(results),1)

    def test_restart_cancels_pending_and_decided(self):
        self.decide()
        self.a.invalidate()
        self.assertIsNone(self.a.consume(self.ident))

    def test_expiry_removes_buttons(self):
        self.a.expire(self.ident)
        r=self.a.pending_cards()[0]
        self.assertEqual(r['state'],'expired')
        self.assertNotIn('actions',json.dumps(card(r)))

    def test_callback_uses_authenticated_operator_and_context(self):
        class JSON:
            marshal=staticmethod(json.dumps)
        class Lark:
            pass
        Lark.JSON=JSON
        class Feishu:
            lark=Lark
        config={'database':str(self.path),'approvals':{'enabled':True,'admin_open_ids':['ou_admin']}}
        service=Service(config,self.store,None,Feishu())
        value={'approval_id':self.ident,'decision':'allow','actor':'ou_admin'}
        e={'event':{'operator':{'open_id':'ou_other'},'context':{'open_chat_id':'oc_group','open_message_id':'om_card'},'action':{'value':value}}}
        r=service.approval_callback(e)
        self.assertEqual(r.toast.type,'error')
        e['event']['operator']['open_id']='ou_sender'
        self.assertEqual(service.approval_callback(e).toast.type,'success')

    def test_hook_returns_only_verified_decision(self):
        import io
        from bridge.approvals import hook
        config={'database':str(self.path),'bindings':[self.binding],'approvals':{'enabled':True}}
        old_create=Approvals.create
        def create(a,event,ttl=300):
            ident=old_create(a,event,ttl)
            a.rendered(ident,'pending','om_new')
            a.decide(ident,'ou_sender','oc_group','om_new','allow',[])
            return ident
        output=io.StringIO()
        with patch('bridge.approvals.Path.read_text',return_value=json.dumps(config)),patch('bridge.approvals.sys.stdin',io.StringIO(json.dumps(self.event))),patch('bridge.approvals.sys.stdout',output),patch.object(Approvals,'create',create):
            hook()
        r=json.loads(output.getvalue())
        self.assertEqual(r['hookSpecificOutput']['decision']['behavior'],'allow')

if __name__=='__main__': unittest.main()
