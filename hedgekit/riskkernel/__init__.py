"""Process B: the Risk Kernel veto authority (SPEC S5.1-S5.3).

The Risk Kernel is the sole holder of the approval-token **signing** key and
runs with read-only exchange credentials (SPEC S5.2). It validates normalized
order intents, reserves capital, and signs single-use approval tokens. Per the
SPEC S5.3 import boundary, only this package may import the approval-token
signing key handle; a future import-linter check will enforce that rule in CI.
"""
