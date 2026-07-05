"""Process D: the local Dashboard and alerting surface (SPEC S5.1).

Serves the operator dashboard and alerts on 127.0.0.1. Per SPEC S5.1 this
process holds **no exchange** credentials -- only its own dashboard auth
secret -- and reads evaluation and ledger data without any trade authority.

Two dependency-injection seams are wired by successor issues: the auth
``token`` passed to :func:`create_server` is minted from configuration (issue
#11), and the ``status_source`` callable is backed by the read-only ledger
view (issue #13). Until then callers supply both explicitly, so this module
has no ambient dependency on either.
"""

from hedgekit.dashboard.app import DashboardStatus, create_server

__all__ = [
    "DashboardStatus",
    "create_server",
]
