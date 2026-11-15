"""Jira issues and Confluence pages a request names, fetched read-only by the service for its worker.

The service holds the tokens; a worker's environment never does (Config.worker_env). The service saves each issue or
page as a Markdown file with the request's other attachments, and the worker reads it from there. Jira and Confluence
Server / Data Center take a personal access token as a Bearer token. Every call is a GET.
"""

import html
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

log = logging.getLogger('superagent.atlassian')

MAX_REFS = 5  # issues and pages fetched per message
COMMENTS = 5  # the latest comments of an issue
TEXT_CAP = 60_000  # characters kept of one issue or page
JIRA_KEY = re.compile(r'(?<![\w/.-])([A-Z][A-Z0-9]{1,9}-[1-9]\d{0,6})(?![\w-])')
# Bare names that look like Jira keys but are standards (UTF-8, ISO-8601, SHA-256, ...): not worth a request.
NOT_ISSUES = {'UTF', 'ISO', 'SHA', 'RFC', 'PEP', 'TLS', 'SSL', 'HTTP', 'ES', 'AES', 'RSA', 'MD', 'IEC', 'IEEE', 'CVE'}
urlopen = urllib.request.urlopen  # tests replace it


def references(cfg, text):
    """The Jira issues and Confluence pages `text` names, at most MAX_REFS: ('jira', 'KEY-1'), ('confluence', page
    id) or ('confluence-title', (space, title)). Jira: its keys, bare or in a /browse/ link; Confluence: links to
    pages on confluence_url. Nothing for a service without those URLs."""
    refs = []
    if cfg.jira_url:
        base = re.escape(cfg.jira_url.rstrip('/'))
        refs += [('jira', k) for k in re.findall(base + r'/browse/([A-Z][A-Z0-9]{1,9}-\d+)', text)]
        refs += [('jira', k) for k in JIRA_KEY.findall(text) if k.split('-')[0] not in NOT_ISSUES]
    if cfg.confluence_url:
        for m in re.finditer(re.escape(cfg.confluence_url.rstrip('/')) + r'(/[^\s)>\]"\'`]*)', text):
            path = m.group(1)
            if found := re.search(r'[?&]pageId=(\d+)', path) or re.search(r'/pages/(\d+)', path):
                refs.append(('confluence', found.group(1)))
            elif found := re.match(r'/display/([^/?#]+)/([^?#]+)', path):
                refs.append(('confluence-title', (urllib.parse.unquote(found.group(1)),
                                                  urllib.parse.unquote_plus(found.group(2)))))
    return list(dict.fromkeys(refs))[:MAX_REFS]


def saved_token(name):
    """The value saved for the Windows user (HKCU\\Environment). A window, and every program started from it, keeps
    the values it started with, so after a token is replaced only this has the new one. '' elsewhere or when unset."""
    if os.name != 'nt':
        return ''
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value = winreg.QueryValueEx(key, name)[0]
    except OSError:
        return ''
    return value if isinstance(value, str) else ''


def tokens(name, env=None):
    """The tokens to try in turn: this process's, then (on Windows, without a given `env`) the one saved for the user
    when it differs: a token replaced after this window or `superagent run` started."""
    here = (os.environ if env is None else env).get(name, '')
    return [t for t in dict.fromkeys((here, saved_token(name) if env is None else '')) if t]


def _signed_in(candidates, call):
    """call(token) with each token until one is not refused (HTTP 401 or 403: nothing was read or written): (its result,
    whether a later token than the first did it)."""
    for i, token in enumerate(candidates):
        try:
            return call(token), i > 0
        except (urllib.error.HTTPError, WriteError) as e:
            if getattr(e, 'code', None) not in (401, 403) or i == len(candidates) - 1:
                raise
    raise LookupError('no token')


