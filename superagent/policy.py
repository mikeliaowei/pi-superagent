"""Who may ask for work. MVP: explicit requests only (dsh-superagent's accept mode 'off')."""

import re
from dataclasses import dataclass

from .model import Trigger


@dataclass
class Decision:
    kind: str  # accept | stop | context | ignore
    reason: str
    ticket_ids: tuple = ()


def gate(trigger: Trigger, agent_login, allowed, live_ticket_ids):
    """Decide what a Trigger means. `live_ticket_ids` are the open Tickets subscribed to its thread."""
    actor = trigger.actor.lower()
    if actor == agent_login.lower():
        return Decision('ignore', 'own message')
    if not trigger.human:
        return Decision('ignore', 'bot')
    listed = actor in {a.lower() for a in allowed}
    if listed and trigger.addressed:
        if is_stop(trigger.text, agent_login):
            return Decision('stop', 'listed person asked to stop', tuple(live_ticket_ids))
        if live_ticket_ids:
            # A new request on a thread that already has work goes to that work, like a follow-up.
            return Decision('context', 'addressed follow-up', tuple(live_ticket_ids))
        return Decision('accept', 'listed person addressed the agent')
    if live_ticket_ids:
        # Fan-out gives context only, never new authority.
        return Decision('context', 'listed follow-up' if listed else 'unlisted follow-up', tuple(live_ticket_ids))
    return Decision('ignore', 'not addressed by a listed person')


def _unquoted(text):
    """The text without quoted lines (`> ...`): quoting someone's request or stop is not making one."""
    return '\n'.join(line for line in (text or '').splitlines() if not line.lstrip().startswith('>'))


def _mention(agent_login):
    return rf'(^|[^\w-])@{re.escape(agent_login)}(?![\w-])'


def is_stop(text, agent_login):
    return re.search(_mention(agent_login) + r'\s+stop\b', _unquoted(text), re.I | re.M) is not None


def mentions(text, agent_login):
    return re.search(_mention(agent_login), _unquoted(text), re.I | re.M) is not None
