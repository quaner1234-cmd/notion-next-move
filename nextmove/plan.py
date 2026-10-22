"""Pure planning logic: what is new, what needs a body, what needs a task.

Everything in this module is a pure function over plain data (no network, no
disk). That is what makes the hard parts — coverage proof, dedupe, identity
safety — testable offline, and it is the layer you should review first.

Concepts:

* checkpoint — per folder: ``{"lastUid": int, "lastUidValidity": str,
  "lastMessageId": str}``. lastMessageId makes re-anchoring possible after a
  folder renumber, so pending mail is never lost or double-processed.
* known ids — every Message-ID already archived, i.e. already appended to its
  customer page. Used to skip without re-reading bodies.
* coverage — only a scan that re-found its anchor (by UID, or by Message-ID
  after a UIDVALIDITY change) and matched the EXISTS count may advance.
"""
from __future__ import annotations

import hashlib
import re

PLACEHOLDER = "TODO"
NULL_STATUSES = {"", "none", "null"}


def normalize_message_id(value):
    """Fold whitespace and lowercase only the domain, per RFC 5322.

    The local part of a Message-ID stays case-sensitive: folding it would
    merge two genuinely different ids.
    """
    value = re.sub(r"\s+", "", str(value or "")).strip("<>")
    if not value:
        return ""
    local, sep, domain = value.rpartition("@")
    return local + sep + domain.lower() if sep else value


def message_id_list(value):
    return [normalize_message_id(x) for x in re.findall(r"<[^<>]+>", str(value or ""))]


def effective_timestamp(header):
    """Header date when parseable, INTERNALDATE otherwise, both ISO strings.

    A message with no usable date must still be scannable (it becomes new);
    this returns ``(iso_or_None, source)`` so callers can report honestly.
    """
    from email.utils import parsedate_to_datetime
    from datetime import timezone
    stamp = None
    source = "none"
    raw = header.get("date_header")
    if raw:
        try:
            stamp = parsedate_to_datetime(raw)
            source = "header"
        except (TypeError, ValueError):
            stamp = None
    if stamp is None and header.get("internal_date"):
        try:
            stamp = parsedate_to_datetime(header["internal_date"])
            source = "internal"
        except (TypeError, ValueError):
            stamp = None
    if stamp is None:
        return None, "none"
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.isoformat(), source


def known_ids_index(archived):
    """Set of Message-IDs already archived (the ``sent-ledger`` equivalent)."""
    out = {}
    for item in archived or []:
        ident = normalize_message_id(item.get("message_id"))
        if ident:
            out[ident] = item
    return out


