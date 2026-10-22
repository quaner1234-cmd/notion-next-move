"""Tiered dedupe: ``same`` / ``near`` / ``new`` instead of duplicate-or-create.

Why three outcomes
------------------
A binary dedupe has only two ways to be wrong, and both are bad:

* too strict -> a near-duplicate of an existing row is created (the board
  fills with the same work twice);
* too loose -> genuinely new work that merely resembles an old row is
  swallowed and never surfaces (work is lost silently).

So a candidate that is *probably* the same row is not treated as a duplicate
and is not created either: it is **held for a human** and reported with its
score and the row it resembles. Nothing is lost, nothing is duplicated.

Thresholds (configurable under ``policy.dedupe``; these are the defaults
carried over from a pipeline that was verified against real data):

* ``refresh_ratio`` 0.72 — at or above this, treat as the same row;
* ``hold_ratio``    0.42 — at or above this, hold for a human;
* ``project_floor`` 0.30 — when the customer has exactly **one** open row and
  the score reaches this floor, treat it as the same row (the board keeps one
  next move per customer, so a lone row is the natural target).

Similarity is character-bigram coverage, not ``difflib.SequenceMatcher``:
on CJK text a paraphrase such as "土工材料站是否上线" vs "土工材料站上线了吗"
scores ~0.4 under SequenceMatcher but 0.5-0.9 under bigram coverage, which is
much closer to "is this the same thing".

This module is pure: no network, no disk, no Notion and no mail imports.
"""
from __future__ import annotations

import unicodedata

DEFAULT_THRESHOLDS = {"refresh_ratio": 0.72, "hold_ratio": 0.42,
                      "project_floor": 0.30}
EXACT_MODE = "exact"
TIERED_MODE = "tiered"


def normalize(text):
    """Fold text for comparison: NFKC, no whitespace, casefolded."""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = "".join(value.split())
    return value.casefold()


def bigrams(text):
    """Character-bigram set of the normalized text.

    A normalized string shorter than two characters yields a single-element
    set so that short titles are still comparable.
    """
    value = normalize(text)
    if not value:
        return set()
    if len(value) == 1:
        return {value}
    return {value[i:i + 2] for i in range(len(value) - 1)}


def similarity(left, right):
    """Bigram coverage in [0.0, 1.0]; 0.0 when either side is empty.

    Coverage is measured against the **longer** side, not with
    ``max(inter/len(a), inter/len(b))``. The max() form — what the pipeline
    this was ported from used — gives a short generic title a perfect score
    against any longer title that merely contains it: an existing row called
    ``Alpha`` scored 1.0 against ``Alpha Trading｜核价并回复`` and swallowed
    it. Dividing by the longer side keeps paraphrase detection (two titles of
    comparable length still score high) without letting a stub title absorb
    every candidate that mentions it.
    """
    a, b = bigrams(left), bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a), len(b))


def thresholds(policy=None):
    """Read thresholds from ``policy['dedupe']``, falling back to defaults.

    Bad or missing values fall back to the default rather than raising: a
    wrong threshold must not turn into a crash mid-run, and the defaults are
    the verified ones. Ordering is enforced afterwards — the floor can never
    exceed the hold ratio, nor the hold ratio the refresh ratio.
    """
    raw = ((policy or {}).get("dedupe") or {})
    out = dict(DEFAULT_THRESHOLDS)
    for key in DEFAULT_THRESHOLDS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and 0.0 <= float(value) <= 1.0:
            out[key] = float(value)
    out["hold_ratio"] = min(out["hold_ratio"], out["refresh_ratio"])
    out["project_floor"] = min(out["project_floor"], out["hold_ratio"])
    return out


def mode(policy=None):
    """``exact`` keeps the legacy binary dedupe; ``tiered`` (default) is new."""
    value = str(((policy or {}).get("dedupe") or {}).get("mode") or TIERED_MODE)
    return EXACT_MODE if value.lower() == EXACT_MODE else TIERED_MODE


def classify(rows, title, identity=None, customer=None, policy=None):
    """Classify one candidate against the existing rows.

    ``rows`` is a list of plain dicts: ``{"id", "title", "identities"
    (iterable of stable keys), "customer"}`` — callers build these from
    whatever their storage looks like, so this module stays pure.

    Returns ``{"kind", "row", "score", "reason"}`` where ``kind`` is one of:

    ==================  =====================================================
    ``exact``           the stable identity key or the normalized title
                        matches an existing row (score 1.0)
    ``same``            fuzzy score >= refresh_ratio, or the customer has a
                        single row and the score >= project_floor
    ``near``            fuzzy score >= hold_ratio — held for a human:
                        **not created, not touched**
    ``new``             nothing close enough — create
    ==================  =====================================================
    """
    limits = thresholds(policy)
    wanted = normalize(title)
    # 1. exact matches first: an identity or an identical title is never fuzzy.
    for row in rows:
        identities = {normalize(x) for x in (row.get("identities") or [])}
        if identity and normalize(identity) in identities:
            return {"kind": "exact", "row": row, "score": 1.0,
                    "reason": "identity key matches an existing row"}
    if wanted:
        for row in rows:
            if normalize(row.get("title")) == wanted:
                return {"kind": "exact", "row": row, "score": 1.0,
                        "reason": "title matches an existing row"}
    if mode(policy) == EXACT_MODE:
        return {"kind": "new", "row": None, "score": 0.0,
                "reason": "exact mode: fuzzy matching is disabled"}
    if not rows or not wanted:
        return {"kind": "new", "row": None, "score": 0.0,
                "reason": "no existing row to compare against"}

    # 2. fuzzy tiering.
    best_row, best_score = None, 0.0
    for row in rows:
        score = similarity(wanted, row.get("title"))
        if score > best_score:
            best_row, best_score = row, score
    if best_row is None:
        return {"kind": "new", "row": None, "score": 0.0,
                "reason": "no comparable row"}
    if best_score >= limits["refresh_ratio"]:
        return {"kind": "same", "row": best_row, "score": round(best_score, 4),
                "reason": f"similarity {best_score:.2f} >= refresh_ratio "
                          f"{limits['refresh_ratio']:.2f}"}
    if customer:
        same_customer = [r for r in rows if r.get("customer") == customer]
        if len(same_customer) == 1 and best_row is same_customer[0] \
                and best_score >= limits["project_floor"]:
            return {"kind": "same", "row": best_row, "score": round(best_score, 4),
                    "reason": f"customer has a single row and similarity "
                              f"{best_score:.2f} >= project_floor "
                              f"{limits['project_floor']:.2f}"}
    if best_score >= limits["hold_ratio"]:
        return {"kind": "near", "row": best_row, "score": round(best_score, 4),
                "reason": f"similarity {best_score:.2f} is between hold_ratio "
                          f"{limits['hold_ratio']:.2f} and refresh_ratio "
                          f"{limits['refresh_ratio']:.2f} — held for a human"}
    return {"kind": "new", "row": best_row, "score": round(best_score, 4),
            "reason": f"similarity {best_score:.2f} < hold_ratio "
                      f"{limits['hold_ratio']:.2f}"}
