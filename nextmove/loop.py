"""The loop: scan mail read-only, archive evidence, derive next moves, apply.

This module is orchestration only — every step lives in a small pure module
(plan / mailcore / tasks / state) and every collaborator (mailbox, Notion
client) is injected, so the full loop can run offline against fakes in tests
and against the real mailbox/workspace in production without code changes.

Loop contract:
  1. Only messages whose sender resolves to a configured customer through an
     unambiguous rule are ever written anywhere.
  2. Evidence is appended to the customer's existing page and read back; the
     append is skipped when the page already carries the message marker
     (idempotent across crashes).
  3. A folder checkpoint advances only when the scan proved coverage, every
     new message was fully processed, and nothing was deferred or failed.
  4. Suggestions are candidates: `apply` re-validates each one mechanically
     before any write, and never rewrites a duplicate.
"""
from __future__ import annotations

import email
import hashlib
import html as html_module
import json
import copy
import re
from datetime import datetime, timedelta, timezone
from email import policy

from . import config as config_module
from . import mailcore, plan as plan_module
from . import state as state_module
from . import tasks as tasks_module
from .notion import paragraph, rich_text

MARKER_PREFIX = "mail-source-complete"
BODY_CHUNK = 1500
MAX_APPEND_BLOCKS = 90
CHILDREN_PAGE_LIMIT = 50  # pages of 100 blocks scanned when checking a marker


class LoopError(Exception):
    """The loop cannot continue safely."""


# ---------------------------------------------------------------------------
# small helpers


def offset_from(text):
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})", str(text or "").strip())
    if not match:
        raise LoopError(f"policy.timezone must look like '+08:00' (got {text!r})")
    sign = 1 if match.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))


def now_iso(offset):
    return datetime.now(offset).isoformat(timespec="seconds")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_to_text(html_text):
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html_text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_body(raw_bytes):
    """Return (body_text, source, attachments) from a full message."""
    message = email.message_from_bytes(raw_bytes, policy=policy.default)
    texts, htmls = [], []
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_content()
        except Exception:
            try:
                payload = part.get_payload(decode=True).decode("utf-8", "replace")
            except Exception:
                continue
        if not isinstance(payload, str):
            continue
        (texts if kind == "text/plain" else htmls).append(payload)
    attachments = []
    for part in message.walk():
        if part.get_filename():
            attachments.append(str(part.get_filename()))
    if texts:
        return "\n".join(x.strip() for x in texts if x.strip()).strip(), "text/plain", attachments
    if htmls:
        return strip_to_text("\n".join(htmls)), "text/html-stripped", attachments
    return "", "empty", attachments


def preview(text, limit=600):
    """Trim only conventional quote/signature boundaries, then hard-cap."""
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    cut = re.search(r"(?im)^(?:On .{8,180}wrote:|[- ]{3,}(?:Original Message|原始邮件)[- ]*|_{5,}\s*)$", body)
    if cut and body[:cut.start()].strip():
        body = body[:cut.start()]
    body = body.strip()
    truncated = len(body) > limit
    return {"text": body[:limit], "truncated": truncated}


def build_locator(page_id, block_id=None):
    base = "https://www.notion.so/" + str(page_id).replace("-", "")
    if block_id:
        return base + "#" + str(block_id).replace("-", "")
    return base


# ---------------------------------------------------------------------------
# evidence archiving


def archive_blocks(record):
    """Native Notion blocks carrying one original message, faithful to source."""
    metadata = {key: record.get(key) for key in (
        "message_id", "date", "from", "to", "cc", "reply_to", "subject",
        "in_reply_to", "references", "folder", "uid", "account", "body_source",
        "attachments", "archivedBodySha256", "originalBodySha256")}
    heading_text = "原始邮件｜{}｜{}".format(record.get("date") or "无日期",
                                            record.get("subject") or "无主题")
    blocks = [{"object": "block", "type": "heading_3",
               "heading_3": {"rich_text": rich_text(heading_text)}},
              {"object": "block", "type": "code",
               "code": {"rich_text": rich_text(json.dumps(metadata, ensure_ascii=False, indent=2)),
                        "language": "plain text"}}]
    body = record["body"]
    for start in range(0, len(body), BODY_CHUNK):
        blocks.append({"object": "block", "type": "code",
                       "code": {"rich_text": rich_text(body[start:start + BODY_CHUNK]),
                                "language": "plain text"}})
    if not body:
        blocks.append(paragraph("（正文为空或未能提取文本）"))
    marker = "{}: {} | {}".format(MARKER_PREFIX, record.get("message_id"),
                                 record.get("archivedBodySha256"))
    blocks.append(paragraph(marker))
    return blocks


