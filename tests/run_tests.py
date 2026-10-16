#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline acceptance suite for notion-next-move.

Everything runs against in-process fakes: a fake IMAP server (real imaplib
client) and a fake Notion API (the real NotionClient). No network, no real
mailbox, no real Notion. All data is fictional.

Run:  python tests/run_tests.py        (any Python >= 3.10)
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nextmove import config as config_module            # noqa: E402
from nextmove import loop, mailcore, plan as plan_module  # noqa: E402
from nextmove import state as state_module, tasks as tasks_module  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_notion                                        # noqa: E402
import fake_imap                                          # noqa: E402
import mailbuilder                                        # noqa: E402

BJ = timezone(timedelta(hours=8))
TODAY = datetime(2026, 10, 16, 10, 0, tzinfo=BJ)


def stamp(days_ago=0, hour=9):
    return (TODAY - timedelta(days=days_ago)).replace(hour=hour,
                                                      minute=30).strftime("%a, %d %b %Y %H:%M:%S +0800")


def make_config(tmp, **overrides):
    base = {
        "mailbox": {"account": "work", "host": "127.0.0.1", "port": 1,
                    "user": "sales@company.example", "password_env": "NPM_TEST_IMAP",
                    "inbox_folder": "INBOX", "sent_folder": "已发送"},
        "notion": {
            "token_env": "NPM_TEST_NOTION",
            "task_database_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "owner_user_id": None,
            "properties": {"title": "任务名称", "status": "状态", "key": "任务标识",
                           "source_locator": "来源链接"},
            "status_map": {"todo": "Todo", "in_progress": "In progress",
                           "done": "Done", "archived": "Archived"},
        },
        "customers": [mailbuilder.CUSTOMERS["alpha"], mailbuilder.CUSTOMERS["bravo"]],
        "policy": {"timezone": "+08:00", "body_fetch_limit_per_run": 60,
                   "state_dir": "state", "runs_dir": "runs",
                   "secrets_file": "secrets.json"},
    }
    merged = {**base, **overrides}
    path = Path(tmp) / "nextmove.config.json"
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    secrets = Path(tmp) / "secrets.json"
    secrets.write_text(json.dumps({"imap_password": "fake-password",
                                   "notion_token": "fake-token"}), encoding="utf-8")
    return path


SCHEMA = {"任务名称": "title", "状态": "status", "任务标识": "rich_text",
          "来源链接": "url", "客户": "rich_text", "截止日期": "date"}


def make_notion():
    return fake_notion.FakeNotion(data_source_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                  schema=SCHEMA,
                                  status_options={"Todo", "In progress", "Done", "Archived"})


def build_inbound(uid, customer, subject, mid, days_ago=0, text=None):
    address = customer["email_addresses"][0]
    raw = mailbuilder.build_mail(
        mid=mid, frm=(customer["display_name"], address),
        to=[("Sales Rep", "sales@company.example")],
        date=stamp(days_ago), subject=subject,
        text=text or f"Hello, please advise on {subject}. Thanks.")
    return mailbuilder.folder_message(uid, raw, stamp(days_ago).replace(",", ""))


def build_outbound(uid, to_address, subject, mid, in_reply_to, days_ago=0):
    raw = mailbuilder.build_mail(
        mid=mid, frm=("Sales Rep", "sales@company.example"),
        to=[("Customer", to_address)],
        date=stamp(days_ago), subject=subject, text="Reply body.",
        in_reply_to=in_reply_to, references=in_reply_to)
    return mailbuilder.folder_message(uid, raw, stamp(days_ago).replace(",", ""))


class LoopHarness:
    """Full-loop harness: fake mailbox + fake Notion + real loop code."""

    def __init__(self, folders, **imap_behaviors):
        self.tmp = tempfile.mkdtemp(prefix="nextmove-test-")
        self.config_path = make_config(self.tmp)
        self.config = config_module.load(self.config_path)
        self.config["mailbox"]["port"] = 993  # fake server ignores it
        self.notion = make_notion()
        self.server = fake_imap.start_fake_imap(folders, **imap_behaviors)
        self.state_path = config_module.state_path(self.config)
        self.run_dir = Path(self.tmp) / "runs" / "test"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def close(self):
        self.server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mailbox(self):
        # use_ssl=False only because this is the in-process test server.
        box = mailcore.Mailbox("127.0.0.1", self.server.port, self.config["mailbox"]["user"],
                               "fake-password", timeout=10, use_ssl=False)
        return box

    def scan(self, commit=True, baseline="full"):
        state = state_module.read(self.state_path)
        with self.mailbox() as box:
            packet = loop.scan_mailbox(self.config, box, self.notion, state, self.run_dir,
                                       commit=commit, max_bodies=60, baseline=baseline)
        if commit:
            state_module.write(self.state_path, state)
        return packet, state

    def apply(self, packet, state, commit=True):
        summary = loop.suggest_candidates(self.config, packet, state)
        results = loop.apply_candidates(self.config, self.notion, summary, state, commit=commit)
        if commit:
            state_module.write(self.state_path, state)
        return summary, results


# ===========================================================================


