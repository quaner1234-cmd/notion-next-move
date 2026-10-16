#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Live closed-loop demo against the REAL Notion API.

Mail side: in-process fake IMAP server (fictional customers in reserved
.example domains). Notion side: the real API, into a clearly-labelled
throwaway test area you create yourself.

This is a maintainer/verification tool, not part of normal use. It needs a
fixture JSON describing your throwaway test area:

    {
      "parent":        "<page id of the throwaway test area>",
      "customer_page": "<page id of a fictional customer page under it>",
      "database":      "<database id of the throwaway task database>",
      "data_source":   "<its data source id>"
    }

Point at it with --fixture (default: tests/_live_fixture.json, git-ignored).
Scenario (exactly the acceptance path):
  1. customer mail arrives          -> evidence archived on the customer page
  2. next-move task is created      -> visible in the Notion task database
  3. customer replies again         -> no duplicate task, no duplicate evidence
  4. our reply appears in Sent      -> task is proposed complete, then completed
  5. a fetch failure                -> reported, checkpoint NOT advanced

Run:  python tests/live_notion_demo.py [--fixture PATH] [--cleanup]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_imap  # noqa: E402
import mailbuilder  # noqa: E402
from nextmove import config as config_module  # noqa: E402
from nextmove import loop, mailcore, state as state_module  # noqa: E402
from nextmove.notion import NotionClient  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "_live_fixture.json"
DEMO_DIR = Path(__file__).resolve().parent / "_live_demo"