def fetch(cfg, ref, env=None):
    """(file name, Markdown) of one reference; None when it does not exist or cannot be read (logged)."""
    kind, value = ref
    jira = kind == 'jira'
    base = (cfg.jira_url if jira else cfg.confluence_url).rstrip('/')
    if not base.startswith(('https://', 'http://')):  # only the configured web server, never file: or the like
        log.warning('cannot read %s: %s is not an http(s) URL', value, base)
        return None
    name = cfg.jira_token_env if jira else cfg.confluence_token_env
    candidates = tokens(name, env)
    if not candidates:
        log.warning('cannot read %s: %s is not set', value, name)
        return None

    def read(token):
        if jira:
            issue = _get(f'{base}/rest/api/2/issue/{urllib.parse.quote(value)}?fields=summary,status,issuetype,'
                         'priority,assignee,reporter,labels,components,description,comment', token)
            return f'jira-{value}.md', _issue_markdown(base, issue)
        if kind == 'confluence':
            page = _get(f'{base}/rest/api/content/{value}?expand=body.storage,space,version', token)
        else:
            space, title = value
            query = urllib.parse.urlencode({'spaceKey': space, 'title': title, 'expand': 'body.storage,space,version'})
            pages = _get(f'{base}/rest/api/content?{query}', token).get('results') or []
            if not pages:
                log.info('no Confluence page %s in space %s', title, space)
                return None
            page = pages[0]
        return f"confluence-{page['id']}.md", _page_markdown(base, page)

    try:
        return _signed_in(candidates, read)[0]
    except urllib.error.HTTPError as e:
        (log.info if e.code == 404 else log.warning)('cannot read %s: HTTP %s', value, e.code)
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        log.warning('cannot read %s: %s', value, e)
    return None


def fetch_all(cfg, refs):
    """fetch() each reference: [(file name, Markdown)] of those that could be read."""
    return [found for ref in refs if (found := fetch(cfg, ref))]


