"""Load, validate, and coerce SPEC §16 YAML into a typed configuration.

:func:`load_config` reads a YAML file and builds a fully-typed, immutable
:class:`~windbreak.config.schema.WindbreakConfig`, filling any absent section
from its SPEC §16 default. Every failure mode raises :class:`ConfigError`
with a message naming the offending dotted path, so no raw traceback ever
escapes to the operator: unknown keys are fatal, values are type-checked
(booleans are rejected where integers are expected), and the lone fractional
SPEC value (``bootstrap_confidence``) is converted to an exact integer
parts-per-million field or rejected if it cannot be represented exactly.
"""

from __future__ import annotations

import dataclasses
import math
import types
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, cast, get_args, get_origin, get_type_hints

import yaml

from windbreak.config.schema import _CONVERT, _YAML_KEY, WindbreakConfig
from windbreak.config.versioning import config_hash, diff_configs

if TYPE_CHECKING:
    from typing import Any, NoReturn

    from windbreak.config.recorder import ConfigEventRecorder

#: The integer scale that turns a 0..1 fraction into parts-per-million.
_PPM_SCALE = 1_000_000

#: The recorded ``source`` for a configuration built from built-in defaults.
_DEFAULTS_SOURCE = "<defaults>"

#: Human-readable names for the scalar leaf types, used in error messages.
_SCALAR_NAMES: dict[object, str] = {
    bool: "a boolean",
    int: "an integer",
    str: "a string",
}


class ConfigError(Exception):
    """Raised for any configuration load, parse, or validation failure."""


class _NonMappingRootError(ConfigError):
    """Raised when a configuration file's root document is not a mapping."""

    def __init__(self) -> None:
        """Build the fixed non-mapping-root message inside the exception."""
        super().__init__("configuration root must be a mapping")


def _type_error(expected: str, value: object, path: str) -> NoReturn:
    """Raise a :class:`ConfigError` describing a type mismatch at ``path``."""
    raise ConfigError(f"{path}: expected {expected}, got {type(value).__name__}")


def confidence_to_ppm(value: object, path: str) -> int:
    """Convert a 0..1 confidence into an exact integer parts-per-million.

    Args:
        value: The raw YAML value (an ``int`` or ``float``, never ``bool``).
        path: The dotted config path, for error messages.

    Returns:
        The confidence expressed as an integer in ``[0, 1_000_000]``.

    Raises:
        ConfigError: If ``value`` is not a finite number, cannot map to an
            exact ppm integer, or falls outside the ``[0, 1]`` range.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        _type_error("a number in [0, 1]", value, path)
    if not math.isfinite(value):
        raise ConfigError(
            f"{path}: bootstrap_confidence must be a finite number in [0, 1]"
        )
    scaled = Decimal(str(value)) * _PPM_SCALE
    if scaled != scaled.to_integral_value():
        raise ConfigError(
            f"{path}: bootstrap_confidence must be representable "
            "as an exact ppm integer"
        )
    ppm = int(scaled)
    if not 0 <= ppm <= _PPM_SCALE:
        raise ConfigError(f"{path}: bootstrap_confidence must be between 0 and 1")
    return ppm


#: Registry mapping a schema field's ``convert`` tag to its converter.
_CONVERTERS = {"confidence_to_ppm": confidence_to_ppm}


def _is_union(origin: object) -> bool:
    """Return whether a type origin denotes an ``X | Y`` union."""
    return origin is types.UnionType


def _first_non_none(args: tuple[object, ...]) -> object:
    """Return the first non-``NoneType`` member of a union's arguments."""
    return next(arg for arg in args if arg is not type(None))


def _matches_scalar(hint: object, value: object) -> bool:
    """Return whether ``value`` satisfies a scalar ``hint`` (str/int/bool).

    Booleans are rejected where an integer is expected, since ``bool`` is a
    subclass of ``int`` and would otherwise slip through unchecked.
    """
    if hint is bool:
        return isinstance(value, bool)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, str)


def _coerce_scalar(hint: object, value: object, path: str) -> object:
    """Validate a scalar ``value`` against ``hint`` or raise ``ConfigError``."""
    if _matches_scalar(hint, value):
        return value
    _type_error(_SCALAR_NAMES[hint], value, path)


def _coerce_tuple(hint: object, value: object, path: str) -> object:
    """Coerce a YAML list into a homogeneous tuple of the element type."""
    if not isinstance(value, list | tuple):
        _type_error("a list", value, path)
    item_type = get_args(hint)[0]
    return tuple(
        _coerce_value(item_type, item, _child_path(path, str(index)))
        for index, item in enumerate(value)
    )


def _coerce_optional(hint: object, value: object, path: str) -> object:
    """Coerce a value for an ``X | None`` field, allowing an explicit ``None``."""
    if value is None:
        return None
    return _coerce_value(_first_non_none(get_args(hint)), value, path)


def _coerce_terminal(hint: object, value: object, path: str) -> object:
    """Coerce a value whose type has no generic origin (dataclass or scalar)."""
    if dataclasses.is_dataclass(hint):
        return _build_mapping(cast("type[Any]", hint), value, path)
    return _coerce_scalar(hint, value, path)