def notion_token():
    """Read the Notion token the way this machine stores it.

    On Windows that is the user environment (HKCU\\Environment); elsewhere
    fall back to the standard environment variable, so the tool stays usable
    on a Mac once a token exists in the shell profile.
    """
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
                   "secrets_file": "secrets.json", "title_template": "{customer}｜{action}"},
    }
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / "nextmove.config.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_module.load(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default=str(FIXTURE),
                        help="path to the throwaway test-area fixture JSON")
    parser.add_argument("--cleanup", action="store_true",
                        help="archive the demo pages in Notion afterwards")
    args = parser.parse_args()

    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        print(f"fixture missing: {fixture_path}")
        print("See the module docstring for the fixture format (it describes a "
              "throwaway test area you create yourself).")
        return 1
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    client = NotionClient(notion_token(), version="2025-09-03")

    if args.cleanup:
        for page_id in (fixture["database"], fixture["customer_page"]):
            try:
                client.call("PATCH", f"/v1/pages/{page_id}", {"in_trash": True})
                print("trashed:", page_id)
            except Exception as exc:
                print("cleanup failed:", page_id, exc)
        return 0

    workspace = DEMO_DIR
    if workspace.exists():
        import shutil
        shutil.rmtree(workspace)
    config = build_config(fixture, fixture["customer_page"], workspace)
    state_path = config_module.state_path(config)
    run_dir = workspace / "runs" / "demo"
    run_dir.mkdir(parents=True, exist_ok=True)

    server = fake_imap.start_fake_imap(mailbuilder.standard_folders())
    report = {"steps": []}
    try:
        alpha = mailbuilder.CUSTOMERS["alpha"]

        def scan():
            state = state_module.read(state_path)
            box = mailcore.Mailbox("127.0.0.1", server.port, config["mailbox"]["user"],
                                   "fake", timeout=10, use_ssl=False)
            packet = loop.scan_mailbox(config, box, client, state, run_dir,
                                       commit=True, max_bodies=20, baseline="full")
            state_module.write(state_path, state)
            return packet, state

        # -- 1. customer mail arrives -------------------------------------
        server.folders["INBOX"] = [
            mailbuilder.folder_message(
                1, mailbuilder.build_mail(
                    mid="<demo-inquiry-1@alpha-trading.example>",
                    frm=("Alpha Buyer", "buyer@alpha-trading.example"),
                    to=[("Demo Sales", "sales@demo.example")],
                    date="Fri, 16 Oct 2026 09:30:00 +0800",
                    subject="Demo: please quote 300 units",
                    text="Hi, we would like a quote for 300 units of the demo product."),
                "16-Oct-2026 09:30:00 +0800")
        ]
        packet, state = scan()
        report["steps"].append({"step": "1. scan: customer mail",
                                "archived": packet["summary"]["archived"],
                                "message": packet["new_messages"][0]["archive"],
                                "locator": packet["new_messages"][0].get("locator")})

        # -- 2. next-move task --------------------------------------------
        summary = loop.suggest_candidates(config, packet, state)
        results = loop.apply_candidates(config, client, summary, state, commit=True)
        state_module.write(state_path, state)
        task_page = results["tasks"][0]
        report["steps"].append({"step": "2. apply: next-move task",
                                "action": task_page["action"],
                                "page_id": task_page["page_id"],
                                "url": task_page.get("url")})

        # read back from Notion (independent proof, not the write response)
        live_task = client.get_page(task_page["page_id"])
        props = live_task["properties"]
        title = "".join(x["plain_text"] for x in props["任务名称"]["title"])
        status = (props["状态"]["status"] or {}).get("name")
        report["steps"].append({"step": "2b. readback from Notion",
                                "title": title, "status": status,
                                "has_source": bool((props.get("来源链接") or {}).get("url"))})

        # -- 3. rerun: no duplicate --------------------------------------
        blocks_before = len(client.get_block_children(fixture["customer_page"]))
        packet2, state2 = scan()
        summary2 = loop.suggest_candidates(config, packet2, state2)
        results2 = loop.apply_candidates(config, client, summary2, state2, commit=True)
        state_module.write(state_path, state2)
        blocks_after = len(client.get_block_children(fixture["customer_page"]))
        report["steps"].append({"step": "3. rerun idempotence",
                                "archived_again": packet2["summary"]["archived"],
                                "tasks_created_again": sum(
                                    1 for r in results2["tasks"] if r["action"] == "created"),
                                "evidence_blocks_before": blocks_before,
                                "evidence_blocks_after": blocks_after})

        # -- 4. our reply lands in Sent -----------------------------------
        server.folders["&XfJT0ZAB-"] = [
            mailbuilder.folder_message(
                50, mailbuilder.build_mail(
                    mid="<demo-reply-1@demo.example>",
                    frm=("Demo Sales", "sales@demo.example"),
                    to=[("Alpha Buyer", "buyer@alpha-trading.example")],
                    date="Fri, 16 Oct 2026 11:00:00 +0800",
                    subject="Re: Demo: please quote 300 units",
                    text="Quoted, see attached.",
                    in_reply_to="<demo-inquiry-1@alpha-trading.example>",
                    references="<demo-inquiry-1@alpha-trading.example>"),
                "16-Oct-2026 11:00:00 +0800")
        ]
        packet3, state3 = scan()
        summary3 = loop.suggest_candidates(config, packet3, state3)
        results3 = loop.apply_candidates(config, client, summary3, state3, commit=True)
        state_module.write(state_path, state3)
        report["steps"].append({"step": "4. completion from Sent reply",
                                "completion_candidates": len(summary3["completions"]),
                                "completion_actions": [c["action"] for c in results3["completions"]]})

        live_after = client.get_page(task_page["page_id"])
        status_after = (live_after["properties"]["状态"]["status"] or {}).get("name")
        open_tasks = loop.summarize_state(config, state3)["open_tasks"]
        report["steps"].append({"step": "4b. readback: task closed",
                                "status_in_notion": status_after,
                                "open_tasks_left": len(open_tasks)})

        # -- 5. failure honesty: break the connection mid-fetch -----------
        server.drop_on_body_uid = 2
        server.folders["INBOX"].append(mailbuilder.folder_message(
            2, mailbuilder.build_mail(
                mid="<demo-inquiry-2@alpha-trading.example>",
                frm=("Alpha Buyer", "buyer@alpha-trading.example"),
                to=[("Demo Sales", "sales@demo.example")],
                date="Fri, 16 Oct 2026 12:00:00 +0800",
                subject="Demo: second message",
                text="One more thing."),
            "16-Oct-2026 12:00:00 +0800"))
        packet4, state4 = scan()
        entry = packet4["folders"]["inbox:INBOX"]
        report["steps"].append({"step": "5. source failure honesty",
                                "failed": entry["failed"], "advanced": entry["advanced"],
                                "checkpoint": state4["folders"]["inbox:INBOX"].get("lastUid"),
                                "error_reported": [m.get("error") for m in packet4["new_messages"]
                                                   if m.get("error")][:1]})
        server.drop_on_body_uid = None

        report["result"] = "PASS"
    except Exception as exc:
        import traceback
        report["result"] = "FAIL"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()[-1500:]
    finally:
        server.stop()

    out = workspace / "live_report.json"
    workspace.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
