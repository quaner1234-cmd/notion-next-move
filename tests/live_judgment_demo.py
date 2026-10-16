#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live judgment-chain demo against the REAL Notion API.

Proves the *new* daily chain end to end with a real write:
  scan -> queue -> judgment-request -> (agent writes judgment.json)
       -> validate -> apply -> readback in Notion -> idempotent re-apply

Mail side: in-process fake IMAP server (fictional .example customers).
Notion side: the real API, into the throwaway test area described by the
fixture (same format as live_notion_demo.py).

Run:  python tests/live_judgment_demo.py [--fixture PATH]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_imap  # noqa: E402
import mailbuilder  # noqa: E402
from nextmove import config as config_module  # noqa: E402
from nextmove import judgment, loop, mailcore, state as state_module  # noqa: E402
from nextmove.notion import NotionClient  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "_live_fixture.json"


def notion_token():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, "NOTION_TOKEN")[0]
    except ImportError:
        import os
        token = os.environ.get("NOTION_TOKEN")
        if not token:
            raise SystemExit("NOTION_TOKEN is not set in the environment")
        return token


def build_config(fixture, customer_page_id, workspace):
    config = {
        "mailbox": {"account": "demo", "host": "127.0.0.1", "port": 1,
                    "user": "sales@demo.example", "password_env": "X",
                    "inbox_folder": "INBOX", "sent_folder": "已发送"},
        "notion": {"notion_version": "2025-09-03",
                   "task_database_id": fixture["database"],
                   "task_data_source_id": fixture["data_source"],
                   "properties": {"title": "任务名称", "status": "状态",
                                  "key": "任务标识", "source_locator": "来源链接",
                                  "customer": "客户"},
                   "status_map": {"todo": "Not started", "in_progress": "In progress",
                                  "done": "Done", "archived": "Done"}},
        "customers": [{"key": "alpha", "display_name": "Alpha Trading",
                       "email_addresses": ["buyer@alpha-trading.example"],
                       "email_domains": ["alpha-trading.example"],
                       "page_id": customer_page_id}],
        "policy": {"timezone": "+08:00", "body_fetch_limit_per_run": 20,
                   "state_dir": "state", "runs_dir": "runs",
                   "secrets_file": "secrets.json",
                   "title_template": "{customer}｜{action}"},
    }
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "nextmove.config.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_module.load(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default=str(FIXTURE))
    args = parser.parse_args()
    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        print(f"fixture missing: {fixture_path}")
        return 1
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    client = NotionClient(notion_token(), version="2025-09-03")

    workspace = Path(tempfile.mkdtemp(prefix="nextmove-live-judgment-"))
    config = build_config(fixture, fixture["customer_page"], workspace)
    state_path = config_module.state_path(config)
    run_dir = workspace / "runs" / "demo"
    run_dir.mkdir(parents=True, exist_ok=True)
    checks = []

    def check(label, ok, detail=None):
        checks.append({"check": label, "ok": bool(ok), "detail": detail})
        print(f"[{'ok ' if ok else 'FAIL'}] {label}")

    server = fake_imap.start_fake_imap(mailbuilder.standard_folders())
    try:
        alpha = mailbuilder.CUSTOMERS["alpha"]
        import mailbuilder as mb
        from datetime import datetime, timedelta, timezone
        bj = timezone(timedelta(hours=8))

        # Make the demo re-runnable: trash any task this demo created earlier
        # (identified by the deterministic task key it writes into the
        # database), so the run starts from a clean slate. Only pages this
        # demo owns are touched.
        from nextmove import plan as plan_module
        demo_identity = plan_module.task_identity(
            "alpha", "<live-judg-1@alpha-trading.example>")
        existing = client.query_data_source(fixture["data_source"], payload={})
        stale = []
        for page in existing:
            for prop in (page.get("properties") or {}).values():
                if prop.get("type") == "rich_text":
                    text = "".join(t.get("plain_text", "")
                                   for t in (prop.get("rich_text") or []))
                    if text.startswith(demo_identity):
                        stale.append(page["id"])
        for page_id in stale:
            client.call("PATCH", f"/v1/pages/{page_id}", {"in_trash": True})
        check("stale demo tasks cleaned", True,
              {"trashed": len(stale), "identity": demo_identity})

        server.folders["INBOX"].append(mb.folder_message(
            1, mb.build_mail(
                mid="<live-judg-1@alpha-trading.example>",
                frm=("Buyer", "buyer@alpha-trading.example"),
                to=[("Sales", "sales@demo.example")],
                date=datetime.now(bj).strftime("%a, %d %b %Y %H:%M:%S +0800"),
                subject="Quote 300 units please", text="Please quote 300 units."),
            datetime.now(bj).strftime("%d-%b-%Y %H:%M:%S +0800")))

        # 1. scan (library level, same code path as the CLI)
        state = state_module.read(state_path)
        box = mailcore.Mailbox("127.0.0.1", server.port, config["mailbox"]["user"],
                               "fake", timeout=10, use_ssl=False)
        packet = loop.scan_mailbox(config, box, client, state, run_dir,
                                   commit=True,
                                   max_bodies=int(config["policy"].get(
                                       "body_fetch_limit_per_run") or 60),
                                   baseline="full")
        queued = state_module.queue_judgment_messages(state, packet["new_messages"])
        check("scan archived the message", packet["summary"]["archived"] == 1,
              packet["summary"])
        check("message queued for judgement", len(queued) == 1, queued)
        request = loop.build_judgment_request(
            config, {"new_messages": state["judgmentQueue"]}, state)
        request["requestId"] = judgment.request_id(request)
        check("request carries the archived locator",
              bool(request["messages"][0]["locator"]),
              request["messages"][0].get("locator"))

        # 2. the agent writes a judgment (this is what an agent/user does)
        document = {"kind": "judgment", "basedOn": request["requestId"],
                    "decisions": [{
                        "decision": "create", "customer_key": "alpha",
                        "message_id": "<live-judg-1@alpha-trading.example>",
                        "title": "Alpha Trading｜核价 300 件并回复报价与交期",
                        "action": "与工厂核价后回复客户报价区间与交期",
                        "completion_standard": "报价邮件已发出且客户已收到",
                        "generatedBy": "live-demo-agent",
                    }]}
        validation = judgment.validate(document, request, config, state)
        check("judgment validates", validation["ok"], validation.get("error"))

        # 3. apply for real (REAL Notion writes)
        results = loop.apply_judgment(config, client, validation, state, commit=True)
        created = (results.get("created") or [{}])[0]
        check("task created in Notion", created.get("action") == "created", created)
        task_id = created.get("page_id")
        if task_id:
            page = client.call("GET", f"/v1/pages/{task_id}")
            title = "".join(t.get("plain_text", "")
                            for t in page["properties"]["任务名称"]["title"])
            status = (page["properties"]["状态"].get("status") or {}).get("name")
            url = page["properties"].get("来源链接", {}).get("url")
            check("readback: title is the agent's judgement",
                  "核价 300 件" in title, title)
            check("readback: status is Not started", status == "Not started", status)
            check("readback: source link points at the evidence", bool(url), url)

        # 4. dequeue + idempotent re-apply
        landed = {"created", "updated", "duplicate", "completed", "appended", "held"}
        state_module.mark_judged(
            state, [o["message_id"] for o in results["outcomes"]
                    if o["action"] in landed], request["requestId"])
        check("queue drained", state["judgmentQueue"] == [], state["judgmentQueue"])
        check("judgment recorded as applied",
              state_module.judgment_already_applied(state, request["requestId"]))
        again = loop.apply_judgment(config, client, validation, state, commit=True)
        landed_again = [o for o in again.get("created", [])
                        if o.get("action") in ("created", "updated")]
        check("re-apply produced no duplicate task", not landed_again,
              again.get("created"))
    finally:
        server.stop()
        shutil.rmtree(workspace, ignore_errors=True)

    failures = [c for c in checks if not c["ok"]]
    print(f"\n{len(checks) - len(failures)}/{len(checks)} checks passed")
    print(json.dumps({"result": "PASS" if not failures else "FAIL",
                      "checks": checks}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
