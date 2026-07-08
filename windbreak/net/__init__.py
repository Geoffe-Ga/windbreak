"""Outbound-network boundary primitives for windbreak (issue #57).

Exposes the structural egress allowlist that a live deployment's outbound
connectors dial through, so no connector reaches a host outside the small,
explicit set derived from configuration.
"""

from __future__ import annotations

from windbreak.net.allowlist import (
    EgressDeniedError,
    EventRecorder,
    OutboundAllowlist,
    allowlist_from_config,
)

__all__ = [
    "EgressDeniedError",
    "EventRecorder",
    "OutboundAllowlist",
    "allowlist_from_config",
]