def page_contains_marker(client, page_id, needle, max_pages=CHILDREN_PAGE_LIMIT):
    """Scan the page's block children for a marker; returns (found, complete)."""
    cursor = None
    pages = 0
    while pages < max_pages:
        path = f"/v1/blocks/{page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        data = client.call("GET", path)
        for block in data.get("results", []):
            kind = block.get("type")
            payload = block.get(kind) or {}
            for item in payload.get("rich_text", []) or []:
                text = item.get("plain_text")
                if text is None:
                    text = (item.get("text") or {}).get("content") or ""
                if needle in text:
                    return True, True
        if not data.get("has_more"):
            return False, True
        cursor = data.get("next_cursor")
        pages += 1
    return False, False


def archive_message(client, config, folder_key, customer, record, commit):
    """Append one message's evidence under the customer page, idempotently."""
    page_id = customer["page_id"]
    marker_id = f"{MARKER_PREFIX}: {record['message_id']} "
    result = {"message_id": record["message_id"], "customer_key": record["customer_key"],
              "page_id": page_id, "action": None, "verified": False}
    found, complete = page_contains_marker(client, page_id, marker_id)
    if not complete:
        result["action"] = "held"
        result["reason"] = ("无法完成存档去重检查（页面块过多，超过扫描上限）；"
                            "为避免重复追加，本次先不写入")
        return result
    if found:
        result["action"] = "already-archived"
        result["verified"] = True
        result["locator"] = build_locator(page_id)
        return result
    blocks = archive_blocks(record)
    if not commit:
        result["action"] = "would-archive"
        result["blocks"] = len(blocks)
        return result
    created = []
    for start in range(0, len(blocks), MAX_APPEND_BLOCKS):
        batch = blocks[start:start + MAX_APPEND_BLOCKS]
        data = client.append_children(page_id, batch, readback=False)
        created.extend(data.get("results", []))
    marker = f"{MARKER_PREFIX}: {record['message_id']} | {record['archivedBodySha256']}"
    found, complete = page_contains_marker(client, page_id, marker)
    result["action"] = "archived"
    result["verified"] = bool(found and complete)
    result["blocks"] = len(created)
    for block in created:
        if block.get("type") == "heading_3":
            result["locator"] = build_locator(page_id, block.get("id"))
            break
    if not result["verified"]:
        result["reason"] = "回读未找到完整性标记，请人工检查该客户页面"
    return result


# ---------------------------------------------------------------------------
# scan


def _folder_key(role, name):
    return f"{role}:{name}"