class WriteError(Exception):
    """A write to Jira or Confluence did not happen; the message says why (from Jira's or Confluence's reply)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code  # the HTTP status, when Jira or Confluence answered


def whoami(cfg, kind, env=None):
    """Who the Jira ('jira') or Confluence ('confluence') token signs in as, for `superagent check`: (name, '', note)
    or ('', what is wrong, ''). `note` says when only the token saved for the Windows user worked. One GET."""
    jira = kind == 'jira'
    base = (cfg.jira_url if jira else cfg.confluence_url).rstrip('/')
    name = cfg.jira_token_env if jira else cfg.confluence_token_env
    if not base.startswith(('https://', 'http://')):
        return '', f"{base or '(empty)'} is not an http(s) URL", ''
    candidates = tokens(name, env)
    if not candidates:
        return '', f'environment variable {name} is not set', ''
    url = f'{base}/rest/api/2/myself' if jira else f'{base}/rest/api/user/current'

    def me(token):
        found = _get(url, token)
        if not jira and found.get('type') == 'anonymous':  # Confluence answers a token it does not take as anonymous
            raise urllib.error.HTTPError(url, 401, 'anonymous', {}, None)
        return found

    try:
        found, saved = _signed_in(candidates, me)
    except urllib.error.HTTPError as e:
        return '', f'{base} refused {name} (HTTP {e.code}): create a new personal access token and save it', ''
    except (urllib.error.URLError, OSError, ValueError) as e:
        return '', f'cannot reach {base}: {getattr(e, "reason", e)}', ''
    note = (f'with the {name} saved for your Windows user; this window still has an older one, so open a new '
            'window (close and reopen the terminal app) before `superagent run`' if saved else '')
    return found.get('displayName') or found.get('name') or found.get('username') or '?', '', note


def target(p):
    """What a proposal (report.validate_proposal) would write, in words."""
    if p['kind'] == 'jira-issue':
        return f"a {p.get('issuetype') or 'Task'} in Jira project {p['project']}: {p['summary']}"
    if p['kind'] == 'jira-comment':
        return f"a comment on Jira issue {p['issue']}"
    if p['kind'] == 'confluence-update':
        how = 'adds to the end of' if p.get('mode', 'append') == 'append' else 'REPLACES the whole text of'
        return (f"an update that {how} Confluence page {p['page']}"
                + (f" and retitles it {p['title']}" if p.get('title') else ''))
    return (f"a Confluence page in space {p['space']}: {p['title']}"
            + (f" (under page {p['parent']})" if p.get('parent') else ''))


def write(cfg, proposal, env=None):
    """Do an approved proposal (report.validate_proposal): create the Jira issue, add the Jira comment, or create the
    Confluence page, from its Markdown. Returns (what, url): e.g. ('Jira issue RWT-123', its URL). Raises WriteError.
    Only ever called after a person approved it. A token that is refused (nothing written) is followed by the one saved
    for the Windows user, when that differs."""
    kind = proposal['kind']
    jira = kind.startswith('jira')
    base = (cfg.jira_url if jira else cfg.confluence_url).rstrip('/')
    if not base.startswith(('https://', 'http://')):
        raise WriteError(f"[atlassian] {'jira_url' if jira else 'confluence_url'} is not set")
    name = cfg.jira_token_env if jira else cfg.confluence_token_env
    candidates = tokens(name, env)
    if not candidates:
        raise WriteError(f'{name} is not set where `superagent run` runs')
    text = proposal['body']

    def send(token):
        if kind == 'jira-issue':
            made = _post(f'{base}/rest/api/2/issue', token, {'fields': {
                'project': {'key': proposal['project']}, 'summary': proposal['summary'],
                'issuetype': {'name': proposal['issuetype']}, 'description': to_jira(text)}})
            return f"Jira issue {made['key']}", f"{base}/browse/{made['key']}"
        if kind == 'jira-comment':
            key = proposal['issue']
            made = _post(f'{base}/rest/api/2/issue/{urllib.parse.quote(key)}/comment', token, {'body': to_jira(text)})
            return f'comment on {key}', f"{base}/browse/{key}?focusedCommentId={made.get('id', '')}"
        if kind == 'confluence-update':
            return _update_page(base, token, proposal, text)
        page = {'type': 'page', 'title': proposal['title'], 'space': {'key': proposal['space']},
                'body': {'storage': {'value': to_storage(text), 'representation': 'storage'}}}
        if proposal.get('parent'):
            page['ancestors'] = [{'id': proposal['parent']}]
        made = _post(f'{base}/rest/api/content', token, page)
        return f"Confluence page {proposal['title']}", base + ((made.get('_links') or {}).get('webui') or
                                                              f"/pages/viewpage.action?pageId={made.get('id')}")

    return _signed_in(candidates, send)[0]


def _update_page(base, token, proposal, text):
    """A new version of an existing page: its text with the Markdown added at the end (append, keeping what is there,
    macros included) or as its whole text (replace). Confluence refuses it (409) when the page changed since it was
    read, so a person's edit in between is never overwritten."""
    pid = proposal['page']
    current = _get(f'{base}/rest/api/content/{pid}?expand=body.storage,version,space', token)
    old = ((current.get('body') or {}).get('storage') or {}).get('value') or ''
    body = old + to_storage(text) if proposal.get('mode', 'append') == 'append' else to_storage(text)
    version = ((current.get('version') or {}).get('number') or 0) + 1
    title = proposal.get('title') or current.get('title') or ''
    made = _send('PUT', f'{base}/rest/api/content/{pid}', token, {
        'id': pid, 'type': current.get('type') or 'page', 'title': title,
        'space': {'key': (current.get('space') or {}).get('key')},
        'version': {'number': version, 'message': 'superagent: approved update'},
        'body': {'storage': {'value': body, 'representation': 'storage'}}})
    return (f'Confluence page {title} (version {version})',
            base + ((made.get('_links') or {}).get('webui') or f'/pages/viewpage.action?pageId={pid}'))


def _post(url, token, payload):
    return _send('POST', url, token, payload)


