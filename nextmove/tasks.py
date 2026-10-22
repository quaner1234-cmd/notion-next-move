"""Task database: property discovery, dedupe, create/update, completion gate.

This module deliberately treats the user's existing Notion database as the
source of truth. It reads the live schema, maps your configured property names
onto it, and refuses to write when something does not line up.
"""
from __future__ import annotations

import re

from . import plan as plan_module
from . import dedupe as dedupe_module
from .notion import heading, link_bullet, paragraph, rich_text

STATUS_GROUP_DEFAULT = {"todo": "Todo", "in_progress": "In progress",
                        "done": "Done", "archived": "Archived"}


class TaskError(Exception):
    """A task operation cannot proceed safely."""


def discover_properties(client, data_source_id):
    """Read the live data source schema.

    Returns ``(schema, status_options)`` where schema maps property name ->
    property type, and status_options is the option names of the status-type
    property used for task state (None when the type carries no option list).
    """
    data = client.get_data_source(data_source_id)
    schema = {}
    status_options = None
    for name, meta in (data.get("properties") or {}).items():
        kind = meta.get("type")
        schema[name] = kind
        if kind == "status" and status_options is None:
            options = ((meta.get("status") or {}).get("options")) or []
            status_options = [o.get("name") for o in options]
    return schema, status_options


def resolve_mapping(config, schema):
    """Map config property roles onto the live schema.

    Returns ``(mapping, kinds, problems)``:
    * ``mapping`` role -> actual property name;
    * ``kinds`` role -> live property type, so writers can emit the right
      shape (``status`` vs ``select``);
    * ``problems`` blocks execution until fixed.
    """
    problems = []
    mapping, kinds = {}, {}
    configured = config["notion"].get("properties") or {}
    for role, name in configured.items():
        if name:
            if name not in schema:
                problems.append(f"properties.{role} is set to {name!r} but the "
                                f"database has no such property")
            else:
                mapping[role] = name
                kinds[role] = schema[name]
    # Fall back to type-based discovery only for the two roles we cannot work
    # without, and only when unambiguous.
    if "title" not in mapping:
        titles = [n for n, t in schema.items() if t == "title"]
        if len(titles) == 1:
            mapping["title"] = titles[0]
            kinds["title"] = "title"
        else:
            problems.append("cannot determine the title property (set properties.title)")
    if "status" not in mapping:
        candidates = [n for n, t in schema.items() if t in ("status", "select")]
        if len(candidates) == 1:
            mapping["status"] = candidates[0]
            kinds["status"] = schema[candidates[0]]
        else:
            problems.append("cannot determine the status property (set properties.status)")
    return mapping, kinds, problems


def status_value(config, role):
    return (config["notion"].get("status_map") or STATUS_GROUP_DEFAULT).get(role)


def check_status_option(config, role, schema, mapping, status_options):
    """Refuse to write a status value that does not exist in the database.

    An empty status must map to an explicit configured value; this tool never
    invents a status option and never writes an empty one.
    """
    if role not in ("todo", "in_progress", "done", "archived"):
        raise TaskError(f"unknown status role {role!r}")
    wanted = status_value(config, role)
    if not wanted:
        raise TaskError(f"no status value configured for role {role!r}")
    name = mapping.get("status")
    kind = schema.get(name) if name else None
    if kind == "status" and status_options is not None and wanted not in status_options:
        raise TaskError(
            f"status {wanted!r} does not exist in the database. "
            f"Existing options: {status_options}. Update the mapping in your "
            f"config — this tool never invents options.")
    return wanted


def page_title(page):
    for name, meta in (page.get("properties") or {}).items():
        if meta.get("type") == "title":
            return "".join(x.get("plain_text", "") for x in meta.get("title") or [])
    return ""


def page_property(page, name):
    meta = (page.get("properties") or {}).get(name) or {}
    kind = meta.get("type")
    return meta.get(kind) if kind else None


def load_existing(client, data_source_id):
    """ALL tasks, every status, following pagination to the end.

    Dedupe against a filtered 'open tasks' view alone would happily create a
    duplicate of a completed card, so this reads the complete data source.
    """
    return client.query_data_source(data_source_id, payload={})


def _property_text(value):
    """Plain text of a Notion property value (rich_text list, str or None)."""
    if value is None:
        return ""
    if isinstance(value, list):
        return "".join(x.get("plain_text", "") for x in value)
    if isinstance(value, dict):
        return str(value.get("name") or value.get("plain_text") or "")
    return str(value)


