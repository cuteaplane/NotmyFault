from contextlib import contextmanager
from contextvars import ContextVar

from notmyfault.core.event_stream import EventStream, RUN_TERMINAL_EVENTS

RULE_TERMINAL_EVENTS = RUN_TERMINAL_EVENTS
_default_stream = EventStream()
_current_stream = ContextVar("engine_event_stream", default=None)


def current_stream():
    return _current_stream.get() or _default_stream


@contextmanager
def use_stream(stream):
    token = _current_stream.set(stream)
    try:
        yield
    finally:
        _current_stream.reset(token)


def subscribe(callback, **options):
    return current_stream().subscribe(callback, **options)


def unsubscribe(subscription):
    current_stream().unsubscribe(subscription)


def publish(event_type, data=None):
    return current_stream().publish(event_type, data)


def clear():
    current_stream().clear()


def flush(timeout=5):
    return current_stream().flush(timeout)


def subscriber_count():
    return current_stream().subscriber_count()
