#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic .eml builder for tests and examples.

Everything it produces is fictional. Subjects/addresses are deliberately in
reserved example ranges (.example / .invalid) so they can never point at a
real person or company.
"""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr


def build_mail(*, mid, frm, to, date, subject, text="", html=None,
               in_reply_to=None, references=None, sender=None, cc=None):
    message = EmailMessage()
    message["Message-ID"] = mid
    message["From"] = formataddr(frm) if isinstance(frm, tuple) else frm
    message["To"] = ", ".join(formataddr(a) if isinstance(a, tuple) else a for a in to)
    if cc:
        message["Cc"] = ", ".join(formataddr(a) if isinstance(a, tuple) else a for a in cc)
    if sender:
        message["Sender"] = sender
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    message["Date"] = date
    message["Subject"] = subject
    message.set_content(text or "")
    if html is not None:
        message.add_alternative(html, subtype="html")
    return message.as_bytes()


# A small fictional customer set used across tests/examples. All addresses end
# in .example (reserved by RFC 2606) — they cannot belong to a real party.
CUSTOMERS = {
    "alpha": {"key": "alpha", "display_name": "Alpha Trading",
              "email_addresses": ["buyer@alpha-trading.example"],
              "email_domains": ["alpha-trading.example"]},
    "bravo": {"key": "bravo", "display_name": "Bravo Works",
              "email_addresses": ["hello@bravo-works.example"],
              "email_domains": ["bravo-works.example"]},
    "charlie": {"key": "charlie", "display_name": "Charlie & Co.",
                "email_addresses": ["po@charlie.example"],
                "email_domains": ["charlie.example"]},
}


def standard_folders():
    """Two folders with a localized (modified UTF-7) sent-folder name.

    "已发送" encodes to "&XfJT0ZAB-" in modified UTF-7, which is what many
    Chinese providers actually report in LIST.
    """
    return {"INBOX": [], "&XfJT0ZAB-": []}


def folder_message(uid, raw, internaldate):
    return {"uid": str(uid), "raw": raw, "internaldate": internaldate}