class PlanUnit(unittest.TestCase):
    """Pure planning logic: coverage, dedupe, identity, no-guess rules."""

    def test_same_checkpoint_no_new_mail_is_covered_and_empty(self):
        cp = {"lastUid": 10, "lastMessageId": "<m10@x>", "lastUidValidity": "9"}
        headers = [{"uid": i, "message_id": f"<m{i}@x>"} for i in range(1, 11)]
        p = plan_module.plan_folder_scan(headers, cp, {}, "9")
        self.assertTrue(p["coverage_proven"])
        self.assertEqual(p["new_headers"], [])

    def test_more_than_100_and_old_dated_late_arrival_are_new(self):
        cp = {"lastUid": 100, "lastMessageId": "<m100@x>", "lastUidValidity": "9"}
        headers = [{"uid": i, "message_id": f"<m{i}@x>"} for i in range(1, 261)]
        headers[0]["date_header"] = "Tue, 01 Aug 2006 09:00:00 +0800"  # older date, higher UID
        p = plan_module.plan_folder_scan(headers, cp, {}, "9")
        self.assertTrue(p["coverage_proven"])
        self.assertEqual(len(p["new_headers"]), 160)

    def test_duplicate_ids_collapse_and_known_ids_are_skipped(self):
        cp = {"lastUid": 1, "lastMessageId": "<a@x>", "lastUidValidity": "9"}
        headers = [
            {"uid": 1, "message_id": "<a@x>"},
            {"uid": 2, "message_id": "<same@a.example>"},
            {"uid": 3, "message_id": "<same@A.EXAMPLE>"},  # same id, domain case only
            {"uid": 4, "message_id": "<known@x>"},
        ]
        p = plan_module.plan_folder_scan(headers, cp, {"known@x": {}}, "9")
        self.assertEqual([h["uid"] for h in p["new_headers"]], [2])
        self.assertEqual(p["duplicates"], [3])

    def test_uid_epoch_change_requires_full_inventory_and_reanchors_by_id(self):
        cp = {"lastUid": 500, "lastMessageId": "<anchor@x>", "lastUidValidity": "9"}
        headers = [{"uid": 15, "message_id": "<anchor@x>"},
                   {"uid": 16, "message_id": "<new@x>"}]
        p = plan_module.plan_folder_scan(headers, cp, {}, "10", complete=False)
        self.assertFalse(p["coverage_proven"])
        p = plan_module.plan_folder_scan(headers, cp, {}, "10", complete=True)
        self.assertTrue(p["coverage_proven"])
        self.assertTrue(p["epoch_changed"])
        self.assertEqual([h["uid"] for h in p["new_headers"]], [16])

    def test_lost_anchor_never_advances_coverage(self):
        cp = {"lastUid": 2000, "lastMessageId": "<gone@x>", "lastUidValidity": "9"}
        p = plan_module.plan_folder_scan([{"uid": 1, "message_id": "<a@x>"}], cp, {}, "9")
        self.assertFalse(p["coverage_proven"])
        self.assertIn("checkpoint anchor not found — full header reconciliation required",
                      p["gaps"])

    def test_message_without_id_is_still_new_and_flagged(self):
        cp = {"lastUid": 5, "lastMessageId": "<m5@x>", "lastUidValidity": "9"}
        headers = [{"uid": 5, "message_id": "<m5@x>"},
                   {"uid": 6, "message_id": ""}]
        p = plan_module.plan_folder_scan(headers, cp, {}, "9")
        self.assertEqual([h["uid"] for h in p["new_headers"]], [6])
        self.assertTrue(any("no Message-ID" in gap for gap in p["gaps"]))

    def test_message_id_normalization_keeps_local_case(self):
        self.assertEqual(plan_module.normalize_message_id(" <A@EXAMPLE.COM> "), "A@example.com")
        self.assertNotEqual(plan_module.normalize_message_id("<a@x>"),
                            plan_module.normalize_message_id("<A@x>"))

    def test_resolve_sender_prefers_from_then_reply_to_then_sender(self):
        aliases = {"known@x.example": "k"}
        self.assertEqual(plan_module.resolve_sender({"from": "known@x.example"}, aliases),
                         ("known@x.example", "from"))
        self.assertEqual(
            plan_module.resolve_sender({"from": "other@y.example",
                                        "reply_to": "known@x.example"}, aliases),
            ("known@x.example", "reply-to"))
        self.assertEqual(
            plan_module.resolve_sender({"from": "", "sender": "known@x.example"}, aliases),
            ("known@x.example", "sender"))

    def test_customer_resolution_never_guesses(self):
        lookup = lambda addr: {"a@x.example": "alpha"}.get(addr)  # noqa: E731
        self.assertEqual(plan_module.resolve_customer("a@x.example", "from", set(), lookup),
                         ("alpha", "address (from)"))
        key, reason = plan_module.resolve_customer("stranger@z.example", "from", set(), lookup)
        self.assertIsNone(key)
        self.assertEqual(reason, "unknown sender address")
        # thread wins only when exactly one customer is in the thread
        self.assertEqual(plan_module.resolve_customer(None, "none", {"bravo"}, lookup),
                         ("bravo", "thread"))
        key, reason = plan_module.resolve_customer(None, "none", {"alpha", "bravo"}, lookup)
        self.assertIsNone(key)
        self.assertIn("conflict", reason)

    def test_task_identity_is_stable_and_unique_per_message(self):
        first = plan_module.task_identity("alpha", "<m1@x>")
        again = plan_module.task_identity("alpha", "<m1@x>")
        other = plan_module.task_identity("alpha", "<m2@x>")
        other_customer = plan_module.task_identity("bravo", "<m1@x>")
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, other_customer)

    def test_task_problems_reject_placeholders_and_empty_status(self):
        config = {"customers": [{"key": "alpha"}]}
        base = {"title": "TODO", "status": "", "identity": "x",
                "customer_key": "alpha", "source_locator": ""}
        problems = plan_module.task_problems(base, config)
        joined = "; ".join(problems)
        self.assertIn("TODO", joined)
        self.assertIn("status literally empty", joined)
        self.assertIn("source locator", joined)


class ReadOnlyContract(unittest.TestCase):
    """The client can only read mail; only safe Notion operations exist."""

    def test_mailcore_exposes_no_mutating_imap_commands(self):
        source = (ROOT / "nextmove" / "mailcore.py").read_text(encoding="utf-8")
        for forbidden in ("\".store(", "\".delete(", "\".copy(", "\".move(",
                          "set_flags", "expunge"):
            self.assertNotIn(forbidden, source, f"forbidden IMAP command in mailcore: {forbidden}")
        self.assertIn("readonly=True", source)

    def test_notion_client_has_no_delete_or_archive_path(self):
        source = (ROOT / "nextmove" / "notion.py").read_text(encoding="utf-8")
        for forbidden in ("DELETE", "in_trash", "\"archived\": True", "archive_page"):
            self.assertNotIn(forbidden, source, f"forbidden Notion operation: {forbidden}")

    def test_dry_run_client_records_writes_but_sends_none(self):
        client = tasks_safe_dry_client()
        client.append_children("page", [{"object": "block", "type": "paragraph",
                                         "paragraph": {"rich_text": []}}])
        self.assertEqual(len(client.planned_writes), 1)


def tasks_safe_dry_client():
    from nextmove.notion import NotionClient
    return NotionClient("fake", dry_run=True)


