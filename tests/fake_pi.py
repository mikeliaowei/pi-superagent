"""A stand-in for `pi --mode rpc`, speaking just enough of the protocol for the executor tests.

FAKE_PI_MODE picks the behavior of `prompt`: ok | error | dialog | hang | crash | handled.
"""

import json
import os
import sys

MODE = os.environ.get('FAKE_PI_MODE', 'ok')
SESSION = sys.argv[sys.argv.index('--session-id') + 1] if '--session-id' in sys.argv else 'none'
SESSION_DIR = sys.argv[sys.argv.index('--session-dir') + 1] if '--session-dir' in sys.argv else ''
streaming = False


def emit(obj):
    sys.stdout.write(json.dumps(obj) + '\n')
    sys.stdout.flush()


def respond(cmd, data=None, ok=True, error=''):
    rec = {'id': cmd.get('id'), 'type': 'response', 'command': cmd['type'], 'success': ok}
    if ok and data is not None:
        rec['data'] = data
    if not ok:
        rec['error'] = error
    emit(rec)


def assistant(text, stop='stop', error=''):
    msg = {'role': 'assistant', 'content': [{'type': 'text', 'text': text}], 'stopReason': stop}
    if error:
        msg['errorMessage'] = error
    emit({'type': 'message_start', 'message': msg})
    emit({'type': 'message_end', 'message': msg})


def settle():
    global streaming
    streaming = False
    emit({'type': 'agent_end'})
    emit({'type': 'agent_settled'})


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def prompt(cmd):
    global streaming
    if SESSION_DIR:  # like Pi: the conversation is kept in <session-dir>/<timestamp>_<session-id>.jsonl
        os.makedirs(SESSION_DIR, exist_ok=True)
        with open(os.path.join(SESSION_DIR, f'2026-01-01T00-00-00-000Z_{SESSION}.jsonl'), 'a') as f:
            f.write(json.dumps({'type': 'message', 'message': {'role': 'user', 'content': cmd['message']}}) + '\n')
    if MODE == 'handled':
        return respond(cmd, {'disposition': 'handled'})
    respond(cmd, {'disposition': 'started'})
    streaming = True
    emit({'type': 'agent_start'})
    emit({'type': 'turn_start'})
    if MODE == 'ok':
        assistant('first')
        emit({'type': 'agent_end'})  # not the end: a later turn follows before agent_settled
        assistant(f'final {SESSION} {os.environ.get("SUPERAGENT_TICKET_ID", "")}: {cmd["message"]}')
        settle()
    elif MODE == 'error':
        assistant('', stop='error', error='provider exploded')
        settle()
    elif MODE == 'dialog':
        emit({'type': 'extension_ui_request', 'id': 'n1', 'method': 'notify', 'message': 'hi'})
        emit({'type': 'extension_ui_request', 'id': 'd1', 'method': 'confirm', 'title': 'Allow?'})
        while (reply := read()) and not (reply.get('type') == 'extension_ui_response' and reply.get('id') == 'd1'):
            pass
        assistant(f'dialog cancelled={bool(reply and reply.get("cancelled"))}')
        settle()
    elif MODE == 'crash':
        sys.stderr.write('fatal: worker blew up\n')
        sys.stderr.flush()
        os._exit(3)
    # hang: nothing until abort or steer


def main():
    while (cmd := read()) is not None:
        kind = cmd.get('type')
        if kind == 'get_state':
            respond(cmd, {'sessionId': SESSION, 'isStreaming': streaming})
        elif kind == 'prompt':
            prompt(cmd)
        elif kind in ('steer', 'follow_up'):
            respond(cmd, {'disposition': 'queued'})
            if streaming:
                assistant(f'{kind}: {cmd["message"]}')
                settle()
        elif kind == 'abort':
            respond(cmd)
            if streaming:
                assistant('', stop='aborted')
                settle()
        elif kind == 'extension_ui_response':
            pass
        else:
            respond(cmd, ok=False, error=f'unknown command: {kind}')


if __name__ == '__main__':
    main()