def _send(method, url, token, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method=method,  # noqa: S310 - write() allows http(s) only
                                 headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json',
                                          'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')
        try:  # Jira: errorMessages + errors; Confluence: message
            data = json.loads(detail)
            detail = '; '.join([*data.get('errorMessages', []), *(f'{k}: {v}' for k, v in
                                                                (data.get('errors') or {}).items()),
                                *([data['message']] if data.get('message') else [])]) or detail
        except (ValueError, AttributeError):
            pass
        raise WriteError(f'HTTP {e.code}: {detail[:500]}', e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise WriteError(str(e)) from None


# Markdown -> Jira wiki markup / Confluence storage format: headings, paragraphs, **bold**, *italic*, `code`,
# [links](url), nested - and 1. lists, ``` code blocks, | tables |, > quotes and --- rules. Enough for issues,
# comments and research or design pages; anything else stays as text.

def _blocks(md):
    """Markdown as blocks: (h, level, text) (p, text) (list, [(depth, ordered, text)]) (code, lang, text)
    (table, [cells per row], has header) (quote, text) (hr,)."""
    lines, out, i = md.replace('\r\n', '\n').split('\n'), [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif fence := re.match(r'\s*```\s*([\w+-]*)', line):
            body, i = [], i + 1
            while i < len(lines) and not re.match(r'\s*```', lines[i]):
                body.append(lines[i])
                i += 1
            out.append(('code', fence.group(1), '\n'.join(body)))
            i += 1
        elif h := re.match(r'(#{1,6})\s+(.*)', line):
            out.append(('h', len(h.group(1)), h.group(2).strip().rstrip('#').strip()))
            i += 1
        elif re.fullmatch(r'\s*([-*_])(\s*\1){2,}\s*', line):
            out.append(('hr',))
            i += 1
        elif line.lstrip().startswith('|'):
            rows, header = [], False
            while i < len(lines) and lines[i].lstrip().startswith('|'):
                cells = [c.strip() for c in lines[i].strip().strip('|').split('|')]
                if all(re.fullmatch(r':?-{2,}:?', c) for c in cells):
                    header = len(rows) == 1  # | a | b | then |---|---|: the first row is the header
                else:
                    rows.append(cells)
                i += 1
            out.append(('table', rows, header))
        elif line.lstrip().startswith('>'):
            body = []
            while i < len(lines) and lines[i].lstrip().startswith('>'):
                body.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append(('quote', ' '.join(body)))
        elif re.match(r'\s*([-*+]|\d+[.)])\s+', line):
            items = []
            while i < len(lines) and (m := re.match(r'(\s*)([-*+]|\d+[.)])\s+(.*)', lines[i])):
                items.append((len(m.group(1).replace('\t', '    ')) // 2, m.group(2)[0] not in '-*+', m.group(3)))
                i += 1
            out.append(('list', items))
        else:
            body = []
            while i < len(lines) and lines[i].strip() and not re.match(r'\s*(```|#{1,6}\s|>|\||[-*+]\s|\d+[.)]\s)',
                                                                        lines[i]):
                body.append(lines[i].strip())
                i += 1
            out.append(('p', ' '.join(body)))
    return out


def _inline_jira(text):
    text = re.sub(r'`([^`]+)`', r'{{\1}}', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', r'[\1|\2]', text)
    text = re.sub(r'\*\*(.+?)\*\*', lambda m: f'\x01{m.group(1)}\x01', text)  # bold, kept apart from italics
    text = re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])', r'_\1_', text)
    return text.replace('\x01', '*')


def to_jira(md):
    """Markdown as Jira wiki markup."""
    out = []
    for b in _blocks(md):
        if b[0] == 'h':
            out.append(f'h{b[1]}. {_inline_jira(b[2])}')
        elif b[0] == 'p':
            out.append(_inline_jira(b[1]))
        elif b[0] == 'list':
            out.append('\n'.join(('#' if ordered else '*') * (depth + 1) + ' ' + _inline_jira(t)
                                 for depth, ordered, t in b[1]))
        elif b[0] == 'code':
            out.append(f"{{code{':' + b[1] if b[1] else ''}}}\n{b[2]}\n{{code}}")
        elif b[0] == 'table':
            rows = b[1]
            lines = [('||' + '||'.join(_inline_jira(c) for c in r) + '||') if n == 0 and b[2] else
                     ('|' + '|'.join(_inline_jira(c) for c in r) + '|') for n, r in enumerate(rows)]
            out.append('\n'.join(lines))
        elif b[0] == 'quote':
            out.append(f'bq. {_inline_jira(b[1])}')
        else:
            out.append('----')
    return '\n\n'.join(out)


def _inline_html(text):
    text = html.escape(text, quote=False)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)\s]+)\)', lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>',
                  text)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    return re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])', r'<em>\1</em>', text)


def _list_html(items):
    out, stack = [], []  # stack: the tags of the open lists
    for depth, ordered, text in items:
        tag = 'ol' if ordered else 'ul'
        while len(stack) > depth + 1:
            out.append(f'</li></{stack.pop()}>')
        if len(stack) == depth + 1:
            # the next item at this depth: same kind of list, or a new list when - turns into 1. (or back)
            out.append('</li>' if stack[-1] == tag else f'</li></{stack.pop()}>')
        while len(stack) < depth + 1:
            out.append(f'<{tag}>')
            stack.append(tag)
        out.append(f'<li>{_inline_html(text)}')
    while stack:
        out.append(f'</li></{stack.pop()}>')
    return ''.join(out)


