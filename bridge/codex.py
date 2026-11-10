import json
import queue
import subprocess
import threading
import time
import re


class RpcError(RuntimeError):
    pass


class TurnEnded(RpcError):
    """A positively observed terminal turn; never resubmit this job."""
    def __init__(self, status):
        if status not in ('failed', 'interrupted'):
            raise ValueError('Not a terminal failure')
        self.status = status
        super().__init__('Codex turn ended: ' + status)


def final_text(turn):
    items = [i for i in turn.get('items', []) if i.get('type') == 'agentMessage']
    finals = [i.get('text', '') for i in items if i.get('phase') == 'final_answer']
    return '\n\n'.join(finals or [i.get('text', '') for i in items if i.get('phase') != 'commentary'])


def source_message_id(turn):
    for item in turn.get('items', []):
        if item.get('type') != 'userMessage':
            continue
        for content in item.get('content', []):
            match = re.search(r'\[FEISHU_BRIDGE_MESSAGE_ID:([^\]\s]+)\]', content.get('text', ''))
            if match:
                return match[1]
    return None


class Codex:
    def __init__(self, command):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.lock = threading.Lock()
        self.pending = {}
        self.counter = 0
        self.events = queue.Queue()
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.call('initialize', {'clientInfo': {'name': 'feishu_codex_bridge', 'version': '0.1.0'},
                                     'capabilities': {'experimentalApi': True}})
            self._send({'method': 'initialized'})
        except Exception:
            self.close()
            raise

    def _send(self, obj):
        with self.lock:
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()

    def _read(self):
        try:
            for line in self.proc.stdout:
                obj = json.loads(line)
                if 'id' in obj and 'method' not in obj:
                    with self.lock:
                        waiter = self.pending.pop(obj['id'], None)
                    if waiter:
                        waiter.put(obj)
                elif 'id' in obj:
                    # External group members cannot approve host access or tool actions.
                    self.events.put({'method': 'bridge/approvalRequired', 'params': obj.get('params', {})})
                    self._send({'id': obj['id'], 'error': {'code': -32000,
                                'message': 'Requires local review; bridge does not approve requests'}})
                else:
                    self.events.put(obj)
        except (OSError, ValueError):
            pass
        finally:
            with self.lock:
                self.closed = True
                for waiter in self.pending.values():
                    waiter.put({'error': {'message': 'Codex process disconnected'}})
                self.pending.clear()

    def call(self, method, params, timeout=40):
        with self.lock:
            if self.closed:
                raise RpcError('Codex process disconnected')
            self.counter += 1
            ident = self.counter
            waiter = queue.Queue()
            self.pending[ident] = waiter
        try:
            self._send({'id': ident, 'method': method, 'params': params})
            result = waiter.get(timeout=timeout)
        except queue.Empty:
            raise RpcError('Codex request timed out: ' + method)
        finally:
            with self.lock:
                self.pending.pop(ident, None)
        if 'error' in result:
            raise RpcError(str(result['error']))
        return result['result']

    def turns(self, tid, cursor=None):
        params = {'threadId': tid, 'limit': 50, 'itemsView': 'full', 'sortDirection': 'desc'}
        if cursor:
            params['cursor'] = cursor
        return self.call('thread/turns/list', params)

    def inspect_job(self, job):
        cursor = None
        for _ in range(20):
            page = self.turns(job['thread_id'], cursor)
            for turn in page['data']:
                mid = source_message_id(turn)
                if mid == job['message_id'] or (job.get('turn_id') == turn['id'] and mid is None):
                    return turn
            cursor = page.get('nextCursor')
            if not cursor:
                break
        return None  # Absence is not proof of non-execution.

    def run(self, job, attached, stop, timeout=1800):
        # Use only for a thread exclusively driven by this bridge until shared-host
        # concurrency is verified. Independent servers cannot detect desktop activity.
        self.call('thread/resume', {'threadId': job['thread_id'], 'excludeTurns': True})
        while not self.events.empty():
            self.events.get_nowait()
        result = self.call('turn/start', {'threadId': job['thread_id'], 'input': [
            {'type': 'text', 'text': '[FEISHU_BRIDGE_MESSAGE_ID:%s]\n%s' %
             (job['message_id'], job['prompt'])}]})
        turn_id = result['turn']['id']
        attached(turn_id)
        end = time.monotonic() + timeout
        while not stop.is_set() and time.monotonic() < end:
            try:
                event = self.events.get(timeout=1)
            except queue.Empty:
                if self.closed:
                    raise RpcError('Codex disconnected after submitting task')
                continue
            params = event.get('params', {})
            if params.get('threadId') != job['thread_id']:
                continue
            if event.get('method') == 'bridge/approvalRequired':
                raise RpcError('任务需要本机权限审批；桥接程序没有批准，请在本机核对任务状态。')
            if event.get('method') == 'turn/completed' and params['turn']['id'] == turn_id:
                turn = params['turn']
                if turn.get('status') != 'completed':
                    if turn.get('status') in ('failed', 'interrupted'):
                        raise TurnEnded(turn['status'])
                    raise RpcError('任务未成功完成：' + turn.get('status', 'unknown'))
                # Stream completion need not carry the full item list.
                if not final_text(turn):
                    page = self.turns(job['thread_id'])
                    turn = next((t for t in page['data'] if t['id'] == turn_id), turn)
                return final_text(turn) or '任务已完成，没有文本回复。'
        raise RpcError('等待结果中断或超时；任务可能仍在运行，请在本机核对，勿重复提交。')

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


class DesktopQueue(Codex):
    """Use the supported CLI queue; let the desktop host retain the writer lock."""
    def __init__(self, command, queue_command=None):
        super().__init__(command)
        self.queue_command = queue_command or ['codex', 'queue']

    def run(self, job, attached, stop, timeout=1800):
        baseline = {t['id'] for t in self.turns(job['thread_id'])['data']}
        mid = job['message_id']
        prompt = '[FEISHU_BRIDGE_MESSAGE_ID:%s]\n%s' % (mid, job['prompt'])
        result = subprocess.run(self.queue_command + ['--thread', job['thread_id'], '--message', prompt],
                                capture_output=True, text=True, timeout=40)
        if result.returncode:
            raise RpcError('Codex queue submission failed; local review required')
        if '--resume' in self.queue_command and 'resum' in result.stderr.lower() and any(word in result.stderr.lower() for word in ('failed', 'unable', 'cannot')):
            raise RpcError('Message queued but session resume was not confirmed; inspect original queue before retry')
        end = time.monotonic() + timeout
        found_id = None
        terminal_seen = None
        while not stop.is_set() and time.monotonic() < end:
            page = self.turns(job['thread_id'])
            found = None
            while True:
                reached = False
                for turn in page['data']:
                    if turn['id'] == found_id or source_message_id(turn) == mid:
                        found = turn
                        break
                    if turn['id'] in baseline:
                        reached = True
                        break
                if found or reached or not page.get('nextCursor'):
                    break
                page = self.turns(job['thread_id'], page['nextCursor'])
            if found:
                if not found_id:
                    found_id = found['id']
                    attached(found_id)
                if found.get('status') == 'completed':
                    return final_text(found) or '任务已完成，没有文本回复。'
                # A separate reader reconstructs unfinished persisted turns as
                # interrupted while the desktop writer is still working. Only
                # trust a stable terminal record carrying an actual end time.
                if found.get('status') in ('failed', 'interrupted') and found.get('completedAt') is not None:
                    evidence = (found['id'], found['status'], found['completedAt'])
                    if terminal_seen is None or terminal_seen[0] != evidence:
                        terminal_seen = (evidence, time.monotonic())
                    elif time.monotonic() - terminal_seen[1] >= 15:
                        raise TurnEnded(found['status'])
                else:
                    terminal_seen = None
            stop.wait(2)
        raise RpcError('Desktop queue result timed out; task may still be pending or awaiting local approval')
