"""Hard execution bounds for the isolated raster OCR path.

These limits intentionally do not inherit the general Agent client timeout.
Raster OCR is a bounded asynchronous preview operation, so allowing a local
profile or an environment value to stretch it beyond the evaluated budget
would make the queue lease and the user-visible latency contract incoherent.
"""

from __future__ import annotations

from typing import Final


# One OCR/layout attempt may consume at most one minute.  The Agent enforces
# the same (or a tighter internal) execution bound before Backend's transport
# deadline expires.
RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS: Final[float] = 60.0

# Rendering one already-approved PDF page is materially cheaper than OCR and
# must not occupy the OCR worker's full minute.
RASTER_PAGE_RENDER_TIMEOUT_SECONDS: Final[float] = 15.0

# Status is a small immutable-pointer read, never model execution.  Keeping
# it short prevents an unavailable Agent from consuming the OCR budget before
# a job is even dispatched.
RASTER_PROFILE_STATUS_TIMEOUT_SECONDS: Final[float] = 5.0

# The durable lease outlives the 60-second Agent call so the worker can finish
# source readback, profile rechecks, and one terminal repository transition.
# It is deliberately finite: a crashed worker is reclaimed promptly instead
# of leaving a job unavailable for the historical 450-second window.
RASTER_WORKER_LEASE_GRACE_SECONDS: Final[float] = 30.0
RASTER_WORKER_LEASE_SECONDS: Final[float] = (
    RASTER_AGENT_EXECUTION_TIMEOUT_SECONDS + RASTER_WORKER_LEASE_GRACE_SECONDS
)
