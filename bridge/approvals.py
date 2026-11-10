"""Request-scoped remote approvals. Never infer an owner from group membership."""
import json
import os
import secrets
import sqlite3
import sys
import time
try:
    from .formatting import task_label
except ImportError:  # Executed directly by the Codex hook command.
    from formatting import task_label
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Approvals:
    def __init__(self, database):
        self.path = str(database)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                source_mid TEXT NOT NULL, chat_id TEXT NOT NULL, sender_id TEXT NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL, decision TEXT, actor TEXT,
                expires REAL NOT NULL, heartbeat REAL NOT NULL, message_id TEXT,
                updated REAL NOT NULL, rendered TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS approval_audit (
                id INTEGER PRIMARY KEY, request_id TEXT, actor TEXT,
                decision TEXT, accepted INTEGER, created REAL)''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, event, ttl=300):
        tid, turn = event.get('session_id'), event.get('turn_id')
        if event.get('hook_event_name') != 'PermissionRequest' or not tid or not turn:
            return None
        payload = json.dumps({'tool': event.get('tool_name'), 'input': event.get('tool_input')}, ensure_ascii=False)
        # The complete action must fit on the card; never approve a truncated action.
        if len(payload.encode()) > 12000:
            return None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            jobs = db.execute("SELECT * FROM jobs WHERE thread_id=? AND turn_id=? AND status='running'", (tid, turn)).fetchall()
            if len(jobs) != 1 or not jobs[0]['sender_id'].startswith('ou_'):
                return None
            j = jobs[0]
            payload = json.dumps({'tool':event.get('tool_name'), 'input':event.get('tool_input'), 'task_label':task_label(dict(j))},ensure_ascii=False)
            ident = secrets.token_hex(16)
            now = time.time()
            db.execute('INSERT INTO approvals(id,thread_id,turn_id,source_mid,chat_id,sender_id,payload,state,expires,heartbeat,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (ident, tid, turn, j['message_id'], j['chat_id'], j['sender_id'], payload, 'pending', now+ttl, now, now))
            return ident

    def get(self, ident):
        with self.connect() as db:
            r = db.execute('SELECT * FROM approvals WHERE id=?', (ident,)).fetchone()
            return dict(r) if r else None

    def audit(self, ident, actor, decision, accepted):
        with self.connect() as db:
            db.execute('INSERT INTO approval_audit(request_id,actor,decision,accepted,created) VALUES(?,?,?,?,?)',
                       (ident,actor,decision,int(accepted),time.time()))

    def pulse(self, ident):
        with self.connect() as db:
            db.execute("UPDATE approvals SET heartbeat=? WHERE id=? AND state IN ('pending','decided')", (time.time(), ident))

    def expire(self, ident):
        with self.connect() as db:
            db.execute("UPDATE approvals SET state='expired',updated=? WHERE id=? AND state IN ('pending','decided')", (time.time(), ident))

    def decide(self, ident, actor, chat, message, decision, admins):
        if decision not in ('allow', 'deny') or not actor:
            return False, '无效操作'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            r = db.execute('SELECT * FROM approvals WHERE id=?', (ident,)).fetchone()
            if not r or r['chat_id'] != chat or not message or r['message_id'] != message:
                return False, '审批请求不匹配'
            if actor != r['sender_id'] and actor not in admins:
                return False, '仅管理员或本条任务发起人可审批'
            if r['state'] != 'pending':
                return False, '此请求已处理或失效'
            now = time.time()
            job = db.execute("SELECT 1 FROM jobs WHERE message_id=? AND turn_id=? AND status='running'", (r['source_mid'],r['turn_id'])).fetchone()
            if r['expires'] <= now or r['heartbeat'] < now-5 or not job:
                db.execute("UPDATE approvals SET state='expired',updated=? WHERE id=?", (now,ident))
                return False, '请求已过期或任务已结束'
            db.execute("UPDATE approvals SET state='decided',decision=?,actor=?,updated=? WHERE id=?", (decision,actor,now,ident))
            return True, '决定已记录，等待交回 Codex'

    def consume(self, ident):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            r = db.execute('SELECT * FROM approvals WHERE id=?', (ident,)).fetchone()
            now = time.time()
            job = db.execute("SELECT 1 FROM jobs WHERE message_id=? AND turn_id=? AND status='running'",(r['source_mid'],r['turn_id'])).fetchone() if r else None
            if not r or r['state'] != 'decided' or r['expires'] <= now or not job:
                return None
            db.execute("UPDATE approvals SET state='returned',updated=? WHERE id=?", (now,ident))
            return r['decision']

    def invalidate(self):
        with self.connect() as db:
            db.execute("UPDATE approvals SET state='expired',updated=? WHERE state IN ('pending','decided')", (time.time(),))

    def pending_cards(self):
        with self.connect() as db:
            now = time.time()
            db.execute("UPDATE approvals SET state='expired',updated=? WHERE state IN ('pending','decided') AND (expires<=? OR heartbeat<? OR NOT EXISTS(SELECT 1 FROM jobs j WHERE j.message_id=approvals.source_mid AND j.turn_id=approvals.turn_id AND j.status='running'))", (now,now,now-5))
            return [dict(r) for r in db.execute("SELECT * FROM approvals WHERE rendered IS NULL OR rendered!=state")]

    def rendered(self, ident, state, message):
        with self.connect() as db:
            db.execute('UPDATE approvals SET rendered=?,message_id=? WHERE id=?', (state,message,ident))


def card(r):
    labels = {'pending':'等待审批', 'decided':'决定已记录，等待交回 Codex', 'returned':'决定已交回审批钩子，执行结果待反馈', 'expired':'已失效，请在桌面查看'}
    p = json.loads(r['payload'])
    elements = [{'tag':'div','text':{'tag':'plain_text','content':
        (p.get('task_label','')+'\n' if p.get('task_label') else '')+'请求 '+r['id'][:8]+'\n工具：'+str(p['tool'])+'\n完整操作：\n'+json.dumps(p['input'],ensure_ascii=False,indent=2)+'\n\n状态：'+labels[r['state']]+'\n仅管理员或本条任务发起人可审批；5 分钟内有效。'}}]
    if r['state']=='pending':
        elements.append({'tag':'action','actions':[{'tag':'button','text':{'tag':'plain_text','content':label},'type':style,'value':{'approval_id':r['id'],'decision':value}} for label,style,value in [('批准本次','primary','allow'),('拒绝','danger','deny')]]})
    return {'config':{'wide_screen_mode':True,'update_multi':True},'header':{'title':{'tag':'plain_text','content':'Codex 权限审批'},'template':'orange' if r['state']=='pending' else 'grey'},'elements':elements}


def hook():
    os.umask(0o077)
    ident = None
    approvals = None
    try:
        event = json.load(sys.stdin)
        config = json.loads((ROOT/'config.json').read_text())
        settings = config.get('approvals',{})
        if not settings.get('enabled'):
            print('{}'); return
        database = (ROOT/config.get('database','var/bridge.sqlite3')).resolve()
        approvals = Approvals(database)
        # Worker may be a polling interval behind the active turn. Bind only by exact turn.
        for _ in range(12):
            ident = approvals.create(event)
            if ident: break
            if not any(b['thread_id']==event.get('session_id') for b in config['bindings']): break
            time.sleep(.5)
        if not ident:
            print('{}'); return
        end=time.monotonic()+300
        while time.monotonic()<end:
            approvals.pulse(ident)
            r=approvals.get(ident)
            if r['state']=='decided':
                decision=approvals.consume(ident)
                if decision:
                    print(json.dumps({'hookSpecificOutput':{'hookEventName':'PermissionRequest','decision':{'behavior':decision}}}));return
            if r['state'] not in ('pending','decided'): break
            time.sleep(.25)
        approvals.expire(ident)
        print('{}') # Normal desktop approval remains available; never auto-allow on timeout.
    except Exception:
        if ident and approvals:
            try: approvals.expire(ident)
            except Exception: pass
        print('{}')


if __name__=='__main__':
    hook()
