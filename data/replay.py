"""Append-only raw stream recording for deterministic replay fixtures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RawStreamRecord:
    stream: str
    received_at: datetime
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "stream": self.stream,
            "received_at": self.received_at.astimezone(UTC).isoformat(),
            "payload": self.payload,
        }


class JsonlReplayRecorder:
    """Write one complete, parseable record per line without mutating prior records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, stream: str, payload: dict[str, Any], *, received_at: datetime) -> None:
        record = RawStreamRecord(stream, received_at, payload)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.as_dict(), sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    def read(self) -> tuple[RawStreamRecord, ...]:
        if not self.path.exists():
            return ()
        records: list[RawStreamRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            records.append(
                RawStreamRecord(
                    stream=value["stream"],
                    received_at=datetime.fromisoformat(value["received_at"]),
                    payload=value["payload"],
                )
            )
        return tuple(records)
