"""Local state: checkpoints, the task ledger and the archived-message ledger.

The state file is plain JSON you can read and diff. It is the tool's memory
across runs; Notion stays the visible record. Writes are atomic (temp file +
replace) and every mutation reports what changed.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

SCHEMA_VERSION = 1


def read(path):
    path = Path(path)
    if not path.exists():
        return {
            "schemaVersion": SCHEMA_VERSION,
            "mailbox": {"account": None},
            "folders": {},
            "archived": [],
            "tasks": {},
            "pending": [],
            "runs": [],
        }
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def folder_checkpoint(state, folder, uid_validity):
    """Fetch or initialize the checkpoint for one folder."""
    entry = state["folders"].setdefault(folder, {
        "lastUid": 0, "lastUidValidity": None, "lastMessageId": None,
        "checkedAt": None, "coverageProven": False,
    })
    if entry.get("lastUidValidity") is None:
        entry["lastUidValidity"] = str(uid_validity)
    return entry


def advance_checkpoint(state, folder, headers, uid_validity, anchor_header=None):
    """Advance one folder's checkpoint after its work is fully verified.

    Only called when coverage was proven AND every new message in the folder
    was fully processed. Advances to the newest observed header (or the
    caller-supplied anchor) and records its Message-ID for re-anchoring.
    """
    entry = state["folders"][folder]
    if anchor_header is None:
        if not headers:
            entry["checkedAt"] = _now()
            entry["coverageProven"] = True
            return entry
        anchor_header = max(headers, key=lambda h: int(h["uid"]))
    entry.update({
        "lastUid": int(anchor_header["uid"]),
        "lastMessageId": anchor_header.get("message_id"),
        "lastUidValidity": str(uid_validity),
        "lastDate": anchor_header.get("date_header"),
        "checkedAt": _now(),
        "coverageProven": True,
        "anchorInclusive": False,
    })
    return entry


def keep_checkpoint(state, folder, reason):
    entry = state["folders"].setdefault(folder, {})
    entry["checkedAt"] = _now()
    entry["coverageProven"] = False
    entry["lastGap"] = reason
    return entry


def record_archive(state, message_id, customer_key, page_id, locator, body_sha256=None):
    """Record that one original message is archived under a customer page.

    The stored id stays exactly as it appeared in the mail; comparisons
    elsewhere normalize both sides (never the stored original).
    """
    for item in state["archived"]:
        if normalize_id(item.get("message_id")) == normalize_id(message_id):
            return item
    item = {
        "message_id": message_id,
        "customer_key": customer_key,
        "page_id": page_id,
        "locator": locator,
        "body_sha256": body_sha256,
        "archivedAt": _now(),
    }
    state["archived"].append(item)
    return item


def normalize_id(value):
    """Fold a Message-ID for comparison; domain lowercased, local part kept."""
    import re
    value = re.sub(r"\s+", "", str(value or "")).strip("<>")
    local, sep, domain = value.rpartition("@")
    return local + sep + domain.lower() if sep else value


def archived_index(state):
    """Map normalized Message-ID -> archive record for dedupe decisions.

    Keys are normalized so a header read back from the server matches the
    stored original regardless of angle brackets or domain case.
    """
    out = {}
    for item in state.get("archived", []):
        ident = normalize_id(item.get("message_id"))
        if ident:
            out[ident] = item
    return out


def upsert_task(state, identity, record):
    """Add or update one ledger entry for a task, keyed by its stable identity."""
    state["tasks"][identity] = {**state["tasks"].get(identity, {}), **record,
                                "updatedAt": _now()}
    return state["tasks"][identity]


def add_pending(state, entry):
    """Queue one item for human review (unknown sender, no customer mapping)."""
    key = (entry.get("message_id") or entry.get("uid"))
    for item in state["pending"]:
        if (item.get("message_id") or item.get("uid")) == key:
            return item
    entry = {**entry, "queuedAt": _now(), "status": entry.get("status", "needs_review")}
    state["pending"].append(entry)
    return entry


def resolve_pending(state, message_id):
    before = len(state["pending"])
    state["pending"] = [item for item in state["pending"]
                        if item.get("message_id") != message_id]
    return before != len(state["pending"])


def append_run(state, record):
    state.setdefault("runs", []).append(record)
    state["runs"] = state["runs"][-60:]


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
