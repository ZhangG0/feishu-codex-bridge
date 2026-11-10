"""Connect the app before publishing; record only routing metadata for onboarding."""
import asyncio
import json
import os
from pathlib import Path

import lark_oapi as lark
from lark_oapi.channel.bot_identity import fetch_bot_identity


def credentials(root):
    path = Path(root) / 'var' / 'credentials.json'
    saved = json.loads(path.read_text()) if path.exists() else {}
    app_id = os.environ.get('FEISHU_APP_ID') or saved.get('app_id')
    secret = os.environ.get('FEISHU_APP_SECRET') or saved.get('app_secret')
    if not app_id or not secret:
        raise ValueError('缺少本机飞书应用凭据')
    return app_id, secret


def connect(root):
    os.umask(0o077)
    root = Path(root)
    app_id, secret = credentials(root)
    client = lark.Client.builder().app_id(app_id).app_secret(secret).build()
    identity = asyncio.run(fetch_bot_identity(client.config))
    if identity:
        (root / 'var' / 'bot.json').write_text(json.dumps({'open_id': identity.open_id, 'name': identity.name}, ensure_ascii=False))
        print('机器人身份已获取并保存在本机。', flush=True)
    else:
        print('机器人尚未激活，发布应用后重新获取身份。', flush=True)

    def received(data):
        event = json.loads(lark.JSON.marshal(data)).get('event', {})
        message = event.get('message', {})
        if message.get('chat_type') != 'group' or event.get('sender', {}).get('sender_type') != 'user':
            return
        # No conversation body or sender details are recorded during setup.
        path = root / 'var' / 'discovered-chats.json'
        chats = json.loads(path.read_text()) if path.exists() else []
        chat_id = message.get('chat_id')
        if chat_id and chat_id not in chats:
            chats.append(chat_id)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps(chats))
            temp.replace(path)
            print('已识别一个 @机器人的群，群 ID 已保存在本机。', flush=True)

    handler = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(received).build()
    ws = lark.ws.Client(app_id, secret, event_handler=handler, log_level=lark.LogLevel.WARNING)
    print('正在启动飞书长连接（配置阶段，不执行 Codex 指令、不向群发送消息）。', flush=True)
    ws.start()
