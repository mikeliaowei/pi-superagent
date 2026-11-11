"""A stand-in for `copilot --acp --stdio`, speaking just enough ACP for the executor tests.

FAKE_COPILOT_MODE picks the behavior of session/prompt: ok | hang | crash | permission. Sessions it creates are
remembered in FAKE_COPILOT_STATE (a directory), so a new process can session/load them.
"""

import json
import os
import sys

MODE = os.environ.get('FAKE_COPILOT_MODE', 'ok')
STATE = os.environ.get('FAKE_COPILOT_STATE', '.')
pending_prompt = None


def emit(obj):
    sys.stdout.write(json.dumps(obj) + '\n')
    sys.stdout.flush()


def say(sid, text):
    emit({'jsonrpc': '2.0', 'method': 'session/update',
          'params': {'sessionId': sid, 'update': {'sessionUpdate': 'agent_message_chunk',
                                                  'content': {'type': 'text', 'text': text}}}})


def history(sid):
    path = os.path.join(STATE, sid)
    return open(path).read().splitlines() if os.path.exists(path) else None


for line in sys.stdin:
    msg = json.loads(line)
    method, rid, params = msg.get('method'), msg.get('id'), msg.get('params') or {}
    if method is None:  # our answer to a permission request
        if pending_prompt:
            sid, prid = pending_prompt
            say(sid, f"permission {msg['result']['outcome'].get('optionId')}")
            emit({'jsonrpc': '2.0', 'id': prid, 'result': {'stopReason': 'end_turn'}})
            pending_prompt = None
        continue
    if method == 'initialize':
        emit({'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True}}})
    elif method == 'session/new':
        sid = f's{len(os.listdir(STATE)) + 1}'
        open(os.path.join(STATE, sid), 'w').close()
        emit({'jsonrpc': '2.0', 'id': rid, 'result': {'sessionId': sid, 'models': {'availableModels': [
            {'modelId': 'auto', 'name': 'Auto', 'description': 'Let Copilot pick'},
            {'modelId': 'gpt-6', 'name': 'GPT 6', '_meta': {'copilotUsage': '1x', 'copilotEnablement': 'enabled'}},
            {'modelId': 'opus', 'name': 'Opus', '_meta': {'copilotUsage': '15x', 'copilotEnablement': 'enabled'}},
            {'modelId': 'off', 'name': 'Off', '_meta': {'copilotUsage': '1x', 'copilotEnablement': 'disabled'}}]}}})
    elif method == 'session/load':
        if history(params['sessionId']) is None:
            emit({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32002, 'message': 'session not found'}})
        else:
            say(params['sessionId'], 'replayed history')  # outside a run: must not count as a result
            emit({'jsonrpc': '2.0', 'id': rid, 'result': {}})
    elif method == 'session/prompt':
        sid, text = params['sessionId'], params['prompt'][0]['text']
        with open(os.path.join(STATE, sid), 'a') as f:
            f.write(text.replace('\n', ' ') + '\n')
        if MODE == 'crash':
            sys.exit(3)
        if MODE == 'hang':
            pending_prompt = (sid, rid)
            continue
        if MODE == 'permission':
            pending_prompt = (sid, rid)
            emit({'jsonrpc': '2.0', 'id': 'perm-1', 'method': 'session/request_permission',
                  'params': {'sessionId': sid, 'options': [{'optionId': 'no', 'kind': 'reject_once'},
                                                           {'optionId': 'yes', 'kind': 'allow_once'}]}})
            continue
        say(sid, 'looking')
        emit({'jsonrpc': '2.0', 'method': 'session/update',
              'params': {'sessionId': sid, 'update': {'sessionUpdate': 'tool_call', 'toolCallId': 't1'}}})
        say(sid, f'turns {len(history(sid))}: ')
        say(sid, text.split('\n\n---\n\n')[-1])  # the message, without a preamble
        emit({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})
    elif method == 'session/cancel':
        if pending_prompt:
            emit({'jsonrpc': '2.0', 'id': pending_prompt[1], 'result': {'stopReason': 'cancelled'}})
            pending_prompt = None
    elif rid is not None:
        emit({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'unknown method'}})