def scan_mailbox(config, mailbox, client, state, run_dir, commit, max_bodies,
                 baseline="latest"):
    """Read both folders, archive evidence for resolvable messages.

    Returns the packet dict. State mutations happen in-place only when
    ``commit`` is true and the corresponding work was fully verified.

    ``mailbox`` may be connected already (a context manager) or not; this
    function connects it when needed and leaves an already-open session alone.
    """
    offset = offset_from(config["policy"]["timezone"])
    owns_connection = mailbox.connection is None
    if owns_connection:
        mailbox.connect()
    known = state_module.archived_index(state)
    customers = config_module.customers_by_key(config)
    packet = {
        "generated_at": now_iso(offset),
        "account": config["mailbox"]["account"],
        "mailbox_user": config["mailbox"]["user"],
        "folders": {},
        "new_messages": [],
        "pending_recheck": [],
        "summary": {},
    }
    # First: re-try earlier unknown-ownership items (config may have changed).
    packet["pending_recheck"] = recheck_pending(config, mailbox, client, state, run_dir, commit)

    total_new = total_fetched = total_archived = total_pending = total_deferred = 0
    for role, folder_name in (("inbox", config["mailbox"]["inbox_folder"]),
                              ("sent", config["mailbox"]["sent_folder"])):
        if not folder_name:
            packet["folders"][role] = {"error": "not configured"}
            continue
        key = _folder_key(role, folder_name)
        entry = {"folder": folder_name, "role": role, "error": None,
                 "scanned": False, "new": 0, "fetched": 0, "archived": 0,
                 "pending_review": 0, "deferred": 0, "failed": 0,
                 "coverage_proven": False, "advanced": False, "gaps": []}
        try:
            raw_name = mailbox.resolve_folder(folder_name)
            exists, uid_validity = mailbox.examine(raw_name)
            inventory = mailbox.uid_inventory()
            if len(inventory) != exists:
                raise mailcore.MailError(
                    f"UID inventory {len(inventory)} != EXISTS {exists} for {folder_name}")
        except mailcore.MailError as exc:
            entry["error"] = str(exc)
            packet["folders"][key] = entry
            if commit:
                state_module.keep_checkpoint(state, key, str(exc)[:200])
            continue

        checkpoint = state_module.folder_checkpoint(state, key, uid_validity)
        first_run = not checkpoint.get("baselineAt")
        working = copy.deepcopy(checkpoint)
        if first_run:
            # Adopt a baseline into the *working* copy only. It is committed
            # to the real checkpoint by the same advance path as any other
            # run, so a first run that fails to process its window does not
            # silently skip over it on the next run.
            #   latest (default) — anchor at the newest message, exclusive:
            #     nothing currently in the folder is imported; the next run
            #     picks up whatever arrives from now on.
            #   full — anchor inclusively at the OLDEST message: the whole
            #     folder is treated as new and processed in this run.
            working["baselineAt"] = now_iso(offset)
            working["baselineMode"] = baseline
            if not inventory:
                working.update({"lastUid": 0, "anchorInclusive": False})
                entry["baseline"] = {"mode": baseline, "uid": None,
                                     "note": "文件夹为空，已记录基线"}
            elif baseline == "full":
                oldest = min(inventory)
                anchor = next((h for h in mailbox.fetch_headers([oldest])
                               if h["uid"] == oldest), None)
                working.update({
                    "lastUid": oldest,
                    "lastMessageId": anchor.get("message_id") if anchor else None,
                    "lastUidValidity": str(uid_validity),
                    "anchorInclusive": True,
                })
                entry["baseline"] = {"mode": "full", "uid": oldest,
                                     "note": "全量基线：从最早一封开始导入（只归档可解析发件人的邮件）"}
            else:
                newest = max(inventory)
                anchor = next((h for h in mailbox.fetch_headers([newest])
                               if h["uid"] == newest), None)
                working.update({
                    "lastUid": newest,
                    "lastMessageId": anchor.get("message_id") if anchor else None,
                    "lastUidValidity": str(uid_validity),
                    "anchorInclusive": False,
                })
                entry["baseline"] = {
                    "mode": "latest", "uid": newest,
                    "note": "首次运行：默认从最新邮件开始；如需导入历史，先删除 state/state.json 并用 --baseline full 重跑"}

        if not inventory:
            if commit:
                checkpoint.update(working)
            entry["scanned"] = True
            entry["coverage_proven"] = True
            entry["advanced"] = commit
            packet["folders"][key] = entry
            continue

        recovery = int(working["lastUid"]) not in inventory or \
            str(working.get("lastUidValidity")) not in (None, str(uid_validity))
        try:
            if recovery or working.get("anchorInclusive"):
                selected = inventory
            else:
                selected = sorted(set(inventory[-100:]) |
                                  {u for u in inventory if u > int(working["lastUid"])})
            headers = mailbox.fetch_headers(selected)
            scan = plan_module.plan_folder_scan(headers, working, known, uid_validity,
                                                complete=recovery)
            if not scan["coverage_proven"] and not recovery:
                headers = mailbox.fetch_headers(inventory)
                scan = plan_module.plan_folder_scan(headers, working, known, uid_validity,
                                                    complete=True)
        except mailcore.MailError as exc:
            entry["error"] = str(exc)
            packet["folders"][key] = entry
            if commit:
                state_module.keep_checkpoint(state, key, str(exc)[:200])
            continue

        entry["scanned"] = True
        entry["coverage_proven"] = scan["coverage_proven"]
        entry["gaps"] = scan["gaps"]
        entry["new"] = len(scan["new_headers"])
        total_new += entry["new"]

        fetch_list = scan["new_headers"][:max_bodies]
        entry["deferred"] = len(scan["new_headers"]) - len(fetch_list)
        total_deferred += entry["deferred"]
        failures = 0

        for header in fetch_list:
            record_result = process_message(
                config, mailbox, client, state, run_dir, commit, role, folder_name, header)
            packet["new_messages"].append(record_result["packet"])
            total_fetched += 1
            entry["fetched"] += 1
            # A message is counted once: either it failed (fetch/write error)
            # or it is deferred by the body budget — never double counted.
            if record_result["packet"].get("error"):
                failures += 1
                entry["failed"] += 1
            if record_result["archived"]:
                entry["archived"] += 1
                total_archived += 1
            if record_result["pending"]:
                entry["pending_review"] += 1
                total_pending += 1

        advance_ok = (scan["coverage_proven"] and entry["deferred"] == 0
                      and failures == 0 and entry["new"] > 0)
        blocked = any(item.get("folder") == folder_name for item in state.get("pending", []))
        if first_run and entry["new"] == 0 and scan["coverage_proven"] and failures == 0:
            # Baseline-only run (latest mode): nothing in scope, commit the
            # adopted anchor so future runs diff against it.
            if commit:
                checkpoint.update(working)
            entry["advanced"] = True
        elif advance_ok and blocked:
            # Unresolved ownership keeps this folder's checkpoint where it is,
            # so nothing is ever skipped over while a message awaits review.
            entry["held"] = "pending_review_blocks_checkpoint"
        elif advance_ok and commit:
            state_module.advance_checkpoint(state, key, scan["new_headers"], uid_validity)
            entry["advanced"] = True
        elif entry["new"] == 0 and already_tail(working, inventory) and commit:
            entry["advanced"] = True

        packet["folders"][key] = entry

    packet["summary"] = {
        "new": total_new, "fetched": total_fetched, "archived": total_archived,
        "pending_review": total_pending, "deferred": total_deferred,
        "commit": bool(commit),
    }
    if owns_connection:
        mailbox.close()
    return packet


