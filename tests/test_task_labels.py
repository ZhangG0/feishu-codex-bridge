import sqlite3,tempfile,unittest,json
from pathlib import Path
from types import SimpleNamespace
from bridge.store import Store
from bridge.formatting import task_summary
from bridge.approvals import Approvals,card
from bridge.service import Feishu

class TaskLabels(unittest.TestCase):
 def test_summary_bounded_and_first_meaningful_line(self):
  self.assertEqual(task_summary('\n---\n## 测试标题\n第二行'),'测试标题')
  self.assertEqual(len(task_summary('中'*60)),24)
  self.assertTrue(task_summary('中'*60).endswith('…'))
 def test_stable_number_across_restart_duplicates_and_statuses(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'s.db';s=Store(p);b={'thread_id':'t','chat_id':'c','alias':'联调'}
   s.enqueue('m','c','ou_owner',b,'研究仓库',30);j=s.claim();s.attach_turn('m','turn');s.finish(j,'结果正文')
   rows=s.db.execute('SELECT text,parent_mid FROM outbox').fetchall()
   self.assertEqual(len(rows),2)
   for r in rows:self.assertIn('#0001｜研究仓库',r[0]);self.assertEqual(r[1],'m')
   s.db.close();s=Store(p)
   self.assertFalse(s.enqueue('m','c','ou_owner',b,'重复',30))
   s.enqueue('next','c','ou_owner',b,'新任务',30)
   self.assertEqual(s.claim()['task_no'],2);s.db.close()
 def test_long_results_all_identified(self):
  with tempfile.TemporaryDirectory() as tmp:
   s=Store(Path(tmp)/'s.db');b={'thread_id':'t','chat_id':'c','alias':'联调'}
   s.enqueue('m','c','ou_owner',b,'标题',30);j=s.claim();s.finish(j,'中'*6001)
   rows=s.db.execute('SELECT text FROM outbox ORDER BY rowid').fetchall()
   for r in rows:self.assertIn('#0001｜标题',r[0])
   self.assertEqual(sum(r[0].count('中') for r in rows),6001);s.db.close()
 def test_legacy_migration_and_approval_label(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'s.db';s=Store(p);b={'thread_id':'t','chat_id':'c','alias':'联调'}
   s.enqueue('m','c','ou_owner',b,'原任务',30);s.claim();s.attach_turn('m','turn');s.db.close()
   d=sqlite3.connect(p);d.execute('DROP INDEX jobs_task_no');d.execute('ALTER TABLE jobs DROP COLUMN task_no');d.execute('ALTER TABLE outbox DROP COLUMN parent_mid');d.commit();d.close()
   s=Store(p);self.assertEqual(s.db.execute('SELECT task_no FROM jobs').fetchone()[0],1)
   a=Approvals(p);i=a.create({'hook_event_name':'PermissionRequest','session_id':'t','turn_id':'turn','tool_name':'Bash','tool_input':{'command':'echo test'}})
   self.assertIn('#0001｜原任务',json.dumps(card(a.get(i)),ensure_ascii=False));s.db.close()
 def test_reply_transport_and_rejected_reply_fallback(self):
  for success in (True,False):
   sent=[]
   class Msg:
    def reply(self,req):sent.append(('reply',req));return SimpleNamespace(success=lambda:success,code=230011)
    def create(self,req):sent.append(('create',req));return SimpleNamespace(success=lambda:True)
   f=Feishu.__new__(Feishu);f.client=SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=Msg())))
   f.send({'parent_mid':'m','chat_id':'c','id':'uuid','text':'#0001｜任务\n\n正文'})
   self.assertEqual([a[0] for a in sent],['reply'] if success else ['reply','create'])
if __name__=='__main__':unittest.main()
