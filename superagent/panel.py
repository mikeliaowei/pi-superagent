"""The web panel: shows only what needs a person, plus enough context to act on it.

Run it with `superagent panel` (or `streamlit run superagent/panel.py -- <config>`); the config path comes from
the SUPERAGENT_CONFIG environment variable or the argument after `--`. The panel reads the store through Store
methods only. Its Stop and Retry buttons record the same operator requests as `superagent stop|retry`, which the
running service applies. It never shows the GitHub token, a Ticket's report token, or environment values.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from superagent import cli
from superagent.model import stage

REFRESH_SECONDS = 10
RECENT_TRIGGERS = 300
DECISIONS = ('accept', 'stop', 'context', 'ignore')


def config_path():
    if os.environ.get('SUPERAGENT_CONFIG'):
        return os.environ['SUPERAGENT_CONFIG']
    if os.path.basename(sys.argv[0]) == 'panel.py' and len(sys.argv) > 1:
        return sys.argv[1]
    return None


def parse_time(s):
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def issue_url(cfg, n):
    return f'https://github.com/{cfg.repo}/issues/{n}'


def pr_url(cfg, n):
    return f'https://github.com/{cfg.repo}/pull/{n}' if n else None


def updated_at(t):
    times = [t.created_at, t.last_human_at, *(x for a in t.assignments for x in (a['started_at'], a['ended_at']))]
    return max((x for x in times if x), default=t.created_at)


def ended_at(t):
    a = t.current
    return (a.ended_at if a and a.ended_at else None) or updated_at(t)


def act(store, ticket, action, where):
    if st.button(action.capitalize(), key=f'{where}-{action}-{ticket.id}'):
        try:
            st.success(cli.request_action(store, ticket.id, action))
        except RuntimeError as e:
            st.error(str(e))


def actions(store, t, where):
    if t.state == 'ended':
        return
    left, right, _ = st.columns([1, 1, 6])
    with left:
        act(store, t, 'stop', where)
    with right:
        act(store, t, 'retry', where)


# Tabs

def overview(cfg, store, tickets):
    st.subheader('Needs a person')
    people = [t for t in tickets if t.state == 'open' and stage(t).startswith('person/')]
    if not people:
        st.info('Nothing needs a person right now.')
    for t in people:
        a = t.current
        with st.container(border=True):
            st.markdown(f'**{t.name}** · `{stage(t)}` · [#{t.number}]({issue_url(cfg, t.number)})'
                        + (f' · [PR #{t.pr_number}]({pr_url(cfg, t.pr_number)})' if t.pr_number else ''))
            if t.delivery_error:
                st.markdown(f'Delivery failed: {t.delivery_error}')
            if a and a.question:
                st.markdown(f'Question: {a.question}')
            if a and a.summary:
                st.markdown(f'Summary: {a.summary}')
            actions(store, t, 'ov')

    st.subheader('Last 24 hours')
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = lambda s: (parse_time(s) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff  # noqa: E731
    triggers = [trig for trig, _, _ in store.recent_triggers(RECENT_TRIGGERS) if recent(trig.at)]
    opened = [t for t in tickets if recent(t.created_at)]
    ended = [t for t in tickets if t.state == 'ended' and recent(ended_at(t))]
    running = sum(1 for t in tickets if t.state == 'open' and t.current and t.current.state == 'running')
    cols = st.columns(4)
    cols[0].metric('Triggers received (24 h)', len(triggers))
    cols[1].metric('Tickets opened (24 h)', len(opened))
    cols[2].metric('Tickets ended (24 h)', len(ended))
    cols[3].metric('Running workers', f'{running} / {cfg.max_workers}')
    if ended:
        outcomes = {}
        for t in ended:
            outcomes[t.outcome or 'unknown'] = outcomes.get(t.outcome or 'unknown', 0) + 1
        st.markdown('Ended by outcome: ' + ', '.join(f'{k} {v}' for k, v in sorted(outcomes.items())))

    status = cli.status_data(cfg, store)
    st.markdown(f"Last poll: {status['last_poll'] or 'never (is `superagent run` running?)'}")
    st.subheader('Failing effects')
    if status['failing_effects']:
        st.dataframe(pd.DataFrame(status['failing_effects'])[['kind', 'key', 'attempts', 'error']],
                     hide_index=True, width='stretch')
    else:
        st.markdown('None.')


def tickets_tab(cfg, store, tickets):
    c1, c2, c3 = st.columns([1, 2, 2])
    scope = c1.radio('Show', ['open', 'all'], horizontal=True, key='ticket-scope')
    stages = c2.multiselect('Stage', sorted({stage(t) for t in tickets}), key='ticket-stages')
    query = c3.text_input('Search', key='ticket-search').strip().lower()
    shown = [t for t in reversed(tickets) if (scope == 'all' or t.state == 'open')
             and (not stages or stage(t) in stages)
             and (not query or query in ' '.join([t.id, t.name, t.title, *(a['summary'] for a in t.assignments)])
                  .lower())]
    if not shown:
        st.info('No tickets match.')
        return
    rows = [{'id': t.id, 'name': t.name, 'stage': stage(t), 'issue': issue_url(cfg, t.number),
             'pr': pr_url(cfg, t.pr_number), 'last report': t.current.report if t.current else None,
             'updated': updated_at(t)} for t in shown]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch', column_config={
        'issue': st.column_config.LinkColumn('#', display_text=r'issues/(\d+)$'),
        'pr': st.column_config.LinkColumn('PR', display_text=r'pull/(\d+)$')})

    by_id = {t.id: t for t in shown}
    tid = st.selectbox('Details for', list(by_id), format_func=lambda i: f'{by_id[i].name} ({i})',
                       key='ticket-detail')
    t = by_id[tid]
    m = t.mandate
    st.markdown(f"**{t.name}**: {t.title} · `{stage(t)}` · [#{t.number}]({issue_url(cfg, t.number)})"
                + (f' · [PR #{t.pr_number}]({pr_url(cfg, t.pr_number)})' if t.pr_number else ''))
    st.markdown(f"Asked by **{m.get('actor', '?')}** at {m.get('at') or t.created_at}"
                f" ({m.get('strength', 'explicit')} mandate)")
    st.markdown(f'Branch `{t.branch or "-"}` · worktree `{t.worktree or "-"}`'
                + (f' · outcome **{t.outcome}**' if t.outcome else ''))
    if t.delivery_error:
        st.markdown(f'Delivery error: {t.delivery_error}')
    request = store.trigger(m.get('trigger_id'))
    if request:
        st.markdown('Request')
        st.text(request.text)
    st.markdown('Assignments')
    st.dataframe(pd.DataFrame([{k: a[k] for k in ('id', 'state', 'report', 'summary', 'question', 'end_reason',
                                                    'started_at', 'ended_at')} for a in t.assignments],
                              columns=['id', 'state', 'report', 'summary', 'question', 'end_reason', 'started_at',
                                       'ended_at']), hide_index=True, width='stretch')
    inputs = store.inputs(t.id)
    waiting = sum(1 for i in inputs if not i['delivered'])
    st.markdown(f'Messages for the worker: {len(inputs)} ({waiting} not yet delivered)')
    if inputs:
        st.dataframe(pd.DataFrame([{'at': i['at'], 'kind': i['kind'], 'text': i['text'],
                                    'delivered': 'yes' if i['delivered'] else 'no'} for i in inputs]),
                     hide_index=True, width='stretch')
    actions(store, t, 'tk')


def triggers_tab(cfg, store):
    st.caption('Every message the service read, with its policy decision and why.')
    decisions = st.multiselect('Decision', DECISIONS, key='trigger-decisions')
    trigs = [(trig, d, r) for trig, d, r in store.recent_triggers(RECENT_TRIGGERS) if not decisions or d in decisions]
    if not trigs:
        st.info('No triggers yet.' if not decisions else 'No triggers match.')
        return
    rows = [{'at': trig.at, 'decision': d, 'reason': r or '(recorded before reasons were kept)', 'actor': trig.actor,
             'kind': trig.kind, 'human': trig.human, 'addressed': trig.addressed, 'thread': trig.thread_key,
             'text': trig.text[:300], 'url': trig.url or None}
            for trig, d, r in trigs]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch',
                 column_config={'url': st.column_config.LinkColumn('link')})


def settings_tab(cfg):
    st.caption('Read-only. Edit the config file and restart `superagent run` to change it.')
    rows = [('config file', config_path()), ('repo', cfg.repo), ('agent_login', cfg.agent_login),
            ('allowed', ', '.join(cfg.allowed)), ('provider', cfg.provider), ('model', cfg.model),
            ('thinking', cfg.thinking), ('tools', cfg.tools), ('max_workers', cfg.max_workers),
            ('poll_seconds', cfg.poll_seconds), ('checkout', cfg.checkout), ('work_dir', cfg.work_dir),
            ('state_dir', cfg.state_dir), (f'token set ({cfg.token_env})', 'yes' if cfg.token else 'no')]
    st.dataframe(pd.DataFrame([{'setting': k, 'value': str(v)} for k, v in rows]), hide_index=True, width='stretch')


# App

@st.cache_resource
def open_store(db_path):
    from superagent.store import Store
    return Store(db_path)


@st.fragment(run_every=REFRESH_SECONDS)
def body(cfg):
    if not os.path.exists(cfg.db_path):
        st.info(f'No database yet at `{cfg.db_path}`. Run `superagent run` first.')
        return
    store = open_store(cfg.db_path)
    tickets = store.tickets()
    if st.button('Refresh', key='refresh'):
        pass  # the click itself reruns this fragment
    st.caption(f'Updated {datetime.now().strftime("%H:%M:%S")}; refreshes every {REFRESH_SECONDS} s.')
    if not tickets:
        st.info('No tickets yet. Mention the bot on an issue to start one.')
    tabs = st.tabs(['Overview', 'Tickets', 'Triggers', 'Settings'])
    with tabs[0]:
        overview(cfg, store, tickets)
    with tabs[1]:
        if tickets:
            tickets_tab(cfg, store, tickets)
        else:
            st.info('No tickets yet.')
    with tabs[2]:
        triggers_tab(cfg, store)
    with tabs[3]:
        settings_tab(cfg)


def main():
    st.set_page_config(page_title='superagent', layout='wide')
    st.title('superagent')
    path = config_path()
    if not path:
        st.error('No config: set SUPERAGENT_CONFIG or start the panel with `superagent panel`.')
        return
    try:
        cfg = cli.load_config(path)
    except cli.ConfigError as e:
        st.error(str(e))
        return
    st.caption(f'{cfg.repo} as @{cfg.agent_login}')
    body(cfg)


main()
