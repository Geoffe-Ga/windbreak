"""Executable module entry point for ``python -m hedgekit.order_gateway``.

Delegates to :func:`hedgekit.order_gateway.gateway.main` so the Order Gateway's
bounded heartbeat loop can be launched directly as a runnable module.
"""

from hedgekit.order_gateway.gateway import main

if __name__ == "__main__":
    raise SystemExit(main())
