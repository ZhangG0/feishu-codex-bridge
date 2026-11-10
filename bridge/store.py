import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from .formatting import message_chunks, task_label


def chunks(text, size=2500):
    return message_chunks(text, size)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS jobs (
              message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
              sender_id TEXT NOT NULL, thread_id TEXT NOT NULL,
              alias TEXT NOT NULL, prompt TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'queued', turn_id TEXT,
              error TEXT, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
              id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
              due REAL DEFAULT 0, error TEXT);
            CREATE TABLE IF NOT EXISTS seen_turns (
              thread_id TEXT, turn_id TEXT, PRIMARY KEY(thread_id,turn_id));
            CREATE TABLE IF NOT EXISTS monitor_init (thread_id TEXT PRIMARY KEY);
        ''')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if 'task_no' not in {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}:
                self.db.execute('ALTER TABLE jobs ADD COLUMN task_no INTEGER')
                self.db.execute('UPDATE jobs SET task_no=rowid')
            self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS jobs_task_no ON jobs(task_no)')
            if 'parent_mid' not in {r[1] for r in self.db.execute('PRAGMA table_info(outbox)')}:
                self.db.execute('ALTER TABLE outbox ADD COLUMN parent_mid TEXT')

    def _output(self, key, chat, text, parent_mid=None):
        for i, part in enumerate(chunks(text)):
            ident = hashlib.sha256((key + ':' + str(i)).encode()).hexdigest()[:32]
            self.db.execute('INSERT OR IGNORE INTO outbox(id,chat_id,text,parent_mid) VALUES(?,?,?,?)',
                            (ident, chat, part, parent_mid))

    def _task_output(self, job, suffix, text):
        job = dict(job)
        label = task_label(job)
        # A literal label avoids interpreting user-provided summary as Markdown.
        import html
        label = html.escape(label).replace('*', '\\*').replace('_', '\\_').replace('`', '\\`').replace('[', '\\[').replace(']', '\\]')
        for i, part in enumerate(chunks(text, 2300)):
            self._output(job['message_id'] + suffix + ':part:' + str(i), job['chat_id'], label + '\n\n' + part, job['message_id'])

    def output(self, key, chat, text):
        with self.lock, self.db:
            self._output(key, chat, text)

    def workflow_received(self, payload):
        """Durable, deduplicated receipt after ingress has safely saved a message."""
        mid, chat = payload['source_message_id'], payload['conversation_id']
        with self.lock, self.db:
            self._output('workflow:received:' + chat + ':' + mid, chat,
                         '已收到并保存，等待处理。处理结果会另行回复。', mid)

    def direct_received(self, mid, chat):
        """Persist an immediate receipt after the direct job is queued."""
        with self.lock, self.db:
            self._output('bridge:direct-received:' + chat + ':' + mid, chat,
                         '已收到，正在转交给团队入口处理；完成后会回复结果。', mid)

    def job_exists(self, mid):
        with self.lock:
            return self.db.execute('SELECT 1 FROM jobs WHERE message_id=?', (mid,)).fetchone() is not None

    def enqueue(self, mid, chat, sender, binding, prompt, limit, receipt=False):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.db.execute('SELECT 1 FROM jobs WHERE message_id=?', (mid,)).fetchone():
                return False
            count = self.db.execute("SELECT count(*) FROM jobs WHERE thread_id=? AND status IN ('queued','running','needs_review')",
                                    (binding['thread_id'],)).fetchone()[0]
            blocked = self.db.execute("SELECT count(*) FROM jobs WHERE thread_id=? AND status='needs_review'", (binding['thread_id'],)).fetchone()[0]
            # One worker serves all bindings. Queued jobs in a paused thread
            # are skipped by claim(), so they do not delay other threads.
            ahead = self.db.execute("""SELECT count(*) FROM jobs j WHERE status='running'
                OR (status='queued' AND NOT EXISTS(SELECT 1 FROM jobs b
                  WHERE b.thread_id=j.thread_id AND b.status='needs_review'))""").fetchone()[0]
            status = 'rejected' if count >= limit else 'queued'
            number = self.db.execute('SELECT coalesce(max(task_no),0)+1 FROM jobs').fetchone()[0]
            self.db.execute('INSERT INTO jobs(message_id,chat_id,sender_id,thread_id,alias,prompt,status,created,task_no) VALUES(?,?,?,?,?,?,?,?,?)',
                            (mid, chat, sender, binding['thread_id'], binding['alias'], prompt, status, time.time(), number))
            if status == 'rejected':
                acknowledgement = '队列已满，本条未提交，请稍后重试。'
            elif blocked:
                acknowledgement = '消息已保存，但此前任务结果不确定，该聊天队列已暂停；正在只读核对，不会重发旧任务。当前待处理 %d 条。' % (count + 1)
            elif ahead:
                acknowledgement = '消息已保存并排队，桥接服务前面有 %d 条任务待处理；尚未提交给 Codex。' % ahead
            else:
                acknowledgement = None
            if acknowledgement:
                self._task_output({'message_id':mid,'chat_id':chat,'prompt':prompt,'task_no':number}, ':ack', acknowledgement)
            if receipt and status == 'queued':
                self._output('bridge:direct-received:' + chat + ':' + mid, chat,
                             '已收到，正在转交给团队入口处理；完成后会回复结果。', mid)
            return status == 'queued'

    def claim(self, allowed_threads=None):
        with self.lock, self.db:
            params = []
            thread_filter = ''
            if allowed_threads is not None:
                allowed_threads = list(dict.fromkeys(allowed_threads))
                if not allowed_threads:
                    return None
                thread_filter = ' AND j.thread_id IN (' + ','.join('?' for _ in allowed_threads) + ')'
                params.extend(allowed_threads)
            row = self.db.execute("""SELECT * FROM jobs j WHERE status='queued'""" + thread_filter + """
                AND NOT EXISTS(SELECT 1 FROM jobs b WHERE b.thread_id=j.thread_id
                  AND b.status IN ('running','needs_review')) ORDER BY created LIMIT 1""", params).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET status='running' WHERE message_id=?", (row['message_id'],))
                return dict(row)

    def attach_turn(self, mid, turn):
        with self.lock, self.db:
            old = self.db.execute('SELECT * FROM jobs WHERE message_id=?', (mid,)).fetchone()
            self.db.execute('UPDATE jobs SET turn_id=? WHERE message_id=?', (turn, mid))
            if old and not old['turn_id']:
                self._task_output(old, ':started', 'Codex 已接收，正在等待结果。')

    def finish(self, job, text, status='done'):
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET status=?,error=? WHERE message_id=?',
                            (status, None if status == 'done' else text, job['message_id']))
            current = self.db.execute('SELECT * FROM jobs WHERE message_id=?', (job['message_id'],)).fetchone()
            self._task_output(current, ':result', text)

    def recover(self):
        # A crash between turn/start and persisting its response is ambiguous.
        # Never automatically submit the prompt again.
        with self.lock, self.db:
            rows = self.db.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
            for row in rows:
                self.db.execute("UPDATE jobs SET status='needs_review' WHERE message_id=?", (row['message_id'],))
                self._task_output(row, ':recovery',
                             '桥接程序已重启：已有任务可能仍在 Codex 执行。后续队列暂缓，正在定期只读核对；不会重复提交旧指令。')

    def resolve(self, mid):
        with self.lock, self.db:
            n = self.db.execute("UPDATE jobs SET status='resolved' WHERE message_id=? AND status='needs_review'", (mid,)).rowcount
            if not n:
                raise ValueError('没有找到待核对的任务')

    def reviews(self):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE status='needs_review'")]

    def reconcile(self, job, turn):
        """Settle once after external evidence; never submit anything."""
        from .codex import final_text
        status = turn.get('status')
        if status not in ('completed', 'failed', 'interrupted'):
            return False
        with self.lock, self.db:
            n = self.db.execute("UPDATE jobs SET status=?,turn_id=?,error=NULL WHERE message_id=? AND status='needs_review'", ('done' if status == 'completed' else status, turn['id'], job['message_id'])).rowcount
            if not n:
                return False
            if status == 'completed':
                text = '已核实此前任务已完成，补回结果：\n' + (final_text(turn) or '任务已完成，没有文本回复。')
            else:
                text = '已核实此前任务已%s，可能有部分操作完成；未自动重试旧任务。' % ('中断' if status == 'interrupted' else '失败')
            self._task_output(job, ':reconciled', text + '\n该任务的队列阻塞已解除；如仍有其他待核对任务，会继续暂停。')
            return True

    def due(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM outbox WHERE status='pending' AND due<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
            return dict(row) if row else None

    def delivered(self, ident):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET status='sent' WHERE id=?", (ident,))

    def retry(self, row, error):
        attempts = row['attempts'] + 1
        with self.lock, self.db:
            self.db.execute('UPDATE outbox SET attempts=?,due=?,error=?,status=? WHERE id=?',
                            (attempts, time.time() + min(300, 2 ** min(attempts, 9)), error,
                             'failed' if attempts >= 20 else 'pending', row['id']))

    def monitor_seeded(self, tid):
        with self.lock:
            return bool(self.db.execute('SELECT 1 FROM monitor_init WHERE thread_id=?', (tid,)).fetchone())

    def seed(self, tid, completed_turns):
        with self.lock, self.db:
            self.db.executemany('INSERT OR IGNORE INTO seen_turns VALUES(?,?)', [(tid, t) for t in completed_turns])
            self.db.execute('INSERT OR IGNORE INTO monitor_init VALUES(?)', (tid,))

    def seen(self, tid, turn):
        with self.lock:
            return bool(self.db.execute('SELECT 1 FROM seen_turns WHERE thread_id=? AND turn_id=?', (tid, turn)).fetchone())

    def forward(self, tid, turn, bindings, text, source_mid=None):
        with self.lock, self.db:
            if self.seen(tid, turn):
                return
            own = self.db.execute('SELECT 1 FROM jobs WHERE thread_id=? AND turn_id=?', (tid, turn)).fetchone()
            if not own and source_mid:
                own = self.db.execute('SELECT 1 FROM jobs WHERE thread_id=? AND message_id=?', (tid, source_mid)).fetchone()
            if not own and text:
                for b in bindings:
                    self._output(tid + ':' + turn + ':' + b['chat_id'], b['chat_id'], text)
            self.db.execute('INSERT INTO seen_turns VALUES(?,?)', (tid, turn))

    def summary(self):
        with self.lock:
            return {table: [dict(r) for r in self.db.execute('SELECT status,count(*) AS count FROM ' + table + ' GROUP BY status')]
                    for table in ('jobs', 'outbox')}
