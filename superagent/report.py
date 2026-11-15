"""How a worker reports back: the `superagent-report` CLI and the localhost server the service runs to receive it."""

import argparse
import json
import logging
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .model import REPORTS

log = logging.getLogger('superagent.report')

SUMMARY_MAX = 4000
DETAILS_MAX = 16000
BODY_MAX = 512 * 1024  # a proposed Confluence page can be long
REPO_NAME = re.compile(r'[\w.-]+/[\w.-]+')
# What a worker may propose to write to Jira or Confluence; the service writes it only after a person approves.
PROPOSALS = ('jira-issue', 'jira-comment', 'confluence-page', 'confluence-update')
UPDATE_MODES = ('append', 'replace')  # confluence-update: add to the end of the page, or replace its whole text
PROPOSAL_MAX = 400_000  # characters of Markdown
JIRA_PROJECT = re.compile(r'[A-Z][A-Z0-9]{1,9}')
JIRA_ISSUE = re.compile(r'[A-Z][A-Z0-9]{1,9}-[1-9]\d{0,8}')
WIKI_SPACE = re.compile(r'~?[A-Za-z0-9_.-]{1,64}')


class NotRunning(Exception):
    """`on_report` raises this when the Ticket's current Assignment is not running: the report is refused (409)."""


def validate(body):
    """Return (report, '') or (None, reason)."""
    if not isinstance(body, dict):
        return None, 'body must be a JSON object'
    status, summary, question = body.get('status'), body.get('summary'), body.get('question') or ''
    details = body.get('details') or ''
    if status not in REPORTS:
        return None, f'status must be one of {", ".join(REPORTS)}'
    if not isinstance(summary, str) or not summary.strip():
        return None, 'summary must be a non-empty string'
    if len(summary) > SUMMARY_MAX:
        return None, f'summary is longer than {SUMMARY_MAX} characters'
    if not isinstance(question, str):
        return None, 'question must be a string'
    if status == 'waiting' and not question.strip():
        return None, 'waiting needs a question'
    if not isinstance(details, str):
        return None, 'details must be a string'
    if len(details) > DETAILS_MAX:
        return None, f'details is longer than {DETAILS_MAX} characters'
    needs_repo = body.get('needs_repo') or ''
    if not isinstance(needs_repo, str) or (needs_repo and not REPO_NAME.fullmatch(needs_repo.strip())):
        return None, 'needs_repo must be a repository as owner/name'
    if needs_repo and status != 'waiting':
        return None, 'needs_repo goes with status waiting: a person approves adding the repo'
    report = {'status': status, 'summary': summary.strip(), 'question': question.strip(), 'details': details.strip()}
    return ({**report, 'needs_repo': needs_repo.strip()} if needs_repo else report), ''


def validate_proposal(body):
    """Return (proposal, '') or (None, reason). A proposal: {kind, body (Markdown), and per kind: jira-issue:
    project, summary, issuetype; jira-comment: issue; confluence-page: space, title, parent ('' or a page id);
    confluence-update: page (its id), mode (append or replace), title ('' keeps the page's)}."""
    if not isinstance(body, dict):
        return None, 'body must be a JSON object'
    kind, text = body.get('kind'), body.get('body')
    if kind not in PROPOSALS:
        return None, f'kind must be one of {", ".join(PROPOSALS)}'
    if not isinstance(text, str) or not text.strip():
        return None, 'body must be non-empty Markdown'
    if len(text) > PROPOSAL_MAX:
        return None, f'body is longer than {PROPOSAL_MAX} characters'
    fields = {k: body.get(k) if isinstance(body.get(k), str) else '' for k in
              ('project', 'summary', 'issuetype', 'issue', 'space', 'title', 'parent', 'page', 'mode')}
    fields = {k: v.strip() for k, v in fields.items()}
    if kind == 'jira-issue':
        if not JIRA_PROJECT.fullmatch(fields['project']):
            return None, 'project must be a Jira project key, e.g. RWT'
        if not fields['summary'] or len(fields['summary']) > 255:
            return None, 'summary must be 1-255 characters'
        keep = {'project': fields['project'], 'summary': fields['summary'],
                'issuetype': fields['issuetype'][:60] or 'Task'}
    elif kind == 'jira-comment':
        if not JIRA_ISSUE.fullmatch(fields['issue']):
            return None, 'issue must be a Jira issue key, e.g. RWT-123'
        keep = {'issue': fields['issue']}
    elif kind == 'confluence-update':
        if not fields['page'].isdigit():
            return None, 'page must be the id of the Confluence page to update (digits, as in ?pageId= or /pages/<id>)'
        mode = fields['mode'] or 'append'
        if mode not in UPDATE_MODES:
            return None, f'mode must be one of {", ".join(UPDATE_MODES)}'
        if len(fields['title']) > 255:
            return None, 'title must be at most 255 characters'
        keep = {'page': fields['page'], 'mode': mode, 'title': fields['title']}
    else:
        if not WIKI_SPACE.fullmatch(fields['space']):
            return None, 'space must be a Confluence space key'
        if not fields['title'] or len(fields['title']) > 255:
            return None, 'title must be 1-255 characters'
        if fields['parent'] and not fields['parent'].isdigit():
            return None, 'parent must be a Confluence page id (digits)'
        keep = {'space': fields['space'], 'title': fields['title'], 'parent': fields['parent']}
    return {'kind': kind, **keep, 'body': text.strip()}, ''


