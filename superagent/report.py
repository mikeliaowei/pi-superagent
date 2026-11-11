"""How a worker reports back: the `superagent-report` CLI and the localhost server the service runs to receive it."""

import argparse
import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .model import REPORTS

log = logging.getLogger('superagent.report')

SUMMARY_MAX = 4000
DETAILS_MAX = 16000
BODY_MAX = 64 * 1024


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
    return {'status': status, 'summary': summary.strip(), 'question': question.strip(), 'details': details.strip()}, ''


def main(argv=None):
    p = argparse.ArgumentParser(prog='superagent-report', description='Report this Ticket\'s status to the service.')
    p.add_argument('--status', required=True, choices=REPORTS)
    p.add_argument('--summary', required=True, help='what happened, in at most about 100 words')
    p.add_argument('--question', default='', help='the one question for a human (required with --status waiting)')
    p.add_argument('--details-file', default='',
                   help='a markdown file describing the change for the pull request (with --status ready)')
    args = p.parse_args(argv)
    if args.status == 'waiting' and not args.question.strip():
        p.error('--question is required when --status is waiting')
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
                       'details': details}).encode()
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

    def __init__(self, lookup, on_report, port=0, host='127.0.0.1'):
        self.lookup, self.on_report = lookup, on_report
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
                if self.path != '/report':
                    return self.reply(404, {'ok': False, 'error': 'not found'})
                auth = self.headers.get('Authorization', '')
                token = auth[7:] if auth.startswith('Bearer ') else ''
                ticket_id = server.lookup(token) if token else None
                if not ticket_id:
                    return self.reply(401, {'ok': False, 'error': 'unknown ticket token'})
                length = (self.headers.get('Content-Length') or '0').strip()
                if not (length.isascii() and length.isdigit()):
                    return self.reply(400, {'ok': False, 'error': 'bad Content-Length'})
                size = int(length)
                if size > BODY_MAX:
                    return self.reply(413, {'ok': False, 'error': 'body too large'})
                try:
                    body = json.loads(self.rfile.read(size) or b'null')
                except ValueError:
                    return self.reply(400, {'ok': False, 'error': 'body is not JSON'})
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

            def log_message(self, fmt, *args):
                log.debug('report server: ' + fmt, *args)

        return Handler
