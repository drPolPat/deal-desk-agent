import json
from pathlib import Path
from typing import Any, Protocol


class AuditStore(Protocol):
    def append(self, table: str, row: dict[str, Any]) -> None: ...


class JsonlAuditStore:
    """Append-only local audit log: one JSON line per row in <dir>/<table>.jsonl."""

    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def append(self, table: str, row: dict[str, Any]) -> None:
        with (self.dir / f"{table}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
