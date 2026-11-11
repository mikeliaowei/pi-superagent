"""An asyncio client for `copilot --acp --stdio`: GitHub Copilot CLI as the worker, over the Agent Client Protocol.

JSON-RPC 2.0, one message per line (https://agentclientprotocol.com). It has PiRpc's surface: start, run, steer,
request('abort') and close. Copilot cannot be steered: a prompt sent during a run cancels that run, so `steer`
refuses and the Worker keeps the text for the next run. The session id is kept in `session_file`, so a later
process (a retry, an answer) resumes the conversation with session/load.
"""

import asyncio
import contextlib
import logging
import os

from .pi_rpc import PiRpc, RpcError, RunResult

log = logging.getLogger('superagent.copilot')

ERRORS = {'refusal': 'the model refused', 'max_tokens': 'the model hit its output limit',
          'max_turn_requests': 'the run hit its model call limit'}


class CopilotAcp(PiRpc):
    NAME = 'copilot'

    def __init__(self, session_file='', preamble=''):
        super().__init__()
        self.session_file, self.preamble = session_file, preamble
        self.session, self.fresh = '', False
        self.models = []  # what session/new or session/load offered: [{modelId, name, _meta}]
        self._said = None  # the running prompt's latest agent message; None outside a run

    async def start(self, argv, cwd, env):
        await super().start(argv, cwd, env)
        await self._call('initialize', {'protocolVersion': 1, 'clientCapabilities': {}}, 120)
        try:
            with open(self.session_file, encoding='utf-8') as f:
                saved = f.read().strip()
        except OSError:
            saved = ''
        if saved:
            try:
                r = await self._call('session/load', {'sessionId': saved, 'cwd': cwd, 'mcpServers': []}, 120)
                self.session, self.models = saved, (r.get('models') or {}).get('availableModels') or []
                return
            except RpcError as e:
                if self.exit_error:
                    raise
                log.warning('copilot could not resume session %s, starting a new one: %s', saved, e)
        r = await self._call('session/new', {'cwd': cwd, 'mcpServers': []}, 120)
        self.session, self.fresh = r['sessionId'], True
        self.models = (r.get('models') or {}).get('availableModels') or []
        if not self.session_file:  # only asked for its models
            return
        try:
            os.makedirs(os.path.dirname(self.session_file), exist_ok=True)
            with open(self.session_file, 'w', encoding='utf-8') as f:
                f.write(self.session)
        except OSError as e:  # this run still works; a later one starts a new session
            log.warning('could not save copilot session %s to %s: %s', self.session, self.session_file, e)

    # Protocol
    def _ask(self, method, params):
        """Send a request; the future gets its JSON-RPC result, or RpcError."""
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        asyncio.ensure_future(self._send_or_fail(fut, {'jsonrpc': '2.0', 'id': rid, 'method': method,
                                                        'params': params}))
        return fut

    async def _send_or_fail(self, fut, obj):
        try:
            await self._send(obj)
        except RpcError as e:
            self._pending.pop(obj['id'], None)
            if not fut.done():
                fut.set_exception(e)

    async def _call(self, method, params, timeout):
        fut = self._ask(method, params)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RpcError(f'{method}: no response in {timeout}s') from None

    async def _route(self, rec):
        rid, method = rec.get('id'), rec.get('method')
        if method is None and rid in self._pending:
            fut = self._pending.pop(rid)
            if fut.done():
                return
            if 'error' in rec:
                fut.set_exception(RpcError((rec['error'] or {}).get('message') or f'{rid} failed'))
            else:
                fut.set_result(rec.get('result') or {})
        elif method == 'session/request_permission' and rid is not None:
            # --allow-all-tools should mean none arrive; nobody is at a terminal to answer one.
            options = (rec.get('params') or {}).get('options') or []
            allow = next((o for o in options if str(o.get('kind', '')).startswith('allow')), None)
            outcome = {'outcome': 'selected', 'optionId': allow['optionId']} if allow else {'outcome': 'cancelled'}
            await self._reply(rid, {'outcome': outcome})
        elif rid is not None:  # a client method this client does not offer (fs, terminal)
            await self._reply(rid, error={'code': -32601, 'message': f'{method} is not supported'})
        elif method == 'session/update' and self._said is not None:
            update = (rec.get('params') or {}).get('update') or {}
            kind = update.get('sessionUpdate')
            if kind == 'agent_message_chunk':
                self._said += (update.get('content') or {}).get('text', '')
            elif kind == 'tool_call':
                self._said = ''  # the result is the agent's last message, after its last tool call

    async def _reply(self, rid, result=None, error=None):
        with contextlib.suppress(RpcError):
            await self._send({'jsonrpc': '2.0', 'id': rid, **({'error': error} if error else {'result': result})})

    # The PiRpc surface
    async def request(self, type, timeout=30, **fields):
        if type != 'abort':
            raise RpcError(f'{type} is not supported by copilot')
        await self._send({'jsonrpc': '2.0', 'method': 'session/cancel', 'params': {'sessionId': self.session}})
        return {}

    async def run(self, message, timeout):
        """Prompt and wait for its stop reason. The result holds the agent's last message."""
        if self.fresh and self.preamble:
            message, self.fresh = f'{self.preamble}\n\n---\n\n{message}', False
        result, self._said = RunResult(), ''
        fut = self._ask('session/prompt', {'sessionId': self.session, 'prompt': [{'type': 'text', 'text': message}]})
        try:
            try:
                r = await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                result.error, result.aborted = 'timeout', True
                try:
                    await self.request('abort')
                    await asyncio.wait_for(fut, 5)
                except (RpcError, asyncio.TimeoutError):
                    pass
                return result
            except RpcError as e:
                result.error = str(e)
                return result
            stop = r.get('stopReason')
            result.text, result.settled = self._said.strip(), True
            result.aborted = stop == 'cancelled'
            result.error = ERRORS.get(stop, '')
            return result
        finally:
            self._said = None

    async def steer(self, text):
        raise RpcError('copilot takes no message during a run')

    async def follow_up(self, text):
        raise RpcError('copilot takes no message during a run')


def model_choices(models):
    """[(model id, note)] of the enabled models in a session's availableModels; the note is the premium-request
    multiplier, as the Copilot CLI shows it (1x, 15x, ...)."""
    out = []
    for m in models:
        meta = m.get('_meta') or {}
        if not m.get('modelId') or meta.get('copilotEnablement', 'enabled') != 'enabled':
            continue
        usage = meta.get('copilotUsage')
        out.append((m['modelId'], f'{usage} premium requests' if usage else m.get('description') or ''))
    return out


async def _offered(argv, cwd, env):
    acp = CopilotAcp()
    try:
        await acp.start(argv, cwd, env)
        return acp.models
    finally:
        await acp.close()


def offered_models(argv, cwd, env):
    """model_choices of what Copilot offers this user, read from a new session: no prompt, so no premium request.
    [] when Copilot cannot be asked (not installed, not signed in)."""
    try:
        return model_choices(asyncio.run(_offered(argv, cwd, env)))
    except (RpcError, OSError) as e:
        log.warning('could not list the copilot models: %s', e)
        return []