def already_tail(checkpoint, inventory):
    return bool(inventory) and int(checkpoint.get("lastUid") or 0) >= max(inventory)


def process_message(config, mailbox, client, state, run_dir, commit, role, folder_name, header):
    """Fetch one body, resolve its customer, archive it. Never raises for
    per-message problems — they are reported in the packet instead."""
    offset = offset_from(config["policy"]["timezone"])
    uid = int(header["uid"])
    packet = {"uid": uid, "folder": folder_name, "role": role,
              "message_id": header.get("message_id"),
              "date": header.get("date_header"), "subject": header.get("subject"),
              "from": header.get("from"), "to": header.get("to"),
              "direction": "inbound" if role == "inbox" else "outbound"}
    result = {"packet": packet, "archived": False, "pending": False}
    try:
        raw = mailbox.fetch_body(uid)
    except mailcore.MailError as exc:
        packet["error"] = f"正文读取失败：{exc}"
        packet["body_unfetched"] = True
        return result
    body, body_source, attachments = extract_body(raw)
    if not body:
        body = ""
    try:
        parsed = email.message_from_bytes(raw, policy=policy.default)
        date_value = parsed.get("Date")
        from_value = parsed.get("From")
        to_value = parsed.get("To")
        cc_value = parsed.get("Cc")
        reply_to = parsed.get("Reply-To")
        in_reply_to = parsed.get("In-Reply-To")
        references = parsed.get("References")
    except Exception:
        date_value = from_value = to_value = cc_value = reply_to = in_reply_to = references = None
    packet.update({"date": date_value or header.get("date_header"),
                   "from": from_value, "to": to_value})
    if role == "sent":
        # Outbound mail needs its thread headers in the packet so reply
        # matching can find the task it answers.
        packet["in_reply_to"] = in_reply_to
        packet["references"] = references
    body_sha = sha256_text(body)
    record_base = {
        "message_id": header.get("message_id"), "date": date_value,
        "from": from_value, "to": to_value, "cc": cc_value, "reply_to": reply_to,
        "subject": header.get("subject"), "in_reply_to": in_reply_to,
        "references": references, "folder": folder_name, "uid": uid,
        "account": config["mailbox"]["account"], "body_source": body_source,
        "attachments": attachments, "body": body,
        "archivedBodySha256": body_sha, "originalBodySha256": body_sha,
    }
    preview_info = preview(body)
    packet["body_preview"] = preview_info["text"]
    packet["body_chars"] = len(body)
    packet["body_truncated_in_packet"] = preview_info["truncated"]
    packet["attachments"] = attachments

    # Resolve the customer without guessing.
    if role == "inbox":
        addresses_headers = {"from": from_value, "reply_to": reply_to,
                             "sender": header.get("sender")}
    else:
        addresses_headers = {"from": to_value, "reply_to": cc_value, "sender": None}
    aliases = {}
    for item in config["customers"]:
        for address in item.get("email_addresses", []) or []:
            aliases[str(address).strip().lower()] = item["key"]
    address, source = plan_module.resolve_sender(addresses_headers, aliases)
    known = state_module.archived_index(state)
    thread_customers = set()
    for ref in plan_module.message_id_list(in_reply_to) + plan_module.message_id_list(references):
        item = known.get(ref)
        if item and item.get("customer_key"):
            thread_customers.add(item["customer_key"])
    customer_key, reason = plan_module.resolve_customer(
        address, source, thread_customers,
        lambda addr: config_module.customer_for_address(config, addr))
    packet["resolved_address"] = address
    packet["resolution"] = reason
    packet["customer_key"] = customer_key
    if not customer_key:
        packet["pending_review"] = True
        result["pending"] = True
        review_page = str(config["policy"].get("review_page_id") or "").replace("-", "")
        if commit and review_page:
            # Same safety valve as the production system: capture the raw
            # message under a dedicated review page so nothing is lost while
            # ownership is unresolved.
            review_result = archive_message(
                client, config, folder_name,
                {"page_id": review_page, "key": "unassigned"}, record_base, commit)
            packet["archive"] = review_result["action"]
            packet["archive_verified"] = review_result["verified"]
        if commit:
            state_module.add_pending(state, {
                "message_id": header.get("message_id"), "folder": folder_name, "uid": uid,
                "date": date_value, "from": from_value, "subject": header.get("subject"),
                "reason": reason, "status": "needs_review"})
        return result

    customer = config_module.customers_by_key(config).get(customer_key) or {}
    page_id = str(customer.get("page_id") or "").replace("-", "")
    if not page_id:
        packet["pending_review"] = True
        packet["resolution"] = f"customer {customer_key} has no page_id"
        result["pending"] = True
        if commit:
            state_module.add_pending(state, {
                "message_id": header.get("message_id"), "folder": folder_name, "uid": uid,
                "reason": "customer page not configured", "status": "needs_review"})
        return result

    record = {**record_base, "customer_key": customer_key}
    try:
        archive_result = archive_message(client, config, folder_name,
                                         {**customer, "page_id": page_id}, record, commit)
    except Exception as exc:  # NotionError / network — report, never claim success
        packet["error"] = f"存档写入失败：{exc}"
        packet["archive"] = "failed"
        result["deferred"] = True
        return result
    packet["archive"] = archive_result["action"]
    packet["archive_verified"] = archive_result["verified"]
    if archive_result.get("reason"):
        packet["archive_reason"] = archive_result["reason"]
    if archive_result.get("locator"):
        packet["locator"] = archive_result["locator"]
    if archive_result["verified"]:
        result["archived"] = True
        if commit:
            state_module.record_archive(state, header.get("message_id"), customer_key,
                                        page_id, archive_result.get("locator") or build_locator(page_id),
                                        body_sha)
    elif archive_result["action"] in ("held",) or (
            archive_result["action"] == "archived" and not archive_result["verified"]):
        packet["error"] = archive_result.get("reason") or "存档回读失败"
    return result


