import argparse
import json
import logging
import os
from pathlib import Path

from .codex import Codex
from .service import load_config, run
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='Feishu ↔ Codex local bridge')
    parser.add_argument('--config', default='config.json')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('run')
    sub.add_parser('threads', help='Read-only list of existing Codex chats')
    sub.add_parser('connect', help='Start Feishu setup connection without executing Codex tasks')
    sub.add_parser('status', help='Queue and outbox counts')
    resolve = sub.add_parser('resolve', help='Unblock a job after checking its outcome locally; never resubmits it')
    resolve.add_argument('message_id')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o077)
    if args.command == 'connect':
        from .setup import connect
        connect(Path(__file__).resolve().parent.parent)
        return
    if args.command == 'threads':
        command = ['codex', 'app-server', '--stdio']
        if Path(args.config).exists():
            command = json.loads(Path(args.config).read_text())['codex_command']
        codex = Codex(command)
        try:
            result = codex.call('thread/list', {'limit': 50, 'useStateDbOnly': True})
            print(json.dumps([{'id': t['id'], 'name': t.get('name'), 'cwd': t.get('cwd')}
                              for t in result['data']], ensure_ascii=False, indent=2))
        finally:
            codex.close()
        return
    config = load_config(args.config)
    if args.command == 'run':
        run(config)
    else:
        store = Store(config['database'])
        if args.command == 'resolve':
            store.resolve(args.message_id)
        print(json.dumps(store.summary(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
