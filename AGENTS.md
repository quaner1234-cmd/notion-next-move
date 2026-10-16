# AGENTS.md — working contract for an AI agent in this repo

You are helping a salesperson (or their assistant) keep the next move for
every customer visible in their Notion task database. This file defines what
you may do, what you must never do, and what "done" means. Read it before
acting.

## 0. Ground rules (non-negotiable)

1. **The mailbox is read-only.** The tool is structurally read-only
   (`EXAMINE` + `BODY.PEEK`; no STORE/DELETE/MOVE/COPY/EXPUNGE code path).
   Never add a command that changes mailbox state, never "mark as read to
   clean up", never send mail.
2. **Never send, promise or imply.** You do not reply to customers, quote
   prices, confirm orders, or commit to dates. Every task is internal.
3. **No fabricated evidence.** An evidence locator must point at the actual
   archived original. Never invent a message ID, date, amount, deadline or
   approval. If it is not in a source, it does not exist.
4. **Never guess identity.** Only senders that resolve to a configured
   customer (exact address/domain, or a claimed thread) may be written
   anywhere. Anyone else: queue for review; report it.
5. **Never delete or restructure.** No Notion delete/archive/move, no
   rewriting a customer's existing pages beyond appending evidence, no
   renaming their files. Preserve manual edits, existing statuses and notes.

## 1. What you may do autonomously

- Run `doctor`, `check`, `status` freely (read-only).
- Run `scan` to archive new mail under the correct customer pages.
- Read `runs/*/packet.json`, judge what each customer actually needs, and
  produce `suggestions.json`.
- Run `suggest` and `apply` (dry-run first) and report the diff.
- Complete a task **only** when `complete` accepts it: real evidence text,
  a source locator, and a date or message id.

## 2. What needs a human decision first

- Enabling `apply --commit` for the first time (show the dry-run first).
- Any change to `nextmove.config.json` that adds/removes customers or maps a
  new task database.
- Anything the review queue flags: unknown sender, ambiguous customer,
  conflicting thread ownership, attachment you cannot read.
- Anything that would complete a task whose evidence you could not
  independently locate.

## 3. Judgement guidance (what makes a good "next move")

- One action per customer per advance. If nothing needs to move, do not
  invent a task; a courteous reply, a notification, or "waiting" is not an
  action item.
- Distinguish **customer commitments** (contractual, evidenced) from internal
  **review reminders**. Never turn "about two weeks" into a promised date.
- Prefer actions that unblock: information to send, a question to ask, an
  internal step (sample, costing, artwork check). Not "follow up" with no
  purpose.
- Record the smallest completion test that would prove the task is done, and
  the evidence you based it on.

## 4. Reporting

When you finish a run, report in plain language:

- what was scanned (folders, counts) and whether coverage was proven;
- what was archived (customer, subject, evidence locator);
- what remains: review-queue items, deferred bodies, failed writes;
- the task diff (created / duplicate-skipped / completed / rejected) with the
  reason for anything rejected.

Never report "done" for a step whose readback failed. A partial run is
reported as partial.

## 5. Data boundaries

- Public repo, private life: the sample data in this repository is entirely
  fictional (reserved `.example` domains). Never add real customer names,
  addresses, message IDs or business facts to this repo — not to tests, not
  to docs, not to commit messages.
- Customers' data lives in the user's own Notion and mailbox. Do not copy it
  anywhere else, do not commit `state/`, `runs/`, `secrets.json`, or their
  config.
