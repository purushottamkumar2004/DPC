"""
Health-check interface.

A health check answers one question: "is this worker doing its job?"
(The monitor has already checked that the process is alive.)

To add a new kind of check: subclass HealthCheck, implement check(),
and register it in basic_health_check.build_health_check().
"""

from dataclasses import dataclass


@dataclass
class HealthResult:
    healthy: bool
    message: str = "ok"


class HealthCheck:
    def check(self, worker) -> HealthResult:
        raise NotImplementedError