def to_storage(md):
    """Markdown as Confluence storage format (XHTML; code blocks as code macros)."""
    out = []
    for b in _blocks(md):
        if b[0] == 'h':
            out.append(f'<h{b[1]}>{_inline_html(b[2])}</h{b[1]}>')
        elif b[0] == 'p':
            out.append(f'<p>{_inline_html(b[1])}</p>')
        elif b[0] == 'list':
            out.append(_list_html(b[1]))
        elif b[0] == 'code':
            lang = f'<ac:parameter ac:name="language">{html.escape(b[1])}</ac:parameter>' if b[1] else ''
            body = b[2].replace(']]>', ']]]]><![CDATA[>')
            out.append(f'<ac:structured-macro ac:name="code">{lang}'
                       f'<ac:plain-text-body><![CDATA[{body}]]></ac:plain-text-body></ac:structured-macro>')
        elif b[0] == 'table':
            rows = ''.join('<tr>' + ''.join(f'<{"th" if n == 0 and b[2] else "td"}>{_inline_html(c)}'
                                            f'</{"th" if n == 0 and b[2] else "td"}>' for c in r) + '</tr>'
                           for n, r in enumerate(b[1]))
            out.append(f'<table><tbody>{rows}</tbody></table>')
        elif b[0] == 'quote':
            out.append(f'<blockquote><p>{_inline_html(b[1])}</p></blockquote>')
        else:
            out.append('<hr/>')
    return ''.join(out)


def _get(url, token):
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})  # noqa: S310 - fetch() allows http(s) only
    with urlopen(req, timeout=30) as r:
        return json.load(r)


def _name(person):
    return (person or {}).get('displayName') or (person or {}).get('name') or '-'


def _issue_markdown(base, issue):
    f = issue.get('fields') or {}
    key = issue.get('key', '?')
    facts = [f"Jira issue {base}/browse/{key}", (f.get('issuetype') or {}).get('name'),
             (f.get('status') or {}).get('name'), f"priority {(f.get('priority') or {}).get('name') or '-'}",
             f"assignee {_name(f.get('assignee'))}", f"reporter {_name(f.get('reporter'))}"]
    lines = [f"# {key}: {f.get('summary') or ''}", ' · '.join(x for x in facts if x)]
    if f.get('labels'):
        lines.append('Labels: ' + ', '.join(f['labels']))
    if f.get('components'):
        lines.append('Components: ' + ', '.join(c.get('name', '') for c in f['components']))
    lines += ['', '## Description', '', (f.get('description') or '(none)').strip()]
    comments = ((f.get('comment') or {}).get('comments') or [])[-COMMENTS:]
    if comments:
        lines += ['', f'## Latest comments ({len(comments)})']
        for c in comments:
            lines += ['', f"### {_name(c.get('author'))} · {c.get('created', '')[:16]}", '', (c.get('body') or '').strip()]
    return _cap('\n'.join(lines))


def _page_markdown(base, page):
    space = (page.get('space') or {}).get('key', '-')
    version = (page.get('version') or {}).get('number', '-')
    storage = ((page.get('body') or {}).get('storage') or {}).get('value') or ''
    return _cap(f"# {page.get('title', '')}\nConfluence page {base}/pages/viewpage.action?pageId={page.get('id')} · "
                f'space {space} · version {version}\n\n{storage_text(storage)}')


def _cap(text):
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP] + '\n\n… (cut: the rest is on the page)'


class _Text(HTMLParser):
    """Confluence storage format (XHTML with ac: macros) as plain text: blocks on their own lines, list items as
    `- `, headings as `#`, table cells separated by ` | `."""

    BLOCKS = {'p', 'div', 'br', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'li', 'pre', 'blockquote', 'table',
              'ac:structured-macro'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip += 1
        elif tag in self.BLOCKS:
            self.out.append('\n')
            if tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
                self.out.append('#' * int(tag[1]) + ' ')
            elif tag == 'li':
                self.out.append('- ')
        elif tag in ('td', 'th'):
            self.out.append(' | ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCKS:
            self.out.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def storage_text(storage):
    parser = _Text()
    parser.feed(storage)
    parser.close()
    text = html.unescape(''.join(parser.out))
    lines = [line.rstrip() for line in text.splitlines()]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
