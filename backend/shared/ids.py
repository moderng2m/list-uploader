"""ULID generation (time-sortable IDs for jobs and audit events)."""

from __future__ import annotations

import os
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RANDOM_BITS = 80
_lock = threading.Lock()
_last: tuple[int, int] = (-1, 0)


def _encode(value: int) -> str:
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(timestamp_ms: int | None = None) -> str:
    """26-char Crockford base32 ULID: 48-bit ms timestamp + 80 random bits.

    Monotonic within a process: IDs made in the same millisecond still sort in
    creation order, so audit events written together keep their order.
    """
    global _last
    ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    with _lock:
        last_ts, last_rand = _last
        if ts <= last_ts and timestamp_ms is None:
            ts, rand = last_ts, (last_rand + 1) % (1 << _RANDOM_BITS)
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        if timestamp_ms is None:
            _last = (ts, rand)
    return _encode((ts << _RANDOM_BITS) | rand)


def new_job_id() -> str:
    return f"j_{new_ulid()}"
