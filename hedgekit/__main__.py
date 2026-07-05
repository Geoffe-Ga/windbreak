"""Executable module entry point for ``python -m hedgekit``.

Delegates to :func:`hedgekit.main.main` so the package can be launched either
through the ``hedgekit`` console script or as a runnable module.
"""

from hedgekit.main import main

if __name__ == "__main__":
    raise SystemExit(main())
