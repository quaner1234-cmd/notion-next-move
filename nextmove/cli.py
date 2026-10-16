"""CLI entry point: ``python -m nextmove <command>``.

The daily chain — one scheduled run, complete loop:

    python -m nextmove scan --apply-judgment

  1. reads new mail read-only and archives each original under the customer's
     Notion page (checkpoints advance only when everything in scope was
     processed and read back);
  2. queues the newly archived messages for judgement and writes
     ``state/judgment-request.json``;
  3. if ``state/judgment.json`` is waiting and still covers queued messages,
     validates and applies it in the same run.

Judgement hand-off (no vendor, no model assumed):

    python -m nextmove judgment-request      # (re)write the request file
    #   -> your agent (or you) reads it and writes state/judgment.json
    python -m nextmove apply --judgment state/judgment.json --commit

Mechanical fallback (no agent involved): ``suggest`` + ``apply --input``
produce one reply task per inbound message. Both paths share the same
validation, dedupe, readback and ledger machinery.

Every command prints a JSON report and exits non-zero only on real failure.
Exit codes: 0 ok, 1 failure, 2 configuration problem, 3 connection problem.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as config_module
from . import judgment as judgment_module
from . import loop, mailcore, state as state_module, tasks as tasks_module
from .notion import NotionClient, NotionError

EXIT_OK, EXIT_FAIL, EXIT_CONFIG, EXIT_CONNECTION = 0, 1, 2, 3


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _load(args):
    config = config_module.load(args.config)
    problems = config_module.validate(config)
    if problems:
        _print({"status": "CONFIG_INVALID", "problems": problems})
        raise SystemExit(EXIT_CONFIG)
    return config


def _mailbox(config):
    password, source = config_module.resolve_secret(config, "imap_password")
    if not password:
        _print({"status": "NO_IMAP_PASSWORD",
                "looked_for": source,
                "hint": "export NEXTMOVE_IMAP_PASSWORD (or set mailbox.password_env) "
                        "or put imap_password into your local secrets file"})
        raise SystemExit(EXIT_CONFIG)
    return mailcore.Mailbox(config["mailbox"]["host"], config["mailbox"]["port"],
                            config["mailbox"]["user"], password,
                            timeout=int(config["mailbox"].get("timeout_seconds") or 30)), source


def _client(config, dry_run=False):
    token, source = config_module.resolve_secret(config, "notion_token")
    if not token:
        _print({"status": "NO_NOTION_TOKEN", "looked_for": source,
                "hint": "export NOTION_TOKEN or put notion_token into your local "
                        "secrets file (never commit it)"})
        raise SystemExit(EXIT_CONFIG)
    client = NotionClient(token, version=config["notion"].get("notion_version", "2025-09-03"),
                          dry_run=dry_run)
    resolve_task_data_source(config, client)
    return client, source


def resolve_task_data_source(config, client):
    """Resolve + cache the data source id tasks are written to.

    A database URL gives the *database* id; the write API needs the data
    source under it. Resolved once per process and stashed on the config.
    """
    explicit = config["notion"].get("task_data_source_id")
    if config["notion"].get("_data_source_id"):
        return config["notion"]["_data_source_id"]
    try:
        data_source_id = client.resolve_data_source(
            config["notion"]["task_database_id"], explicit)
    except NotionError as exc:
        _print({"status": "NOTION_RESOLVE_FAILED", "error": str(exc)})
        raise SystemExit(EXIT_CONNECTION)
    config["notion"]["_data_source_id"] = data_source_id
    return data_source_id


def _runs_dir(config, tag):
    import time
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    path = Path(config_module.runs_dir(config)) / f"{stamp}-{tag}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _judgment_paths(config):
    """Where the request/judgment pair lives.

    Both sit next to the state file (``state/``), so a scheduled run and the
    agent hand-off always agree on the location without extra configuration.
    """
    state_dir = Path(config_module.state_path(config)).parent
    state_dir.mkdir(parents=True, exist_ok=True)
    return {"request": state_dir / "judgment-request.json",
            "judgment": state_dir / "judgment.json"}


def _write_request(config, state):
    """Build the judgment request from the queue and persist it."""
    paths = _judgment_paths(config)
    packet = {"new_messages": state.get("judgmentQueue", [])}
    request = loop.build_judgment_request(config, packet, state)
    paths["request"].write_text(json.dumps(request, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return request, paths


def _apply_judgment_document(config, client, state, document, request, commit):
    """Shared core: validate + apply + dequeue one judgment document.

    Returns a report dict; never raises for a stale/invalid document (that is
    a status, so a scheduled run continues and the queue keeps every
    unjudged message). Raises only for infrastructure errors (Notion/loop).
    """
    if state_module.judgment_already_applied(state, document.get("basedOn")):
        return {"status": "ALREADY_APPLIED", "basedOn": document.get("basedOn")}
    validation = judgment_module.validate(document, request, config, state)
    if not validation["ok"]:
        return {"status": "JUDGMENT_INVALID", "error": validation["error"],
                "decisions": validation["decisions"]}
    results = loop.apply_judgment(config, client, validation, state, commit=commit)
    report = {"status": "JUDGMENT_APPLIED" if commit else "JUDGMENT_DRY_RUN",
              "basedOn": document.get("basedOn"),
              "requestId": request.get("requestId"), "results": results}
    if document.get("basedOn") != request.get("requestId"):
        report["note"] = ("judgment was based on an earlier message set; its "
                          "still-queued decisions applied, the remainder stays "
                          "queued — re-read state/judgment-request.json")
    if commit:
        landed = {"created", "updated", "duplicate", "completed", "appended", "held"}
        handled = [o.get("message_id") for o in results.get("outcomes", [])
                   if o.get("action") in landed and o.get("message_id")]
        state_module.mark_judged(state, handled, document.get("basedOn") or "unknown")
    return report


def _try_apply_pending_judgment(config, client, state, paths, commit):
    """Apply state/judgment.json when it exists and messages are queued."""
    if not paths["judgment"].exists():
        return None
    request, _ = _write_request(config, state)
    if not request.get("messages"):
        return {"status": "NOTHING_QUEUED", "note": "no messages awaiting judgement"}
    try:
        document = judgment_module.load(paths["judgment"])
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "JUDGMENT_UNREADABLE", "error": str(exc)[:300]}
    return _apply_judgment_document(config, client, state, document, request, commit)


# ---------------------------------------------------------------------------


def cmd_doctor(args):
    config = _load(args)
    report = {"status": "ok", "checks": []}

    def check(name, result, detail=None):
        report["checks"].append({"check": name, "result": result, "detail": detail})

    check("config.secrets.imap", "found"
          if config_module.resolve_secret(config, "imap_password")[0] else "missing",
          config_module.resolve_secret(config, "imap_password")[1])
    check("config.secrets.notion", "found"
          if config_module.resolve_secret(config, "notion_token")[0] else "missing",
          config_module.resolve_secret(config, "notion_token")[1])

    mailbox, _ = _mailbox(config)
    folders = []
    try:
        with mailbox:
            for role in ("inbox", "sent"):
                name = config["mailbox"][f"{role}_folder"]
                try:
                    raw = mailbox.resolve_folder(name)
                    exists, validity = mailbox.examine(raw)
                    folders.append({"role": role, "configured": name, "resolved": raw,
                                    "messages": exists, "uidvalidity": validity})
                except mailcore.MailError as exc:
                    folders.append({"role": role, "configured": name, "error": str(exc)})
        check("mailbox.read_only_login", "ok", {"folders": folders})
    except mailcore.MailError as exc:
        check("mailbox.read_only_login", "failed", str(exc))
        report["status"] = "mailbox_unreachable"

    client, source = _client(config)
    ok, detail = client.can_reach()
    check("notion.token", "ok" if ok else "failed", detail)
    if ok:
        try:
            data_source_id = resolve_task_data_source(config, client)
            schema, options = tasks_module.discover_properties(client, data_source_id)
            mapping, kinds, problems = tasks_module.resolve_mapping(config, schema)
            check("notion.schema_mapping", "ok" if not problems else "problems",
                  {"mapping": mapping, "kinds": kinds, "status_options": options,
                   "problems": problems})
        except NotionError as exc:
            check("notion.schema_mapping", "failed", str(exc))
    if any(c["result"] in ("failed",) for c in report["checks"]):
        report["status"] = "problems"
    _print(report)
    return EXIT_OK if report["status"] == "ok" else EXIT_CONNECTION


def cmd_check(args):
    config = _load(args)
    mailbox, _ = _mailbox(config)
    client, _ = _client(config, dry_run=True)
    state = state_module.read(config_module.state_path(config))
    run_dir = _runs_dir(config, "check")
    try:
        packet = loop.scan_mailbox(config, mailbox, client, state, run_dir,
                                   commit=False,
                                   max_bodies=int(config["policy"].get("body_fetch_limit_per_run") or 60),
                                   baseline=args.baseline)
    except mailcore.MailError as exc:
        _print({"status": "MAILBOX_ERROR", "error": str(exc)})
        return EXIT_CONNECTION
    (run_dir / "check.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    _print({"status": "DRY_RUN", "packet": str(run_dir / "check.json"),
            "summary": packet["summary"],
            "folders": {k: {kk: v.get(kk) for kk in ("new", "deferred", "coverage_proven",
                                                     "error", "baseline")}
                        for k, v in packet["folders"].items()},
            "note": "check 不写入任何内容（邮箱只读、Notion 不写、checkpoint 不动）"})
    return EXIT_OK


def cmd_scan(args):
    """The daily chain: scan + queue + request + apply any pending judgment."""
    config = _load(args)
    mailbox, _ = _mailbox(config)
    client, _ = _client(config)
    state = state_module.read(config_module.state_path(config))
    run_dir = _runs_dir(config, "scan")
    try:
        packet = loop.scan_mailbox(config, mailbox, client, state, run_dir,
                                   commit=True,
                                   max_bodies=int(config["policy"].get("body_fetch_limit_per_run") or 60),
                                   baseline=args.baseline)
    except mailcore.MailError as exc:
        _print({"status": "MAILBOX_ERROR", "error": str(exc)})
        return EXIT_CONNECTION
    # Queue every newly archived message for judgement, then publish the
    # request file so an agent (or the user) can judge it.
    queued = state_module.queue_judgment_messages(state, packet.get("new_messages", []))
    request, paths = _write_request(config, state)
    # If a judgment is already waiting and still current, apply it in the
    # same run (this is what makes the daily loop end-to-end).
    judgment_report = None
    if args.apply_judgment:
        judgment_report = _try_apply_pending_judgment(config, client, state, paths,
                                                      commit=True)
        if judgment_report and judgment_report.get("status") == "JUDGMENT_APPLIED":
            # re-publish the request: the queue changed
            _write_request(config, state)
    state_module.write(config_module.state_path(config), state)
    (run_dir / "packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    _print({"status": "SCANNED", "packet": str(run_dir / "packet.json"),
            "summary": packet["summary"],
            "queued_for_judgment": len(queued),
            "judgment_request": str(paths["request"]),
            "judgment_queue": len(state.get("judgmentQueue", [])),
            "judgment": judgment_report,
            "folders": {k: {kk: v.get(kk) for kk in ("new", "fetched", "archived",
                                                     "deferred", "failed", "advanced",
                                                     "coverage_proven", "error")}
                        for k, v in packet["folders"].items()}})
    return EXIT_OK


def cmd_judgment_request(args):
    """(Re)write the request file from the current queue; no mail access."""
    config = _load(args)
    state = state_module.read(config_module.state_path(config))
    request, paths = _write_request(config, state)
    _print({"status": "REQUEST_WRITTEN", "request": str(paths["request"]),
            "requestId": request["requestId"],
            "messages": len(request["messages"]),
            "open_tasks": len(request["open_tasks"]),
            "instructions": request["instructions"],
            "note": "把这个文件交给你的 Agent（或自己填写），把决定写成 "
                    "state/judgment.json，然后运行 apply --judgment"})
    return EXIT_OK


def cmd_apply_judgment(args):
    """Validate and persist a judgment document (dry-run by default)."""
    config = _load(args)
    state = state_module.read(config_module.state_path(config))
    path = Path(args.judgment)
    if not path.exists():
        _print({"status": "INPUT_MISSING", "expected": str(path)})
        return EXIT_FAIL
    request, _ = _write_request(config, state)
    try:
        document = judgment_module.load(path)
    except (OSError, json.JSONDecodeError) as exc:
        _print({"status": "JUDGMENT_UNREADABLE", "error": str(exc)[:300]})
        return EXIT_FAIL
    # Check "already applied" before the empty-queue check: re-running a
    # judgment that already landed should say so precisely, not report a
    # generic empty queue.
    if state_module.judgment_already_applied(state, document.get("basedOn")):
        _print({"status": "ALREADY_APPLIED", "basedOn": document.get("basedOn"),
                "note": "this judgment already landed; nothing to do"})
        return EXIT_OK
    if not request.get("messages"):
        _print({"status": "NOTHING_QUEUED",
                "note": "the judgment queue is empty — nothing to apply"})
        return EXIT_OK
    commit = bool(args.commit)
    client, _ = _client(config, dry_run=not commit)
    try:
        report = _apply_judgment_document(config, client, state, document, request, commit)
    except (loop.LoopError, tasks_module.TaskError) as exc:
        _print({"status": "REJECTED", "error": str(exc)})
        return EXIT_FAIL
    except NotionError as exc:
        _print({"status": "NOTION_ERROR", "error": str(exc)})
        return EXIT_CONNECTION
    if commit:
        state_module.write(config_module.state_path(config), state)
        _write_request(config, state)
    report["note"] = ("dry-run 未写入" if not commit else "已写入并逐项回读校验")
    _print(report)
    return EXIT_OK


def cmd_suggest(args):
    config = _load(args)
    state = state_module.read(config_module.state_path(config))
    packet_path = Path(args.input)
    if not packet_path.exists():
        _print({"status": "INPUT_MISSING", "expected": str(packet_path)})
        return EXIT_FAIL
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    summary = loop.suggest_candidates(config, packet, state)
    out = packet_path.parent / "suggestions.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _print({"status": "SUGGESTED", "output": str(out),
            "task_candidates": len(summary["tasks"]),
            "completion_candidates": len(summary["completions"]),
            "titles": [t["title"] for t in summary["tasks"]],
            "note": "建议尚未写入；先人工审阅，再运行 apply"})
    return EXIT_OK


def cmd_apply(args):
    config = _load(args)
    state = state_module.read(config_module.state_path(config))
    summary_path = Path(args.input)
    if not summary_path.exists():
        _print({"status": "INPUT_MISSING", "expected": str(summary_path),
                "hint": "point --input at a suggestions.json produced by `suggest`"})
        return EXIT_FAIL
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    commit = bool(args.commit)
    client, _ = _client(config, dry_run=not commit)
    try:
        results = loop.apply_candidates(config, client, summary, state, commit=commit)
    except (loop.LoopError, tasks_module.TaskError) as exc:
        _print({"status": "REJECTED", "error": str(exc)})
        return EXIT_FAIL
    except NotionError as exc:
        _print({"status": "NOTION_ERROR", "error": str(exc)})
        return EXIT_CONNECTION
    if commit:
        state_module.write(config_module.state_path(config), state)
    _print({"status": "APPLIED" if commit else "DRY_RUN", "results": results,
            "note": "dry-run 未写入任何内容" if not commit else "已写入并逐项回读校验"})
    return EXIT_OK


def cmd_complete(args):
    config = _load(args)
    state = state_module.read(config_module.state_path(config))
    payload = {"evidence": args.evidence, "source_locator": args.source,
               "evidence_date": args.date, "message_id": args.message_id}
    client, _ = _client(config, dry_run=not args.commit)
    record = state.get("tasks", {}).get(args.identity)
    gate = tasks_module.completion_gate(payload, record)
    if not gate["allowed"]:
        _print({"status": "REJECTED", "reasons": gate["reasons"]})
        return EXIT_FAIL
    try:
        schema, options = tasks_module.discover_properties(
            client, resolve_task_data_source(config, client))
        mapping, kinds, problems = tasks_module.resolve_mapping(config, schema)
        if problems:
            _print({"status": "REJECTED", "problems": problems})
            return EXIT_CONFIG
        outcome = loop.complete_task(client, config, mapping, kinds,
                                     {"identity": args.identity, **payload}, state,
                                     commit=bool(args.commit))
    except (NotionError, tasks_module.TaskError) as exc:
        _print({"status": "FAILED", "error": str(exc)})
        return EXIT_CONNECTION
    if args.commit:
        state_module.write(config_module.state_path(config), state)
    _print({"status": "COMPLETED" if args.commit else "DRY_RUN", "task": outcome})
    return EXIT_OK


def cmd_status(args):
    config = _load(args)
    state = state_module.read(config_module.state_path(config))
    _print(loop.summarize_state(config, state))
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nextmove", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="nextmove.config.json",
                        help="path to your config JSON (default: ./nextmove.config.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check config, secrets and connections")
    for name, help_text in (("check", "dry scan (no writes at all)"),
                            ("scan", "read + archive + queue + apply pending judgment")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--baseline", choices=["latest", "full"], default="latest",
                       help="first run only: start from newest (default) or import all")
    sub.choices["scan"].add_argument(
        "--apply-judgment", action="store_true",
        help="apply state/judgment.json in the same run when it is waiting")
    sub.add_parser("judgment-request",
                   help="(re)write state/judgment-request.json from the queue")
    p = sub.add_parser("apply", help="write tasks: judgment (--judgment) or mechanical (--input)")
    p.add_argument("--judgment", help="path to a judgment document")
    p.add_argument("--input", help="path to a mechanical suggestions.json")
    p.add_argument("--commit", action="store_true", help="actually write (default: dry-run)")
    p = sub.add_parser("complete", help="mark one task done with evidence")
    p.add_argument("--identity", required=True, help="task identity from the ledger")
    p.add_argument("--evidence", required=True, help="what was actually done")
    p.add_argument("--source", required=True, help="evidence URL")
    p.add_argument("--date", help="evidence date (YYYY-MM-DD)")
    p.add_argument("--message-id", help="evidence message id")
    p.add_argument("--commit", action="store_true", help="actually write (default: dry-run)")
    p = sub.add_parser("suggest", help="mechanical task suggestions from a packet")
    p.add_argument("--input", required=True, help="path to a scan packet.json")
    sub.add_parser("status", help="local checkpoint / queue / pending / task summary")

    args = parser.parse_args(argv)
    try:
        if args.command == "apply":
            if getattr(args, "judgment", None):
                return cmd_apply_judgment(args)
            if getattr(args, "input", None):
                return cmd_apply(args)
            _print({"status": "USAGE", "error": "apply needs --judgment or --input"})
            return EXIT_FAIL
        handler = {"doctor": cmd_doctor, "check": cmd_check, "scan": cmd_scan,
                   "judgment-request": cmd_judgment_request, "complete": cmd_complete,
                   "suggest": cmd_suggest, "status": cmd_status}[args.command]
        return handler(args)
    except config_module.ConfigError as exc:
        _print({"status": "CONFIG_INVALID", "error": str(exc)})
        return EXIT_CONFIG
    except KeyboardInterrupt:
        _print({"status": "interrupted"})
        return EXIT_FAIL


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
