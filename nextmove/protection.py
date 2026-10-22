"""Human-edit protection: never overwrite what a human changed after us.

The rule is small and mechanical:

* every time this tool writes to a task page it records *when* it wrote
  (``lastWriteAt`` in the local ledger);
* before a write that would change a human-visible field (a status change on
  completion), the page's Notion ``last_edited_time`` is compared with that
  record;
* if the page was edited **after** the tool's last write, the write is
  refused and reported as ``protected`` — the human's edit wins, always.

Additive writes (appending an evidence paragraph) do not destroy a human's
edit, so they are still allowed; protection covers the field-changing write.

When the comparison cannot be made — no ``last_edited_time`` on the page, or
a timestamp this code cannot parse — the page is treated as **protected**:
absence of evidence that we may write is not evidence that we may.
"""
from __future__ import annotations

from datetime import datetime, timezone


def parse_iso(value):
    """Parse a Notion/ISO-8601 timestamp; ``None`` when it cannot be parsed.

    Handles a trailing ``Z`` (UTC) and numeric offsets. A value without an
    offset is read as UTC — Notion always emits one, so this only affects
    hand-made fixtures, and being wrong there fails safe (see
    :func:`human_touched`).
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def page_edited_at(page):
    return (page or {}).get("last_edited_time")


def agent_write_at(record):
    return (record or {}).get("lastWriteAt")


def human_touched(page_timestamp, agent_timestamp):
    """True when the page was edited after the tool's last recorded write.

    Unknown page timestamp -> False (nothing to compare, and the write path
    that calls this only runs on rows this tool has written before).
    Unknown agent timestamp -> True: we have no proof we may overwrite.
    Unparseable timestamps -> True (fails safe).
    """
    if not page_timestamp:
        return False
    page_dt = parse_iso(page_timestamp)
    if page_dt is None:
        return True
    if not agent_timestamp:
        return True
    agent_dt = parse_iso(agent_timestamp)
    if agent_dt is None:
        return True
    return page_dt > agent_dt


def later_of(left, right):
    """The later of two ISO timestamps; ``None`` inputs are ignored.

    After a write the tool records ``max(our clock, Notion's last_edited)``:
    Notion stamps server-side, so its timestamp can be a second or two ahead
    of the local clock. Recording only our own clock would make the *next*
    run read the row as human-edited and refuse legitimate updates.
    """
    pair = [value for value in (left, right) if value]
    if not pair:
        return left or right
    known = []
    for value in pair:
        parsed = parse_iso(value)
        if parsed is not None:
            known.append((parsed, value))
    if not known:
        return max(pair)
    return max(known, key=lambda item: item[0])[1]


def decide(page, record, policy=None):
    """Decide whether a field-changing write on ``page`` is allowed."""
    if ((policy or {}).get("protect_human_edits") is False):
        return {"protected": False, "reason": "human-edit protection disabled",
                "checked": False}
    page_ts = page_edited_at(page)
    agent_ts = agent_write_at(record)
    touched = human_touched(page_ts, agent_ts)
    return {
        "protected": touched,
        "checked": True,
        "pageEditedAt": page_ts,
        "agentWroteAt": agent_ts,
        "reason": ("page was edited by a human after the tool's last write "
                   f"({page_ts} > {agent_ts})" if touched else
                   "no human edit after the tool's last write"),
    }
