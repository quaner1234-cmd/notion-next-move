#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Acceptance: one scheduled run produces an evidenced Next Move in Notion.

This drives the *CLI* (not the library) exactly as a cron job would, against
the in-process fakes. It proves the daily chain end to end:

  run 1  scan --apply-judgment   new mail -> archived -> queued -> request
                                 written; no judgment yet -> nothing else
  (judge) a judgment document is produced from the request (the "agent" step)
  run 2  scan --apply-judgment   same chain, plus the judgment applies:
                                 the task appears in Notion
  run 3  scan --apply-judgment   duplicate mail -> no second task, no second
                                 evidence block, judgment already applied
  run 4  scan --apply-judgment   fetch failure -> reported, checkpoint held
  run 5  scan --apply-judgment   unknown sender -> review queue, no task

Exit 0 only if every assertion holds. No network, no credentials.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_imap  # noqa: E402
import fake_notion  # noqa: E402
import mailbuilder  # noqa: E402
import run_tests  # noqa: E402
from nextmove import config as config_module  # noqa: E402
from nextmove import cli, judgment, loop, mailcore, state as state_module  # noqa: E402

BJ = timezone(timedelta(hours=8))
CHECKS = []


def check(label, condition, detail=None):
    CHECKS.append({"check": label, "ok": bool(condition), "detail": detail})
    marker = "ok " if condition else "FAIL"
    print(f"[{marker}] {label}" + (f" — {detail}" if detail and not condition else ""))
    return bool(condition)


def run_cli(harness, argv, expect_exit=0):
    code, report = run_tests._run_cli(cli, harness, argv, expect_exit=expect_exit)
    return code, report