def existing_rows(existing_pages, mapping=None):
    """Reduce live Notion pages to the plain rows the dedupe module compares.

    Keeping this translation here is what lets ``dedupe`` stay pure (no
    Notion shapes, no network) and therefore testable offline.
    """
    rows = []
    for page in existing_pages:
        key_property = (mapping or {}).get("key")
        customer_property = (mapping or {}).get("customer")
        rows.append({
            "id": page.get("id"),
            "title": page_title(page),
            "identities": (_rich_text_tokens(page_property(page, key_property))
                           if key_property else set()),
            "customer": (_property_text(page_property(page, customer_property))
                         if customer_property else None),
            "page": page,
        })
    return rows


def classify_duplicate(existing_pages, config, candidate, mapping):
    """Three-way verdict: ``exact`` / ``same`` / ``near`` / ``new``.

    Only ``exact`` and ``same`` count as a real duplicate. ``near`` is held
    for a human: it is neither created (no duplicate rows) nor swallowed
    (it is reported with its score and the row it resembles).
    """
    rows = existing_rows(existing_pages, mapping)
    return dedupe_module.classify(rows, candidate.get("title"),
                                  identity=candidate.get("identity"),
                                  customer=candidate.get("customer_key"),
                                  policy=config.get("policy") or {})


def find_duplicate(existing_pages, config, candidate, mapping):
    """Find an existing task that already represents this work.

    Priority:
      1. the stable identity key, when a key property is configured (exact
         token match on the property's text or the page title);
      2. normalized title equality when the key property is unconfigured;
      3. fuzzy similarity at or above ``policy.dedupe.refresh_ratio``.

    A candidate that is only *similar* (between ``hold_ratio`` and
    ``refresh_ratio``) is **not** a duplicate: it is held for a human by
    :func:`classify_duplicate` instead, so it can neither duplicate an
    existing row nor disappear.
    """
    verdict = classify_duplicate(existing_pages, config, candidate, mapping)
    if verdict["kind"] in ("exact", "same") and verdict.get("row"):
        return verdict["row"].get("page")
    return None


def _rich_text_tokens(value):
    if value is None:
        return set()
    if isinstance(value, list):
        text = "".join(x.get("plain_text", "") for x in value)
    else:
        text = str(value)
    return {token.strip() for token in re.split(r"[;,\s]+", text) if token.strip()}


def build_properties(config, mapping, kinds, candidate, status_role="todo"):
    """Translate canonical role values into the database's own properties."""
    props = {}
    if "title" in mapping:
        props[mapping["title"]] = {"title": rich_text(candidate["title"])}
    if "status" in mapping:
        wanted = status_value(config, status_role)
        if kinds.get("status") == "select":
            props[mapping["status"]] = {"select": {"name": wanted}}
        else:
            props[mapping["status"]] = {"status": {"name": wanted}}
    if "customer" in mapping and candidate.get("customer_display"):
        props[mapping["customer"]] = {"rich_text": rich_text(candidate["customer_display"])}
    if "key" in mapping and candidate.get("identity"):
        props[mapping["key"]] = {"rich_text": rich_text(f"{candidate['identity']};")}
    for role, field in (("due_date", "due_date"),
                        ("internal_review", "internal_review"),
                        ("last_checked", "last_checked")):
        if role in mapping and candidate.get(field):
            props[mapping[role]] = {"date": {"start": candidate[field]}}
    if "source_locator" in mapping and candidate.get("source_locator"):
        kind = candidate.get("source_locator_kind", "url")
        if kind == "url":
            props[mapping["source_locator"]] = {"url": candidate["source_locator"]}
        else:
            props[mapping["source_locator"]] = {"rich_text": rich_text(candidate["source_locator"])}
    owner = config["notion"].get("owner_user_id")
    if owner and "owner" in mapping:
        props[mapping["owner"]] = {"people": [{"object": "user", "id": owner}]}
    return props


def body_blocks(candidate):
    """The evidence body appended under a task: action, completion test,
    evidence locator, and where the original lives."""
    blocks = []
    if candidate.get("action"):
        blocks.append(heading("下一行动"))
        blocks.append(paragraph(candidate["action"]))
    if candidate.get("completion_standard"):
        blocks.append(heading("完成标准"))
        blocks.append(paragraph(candidate["completion_standard"]))
    blocks.append(heading("证据与来源"))
    if candidate.get("evidence"):
        blocks.append(paragraph(candidate["evidence"]))
    if candidate.get("source_locator"):
        blocks.append(link_bullet("来源", candidate["source_locator"]))
    if candidate.get("customer_display"):
        blocks.append(paragraph(f"客户：{candidate['customer_display']}"
                                f"（key: {candidate.get('customer_key')}）"))
    if candidate.get("identity"):
        blocks.append(paragraph(f"任务标识：{candidate['identity']}"))
    return blocks


def apply_candidate(client, config, mapping, kinds, candidate, existing_pages,
                    known_keys, commit=False):
    """Create or update one task candidate. Returns a result dict.

    ``commit=False`` is a dry run: nothing is written; the result says what
    would happen. A duplicate is reported and skipped, never rewritten.
    """
    problems = plan_module.task_problems(candidate, config, known_keys)
    result = {"identity": candidate.get("identity"), "title": candidate.get("title"),
              "action": None, "problems": problems, "page_id": None}
    if problems:
        result["action"] = "rejected"
        return result
    verdict = classify_duplicate(existing_pages, config, candidate, mapping)
    result["match"] = {"kind": verdict["kind"], "score": verdict["score"],
                       "reason": verdict["reason"]}
    if verdict["kind"] == "near":
        # Held for a human: not created (no duplicate row) and not dropped
        # (it is reported with the row it resembles and its score).
        result["action"] = "needs_review"
        result["matched_page_id"] = (verdict.get("row") or {}).get("id")
        result["matched_title"] = (verdict.get("row") or {}).get("title")
        return result
    duplicate = find_duplicate(existing_pages, config, candidate, mapping)
    if duplicate:
        result["action"] = "duplicate"
        result["page_id"] = duplicate.get("id")
        result["existing_title"] = page_title(duplicate)
        if candidate.get("append_evidence") and commit:
            client.append_children(duplicate["id"], [paragraph(
                f"补充证据（{candidate.get('evidence_date') or '日期未记录'}）："
                f"{candidate.get('evidence') or ''}")])
            result["action"] = "updated"
        return result
    properties = build_properties(config, mapping, kinds, candidate)
    if not commit:
        result["action"] = "would_create"
        result["planned_properties"] = sorted(properties)
        return result
    page = client.create_page(plan_module.task_data_source_id(config), properties,
                              children=body_blocks(candidate))
    result["action"] = "created"
    result["page_id"] = page.get("id")
    result["url"] = page.get("url")
    return result


def completion_gate(candidate_evidence, task_record):
    """Mechanical precondition for marking a task done.

    **What this gate does and does not do — read carefully.**

    It checks *completeness and traceability of the claim*: the task exists in
    the local ledger with a live page, the completion note carries evidence
    text, a source locator, and a date or message id. It does NOT verify that
    the evidence is real, that the locator points at the claimed outcome, or
    that the outcome actually happened — that is human (or agent) judgement
    and is deliberately outside the tool.

    So: a task that passes this gate is *well-formed*, not *verified true*.
    The `verify_evidence_locator` helper below is what raises the bar from
    "well-formed" to "the locator resolves to a message that was actually
    archived by this tool"; callers that can check it should.
    """
    reasons = []
    if not task_record or not task_record.get("page_id"):
        reasons.append("task is not in the local ledger (nothing to complete)")
    if not str(candidate_evidence.get("evidence") or "").strip():
        reasons.append("completion evidence text is required")
    if not str(candidate_evidence.get("source_locator") or "").strip():
        reasons.append("evidence source locator (URL) is required")
    if not (candidate_evidence.get("evidence_date") or candidate_evidence.get("message_id")):
        reasons.append("evidence date or message id is required")
    return {"allowed": not reasons, "reasons": reasons,
            "scope": "well_formed_claim_not_verified_outcome"}


def verify_evidence_locator(candidate_evidence, state):
    """Raise the completion check from 'well-formed' to 'traceable to source'.

    Returns ``{"verifiable": bool, "checks": [...]}``. When a message id is
    given it must appear in the local archive ledger (this tool archived it);
    when only a locator is given it must be a Notion URL under the same
    workspace shape the archive uses. A pass still proves traceability, not
    business truth — the outcome itself remains the user's judgement.
    """
    checks = []
    ident = None
    message_id = candidate_evidence.get("message_id")
    if message_id:
        from .plan import normalize_message_id
        wanted = normalize_message_id(message_id)
        ident = next((item for item in state.get("archived", [])
                      if normalize_message_id(item.get("message_id")) == wanted), None)
        checks.append({"check": "message_id_in_archive_ledger", "ok": bool(ident)})
    locator = str(candidate_evidence.get("source_locator") or "").strip()
    locator_ok = locator.startswith(("https://www.notion.so/", "https://app.notion.com/p/"))
    checks.append({"check": "locator_is_notion_url", "ok": locator_ok})
    if ident and locator and ident.get("locator"):
        same_page = ident["locator"].split("#")[0].rstrip("/") == locator.split("#")[0].rstrip("/")
        checks.append({"check": "locator_matches_archived_page", "ok": same_page})
    verifiable = all(c["ok"] for c in checks) and bool(checks)
    return {"verifiable": verifiable, "checks": checks,
            "scope": "traceable_to_archived_source_not_business_truth"}
