"""Read-only IMAP access.

Every mailbox session is opened with EXAMINE (read-only) and only BODY.PEEK is
used, so your unread flags are never changed. There is deliberately no code
path that deletes, moves, starts or alters a message — the read-only contract
is structural, not just a promise in the README.

Coverage model (the part that matters for "never miss a customer reply"):

* All UIDs are enumerated with ``UID SEARCH ALL`` and cross-checked against
  the folder's EXISTS count. A mismatch refuses to prove coverage.
* The state file stores a checkpoint per folder: the UID and RFC Message-ID of
  the newest message that was fully processed. Anything above that UID is new,
  even if its internal date is older than the last run (late-arriving mail).
* When UIDVALIDITY changes (server renumbers the folder) or the checkpoint
  anchor message can no longer be found, the scan falls back to a complete
  header inventory and re-anchors on the RFC Message-ID instead of the UID.
* The 100 newest UIDs are always re-listed so first-run epoch adoption and
  gaps can be supplemented; dedupe is by Message-ID, so overlaps are free.

Nothing here is specific to any provider. One account, one credential, the
two folders you configure.
"""
from __future__ import annotations

import imaplib
import re
import socket
import ssl
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

IL = re.compile(rb'\((?P<flags>[^)]*)\)\s+"?(?P<date>[^"]*)"?\s*\((?P<body>.*)\)', re.S)
ID_RE = re.compile(rb"^\s*(.+?)=$")
_IMAP_ERRORS = (imaplib.IMAP4.error, OSError, ssl.SSLError, socket.timeout, TimeoutError)


class MailError(Exception):
    """Read-only access failed; callers keep their existing checkpoint."""


def decode_mime(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _imap_utf7_decode(name):
    """Decode the modified UTF-7 folder names used by IMAP servers."""
    out = []
    i = 0
    while i < len(name):
        ch = name[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = name.find("-", i)
        if end == -1:
            out.append(name[i:])
            break
        chunk = name[i + 1:end]
        if not chunk:
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            pad = "=" * ((4 - len(b64) % 4) % 4)
            try:
                raw = __import__("base64").b64decode(b64 + pad)
                out.append(raw.decode("utf-16-be"))
            except Exception:
                out.append(name[i:end + 1])
        i = end + 1
    return "".join(out)


class Mailbox:
    """A read-only session against one IMAP account.

    Use as a context manager. Folder arguments accept the display name
    (e.g. "Sent" or a localized name) or a raw server-side name.

    ``use_ssl`` defaults to True (IMAP over TLS, what every provider expects).
    Setting it False is for the offline test server only.
    """

    def __init__(self, host, port, user, password, timeout=30, use_ssl=True):
        self.host = host
        self.port = int(port)
        self.user = user
        self._password = password
        self.timeout = timeout
        self.use_ssl = use_ssl
        self.connection = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self):
        try:
            if self.use_ssl:
                self.connection = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout)
            else:
                self.connection = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
            self.connection.login(self.user, self._password)
        except (imaplib.IMAP4.error, OSError) as exc:
            msg = str(exc) or type(exc).__name__
            raise MailError(
                f"IMAP login/connect failed ({self.host}:{self.port}): {msg}. "
                "Check host/port, that IMAP is enabled for the mailbox, and that "
                "you are using the client authorization code (not the web password) "
                "if your provider requires one."
            ) from exc
        return self

    def close(self):
        try:
            if self.connection is not None:
                try:
                    self.connection.logout()
                except Exception:
                    pass
        finally:
            self.connection = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc_info):
        self.close()
        return False

    # -- folders -----------------------------------------------------------

    def list_folders(self):
        """Return [(raw_name, display_name), ...] from LIST."""
        out = []
        try:
            typ, rows = self.connection.list()
        except _IMAP_ERRORS as exc:
            raise MailError(f"LIST failed: {exc}") from exc
        if typ != "OK":
            raise MailError("LIST failed")
        for row in rows or []:
            if row is None:
                continue
            text = row.decode("utf-8", "replace") if isinstance(row, bytes) else str(row)
            parts = text.split(' "')
            if len(parts) < 3:
                continue
            raw = parts[-1].strip('"')
            out.append((raw, _imap_utf7_decode(raw)))
        return out

    def resolve_folder(self, name):
        """Match a configured folder name against the server list, read-only.

        Matching order: exact raw name, exact decoded name, decoded casefold,
        then a unique substring of the decoded name. Raises MailError when
        nothing or more than one folder matches.
        """
        folders = self.list_folders()
        if not folders:
            raise MailError("server returned no folders")
        wanted = name.strip().strip('"')
        for raw, decoded in folders:
            if raw == wanted or decoded == wanted:
                return raw
        low = wanted.casefold()
        exact_casefold = [raw for raw, decoded in folders if decoded.casefold() == low]
        if len(exact_casefold) == 1:
            return exact_casefold[0]
        partial = [raw for raw, decoded in folders if low in decoded.casefold()]
        if len(partial) == 1:
            return partial[0]
        names = ", ".join(decoded for _, decoded in folders)
        raise MailError(f"folder {name!r} not found or ambiguous. Server folders: {names}")

    def examine(self, raw_name):
        """Open a folder read-only. Returns (exists_count, uidvalidity)."""
        try:
            typ, data = self.connection.select(raw_name, readonly=True)
        except _IMAP_ERRORS as exc:
            raise MailError(f"EXAMINE failed for folder {raw_name!r}: {exc}") from exc
        if typ != "OK":
            raise MailError(f"EXAMINE failed for folder {raw_name!r}")
        exists = int((data[0] or b"0") if data and data[0] else 0)
        validity = _read_uidvalidity(self.connection)
        if validity is None:
            raise MailError("server did not report UIDVALIDITY")
        return exists, validity

    # -- UIDs and headers --------------------------------------------------

    def uid_inventory(self):
        try:
            typ, data = self.connection.uid("SEARCH", "ALL")
        except _IMAP_ERRORS as exc:
            raise MailError(f"UID SEARCH ALL failed: {exc}") from exc
        if typ != "OK" or not data or data[0] is None:
            raise MailError("UID SEARCH ALL failed")
        return sorted(int(u) for u in data[0].split())

    def fetch_headers(self, uids, chunk=100):
        """Fetch headers for the given UIDs, verifying every UID came back."""
        fields = ("(UID FLAGS INTERNALDATE RFC822.SIZE "
                  "BODY.PEEK[HEADER.FIELDS (DATE FROM SENDER TO CC REPLY-TO SUBJECT "
                  "MESSAGE-ID IN-REPLY-TO REFERENCES)])")
        out = []
        uids = [int(u) for u in uids]
        for start in range(0, len(uids), chunk):
            part = uids[start:start + chunk]
            try:
                typ, data = self.connection.uid("FETCH", ",".join(str(u) for u in part), fields)
            except _IMAP_ERRORS as exc:
                raise MailError(f"header FETCH failed: {exc}") from exc
            if typ != "OK":
                raise MailError("header FETCH failed")
            obtained = set()
            for item in data or []:
                if not isinstance(item, tuple) or not isinstance(item[1], bytes):
                    continue
                meta = item[0].decode("ascii", "replace")
                match = re.search(r"\bUID (\d+)\b", meta)
                if not match:
                    continue
                uid = int(match.group(1))
                header = _parse_header_bytes(item[1])
                header["uid"] = uid
                flags = re.search(r"FLAGS \(([^)]*)\)", meta)
                header["flags"] = flags.group(1).split() if flags else []
                internal = re.search(r'INTERNALDATE "([^"]+)"', meta)
                header["internal_date"] = internal.group(1) if internal else None
                out.append(header)
                obtained.add(uid)
            if obtained != set(part):
                missing = sorted(set(part) - obtained)
                raise MailError(f"incomplete header batch; missing UIDs {missing[:5]} — "
                                "keeping the old checkpoint")
        return out

    def fetch_body(self, uid):
        """Fetch one full RFC822 message with BODY.PEEK (never sets Seen)."""
        try:
            typ, data = self.connection.uid("FETCH", str(uid), "(BODY.PEEK[])")
        except _IMAP_ERRORS as exc:
            raise MailError(f"BODY.PEEK fetch failed for UID {uid}: {exc}") from exc
        if typ != "OK":
            raise MailError(f"BODY.PEEK fetch failed for UID {uid}")
        for item in data or []:
            if isinstance(item, tuple) and isinstance(item[1], bytes):
                return item[1]
        raise MailError(f"no body returned for UID {uid}")


