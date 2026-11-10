import fcntl
import json
import logging
import os
import threading
import hashlib
import time
import sqlite3
import sys
from pathlib import Path

from .codex import Codex, DesktopQueue, TurnEnded, final_text, source_message_id
from .store import Store
from .approvals import Approvals, card
from .formatting import markdown_card

log = logging.getLogger(__name__)


def route(event, config):
    """Accept group text mentioning this bot, not an arbitrary other member."""
    message = event.get('message', {})
    if event.get('sender', {}).get('sender_type') != 'user' or message.get('chat_type') != 'group':
        return None
    if message.get('message_type') != 'text':
        return None
    bot = config.get('bot_open_id')
    mentions = message.get('mentions', []) or []
    own = [m for m in mentions if m.get('id', {}).get('open_id') == bot]
    if not bot or not own:
        return None
    try:
        text = json.loads(message['content'])['text']
    except (KeyError, ValueError, TypeError):
        return None
    for mention in own:
        text = text.replace(mention['key'], '')
    text = text.strip()
    bindings = [b for b in config['bindings'] if b['chat_id'] == message.get('chat_id')]
    if not bindings:
        return None
    # Multi-thread groups must name an exact alias; single-thread groups can omit it.
    alias, _, rest = text.partition(' ')
    selected = next((b for b in bindings if b['alias'] == alias), None)
    if selected:
        return selected, rest.strip()
    if len(bindings) == 1:
        return bindings[0], text
    return None


class Feishu:
    def __init__(self, app_id, app_secret):
        import lark_oapi as lark
        self.lark = lark
        self.app_id, self.app_secret = app_id, app_secret
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    def send(self, row):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        if row.get('parent_mid'):
            from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
            req = ReplyMessageRequest.builder().message_id(row['parent_mid']).request_body(
                ReplyMessageRequestBody.builder().msg_type('interactive').content(json.dumps(markdown_card(row['text']), ensure_ascii=False)).uuid(row['id']).build()).build()
            response = self.client.im.v1.message.reply(req)
            if response.success():
                return
            # Explicit API rejection only: original may be deleted or inaccessible.
            # Network/transport exceptions propagate for retry, never duplicate-send.
            log.warning('Reply-to-original rejected (code=%s); sending to bound group', response.code)
        req = CreateMessageRequest.builder().receive_id_type('chat_id').request_body(
            CreateMessageRequestBody.builder().receive_id(row['chat_id']).msg_type('interactive')
            .content(json.dumps(markdown_card(row['text']), ensure_ascii=False)).uuid(row['id']).build()).build()
        response = self.client.im.v1.message.create(req)
        if not response.success():
            raise RuntimeError('Feishu API code=%s log_id=%s' % (response.code, response.get_log_id()))

    def send_approval(self, row):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody, PatchMessageRequest, PatchMessageRequestBody
        content = json.dumps(card(row), ensure_ascii=False)
        if row.get('message_id'):
            req = PatchMessageRequest.builder().message_id(row['message_id']).request_body(PatchMessageRequestBody.builder().content(content).build()).build()
            response = self.client.im.v1.message.patch(req)
            mid = row['message_id']
        else:
            req = CreateMessageRequest.builder().receive_id_type('chat_id').request_body(CreateMessageRequestBody.builder().receive_id(row['chat_id']).msg_type('interactive').content(content).uuid(hashlib.sha256(('approval:'+row['id']).encode()).hexdigest()[:32]).build()).build()
            response = self.client.im.v1.message.create(req)
            mid = response.data.message_id if response.success() else None
        if not response.success():
            raise RuntimeError('Feishu card API code=%s' % response.code)
        return mid

    def start(self, callback, card_callback=None):
        builder = self.lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(callback)
        if card_callback:
            builder.register_p2_card_action_trigger(card_callback)
        handler = builder.build()
        self.lark.ws.Client(self.app_id, self.app_secret, event_handler=handler,
                            log_level=self.lark.LogLevel.WARNING).start()


class Service:
    def __init__(self, config, store, codex, feishu):
        self.config, self.store, self.codex, self.feishu = config, store, codex, feishu
        self.stop = threading.Event()
        self.review_evidence = {}
        from .workflow import WorkflowIngress
        self.workflow = WorkflowIngress(config['workflow'], on_received=store.workflow_received) if config.get('workflow', {}).get('enabled') else None
        self.approvals = Approvals(config['database']) if config.get('approvals', {}).get('enabled') else None

    def _workflow_state(self, binding, source_message_id=None, conversation_id=None):
        """Read the shared workflow ledger without requiring its HTTP service.

        Errors fail closed: an unreadable setting means keep buffering through
        workflow ingress, never infer that the user switched the workflow off.
        """
        if not self.workflow:
            return False, False
        database = self.config.get('workflow', {}).get('mode_database') or default_workflow_database()
        team = binding.get('workflow_team_key') or binding.get('project_key') or self.config['workflow'].get('project_key')
        try:
            uri = Path(database).resolve().as_uri() + '?mode=ro'
            with sqlite3.connect(uri, uri=True, timeout=0.5) as db:
                row = db.execute('SELECT enabled FROM scheduler_controls WHERE scope=?', ('workflow:' + team,)).fetchone()
                enabled = row is None or bool(row[0])
                registered = False
                if source_message_id and conversation_id:
                    registered = db.execute('SELECT 1 FROM messages WHERE source=? AND conversation_id=? AND source_message_id=?',
                                             ('feishu', conversation_id, source_message_id)).fetchone() is not None
                return enabled, registered
        except (OSError, sqlite3.Error, TypeError):
            log.warning('Workflow mode ledger unavailable; ingress remains buffered')
            return True, False

    def approval_callback(self, data):
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
        try:
            if not self.approvals:
                return P2CardActionTriggerResponse({'toast': {'type': 'error', 'content': '远程审批未启用'}})
            event = json.loads(self.feishu.lark.JSON.marshal(data)).get('event', {})
            value = event.get('action', {}).get('value', {})
            context = event.get('context', {})
            ok, message = self.approvals.decide(value.get('approval_id'), event.get('operator', {}).get('open_id'), context.get('open_chat_id'), context.get('open_message_id'), value.get('decision'), self.config.get('approvals', {}).get('admin_open_ids', []))
            try:
                self.approvals.audit(value.get('approval_id'), event.get('operator', {}).get('open_id'), value.get('decision'), ok)
            except Exception:
                log.warning('Approval audit write unavailable')
            return P2CardActionTriggerResponse({'toast': {'type': 'success' if ok else 'error', 'content': message}})
        except Exception:
            log.warning('Approval callback rejected')
            return P2CardActionTriggerResponse({'toast': {'type': 'error', 'content': '审批未成功，请重试或在桌面处理'}})

    def approval_sender(self):
        while not self.stop.is_set():
            try:
                for row in self.approvals.pending_cards():
                    if self.stop.is_set():
                        break
                    # Don't deliver an expired request for the first time.
                    if row['state'] == 'expired' and not row['message_id']:
                        self.approvals.rendered(row['id'], row['state'], None)
                        continue
                    mid = self.feishu.send_approval(row)
                    self.approvals.rendered(row['id'], row['state'], mid)
            except Exception:
                log.warning('Approval card delivery unavailable; will retry')
            self.stop.wait(1)

    def receive(self, data):
        # SDK event callback must return quickly: persist, then acknowledge.
        event = json.loads(self.feishu.lark.JSON.marshal(data)).get('event', {})
        selected = route(event, self.config)
        if not selected:
            return
        binding, text = selected
        m = event['message']
        if not text:
            self.store.output(m['message_id'] + ':help', m['chat_id'], '请在 @机器人 后输入指令。')
            return
        # Preserve the route chosen for a message before any team-mode change.
        if self.store.job_exists(m['message_id']):
            return
        workflow_enabled, registered = self._workflow_state(binding, m['message_id'], m['chat_id'])
        if self.workflow and (workflow_enabled or registered):
            sender = event['sender'].get('sender_id', {}).get('open_id', 'unknown')
            self.workflow.enqueue(m, sender, binding, text)
            return
        if not self.config.get('send_enabled'):
            self.store.output(m['message_id'] + ':disabled', m['chat_id'],
                              '已收到。目前 Codex 发送接入尚未完成验证，未执行这条指令。')
            return
        sender = event['sender'].get('sender_id', {}).get('open_id', 'unknown')
        queued = self.store.enqueue(m['message_id'], m['chat_id'], sender, binding, text,
                                    self.config.get('max_pending_per_thread', 30), receipt=bool(self.workflow))

    def worker(self):
        while not self.stop.is_set():
            if not self.config.get('send_enabled'):
                self.stop.wait(1)
                continue
            # A job persisted in Bridge's direct queue keeps its original
            # routing decision even if the team switch changes afterwards.
            job = self.store.claim()
            if not job:
                self.stop.wait(1)
                continue
            try:
                text = self.codex.run(job, lambda turn: self.store.attach_turn(job['message_id'], turn), self.stop)
                self.store.finish(job, text)
            except TurnEnded as exc:
                self.store.finish(job, '任务已%s，可能有部分操作完成；本条不会自动重试，后续队列将继续处理。' % ('中断' if exc.status == 'interrupted' else '失败'), exc.status)
            except Exception:
                log.exception('Codex job failed; retained for local review')
                self.store.finish(job, '任务执行状态暂无法确认；该聊天队列已暂停，后续消息会保存。正在定期只读核对；不会自动重试旧指令。', 'needs_review')

    def review_once(self):
        jobs = self.store.reviews()
        active = {j['message_id'] for j in jobs}
        self.review_evidence = {k:v for k,v in self.review_evidence.items() if k in active}
        for job in jobs:
            try:
                turn = self.codex.inspect_job(job)
                if not turn:
                    self.review_evidence.pop(job['message_id'], None)
                    continue
                status = turn.get('status')
                if status == 'completed':
                    self.store.reconcile(job, turn)
                elif status in ('failed', 'interrupted') and turn.get('completedAt') is not None:
                    evidence = (turn['id'], status, turn['completedAt'])
                    previous = self.review_evidence.get(job['message_id'])
                    if previous and previous[0] == evidence and time.monotonic()-previous[1] >= 15:
                        self.store.reconcile(job, turn)
                    elif not previous or previous[0] != evidence:
                        self.review_evidence[job['message_id']] = (evidence, time.monotonic())
                else:
                    self.review_evidence.pop(job['message_id'], None)
            except Exception:
                self.review_evidence.pop(job['message_id'], None)
                log.warning('Read-only job reconciliation unavailable; queue remains paused')

    def reviewer(self):
        while not self.stop.is_set():
            if getattr(self.codex, 'closed', False):
                try:
                    old = self.codex
                    self.codex = (DesktopQueue(self.config['codex_command'], self.config.get('codex_queue_command'))
                                  if self.config.get('send_transport') == 'desktop_queue' else Codex(self.config['codex_command']))
                    try:
                        old.close()
                    except Exception:
                        pass
                    log.info('Codex reader reconnected; existing jobs will only be inspected')
                except Exception:
                    log.warning('Codex reader reconnect failed; will retry')
            self.review_once()
            self.stop.wait(30)

    def sender(self):
        while not self.stop.is_set():
            row = self.store.due()
            if row is None:
                self.stop.wait(1)
                continue
            try:
                self.feishu.send(row)
                self.store.delivered(row['id'])
            except Exception as exc:
                # Do not print tokens, message text, API response bodies, or SDK objects.
                log.warning('Feishu send failed (%s); scheduled retry', type(exc).__name__)
                self.store.retry(row, type(exc).__name__)

    def monitor_once(self, tid, bindings):
        first = self.codex.turns(tid)
        if not self.store.monitor_seeded(tid):
            # Establish a baseline. Never broadcast the old chat history on binding.
            self.store.seed(tid, [t['id'] for t in first['data'] if t.get('status') == 'completed'])
            return
        turns, page = [], first
        while True:
            reached = False
            for turn in page['data']:
                if self.store.seen(tid, turn['id']):
                    reached = True
                    break
                if turn.get('status') == 'completed':
                    turns.append(turn)
            if reached or not page.get('nextCursor'):
                break
            page = self.codex.turns(tid, page['nextCursor'])
        for turn in reversed(turns):
            self.store.forward(tid, turn['id'], bindings, final_text(turn), source_message_id(turn))

    def monitor(self):
        groups = {}
        for binding in self.config['bindings']:
            if binding.get('forward_desktop_replies', True):
                groups.setdefault(binding['thread_id'], []).append(binding)
        while not self.stop.is_set():
            for tid, bindings in groups.items():
                bindings = [b for b in bindings if not self.workflow or not self._workflow_state(b)[0]]
                if not bindings:
                    continue
                try:
                    self.monitor_once(tid, bindings)
                except Exception:
                    log.warning('Codex reply monitor unavailable; will retry')
            self.stop.wait(self.config.get('poll_seconds', 5))

    def start(self):
        self.store.recover()
        if self.approvals:
            self.approvals.invalidate()
            threading.Thread(target=self.approval_sender, daemon=True).start()
        if self.workflow:
            # Only require the workflow HTTP service for teams whose saved
            # setting is still on. A fully disabled setup can run Bridge alone.
            workflow_teams = [b for b in self.config['bindings'] if self._workflow_state(b)[0]]
            if workflow_teams:
                active = self.store.db.execute("SELECT * FROM jobs WHERE status IN ('queued','running','needs_review')").fetchall()
                handoffs = self.config['workflow'].get('legacy_review_handoffs', {})
                if any(row['message_id'] in handoffs and not self.workflow.verify_legacy_handoff(dict(row), handoffs) for row in active):
                    raise RuntimeError('Settle or explicitly preserve legacy bridge jobs before switching workflow ingress')
            targets = (self.workflow_loop, self.sender, self.worker, self.monitor, self.reviewer)
        else:
            targets = (self.worker, self.sender, self.monitor, self.reviewer)
        for target in targets:
            threading.Thread(target=target, daemon=True).start()
        try:
            self.feishu.start(self.receive, self.approval_callback if self.approvals else None)
        finally:
            self.stop.set()
            self.codex.close()

    def workflow_loop(self):
        while not self.stop.is_set():
            try:
                # A message safely spooled before the user disabled this team
                # has not necessarily reached the workflow ledger. Move only
                # unregistered messages to Bridge's durable direct queue.
                for path in sorted(self.workflow.directory.glob('*.json')):
                    payload = json.loads(path.read_text())
                    binding = next((b for b in self.config['bindings']
                                    if b['chat_id'] == payload.get('conversation_id')
                                    and b['alias'] == payload.get('metadata', {}).get('alias')), None)
                    if not binding:
                        continue
                    enabled, registered = self._workflow_state(binding, payload.get('source_message_id'), payload.get('conversation_id'))
                    if enabled:
                        continue
                    if not registered:
                        self.store.enqueue(payload['source_message_id'], payload['conversation_id'],
                                           payload.get('sender_id', 'unknown'), binding, payload['content'],
                                           self.config.get('max_pending_per_thread', 30))
                    path.unlink()
                self.workflow.forward_once()
            except Exception:
                log.warning('Workflow ingress unavailable; message retained in local spool')
            self.stop.wait(2)


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    if config.get('workflow', {}).get('enabled'):
        for key in ('token_file', 'spool_directory'):
            config['workflow'][key] = str((path.parent / config['workflow'][key]).resolve())
        if config['workflow'].get('mode_database'):
            mode_database = Path(config['workflow']['mode_database']).expanduser()
            config['workflow']['mode_database'] = str((mode_database if mode_database.is_absolute() else path.parent / mode_database).resolve())
    config['database'] = str((path.parent / config.get('database', 'var/bridge.sqlite3')).resolve())
    if config.get('send_enabled') and config.get('send_transport') != 'desktop_queue' and not config.get('exclusive_thread_confirmed'):
        raise ValueError('发送接入需先验证共享服务，或确认该聊天仅由桥接程序驱动。')
    keys = [(b['chat_id'], b['alias']) for b in config['bindings']]
    if len(keys) != len(set(keys)):
        raise ValueError('同一群里的聊天别名不能重复')
    for binding in config['bindings']:
        if not all(binding.get(k) for k in ('chat_id', 'alias', 'thread_id')) or 'REPLACE' in json.dumps(binding):
            raise ValueError('请先完成群和聊天绑定，不能使用示例占位符')
    return config


def default_workflow_database():
    """Match workflow/paths.ts so the bridge works across macOS, Windows and Linux."""
    override = os.environ.get('WORKFLOW_DATA_DIR')
    if override:
        data_dir = Path(override).expanduser()
    elif os.name == 'nt':
        data_dir = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local')) / 'codex-superagent-sample'
    elif sys.platform == 'darwin':
        data_dir = Path.home() / 'Library' / 'Application Support' / 'codex-superagent-sample'
    else:
        data_dir = Path(os.environ.get('XDG_DATA_HOME') or (Path.home() / '.local' / 'share')) / 'codex-superagent-sample'
    return data_dir / 'workflow.sqlite3'


def run(config):
    if not config.get('bot_open_id'):
        raise ValueError('缺少 bot_open_id，无法准确识别 @机器人')
    from .setup import credentials
    app_id, app_secret = credentials(Path(__file__).resolve().parent.parent)
    lock_path = Path(config['database']).parent / 'bridge.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Local state and SQLite contain conversation text; create new files privately.
    os.umask(0o077)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        codex = (DesktopQueue(config['codex_command'], config.get('codex_queue_command'))
                 if config.get('send_transport') == 'desktop_queue' else Codex(config['codex_command']))
        Service(config, Store(config['database']), codex, Feishu(app_id, app_secret)).start()
