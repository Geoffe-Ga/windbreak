# Operator Warnings

This document lists the residual risks SPEC §2 requires operators be told
about explicitly — risks no amount of software engineering can remove — and
the mitigation of last resort SPEC §2 recommends against all of them. Read
this before funding any exchange account this software will trade against.

## Residual risks this software cannot remove

- **Exchange insolvency or account freeze.** The exchange itself can fail,
  freeze withdrawals, or become otherwise unreachable, independent of
  anything this software does correctly.
- **Resolution-criteria disputes and reversals.** A market's settlement can
  be disputed or later reversed by the exchange after it appears final;
  windbreak's position and equity accounting must recompute around such a
  reversal, but it cannot prevent one from happening.
- **Fee-schedule changes.** The exchange can change its fee schedule at any
  time; a strategy calibrated against today's fees can lose its edge, or
  turn net-negative, the moment fees change.
- **Alpha decay from competing bots.** Any edge this software finds is
  competed away over time as other participants — human or automated — find
  and exploit the same mispricing. Historical results are not a guarantee of
  future results.
- **Silent LLM provider drift.** A forecasting model provider can change model
  behavior behind a pinned version string, silently degrading forecast
  quality without any error or exception windbreak can detect on its own.

None of these are hypothetical edge cases invented for legal cover — they are
the concrete, named failure modes this design is built around, and no
Risk Kernel or floor invariant can prevent any of them from happening. What
those mechanisms bound is *how much this software can lose you once one of
these events occurs* — not whether it occurs.

## Mitigation of last resort: money never deposited

The truest floor is money never deposited. Concretely:

- Fund the exchange account only with `total risk budget - external floor` —
  never with the floor capital itself.
- Keep floor capital in an unlinked account the exchange account cannot touch,
  so a compromised or frozen exchange account cannot reach it.
- Grant only trade-scope API keys to windbreak; never a key with withdrawal
  capability. windbreak's own preflight checklist (`credentials.no_withdrawal_scope`,
  see [`SECURITY.md`](SECURITY.md)) fails closed if a withdrawal-capable key is
  detected, but the first and best defense is never generating one for this
  software in the first place.

No configuration, Risk Kernel check, or dashboard control can substitute for
this operator-side discipline: it is the one mitigation that holds even if
every other layer of this software fails simultaneously.
