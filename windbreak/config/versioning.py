"""Stable hashing and path-level diffing of windbreak configurations.

Every configuration version is ledgerable: :func:`config_hash` gives a
deterministic, YAML-key-order-independent SHA-256 digest, and
:func:`diff_configs` reports exactly which dotted leaf paths were added,
removed, or changed between two versions. :func:`format_diff` renders that
diff for humans. All leaf values are integers, strings, booleans, or ``None``
(never floats; SPEC §6.1).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from windbreak.config.schema import WindbreakConfig


def _join_path(prefix: str, key: str) -> str:
    """Append ``key`` to a dotted ``prefix``, or start a fresh path."""
    return f"{prefix}.{key}" if prefix else key


def _flatten_node(node: object, prefix: str) -> dict[str, object]:
    """Recursively flatten a nested mapping/sequence into dotted-path leaves.

    Args:
        node: A mapping, list, tuple, or scalar drawn from ``asdict`` output.
        prefix: The dotted path accumulated to reach ``node``.

    Returns:
        A flat mapping of dotted leaf paths to their scalar values.
    """
    result: dict[str, object] = {}
    if isinstance(node, Mapping):
        for key, value in node.items():
            result.update(_flatten_node(value, _join_path(prefix, str(key))))
    elif isinstance(node, list | tuple):
        for index, value in enumerate(node):
            result.update(_flatten_node(value, _join_path(prefix, str(index))))
    else:
        result[prefix] = node
    return result


def flatten(config: WindbreakConfig) -> dict[str, object]:
    """Flatten a configuration into a mapping of dotted leaf paths to values."""
    return _flatten_node(dataclasses.asdict(config), "")


def canonical_json(config: WindbreakConfig) -> str:
    """Render a configuration as canonical, key-sorted, compact JSON."""
    return json.dumps(
        flatten(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def config_hash(config: WindbreakConfig) -> str:
    """Return the SHA-256 hex digest of a configuration's canonical form."""
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ConfigDiff:
    """The path-level difference between two configuration versions.

    Attributes:
        added: Leaf paths present only in the new configuration.
        removed: Leaf paths present only in the old configuration.
        changed: Leaf paths present in both, mapped to ``(old, new)`` values.
    """

    added: dict[str, object] = field(default_factory=dict)
    removed: dict[str, object] = field(default_factory=dict)
    changed: dict[str, tuple[object, object]] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """Return whether the diff records no additions, removals, or changes."""
        return not (self.added or self.removed or self.changed)


def diff_configs(old: WindbreakConfig, new: WindbreakConfig) -> ConfigDiff:
    """Compute the added, removed, and changed leaf paths from ``old`` to ``new``."""
    old_flat = flatten(old)
    new_flat = flatten(new)
    old_keys = set(old_flat)
    new_keys = set(new_flat)
    return ConfigDiff(
        added={key: new_flat[key] for key in new_keys - old_keys},
        removed={key: old_flat[key] for key in old_keys - new_keys},
        changed={
            key: (old_flat[key], new_flat[key])
            for key in old_keys & new_keys
            if old_flat[key] != new_flat[key]
        },
    )


def format_diff(diff: ConfigDiff) -> str:
    """Render a diff as sorted ``+``/``-``/``~`` lines, or a no-op marker."""
    if diff.is_empty:
        return "(no changes)"
    added = [f"+ {path}: {diff.added[path]}" for path in sorted(diff.added)]
    removed = [f"- {path}: {diff.removed[path]}" for path in sorted(diff.removed)]
    changed = [
        f"~ {path}: {diff.changed[path][0]} -> {diff.changed[path][1]}"
        for path in sorted(diff.changed)
    ]
    return "\n".join(added + removed + changed)