def main():
    tmp = tempfile.mkdtemp(prefix="nextmove-accept-")
    harness = run_tests.LoopHarness.__new__(run_tests.LoopHarness)
    harness.tmp = tmp
    harness.config_path = run_tests.make_config(tmp)
    harness.config = config_module.load(harness.config_path)
    harness.notion = run_tests.make_notion()
    folders = mailbuilder.standard_folders()
    harness.server = fake_imap.start_fake_imap(folders)
    harness.state_path = config_module.state_path(harness.config)
    harness.run_dir = Path(tmp) / "runs" / "accept"
    harness.run_dir.mkdir(parents=True, exist_ok=True)
    try:
        alpha = mailbuilder.CUSTOMERS["alpha"]
        customer_page = harness.notion.create_page(
            harness.notion.data_source_id,
            {"任务名称": {"title": [{"type": "text", "text": {"content": "Alpha page"}}]}},
            children=[])
        # The CLI re-reads the config from disk, so the customer page id must
        # be persisted there (not just mutated in memory).
        on_disk = json.loads(harness.config_path.read_text(encoding="utf-8"))
        on_disk["customers"][0]["page_id"] = customer_page["id"]
        harness.config_path.write_text(json.dumps(on_disk, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        harness.config["customers"][0]["page_id"] = customer_page["id"]
        cfg = ["--config", str(harness.config_path)]

        # ---------------------------------------------------------------- run 1
        folders["INBOX"] = [run_tests.build_inbound(
            1, alpha, "Please quote 300 units", "<acc-1@alpha-trading.example>")]
        _, r1 = run_cli(harness, cfg + ["scan", "--apply-judgment", "--baseline", "full"])
        check("run1: mail archived", r1["summary"]["archived"] == 1, r1["summary"])
        check("run1: message queued for judgement", r1["queued_for_judgment"] == 1)
        check("run1: request file written", Path(r1["judgment_request"]).exists())
        check("run1: no judgment yet -> no task", len(harness.notion.pages) == 1,
              f"pages={len(harness.notion.pages)}")
        request = json.loads(Path(r1["judgment_request"]).read_text(encoding="utf-8"))
        check("run1: request lists the message with a locator",
              request["messages"] and request["messages"][0]["locator"],
              request["messages"])

        # -------------------------------------------------------- agent step
        judgment_path = Path(r1["judgment_request"]).parent / "judgment.json"
        judgment_path.write_text(json.dumps({
            "kind": "judgment", "basedOn": request["requestId"],
            "decisions": [{
                "decision": "create", "customer_key": "alpha",
                "message_id": "<acc-1@alpha-trading.example>",
                "title": "Alpha Trading｜核价 300 件并回复报价与交期",
                "action": "与工厂核价后，回复客户 300 件的报价区间与交期",
                "completion_standard": "报价邮件已发出且客户已收到（可在已发送核对）",
                "generatedBy": "acceptance-agent",
            }],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        # ---------------------------------------------------------------- run 2
        _, r2 = run_cli(harness, cfg + ["scan", "--apply-judgment"])
        jr = r2["judgment"] or {}
        check("run2: judgment applied", jr.get("status") == "JUDGMENT_APPLIED", jr)
        created = (jr.get("results", {}).get("created") or [{}])[0]
        check("run2: task created in Notion", created.get("action") == "created", created)
        task_id = created.get("page_id")
        title = harness.notion.page_titles().get(task_id, "")
        check("run2: title is the agent's judgement", "核价 300 件" in title, title)
        check("run2: status is the configured todo value",
              bool(task_id) and harness.notion.page_status(task_id) == "Todo",
              harness.notion.page_status(task_id) if task_id else "no task created")
        task_text = harness.notion.page_text(task_id) if task_id else ""
        check("run2: task carries evidence source", "来源" in task_text and "notion.so" in task_text)
        check("run2: queue drained", r2["judgment_queue"] == 0, r2["judgment_queue"])

        # ---------------------------------------------------------------- run 3
        pages_before = len(harness.notion.pages)
        blocks_before = len(harness.notion.blocks[customer_page["id"]])
        _, r3 = run_cli(harness, cfg + ["scan", "--apply-judgment"])
        check("run3: rerun archives nothing new", r3["summary"]["archived"] == 0, r3["summary"])
        check("run3: rerun creates no task", len(harness.notion.pages) == pages_before)
        check("run3: rerun adds no evidence blocks",
              len(harness.notion.blocks[customer_page["id"]]) == blocks_before)
        jr3 = r3["judgment"] or {}
        check("run3: applied judgment is not re-applied",
              jr3.get("status") in ("ALREADY_APPLIED", "NOTHING_QUEUED"), jr3)

        # ---------------------------------------------------------------- run 4
        folders["INBOX"].append(run_tests.build_inbound(
            2, alpha, "Second message", "<acc-2@alpha-trading.example>"))
        harness.server.drop_on_body_uid = 2
        _, r4 = run_cli(harness, cfg + ["scan", "--apply-judgment"])
        entry = r4["folders"]["inbox:INBOX"]
        check("run4: failure is reported", entry["failed"] == 1, entry)
        check("run4: checkpoint NOT advanced", entry["advanced"] is False, entry)
        state_after = state_module.read(harness.state_path)
        check("run4: checkpoint value unchanged in state",
              state_after["folders"]["inbox:INBOX"]["lastUid"] == 1,
              state_after["folders"]["inbox:INBOX"])
        check("run4: failed message not queued",
              all(q["message_id"] != "<acc-2@alpha-trading.example>"
                  for q in state_after.get("judgmentQueue", [])))
        harness.server.drop_on_body_uid = None

        # ---------------------------------------------------------------- run 5
        folders["INBOX"].append(mailbuilder.folder_message(
            3, mailbuilder.build_mail(
                mid="<acc-stranger@unknown.example>",
                frm=("Unknown Sender", "stranger@unknown.example"),
                to=[("Sales", "sales@company.example")],
                date=datetime.now(BJ).strftime("%a, %d %b %Y %H:%M:%S +0800"),
                subject="Cold outreach", text="We sell widgets."),
            datetime.now(BJ).strftime("%d-%b-%Y %H:%M:%S +0800")))
        _, r5 = run_cli(harness, cfg + ["scan", "--apply-judgment"])
        state5 = state_module.read(harness.state_path)
        pending = [p for p in state5["pending"]
                   if p.get("message_id") == "<acc-stranger@unknown.example>"]
        check("run5: unknown sender goes to review queue", len(pending) == 1, pending)
        check("run5: unknown sender creates no task",
              len(harness.notion.pages) == pages_before)
        check("run5: unknown sender is not in the judgment queue",
              all(q["message_id"] != "<acc-stranger@unknown.example>"
                  for q in state5.get("judgmentQueue", [])))

        # ---------------------------------------------------------------- run 6
        # recovery: the failed message is re-fetched on the next run and queued
        _, r6 = run_cli(harness, cfg + ["scan", "--apply-judgment"])
        state6 = state_module.read(harness.state_path)
        recovered = [q for q in state6.get("judgmentQueue", [])
                     if q["message_id"] == "<acc-2@alpha-trading.example>"]
        check("run6: previously failed message recovered and queued",
              len(recovered) == 1, state6.get("judgmentQueue"))
        request6 = json.loads(
            (Path(r6["judgment_request"])).read_text(encoding="utf-8"))
        check("run6: new request has a different id than run 1",
              request6["requestId"] != request["requestId"])
        # The unresolved stranger keeps the checkpoint held (honest blocking,
        # same as the verified production design) — that must not stop the
        # chain from processing new legitimate mail.
        check("run6: unresolved review item holds the checkpoint",
              state6["folders"]["inbox:INBOX"]["lastUid"] == 1,
              state6["folders"]["inbox:INBOX"])

        # ---------------------------------------------------------------- run 7
        folders["INBOX"].append(run_tests.build_inbound(
            4, alpha, "Third question", "<acc-3@alpha-trading.example>"))
        _, r7 = run_cli(harness, cfg + ["scan", "--apply-judgment"])
        state7 = state_module.read(harness.state_path)
        queued7 = [q for q in state7.get("judgmentQueue", [])
                   if q["message_id"] == "<acc-3@alpha-trading.example>"]
        check("run7: new mail still flows while a review item is open",
              r7["summary"]["archived"] == 1 and len(queued7) == 1,
              {"summary": r7["summary"], "queued": state7.get("judgmentQueue")})
        check("run7: the stranger stays in review, not in the judgment queue",
              any(p.get("message_id") == "<acc-stranger@unknown.example>"
                  for p in state7["pending"])
              and all(q["message_id"] != "<acc-stranger@unknown.example>"
                      for q in state7.get("judgmentQueue", [])))

    finally:
        harness.server.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    failures = [c for c in CHECKS if not c["ok"]]
    print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
    if failures:
        print(json.dumps(failures, ensure_ascii=False, indent=2))
        return 1
    print("ACCEPTANCE PASS")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
