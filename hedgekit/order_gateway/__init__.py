"""Process C: the Order Gateway (SPEC S5.1-S5.3).

The Order Gateway holds trade-only exchange credentials and the sole
approval-token **verification** key (SPEC S5.2). It verifies each single-use
token before submitting the corresponding order and hosts the Reconciler. Per
the SPEC S5.3 import boundary, only this package may import the exchange
order-submission client; a future import-linter check will enforce that rule
in CI.
"""