def plan_folder_scan(headers, checkpoint, known_ids, uid_validity, complete=False,
                     anchor_inclusive=None):
    """Decide the new-header set for one folder.

    Mirrors the verified production behaviour:
    * duplicate Message-IDs in the folder collapse to the first;
    * already-archived ids are excluded;
    * UIDVALIDITY change or lost anchor falls back to the complete header
      inventory and re-anchors by Message-ID;
    * coverage is proven only when the anchor is found and the epoch matches,
      otherwise the caller must re-scan or keep the old checkpoint.

    ``anchor_inclusive`` (default: the checkpoint's own ``anchorInclusive``
    flag) makes messages at the anchor UID itself count as new — used by a
    ``full`` baseline adoption so the very first historical message is not
    skipped.
    """
    if anchor_inclusive is None:
        anchor_inclusive = bool(checkpoint.get("anchorInclusive"))
    ordered = sorted(headers, key=lambda h: int(h["uid"]))
    anchor_uid = int(checkpoint.get("lastUid") or 0)
    anchor_id = normalize_message_id(checkpoint.get("lastMessageId"))
    old_epoch = checkpoint.get("lastUidValidity")
    epoch_changed = old_epoch is not None and str(old_epoch) != str(uid_validity)
    gaps = []
    exact = next((h for h in ordered
                  if int(h["uid"]) == anchor_uid
                  and normalize_message_id(h.get("message_id")) == anchor_id), None)
    by_id = next((h for h in ordered
                  if anchor_id and normalize_message_id(h.get("message_id")) == anchor_id), None)
    if not anchor_uid or not anchor_id:
        if complete:
            # Empty-folder baseline: the checkpoint was created before any
            # message existed (UID 0). A complete inventory now proves
            # coverage and everything in it is new against the virtual
            # anchor UID 0.
            anchor = {"uid": 0}
        else:
            gaps.append("missing_checkpoint_requires_explicit_baseline")
    else:
        anchor = by_id if epoch_changed else exact
        if post_checkpoint_lost(anchor, exact, epoch_changed):
            gaps.append("checkpoint anchor not found — full header reconciliation required")
    if epoch_changed and not complete:
        gaps.append("UIDVALIDITY changed — full header inventory needed")
    coverage = bool(anchor) and not gaps
    new_headers, seen, duplicates = [], set(), []
    for header in ordered:
        ident = normalize_message_id(header.get("message_id"))
        if not ident:
            gaps.append(f"message with no Message-ID (UID {header['uid']})")
            new_headers.append(header)
            continue
        if ident in seen:
            duplicates.append(header["uid"])
            continue
        seen.add(ident)
        if ident in known_ids:
            continue
        if coverage and anchor is not None:
            # Normally the checkpoint UID is already processed, so it is
            # skipped (uid <= anchor). A "full" baseline anchors inclusively
            # at the oldest message so the anchor itself is still new.
            threshold = int(anchor["uid"]) + (0 if anchor_inclusive else 1)
            if int(header["uid"]) < threshold:
                continue
        new_headers.append(header)
    return {
        "coverage_proven": coverage and not gaps,
        "gaps": sorted(set(gaps)),
        "uid_validity": str(uid_validity),
        "epoch_changed": epoch_changed,
        "anchor_uid": anchor["uid"] if anchor else None,
        "new_headers": new_headers,
        "duplicates": duplicates,
        "headers_read": len(ordered),
    }


def post_checkpoint_lost(anchor, exact, epoch_changed):
    if not epoch_changed:
        return anchor is None
    return exact is None and anchor is None


def resolve_sender(headers, aliases):
    """Map From/Reply-To to a known address; Sender header only when unknown.

    Returns ``(address_or_None, source)`` where source is one of
    ``from``, ``reply-to``, ``sender``, ``none``. Never guesses.
    """
    def candidates(key):
        value = headers.get(key)
        if not value:
            return []
        import re as _re
        return [a.strip().lower() for a in _re.findall(r"[^\s<>,;]+@[^\s<>,;]+", str(value))]
    for address in candidates("from"):
        if address in aliases:
            return address, "from"
    for address in candidates("reply_to"):
        if address in aliases:
            return address, "reply-to"
    known = candidates("from") + candidates("reply_to")
    if known:
        return known[0], "from"
    for address in candidates("sender"):
        return address, "sender"
    return None, "none"


def resolve_customer(address, sender_source, thread_customers, config_lookup):
    """Identify the customer for one message without guessing.

    Precedence, strongest first:
      1. explicit thread membership (In-Reply-To / References pointing at an
         archived message of exactly one customer);
      2. config lookup of the actual From/Reply-To address;
      3. a ``Sender`` header address (mailing-list style), only when the From
         address is unknown.

    Returns ``(customer_key_or_None, reason)``. ``None`` means: leave the
    message for human review; do not create, complete or advance anything.
    """
    if len(thread_customers) == 1:
        only = next(iter(thread_customers))
        if only:
            return only, "thread"
    if len(thread_customers) > 1:
        return None, "thread conflict — several customers in one thread"
    if sender_source in ("from", "reply-to") and address:
        key = config_lookup(address)
        if key:
            return key, f"address ({sender_source})"
        return None, "unknown sender address"
    if sender_source == "sender" and address:
        key = config_lookup(address)
        if key:
            return key, "sender header"
        return None, "unknown sender header"
    return None, "no usable sender address"


