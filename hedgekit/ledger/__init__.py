"""Shared component: the append-only, hash-chained ledger (SPEC S5.1).

Provides the tamper-evident event log that every process writes to and that
Evaluation and the Dashboard read from. Concrete persistence lands in a later
issue; this package reserves the shared import path.
"""
