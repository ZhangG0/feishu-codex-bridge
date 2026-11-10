"""Opt-in local workflow ingress; durable spool is delivery buffering, not a task ledger."""
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

class WorkflowIngress:
    def __init__(self, config, on_received=None):
        self.url = config['url'].rstrip('/')
        parsed = urlparse(self.url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path not in ('', '/'):
            raise ValueError('Workflow ingress requires a loopback HTTP URL')
        self.token_file = Path(config['token_file'])
        self.directory = Path(config['spool_directory'])
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.project = config.get('project_key')
        self.on_received = on_received

    def enqueue(self, message, sender, binding, text):
        project = binding.get('project_key') or self.project
        if not project:
            raise ValueError('Workflow project_key is required')
        key = hashlib.sha256(('feishu:' + message['chat_id'] + ':' + message['message_id']).encode()).hexdigest()
        payload = {'source': 'feishu', 'source_message_id': message['message_id'],
                   'conversation_id': message['chat_id'], 'sender_id': sender,
                   'content': text, 'project_key': project, 'metadata': {'alias': binding['alias'], **({key:message[key] for key in ('parent_id','root_id') if message.get(key)})}}
        target = self.directory / (key + '.json')
        encoded = json.dumps(payload, ensure_ascii=False)
        if target.exists():
            if json.loads(target.read_text()) != payload:
                raise ValueError('Conflicting duplicate ingress message')
            self._received(payload)
            return
        # Link a fully fsynced file into the spool without overwriting an existing event.
        fd, name = tempfile.mkstemp(prefix='.ingress-', dir=self.directory)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, target)
            except FileExistsError:
                if json.loads(target.read_text()) != payload:
                    raise ValueError('Conflicting duplicate ingress message')
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(name)
        self._received(payload)

    def _received(self, payload):
        if self.on_received:
            self.on_received(payload)

    def verify_legacy_handoff(self, row, handoffs):
        rid = handoffs.get(row['message_id'])
        if not rid or row['status'] != 'needs_review':
            return False
        request = Request(self.url + '/api/requirements/' + rid,
                          headers={'Authorization': 'Bearer ' + self.token_file.read_text().strip()})
        with urlopen(request, timeout=5) as response:
            result = json.load(response)
        return (result.get('state') in ('paused', 'blocked') and not result.get('runs')
                and any(m.get('source') == 'feishu' and m.get('source_message_id') == row['message_id']
                        and m.get('content') == row['prompt'] for m in result.get('messages', [])))

    def forward_once(self):
        for path in sorted(self.directory.glob('*.json'), key=lambda p: p.stat().st_mtime):
            # Recover the receipt if the process died between spool fsync and outbox commit.
            self._received(json.loads(path.read_text()))
            token = self.token_file.read_text().strip()
            request = Request(self.url + '/api/messages', data=path.read_bytes(), method='POST',
                              headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            with urlopen(request, timeout=5) as response:
                result = json.load(response)
                if not result.get('message', {}).get('id'):
                    raise ValueError('Missing workflow receipt')
            path.unlink()  # After a receipt; a crash before unlink is safe to replay with stable identity.

    def run(self, stop):
        while not stop.is_set():
            try:
                self.forward_once()
            except Exception:
                log.warning('Workflow ingress unavailable; message retained in local spool')
            stop.wait(2)
