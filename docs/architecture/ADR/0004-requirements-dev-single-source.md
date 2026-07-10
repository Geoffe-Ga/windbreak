# ADR-0004: requirements-dev.txt + constraints-quality.txt are the single dev-dependency source

- **Status:** Accepted
- **Date:** 2026-07-10
- **Issue:** #80

## Context

Dev dependencies were declared in **two** places: `requirements-dev.txt` (the
list every consumer actually installs from — `ci.yml`,
`provision-venv.sh`'s shared fleet venv, `metrics.yml`, `deslop.yml`,
`_claude-scan.yml`, `mutation-gate.yml`, and the README setup instructions)
and `pyproject.toml`'s `[project.optional-dependencies].dev` extra, which was
read by **nothing** — no `.[dev]` install exists anywhere in the repo.

The two lists had already drifted: the pyproject extra was missing
`pytest-mock`, `pytest-asyncio`, `pytest-timeout`, `pytest-xdist`, `xenon`,
`PyYAML`, and `types-PyYAML`, all of which `requirements-dev.txt` carried
(surfaced by PR #78). Two hand-maintained lists that must agree, with nothing
forcing them to agree, is drift by construction — the same governance flaw
ADR-0002 identified for formatters, and that #104 solved for versions
(`constraints-quality.txt` is the single version-pin authority for dev
tooling).

## Decision

Delete the pyproject `dev` extra entirely. `requirements-dev.txt` (names) +
`constraints-quality.txt` (versions, per #104) are the single
dev-dependency source of truth that CI and local setup already read. A
regression test, `test_pyproject_declares_no_dev_extra` in
`tests/toolchain/test_toolchain_pins.py`, keeps the extra from silently
reappearing — making re-drift structurally impossible rather than merely
CI-policed.

## Alternatives considered

1. **Keep both lists and add a CI test asserting their name-sets stay
   equal.** Rejected: this institutionalizes the duplication DRY exists to
   prevent. Two lists policed into agreement is strictly worse than one
   list.
2. **Make the pyproject extra canonical and reduce `requirements-dev.txt` to
   `-e .[dev]` / `.[dev]`.** Rejected: `provision-venv.sh` installs
   `requirements-dev.txt` into a *shared* fleet venv and explicitly forbids
   editable/self-installs there — the shared venv must not bind to any one
   lane's checkout. A bare `.[dev]` line would also install the `windbreak`
   package itself into that shared venv, a behavior change. Because
   `requirements-dev.txt` cannot be the self-referential path, the pyproject
   extra is the removable duplicate, not the other way around.

## Consequences

- **Positive:** Single authority, drift impossible by construction. No
  consumer changed — every consumer already read `requirements-dev.txt`,
  which is precisely why deletion, rather than synchronization, was the
  correct fix.
- **Out of scope:** A compiled/hashed lockfile is a separate concern, already
  tracked as issue #64. This ADR eliminates the duplicate *declaration*; it
  does not introduce a lockfile.
- **Cross-references:** #104 (`constraints-quality.txt` as the version-pin
  authority) and ADR-0002 (single-formatter governance precedent for the
  same class of problem).