def task_identity(customer_key, message_id, explicit_key=None):
    """Stable, deterministic key for a new task. No clock, no counter."""
    canonical = normalize_message_id(message_id)
    if explicit_key:
        material = f"{customer_key}|{explicit_key}"
    else:
        material = f"{customer_key}|{canonical or 'no-message-id'}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def task_problems(candidate, config, existing_keys=None, live_notion_props=None):
    """Pre-flight validation of a task candidate before touching Notion.

    Returns a list of problems; empty means the candidate may be written.
    Answers the acceptance questions mechanically:
    * a rerun cannot create a duplicate (key uniqueness here + Notion check);
    * empty/absent status is rendered as an explicit configured To-do value;
    * placeholder text is rejected instead of being written as a fact.
    """
    problems = []
    title = str(candidate.get("title") or "").strip()
    if not title:
        problems.append("title is empty")
    if PLACEHOLDER in title:
        problems.append("title still contains the TODO placeholder")
    if str(candidate.get("status") or "").strip().lower() in NULL_STATUSES:
        problems.append("status literally empty — pick an explicit status value; "
                        "an empty status must not silently create an empty page")
    key = candidate.get("identity")
    if not key:
        problems.append("missing stable task identity")
    if existing_keys and key in existing_keys:
        problems.append("duplicate of an existing task key on this customer page")
    customer_key = candidate.get("customer_key")
    if not customer_key:
        problems.append("no customer key (record is not yet bound to a customer)")
    if customer_key and customer_key not in {str(c.get("key")) for c in config.get("customers", [])}:
        problems.append(f"customer {customer_key!r} is not in the config mapping")
    if customer_key and not candidate.get("source_locator"):
        problems.append("no source locator — every task must carry its evidence link")
    if live_notion_props is not None:
        expected = live_notion_props.get("title_name")
        if expected and candidate.get("title_property") and candidate["title_property"] != expected:
            problems.append(f"title property mismatch: config says {candidate['title_property']!r}, "
                            f"database has {expected!r}")
    return problems


def task_data_source_id(config):
    """The data source id tasks are read/written through.

    Resolution order: an explicit ``task_data_source_id``; the id the CLI
    resolved from the database and cached as ``_data_source_id``; finally the
    database id itself (older API versions, and tests that model a single id).
    """
    notion = config["notion"]
    return notion.get("task_data_source_id") or notion.get("_data_source_id") \
        or notion["task_database_id"]


def title_from_template(template, customer_name, action):
    try:
        return str(template).format(customer=customer_name, action=action).strip()
    except (KeyError, IndexError, ValueError):
        return f"{customer_name}｜{action}".strip("｜")


def dedupe_text_summary(folder, scan, body_candidates):
    return {
        "folder": folder,
        "headers_read": scan.get("headers_read"),
        "new_headers": len(scan.get("new_headers", [])),
        "duplicates": len(scan.get("duplicates", [])),
        "coverage_proven": scan.get("coverage_proven"),
        "gaps": scan.get("gaps"),
        "body_budget": len(body_candidates),
    }


def packet_shape_problem(packet):
    """Why ``packet`` cannot be read as a scan packet (``None`` when it can).

    An unreadable input must never look like an empty input. Reading a
    foreign JSON file here and reporting "0 suggestions" would silently
    discard real work — the same failure as a producer renaming the section
    an extractor greps for. So callers must check this first and report.
    """
    if not isinstance(packet, dict):
        return "not a JSON object"
    if "new_messages" not in packet:
        return "missing the 'new_messages' key (expected a scan packet)"
    if not isinstance(packet["new_messages"], list):
        return "'new_messages' is not a list"
    return None


def suggestions_shape_problem(summary):
    """Why ``summary`` cannot be read as a suggestions file (``None`` = fine)."""
    if not isinstance(summary, dict):
        return "not a JSON object"
    if "tasks" not in summary and "completions" not in summary:
        return ("missing both 'tasks' and 'completions' "
                "(expected a suggestions.json written by `suggest`)")
    for key in ("tasks", "completions"):
        if key in summary and not isinstance(summary[key], list):
            return f"'{key}' is not a list"
    return None
