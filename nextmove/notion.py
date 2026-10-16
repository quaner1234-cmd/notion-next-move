"""Notion REST client: only the operations this tool actually needs.

Exposure is deliberately narrow. The client can read pages/blocks, append
blocks, create pages under one configured data source, update page properties
and query one data source. There is no delete, archive or move code path.

Every write method takes ``readback=True`` to re-fetch the object and verify
what actually landed; a write without readback is not a success.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

API_ROOT = "https://api.notion.com"


class NotionError(Exception):
    """A Notion call failed after retries."""


class NotionClient:
    def __init__(self, token, version="2022-06-28", timeout=30, dry_run=False):
        self.token = token
        self.version = version
        self.timeout = timeout
        self.dry_run = dry_run
        self.planned_writes = []  # recorded intents in dry-run mode

    # -- transport ---------------------------------------------------------

    def call(self, method, path, payload=None, tries=4):
        url = API_ROOT + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        last_error = None
        for attempt in range(tries):
            request = urllib.request.Request(url, data=data, method=method)
            request.add_header("Authorization", "Bearer " + self.token)
            request.add_header("Notion-Version", self.version)
            request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass
                last_error = NotionError(f"HTTP {exc.code} {method} {path}: {detail}")
                # Rate limits and transient server errors are retried; client
                # errors (400/401/403/404) are returned immediately.
                if exc.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                    time.sleep(min(2 * (attempt + 1), 15))
                    continue
                raise last_error from exc
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_error = NotionError(f"network error {method} {path}: {exc}")
                if attempt < tries - 1:
                    time.sleep(min(2 * (attempt + 1), 15))
                    continue
        raise last_error or NotionError(f"{method} {path} failed")

    def can_reach(self):
        try:
            self.call("GET", "/v1/users/me")
            return True, "ok"
        except NotionError as exc:
            return False, str(exc)

    # -- reads -------------------------------------------------------------

    def get_page(self, page_id):
        return self.call("GET", f"/v1/pages/{page_id}")

    def get_block_children(self, block_id, page_size=100):
        out = []
        cursor = None
        while True:
            path = f"/v1/blocks/{block_id}/children?page_size={page_size}"
            if cursor:
                path += f"&start_cursor={cursor}"
            data = self.call("GET", path)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    def get_data_source(self, data_source_id):
        return self.call("GET", f"/v1/data_sources/{data_source_id}")

    def get_database(self, database_id):
        return self.call("GET", f"/v1/databases/{database_id}")

    def resolve_data_source(self, database_id, explicit_data_source_id=None):
        """Resolve the data source to write tasks into.

        People copy the *database* id out of a Notion URL, but since API
        version 2025-09-03 a database is a container that holds one or more
        data sources, and pages are created/queried against a data source.
        With exactly one data source under the database (the normal case for
        a task list) it is used automatically; a database with several needs
        ``notion.task_data_source_id`` set explicitly rather than guessing.
        """
        if explicit_data_source_id:
            data = self.get_data_source(explicit_data_source_id)
            return data.get("id", explicit_data_source_id)
        database = self.get_database(database_id)
        sources = database.get("data_sources") or []
        if len(sources) == 1:
            return sources[0].get("id")
        if not sources:
            raise NotionError(
                "the database has no data source reachable by this token — "
                "check that the integration is connected to it")
        listing = ", ".join(f"{s.get('name') or '?'}={s.get('id')}" for s in sources)
        raise NotionError(
            f"the database holds {len(sources)} data sources ({listing}); set "
            f"notion.task_data_source_id in the config to pick the one for tasks")

    def query_data_source(self, data_source_id, payload=None, page_size=100):
        """Paginate a data source query to the END; truncated results are useless
        for dedupe decisions, so an unfinished pagination raises."""
        out = []
        cursor = None
        while True:
            body = dict(payload or {})
            body["page_size"] = page_size
            if cursor:
                body["start_cursor"] = cursor
            data = self.call("POST", f"/v1/data_sources/{data_source_id}/query", body)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")
            if not cursor:
                raise NotionError("data source query said has_more but returned no cursor")

    # -- writes (all readback-verified) ------------------------------------

    def append_children(self, block_id, children, readback=True):
        if self.dry_run:
            self.planned_writes.append({"op": "append_children", "block_id": block_id,
                                        "children": len(children)})
            return {"dry_run": True}
        data = self.call("PATCH", f"/v1/blocks/{block_id}/children", {"children": children})
        if readback:
            landed = self.get_block_children(block_id)
            expected = len(children)
            got_ids = {item.get("id") for item in data.get("results", [])}
            present = [item for item in landed if item.get("id") in got_ids]
            if len(present) != expected:
                raise NotionError(f"append readback mismatch: wrote {expected} blocks, "
                                  f"read back {len(present)}")
        return data

    def create_page(self, data_source_id, properties, children=None, readback=True):
        body = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        if children:
            body["children"] = children
        if self.dry_run:
            self.planned_writes.append({"op": "create_page", "data_source_id": data_source_id,
                                        "properties": properties})
            return {"dry_run": True}
        data = self.call("POST", "/v1/pages", body)
        if readback:
            page = self.get_page(data["id"])
            if page.get("id") != data["id"]:
                raise NotionError("create readback failed: page not retrievable")
            data = {**data, "_readback": page}
        return data

    def update_page(self, page_id, properties, readback=True):
        if self.dry_run:
            self.planned_writes.append({"op": "update_page", "page_id": page_id,
                                        "properties": properties})
            return {"dry_run": True}
        data = self.call("PATCH", f"/v1/pages/{page_id}", {"properties": properties})
        if readback:
            page = self.get_page(page_id)
            for name, value in properties.items():
                if not _property_matches(page.get("properties", {}).get(name), value):
                    raise NotionError(f"update readback mismatch on property {name!r}")
            data = {**data, "_readback": page}
        return data


def _property_matches(actual, wanted):
    """Loose structural comparison for readback verification."""
    if actual is None:
        return False
    kind = actual.get("type")
    inside = actual.get(kind) if kind else None
    if kind == "status":
        return (inside or {}).get("name") == (wanted.get(kind) or {}).get("name")
    if kind == "select":
        return (inside or {}).get("name") == (wanted.get(kind) or {}).get("name")
    if kind == "rich_text":
        got = "".join(x.get("plain_text", "") for x in inside or [])
        exp = "".join(x.get("text", {}).get("content", "") for x in (wanted.get(kind) or []))
        return got == exp
    if kind == "title":
        got = "".join(x.get("plain_text", "") for x in inside or [])
        exp = "".join(x.get("text", {}).get("content", "") for x in (wanted.get(kind) or []))
        return got == exp
    if kind == "date":
        return (inside or {}).get("start") == (wanted.get(kind) or {}).get("start")
    if kind == "url":
        return inside == (wanted.get(kind))
    if kind == "people":
        got = sorted(x.get("id") for x in inside or [])
        exp = sorted(x.get("id") for x in (wanted.get(kind) or []))
        return got == exp
    return True


def rich_text(value):
    value = "" if value is None else str(value)
    chunks = [value[i:i + 1900] for i in range(0, len(value), 1900)] or [""]
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]


def paragraph(text):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": rich_text(text)}}


def heading(text, level=3):
    kind = f"heading_{level}"
    return {"object": "block", "type": kind, kind: {"rich_text": rich_text(text)}}


def bullet(text):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": rich_text(text)}}


def link_bullet(label, url):
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [
                {"type": "text", "text": {"content": label + "："}},
                {"type": "text", "text": {"content": url, "link": {"url": url}}},
            ]}}
