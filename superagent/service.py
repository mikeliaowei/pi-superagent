"""The service: polls GitHub into Triggers, runs one Pi worker per Ticket, and delivers replies and PRs.

Every side effect goes through the store's effect queue (reply, launch, deliver), so a crash between deciding and
acting replays the action instead of losing it. The philosophy follows dsh-superagent: confirm once, then stay
quiet until there is a question, a result, or an honest failure.

Operator actions from the CLI reach a running service through kv keys `operator:stop:<ticket>` and
`operator:retry:<ticket>`; the service applies them at the start of each poll round (or at the next start).
Local requests (`superagent ask` and `answer`) wait in the store's `local_requests` table and are applied in the
same place. A local Ticket's thread is `local:<request id>`; its replies go to the store's local outbox, not GitHub.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from . import executor, github, policy
from .model import Assignment, Ticket, Trigger, named_repos, new_id, new_part, short_name
from .pi_rpc import PiRpc, RunResult
from .report import NotRunning

log = logging.getLogger('superagent.service')

RUN_TIMEOUT = 2 * 3600  # one worker run, seconds
DELIVER_ATTEMPTS = 5
REPLY_WORDS = 100
TITLE_MAX = 80
LOCAL = 'local:'  # thread key prefix of local Tickets


def is_local(t):
    return t.thread_key.startswith(LOCAL)
RATE_PAUSE_START, RATE_PAUSE_MAX = timedelta(minutes=5), timedelta(hours=1)


def utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='seconds')


def cap_words(text, n=REPLY_WORDS):
    words = (text or '').split()
    return ' '.join(words) if len(words) <= n else ' '.join(words[:n]) + '…'


def _digest(*parts):
    return hashlib.sha1('\0'.join(parts).encode()).hexdigest()[:10]


class Service:
    def __init__(self, cfg, store, gh, *, spawn=PiRpc, run_git=subprocess.run, clock=utcnow, report_url='',
                 run_timeout=RUN_TIMEOUT):
        self.cfg, self.store, self.gh = cfg, store, gh
        self.spawn, self.run_git, self.clock = spawn, run_git, clock
        self.report_url, self.run_timeout = report_url, run_timeout
        self.workers = {}  # ticket id -> executor.Worker, only while its process is alive
        self.tasks = {}  # ticket id -> the asyncio.Task running that worker
        self.test_tasks = {}  # ticket id -> the asyncio.Task running its repo's test command
        self.test_runs = {}  # ticket id -> executor.TestRun, only while the command runs
        self.loop = None
        self.closing = False
        self.paused_until, self.pause = None, None

    # Helpers
    def now_iso(self):
        return _iso(self.clock())

    def _name(self, ticket_id):
        t = self.store.ticket(ticket_id)
        return t.name if t else ticket_id

    def _request(self, t):
        """The Trigger that opened the Ticket, as a dict (text, meta)."""
        return self.store.trigger(t.mandate.get('trigger_id'))

    def _set_current(self, t, a):
        t.assignments[-1] = asdict(a)

    def _gh(self, repo):
        """The GitHub client for `repo` (one token for every configured repo)."""
        return self.gh.for_repo(repo) if repo else self.gh

    def reply(self, t, msg_id, body):
        trig = self._request(t)
        review_root = (trig.meta.get('review_root') if trig else None)
        self.store.enqueue('reply', msg_id, {'ticket_id': t.id, 'repo': t.repo, 'number': t.number,
                                             'body': cap_words(body), 'review_root': review_root}, now=self.clock())

    def _new_assignment(self, t, mode, message=''):
        """`message`, when given, is what the relaunched worker is told (before any new thread messages)."""
        a = Assignment(id=new_id('as'))
        t.assignments.append(asdict(a))
        self.store.save_ticket(t)
        data = {'ticket_id': t.id, 'assignment_id': a.id, 'mode': mode}
        if message:
            data['message'] = message
        self.store.enqueue('launch', f'{t.id}:{a.id}', data, now=self.clock())
        return a

    # Report server hooks (called on its thread)
    def lookup(self, token):
        t = self.store.ticket_by_token(token)
        return t.id if t else None

    def on_report(self, ticket_id, report):
        if self.loop is None:
            raise RuntimeError('service is not running')
        t = self.store.ticket(ticket_id)
        if not t or t.state == 'ended' or not t.current or t.current.state != 'running':
            raise NotRunning('this Ticket has no running assignment; the report was not recorded')
        self.loop.call_soon_threadsafe(self.handle_report, ticket_id, report)

    # Polling
    async def poll_once(self):
        self.loop = asyncio.get_running_loop()
        await self.apply_operator_requests()
        await self.take_local_requests()
        now = self.clock()
        if self.paused_until and now < self.paused_until:
            return
        subscribed = {}
        for t in self.store.tickets(state='open'):
            if not is_local(t):  # a local Ticket has no GitHub thread to read
                subscribed.setdefault(t.repo, set()).add(t.number)
        try:
            triggers = await asyncio.to_thread(github.poll_repos, self.gh, self.store, self.cfg.repo_names,
                                               self.cfg.agent_login, subscribed, now, self.cfg.legacy)
        except github.RateLimited as e:
            self.pause = min(self.pause * 2, RATE_PAUSE_MAX) if self.pause else RATE_PAUSE_START
            self.paused_until = now + self.pause
            log.warning('GitHub rate limit hit; pausing polls until %s (%s)', _iso(self.paused_until), e)
            return
        except github.GitHubError as e:
            log.warning('poll failed: %s', e)
            return
        self.paused_until, self.pause = None, None
        self.store.put('last-poll', _iso(now))
        for trig in triggers:
            await self.take(trig)

    async def take(self, trig):
        live = [t.id for t in self.store.tickets(state='open', thread_key=trig.thread_key)]
        d = policy.gate(trig, self.cfg.agent_login, self.cfg.allowed, live)
        if d.kind in ('accept', 'stop') and self._edit_of_handled(trig):
            # Editing a comment re-reads it, but its authority was used once: never a second Ticket or stop.
            d = (policy.Decision('context', 'edit of a handled request', tuple(live)) if live
                 else policy.Decision('ignore', 'edit of a handled request'))
        if not self.store.add_trigger(trig, d.kind, d.reason):
            return
        log.info('trigger %s from %s on %s: %s (%s)', trig.id, trig.actor, trig.thread_key, d.kind, d.reason)
        if d.kind == 'accept':
            self._accept(trig)
        elif d.kind == 'stop':
            for tid in d.ticket_ids:
                if await self.end_ticket(tid, 'stopped'):
                    self.reply(self.store.ticket(tid), f'{tid}-stopped', f'Stopped, as {trig.actor} asked.')
        elif d.kind == 'context':
            for tid in d.ticket_ids:
                await self._context(tid, trig)

    def _edit_of_handled(self, trig):
        """True when `trig` is an edit of a comment that already opened a Ticket or stopped one."""
        if not trig.id.startswith(('github-comment|', 'github-review|')):
            return False
        prefix = trig.id.rsplit('|', 1)[0] + '|'  # github-comment|<id>|
        return any(d in ('accept', 'stop') for tid, d in self.store.trigger_decisions(prefix).items() if tid != trig.id)

    def _accept(self, trig):
        if trig.kind == 'issue':
            title = trig.text.split('\n', 1)[0]
        else:
            first = next((line for line in trig.text.splitlines() if line.strip()), '')
            title = re.sub(rf'@{re.escape(self.cfg.agent_login)}\b[,:]?', '', first, flags=re.I)
        title = ' '.join(title.split())[:TITLE_MAX].strip() or 'Request'
        repo = trig.thread_key.removeprefix('github:').rsplit('#', 1)[0]
        repos = [repo]
        if not self.cfg.legacy:  # a request may name other configured repos: one Ticket for all of them (S14)
            repos += [r for r in named_repos(trig.text, self.cfg.repo_names) if r.lower() != repo.lower()]
        self.open_ticket(trig, title, repos, int(trig.meta['number']))

    def open_ticket(self, trig, title, repos, number):
        """Open a Ticket for the accepted request `trig` (already stored) and queue its ack and first launch.

        `repos`: configured repo names, the thread's own first; more than one makes a cross-repo Ticket (S14).
        Replies go to the request thread (`repos[0]`, `number`). Returns the Ticket."""
        taken = {t.name for t in self.store.tickets()}  # unique across every repo
        a = Assignment(id=new_id('as'))
        t = Ticket(id=new_id('tk'), name=short_name(title, taken), title=title, thread_key=trig.thread_key,
                   repo=repos[0], number=number,
                   mandate={'actor': trig.actor, 'strength': 'explicit', 'trigger_id': trig.id, 'at': trig.at},
                   created_at=self.now_iso(), token=secrets.token_urlsafe(24), assignments=[asdict(a)],
                   last_human_at=trig.at, parts=[new_part(r) for r in repos] if len(repos) > 1 else [])
        self.store.save_ticket(t)
        log.info('[%s] new ticket for #%s from %s%s', t.name, t.number, trig.actor,
                 f' across {", ".join(repos)}' if t.parts else '')
        self._queue_start(t)
        return t

    def _queue_start(self, t):
        """Queue a new Ticket's ack and first launch. Both effects are keyed, so queuing them again never doubles."""
        a = t.current
        self.reply(t, f'{t.id}-ack', f"On it: I'll work on this as `{t.name}` and report back here.")
        self.store.enqueue('launch', f'{t.id}:{a.id}', {'ticket_id': t.id, 'assignment_id': a.id, 'mode': 'first'},
                           now=self.clock())

    async def _context(self, tid, trig):
        self.store.add_input(tid, trig.kind, trig.text, trig.id)
        t = self.store.ticket(tid)
        if trig.human:
            t.last_human_at = trig.at
            self.store.save_ticket(t)
        worker = self.workers.get(tid)
        if worker and worker.running:
            inputs = self.store.pending_inputs(tid)
            how = await worker.send(executor.followup_message(inputs))
            self.store.mark_delivered([i['seq'] for i in inputs])
            log.info('[%s] follow-up from %s %s', t.name, trig.actor, how)
            return
        a = t.current
        if t.state == 'open' and a and a.state == 'ended' and tid not in self.workers:
            self._new_assignment(t, 'resume')
            log.info('[%s] follow-up from %s resumes the session', t.name, trig.actor)

    # Local requests (S15)
    async def take_local_requests(self):
        """Apply each queued `superagent ask` and `answer` once; a failure leaves it queued for the next round."""
        for r in self.store.local_requests():
            try:
                if r['kind'] == 'ask':
                    self._take_ask(r)
                elif r['kind'] == 'answer':
                    await self._take_answer(r)
                else:
                    self.store.local_request_done(r['id'])
            except Exception:
                log.exception('local request %s failed', r['id'])

    def _take_ask(self, r):
        """Open the Ticket of a local request: the local operator's explicit mandate. Idempotent: a request whose
        Ticket exists (the service stopped before marking it done) opens no second Ticket, but gets its start queued
        again, since the stop may have come before the ack and launch were queued."""
        thread = f"{LOCAL}{r['id']}"
        existing = self.store.tickets(thread_key=thread)
        if existing:
            t = existing[0]
            if t.state == 'open' and t.current and t.current.state == 'pending':
                self._queue_start(t)
            self.store.local_request_done(r['id'], t.id)
            return
        trig = Trigger(id=f"local|{r['id']}", thread_key=thread, actor=r['actor'], human=True, addressed=True,
                       at=r['at'], text=r['text'], kind='local')
        repos = []
        for name in json.loads(r['repos'] or '[]'):
            try:
                repos.append(self.cfg.repo_config(name).name)
            except ValueError:
                pass  # the config changed since `ask`
        if not repos:
            self.store.add_trigger(trig, 'ignore', 'local request names no configured repo')
            self.store.local_request_done(r['id'])
            log.warning('local request %s names no configured repo: ignored', r['id'])
            return
        self.store.add_trigger(trig, 'accept', 'local request')
        first = next((line for line in r['text'].splitlines() if line.strip()), '')
        title = ' '.join(first.split())[:TITLE_MAX].strip() or 'Request'
        t = self.open_ticket(trig, title, repos, 0)
        self.store.local_request_done(r['id'], t.id)

    async def _take_answer(self, r):
        """Deliver an operator's answer like a human follow-up on the thread: steer or resume the worker."""
        t = self.store.ticket(r['ticket_id'])
        trig_id = f"local-answer|{r['id']}"
        if not t or t.state == 'ended':
            log.info('answer %s for %s dropped: the ticket ended', r['id'], r['ticket_id'])
        elif not any(i['trigger_id'] == trig_id for i in self.store.inputs(t.id)):
            trig = Trigger(id=trig_id, thread_key=t.thread_key, actor=r['actor'], human=True, addressed=True,
                           at=r['at'], text=r['text'], kind='answer')
            self.store.add_trigger(trig, 'context', 'local answer')
            await self._context(t.id, trig)
        self.store.local_request_done(r['id'])

    # Operator requests
    async def apply_operator_requests(self):
        for t in self.store.tickets(state='open'):
            if self.store.get(f'operator:stop:{t.id}'):
                self.store.put(f'operator:stop:{t.id}', None)
                if await self.end_ticket(t.id, 'stopped'):
                    self.reply(self.store.ticket(t.id), f'{t.id}-stopped', 'Stopped by the operator.')
                continue
            if self.store.get(f'operator:retry:{t.id}'):
                self.store.put(f'operator:retry:{t.id}', None)
                self.retry(t.id)

    def retry(self, tid):
        t = self.store.ticket(tid)
        a = t.current
        if t.delivery_error and a:
            t.delivery_error = ''
            self.store.save_ticket(t)
            self.store.enqueue('deliver', f'{t.id}:{a.id}:retry-{self.now_iso()}',
                               {'ticket_id': t.id, 'assignment_id': a.id}, now=self.clock())
            log.info('[%s] operator retry: delivering again', t.name)
        elif a is None or (a.state == 'ended' and tid not in self.workers):
            t.test_failures = 0  # the operator grants fresh fix attempts
            self._new_assignment(t, 'retry')
            log.info('[%s] operator retry: relaunching', t.name)
        else:
            log.info('[%s] operator retry ignored: the ticket is %s', t.name, a.state)

    # Ending
    async def end_ticket(self, tid, outcome):
        """End an open Ticket and stop its worker. Returns False when it had already ended."""
        t = self.store.ticket(tid)
        if not t or t.state == 'ended':
            return False
        t.state, t.outcome = 'ended', outcome
        a = t.current
        if a and a.state != 'ended':
            a.state, a.ended_at, a.end_reason = 'ended', self.now_iso(), outcome
            self._set_current(t, a)
        self.store.save_ticket(t)
        log.info('[%s] ended: %s', t.name, outcome)
        worker = self.workers.pop(tid, None)
        if worker:
            await worker.stop()
        return True

    # Effects
    async def run_effects(self):
        """Run every due effect once, including those the pass itself queues (a delivery's link reply)."""
        self.loop = asyncio.get_running_loop()
        seen = set()
        while due := [e for e in self.store.due_effects(now=self.clock()) if e['id'] not in seen]:
            for e in due:
                seen.add(e['id'])
                if e['kind'] == 'launch' and len(self.workers) >= self.cfg.max_workers:
                    continue  # stays queued: capacity is not a failure
                try:
                    await getattr(self, f'_effect_{e["kind"]}')(e)
                except Exception as ex:
                    self._effect_failed(e, ex)

    def _effect_failed(self, e, ex):
        d = e['data']
        if e['kind'] == 'deliver' and e['attempts'] + 1 >= DELIVER_ATTEMPTS:
            t = self.store.ticket(d['ticket_id'])
            t.delivery_error = str(ex)[:500]
            self.store.save_ticket(t)
            self.store.effect_done(e['id'])
            log.error('[%s] delivery gave up after %d attempts: %s', t.name, DELIVER_ATTEMPTS, ex)
            return
        delay = self.store.effect_failed(e['id'], ex, now=self.clock())
        log.warning('[%s] %s failed (retry in %ss): %s', self._name(d.get('ticket_id', '')), e['kind'], delay, ex)

    async def _effect_reply(self, e):
        d = e['data']
        t = self.store.ticket(d.get('ticket_id', ''))
        if t and is_local(t):
            self.store.add_local_message(t.id, 'reply', d['body'], e['key'])
            self.store.effect_done(e['id'])
            log.info('[%s] reply %s to the local outbox', t.name, e['key'])
            return
        repo = d.get('repo')
        if not repo:  # queued by a version before replies named their repo
            t = self.store.ticket(d.get('ticket_id', ''))
            repo = t.repo if t else None
        result = await asyncio.to_thread(github.post_comment, self._gh(repo), d['number'], d['body'], e['key'],
                                         d.get('review_root'))
        self.store.effect_done(e['id'])
        log.info('[%s] reply %s on #%s: %s', self._name(d.get('ticket_id', '')), e['key'], d['number'], result)

    async def _effect_launch(self, e):
        d = e['data']
        t = self.store.ticket(d['ticket_id'])
        a = t.current if t else None
        if not t or t.state == 'ended' or not a or a.id != d['assignment_id'] or a.state != 'pending':
            self.store.effect_done(e['id'])  # stale: the Ticket moved on
            return
        try:
            path, branch = await asyncio.to_thread(executor.prepare_worktree, self.cfg, t, self.run_git)
        except Exception as ex:  # no automatic retry: a broken checkout needs a person
            self.store.effect_done(e['id'])
            self._fail_assignment(t, a, f'could not prepare the worktree: {ex}', 'prepare failed')
            return
        t.worktree, t.branch = path, branch
        fresh = not any(x.get('started_at') for x in t.assignments[:-1])
        inputs = self.store.pending_inputs(t.id)
        message = self._message(t, d['mode'], fresh, inputs, d.get('message', ''))
        a.state, a.started_at = 'running', self.now_iso()
        self._set_current(t, a)
        self.store.save_ticket(t)
        worker = executor.Worker()
        self.workers[t.id] = worker
        try:
            await worker.start(self.cfg, t, self.report_url, spawn=self.spawn)
        except Exception as ex:
            self.workers.pop(t.id, None)
            self.store.effect_done(e['id'])
            await worker.stop()
            self._fail_assignment(self.store.ticket(t.id), a, f'could not start pi: {ex}', 'start failed')
            return
        self.store.mark_delivered([i['seq'] for i in inputs])
        self.store.effect_done(e['id'])
        self.tasks[t.id] = asyncio.create_task(self._run_worker(t.id, a.id, worker, message))
        log.info('[%s] worker launched (%s) in %s', t.name, d['mode'], path)

    def _message(self, t, mode, fresh, inputs, note=''):
        if fresh:
            trig = self._request(t)
            text = trig.text if trig else t.title
            if t.parts:
                repos = [{'repo': p['repo'], 'path': p['worktree'], 'base_branch': rc.base_branch,
                          'test_command': rc.test_command}
                         for p in t.parts for rc in [self.cfg.repo_config(p['repo'])]]
                return executor.first_message(t, text, inputs, repos=repos)
            return executor.first_message(t, text, inputs, self.cfg.repo_config(t.repo).test_command)
        followups = executor.followup_message(inputs) if inputs else ''
        if note:
            return note + ('\n\n' + followups if followups else '')
        if mode == 'restart':
            return executor.recovery_note() + ('\n\n' + followups if followups else '')
        if followups:
            return followups
        return ('The operator asked you to try this Ticket again. Check your directory and git log, '
                'then continue and report.')

    async def _run_worker(self, tid, aid, worker, message):
        try:
            result = await worker.run(message, self.run_timeout)
        except Exception as ex:
            result = RunResult(error=str(ex) or type(ex).__name__)
        finally:
            await worker.stop()
            if self.workers.get(tid) is worker:
                del self.workers[tid]
        if self.closing:
            return  # the assignment stays running; recover() relaunches it at the next start
        self._finish(tid, aid, result)

    def _finish(self, tid, aid, result):
        t = self.store.ticket(tid)
        if not t or t.state == 'ended' or not t.current or t.current.id != aid:
            return
        a = t.current
        if a.report in (None, 'progress'):
            reason = result.error or 'no report'
            self._fail_assignment(t, a, reason, reason)
            return
        a.state, a.ended_at, a.end_reason = 'ended', self.now_iso(), (result.error or 'settled')[:300]
        self._set_current(t, a)
        self.store.save_ticket(t)
        if not result.error:
            log.info('[%s] run settled after report %s', t.name, a.report)
            return
        log.warning('[%s] run ended with an error after report %s: %s', t.name, a.report, result.error[:200])
        if a.report == 'ready':
            # The worker reported committed, checked work, so its delivery stands; say the session did not end cleanly.
            self.reply(t, f'{t.id}-{a.id}-error-after-ready',
                       f'Note: my session ended with an error after I reported this ready: {result.error[:300]}')

    def _fail_assignment(self, t, a, summary, end_reason):
        a.state, a.report, a.ended_at, a.end_reason = 'ended', 'failed', self.now_iso(), end_reason[:300]
        a.summary = summary[:500]
        self._set_current(t, a)
        self.store.save_ticket(t)
        log.warning('[%s] assignment failed: %s', t.name, end_reason[:200])
        if end_reason == 'no report':
            body = 'The worker stopped without reporting a result, so I am not sure this is done.'
        else:
            body = f'I could not finish this: {summary[:300]}'
        self.reply(t, f'{t.id}-{a.id}-failed', body)

    # Reports
    def handle_report(self, ticket_id, report):
        t = self.store.ticket(ticket_id)
        if not t or t.state == 'ended' or not t.current or t.current.state != 'running':
            return
        a = t.current
        status = report['status']
        a.report, a.summary, a.question = status, report['summary'], report.get('question', '')
        self._set_current(t, a)
        self.store.save_ticket(t)
        log.info('[%s] report %s', t.name, status)
        msg_id = f'{t.id}-{a.id}-{status}-{_digest(a.summary, a.question)}'
        if status == 'waiting':
            self.reply(t, msg_id, f'Question: {a.question}')
        elif status == 'ready':
            if any(self.cfg.repo_config(r).test_command for r in t.repos):
                self.store.enqueue('test', f'{t.id}:{a.id}:test-{t.test_failures + 1}',
                                   {'ticket_id': t.id, 'assignment_id': a.id}, now=self.clock())
            else:
                self.store.enqueue('deliver', f'{t.id}:{a.id}', {'ticket_id': t.id, 'assignment_id': a.id},
                                   now=self.clock())
        elif status == 'blocked':
            self.reply(t, msg_id, f'I am blocked: {a.summary}')
        elif status == 'failed':
            self.reply(t, msg_id, f'I could not finish this: {a.summary}')
        elif status == 'dropped':
            self.reply(t, msg_id, f'I am not taking this on: {a.summary}')

    # Tests: the repo's test command runs after a `ready` report, before any pull request.
    async def _effect_test(self, e):
        d = e['data']
        t = self.store.ticket(d['ticket_id'])
        a = t.current if t else None
        if not t or t.state == 'ended' or not a or a.id != d['assignment_id']:
            self.store.effect_done(e['id'])  # stale: the Ticket moved on
            return
        running = self.test_tasks.get(t.id)
        if t.id in self.workers or (running and not running.done()):
            return  # stays queued until the worker's run has ended
        self.test_tasks[t.id] = asyncio.create_task(self._run_tests(e, t.id, a.id))

    def _test_targets(self, t):
        """[(RepoConfig, worktree)] to test: the Ticket's repo; for a cross-repo Ticket, each changed repo that has a
        test command (blocking: run it in a thread)."""
        if not t.parts:
            return [(self.cfg.repo_config(t.repo), t.worktree)]
        env = executor.git_env(self.cfg)
        targets = []
        for p in t.parts:
            repo = self.cfg.repo_config(p['repo'])
            if repo.test_command and github.has_new_commits(p['worktree'], f'origin/{repo.base_branch}',
                                                            self.run_git, env):
                targets.append((repo, p['worktree']))
        return targets

    async def _run_tests(self, e, tid, aid):
        t = self.store.ticket(tid)
        results = []  # [(RepoConfig, result)]
        try:
            for repo, cwd in await asyncio.to_thread(self._test_targets, t):
                run = self.test_runs[tid] = executor.TestRun(repo.test_command, cwd, executor.test_env(self.cfg),
                                                             repo.test_timeout)
                log.info('[%s] running tests: %s', t.name, repo.test_command)
                results.append((repo, await asyncio.to_thread(run.wait)))
                if self.closing:
                    break
        except Exception as ex:
            self._effect_failed(e, ex)
            return
        finally:
            self.test_runs.pop(tid, None)
        if self.closing:
            return  # the effect stays queued: the next start runs the tests again
        try:
            self._tests_done(e, tid, aid, results)
        except Exception as ex:
            self._effect_failed(e, ex)

    def _tests_done(self, e, tid, aid, results):
        t = self.store.ticket(tid)
        if not t or t.state == 'ended' or not t.current or t.current.id != aid:
            self.store.effect_done(e['id'])
            return
        a = t.current
        failed = [(repo, result) for repo, result in results if not result['passed']]
        if not failed:
            lines = {repo.name: f'Tests: `{repo.test_command}` passed in {result["seconds"]}s'
                     for repo, result in results}
            t.test_failures = 0
            if t.parts:
                for p in t.parts:
                    p['test_result'] = lines.get(p['repo'], '')
            else:
                t.test_result, = lines.values()
            self.store.save_ticket(t)
            self.store.enqueue('deliver', f'{t.id}:{a.id}', {'ticket_id': t.id, 'assignment_id': a.id},
                               now=self.clock())
            self.store.effect_done(e['id'])
            log.info('[%s] tests passed in %s', t.name, ', '.join(lines) or 'no changed repo with a test command')
            return

        def how(repo, result):
            return f'timed out after {repo.test_timeout}s' if result['timed_out'] else f'exit code {result["code"]}'

        t.test_failures += 1
        t.test_output = '\n\n'.join((f'{repo.name}:\n' if t.parts else '') + result['output']
                                     for repo, result in failed)[-executor.TEST_TAIL:]
        log.warning('[%s] tests failed (%s), failure %d', t.name,
                    ', '.join(f'{repo.name} {how(repo, result)}' for repo, result in failed), t.test_failures)
        fixes = self.cfg.max_test_retries
        if t.test_failures > fixes:
            if t.parts:
                what = '; '.join(f'{repo.name}: `{repo.test_command}` ({how(repo, result)})' for repo, result in failed)
                summary = 'tests still failing in ' + ', '.join(repo.name for repo, _ in failed)
            else:
                (repo, result), = failed
                what = f'`{repo.test_command}` ({how(repo, result)})'
                summary = f'tests still failing: {repo.test_command}'
            a.report, a.summary, a.question = 'blocked', summary[:500], ''
            self._set_current(t, a)
            self.store.save_ticket(t)
            self.reply(t, f'{t.id}-{a.id}-tests-blocked',
                       f'I am blocked: tests still failing: {what} after {fixes} '
                       f'fix attempt{"" if fixes == 1 else "s"}, so I opened no pull request.')
        else:
            self.store.save_ticket(t)
            if t.parts:
                message = executor.cross_test_failure_message(
                    [(repo.name, repo.test_command, result) for repo, result in failed],
                    {repo.name: repo.test_timeout for repo, _ in failed}, t.test_failures, fixes)
            else:
                (repo, result), = failed
                message = executor.test_failure_message(repo.test_command, result, repo.test_timeout,
                                                        t.test_failures, fixes)
            self._new_assignment(t, 'tests', message)
        self.store.effect_done(e['id'])

    async def _effect_deliver(self, e):
        d = e['data']
        t = self.store.ticket(d['ticket_id'])
        if not t or t.state == 'ended':
            self.store.effect_done(e['id'])
            return
        a = next((Assignment(**x) for x in t.assignments if x['id'] == d['assignment_id']), t.current)
        if t.parts:
            await self._deliver_parts(e, t, a)
            return
        repo = self.cfg.repo_config(t.repo)
        base = f'origin/{repo.base_branch}'
        env = executor.git_env(self.cfg)
        if not await asyncio.to_thread(github.has_new_commits, t.worktree, base, self.run_git, env):
            self.store.effect_done(e['id'])
            self.reply(t, f'{t.id}-{a.id}-nochange', 'I finished, but there were no changes to propose.')
            log.info('[%s] ready without commits: no PR', t.name)
            return
        push_dir = self.cfg.push_dir(repo)
        os.makedirs(os.path.dirname(push_dir), exist_ok=True)
        await asyncio.to_thread(github.push_branch, t.worktree, t.branch, self.cfg.token, repo.name, push_dir,
                                self.run_git, env)
        number = await asyncio.to_thread(github.open_pull, self._gh(repo.name), t.branch, repo.base_branch, t.title,
                                         self._pr_body(t, a, repo, t.test_result))
        t = self.store.ticket(t.id)
        t.pr_number = number
        self.store.save_ticket(t)
        self.store.effect_done(e['id'])
        self.reply(t, f'{t.id}-pr-{number}', f'Opened a pull request: https://github.com/{repo.name}/pull/{number}')
        log.info('[%s] delivered PR #%s', t.name, number)

    def _pr_body(self, t, a, repo, test_result):
        trig = self._request(t)
        if not repo.test_command:
            tests = f'Tests: no test command configured for {repo.name}.'
        else:
            tests = test_result or f'Tests: `{repo.test_command}` was not run for this delivery.'
        body = f'{a.summary or t.title}\n\n{tests}'
        if is_local(t):
            body += f"\n\nRequested locally by {t.mandate.get('actor') or 'the operator'}."
        elif repo.name.lower() != t.repo.lower():
            body += f'\n\nRequested in https://github.com/{t.repo}/issues/{t.number}'
        elif not (trig and trig.meta.get('is_pr')):
            body += f'\n\nCloses #{t.number}'
        return body

    async def _deliver_parts(self, e, t, a):
        """Deliver a cross-repo Ticket: push and open one PR per changed repo, cross-link them, reply once.

        Tests already passed for every changed repo. Each part records the assignment it was pushed for, so a
        retried delivery pushes nothing twice; open_pull reuses the open PR of the branch."""
        env = executor.git_env(self.cfg)
        changed = []
        for p in t.parts:
            repo = self.cfg.repo_config(p['repo'])
            if await asyncio.to_thread(github.has_new_commits, p['worktree'], f'origin/{repo.base_branch}',
                                       self.run_git, env):
                changed.append(p)
        if not changed:
            self.store.effect_done(e['id'])
            self.reply(t, f'{t.id}-{a.id}-nochange', 'I finished, but there were no changes to propose.')
            log.info('[%s] ready without commits in any repo: no PR', t.name)
            return
        numbers = {}  # repo -> PR number
        for p in changed:
            repo = self.cfg.repo_config(p['repo'])
            if p.get('pushed') != a.id:
                push_dir = self.cfg.push_dir(repo)
                os.makedirs(os.path.dirname(push_dir), exist_ok=True)
                await asyncio.to_thread(github.push_branch, p['worktree'], t.branch, self.cfg.token, repo.name,
                                        push_dir, self.run_git, env)
                self._update_part(t.id, repo.name, pushed=a.id)
            numbers[repo.name] = await asyncio.to_thread(github.open_pull, self._gh(repo.name), t.branch,
                                                         repo.base_branch, t.title,
                                                         self._pr_body(t, a, repo, p['test_result']))
        urls = {r: f'https://github.com/{r}/pull/{n}' for r, n in numbers.items()}
        if len(urls) > 1:
            for p in changed:
                repo = self.cfg.repo_config(p['repo'])
                links = '\n'.join(f'- {u}' for r, u in urls.items() if r != repo.name)
                body = f'{self._pr_body(t, a, repo, p["test_result"])}\n\nPart of a cross-repo change:\n{links}'
                await asyncio.to_thread(github.update_pull_body, self._gh(repo.name), numbers[repo.name], body)
        for r, n in numbers.items():
            t = self._update_part(t.id, r, pr_number=n)
        self.store.effect_done(e['id'])
        lines = [f'- {p["repo"]}: {urls[p["repo"]]}' if p['repo'] in urls else f'- {p["repo"]}: no changes'
                 for p in t.parts]
        self.reply(t, f'{t.id}-prs-{_digest(*sorted(urls.values()))}',
                   'Opened pull requests:\n' + '\n'.join(lines))
        log.info('[%s] delivered %s', t.name, ', '.join(f'{r}#{n}' for r, n in numbers.items()))

    def _update_part(self, tid, repo, **fields):
        """Set fields of one part on the stored Ticket (re-read: other handlers may have saved it meanwhile)."""
        t = self.store.ticket(tid)
        next(p for p in t.parts if p['repo'] == repo).update(fields)
        self.store.save_ticket(t)
        return t

    async def _cross_state(self, t):
        """'merged' or 'closed' once no PR of a cross-repo Ticket is open, else None (also when one cannot be read)."""
        states = []
        for p in t.parts:
            if p['pr_number']:
                try:
                    states.append(await asyncio.to_thread(github.pull_state, self._gh(p['repo']), p['pr_number']))
                except github.RateLimited:
                    raise
                except github.GitHubError as e:
                    log.warning('[%s] cannot read %s PR #%s: %s', t.name, p['repo'], p['pr_number'], e)
                    return None
        if not states or 'open' in states:
            return None
        return 'merged' if all(s == 'merged' for s in states) else 'closed'

    # Pull requests
    async def check_pulls(self):
        for t in self.store.tickets(state='open'):
            if t.parts:
                try:
                    state = await self._cross_state(t)
                except github.RateLimited:
                    return
                if state:
                    await self.end_ticket(t.id, state)
                continue
            if not t.pr_number:
                continue
            try:
                state = await asyncio.to_thread(github.pull_state, self._gh(t.repo), t.pr_number)
            except github.RateLimited:
                return
            except github.GitHubError as e:
                log.warning('[%s] cannot read PR #%s: %s', t.name, t.pr_number, e)
                continue
            if state in ('merged', 'closed'):
                await self.end_ticket(t.id, state)

    # Durability
    def recover(self):
        """After a restart: running assignments end as 'host restarted'; unfinished ones relaunch with a note."""
        for t in self.store.tickets(state='open'):
            a = t.current
            if not a or a.state != 'running':
                continue
            a.state, a.ended_at, a.end_reason = 'ended', self.now_iso(), 'host restarted'
            self._set_current(t, a)
            self.store.save_ticket(t)
            if a.report in (None, 'progress'):
                self._new_assignment(t, 'restart')
                log.info('[%s] host restarted: relaunching with a recovery note', t.name)
            else:
                log.info('[%s] host restarted after report %s', t.name, a.report)

    # Main loop
    async def run_forever(self, stop_event, tick=1.0):
        self.loop = asyncio.get_running_loop()
        self.recover()
        next_poll = 0.0
        try:
            while not stop_event.is_set():
                if self.loop.time() >= next_poll:
                    next_poll = self.loop.time() + self.cfg.poll_seconds
                    for step in (self.poll_once, self.check_pulls):
                        try:
                            await step()
                        except Exception:
                            log.exception('%s failed', step.__name__)
                try:
                    await self.run_effects()
                except Exception:
                    log.exception('run_effects failed')
                try:
                    await asyncio.wait_for(stop_event.wait(), tick)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.close()

    async def close(self):
        """Stop every worker. Their assignments stay running, so the next start recovers them."""
        self.closing = True
        workers, self.workers = list(self.workers.values()), {}
        for w in workers:
            await w.stop()
        for run in list(self.test_runs.values()):
            run.kill()
        tasks = [t for t in [*self.tasks.values(), *self.test_tasks.values()] if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=10)