def _read_uidvalidity(connection):
    """Best-effort UIDVALIDITY extraction with two independent strategies.

    ``response('UIDVALIDITY')`` is the documented imaplib API, but its return
    shape differs between Python versions (raw line vs. bare number), so the
    digits are parsed out explicitly and raw untagged responses are a fallback.
    """
    candidates = []
    try:
        _, data = connection.response("UIDVALIDITY")
        if data and data[0]:
            candidates.append(data[0])
    except Exception:
        pass
    for values in (getattr(connection, "untagged_responses", {}) or {}).values():
        for item in values or []:
            candidates.append(item)
    for item in candidates:
        if isinstance(item, bytes):
            match = re.search(rb"UIDVALIDITY (\d+)", item)
            if match:
                return match.group(1).decode("ascii")
            if item.strip().isdigit():
                return item.strip().decode("ascii")
        elif isinstance(item, str):
            match = re.search(r"UIDVALIDITY (\d+)", item)
            if match:
                return match.group(1)
            if item.strip().isdigit():
                return item.strip()
    return None


def _parse_header_bytes(raw):
    from email import message_from_bytes, policy
    message = message_from_bytes(raw, policy=policy.default)
    get = lambda key: decode_mime(message.get(key))  # noqa: E731
    return {
        "message_id": get("Message-ID").strip(),
        "subject": get("Subject"),
        "from": get("From"),
        "sender": get("Sender"),
        "to": get("To"),
        "cc": get("Cc"),
        "reply_to": get("Reply-To"),
        "in_reply_to": get("In-Reply-To"),
        "references": get("References"),
        "date_header": get("Date"),
    }


def parse_date(header):
    """Best-effort parse of a Date header into an aware datetime, or None."""
    value = header.get("date_header") or ""
    try:
        stamp = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        from datetime import timezone
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp
