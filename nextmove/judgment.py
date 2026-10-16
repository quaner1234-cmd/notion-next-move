"""Judgment channel: business judgement belongs to the user's own agent.

This tool never decides what a customer needs. It splits the work:

* **Request** — after a scan, it writes ``state/judgment-request.json``: the
  new mail (with previews and evidence locators), the customer's open tasks,
  and the exact rules. Any agent (or the user, in an editor) can read it.
* **Judgment** — the agent writes ``state/judgment.json``: for each message
  and task, what to do next (create / complete / append / hold), with its own
  wording.
* **Validation** — every decision is checked mechanically before anything is
  written. A decision cannot invent a message, a task, a customer, or an
  evidence locator: each must trace back to the request that was actually
  produced from the mailbox. The Python side owns this boundary.

No provider, model or vendor is assumed. The contract is two JSON files.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import plan as plan_module

REQUEST_KIND = "judgment_request"
JUDGMENT_KIND = "judgment"

MAX_TITLE = 200
MAX_TEXT = 4000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Fields each decision type may carry. Anything else is rejected outright:
# an unapproved field is almost always a sign of a different tool's output
# being pasted in, and silently ignoring it would hide the mistake.
DECISION_FIELDS = {
    "create": {"decision", "customer_key", "message_id", "title", "action",
               "completion_standard", "due_date", "internal_review",
               "evidence", "note", "generatedBy"},
    "complete": {"decision", "identity", "evidence", "source_locator",
                 "evidence_date", "message_id", "note", "generatedBy"},
    "append": {"decision", "identity", "message_id", "evidence", "note",
               "generatedBy"},
    "hold": {"decision", "message_id", "reason", "generatedBy"},
}


class JudgmentError(Exception):
    """The judgment document itself is unusable."""


def request_id(request):
    """Stable id for a request: derived from the message set it covers.

    Same messages in scope = same id, so a judgment written after a scan stays
    valid until the message set actually changes; a new message makes it
    stale, and the tool asks for a re-judge instead of applying a decision
    made without seeing the new mail.
    """
    material = "|".join(sorted(
        plan_module.normalize_message_id(m.get("message_id"))
        for m in request.get("messages", []) if m.get("message_id")))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def build_instructions(config):
    return {
        "purpose": "Decide the next action for each message/task, in your own "
                   "words. This tool validates and persists your decisions; it "
                   "does not make business judgements itself.",
        "write_to": "state/judgment.json (same directory as this request)",
        "must_include": "kind=judgment, basedOn=<this request's requestId>, "
                        "decisions=[...]",
        "decision_types": {
            "create": "a new next-move task for one inbound message "
                      "(title/action/completion_standard in your words)",
            "complete": "an open task whose real outcome is now proven "
                        "(evidence text + locator + date or message id)",
            "append": "new evidence for an existing open task, no new task",
            "hold": "deliberately do nothing for a message, with a reason",
        },
        "rules": [
            "Reference only message_id / identity values that appear in this request.",
            "create: customer_key must match the message's customer in this request; "
            "the evidence locator is taken from the archived original, not from you.",
            "complete: only when the outcome is actually evidenced; state the evidence.",
            "Never invent a date, amount, approval or commitment.",
            "One action per customer per advance; do not create a task for a "
            "courtesy message or pure waiting.",
        ],
        "then_run": "python -m nextmove apply --judgment state/judgment.json "
                    "(or simply let the next scheduled run pick it up)",
    }


def validate(judgment_doc, request, config, state):
    """Validate a judgment document against the request it claims to be based on.

    Returns ``{"ok": bool, "error": str|None, "decisions": [...]}`` where each
    decision carries ``status`` (valid/rejected) and ``problems``. A malformed
    document fails as a whole (``ok=False``, nothing applies); an individual
    bad decision is rejected while the rest may still apply.

    Staleness: the *queue* is the idempotency guard. A decision is rejected
    when its message is no longer in the request (already handled, or never
    seen) or its task is not in local state — so a judgment written for an
    earlier request still lands whatever it legitimately covers, and can
    never re-apply something already handled.
    """
    result = {"ok": False, "error": None, "decisions": []}
    if not isinstance(judgment_doc, dict):
        result["error"] = "judgment must be a JSON object"
        return result
    kind = judgment_doc.get("kind")
    if kind not in (None, JUDGMENT_KIND):
        result["error"] = f"unexpected kind: {kind!r} (expected {JUDGMENT_KIND!r})"
        return result
    if not str(judgment_doc.get("basedOn") or "").strip():
        result["error"] = ("judgment has no basedOn — it must name the request it "
                           "was written against (see state/judgment-request.json)")
        return result
    decisions = judgment_doc.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        result["error"] = "judgment has no decisions"
        return result
    messages = {plan_module.normalize_message_id(m.get("message_id")): m
                for m in request.get("messages", []) if m.get("message_id")}
    tasks = {t.get("identity"): t for t in request.get("open_tasks", []) if t.get("identity")}
    archived = state.get("archived", [])
    archived_ids = set()
    for item in archived:
        ident = plan_module.normalize_message_id(item.get("message_id"))
        if ident:
            archived_ids.add(ident)
    ledger = state.get("tasks", {})
    result["ok"] = True
    for index, decision in enumerate(decisions):
        entry = _validate_one(index, decision, messages, tasks, archived_ids, ledger, config)
        # A create decision may reference the message; attach the *request's*
        # facts (locator, date, sender) to the payload so the writer never
        # takes those from the judgment document.
        message = entry.pop("_message", None)
        if entry["status"] == "valid" and message:
            entry["payload"]["message"] = {
                "message_id": message.get("message_id"),
                "date": message.get("date"),
                "from": message.get("from"),
                "subject": message.get("subject"),
                "locator": message.get("locator"),
                "customer_display": message.get("customer_display"),
            }
        result["decisions"].append(entry)
    if not any(d["status"] == "valid" for d in result["decisions"]):
        result["ok"] = False
        result["error"] = "no valid decisions in the judgment"
    return result


def _validate_one(index, decision, messages, tasks, archived_ids, ledger, config):
    entry = {"index": index, "status": "rejected", "problems": []}
    if not isinstance(decision, dict):
        entry["problems"].append("decision must be an object")
        return entry
    kind = decision.get("decision")
    entry["decision"] = kind
    if kind not in DECISION_FIELDS:
        entry["problems"].append(f"unknown decision type: {kind!r}")
        return entry
    allowed = DECISION_FIELDS[kind]
    extra = set(decision) - allowed
    if extra:
        entry["problems"].append(f"unapproved fields: {sorted(extra)}")
        return entry
    if kind == "create":
        _validate_create(entry, decision, messages, config)
    elif kind == "complete":
        _validate_complete(entry, decision, tasks, archived_ids, ledger)
    elif kind == "append":
        _validate_append(entry, decision, messages, tasks, ledger)
    elif kind == "hold":
        _validate_hold(entry, decision, messages)
    if not entry["problems"]:
        entry["status"] = "valid"
        entry["payload"] = _normalized(kind, decision)
    return entry


def _clean(value, cap=MAX_TEXT):
    return str(value or "").strip()[:cap]


def _validate_create(entry, decision, messages, config):
    message_id = plan_module.normalize_message_id(decision.get("message_id"))
    message = messages.get(message_id)
    if not message:
        entry["problems"].append("message_id is not in this request")
        return
    if not message.get("locator"):
        entry["problems"].append("message has no archived evidence locator")
        return
    customer_key = _clean(decision.get("customer_key"), 80)
    if customer_key != message.get("customer_key"):
        entry["problems"].append(
            f"customer_key {customer_key!r} does not match the message's "
            f"customer {message.get('customer_key')!r}")
    if customer_key not in {str(c.get("key")) for c in config.get("customers", [])}:
        entry["problems"].append("customer_key is not in the config")
    title = _clean(decision.get("title"), MAX_TITLE)
    if not title:
        entry["problems"].append("title is empty")
    if plan_module.PLACEHOLDER in title:
        entry["problems"].append("title still contains a placeholder")
    if not _clean(decision.get("action")):
        entry["problems"].append("action is empty")
    if not _clean(decision.get("completion_standard")):
        entry["problems"].append("completion_standard is empty")
    for field in ("due_date", "internal_review"):
        value = _clean(decision.get(field), 40)
        if value and not DATE_RE.match(value):
            entry["problems"].append(f"{field} must be YYYY-MM-DD")
    entry["_message"] = message


def _validate_complete(entry, decision, tasks, archived_ids, ledger):
    identity = _clean(decision.get("identity"), 120)
    record = ledger.get(identity)
    if not record or not record.get("page_id"):
        entry["problems"].append("identity is not an open task in local state")
    if not _clean(decision.get("evidence")):
        entry["problems"].append("evidence text is required")
    locator = _clean(decision.get("source_locator"), 1000)
    if not locator:
        entry["problems"].append("source_locator is required")
    message_id = plan_module.normalize_message_id(decision.get("message_id"))
    date = _clean(decision.get("evidence_date"), 40)
    if not (message_id or date):
        entry["problems"].append("evidence_date or message_id is required")
    if message_id and message_id not in archived_ids:
        entry["problems"].append("message_id is not an archived original — "
                                 "the outcome cannot be verified")
    if date and not DATE_RE.match(date):
        entry["problems"].append("evidence_date must be YYYY-MM-DD")


def _validate_append(entry, decision, messages, tasks, ledger):
    identity = _clean(decision.get("identity"), 120)
    record = ledger.get(identity)
    if not record or not record.get("page_id"):
        entry["problems"].append("identity is not an open task in local state")
    message_id = plan_module.normalize_message_id(decision.get("message_id"))
    if message_id not in messages:
        entry["problems"].append("message_id is not in this request")
    if not _clean(decision.get("evidence")):
        entry["problems"].append("evidence text is required")


def _validate_hold(entry, decision, messages):
    message_id = plan_module.normalize_message_id(decision.get("message_id"))
    if message_id not in messages:
        entry["problems"].append("message_id is not in this request")
    if not _clean(decision.get("reason"), 300):
        entry["problems"].append("reason is required")


def _normalized(kind, decision):
    """The fields the apply step actually consumes, already cleaned."""
    out = {"decision": kind}
    if kind == "create":
        out.update({
            "customer_key": _clean(decision.get("customer_key"), 80),
            "message_id": _clean(decision.get("message_id"), 400),
            "title": _clean(decision.get("title"), MAX_TITLE),
            "action": _clean(decision.get("action")),
            "completion_standard": _clean(decision.get("completion_standard")),
            "due_date": _clean(decision.get("due_date"), 40) or None,
            "internal_review": _clean(decision.get("internal_review"), 40) or None,
            "evidence": _clean(decision.get("evidence")),
            "note": _clean(decision.get("note"), 500),
            "generatedBy": _clean(decision.get("generatedBy"), 80),
        })
    elif kind == "complete":
        out.update({
            "identity": _clean(decision.get("identity"), 120),
            "evidence": _clean(decision.get("evidence")),
            "source_locator": _clean(decision.get("source_locator"), 1000),
            "evidence_date": _clean(decision.get("evidence_date"), 40) or None,
            "message_id": _clean(decision.get("message_id"), 400) or None,
            "note": _clean(decision.get("note"), 500),
        })
    elif kind == "append":
        out.update({
            "identity": _clean(decision.get("identity"), 120),
            "message_id": _clean(decision.get("message_id"), 400),
            "evidence": _clean(decision.get("evidence")),
            "note": _clean(decision.get("note"), 500),
        })
    elif kind == "hold":
        out.update({
            "message_id": _clean(decision.get("message_id"), 400),
            "reason": _clean(decision.get("reason"), 300),
        })
    return out


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