def recheck_pending(config, mailbox, client, state, run_dir, commit):
    """Re-attempt earlier unknown-ownership messages after a config update."""
    out = []
    for item in list(state.get("pending", [])):
        address = None
        from_value = item.get("from") or ""
        match = re.search(r"[^\s<>,;]+@[^\s<>,;]+", from_value)
        if match:
            address = match.group(0).lower()
        customer_key = config_module.customer_for_address(config, address) if address else None
        if not customer_key:
            out.append({"message_id": item.get("message_id"), "status": "still-unresolved",
                        "reason": item.get("reason")})
            continue
        entry = {"message_id": item.get("message_id"), "status": "resolved"}
        if commit:
            try:
                raw = mailbox.fetch_body(int(item["uid"]))
                body, body_source, attachments = extract_body(raw)
                record = {
                    "message_id": item.get("message_id"), "date": item.get("date"),
                    "from": from_value, "to": None, "cc": None, "reply_to": None,
                    "subject": item.get("subject"), "in_reply_to": None, "references": None,
                    "folder": item.get("folder"), "uid": item.get("uid"),
                    "account": config["mailbox"]["account"], "body_source": body_source,
                    "attachments": attachments, "body": body,
                    "archivedBodySha256": sha256_text(body),
                    "originalBodySha256": sha256_text(body),
                    "customer_key": customer_key,
                }
                customer = config_module.customers_by_key(config)[customer_key]
                archive_result = archive_message(
                    client, config, item.get("folder", ""),
                    {**customer, "page_id": str(customer["page_id"]).replace("-", "")},
                    record, commit)
                entry["archive"] = archive_result["action"]
                if archive_result["verified"]:
                    state_module.record_archive(state, item.get("message_id"), customer_key,
                                                customer["page_id"],
                                                archive_result.get("locator", ""),
                                                sha256_text(body))
                    state_module.resolve_pending(state, item.get("message_id"))
            except mailcore.MailError as exc:
                entry["status"] = "fetch-failed"
                entry["error"] = str(exc)
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# suggest / apply


SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|回复|转发|答复)\s*[:：]\s*)+", re.I)


def suggest_candidates(config, packet, state):
    """Mechanical suggestions from the scan packet.

    For every new INBOUND message resolved to a customer: one reply task.
    For every new OUTBOUND message whose thread matches an open reply task:
    a completion candidate. Both are only proposals — `apply` validates.
    """
    offset = offset_from(config["policy"]["timezone"])
    customers = config_module.customers_by_key(config)
    template = config["policy"].get("title_template") or "{customer}｜{action}"
    todo_label = tasks_module.status_value(config, "todo")
    candidates, completions = [], []
    for message in packet.get("new_messages", []):
        key = message.get("customer_key")
        if not key or message.get("error"):
            continue
        display = (customers.get(key) or {}).get("display_name") or key
        subject = SUBJECT_PREFIX.sub("", str(message.get("subject") or "")).strip() or "（无主题）"
        date_text = str(message.get("date") or "").strip()
        if message.get("direction") == "inbound":
            action = f"回复客户邮件：{subject}"
            candidates.append({
                "identity": plan_module.task_identity(key, message.get("message_id")),
                "customer_key": key,
                "customer_display": display,
                "title": plan_module.title_from_template(template, display, action),
                "action": f"回复客户来信（{date_text}，主题：{subject}）",
                "completion_standard": "已回复该客户邮件，且回复可在邮箱「已发送」中核对（含日期与邮件ID）。",
                "evidence": f"客户来信：{date_text}｜{message.get('from')}｜{subject}",
                "evidence_date": message.get("date"),
                "message_id": message.get("message_id"),
                "source_locator": message.get("locator"),
                "status": todo_label,
                "origin": "scan:inbound",
            })
        else:
            refs = plan_module.message_id_list(message.get("in_reply_to")) + \
                plan_module.message_id_list(message.get("references"))
            for ref in refs:
                open_task = find_open_reply_task(state, key, ref)
                if not open_task:
                    continue
                completions.append({
                    "identity": open_task["identity"],
                    "customer_key": key,
                    "customer_display": display,
                    "title": open_task.get("title"),
                    "evidence": f"我方已发送回复：{date_text}｜主题：{subject}"
                                f"｜Message-ID：{message.get('message_id')}",
                    "evidence_date": message.get("date"),
                    "message_id": message.get("message_id"),
                    "source_locator": message.get("locator") or open_task.get("source_locator"),
                    "origin": "scan:sent-reply",
                    "reply_to_message": ref,
                })
                break
    return {"generated_at": now_iso(offset), "tasks": candidates,
            "completions": dedupe_completions(completions)}


def dedupe_completions(items):
    seen, out = set(), []
    for item in items:
        if item["identity"] in seen:
            continue
        seen.add(item["identity"])
        out.append(item)
    return out


def find_open_reply_task(state, customer_key, message_id):
    wanted = plan_module.normalize_message_id(message_id)
    for identity, record in state.get("tasks", {}).items():
        if record.get("customer_key") != customer_key:
            continue
        if plan_module.normalize_message_id(record.get("message_id")) != wanted:
            continue
        if record.get("status") in ("done", "archived", "完成", "已归档"):
            continue
        return {**record, "identity": identity}
    return None


