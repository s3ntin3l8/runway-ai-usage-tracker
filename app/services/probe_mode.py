"""A task-local "this is only a look" switch for the on-demand credential source probe (#434).

A probe runs real collectors, whose normal behaviour is to remember what they learn: refresh and
store tokens, cache a bearer they exchanged, flag an account as rejected, log a provider error.
None of that may happen because someone pressed "Probe". Rather than chase every collector, the
writes themselves honour this flag (``token_cache.store``, ``auth_failures.mark``, the collector
error recorder) and the one collector with its own refresh (xAI) asks it before refreshing.

A ``ContextVar``, so a probe running beside the poller never switches the poller's writes off.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_probing: ContextVar[bool] = ContextVar("runway_probe_mode", default=False)


def is_probing() -> bool:
    return _probing.get()


@contextmanager
def probing() -> Iterator[None]:
    token = _probing.set(True)
    try:
        yield
    finally:
        _probing.reset(token)
