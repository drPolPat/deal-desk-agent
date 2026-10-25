"""Append-only audit log and review queue storage: JSONL locally, BigQuery on Cloud Run."""

import json
import logging
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)


class AuditStore(Protocol):
    def append(self, table: str, row: dict[str, Any]) -> None: ...
    def read(self, table: str) -> list[dict[str, Any]]: ...


def make_store(settings: Any) -> "AuditStore":
    if settings.gcp_project and settings.bq_dataset:
        return BigQueryAuditStore(settings.gcp_project, settings.bq_dataset)
    return JsonlAuditStore(settings.audit_dir)


class JsonlAuditStore:
    """One JSON line per row in <dir>/<table>.jsonl."""

    def __init__(self, directory: str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def append(self, table: str, row: dict[str, Any]) -> None:
        with (self.dir / f"{table}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def read(self, table: str) -> list[dict[str, Any]]:
        path = self.dir / f"{table}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# Typed columns worth querying directly; every row is also stored whole in the `data` JSON column,
# so audit fields can be added without a schema migration.
COLUMNS: dict[str, dict[str, str]] = {
    "decisions": {"run_id": "STRING", "opportunity_id": "STRING", "decision": "STRING",
                  "status": "STRING", "confidence": "FLOAT", "amount": "FLOAT", "model": "STRING",
                  "prompt_version": "STRING", "cost_usd": "FLOAT", "latency_ms": "INTEGER",
                  "task_id": "STRING", "dry_run": "BOOLEAN"},
    "runs": {"run_id": "STRING", "model": "STRING", "prompt_version": "STRING", "dry_run": "BOOLEAN",
             "opportunities": "INTEGER", "auto_write": "INTEGER", "human_review": "INTEGER",
             "cost_usd": "FLOAT", "stopped_early": "STRING"},
    "reviews": {"review_id": "STRING", "run_id": "STRING", "opportunity_id": "STRING",
                "action": "STRING", "reviewer": "STRING", "task_id": "STRING"},
    # Writes made through the MCP server's create_task tool (outside agent runs).
    "tool_writes": {"write_id": "STRING", "source": "STRING", "opportunity_id": "STRING",
                    "task_id": "STRING", "dry_run": "BOOLEAN"},
}
ROW_KEY = {"decisions": ("run_id", "opportunity_id"), "runs": ("run_id",), "reviews": ("review_id",),
           "tool_writes": ("write_id",)}


class BigQueryAuditStore:
    def __init__(self, project: str, dataset: str):
        from google.cloud import bigquery  # imported here so local runs don't need GCP credentials

        self.bq = bigquery
        self.client = bigquery.Client(project=project)
        self.dataset = f"{project}.{dataset}"
        for table, cols in COLUMNS.items():
            schema = [bigquery.SchemaField("ts", "TIMESTAMP", mode="REQUIRED"),
                      *[bigquery.SchemaField(c, t) for c, t in cols.items()],
                      bigquery.SchemaField("data", "JSON")]
            t = bigquery.Table(f"{self.dataset}.{table}", schema=schema)
            t.time_partitioning = bigquery.TimePartitioning(field="ts")
            self.client.create_table(t, exists_ok=True)

    def append(self, table: str, row: dict[str, Any]) -> None:
        # shortcut: legacy streaming inserts (simple, ~$0.01 per 200 MB). Rows can't be UPDATEd while
        # in the streaming buffer, which is fine because every table here is append-only. Move to
        # the Storage Write API if volume grows.
        bq_row = {"ts": row.get("ts") or row.get("started"), **{c: row.get(c) for c in COLUMNS[table]},
                  "data": json.dumps(row, default=str)}
        try:
            errors = self.client.insert_rows_json(
                f"{self.dataset}.{table}", [bq_row], row_ids=[":".join(str(row[k]) for k in ROW_KEY[table])])
        except Exception as exc:  # noqa: BLE001
            errors = [repr(exc)]
        if errors:
            # Never lose an audit record: the full row goes to Cloud Logging, the run continues.
            log.critical("audit write failed",
                         extra={"fields": {"table": table, "errors": errors, "row": row}})

    def read(self, table: str) -> list[dict[str, Any]]:
        # shortcut: full table scan; fine at demo volume. Push the review-queue filter into SQL
        # (see review.pending) once the tables hold more than a few thousand rows.
        rows = self.client.query(f"SELECT data FROM `{self.dataset}.{table}` ORDER BY ts").result()  # noqa: S608
        return [r["data"] if isinstance(r["data"], dict) else json.loads(r["data"]) for r in rows]