def _coerce_value(hint: object, value: object, path: str) -> object:
    """Coerce ``value`` to the resolved field type ``hint`` at ``path``."""
    origin = get_origin(hint)
    if origin is None:
        return _coerce_terminal(hint, value, path)
    if _is_union(origin):
        return _coerce_optional(hint, value, path)
    return _coerce_tuple(hint, value, path)


def _build_mapping(cls: type[Any], value: object, path: str) -> object:
    """Require ``value`` to be a mapping and build a nested dataclass from it."""
    if not isinstance(value, Mapping):
        _type_error("a mapping", value, path)
    return _build(cls, value, path)


def _yaml_key(field_def: dataclasses.Field[object]) -> str:
    """Return the YAML key a field reads from (its alias, else its name)."""
    return str(field_def.metadata.get(_YAML_KEY, field_def.name))


def _child_path(prefix: str, key: str) -> str:
    """Append ``key`` to a dotted ``prefix``, or start a fresh path."""
    return f"{prefix}.{key}" if prefix else key


def _coerce_field(
    field_def: dataclasses.Field[object], hint: object, value: object, path: str
) -> object:
    """Coerce one field's raw value, applying its converter if it declares one."""
    converter_key = field_def.metadata.get(_CONVERT)
    if converter_key is not None:
        return _CONVERTERS[str(converter_key)](value, path)
    return _coerce_value(hint, value, path)


def _reject_unknown_keys(raw: Mapping[str, object], known: set[str], path: str) -> None:
    """Raise ``ConfigError`` if ``raw`` contains any key outside ``known``."""
    unknown = sorted(set(raw) - known)
    if not unknown:
        return
    offending = ", ".join(_child_path(path, key) for key in unknown)
    raise ConfigError(
        f"unknown configuration key(s): {offending} "
        "- unknown keys are fatal per SPEC §16"
    )


def _build(cls: type[Any], raw: Mapping[str, object], path: str) -> Any:
    """Build a dataclass of type ``cls`` from a raw mapping at ``path``.

    Args:
        cls: The dataclass type to construct.
        raw: The mapping of YAML keys to raw values for this section.
        path: The dotted path to this section, for error messages.

    Returns:
        A ``cls`` instance with every present key coerced and validated and
        every absent key left to its schema default.

    Raises:
        ConfigError: On unknown keys or any leaf-value type mismatch.
    """
    fields_by_key = {_yaml_key(f): f for f in dataclasses.fields(cls)}
    _reject_unknown_keys(raw, set(fields_by_key), path)
    hints = get_type_hints(cls)
    kwargs = {
        field_def.name: _coerce_field(
            field_def, hints[field_def.name], raw[key], _child_path(path, key)
        )
        for key, field_def in fields_by_key.items()
        if key in raw
    }
    return cls(**kwargs)


def _read_text(path: str | Path) -> str:
    """Read a UTF-8 text file, or raise ``ConfigError`` if it cannot be read.

    Args:
        path: The filesystem path to read.

    Returns:
        The file's decoded text.

    Raises:
        ConfigError: If the file cannot be opened, read, or decoded as UTF-8.
    """
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read configuration file: {path}") from exc


def _parse_mapping(text: str, path: str | Path) -> Mapping[str, object]:
    """Parse YAML ``text`` into a root mapping, or raise ``ConfigError``.

    Args:
        text: The raw YAML document text.
        path: The originating filesystem path, for error messages.

    Returns:
        The parsed root mapping.

    Raises:
        ConfigError: If ``text`` is malformed YAML or its root is not a mapping.
    """
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in configuration file: {path}") from exc
    if not isinstance(parsed, Mapping):
        raise _NonMappingRootError()
    return parsed


def _read_yaml(path: str | Path) -> Mapping[str, object]:
    """Read and parse a YAML file, requiring a mapping at its root.

    Args:
        path: The filesystem path to the YAML configuration.

    Returns:
        The parsed root mapping.

    Raises:
        ConfigError: If the file is unreadable, malformed, or not a mapping.
    """
    return _parse_mapping(_read_text(path), path)


def _record(
    config: WindbreakConfig, source: str, recorder: ConfigEventRecorder | None
) -> None:
    """Notify ``recorder``, if any, of a completed configuration load."""
    if recorder is None:
        return
    recorder.record_config_loaded(
        config_hash=config_hash(config),
        diff=diff_configs(WindbreakConfig(), config),
        source=source,
    )


def load_config(
    path: str | Path, *, recorder: ConfigEventRecorder | None = None
) -> WindbreakConfig:
    """Load and validate a SPEC §16 configuration from a YAML file.

    Args:
        path: The filesystem path to the YAML configuration.
        recorder: Optional sink notified of the resulting hash and diff.

    Returns:
        The fully-typed, immutable configuration.

    Raises:
        ConfigError: If the file cannot be read, parsed, or validated.
    """
    raw = _read_yaml(path)
    config: WindbreakConfig = _build(WindbreakConfig, raw, "")
    _record(config, str(path), recorder)
    return config


def load_default_config(
    *, recorder: ConfigEventRecorder | None = None
) -> WindbreakConfig:
    """Return the built-in SPEC §16 default configuration.

    Args:
        recorder: Optional sink notified of the resulting hash and diff.

    Returns:
        The default configuration, identical to ``WindbreakConfig()``.
    """
    config = WindbreakConfig()
    _record(config, _DEFAULTS_SOURCE, recorder)
    return config
