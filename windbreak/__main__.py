"""Executable module entry point for ``python -m windbreak``.

Delegates to :func:`windbreak.main.main` so the package can be launched either
through the ``windbreak`` console script or as a runnable module.
"""

from windbreak.main import main

if __name__ == "__main__":
    raise SystemExit(main())