class LoopEndToEnd(unittest.TestCase):
    """The full closed loop against fakes, all data fictional."""

    def setUp(self):
        self.folders = mailbuilder.standard_folders()
        self.h = LoopHarness(self.folders)
        self.addCleanup(self.h.close)

    # -- helpers ------------------------------------------------------------

    def seed_customer_page(self, customer_key, title="Customer page"):
        page = self.h.notion.create_page(
            self.h.notion.data_source_id,
            {"任务名称": {"title": [{"type": "text", "text": {"content": title}}]}},
            children=[])
        return page["id"]

    # -- the closed loop ----------------------------------------------------

    def test_full_loop_customer_change_to_next_move_in_notion(self):
        """Customer email -> evidence archived -> task proposed -> applied."""
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [build_inbound(1, alpha, "New order request",
                                               "<alpha-1@alpha-trading.example>")]
        packet, state = self.h.scan(commit=True)
        self.assertEqual(packet["summary"]["archived"], 1)
        self.assertTrue(packet["new_messages"][0]["archive_verified"])
        self.assertIn("New order request", self.h.notion.page_text(page_id))
        locator = packet["new_messages"][0]["locator"]
        self.assertTrue(locator.startswith("https://www.notion.so/"))

        summary, results = self.h.apply(packet, state, commit=True)
        self.assertEqual(len(summary["tasks"]), 1)
        self.assertEqual(results["tasks"][0]["action"], "created")
        task_id = results["tasks"][0]["page_id"]
        self.assertIn("Alpha Trading", self.h.notion.page_titles()[task_id])
        self.assertEqual(self.h.notion.page_status(task_id), "Todo")
        self.assertIn("来源", self.h.notion.page_text(task_id))

    def test_rerun_creates_no_duplicate_task_and_no_duplicate_evidence(self):
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [build_inbound(1, alpha, "Quote please",
                                               "<alpha-2@alpha-trading.example>")]
        packet, state = self.h.scan(commit=True)
        self.h.apply(packet, state, commit=True)
        first_blocks = len(self.h.notion.blocks[page_id])
        first_tasks = len(self.h.notion.pages)

        # Re-run the exact same scan+apply: nothing new anywhere.
        packet2, state2 = self.h.scan(commit=True)
        self.assertEqual(packet2["summary"]["archived"], 0)
        self.assertEqual(len(self.h.notion.blocks[page_id]), first_blocks,
                         "rerun duplicated evidence")
        summary2, results2 = self.h.apply(packet2, state2, commit=True)
        self.assertEqual(len(self.h.notion.pages), first_tasks, "rerun duplicated a task")
        self.assertEqual([t["action"] for t in results2["tasks"] if t["action"] == "created"], [])

    def test_duplicate_task_is_detected_even_when_key_property_is_missing(self):
        """Title-based dedupe path: existing card survives, no second card."""
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [build_inbound(1, alpha, "Sample request",
                                               "<alpha-3@alpha-trading.example>")]
        packet, state = self.h.scan(commit=True)
        summary, results = self.h.apply(packet, state, commit=True)
        created = results["tasks"][0]["page_id"]

        # Same message again in a fresh state (worst case: state lost).
        state2 = state_module.read(self.h.state_path)
        state2["tasks"] = {}
        summary2 = loop.suggest_candidates(self.h.config, packet, state2)
        results2 = loop.apply_candidates(self.h.config, self.h.notion, summary2, state2,
                                         commit=True)
        self.assertEqual(results2["tasks"][0]["action"], "duplicate")
        self.assertEqual(results2["tasks"][0]["page_id"], created)
        self.assertEqual(len([p for p in self.h.notion.pages.values()
                              if "Sample request" in fake_notion._title(p)]), 1)

    def test_source_failure_reports_gap_and_never_advances(self):
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [
            build_inbound(1, alpha, "Fine", "<alpha-4@alpha-trading.example>"),
            build_inbound(2, alpha, "Will fail", "<alpha-5@alpha-trading.example>"),
        ]
        self.h.server.drop_on_body_uid = 2
        packet, state = self.h.scan(commit=True)
        entry = packet["folders"]["inbox:INBOX"]
        self.assertEqual(entry["deferred"] + entry["failed"], 1)
        self.assertFalse(entry["advanced"], "checkpoint advanced despite a failed fetch")
        self.assertEqual(state["folders"]["inbox:INBOX"].get("lastUid", 0), 0)
        failed = [m for m in packet["new_messages"] if m.get("error")]
        self.assertEqual(len(failed), 1)
        self.assertIn("正文读取失败", failed[0]["error"])

    def test_notion_write_failure_keeps_checkpoint_and_reports(self):
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [build_inbound(1, alpha, "Archive fails",
                                               "<alpha-6@alpha-trading.example>")]
        self.h.notion.fail_writes = 1
        packet, state = self.h.scan(commit=True)
        failed = [m for m in packet["new_messages"] if m.get("error")]
        self.assertEqual(len(failed), 1, "a failed Notion write was reported as success")
        self.assertNotEqual(failed[0].get("archive_verified"), True)
        self.assertEqual(state["folders"]["inbox:INBOX"].get("lastUid", 0), 0,
                         "checkpoint advanced after a failed write")

    def test_unknown_sender_goes_to_review_not_to_a_customer(self):
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        raw = mailbuilder.build_mail(
            mid="<stranger@unknown.example>", frm=("Unknown", "stranger@unknown.example"),
            to=[("Sales", "sales@company.example")], date=stamp(0),
            subject="Cold outreach", text="We sell widgets.")
        self.folders["INBOX"] = [mailbuilder.folder_message(1, raw, stamp(0).replace(",", ""))]
        packet, state = self.h.scan(commit=True)
        message = packet["new_messages"][0]
        self.assertTrue(message["pending_review"])
        self.assertEqual(message["customer_key"], None)
        self.assertIn("unknown sender", message["resolution"])
        self.assertEqual(state["pending"][0]["message_id"], "<stranger@unknown.example>")
        task_pages = [p for p in self.h.notion.pages.values()
                      if "Cold outreach" in fake_notion._title(p)]
        self.assertEqual(task_pages, [], "unknown sender created a task")

    def test_sent_reply_completes_the_open_reply_task(self):
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [build_inbound(1, alpha, "Need pricing",
                                               "<alpha-7@alpha-trading.example>")]
        packet, state = self.h.scan(commit=True)
        _, results = self.h.apply(packet, state, commit=True)
        task_id = results["tasks"][0]["page_id"]
        self.assertEqual(self.h.notion.page_status(task_id), "Todo")

        # The rep replies; next scan should propose completion, apply completes.
        self.folders["&XfJT0ZAB-"] = [build_outbound(
            50, "buyer@alpha-trading.example", "Re: Need pricing",
            "<reply-1@company.example>", in_reply_to="<alpha-7@alpha-trading.example>")]
        packet2, state2 = self.h.scan(commit=True)
        outbound = [m for m in packet2["new_messages"] if m["direction"] == "outbound"]
        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0]["archive_verified"], True,
                         "the sent reply must be archived before any completion decision")
        summary, results2 = self.h.apply(packet2, state2, commit=True)
        self.assertEqual(results2["completions"][0]["action"], "completed")
        self.assertEqual(self.h.notion.page_status(task_id), "Done")
        self.assertIn("完成依据", self.h.notion.page_text(task_id))
        done = [t for t in state2["tasks"].values() if t.get("status") == "done"]
        self.assertEqual(len(done), 1)

    def test_completed_task_leaves_the_open_list(self):
        """Acceptance: a completed task exits the to-do view (status != todo)."""
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [build_inbound(1, alpha, "Follow up",
                                               "<open-1@alpha-trading.example>")]
        packet, state = self.h.scan(commit=True)
        _, results = self.h.apply(packet, state, commit=True)
        identity = results["tasks"][0]["identity"]
        summary = loop.summarize_state(self.h.config, state)
        self.assertEqual([t["identity"] for t in summary["open_tasks"]], [identity])
        self.folders["&XfJT0ZAB-"] = [build_outbound(
            70, "buyer@alpha-trading.example", "Re: Follow up",
            "<reply-2@company.example>", in_reply_to="<open-1@alpha-trading.example>")]
        packet2, state2 = self.h.scan(commit=True)
        self.h.apply(packet2, state2, commit=True)
        summary2 = loop.summarize_state(self.h.config, state2)
        self.assertEqual(summary2["open_tasks"], [], "completed task still open")
        self.assertEqual(summary2["pending_review"], [])

    def test_completion_without_evidence_is_rejected(self):
        record = {"page_id": "some-page"}
        gate = tasks_module.completion_gate({"evidence": "", "source_locator": ""}, record)
        self.assertFalse(gate["allowed"])
        self.assertTrue(any("evidence" in r for r in gate["reasons"]))
        gate = tasks_module.completion_gate({"evidence": "done"}, None)
        self.assertFalse(gate["allowed"])
        good = {"evidence": "replied", "source_locator": "https://www.notion.so/x",
                "message_id": "<m@x>"}
        self.assertTrue(tasks_module.completion_gate(good, record)["allowed"])
        # the gate is explicit about its own scope: well-formed, not verified
        self.assertEqual(gate["scope"], "well_formed_claim_not_verified_outcome")

    def test_completion_traceability_distinguishes_claim_from_source(self):
        """Field completeness != the evidence actually tracing to an archive."""
        state = {"archived": [{"message_id": "<m1@x>",
                               "locator": "https://www.notion.so/page-a#blk1"}]}
        # an invented message id is a well-formed but untraceable claim
        r = tasks_module.verify_evidence_locator(
            {"message_id": "<invented@x>", "source_locator": "https://www.notion.so/page-a"},
            state)
        self.assertFalse(r["verifiable"])
        self.assertFalse([c for c in r["checks"]
                          if c["check"] == "message_id_in_archive_ledger"][0]["ok"])
        # a locator pointing at a different page fails too
        r = tasks_module.verify_evidence_locator(
            {"message_id": "<m1@x>", "source_locator": "https://www.notion.so/other-page"},
            state)
        self.assertFalse(r["verifiable"])
        # the archived original traces
        r = tasks_module.verify_evidence_locator(
            {"message_id": "<m1@x>", "source_locator": "https://www.notion.so/page-a#blk2"},
            state)
        self.assertTrue(r["verifiable"])
        self.assertEqual(r["scope"], "traceable_to_archived_source_not_business_truth")

    def test_invented_evidence_cannot_complete_a_task(self):
        """End to end: a completion whose message was never archived is refused."""
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [build_inbound(1, alpha, "Need pricing",
                                               "<trace-1@alpha-trading.example>")]
        packet, state = self.h.scan(commit=True)
        _, results = self.h.apply(packet, state, commit=True)
        task_id = results["tasks"][0]["page_id"]
        identity = results["tasks"][0]["identity"]
        self.assertEqual(self.h.notion.page_status(task_id), "Todo")
        # an agent claims completion with a message id that was never archived
        outcome = loop.complete_task(
            self.h.notion, self.h.config, {"status": "状态"}, {"status": "status"},
            {"identity": identity, "evidence": "声称已回复",
             "source_locator": "https://www.notion.so/whatever",
             "message_id": "<invented-reply@nowhere.example>"},
            state, commit=True)
        self.assertEqual(outcome["action"], "rejected")
        self.assertFalse(outcome["traceability"]["verifiable"])
        self.assertEqual(self.h.notion.page_status(task_id), "Todo",
                         "an untraceable claim completed the task")

    def test_outbound_only_thread_does_not_create_or_complete_anything(self):
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        self.folders["&XfJT0ZAB-"] = [build_outbound(
            60, "buyer@alpha-trading.example", "Introduction",
            "<intro-1@company.example>", in_reply_to="<never-seen@nowhere.example>")]
        packet, state = self.h.scan(commit=True)
        summary, results = self.h.apply(packet, state, commit=True)
        self.assertEqual(summary["tasks"], [])
        self.assertEqual(summary["completions"], [])
        self.assertEqual(len(self.h.notion.pages), 1)  # only the seeded customer page

    def test_first_run_baseline_latest_then_new_mail_is_picked_up(self):
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [build_inbound(1, alpha, "Old history",
                                               "<old-1@alpha-trading.example>", days_ago=30)]
        packet, state = self.h.scan(commit=True, baseline="latest")  # adopt latest, no import
        self.assertEqual(packet["folders"]["inbox:INBOX"]["baseline"]["mode"], "latest")
        self.assertEqual(packet["new_messages"], [])
        self.folders["INBOX"].append(build_inbound(2, alpha, "Fresh message",
                                                   "<fresh-1@alpha-trading.example>"))
        packet2, state2 = self.h.scan(commit=True)
        self.assertEqual(len(packet2["new_messages"]), 1)
        self.assertEqual(packet2["new_messages"][0]["subject"], "Fresh message")
        # And the very newest message at baseline time was NOT imported.
        self.assertNotIn("Old history", [m["subject"] for m in packet2["new_messages"]])

    def test_localized_sent_folder_is_resolved(self):
        with self.h.mailbox() as box:
            raw = box.resolve_folder("已发送")
        self.assertEqual(raw, "&XfJT0ZAB-")
        # the modified-UTF-7 name also resolves when only the raw name matches
        with self.h.mailbox() as box:
            raw2 = box.resolve_folder("&XfJT0ZAB-")
        self.assertEqual(raw2, "&XfJT0ZAB-")

    def test_uidvalidity_change_rescans_and_does_not_lose_or_duplicate(self):
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [build_inbound(1, alpha, "First",
                                               "<epoch-1@alpha-trading.example>"),
                                 build_inbound(2, alpha, "Second",
                                               "<epoch-2@alpha-trading.example>")]
        packet, state = self.h.scan(commit=True)
        self.assertEqual(packet["summary"]["archived"], 2)
        blocks_after_first = len(self.h.notion.blocks[page_id])

        # Server renumbers the folder: same messages, new UIDs, new epoch.
        self.h.server.uidvalidity = "18"
        self.folders["INBOX"] = [build_inbound(900, alpha, "First",
                                               "<epoch-1@alpha-trading.example>"),
                                 build_inbound(901, alpha, "Second",
                                               "<epoch-2@alpha-trading.example>"),
                                 build_inbound(902, alpha, "Third",
                                               "<epoch-3@alpha-trading.example>")]
        packet2, state2 = self.h.scan(commit=True)
        self.assertEqual(len(packet2["new_messages"]), 1, "rescan mishandled the epoch change")
        self.assertEqual(packet2["new_messages"][0]["subject"], "Third")
        self.assertEqual(state2["folders"]["inbox:INBOX"]["lastUidValidity"], "18")
        # The renumber must not re-archive what was already archived: every
        # message marker appears exactly once on the page.
        text = self.h.notion.page_text(page_id)
        for marker in ("<epoch-1@alpha-trading.example>", "<epoch-2@alpha-trading.example>",
                       "<epoch-3@alpha-trading.example>"):
            self.assertEqual(text.count("mail-source-complete: " + marker), 1,
                             f"{marker} archived a wrong number of times")

    def test_mail_reading_never_sets_seen_flag(self):
        """The fake server treats any mutating command as a protocol violation."""
        page_id = self.seed_customer_page("alpha")
        self.h.config["customers"][0]["page_id"] = page_id
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.folders["INBOX"] = [build_inbound(1, alpha, "Flag test",
                                               "<flag-1@alpha-trading.example>")]
        self.h.scan(commit=True)
        forbidden = [c for c in self.h.server.commands
                     if any(word in c.upper() for word in ("STORE", "DELETE", "EXPUNGE", "MOVE", "COPY"))]
        self.assertEqual(forbidden, [], f"mailbox mutation commands were sent: {forbidden}")
        self.assertIn("EXAMINE", " ".join(self.h.server.commands).upper())


class ConfigUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nextmove-cfg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_placeholder_config_fails_validation_with_clear_problems(self):
        path = make_config(self.tmp)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["notion"]["task_database_id"] = ""
        data["customers"] = []
        path.write_text(json.dumps(data), encoding="utf-8")
        config = config_module.load(path)
        problems = config_module.validate(config)
        joined = "; ".join(problems)
        self.assertIn("task_database_id", joined)
        self.assertIn("customers is empty", joined)

    def test_secret_resolution_reports_source_but_not_value(self):
        path = make_config(self.tmp)
        config = config_module.load(path)
        value, source = config_module.resolve_secret(config, "imap_password")
        self.assertEqual(value, "fake-password")
        self.assertTrue(source.startswith("file:"))

    def test_ambiguous_customer_address_match_returns_none(self):
        path = make_config(self.tmp)
        config = config_module.load(path)
        config["customers"].append({"key": "twin", "display_name": "Twin",
                                    "email_addresses": ["buyer@alpha-trading.example"]})
        self.assertIsNone(config_module.customer_for_address(
            config, "buyer@alpha-trading.example"))
        self.assertEqual(config_module.customer_for_address(
            config, "hello@bravo-works.example"), "bravo")


class TasksUnit(unittest.TestCase):
    def setUp(self):
        self.notion = make_notion()
        self.config = {"notion": {"properties": {"title": "任务名称", "status": "状态",
                                                 "key": "任务标识"},
                                  "status_map": {"todo": "Todo", "done": "Done"}},
                       "customers": [mailbuilder.CUSTOMERS["alpha"]]}

    def test_mapping_reports_unknown_property_instead_of_writing(self):
        _, _, problems = tasks_module.resolve_mapping(
            {"notion": {"properties": {"title": "No Such Property", "status": None}}},
            SCHEMA)
        self.assertTrue(any("No Such Property" in p for p in problems))

    def test_status_option_not_in_database_is_refused(self):
        config = copy.deepcopy(self.config)
        config["notion"]["status_map"] = {"todo": "Not A Real Status"}
        with self.assertRaises(tasks_module.TaskError):
            tasks_module.check_status_option(config, "todo", SCHEMA,
                                             {"status": "状态"}, ["Todo", "Done"])

    def test_dedupe_reads_all_statuses_not_just_open(self):
        """A completed duplicate must still be found (query follows pagination)."""
        for i in range(120):
            self.notion.create_page("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                    {"任务名称": {"title": [
                                        {"type": "text", "text": {"content": f"task {i}"}}]}})
        self.notion.create_page("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                {"任务名称": {"title": [
                                    {"type": "text", "text": {"content": "the one"}}]}})
        existing = tasks_module.load_existing(self.notion,
                                              "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(len(existing), 121)
        found = tasks_module.find_duplicate(existing, self.config,
                                            {"title": "the one"}, {"title": "任务名称"})
        self.assertIsNotNone(found)


class JudgmentUnit(unittest.TestCase):
    """The judgement channel: request building + validation boundaries."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="nextmove-judgment-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = config_module.load(make_config(self.tmp))
        self.config["customers"][0]["page_id"] = "page-alpha"
        self.state = {
            "tasks": {
                "open-task-1": {"identity": "open-task-1", "title": "Alpha reply",
                                "customer_key": "alpha", "page_id": "task-page-1",
                                "message_id": "<alpha-1@alpha-trading.example>",
                                "status": "todo"},
            },
            "archived": [
                {"message_id": "<alpha-1@alpha-trading.example>", "customer_key": "alpha",
                 "page_id": "page-alpha", "locator": "https://www.notion.so/page-alpha#blk1"},
                {"message_id": "<reply-1@company.example>", "customer_key": "alpha",
                 "page_id": "page-alpha", "locator": "https://www.notion.so/page-alpha#blk2"},
            ],
        }

    def _message(self, mid="<alpha-2@alpha-trading.example>", direction="inbound"):
        return {
            "message_id": mid, "customer_key": "alpha",
            "customer_display": "Alpha Trading", "direction": direction,
            "date": "Fri, 16 Oct 2026 09:30:00 +0800", "from": "buyer@alpha-trading.example",
            "subject": "Quote please", "body_preview": "Please quote 300 units.",
            "body_truncated_in_packet": False, "attachments": [], "locator":
                "https://www.notion.so/page-alpha#blk3",
            "thread_references": [],
        }

    def _request(self, messages=None):
        from nextmove import judgment
        packet = {"new_messages": messages if messages is not None else [self._message()]}
        request = loop.build_judgment_request(self.config, packet, self.state)
        request["requestId"] = judgment.request_id(request)
        return request

    def _validate(self, document, request):
        from nextmove import judgment
        return judgment.validate(document, request, self.config, self.state)

    def test_request_carries_messages_tasks_rules_and_stable_id(self):
        request = self._request()
        self.assertEqual(request["kind"], "judgment_request")
        self.assertEqual(len(request["messages"]), 1)
        self.assertEqual(request["messages"][0]["locator"],
                         "https://www.notion.so/page-alpha#blk3")
        self.assertEqual(request["open_tasks"][0]["identity"], "open-task-1")
        self.assertIn("decision_types", request["instructions"])
        again = self._request()
        self.assertEqual(request["requestId"], again["requestId"])
        # a new message changes the id
        more = self._request([self._message(), self._message("<alpha-3@alpha-trading.example>")])
        self.assertNotEqual(request["requestId"], more["requestId"])

    def test_valid_create_passes_and_carries_request_facts(self):
        request = self._request()
        document = {"kind": "judgment", "basedOn": request["requestId"], "decisions": [{
            "decision": "create", "customer_key": "alpha",
            "message_id": "<alpha-2@alpha-trading.example>",
            "title": "Alpha Trading｜报价 300 件并核对交期",
            "action": "核价后回复客户报价区间与交期",
            "completion_standard": "报价邮件已发出且客户已收到",
            "generatedBy": "test-agent",
        }]}
        result = self._validate(document, request)
        self.assertTrue(result["ok"], result)
        decision = result["decisions"][0]
        self.assertEqual(decision["status"], "valid")
        # evidence locator comes from the request, not the judgment
        self.assertEqual(decision["payload"]["message"]["locator"],
                         "https://www.notion.so/page-alpha#blk3")
        self.assertIsNone(decision["payload"]["due_date"])

    def test_create_rejections(self):
        request = self._request()
        base = {"decision": "create", "customer_key": "alpha",
                "message_id": "<alpha-2@alpha-trading.example>",
                "title": "T", "action": "A", "completion_standard": "C"}
        cases = {
            "unknown message": {**base, "message_id": "<never-seen@x.example>"},
            "wrong customer": {**base, "customer_key": "bravo"},
            "empty title": {**base, "title": "  "},
            "placeholder title": {**base, "title": "TODO"},
            "missing action": {**base, "action": ""},
            "bad date": {**base, "due_date": "next week"},
            "unapproved field": {**base, "amount": 12345},
        }
        for label, decision in cases.items():
            result = self._validate({"kind": "judgment", "basedOn": request["requestId"],
                                     "decisions": [decision]}, request)
            self.assertFalse(result["ok"], f"{label} should not validate")
            self.assertTrue(result["decisions"][0]["problems"], label)

    def test_complete_requires_archived_evidence(self):
        request = self._request()
        good = {"decision": "complete", "identity": "open-task-1",
                "evidence": "已发出报价", "source_locator": "https://www.notion.so/page-alpha#blk2",
                "message_id": "<reply-1@company.example>", "evidence_date": "2026-10-16"}
        result = self._validate({"kind": "judgment", "basedOn": request["requestId"],
                                 "decisions": [good]}, request)
        self.assertTrue(result["ok"], result)
        # an evidence id that was never archived cannot complete anything
        bad = {**good, "message_id": "<invented@nowhere.example>"}
        result = self._validate({"kind": "judgment", "basedOn": request["requestId"],
                                 "decisions": [bad]}, request)
        self.assertFalse(result["ok"])
        self.assertIn("not an archived original", result["decisions"][0]["problems"][0])
        # a task that is not in local state cannot be completed
        bad = {**good, "identity": "not-a-task"}
        result = self._validate({"kind": "judgment", "basedOn": request["requestId"],
                                 "decisions": [bad]}, request)
        self.assertFalse(result["ok"])

    def test_hold_and_append(self):
        request = self._request()
        hold = {"decision": "hold", "message_id": "<alpha-2@alpha-trading.example>",
                "reason": "courtesy mail, no action needed"}
        result = self._validate({"kind": "judgment", "basedOn": request["requestId"],
                                 "decisions": [hold]}, request)
        self.assertTrue(result["ok"], result)
        append = {"decision": "append", "identity": "open-task-1",
                  "message_id": "<alpha-2@alpha-trading.example>",
                  "evidence": "客户补充了数量 300"}
        result = self._validate({"kind": "judgment", "basedOn": request["requestId"],
                                 "decisions": [append]}, request)
        self.assertTrue(result["ok"], result)

    def test_missing_based_on_is_rejected_whole(self):
        request = self._request()
        document = {"kind": "judgment", "decisions": [
            {"decision": "hold", "message_id": "<alpha-2@alpha-trading.example>",
             "reason": "x"}]}
        result = self._validate(document, request)
        self.assertFalse(result["ok"])
        self.assertIn("basedOn", result["error"])


class JudgmentLoop(unittest.TestCase):
    """End-to-end: queue -> request -> judgment -> Notion, with retries."""

    def setUp(self):
        self.h = LoopHarness(mailbuilder.standard_folders())
        self.addCleanup(self.h.close)
        page = self.h.notion.create_page(
            self.h.notion.data_source_id,
            {"任务名称": {"title": [{"type": "text", "text": {"content": "Customer page"}}]}},
            children=[])
        self.h.config["customers"][0]["page_id"] = page["id"]
        self.page_id = page["id"]

    def _queue_and_request(self):
        """Scan (full baseline) -> queue -> request, as the CLI does."""
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.h.server.folders["INBOX"].append(build_inbound(
            1, alpha, "New order request", "<j-1@alpha-trading.example>"))
        packet, state = self.h.scan(commit=True)
        from nextmove import state as state_module
        queued = state_module.queue_judgment_messages(state, packet.get("new_messages", []))
        state_module.write(self.h.state_path, state)
        request = loop.build_judgment_request(
            self.h.config, {"new_messages": state["judgmentQueue"]}, state)
        from nextmove import judgment
        request["requestId"] = judgment.request_id(request)
        return request, state, queued

    def test_judgment_create_updates_notion_and_dequeues(self):
        from nextmove import judgment
        request, state, queued = self._queue_and_request()
        self.assertEqual(len(queued), 1)
        document = {"kind": "judgment", "basedOn": request["requestId"], "decisions": [{
            "decision": "create", "customer_key": "alpha",
            "message_id": "<j-1@alpha-trading.example>",
            "title": "Alpha Trading｜核价 300 件并回复报价",
            "action": "与工厂核价后回复客户报价区间与交期",
            "completion_standard": "报价邮件已发出且客户已收到",
            "generatedBy": "test-agent",
        }]}
        validation = judgment.validate(document, request, self.h.config, state)
        self.assertTrue(validation["ok"], validation)
        results = loop.apply_judgment(self.h.config, self.h.notion, validation, state,
                                      commit=True)
        self.assertEqual(results["created"][0]["action"], "created")
        task_id = results["created"][0]["page_id"]
        title = self.h.notion.page_titles()[task_id]
        self.assertIn("核价 300 件", title)
        self.assertEqual(self.h.notion.page_status(task_id), "Todo")
        self.assertIn("来源", self.h.notion.page_text(task_id))
        # dequeue only the landed message
        landed = {"created", "updated", "duplicate", "completed", "appended", "held"}
        handled = [o["message_id"] for o in results["outcomes"]
                   if o["action"] in landed]
        from nextmove import state as state_module
        state_module.mark_judged(state, handled, request["requestId"])
        self.assertEqual(state["judgmentQueue"], [])

    def test_rejected_decision_leaves_message_queued(self):
        from nextmove import judgment
        from nextmove import state as state_module
        request, state, _ = self._queue_and_request()
        document = {"kind": "judgment", "basedOn": request["requestId"], "decisions": [{
            "decision": "create", "customer_key": "alpha",
            "message_id": "<j-1@alpha-trading.example>",
            "title": "TODO", "action": "A", "completion_standard": "C",
        }]}
        validation = judgment.validate(document, request, self.h.config, state)
        self.assertFalse(validation["ok"])
        # nothing lands, so nothing is dequeued
        landed = {"created", "updated", "duplicate", "completed", "appended", "held"}
        handled = []
        state_module.mark_judged(state, handled, request["requestId"])
        self.assertEqual(len(state["judgmentQueue"]), 1)
        self.assertIn("placeholder", "; ".join(validation["decisions"][0]["problems"]))

    def test_judgment_is_idempotent_after_reapply(self):
        """Applying the same basedOn twice is refused the second time."""
        from nextmove import judgment
        from nextmove import state as state_module
        request, state, _ = self._queue_and_request()
        document = {"kind": "judgment", "basedOn": request["requestId"], "decisions": [{
            "decision": "create", "customer_key": "alpha",
            "message_id": "<j-1@alpha-trading.example>",
            "title": "Alpha Trading｜核价并回复",
            "action": "核价后回复", "completion_standard": "报价已发出",
        }]}
        validation = judgment.validate(document, request, self.h.config, state)
        results = loop.apply_judgment(self.h.config, self.h.notion, validation, state,
                                      commit=True)
        landed = {"created", "updated", "duplicate", "completed", "appended", "held"}
        state_module.mark_judged(
            state, [o["message_id"] for o in results["outcomes"] if o["action"] in landed],
            request["requestId"])
        self.assertTrue(state_module.judgment_already_applied(state, request["requestId"]))
        # the same judgment document applied again is refused before writing
        pages_before = len(self.h.notion.pages)
        # (mirrors the CLI guard: already-applied check happens first)
        if not state_module.judgment_already_applied(state, document["basedOn"]):
            loop.apply_judgment(self.h.config, self.h.notion, validation, state, commit=True)
        self.assertEqual(len(self.h.notion.pages), pages_before)

    def test_create_decision_title_is_used_as_written(self):
        """The agent's title is not re-templated (no doubled customer prefix)."""
        from nextmove import judgment
        request, state, _ = self._queue_and_request()
        document = {"kind": "judgment", "basedOn": request["requestId"], "decisions": [{
            "decision": "create", "customer_key": "alpha",
            "message_id": "<j-1@alpha-trading.example>",
            "title": "Alpha Trading｜核价并回复报价",
            "action": "核价后回复", "completion_standard": "报价已发出",
        }]}
        validation = judgment.validate(document, request, self.h.config, state)
        self.assertTrue(validation["ok"], validation)
        results = loop.apply_judgment(self.h.config, self.h.notion, validation, state,
                                      commit=True)
        task_id = results["created"][0]["page_id"]
        title = self.h.notion.page_titles()[task_id]
        self.assertEqual(title, "Alpha Trading｜核价并回复报价",
                         "the title was templated on top of the agent's own wording")

    def test_partial_judgment_applies_only_queued_messages(self):
        """A judgment covering a message already handled can still apply the rest."""
        from nextmove import judgment
        from nextmove import state as state_module
        request, state, _ = self._queue_and_request()
        # simulate: message A was judged in a previous round and left the queue;
        # a new message B arrives and is queued.
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.h.server.folders["INBOX"].append(build_inbound(
            2, alpha, "Second question", "<j-2@alpha-trading.example>"))
        packet, state = self.h.scan(commit=True)
        state_module.queue_judgment_messages(state, packet.get("new_messages", []))
        # drop the first from the queue to mimic "already handled"
        state["judgmentQueue"] = [q for q in state["judgmentQueue"]
                                  if q["message_id"] == "<j-2@alpha-trading.example>"]
        request2 = loop.build_judgment_request(
            self.h.config, {"new_messages": state["judgmentQueue"]}, state)
        request2["requestId"] = judgment.request_id(request2)
        # an older document (based on the old set) with decisions for both A and B
        old_document = {"kind": "judgment", "basedOn": request["requestId"], "decisions": [
            {"decision": "create", "customer_key": "alpha",
             "message_id": "<j-1@alpha-trading.example>",
             "title": "A-task", "action": "A", "completion_standard": "C"},
            {"decision": "create", "customer_key": "alpha",
             "message_id": "<j-2@alpha-trading.example>",
             "title": "B-task", "action": "A", "completion_standard": "C"},
        ]}
        validation = judgment.validate(old_document, request2, self.h.config, state)
        statuses = {d.get("decision"): d["status"] for d in validation["decisions"]}
        # A is no longer in the request -> rejected; B still applies
        self.assertEqual(validation["decisions"][0]["status"], "rejected")
        self.assertEqual(validation["decisions"][1]["status"], "valid")
        results = loop.apply_judgment(self.h.config, self.h.notion, validation, state,
                                      commit=True)
        self.assertEqual(len(results["created"]), 1)
        self.assertEqual(len(results["rejected"]), 1)


class CliSmoke(unittest.TestCase):
    """The CLI runs end to end against fakes: doctor/check/status shapes."""

    def setUp(self):
        self.h = LoopHarness(mailbuilder.standard_folders())
        self.addCleanup(self.h.close)

    def test_doctor_reports_shape_and_never_prints_secret_values(self):
        from nextmove import cli
        code, report = _run_cli(cli, self.h, ["--config", str(self.h.config_path), "doctor"])
        self.assertEqual(report["status"], "ok")
        names = [c["check"] for c in report["checks"]]
        self.assertIn("mailbox.read_only_login", names)
        self.assertIn("notion.schema_mapping", names)
        dumped = json.dumps(report)
        self.assertNotIn("fake-password", dumped)
        self.assertNotIn("fake-token", dumped)

    def test_placeholder_config_is_rejected_before_any_connection(self):
        from nextmove import cli
        path = Path(self.h.tmp) / "nextmove.config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["notion"]["task_database_id"] = "TODO"
        path.write_text(json.dumps(data), encoding="utf-8")
        code, report = _run_cli(cli, self.h, ["--config", str(path), "doctor"],
                                expect_exit=2)
        self.assertEqual(report["status"], "CONFIG_INVALID")
        self.assertTrue(any("task_database_id" in p for p in report["problems"]))

    def test_check_is_dry_and_leaves_state_untouched(self):
        from nextmove import cli
        state_file = self.h.state_path
        before = state_file.read_text(encoding="utf-8") if state_file.exists() else None
        code, report = _run_cli(cli, self.h, ["--config", str(self.h.config_path), "check"])
        self.assertEqual(report["status"], "DRY_RUN")
        after = state_file.read_text(encoding="utf-8") if state_file.exists() else None
        self.assertEqual(before, after, "check wrote local state")
        writes = self.h.notion.write_calls()
        self.assertEqual(writes, [], "check attempted a Notion write")

    def test_cli_judgment_chain_scan_request_apply(self):
        """The full daily chain via the CLI: scan -> request -> apply --judgment."""
        from nextmove import cli
        page = self.h.notion.create_page(
            self.h.notion.data_source_id,
            {"任务名称": {"title": [{"type": "text", "text": {"content": "Alpha"}}]}},
            children=[])
        # persist the customer page id so the CLI (which re-reads the config)
        # resolves the customer page
        data = json.loads(self.h.config_path.read_text(encoding="utf-8"))
        data["customers"][0]["page_id"] = page["id"]
        self.h.config_path.write_text(json.dumps(data, ensure_ascii=False),
                                      encoding="utf-8")
        alpha = mailbuilder.CUSTOMERS["alpha"]
        self.h.server.folders["INBOX"].append(build_inbound(
            1, alpha, "Please quote", "<cli-1@alpha-trading.example>"))
        cfg = ["--config", str(self.h.config_path)]
        # 1. scan (full baseline) queues the message and writes the request
        code, report = _run_cli(cli, self.h, cfg + ["scan", "--baseline", "full"])
        self.assertEqual(report["status"], "SCANNED")
        self.assertEqual(report["queued_for_judgment"], 1)
        request_path = Path(report["judgment_request"])
        self.assertTrue(request_path.exists())
        request = json.loads(request_path.read_text(encoding="utf-8"))
        # 2. the agent writes the judgment next to the request
        judgment_path = request_path.parent / "judgment.json"
        judgment_path.write_text(json.dumps({
            "kind": "judgment", "basedOn": request["requestId"],
            "decisions": [{"decision": "create", "customer_key": "alpha",
                           "message_id": "<cli-1@alpha-trading.example>",
                           "title": "Alpha Trading｜核价并回复",
                           "action": "核价后回复", "completion_standard": "报价已发出"}],
        }, ensure_ascii=False), encoding="utf-8")
        # 3. dry run: nothing written
        pages_before = len(self.h.notion.pages)
        code, report = _run_cli(cli, self.h, cfg + ["apply", "--judgment", str(judgment_path)])
        self.assertEqual(report["status"], "JUDGMENT_DRY_RUN")
        self.assertEqual(len(self.h.notion.pages), pages_before)
        # 4. commit: the task lands
        code, report = _run_cli(cli, self.h, cfg + ["apply", "--judgment",
                                                    str(judgment_path), "--commit"])
        self.assertEqual(report["status"], "JUDGMENT_APPLIED")
        self.assertEqual(len(report["results"]["created"]), 1)
        self.assertEqual(len(self.h.notion.pages), pages_before + 1)
        # 5. applying the same judgment again is refused (idempotent)
        code, report = _run_cli(cli, self.h, cfg + ["apply", "--judgment",
                                                    str(judgment_path), "--commit"])
        self.assertEqual(report["status"], "ALREADY_APPLIED")
        self.assertEqual(len(self.h.notion.pages), pages_before + 1)

    def test_cli_judgment_request_command(self):
        from nextmove import cli
        code, report = _run_cli(cli, self.h, ["--config", str(self.h.config_path),
                                              "judgment-request"])
        self.assertEqual(report["status"], "REQUEST_WRITTEN")
        self.assertTrue(Path(report["request"]).exists())
        # empty queue -> zero messages, still a valid request file
        self.assertEqual(report["messages"], 0)


def _run_cli(cli, harness, argv, expect_exit=0):
    """Run a CLI command with the harness's fake mailbox and Notion patched in."""
    import io
    from contextlib import redirect_stdout
    buffer = io.StringIO()
    originals = (cli._mailbox, cli._client)
    cli._mailbox = lambda config: (
        mailcore.Mailbox("127.0.0.1", harness.server.port, config["mailbox"]["user"],
                         "fake-password", timeout=10, use_ssl=False), "test")
    cli._client = lambda config, dry_run=False: (harness.notion, "test")
    try:
        with redirect_stdout(buffer):
            try:
                exit_code = cli.main(argv)
            except SystemExit as exc:
                exit_code = exc.code
    finally:
        cli._mailbox, cli._client = originals
    self_check = exit_code if exit_code is not None else 0
    if expect_exit is not None:
        assert self_check == expect_exit, \
            f"CLI exit {self_check}, expected {expect_exit}: {buffer.getvalue()[:400]}"
    try:
        return self_check, json.loads(buffer.getvalue())
    except json.JSONDecodeError:
        return self_check, {"raw": buffer.getvalue()}


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (PlanUnit, ReadOnlyContract, LoopEndToEnd, ConfigUnit, TasksUnit,
                 JudgmentUnit, JudgmentLoop, CliSmoke):
        suite.addTests(loader.loadTestsFromTestCase(case))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stdout)
    result = runner.run(suite)
    print(f"\nRan {result.testsRun} tests: "
          f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures)} failures, {len(result.errors)} errors")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