def _post(path, payload, prog):
    """POST `payload` to the service as this worker: (0, the JSON reply) or (exit code, None) after saying why."""
    url, token = os.environ.get('SUPERAGENT_URL'), os.environ.get('SUPERAGENT_TICKET_TOKEN')
    if not url or not token:
        print(f'{prog}: SUPERAGENT_URL and SUPERAGENT_TICKET_TOKEN must be set '
              '(this command only works inside a superagent worker)', file=sys.stderr)
        return 2, None
    req = urllib.request.Request(url.rstrip('/') + path, data=json.dumps(payload).encode(), method='POST',  # noqa: S310 - the service's localhost URL
                                 headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - likewise
            return 0, json.load(r)
    except urllib.error.HTTPError as e:
        print(f'{prog}: rejected ({e.code}): {e.read().decode(errors="replace")[:300]}', file=sys.stderr)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f'{prog}: cannot reach the service: {e}', file=sys.stderr)
    return 1, None


def propose_main(argv=None):
    """`superagent-propose`: ask a person to approve a Jira or Confluence write.

    The worker never writes there itself: after a person approves, the service does, and tells the worker the
    result in its next message."""
    p = argparse.ArgumentParser(prog='superagent-propose', description=propose_main.__doc__.split('\n')[0])
    sub = p.add_subparsers(dest='kind', required=True)
    issue = sub.add_parser('jira-issue', help='a new Jira issue')
    issue.add_argument('--project', required=True, help='Jira project key, e.g. RWT')
    issue.add_argument('--summary', required=True)
    issue.add_argument('--type', dest='issuetype', default='Task', help='issue type (default Task)')
    comment = sub.add_parser('jira-comment', help='a comment on a Jira issue')
    comment.add_argument('--issue', required=True, help='issue key, e.g. RWT-123')
    page = sub.add_parser('confluence-page', help='a new Confluence page')
    page.add_argument('--space', required=True, help='Confluence space key')
    page.add_argument('--title', required=True)
    page.add_argument('--parent', default='', help='id of the page to put it under')
    update = sub.add_parser('confluence-update', help='an update to an existing Confluence page')
    update.add_argument('--page', required=True, help='id of the page (as in ?pageId=<id> or /pages/<id>/)')
    update.add_argument('--mode', choices=UPDATE_MODES, default='append',
                        help='append (default): add the Markdown to the end of the page, keeping what is there; '
                             'replace: the Markdown becomes the whole page')
    update.add_argument('--title', default='', help='a new title (default: keep the page title)')
    for s in (issue, comment, page, update):
        s.add_argument('--body-file', required=True, help='Markdown: the description, comment or page text')
    args = p.parse_args(argv)
    try:
        with open(args.body_file, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        p.error(f'cannot read --body-file: {e}')
    payload = {k: v for k, v in vars(args).items() if k != 'body_file'}
    proposal, error = validate_proposal({**payload, 'body': text})
    if error:
        p.error(error)
    code, reply = _post('/propose', proposal, 'superagent-propose')
    if code == 0:
        print(f"proposed #{reply['proposal']}: a person approves it before the service writes it")
    return code


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['propose']:  # superagent-report propose ...: the same as superagent-propose ...
        return propose_main(argv[1:])
    p = argparse.ArgumentParser(prog='superagent-report', description='Report this Ticket\'s status to the service.')
    p.add_argument('--status', required=True, choices=REPORTS)
    p.add_argument('--summary', required=True, help='what happened, in at most about 100 words')
    p.add_argument('--question', default='', help='the one question for a human (required with --status waiting)')
    p.add_argument('--details-file', default='',
                   help='a markdown file describing the change for the pull request (with --status ready)')
    p.add_argument('--needs-repo', default='', metavar='OWNER/NAME',
                   help='another configured repository this Ticket needs a change in (with --status waiting): a '
                        'person approves, then the service adds its worktree and resumes you')
    args = p.parse_args(argv)
    if args.status == 'waiting' and not args.question.strip():
        p.error('--question is required when --status is waiting')
    if args.needs_repo and args.status != 'waiting':
        p.error('--needs-repo goes with --status waiting')
    details = ''
    if args.details_file:
        try:
            with open(args.details_file, encoding='utf-8') as f:
                details = f.read()
        except OSError as e:
            p.error(f'cannot read --details-file: {e}')
    url, token = os.environ.get('SUPERAGENT_URL'), os.environ.get('SUPERAGENT_TICKET_TOKEN')
    if not url or not token:
        print('superagent-report: SUPERAGENT_URL and SUPERAGENT_TICKET_TOKEN must be set '
              '(this command only works inside a superagent worker)', file=sys.stderr)
        return 2
    body = json.dumps({'status': args.status, 'summary': args.summary, 'question': args.question,
                       'details': details, 'needs_repo': args.needs_repo}).encode()
    req = urllib.request.Request(url.rstrip('/') + '/report', data=body, method='POST',
                                 headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        print(f'superagent-report: rejected ({e.code}): {e.read().decode(errors="replace")[:300]}', file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f'superagent-report: cannot reach the service: {e}', file=sys.stderr)
        return 1
    print(f'reported {args.status}')
    return 0


class ReportServer:
    """POST /report on 127.0.0.1. `lookup(token) -> ticket_id | None`; `on_report(ticket_id, report)` runs on a
    server thread, so it must be thread-safe (e.g. hand off with loop.call_soon_threadsafe). It raises NotRunning
    to refuse a report with 409 and change nothing."""

    def __init__(self, lookup, on_report, port=0, host='127.0.0.1', on_propose=None):
        """`on_propose(ticket_id, proposal) -> id`, like on_report: POST /propose (404 without it)."""
        self.lookup, self.on_report, self.on_propose = lookup, on_report, on_propose
        self.httpd = ThreadingHTTPServer((host, port), self._handler())
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name='superagent-report', daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.httpd.server_address[:2]
        return f'http://{host}:{port}'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.path not in ('/report', '/propose') or (self.path == '/propose' and not server.on_propose):
                    return self.reply(404, {'ok': False, 'error': 'not found'})
                length = (self.headers.get('Content-Length') or '0').strip()
                if not (length.isascii() and length.isdigit()):
                    return self.reply(400, {'ok': False, 'error': 'bad Content-Length'})
                size = int(length)
                if size > BODY_MAX:
                    return self.reply(413, {'ok': False, 'error': 'body too large'})
                # Read before any refusal: closing with the body unread resets the connection on Windows, and the
                # worker would see "connection aborted" instead of why it was refused.
                raw = self.rfile.read(size)
                auth = self.headers.get('Authorization', '')
                token = auth[7:] if auth.startswith('Bearer ') else ''
                ticket_id = server.lookup(token) if token else None
                if not ticket_id:
                    return self.reply(401, {'ok': False, 'error': 'unknown ticket token'})
                try:
                    body = json.loads(raw or b'null')
                except ValueError:
                    return self.reply(400, {'ok': False, 'error': 'body is not JSON'})
                if self.path == '/propose':
                    return self.propose(ticket_id, body)
                report, error = validate(body)
                if error:
                    return self.reply(400, {'ok': False, 'error': error})
                try:
                    server.on_report(ticket_id, report)
                except NotRunning as e:
                    return self.reply(409, {'ok': False, 'error': str(e)})
                except Exception as e:  # the worker should retry, not lose the report
                    log.exception('on_report failed for %s', ticket_id)
                    return self.reply(500, {'ok': False, 'error': str(e)})
                self.reply(200, {'ok': True, 'ticket': ticket_id, 'status': report['status']})

            def propose(self, ticket_id, body):
                proposal, error = validate_proposal(body)
                if error:
                    return self.reply(400, {'ok': False, 'error': error})
                try:
                    pid = server.on_propose(ticket_id, proposal)
                except NotRunning as e:
                    return self.reply(409, {'ok': False, 'error': str(e)})
                except ValueError as e:  # e.g. too many proposals waiting
                    return self.reply(429, {'ok': False, 'error': str(e)})
                self.reply(200, {'ok': True, 'ticket': ticket_id, 'proposal': pid})

            def log_message(self, fmt, *args):
                log.debug('report server: ' + fmt, *args)

        return Handler
