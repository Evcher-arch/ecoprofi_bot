"""Итоговое состояние материала (specs/003-autonomous-indexer/data-model.md).

Вычисляется для сводки, отчёта и оповещений; отдельно не хранится.
"""
from ecoprofi.bot.access import MATERIAL_AUDIENCE
from ecoprofi.textkeys import material_key

STATES = ("published", "waiting_source", "editor", "unsupported", "permanent", "deferred")
TITLES = {
    "published": "опубликован", "waiting_source": "ждёт источника", "editor": "ждёт решения редактора",
    "unsupported": "формат не поддерживается", "permanent": "постоянный сбой", "deferred": "отложен",
}

_JOBS = f"""
    SELECT j.item_type, j.owner_id, j.item_id, j.post_id, j.state AS job_state, j.failure_reason,
           j.failed_runs, j.waiting_source, j.retry_at,
           a.id AS material_id, a.state AS material_state, a.audience, a.decision, a.signal, a.seen_signal,
           a.published_version_id
    FROM index_job j
    LEFT JOIN ({MATERIAL_AUDIENCE}) a
           ON a.item_type = j.item_type AND a.owner_id = j.owner_id AND a.item_id = j.item_id
    ORDER BY j.item_type, j.owner_id, j.item_id
"""


def classify(row) -> str | None:
    """Итоговое состояние по строке задания. None — материал удалён у владельца."""
    if row["failure_reason"] == "deleted" or row["material_state"] == "deleted":
        return None
    if row["job_state"] == "permanent":
        return "permanent"
    if row["failure_reason"] == "unsupported":
        return "unsupported"
    if row["job_state"] != "done" or row["published_version_id"] is None:
        return "deferred"
    if row["waiting_source"]:
        return "waiting_source"
    conflict = (row["decision"] and row["signal"] and row["decision"] != row["signal"]
                and row["seen_signal"] != row["signal"])
    if row["material_state"] == "review" or row["audience"] in (None, "unknown") or conflict:
        return "editor"
    return "published"


def material_outcomes(conn) -> dict:
    """Счётчики и ключи материалов по итоговым состояниям."""
    counts = {state: 0 for state in STATES}
    items: dict[str, list] = {state: [] for state in STATES}
    for row in conn.execute(_JOBS).fetchall():
        state = classify(row)
        if state is None:
            continue
        counts[state] += 1
        items[state].append({"key": material_key(row["item_type"], row["owner_id"], row["item_id"]),
                             "post_id": row["post_id"], "reason": row["failure_reason"],
                             "failed_runs": row["failed_runs"], "retry_at": row["retry_at"]})
    return {"counts": counts, "items": items, "total": sum(counts.values())}
