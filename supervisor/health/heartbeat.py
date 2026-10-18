"""
Heartbeats received over MQTT (dpc/worker/<id>/health).

Two payload shapes are accepted (CONTRACTS 2, D-034, append-only):

  old       {"worker_id": "x", "healthy": true}
  extended  {"worker_id": "x", "healthy": true, "pid": 123, "version": "v0.4",
             "config_hash": "ab12", "fps": 3.9, "last_frame_age_s": 0.2, "inf_queue_depth": 1}

Everything except "healthy" is optional, and unknown fields are ignored, so an old
worker and a new worker both work. Only the known extended fields are kept, with their
type checked and strings cut short: the Supervisor republishes them in the worker status,
and a worker must never be able to push arbitrary text (or a secret) onto MQTT that way.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional

# field name -> accepted python types
EXTENDED_FIELDS = {
    "pid": int,
    "version": str,
    "config_hash": str,
    "fps": (int, float),
    "last_frame_age_s": (int, float),
    "inf_queue_depth": int,
}
MAX_TEXT = 64


@dataclass(frozen=True)
class Heartbeat:
    received_at: float
    healthy: bool = True
    info: Dict[str, object] = field(default_factory=dict)   # the extended fields that were present


def parse_heartbeat(payload, now: Optional[float] = None) -> Optional[Heartbeat]:
    """Heartbeat from an MQTT payload, or None when the payload is not a heartbeat at all."""
    if not isinstance(payload, dict):
        return None
    healthy = payload.get("healthy", True)          # old workers may leave it out
    if not isinstance(healthy, bool):
        return None
    info = {}
    for name, types in EXTENDED_FIELDS.items():
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, types):   # bool is an int in Python
            continue
        info[name] = value[:MAX_TEXT] if isinstance(value, str) else value
    return Heartbeat(received_at=time.time() if now is None else now, healthy=healthy, info=info)
