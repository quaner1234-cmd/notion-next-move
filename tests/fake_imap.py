#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal in-process fake IMAP server for deterministic client tests.

Speaks enough IMAP4rev1 for imaplib's read-only flow: CAPABILITY / LOGIN /
LIST / SELECT / EXAMINE / UID SEARCH / UID FETCH (headers and BODY.PEEK[]) /
LOGOUT. Deliberately no DELETE/STORE/MOVE — the real client never sends them,
and neither does this fake implement them.

Failure modes that production actually hit are simulated on request:
  * folders with modified UTF-7 names (a localized "Sent" folder)
  * one UID whose BODY.PEEK fails (drops the connection)
  * a UIDVALIDITY bump between runs
"""
from __future__ import annotations

import re
import socket
import threading


class FakeIMAPServer:
    def __init__(self, folders, *, uidvalidity="17", drop_on_body_uid=None,
                 drop_on_header_fetch=False):
        """folders: {raw_name: [ {"uid", "internaldate", "raw"}, ...]}."""
        self.folders = folders
        self.uidvalidity = str(uidvalidity)
        self.drop_on_body_uid = drop_on_body_uid
        self.drop_on_header_fetch = drop_on_header_fetch
        self.commands = []
        self.selected = []
        self.lock = threading.Lock()
        self._current = None
        self._conn = None
        self.port = 0
        self._thread = None
        self._server = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        self.port = server.getsockname()[1]
        server.listen(4)
        self._server = server
        self._thread = threading.Thread(target=self._serve, args=(server,), daemon=True)
        self._thread.start()
        return self

    def stop(self):
        for sock in (self._conn, self._server):
            try:
                if sock:
                    sock.close()
            except OSError:
                pass

    # -- protocol ----------------------------------------------------------

    def _serve(self, server):
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            self._conn = conn
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        stream = conn.makefile("rb")
        conn.sendall(b"* OK Fake IMAP4rev1 ready\r\n")
        try:
            while True:
                line = stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip("\r\n")
                if not text:
                    continue
                tag, _, rest = text.partition(" ")
                with self.lock:
                    self.commands.append(rest)
                if not self._handle(conn, tag, rest):
                    return
        except (OSError, ConnectionError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle(self, conn, tag, rest):
        upper = rest.upper()
        send = lambda data: conn.sendall(data)  # noqa: E731
        tag_b = tag.encode()

        if upper.startswith("CAPABILITY"):
            send(b"* CAPABILITY IMAP4rev1 AUTH=LOGIN\r\n"
                 + tag_b + b" OK CAPABILITY completed\r\n")
            return True
        if upper.startswith("LOGIN"):
            send(tag_b + b" OK LOGIN completed\r\n")
            return True
        if upper.startswith("LIST"):
            for raw in self.folders:
                quoted = raw if raw.startswith('"') else '"%s"' % raw
                send(("* LIST (\\HasNoChildren) \"/\" %s\r\n" % quoted)
                     .encode("utf-8", "replace"))
            send(tag_b + b" OK LIST completed\r\n")
            return True
        if upper.startswith("SELECT") or upper.startswith("EXAMINE"):
            name = rest.split(" ", 1)[1].strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            self.selected.append(name)
            self._current = name
            messages = self.folders.get(name)
            if messages is None:
                send(tag_b + (" NO no such folder: %s\r\n" % name).encode())
                return True
            send(("* %d EXISTS\r\n* 0 RECENT\r\n" % len(messages)).encode()
                 + ("* OK [UIDVALIDITY %s] UIDs valid\r\n" % self.uidvalidity).encode()
                 + b"* OK [PERMANENTFLAGS ()] read-only\r\n"
                 + tag_b + b" OK [READ-ONLY] EXAMINE completed\r\n")
            return True
        if upper.startswith("UID SEARCH"):
            uids = [str(m["uid"]) for m in self.folders[self._current]]
            send((" ".join(["* SEARCH"] + uids) + "\r\n").encode()
                 + tag_b + b" OK SEARCH completed\r\n")
            return True
        if upper.startswith("UID FETCH"):
            return self._handle_uid_fetch(conn, tag, tag_b, rest)
        if upper.startswith("LOGOUT"):
            send(b"* BYE logging out\r\n" + tag_b + b" OK LOGOUT completed\r\n")
            return False
        send(tag_b + b" OK noop\r\n")
        return True

    def _handle_uid_fetch(self, conn, tag, tag_b, rest):
        parts = rest.split(" ", 3)
        if len(parts) < 4:
            conn.sendall(tag_b + b" BAD malformed fetch\r\n")
            return True
        uid_arg, spec = parts[2], parts[3]
        uid_arg = uid_arg.strip()
        if uid_arg.isdigit():
            uids = [uid_arg]
        elif ":" in uid_arg:
            lo, _, hi = uid_arg.partition(":")
            uids = [str(u) for u in range(int(lo), int(hi) + 1)]
        else:
            uids = uid_arg.split(",")
        want_full_body = "BODY.PEEK[]" in spec or "BODY[]" in spec
        want_headers = "HEADER.FIELDS" in spec
        if self.drop_on_header_fetch and want_headers:
            conn.close()
            return False
        for index, uid in enumerate(uids):
            message = self._by_uid(uid)
            if message is None:
                continue
            if want_full_body:
                if self.drop_on_body_uid is not None and str(self.drop_on_body_uid) == str(uid):
                    conn.close()  # simulate the connection dying mid-transfer
                    return False
                raw = message["raw"]
                conn.sendall(
                    ("* %d FETCH (UID %s BODY[] {%d}\r\n" % (self._seq(uid), uid, len(raw)))
                    .encode() + raw + b")\r\n")
            elif want_headers:
                header_bytes = _header_block(message["raw"])
                conn.sendall(
                    ("* %d FETCH (UID %s FLAGS () INTERNALDATE \"%s\" RFC822.SIZE %d "
                     "BODY[HEADER] {%d}\r\n"
                     % (self._seq(uid), uid, message["internaldate"], len(message["raw"]),
                        len(header_bytes))).encode() + header_bytes + b")\r\n")
            else:
                conn.sendall(
                    ("* %d FETCH (UID %s FLAGS () INTERNALDATE \"%s\")\r\n"
                     % (self._seq(uid), uid, message["internaldate"])).encode())
        conn.sendall(tag_b + b" OK FETCH completed\r\n")
        return True

    # -- helpers -----------------------------------------------------------

    def _by_uid(self, uid):
        for message in self.folders.get(self._current, []):
            if str(message["uid"]) == str(uid):
                return message
        return None

    def _seq(self, uid):
        for index, message in enumerate(self.folders.get(self._current, []), 1):
            if str(message["uid"]) == str(uid):
                return index
        return 0


_HEADER_END = re.compile(rb"\r?\n\r?\n")


def _header_block(raw):
    """Header-only block terminated the way imaplib's parser expects."""
    parts = _HEADER_END.split(raw, 1)
    return parts[0] + b"\r\n\r\n"


def start_fake_imap(folders, **behaviors):
    return FakeIMAPServer(folders, **behaviors).start()
