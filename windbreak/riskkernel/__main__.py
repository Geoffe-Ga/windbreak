"""Executable module entry point for ``python -m windbreak.riskkernel``.

Delegates to :func:`windbreak.riskkernel.process.main` so the Risk Kernel's
bounded heartbeat loop can be launched directly as a runnable module.
"""

from windbreak.riskkernel.process import main

if __name__ == "__main__":
    raise SystemExit(main())
