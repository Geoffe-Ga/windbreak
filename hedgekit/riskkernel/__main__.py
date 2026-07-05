"""Executable module entry point for ``python -m hedgekit.riskkernel``.

Delegates to :func:`hedgekit.riskkernel.process.main` so the Risk Kernel's
bounded heartbeat loop can be launched directly as a runnable module.
"""

from hedgekit.riskkernel.process import main

if __name__ == "__main__":
    raise SystemExit(main())
