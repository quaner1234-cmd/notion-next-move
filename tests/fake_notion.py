#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""In-memory fake Notion API for offline tests.

Implements exactly the REST surface nextmove uses (pages create/read/update,
block children append/read, data-source read/query) with the same pagination
semantics, and records every call so tests can assert on write attempts.

It also implements the two properties the tool must respect:
  * text appended into code/paragraph blocks is retrievable again (readback);
  * data-source queries paginate rather than truncate.
"""
from __future__ import annotations

import copy
import itertools
import threading
import uuid

PAGE_SIZE = 100


class FakeNotion:
    def __init__(self, *, data_source_id, schema, status_options=None,
                 fail_writes=0, page_block_limit=100000, database_id=None):
        self.database_id = database_id or "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        self.data_source_id = data_source_id
        self.schema = schema                      # name -> type
        self.status_options = status_options or {"Todo", "In progress", "Done", "Archived"}
        self.pages = {}                           # id -> page dict
        self.blocks = {}                          # page_id -> [block dict]
        self.calls = []                           # (method, path, payload)
        self.fail_writes = fail_writes            # first N writes raise
        self.page_block_limit = page_block_limit
        self.lock = threading.Lock()
        self._ids = (f"{i:032x}" for i in itertools.count(1))

    # -- client-compatible surface ----------------------------------------

    def call(self, method, path, payload=None, tries=4):
        with self.lock:
            self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path == "/v1/users/me":
            return {"object": "user", "id": "fake", "name": "Fake Token"}
        if method == "GET" and path.startswith("/v1/databases/"):
            return {"object": "database", "id": self.database_id,
                    "data_sources": [{"object": "data_source", "id": self.data_source_id,
                                      "name": "Fake tasks"}]}
        if method == "GET" and path.startswith("/v1/data_sources/"):
            return self._data_source()
        if method == "POST" and path.endswith("/query"):
            return self._query(payload or {})
        if method == "POST" and path == "/v1/pages":
            if self._fail_next_write():
                raise RuntimeError("injected write failure")
            return self._create_page(payload)
        if method == "GET" and path.startswith("/v1/pages/"):
            return self._page(_canon(path.split("/v1/pages/")[1].split("?")[0]))
        if method == "PATCH" and path.startswith("/v1/pages/"):
            if self._fail_next_write():
                raise RuntimeError("injected write failure")
            return self._update_page(_canon(path.split("/v1/pages/")[1]), payload or {})
        if method == "PATCH" and "/children" in path:
            if self._fail_next_write():
                raise RuntimeError("injected write failure")
            return self._append_children(
                _canon(path.split("/children")[0].split("/v1/blocks/")[1]), payload or {})
        if method == "GET" and "/children" in path:
            return self._children(path)
        raise AssertionError(f"FakeNotion: unexpected call {method} {path}")

    def can_reach(self):
        return True, "ok"

    # write helpers used by the real client's readback path
    def append_children(self, block_id, children, readback=True):
        data = self.call("PATCH", f"/v1/blocks/{block_id}/children", {"children": children})
        if readback:
            _ = self._children(f"/v1/blocks/{block_id}/children?page_size=100")
        return data

    def get_page(self, page_id):
        return self._page(_canon(page_id))

    def create_page(self, data_source_id, properties, children=None, readback=True):
        page = self.call("POST", "/v1/pages", {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties, "children": children or []})
        if readback:
            page = {**page, "_readback": self._page(page["id"])}
        return page

    def update_page(self, page_id, properties, readback=True):
        self.call("PATCH", f"/v1/pages/{page_id}", {"properties": properties})
        page = self._page(_canon(page_id))
        return {**page, "_readback": page if readback else None}

    def get_data_source(self, data_source_id):
        return self._data_source()

    def resolve_data_source(self, database_id, explicit_data_source_id=None):
        return explicit_data_source_id or self.data_source_id

    def query_data_source(self, data_source_id, payload=None, page_size=PAGE_SIZE):
        out, cursor = [], None
        while True:
            body = dict(payload or {})
            body["page_size"] = page_size
            if cursor:
                body["start_cursor"] = cursor
            data = self._query(body)
            out.extend(data["results"])
            if not data.get("has_more"):
                return out
            cursor = data["next_cursor"]

    # -- internals ---------------------------------------------------------

    def _fail_next_write(self):
        if self.fail_writes > 0:
            self.fail_writes -= 1
            return True
        return False

    def _data_source(self):
        return {"object": "data_source", "id": self.data_source_id,
                "properties": {
                    name: {"id": name, "name": name, "type": kind,
                           **({"status": {"options": [{"id": o, "name": o}
                                                      for o in sorted(self.status_options)]}}
                              if kind == "status" else {})}
                    for name, kind in self.schema.items()}}

    def _new_id(self):
        return next(self._ids)

    def _empty_property(self, kind):
        return {"title": [], "rich_text": [], "status": None, "select": None,
                "date": None, "url": None, "people": []}[kind]

    def _create_page(self, payload):
        page_id = str(uuid.UUID(self._new_id()))
        properties = {}
        incoming = payload.get("properties") or {}
        for name, kind in self.schema.items():
            meta = incoming.get(name)
            if meta is None:
                properties[name] = {"id": name, "type": kind,
                                    kind: self._empty_property(kind)}
                continue
            value = meta.get(kind)
            if kind == "title":
                properties[name] = {"id": name, "type": "title",
                                    "title": _with_plain_text(value)}
            elif kind in ("rich_text",):
                properties[name] = {"id": name, "type": kind,
                                    kind: _with_plain_text(value)}
            elif kind == "status":
                name_value = (value or {}).get("name")
                if name_value is not None and name_value not in self.status_options:
                    raise AssertionError(f"FakeNotion: status {name_value!r} not an option")
                properties[name] = {"id": name, "type": "status",
                                    "status": {"id": name_value, "name": name_value}
                                    if name_value else None}
            elif kind == "select":
                properties[name] = {"id": name, "type": "select", "select": value}
            elif kind == "date":
                properties[name] = {"id": name, "type": "date", "date": value}
            elif kind == "url":
                properties[name] = {"id": name, "type": "url", "url": value}
            elif kind == "people":
                properties[name] = {"id": name, "type": "people",
                                    "people": [{"object": "user", "id": p.get("id")}
                                               for p in (value or [])]}
            else:
                raise AssertionError(f"FakeNotion: unsupported kind {kind}")
        page = {"object": "page", "id": page_id, "url": "https://www.notion.so/" + page_id.replace("-", ""),
                "properties": properties, "parent": payload.get("parent"),
                "in_trash": False}
        self.pages[page_id] = page
        self.blocks[page_id] = list(payload.get("children") or [])
        return page

    def _page(self, page_id):
        page = self.pages.get(page_id)
        if page is None:
            raise RuntimeError(f"FakeNotion: no such page {page_id}")
        return page

    def _update_page(self, page_id, payload):
        page = self._page(page_id)
        for name, meta in (payload.get("properties") or {}).items():
            kind = self.schema.get(name)
            if kind is None:
                raise AssertionError(f"FakeNotion: unknown property {name!r}")
            value = meta.get(kind)
            if kind == "status":
                wanted = (value or {}).get("name")
                if wanted is not None and wanted not in self.status_options:
                    raise AssertionError(f"FakeNotion: status {wanted!r} not an option")
                page["properties"][name] = {"id": name, "type": "status",
                                            "status": {"id": wanted, "name": wanted}
                                            if wanted else None}
            elif kind == "select":
                page["properties"][name] = {"id": name, "type": "select", "select": value}
            elif kind in ("title", "rich_text"):
                page["properties"][name] = {"id": name, "type": kind,
                                            kind: _with_plain_text(value)}
            else:
                page["properties"][name] = {"id": name, "type": kind, kind: value}
        return page

    def _append_children(self, block_id, payload):
        block_id = _canon(block_id)
        if block_id not in self.blocks:
            # block ids are page ids in this fake
            raise RuntimeError(f"FakeNotion: cannot append to {block_id}")
        results = []
        for index, block in enumerate(payload.get("children") or []):
            block_id_new = f"blk-{block_id[:6]}-{len(self.blocks[block_id]) + index:05d}"
            stored = {**_with_plain_text_block(copy.deepcopy(block)), "id": block_id_new}
            self.blocks[block_id].append(stored)
            results.append(stored)
            if len(self.blocks[block_id]) > self.page_block_limit:
                raise AssertionError("FakeNotion: page block limit exceeded")
        return {"object": "list", "results": results}

    def _children(self, path):
        after = path.split("/v1/blocks/")[1]
        block_id = _canon(after.split("/children")[0])
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(path).query)
        cursor = (query.get("start_cursor") or [None])[0]
        start = int(cursor) if cursor else 0
        everything = self.blocks.get(block_id, [])
        window = everything[start:start + PAGE_SIZE]
        next_cursor = str(start + PAGE_SIZE) if start + PAGE_SIZE < len(everything) else None
        return {"object": "list", "results": copy.deepcopy(window),
                "has_more": next_cursor is not None, "next_cursor": next_cursor}

    def _query(self, body):
        cursor = body.get("start_cursor")
        start = int(cursor) if cursor else 0
        everything = sorted(self.pages.values(), key=lambda p: p["id"])
        window = everything[start:start + int(body.get("page_size", PAGE_SIZE))]
        next_cursor = start + int(body.get("page_size", PAGE_SIZE))
        has_more = next_cursor < len(everything)
        return {"object": "list", "results": copy.deepcopy(window),
                "has_more": has_more, "next_cursor": str(next_cursor) if has_more else None}

    # -- inspection helpers for tests -------------------------------------

    def page_titles(self):
        return {pid: _title(page) for pid, page in self.pages.items()}

    def page_status(self, page_id):
        for name, meta in self.pages[page_id]["properties"].items():
            if meta.get("type") == "status":
                return (meta.get("status") or {}).get("name")
        return None

    def page_text(self, page_id):
        out = []
        for block in self.blocks.get(page_id, []):
            kind = block.get("type")
            payload = block.get(kind) or {}
            for item in payload.get("rich_text", []) or []:
                out.append(item.get("text", {}).get("content", item.get("plain_text", "")))
        return "\n".join(out)

    def write_calls(self):
        return [(m, p) for (m, p, _) in self.calls if m in ("POST", "PATCH")]


def _canon(value):
    """Canonical (dashed) form of a Notion id.

    The real API accepts both the dashed UUID and the bare 32-hex form and
    returns the dashed form; the fake must behave the same or tests would
    'fail' on a difference that does not exist in production.
    """
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return str(value)


def _title(page):
    for name, meta in page["properties"].items():
        if meta.get("type") == "title":
            return "".join(x.get("text", {}).get("content", "")
                           for x in meta.get("title") or [])
    return ""


def _with_plain_text(value):
    """Mirror the real API: stored rich text reads back with `plain_text`."""
    if not isinstance(value, list):
        return value
    out = []
    for item in value:
        if isinstance(item, dict) and "text" in item:
            out.append({**item,
                        "plain_text": item.get("plain_text",
                                               item.get("text", {}).get("content", ""))})
        else:
            out.append(item)
    return out


def _with_plain_text_block(block):
    kind = block.get("type")
    payload = block.get(kind)
    if isinstance(payload, dict) and "rich_text" in payload:
        block = {**block, kind: {**payload,
                                 "rich_text": _with_plain_text(payload["rich_text"])}}
    return block
