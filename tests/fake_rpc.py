"""Protocol fixture: never calls a model or any external service."""
import json
import sys


def emit(obj):
    print(json.dumps(obj), flush=True)


for line in sys.stdin:
    req = json.loads(line)
    method = req.get('method')
    if method == 'initialize':
        emit({'id': req['id'], 'result': {}})
    elif method == 'initialized':
        continue
    elif method == 'thread/resume':
        emit({'id': req['id'], 'result': {'thread': {'id': 'thread_a'}}})
    elif method == 'turn/start':
        emit({'method': 'turn/completed', 'params': {'threadId': 'thread_a',
              'turn': {'id': 'turn_a', 'status': 'completed', 'items': [
                  {'type': 'agentMessage', 'phase': 'final_answer', 'text': '协议测试回答'}]}}})
        emit({'id': req['id'], 'result': {'turn': {'id': 'turn_a'}}})
    elif method == 'test/approval':
        emit({'id': 900, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': 'thread_a'}})
        emit({'id': req['id'], 'result': {}})
    elif 'error' in req:
        # A response to a server-initiated request needs no further response.
        continue
    else:
        emit({'id': req['id'], 'error': {'code': -32601, 'message': 'unknown method'}})
