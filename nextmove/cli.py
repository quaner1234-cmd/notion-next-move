"""CLI entry point: ``python -m nextmove <command>``.

Commands:
  doctor    check config, secrets and both connections (mail read-only,
            Notion token) and print exactly what is verified
  check     dry scan: what is new, what would be archived — writes nothing
  scan      read mail read-only, archive evidence under customer pages,
            advance checkpoints (this one commits)
  suggest   propose next-move tasks from a scan packet (no writes)
  apply     validate and write suggested tasks into your Notion database
  complete  mark a task done — only with real evidence
  status    checkpoint / pending / open-task summary from local state

Every command prints a JSON report and exits non-zero only on real failure.
Exit codes: 0 ok, 1 failure, 2 configuration problem, 3 connection problem.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as config_module
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
    state_module.write(config_module.state_path(config), state)
    (run_dir / "packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    _print({"status": "SCANNED", "packet": str(run_dir / "packet.json"),
            "summary": packet["summary"],
            "folders": {k: {kk: v.get(kk) for kk in ("new", "fetched", "archived",
                                                     "deferred", "failed", "advanced",
                                                     "coverage_proven", "error")}
                        for k, v in packet["folders"].items()}})
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
                            ("scan", "read + archive evidence (commits)")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--baseline", choices=["latest", "full"], default="latest",
                       help="first run only: start from newest (default) or import all")
    p = sub.add_parser("suggest", help="propose tasks from a packet")
    p.add_argument("--input", required=True, help="path to a scan packet.json")
    p = sub.add_parser("apply", help="write suggested tasks")
    p.add_argument("--input", required=True, help="path to suggestions.json")
    p.add_argument("--commit", action="store_true", help="actually write (default: dry-run)")
    p = sub.add_parser("complete", help="mark one task done with evidence")
    p.add_argument("--identity", required=True, help="task identity from the ledger")
    p.add_argument("--evidence", required=True, help="what was actually done")
    p.add_argument("--source", required=True, help="evidence URL")
    p.add_argument("--date", help="evidence date (YYYY-MM-DD)")
    p.add_argument("--message-id", help="evidence message id")
    p.add_argument("--commit", action="store_true", help="actually write (default: dry-run)")
    sub.add_parser("status", help="local checkpoint / pending / task summary")

    args = parser.parse_args(argv)
    handler = {"doctor": cmd_doctor, "check": cmd_check, "scan": cmd_scan,
               "suggest": cmd_suggest, "apply": cmd_apply, "complete": cmd_complete,
               "status": cmd_status}[args.command]
    try:
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
