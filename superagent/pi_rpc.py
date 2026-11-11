"""An asyncio client for `pi --mode rpc`: JSONL commands on stdin, responses and events on stdout.

See Pi's docs/rpc.md. A run is finished only at `agent_settled`; `agent_end` can be followed by retries,
compaction or queued follow-ups.
"""

import asyncio
import itertools
import json
import logging
from collections import deque
from dataclasses import dataclass

log = logging.getLogger('superagent.pi')

STREAM_LIMIT = 16 * 1024 * 1024  # one record can carry a whole file read by a tool
DIALOGS = ('select', 'confirm', 'input', 'editor')
_EXIT = 'superagent_exit'  # synthetic event a waiting run receives when the process dies


class RpcError(Exception):
    pass


@dataclass
class RunResult:
    text: str = ''
    error: str = ''
    aborted: bool = False
    settled: bool = False


def message_text(message):
    content = message.get('content') or []
    if isinstance(content, str):
        return content
    return ''.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')


class PiRpc:
    NAME = 'pi'  # in error messages and logs

    def __init__(self):
        self.proc = None
        self.exit_error = ''
        self.cancelled_dialogs = []
        self._ids = itertools.count(1)
        self._pending = {}
        self._subs = set()
        self._stderr = deque(maxlen=40)
        self._tasks = []

    async def start(self, argv, cwd, env):
        self.proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, limit=STREAM_LIMIT,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        err = asyncio.create_task(self._read_stderr())
        self._tasks = [err, asyncio.create_task(self._read_stdout(err))]

    # Reading
    async def _read_stderr(self):
        while line := await self.proc.stderr.readline():
            text = line.decode(errors='replace').rstrip()
            self._stderr.append(text)
            log.debug('%s stderr: %s', self.NAME, text)

    async def _read_stdout(self, stderr_task):
        # StreamReader.readline splits on b'\n' only, as the protocol requires (never on U+2028).
        while line := await self.proc.stdout.readline():
            line = line.rstrip(b'\r\n')
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                log.warning('%s wrote a non-JSON line: %r', self.NAME, line[:200])
                continue
            await self._route(rec)
        code = await self.proc.wait()
        try:
            await asyncio.wait_for(stderr_task, 2)
        except asyncio.TimeoutError:
            pass
        tail = '\n'.join(self._stderr)[-800:]
        self.exit_error = f'{self.NAME} exited (code {code}): {tail}'.rstrip(': ')
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RpcError(self.exit_error))
        self._pending.clear()
        for q in self._subs:
            q.put_nowait({'type': _EXIT, 'error': self.exit_error})

    async def _route(self, rec):
        kind = rec.get('type')
        if kind == 'response' and rec.get('id') in self._pending:
            fut = self._pending.pop(rec['id'])
            if not fut.done():
                fut.set_result(rec)
        elif kind == 'extension_ui_request':
            # Nobody is at a terminal: a dialog left unanswered would block the worker forever.
            if rec.get('method') in DIALOGS:
                log.info('cancelling pi dialog %s: %s', rec.get('method'), rec.get('title', ''))
                self.cancelled_dialogs.append(rec)
                try:
                    await self._send({'type': 'extension_ui_response', 'id': rec.get('id'), 'cancelled': True})
                except RpcError:
                    pass
        else:
            for q in self._subs:
                q.put_nowait(rec)

    # Writing
    async def _send(self, obj):
        if self.exit_error or self.proc is None or self.proc.stdin.is_closing():
            raise RpcError(self.exit_error or f'{self.NAME} is not running')
        try:
            self.proc.stdin.write(json.dumps(obj).encode() + b'\n')
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            raise RpcError(self.exit_error or f'{self.NAME} stdin closed: {e}') from e

    async def request(self, type, timeout=30, **fields):
        rid = f'sa-{next(self._ids)}'
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({'id': rid, 'type': type, **fields})
            rec = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RpcError(f'{type}: no response in {timeout}s') from None
        finally:
            self._pending.pop(rid, None)
        if not rec.get('success'):
            raise RpcError(rec.get('error') or f'{type} failed')
        return rec.get('data') or {}

    # Runs
    async def run(self, message, timeout):
        """Prompt and wait for agent_settled. The result holds the last assistant message's text."""
        q = asyncio.Queue()
        self._subs.add(q)  # before the prompt, so a fast run cannot finish unseen
        result = RunResult()
        try:
            try:
                data = await self.request('prompt', message=message)
            except RpcError as e:
                return RunResult(error=str(e))
            if data.get('disposition') == 'handled':
                return result
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                left = deadline - asyncio.get_running_loop().time()
                try:
                    ev = await asyncio.wait_for(q.get(), max(left, 0))
                except asyncio.TimeoutError:
                    return await self._abort_run(q, result)
                if self._absorb(ev, result):
                    return result
        finally:
            self._subs.discard(q)

    def _absorb(self, ev, result):
        """Fold one event into the result; True when the run is over."""
        kind = ev.get('type')
        if kind == 'message_end' and (ev.get('message') or {}).get('role') == 'assistant':
            msg = ev['message']
            result.text = message_text(msg)
            # A retried turn that succeeds replaces an earlier error: only the last message counts.
            result.error = (msg.get('errorMessage') or 'provider error') if msg.get('stopReason') == 'error' else ''
            result.aborted = msg.get('stopReason') == 'aborted'
        elif kind == 'agent_settled':
            result.settled = True
            return True
        elif kind == _EXIT:
            result.error = ev['error']
            return True
        return False

    async def _abort_run(self, q, result, grace=5):
        result.error, result.aborted = 'timeout', True
        try:
            await self.request('abort', timeout=grace)
            deadline = asyncio.get_running_loop().time() + grace
            while not result.settled:
                ev = await asyncio.wait_for(q.get(), max(deadline - asyncio.get_running_loop().time(), 0))
                if self._absorb(ev, result) and not result.settled:
                    break
        except (RpcError, asyncio.TimeoutError):
            pass
        result.error, result.aborted = 'timeout', True
        return result

    async def steer(self, text):
        return await self.request('steer', message=text)

    async def follow_up(self, text):
        return await self.request('follow_up', message=text)

    async def close(self, grace=5):
        if self.proc is None:
            return
        if self.proc.returncode is None:
            if not self.proc.stdin.is_closing():
                self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), grace)
            except asyncio.TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        for t in self._tasks:
            try:
                await asyncio.wait_for(t, grace)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                t.cancel()