def apply_candidates(config, client, summary, state, commit):
    """Validate and write task candidates + completions. Dry-run by default."""
    data_source_id = plan_module.task_data_source_id(config)
    schema, status_options = tasks_module.discover_properties(client, data_source_id)
    mapping, kinds, problems = tasks_module.resolve_mapping(config, schema)
    if problems:
        raise LoopError("property mapping problems: " + "; ".join(problems))
    tasks_module.check_status_option(config, "todo", schema, mapping, status_options)
    tasks_module.check_status_option(config, "done", schema, mapping, status_options)
    existing = tasks_module.load_existing(client, data_source_id)
    known_keys = {record.get("identity") for record in state.get("tasks", {}).values()
                  if record.get("identity")}
    results = {"tasks": [], "completions": [], "commit": bool(commit),
               "existing_task_count": len(existing)}
    for candidate in summary.get("tasks", []):
        outcome = tasks_module.apply_candidate(client, config, mapping, kinds, candidate,
                                               existing, known_keys, commit=commit)
        results["tasks"].append(outcome)
        if outcome["action"] in ("created", "updated") and outcome.get("page_id"):
            state_module.upsert_task(state, candidate["identity"], {
                "identity": candidate["identity"], "title": candidate["title"],
                "customer_key": candidate["customer_key"], "page_id": outcome["page_id"],
                "message_id": candidate.get("message_id"),
                "source_locator": candidate.get("source_locator"),
                "status": "todo",
            })
            known_keys.add(candidate["identity"])
            existing.append({"id": outcome["page_id"],
                             "properties": {"_created": True}})
    for candidate in summary.get("completions", []):
        outcome = complete_task(client, config, mapping, kinds, candidate, state,
                                commit=commit)
        results["completions"].append(outcome)
    return results


def complete_task(client, config, mapping, kinds, candidate, state, commit):
    identity = candidate.get("identity")
    record = state.get("tasks", {}).get(identity)
    gate = tasks_module.completion_gate(candidate, record)
    outcome = {"identity": identity, "title": candidate.get("title"),
               "action": None, "reasons": gate["reasons"], "page_id": None}
    if not gate["allowed"]:
        outcome["action"] = "rejected"
        return outcome
    outcome["page_id"] = record["page_id"]
    if not commit:
        outcome["action"] = "would_complete"
        return outcome
    done_name = tasks_module.status_value(config, "done")
    prop_name = mapping["status"]
    payload = {prop_name: ({"select": {"name": done_name}}
                           if kinds.get("status") == "select"
                           else {"status": {"name": done_name}})}
    client.update_page(record["page_id"], payload, readback=True)
    client.append_children(record["page_id"], [paragraph(
        f"完成依据（{candidate.get('evidence_date') or '日期未记录'}）：{candidate.get('evidence')}"
        f" ｜ 来源：{candidate.get('source_locator')}")])
    state_module.upsert_task(state, identity, {"status": "done",
                                               "completedAt": now_iso(offset_from(config['policy']['timezone'])),
                                               "completionEvidence": candidate.get("source_locator")})
    outcome["action"] = "completed"
    return outcome


# ---------------------------------------------------------------------------
# reports


def summarize_state(config, state):
    offset = offset_from(config["policy"]["timezone"])
    open_tasks = [{"identity": key, "title": item.get("title"),
                   "customer_key": item.get("customer_key"), "status": item.get("status")}
                  for key, item in state.get("tasks", {}).items()
                  if item.get("status") not in ("done", "archived")]
    return {
        "generated_at": now_iso(offset),
        "folders": {key: {"lastUid": item.get("lastUid"),
                          "coverageProven": item.get("coverageProven"),
                          "checkedAt": item.get("checkedAt"),
                          "lastGap": item.get("lastGap")}
                    for key, item in state.get("folders", {}).items()},
        "archived_messages": len(state.get("archived", [])),
        "pending_review": [{"message_id": x.get("message_id"), "reason": x.get("reason"),
                            "subject": x.get("subject")}
                           for x in state.get("pending", [])],
        "open_tasks": sorted(open_tasks, key=lambda x: x["identity"] or ""),
        "recent_runs": state.get("runs", [])[-5:],
    }
