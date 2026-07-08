"""Tests for the four-process package skeleton (issue #10, SPEC S5.1-5.2).

Each of windbreak's four processes decomposes into two sub-packages, for
eight import paths total. Every one of them must exist, import cleanly,
and carry a non-empty module docstring. For the four packages whose
credential posture is load-bearing per SPEC S5.2 (the credential
boundary table), the docstring must also state that posture in words,
so the boundary is discoverable directly from the code -- not only from
the spec document that a future refactor could drift away from.
"""

from __future__ import annotations

import importlib

import pytest

#: The eight sub-packages that make up the four-process skeleton.
MODULE_PATHS = [
    "windbreak.pipeline",
    "windbreak.riskkernel",
    "windbreak.order_gateway",
    "windbreak.dashboard",
    "windbreak.ledger",
    "windbreak.config",
    "windbreak.numeric",
    "windbreak.alerts",
]


@pytest.mark.parametrize("module_path", MODULE_PATHS)
def test_module_imports_successfully(module_path: str) -> None:
    """Every skeleton module path is importable without error."""
    module = importlib.import_module(module_path)

    assert module is not None


@pytest.mark.parametrize("module_path", MODULE_PATHS)
def test_module_has_nonempty_docstring(module_path: str) -> None:
    """Every skeleton module documents its own purpose."""
    module = importlib.import_module(module_path)

    assert module.__doc__ is not None
    assert module.__doc__.strip() != ""


def test_riskkernel_docstring_declares_signing_authority() -> None:
    """riskkernel is the only package holding the approval-token signing key.

    SPEC S5.2: "Risk Kernel | read-only | approval-token signing key".
    """
    module = importlib.import_module("windbreak.riskkernel")
    assert module.__doc__ is not None

    assert "signing" in module.__doc__.lower()


def test_order_gateway_docstring_declares_verification_authority() -> None:
    """order_gateway is the only package holding the token-verification key.

    SPEC S5.2: "Order Gateway | trade-only | approval-token verification key".
    """
    module = importlib.import_module("windbreak.order_gateway")
    assert module.__doc__ is not None

    assert "verification" in module.__doc__.lower()


def test_pipeline_docstring_declares_no_trade_credentials() -> None:
    """pipeline (Process A) never holds trade-capable credentials.

    SPEC S5.1: "Process A: main pipeline -- no trade credentials".
    """
    module = importlib.import_module("windbreak.pipeline")
    assert module.__doc__ is not None

    assert "no trade credentials" in module.__doc__.lower()


def test_dashboard_docstring_declares_no_exchange_credentials() -> None:
    """dashboard (Process D) never holds exchange credentials.

    SPEC S5.1: "Process D: Dashboard -- no exchange credentials".
    """
    module = importlib.import_module("windbreak.dashboard")
    assert module.__doc__ is not None

    assert "no exchange" in module.__doc__.lower()
