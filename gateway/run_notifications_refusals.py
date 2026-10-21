"""Pacing for notices the gateway could not inject: which queued copies are the same notice, and when
a refused one is worth a log line. Pure helpers; the caller passes its clock.

A refused notice is retried on a backoff for as long as its refusal may clear, so whatever it logs per
retry adds up: live gateways wrote thousands of lines an hour per profile for notices that were never
admitted, evicting the agent.log history readers depend on."""
from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Hashable, Iterable

# A refused notice logs its first refusal, then at most one line per interval (with the count since).
REFUSAL_LOG_INTERVAL_S = 600.0
_REFUSAL_LOG_CAPACITY = 512


def notice_key(evt: dict[str, Any]) -> str:
    """Identity of a queued notice: its producer payload, without the ``_``-prefixed retry bookkeeping
    the drain adds. Distinct watch matches differ by ``event_id``; only true copies share a key."""
    return json.dumps({k: v for k, v in evt.items() if not str(k).startswith("_")}, sort_keys=True, default=str)


def dedupe_notices(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One event per notice, in first-seen order. Of identical copies, the one furthest into its
    backoff is kept, so a fresh copy cannot reset a refused notice's retry schedule."""
    kept: dict[str, dict[str, Any]] = {}
    for evt in events:
        key = notice_key(evt)
        held = kept.get(key)
        if held is None or int(evt.get("_watch_retry_step") or 0) > int(held.get("_watch_retry_step") or 0):
            kept[key] = evt
    return list(kept.values())


class RefusalLog:
    """When a refused notice gets a log line: its first refusal, then once per ``interval``.

    ``due`` returns how many refusals the line stands for (0: stay quiet). Bounded by ``capacity``
    notices; the least recently refused is forgotten first and logs again on its next refusal."""

    def __init__(self, interval: float = REFUSAL_LOG_INTERVAL_S, capacity: int = _REFUSAL_LOG_CAPACITY):
        self._interval, self._capacity = interval, capacity
        self._seen: OrderedDict[Hashable, tuple[float, int]] = OrderedDict()

    def due(self, key: Hashable, now: float) -> int:
        last, unreported = self._seen.pop(key, (None, 0))
        unreported += 1
        report = last is None or now - last >= self._interval
        self._seen[key] = (now, 0) if report else (last, unreported)
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return unreported if report else 0
