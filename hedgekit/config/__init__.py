"""Shared component: configuration loading and validation.

Centralizes typed configuration for all four processes, including the
credential-boundary and budget checks enforced at startup (SPEC S5.2).
Concrete loading lands in a later issue; this package reserves the shared
import path.
"""
