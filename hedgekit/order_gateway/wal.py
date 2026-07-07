"""Durable write-ahead intent log for crash recovery (issue #40, SPEC S11.4).

Before the Order Gateway takes any externally-visible action for an intent -- and
the instant a placement's ack comes back -- it journals a durable record here, so
a crash at any point along ``APPROVE -> REQUEST_SUBMISSION -> (place) -> SUBMIT
-> ACK`` leaves a fresh Gateway's :meth:`~hedgekit.order_gateway.gateway.
OrderGateway.recover` enough durable truth to reconstruct what happened without
ever double-submitting.

The log is an append-only JSONL file: one :func:`~hedgekit.ledger.events.
canonical_json` line per record, ``flush``ed and ``os.fsync``ed on every append
so a record survives a crash the moment the append returns. Two record kinds
share one :class:`WalRecord` shape:

    * an *intent* record -- the full nine :class:`~hedgekit.riskkernel.checks.
      OrderIntent` fields (the four scaled-int money-path fields as their bare
      ``.value`` ints, SPEC S6.1), written *before* the ``REQUEST_SUBMISSION``
      transition. The signed token and any key material are **never** written.
    * an *ack* record -- the venue-order-id / ``client_order_id`` correlation and
      the immediately-filled quantity, written the instant ``place`` returns.

:meth:`WriteAheadLog.read_all` reconstructs each ``OrderIntent`` from ints only
and re-derives its :func:`~hedgekit.order_gateway.client_order_id.client_order_id`,
failing loudly if the re-derived id disagrees with the recorded one (a tampered
or corrupt journal must never silently mis-attribute an order).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from hedgekit.ledger.events import canonical_json
from hedgekit.numeric.types import (
    ContractCentis,
    MoneyMicros,
    PricePips,
    ProbabilityPpm,
)
from hedgekit.order_gateway.client_order_id import client_order_id
from hedgekit.riskkernel.checks import OrderIntent

if TYPE_CHECKING:
    from pathlib import Path

#: Discriminator value marking a journalled intent record.
_KIND_INTENT = "intent"

#: Discriminator value marking a journalled ack record.
_KIND_ACK = "ack"


@dataclass(frozen=True, slots=True)
class WalRecord:
    """One journalled write-ahead record (an intent or an ack).

    A single shape carries both kinds; only the fields relevant to ``kind`` are
    populated (the rest carry inert sentinels), so a caller filters on ``kind``
    before reading ``intent`` (intent records) or ``order_id``/``filled`` (ack
    records).

    Attributes:
        kind: Either ``"intent"`` or ``"ack"``.
        client_order_id: The content-addressed id the record belongs to.
        intent: The journalled :class:`~hedgekit.riskkernel.checks.OrderIntent`
            on an intent record, else ``None``.
        order_id: The venue's resting-order id on an ack record (``None`` when
            the placement left nothing resting); always ``None`` on an intent
            record.
        filled: The quantity filled immediately on an ack record, in
            contract-centis; ``ContractCentis(0)`` on an intent record.
    """

    kind: str
    client_order_id: str
    intent: OrderIntent | None
    order_id: str | None
    filled: ContractCentis


class WriteAheadLogProtocol(Protocol):
    """The structural seam the Gateway durably journals intents and acks through.

    Mirrors :class:`~hedgekit.order_gateway.ledger_writer.GatewayLedgerWriter`'s
    protocol-first design so a crash-simulating test wrapper (or any alternative
    durable log) can stand in for the real :class:`WriteAheadLog` while staying
    ``mypy --strict`` clean. Parameters are positional-only so an implementer may
    name them freely.
    """

    def append_intent(self, intent: OrderIntent, client_order_id: str, /) -> None:
        """Durably journal ``intent`` before the Gateway acts on it.

        Args:
            intent: The order intent to journal.
            client_order_id: The intent's content-addressed id.
        """
        ...

    def append_ack(
        self,
        client_order_id: str,
        order_id: str | None,
        filled: ContractCentis,
        /,
    ) -> None:
        """Durably journal a placement's ack the instant ``place`` returns.

        Args:
            client_order_id: The intent's content-addressed id.
            order_id: The venue's resting-order id, or ``None`` when nothing
                rested.
            filled: The quantity filled immediately, in contract-centis.
        """
        ...

    def read_all(self) -> tuple[WalRecord, ...]:
        """Return every journalled record, in append order.

        Returns:
            The journalled records, oldest first.
        """
        ...


class WriteAheadLog:
    """An append-only, ``fsync``-durable JSONL write-ahead log (issue #40)."""

    def __init__(self, path: Path) -> None:
        """Bind the log to its JSONL file (created lazily on first append).

        Args:
            path: Filesystem path to the append-only JSONL journal.
        """
        self._path = path

    def append_intent(self, intent: OrderIntent, client_order_id_: str) -> None:
        """Durably journal an intent record.

        Args:
            intent: The order intent to journal, serialized as ints and strings
                only (never the token or any key material).
            client_order_id_: The intent's content-addressed id.
        """
        self._append(
            {
                "kind": _KIND_INTENT,
                "client_order_id": client_order_id_,
                "intent": _intent_to_payload(intent),
            }
        )

    def append_ack(
        self, client_order_id_: str, order_id: str | None, filled: ContractCentis
    ) -> None:
        """Durably journal an ack record.

        Args:
            client_order_id_: The intent's content-addressed id.
            order_id: The venue's resting-order id, or ``None`` when nothing
                rested.
            filled: The quantity filled immediately, in contract-centis.
        """
        self._append(
            {
                "kind": _KIND_ACK,
                "client_order_id": client_order_id_,
                "order_id": order_id,
                "filled": filled.value,
            }
        )

    def _append(self, obj: dict[str, object]) -> None:
        """Append one canonical-JSON line, flushing and ``fsync``ing it durable.

        On the append that first creates the journal file, the parent directory
        is ``fsync``ed too, so the new directory entry survives a crash in the
        window between file creation and the OS flushing that entry -- otherwise
        a just-created log could vanish despite its data being ``fsync``ed.

        Args:
            obj: The record mapping to serialize as a single JSONL line.
        """
        line = canonical_json(obj)
        is_new_file = not self._path.exists()
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if is_new_file:
            self._fsync_parent_dir()

    def _fsync_parent_dir(self) -> None:
        """``fsync`` the journal's parent directory to persist a new file entry."""
        dir_fd = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def read_all(self) -> tuple[WalRecord, ...]:
        """Reconstruct every journalled record, verifying each intent's id.

        Returns:
            The journalled records, oldest first; an empty tuple when the
            journal file does not yet exist.

        Raises:
            ValueError: If a journalled intent's re-derived
                :func:`~hedgekit.order_gateway.client_order_id.client_order_id`
                disagrees with the id it was recorded under (a corrupt journal).
        """
        if not self._path.exists():
            return ()
        text = self._path.read_text(encoding="utf-8")
        records = [self._record_from_line(line) for line in text.splitlines() if line]
        return tuple(records)

    def _record_from_line(self, line: str) -> WalRecord:
        """Parse one JSONL line into a :class:`WalRecord`.

        Args:
            line: One canonical-JSON journal line.

        Returns:
            The reconstructed record.

        Raises:
            ValueError: If an intent line's re-derived id disagrees with its
                recorded ``client_order_id``.
        """
        obj = cast("dict[str, object]", json.loads(line))
        coid = cast("str", obj["client_order_id"])
        if obj["kind"] == _KIND_INTENT:
            return self._intent_record(coid, cast("dict[str, object]", obj["intent"]))
        order_id = cast("str | None", obj["order_id"])
        filled = ContractCentis(cast("int", obj["filled"]))
        return WalRecord(
            kind=_KIND_ACK,
            client_order_id=coid,
            intent=None,
            order_id=order_id,
            filled=filled,
        )

    def _intent_record(self, coid: str, payload: dict[str, object]) -> WalRecord:
        """Rebuild an intent record and verify its content-addressed id.

        Args:
            coid: The id the intent was recorded under.
            payload: The journalled intent payload (ints and strings only).

        Returns:
            The reconstructed intent :class:`WalRecord`.

        Raises:
            ValueError: If ``client_order_id(intent)`` disagrees with ``coid``.
        """
        intent = _intent_from_payload(payload)
        rederived = client_order_id(intent)
        if rederived != coid:
            raise ValueError(
                f"write-ahead log corrupt: intent re-derives client_order_id "
                f"{rederived!r} but was journalled under {coid!r}"
            )
        return WalRecord(
            kind=_KIND_INTENT,
            client_order_id=coid,
            intent=intent,
            order_id=None,
            filled=ContractCentis(0),
        )


def _intent_to_payload(intent: OrderIntent) -> dict[str, object]:
    """Project an ``OrderIntent`` into its JSON-safe, float-free journal payload.

    Args:
        intent: The order intent to serialize.

    Returns:
        The nine intent fields, with every scaled-int money-path field rendered
        as its bare ``.value`` integer (SPEC S6.1).
    """
    return {
        "intent_id": intent.intent_id,
        "market_ticker": intent.market_ticker,
        "outcome": intent.outcome,
        "action": intent.action,
        "price": intent.price.value,
        "size": intent.size.value,
        "max_notional": intent.max_notional.value,
        "implied_probability": intent.implied_probability.value,
        "idempotency_key": intent.idempotency_key,
    }


def _intent_from_payload(payload: dict[str, object]) -> OrderIntent:
    """Rebuild an ``OrderIntent`` from a journal payload, ints only.

    Args:
        payload: The journalled intent payload.

    Returns:
        The reconstructed :class:`~hedgekit.riskkernel.checks.OrderIntent`, its
        scaled-int fields rewrapped from their integer ``.value`` (never a
        float).
    """
    return OrderIntent(
        intent_id=cast("str", payload["intent_id"]),
        market_ticker=cast("str", payload["market_ticker"]),
        outcome=cast("str", payload["outcome"]),
        action=cast("str", payload["action"]),
        price=PricePips(cast("int", payload["price"])),
        size=ContractCentis(cast("int", payload["size"])),
        max_notional=MoneyMicros(cast("int", payload["max_notional"])),
        implied_probability=ProbabilityPpm(cast("int", payload["implied_probability"])),
        idempotency_key=cast("str", payload["idempotency_key"]),
    )
