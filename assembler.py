#!/usr/bin/env python3
"""Anomalica assembler v0 - graph-to-page generator.

--node mode queries the digester's SQLite knowledge graph for a single node;
--record mode reads a per-record digest (digests/records/{name}.yaml). Either
way it formats claims into a prompt and asks Claude (the metered Anthropic API)
to produce a reference-style article in the Hugo content format the site expects.

Generation defaults to the local Claude CLI on Mark's Max subscription (no
metered spend, monitored live). Set ASSEMBLER_USE_API=1 to route through the
metered Anthropic API instead (reads ANTHROPIC_API_KEY - the Anomalica key);
the batch driver's spend gate covers that path. The toggle is load-bearing -
the subscription is paused-not-cancelled - so neither transport is removed.

Run on the host (the `claude` CLI is on Mark's PATH), or in any environment
carrying the CLI / the `anthropic` library:

  # subscription (default)
  python assembler.py --node "Fravor, David" --section people

  # metered API
  ASSEMBLER_USE_API=1 ANTHROPIC_API_KEY=... python assembler.py --node "..."
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml
from anomalica_common.llm import accumulate, usage_entry
from anomalica_common.slug import node_slug as _node_slug
from anomalica_common.slug import slugify


DEFAULT_DB = "/db/knowledge.db"
DEFAULT_CONTENT_ROOT = "/content"
DEFAULT_DIGESTS_ROOT = "/digests"
DEFAULT_BRIEFS_ROOT = os.environ.get("ANOMALICA_BRIEFS_DIR", "/briefs")
DEFAULT_MODEL = "sonnet"

# Entity types surfaced in a record page's `entities` breakdown, in display
# order. Deliberately a subset of SECTION_BY_TYPE: curator-only types (pattern)
# and deprecated aliases (concept/matter) are not listed as standalone groups.
ENTITY_TYPES = [
    "person",
    "place",
    "event",
    "organisation",
    "project",
    "object",
    "topic",
    "document",
]

# Workbench origin used to build the per-claim deep-link URL stamped into
# each reference. Override via env var when the workbench is deployed
# elsewhere. Public-hash convention: first 56 chars of the SHA-256
# content_hash (no "sha256:" prefix). Matches the URL the workbench uses
# for its existing record routes.
WORKBENCH_ORIGIN = os.environ.get(
    "ANOMALICA_WORKBENCH_ORIGIN", "http://localhost:5173"
).rstrip("/")
PUBLIC_HASH_LENGTH = 56

# Frontmatter keys the assembler NEVER generates - they are human-authored
# (reviewers write `directives` strings via the workbench). A (re)assembly fully
# overwrites the article file, so these are carried across from the existing file
# or the human's edit is lost. The model owns everything else (title, tags,
# description, references, ...); tags stay model-owned (grounded tags supersede
# them), so tags are deliberately NOT here.
_PRESERVE_KEYS = ("directives",)

# Section the article goes into is mapped from the node's type. The site's
# Hugo layout expects content/english/{section}/{slug}.en.md.
SECTION_BY_TYPE = {
    "person": "people",
    "organisation": "organisations",
    "place": "places",
    "event": "events",
    "object": "objects",
    "document": "documents",
    # ADR 0029 current taxonomy:
    "project": "projects",  # collapses the old programme + investigation
    "topic": "topics",  # renamed from concept
    "pattern": "patterns",  # curator-created, not extractor-emitted
    # Deprecated types kept for back-compat with older DB state:
    "matter": "matters",
    "concept": "concepts",
    "programme": "programmes",
    "investigation": "investigations",
    "principle": "topics",  # transient pre-0029 name -> routes to /topics/
    # Per-record narrative pages (--record mode): one page per ingested
    # source artefact, routed to /records/{friendly_name}.
    "source": "records",
}


# ----------------------------------------------------------------------------
# Graph queries
# ----------------------------------------------------------------------------


def load_node(conn: sqlite3.Connection, name_or_id: str) -> dict | None:
    """Look up a node by exact name (case-insensitive) or by uuid."""
    row = conn.execute(
        "SELECT id, node_type, name, metadata FROM nodes "
        "WHERE id = ? OR lower(name) = lower(?) LIMIT 1",
        (name_or_id, name_or_id),
    ).fetchone()
    if not row:
        return None
    md = json.loads(row[3]) if row[3] else None
    return {"id": row[0], "type": row[1], "name": row[2], "metadata": md}


def claims_for_node(conn: sqlite3.Connection, node_id: str) -> list[dict]:
    """Pull every claim that references the node OR was spoken by them.

    Each row carries the claim's id (for workbench deep-link), the source
    record's content_hash + friendly_name (for the workbench URL), the
    original_excerpt verbatim, plus the usual content/attestation/date/etc.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT
            c.id, c.content, c.original_excerpt, c.claim_type, c.attestation,
            c.location_in_record, c.date, c.date_end,
            r.title, r.date, r.reference, r.content_hash, r.friendly_name,
            s.name AS speaker_name,
            CASE
                WHEN c.speaker_id = ? THEN 'speaker'
                ELSE 'ref'
            END AS link_kind
        FROM claims c
        LEFT JOIN records r ON c.record_id = r.id
        LEFT JOIN nodes s ON c.speaker_id = s.id
        LEFT JOIN claim_node_refs cnr ON cnr.claim_id = c.id
        WHERE c.speaker_id = ? OR cnr.node_id = ?
        ORDER BY r.date, c.location_in_record
        """,
        (node_id, node_id, node_id),
    ).fetchall()

    seen_ids: set[str] = set()
    out: list[dict] = []
    for row in rows:
        cid = row[0]
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        out.append(
            {
                "id": cid,
                "content": row[1],
                "original_excerpt": row[2],
                "claim_type": row[3],
                "attestation": row[4],
                "location": row[5],
                "date": row[6],
                "date_end": row[7],
                "record_title": row[8] or "(unknown record)",
                "record_date": row[9] or "",
                "record_reference": row[10],
                "record_content_hash": row[11],
                "record_friendly_name": row[12],
                "speaker": row[13] or "",
                "link_kind": row[14],
            }
        )
    return out


def related_nodes(conn: sqlite3.Connection, node_id: str) -> list[dict]:
    """Other nodes that co-appear in claims with this one. Used so the article
    body can link to them ([2004 USS Nimitz encounter](/events/...))."""
    rows = conn.execute(
        """
        SELECT n.id, n.node_type, n.name, n.metadata, COUNT(*) AS shared
        FROM claim_node_refs a
        JOIN claim_node_refs b ON a.claim_id = b.claim_id AND b.node_id != a.node_id
        JOIN nodes n ON n.id = b.node_id
        WHERE a.node_id = ?
        GROUP BY n.id
        ORDER BY shared DESC
        LIMIT 30
        """,
        (node_id,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "type": r[1],
            "name": r[2],
            "metadata": json.loads(r[3]) if r[3] else None,
            "shared_claims": r[4],
        }
        for r in rows
    ]


def _claim_refs(claim: dict) -> list[str]:
    """Node names a digest claim references (refs entries are {id, name})."""
    out = []
    for r in claim.get("refs") or []:
        nm = r.get("name") if isinstance(r, dict) else r
        if nm:
            out.append(nm)
    return out


def load_digest(digests_root: Path, ref: str) -> tuple[dict, str] | None:
    """Locate and parse a per-record digest (digests/records/{name}.yaml, the
    canonical per-record output per ADR 0027). `ref` is a friendly_name (the
    digest filename stem), a path to a digest file, or a record id / title
    matched by scanning. Returns (digest, friendly_name) or None.
    """
    p = Path(ref)
    if p.suffix in {".yaml", ".yml"} and p.is_file():
        return yaml.safe_load(p.read_text()), p.stem
    direct = digests_root / "records" / f"{ref}.yaml"
    if direct.is_file():
        return yaml.safe_load(direct.read_text()), ref
    rec_dir = digests_root / "records"
    if rec_dir.is_dir():
        for f in sorted(rec_dir.glob("*.yaml")):
            d = yaml.safe_load(f.read_text())
            rec = d.get("record") or {}
            if ref in (rec.get("id"), rec.get("title")):
                return d, f.stem
    return None


def record_node(digest: dict, friendly_name: str) -> dict:
    """Synthetic 'source' node so a record flows through the shared prompt /
    section / slug machinery. friendly_name is the URL slug."""
    rec = digest.get("record") or {}
    return {
        "id": rec.get("id") or friendly_name,
        "type": "source",
        "name": rec.get("title") or friendly_name,
        "metadata": {"explicit_slug": friendly_name},
        "content_hash": rec.get("content_hash"),
        "friendly_name": friendly_name,
    }


def claims_from_digest(digest: dict) -> list[dict]:
    """Map digest domain_claims to the claim-dict shape format_claim and
    _check_date_fidelity consume. Document order is the digest's own order."""
    rec = digest.get("record") or {}
    out: list[dict] = []
    for c in digest.get("domain_claims") or []:
        sp = c.get("speaker")
        sp = sp.get("name") if isinstance(sp, dict) else sp
        out.append(
            {
                "id": c.get("id"),
                "content": c.get("text", ""),
                "original_excerpt": c.get("quote"),
                "claim_type": c.get("type", "observation"),
                "attestation": c.get("attestation") or "",
                "location": c.get("location"),
                "date": c.get("date"),
                "date_end": None,
                "record_title": rec.get("title") or "(unknown record)",
                "record_date": rec.get("date") or "",
                "record_content_hash": rec.get("content_hash"),
                "speaker": sp or "",
                "refs": _claim_refs(c),
                "link_kind": "ref",
            }
        )
    return out


def related_from_digest(digest: dict) -> list[dict]:
    """ALL entities this record references, ranked by how many domain_claims
    mention each, as the linkable set for the narrative. Uncapped (over-linking:
    the model links every entity it mentions; the site strips links whose page
    doesn't exist), canonical slug per node via the shared slugifier."""
    counts: dict[str, int] = {}
    for c in digest.get("domain_claims") or []:
        for nm in _claim_refs(c):
            counts[nm] = counts.get(nm, 0) + 1
    nodes = [
        {
            "id": n.get("id"),
            "type": n.get("type"),
            "name": n.get("name"),
            "metadata": None,
            "shared_claims": counts.get(n.get("name"), 0),
        }
        for n in digest.get("nodes") or []
        if n.get("name")
    ]
    nodes.sort(key=lambda x: x["shared_claims"], reverse=True)
    return nodes


def entity_url(node_type: str, name: str, content_root: Path) -> str | None:
    """Encyclopaedia link for an entity: a bare, language-agnostic /<section>/<slug>
    (the site adds the language prefix at render time). Returned only when the
    page exists in the content repo, else null - the site renders plain text.

    NB the slug is minted from the entity's per-record surface name, which can
    diverge from the canonical entity-page slug across extractions. The existence
    check makes that safe (mismatches yield null, never a broken link) but misses
    real pages under a different canonical slug; resolving slugs from the graph's
    canonical node name is the proper fix, pending cross-document naming work."""
    section = SECTION_BY_TYPE.get(node_type, (node_type or "") + "s")
    slug = slugify(name)
    if output_path(content_root, section, slug).is_file():
        return f"/{section}/{slug}"
    return None


def _format_duration(seconds) -> str | None:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return None
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def record_metadata(digest: dict) -> dict:
    """Source metadata for the page header, from the digest record block. The
    publisher line prefers the `publisher` field (right for broadcast/web), and
    falls back to `producer` (the ingest authors, set for authored sources).
    Absent fields are omitted."""
    rec = digest.get("record") or {}
    md = {
        "medium": rec.get("medium"),
        "date": (str(rec.get("date") or "")[:10] or None),
        "publisher": rec.get("publisher") or rec.get("producer"),
        "duration": _format_duration(rec.get("duration")),
    }
    return {k: v for k, v in md.items() if v}


# NOTE: build_entities / build_facts are retained but NOT currently emitted - the
# record page dropped the inline facts/entities QA breakdown (Mark's call: it lives
# in the workbench now, reached via the page's record_hash link). Re-enabling the
# inline breakdown is a render_record_page change plus restoring the two calls in
# main(). Kept because the breakdown logic is tested and may return as a
# reviewer-preview block.
def build_entities(digest: dict, content_root: Path) -> dict:
    """Entities grouped by type for the collapsed breakdown. url resolves to the
    encyclopaedia page when one exists, else null."""
    nodes = digest.get("nodes") or []
    out: dict[str, list] = {}
    for t in ENTITY_TYPES:
        items = [
            {"name": n["name"], "url": entity_url(t, n["name"], content_root)}
            for n in nodes
            if n.get("type") == t and n.get("name")
        ]
        if items:
            out[t] = items
    return out


def build_facts(digest: dict, content_root: Path) -> list[dict]:
    """One self-contained fact card per domain claim, in document order. Each
    carries its own quote/location/refs/workbench_url - the facts are NOT indexed
    into the article references."""
    rec = digest.get("record") or {}
    ntype = {n.get("name"): n.get("type") for n in digest.get("nodes") or []}
    public = _public_hash(rec.get("content_hash"))
    facts: list[dict] = []
    for c in digest.get("domain_claims") or []:
        sp = c.get("speaker")
        sp = sp.get("name") if isinstance(sp, dict) else sp
        fact: dict = {"text": c.get("text", "")}
        if sp:
            fact["speaker"] = sp
        if c.get("attestation"):
            fact["attestation"] = c["attestation"]
        fact["type"] = c.get("type", "observation")
        refs = []
        for nm in _claim_refs(c):
            t = ntype.get(nm, "topic")
            refs.append({"name": nm, "type": t, "url": entity_url(t, nm, content_root)})
        if refs:
            fact["refs"] = refs
        if c.get("quote"):
            fact["quote"] = c["quote"]
        if c.get("location"):
            fact["location"] = c["location"]
        cid = c.get("id")
        if cid:
            # The site sets id="claim-{claim_id}" on each fact card; a public
            # claim link's #claim-<uuid> fragment lands here. Anchor is emitted
            # independently of the (review-only, gated) workbench_url.
            fact["claim_id"] = cid
            if public:
                fact["workbench_url"] = f"{WORKBENCH_ORIGIN}/{public}#claim-{cid}"
        facts.append(fact)
    return facts


# ----------------------------------------------------------------------------
# Brief queries - entity articles from synthesiser briefs (anomalica/brief/1).
# The brief is the SOLE source: render its claims, invent nothing. page.slug and
# related_nodes[].slug are the FINAL (canonical + disambiguated) URLs - consumed
# verbatim, never re-slugified.
# ----------------------------------------------------------------------------


def load_brief(briefs_root: Path, ref: str) -> tuple[dict, str] | None:
    """Locate and parse a synthesiser brief. `ref` is a page slug (the filename
    stem) or a path to a brief .yaml. Returns (brief, slug) or None."""
    p = Path(ref)
    if p.suffix in {".yaml", ".yml"} and p.is_file():
        return yaml.safe_load(p.read_text()), p.stem
    direct = briefs_root / f"{ref}.yaml"
    if direct.is_file():
        return yaml.safe_load(direct.read_text()), ref
    return None


def brief_node(brief: dict) -> dict:
    """Synthetic node from the brief's page block. page.slug is the FINAL,
    disambiguated URL slug - carried as explicit_slug so node_slug returns it
    verbatim (never re-slugified from the title)."""
    pg = brief.get("page") or {}
    return {
        "id": pg.get("node_id"),
        "type": pg.get("node_type"),
        "name": pg.get("title") or "",
        "metadata": {"explicit_slug": pg.get("slug")},
    }


def claims_from_brief(brief: dict) -> list[dict]:
    """Map the brief's ordered claim selection to the claim-dict shape
    format_claim / _augment_references / _check_date_fidelity consume, carrying
    claim_hash for the built_from freeze."""
    out: list[dict] = []
    for c in brief.get("claims") or []:
        sp = c.get("speaker")
        sp_name = sp.get("title") if isinstance(sp, dict) else sp
        prov = c.get("provenance") or {}
        out.append(
            {
                "id": c.get("claim_id"),
                "claim_hash": c.get("claim_hash"),
                "content": c.get("content", ""),
                "original_excerpt": c.get("original_excerpt"),
                "claim_type": c.get("claim_type", "observation"),
                "attestation": c.get("attestation") or "",
                "location": c.get("location_in_record"),
                "date": c.get("date"),
                "date_end": c.get("date_end"),
                "record_title": prov.get("record_title") or "(unknown record)",
                "record_date": prov.get("record_date") or "",
                "record_reference": prov.get("record_reference"),
                "record_content_hash": prov.get("content_hash"),
                "record_friendly_name": prov.get("friendly_name"),
                "speaker": sp_name or "",
                "link_kind": "ref",
            }
        )
    # Deliberately NOT consumed in v1 (entity-article shape doesn't surface them):
    # claim.node_refs (per-claim entity chips are a record-page feature) and
    # claim.evidence.independent_sources (forward-provisioned, neutral until
    # evidence-scoring pins). Both remain in the brief if a future version wants
    # them; node_refs carries node_id so links could resolve to canonical slugs.
    return out


def related_from_brief(brief: dict) -> list[dict]:
    """The brief's related_nodes ("you may link to" set), most-shared first as
    the synthesiser ranked them. Each carries its FINAL slug as explicit_slug so
    cross-links use it verbatim."""
    return [
        {
            "id": rn.get("node_id"),
            "type": rn.get("node_type"),
            "name": rn.get("title"),
            "metadata": {"explicit_slug": rn.get("slug")},
            "shared_claims": rn.get("shared_claims", 0),
        }
        for rn in brief.get("related_nodes") or []
        if rn.get("title")
    ]


def built_from_block(brief: dict) -> dict:
    """The article-level audit field: the brief's brief_hash + the ORDERED list of
    {id, hash} the brief contained. Copied verbatim - the assembler computes no
    hash (single source: assimilator owns claim_hash, synthesiser owns brief_hash).
    Lets staleness detection diff a page's built brief against a rebuilt one."""
    return {
        "brief_hash": brief.get("brief_hash"),
        "claims": [
            {"id": c.get("claim_id"), "hash": c.get("claim_hash")}
            for c in brief.get("claims") or []
        ],
    }


def gather_upstream_ai_usage(claims: list[dict], digests_root: Path) -> list[dict]:
    """The carried-forward AI-usage upstream for an entity article (brief / node
    mode, which draws from many records): each contributing source record's
    digest ai_usage, concatenated, deduped at the SOURCE-RECORD level (a record
    cited by many claims contributes its digest chain once). Record-mode pages
    don't use this - they have the single source digest in hand.

    A digest is loaded by its friendly_name (the digest filename stem); a record
    whose digest is missing or carries no ai_usage simply contributes nothing.
    """
    seen: set[str] = set()
    upstream: list[dict] = []
    for c in claims:
        content_hash = c.get("record_content_hash")
        friendly_name = c.get("record_friendly_name")
        if not content_hash or content_hash in seen:
            continue
        seen.add(content_hash)
        if not friendly_name:
            continue
        loaded = load_digest(digests_root, friendly_name)
        if loaded:
            upstream.extend(loaded[0].get("ai_usage") or [])
    return upstream


# ----------------------------------------------------------------------------
# Slugs and paths
# ----------------------------------------------------------------------------


# slugify + node_slug are imported from anomalica_common.slug - the single
# canonical slugifier shared with the assimilator/synthesiser, so a brief's
# page.slug and the assembler's deployed page slug cannot drift (same discipline
# as claim_hash). This thin adapter keeps the node-dict call interface; it holds
# no slug logic of its own.
def node_slug(node: dict) -> str:
    """URL slug for a node dict, via the canonical slugifier. Honours
    metadata.explicit_slug (ADR 0028 pattern short URLs)."""
    return _node_slug(node.get("name", ""), node.get("metadata"))


def output_path(content_root: Path, section: str, slug: str, lang: str = "en") -> Path:
    # Hugo mounts content/pages/ to content/. So generated articles
    # for the public site live under pages/<section>/<slug>.<lang>.md - URLs
    # then come out as /<section>/<slug>/ to match the site's existing
    # /people/david-fravor pattern.
    return content_root / "pages" / section / f"{slug}.{lang}.md"


# ----------------------------------------------------------------------------
# Prompt
# ----------------------------------------------------------------------------

ASSEMBLY_PROMPT = """You are writing a reference-style article for the Anomalica website, drawing only from the knowledge graph data provided below.

THE NODE THIS ARTICLE IS ABOUT:
- name: {node_name}
- type: {node_type}
{related_block}

INSTRUCTIONS:

Write a single article in British English about this {node_type}. The article goes on a public reference website (Anomalica) about unidentified anomalous phenomena (UAP) research. Tone: neutral, encyclopaedic, like a Wikipedia article. Do not editorialise. Do not advocate.

SPELLING - British English throughout, with ONE exception: proper nouns keep their official/original spelling. Never Briticise the words inside an official American name. US government bodies retain American spelling - "Department of Defense" (never "Defence"), "Defense Intelligence Agency" (never "Defence Intelligence Agency"), "Secretary of Defense", "Office of the Secretary of Defense"; and official US programme names keep "Program" - "Advanced Aerospace Threat Identification Program" (never "Programme"). The British forms (defence, programme, organisation, ...) are correct only as ordinary words, not when they sit inside a proper noun that is spelled the American way officially.
{directives_block}
Your ENTIRE response must be a single YAML+markdown document in this exact shape:

---
title: "<the node's display name>"
description: "<one-sentence PLAIN-TEXT summary of what this {node_type} is - no markdown (no *italics*, **bold**, or backticks); this also becomes the page's search-engine description>"
metadata:
  <type-appropriate fields - role/affiliation/rank for persons; date/location for events; founded/headquartered for organisations; status/type for objects; etc. Omit metadata block entirely if you have nothing to say>
references:
  - text: "<short paraphrase of the cited claim>"
    source: "<record title, e.g. 'USS Nimitz Executive Summary'>"
    location: "<location in record, e.g. 'p. 2' or '2020-09-08, 00:34:12'>"
    claim_index: <the 1-based index from the KNOWLEDGE GRAPH CLAIMS list below>
  - <one entry per cited claim>
---

<body prose, 3-6 paragraphs of British English, with inline citations as <sup>N</sup> matching the references list. Use ISO dates (2004-11-14). Don't invent any facts - every assertion must trace back to a CLAIM in the data below. Use the SAFE acronyms bare (UFO, UAP, CIA, FBI, NSA, NASA, DOD, FAA, NATO, UN, EU, US, USA, UK, USSR, GPS); expand domain acronyms on first use (Anomalous Aerial Vehicle (AAV), forward-looking infrared (FLIR), Advanced Aerospace Threat Identification Program (AATIP)).

Within the body, link entities using markdown links of the form [Display Name](/<section>/<slug>), taking the slug verbatim from the LINKABLE ENTITIES list above. Link EVERY entity from that list the first time you mention it in the body - over-link rather than under-link. The site automatically removes any link whose target page does not yet exist, so a link that turns out dead costs nothing, but a missing link loses a real connection - so when in doubt, link it. For example: [2004 USS Nimitz encounter](/events/2004-uss-nimitz-encounter), [Strike Fighter Squadron 41 (VFA-41)](/organisations/strike-fighter-squadron-41-vfa-41). Use bare language-agnostic paths (no /en/ prefix); the site adds the language prefix at render time. Use ONLY slugs from the LINKABLE ENTITIES list - never invent or guess a slug for an entity not in the list (a guessed slug is silently dropped, losing the link). Use plain markdown links only, never raw HTML anchors.

For each <sup>N</sup> citation, ensure references[N-1] in the frontmatter is the matching source. Reference numbering must be sequential starting at 1. Each reference MUST carry a `claim_index` field with the 1-based index of the originating claim in the KNOWLEDGE GRAPH CLAIMS list below - this index lets downstream tooling link the reference back to the exact source claim in the workbench.

CITATION FIDELITY RULE (this is the most important rule and the easiest to break):

Every <sup>N</sup> must cite a CLAIM in the data below whose content DIRECTLY supports the assertion you just wrote - not one that supports it via inference, temporal logic, or implied negation.

Example of the failure mode to avoid:
- You write: "The Pentagon had never previously confirmed the programme's existence."<sup>3</sup>
- Reference 3 says: "AATIP began as part of the Defense Intelligence Agency and was acknowledged by Pentagon officials in December 2017."
- That citation does NOT directly state "never previously confirmed". It states acknowledgement in December 2017, and you inferred the negation. This is a bad citation. Either find a claim that DIRECTLY states "no prior acknowledgement" / "first confirmation", or soften your assertion ("The 2017 New York Times reporting marked the Pentagon's first public acknowledgement of the programme."<sup>3</sup>) so what you wrote matches what the claim says.

The test: read the cited claim's text. Could it be paraphrased to the sentence you wrote without adding new logic or temporal inference? If not, your citation is wrong - either fix the wording, drop the assertion, or pick a different claim that does support it directly.

CITE AT THE LEVEL OF THE SPECIFIC, NOT THE SENTENCE:

When one sentence asserts several specifics drawn from different claims (a shape AND a list of negatives; a location AND a distance AND a duration; a rank AND a unit AND a date), attach a separate <sup>N</sup> to each specific, citing the claim that actually contains THAT specific - e.g. "...no wings,<sup>14</sup> roughly 97 kilometres away,<sup>22</sup> in under a minute<sup>31</sup>". A single <sup> at the end of a multi-fact sentence is wrong unless one claim contains every one of those facts. (Failure example: writing "shaped like a Tic Tac with no wings and no exhaust"<sup>19</sup> when claim 19 describes only the Tic Tac shape - the "no wings"/"no exhaust" specifics live in other claims and must carry their own citations.)

NEVER state a concrete specific - a number (distance, time, altitude, speed, count, dimension, dollar figure) or a discrete attribute ("no wings", "no exhaust") - unless that exact specific appears in the claim you cite for it. If no claim states a specific you want to write, leave it out. Do not approximate, round, unit-convert, or interpolate a figure that no claim states.

The check for every <sup>N</sup>: take the clause it sits on, list each concrete specific in that clause, and confirm every one appears in references[N-1]'s claim. If any is missing from that claim, the citation is wrong - move the <sup> to the clause its claim supports, split the sentence, or drop the unsupported specific.

If you cannot find a claim that directly supports an assertion you want to make, do not make the assertion. The article must be assembled from what the graph says, not from what the graph implies.

KNOWLEDGE GRAPH CLAIMS (all available evidence for this {node_type}):

{claims_block}

OUTPUT - the article as described above, starting with --- and ending with the closing body paragraph. No commentary, no JSON wrapper.
"""


def format_related_block(related: list[dict]) -> str:
    if not related:
        return ""
    lines = [
        "",
        "LINKABLE ENTITIES - link every one you mention in the body, using its "
        "slug verbatim (these are the only valid slugs):",
    ]
    for r in related:
        t = r.get("type") or "topic"
        section = SECTION_BY_TYPE.get(t, t + "s")
        slug = node_slug(r)
        lines.append(
            f"  - [{r['name']}](/{section}/{slug}) - {t}, "
            f"co-appears in {r.get('shared_claims', 0)} claims"
        )
    return "\n".join(lines)


def _load_directives_file(path: Path) -> list[str]:
    """A _directives.yaml is a list of presentational instruction strings (or a
    mapping carrying a `directives:` list). Missing or malformed -> []."""
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return []
    if isinstance(data, dict):
        data = data.get("directives")
    if not isinstance(data, list):
        return []
    return [str(s).strip() for s in data if str(s) and str(s).strip()]


def collect_directives(
    out_path: Path, content_root: Path, lang: str = "en"
) -> list[str]:
    """Gather presentational directives most-specific first:
    1. the existing article's own frontmatter `directives` (article + this language);
    2. the per-article sidecar `<slug>.directives.yaml` (article, all languages);
    3. `_directives.{lang}.yaml` then `_directives.yaml` at each folder from the
       article's directory up to the content root (folder-level, broader).
    More-specific (earlier) directives win on conflict; duplicates collapse to
    their most-specific position.

    The content layout is flat with a language suffix (pages/<section>/<slug>.<lang>.md),
    not the per-language directory tree the architecture doc sketched, so the
    broader-directive hierarchy is the folder chain, language is a file suffix
    (`_directives.en.yaml`), and a single-article all-languages directive lives in
    the per-article `<slug>.directives.yaml` sidecar rather than in 30 frontmatters."""
    out: list[str] = []
    # 1. Article + THIS language: the existing file's reviewer-authored
    #    frontmatter. Most specific - a language-specific phrasing rule.
    if out_path.is_file():
        parsed = _split_article(out_path.read_text())
        if parsed:
            fm_dirs = parsed[0].get("directives")
            if isinstance(fm_dirs, list):
                out.extend(str(s).strip() for s in fm_dirs if str(s) and str(s).strip())
    # 1b. Article + ALL languages: a per-article sidecar <slug>.directives.yaml
    #     next to the article files. The canonical home for a language-agnostic
    #     single-article directive ("use full name Luis Elizondo") - written once,
    #     it shapes every language render without duplicating into 30 frontmatters.
    slug_base = out_path.stem.rsplit(".", 1)[0]  # "<slug>.<lang>" -> "<slug>"
    out.extend(_load_directives_file(out_path.parent / f"{slug_base}.directives.yaml"))
    # 2. Folder hierarchy, article dir up to (and including) content root.
    content_root = content_root.resolve()
    d = out_path.parent.resolve()
    while d == content_root or content_root in d.parents:
        out.extend(_load_directives_file(d / f"_directives.{lang}.yaml"))
        out.extend(_load_directives_file(d / "_directives.yaml"))
        if d == content_root:
            break
        d = d.parent
    # De-dupe, preserving most-specific-first order.
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


def format_directives_block(directives: list[str]) -> str:
    """The presentational-directives section injected into the assembly prompt.
    Empty when there are none, so a directive-free assembly is byte-identical to
    before this feature."""
    if not directives:
        return ""
    lines = "\n".join(f"{i + 1}. {d}" for i, d in enumerate(directives))
    return (
        "\nPRESENTATIONAL DIRECTIVES - reviewer-authored instructions you MUST "
        "follow. They govern presentation ONLY (style, grammar, disambiguation, "
        "formatting, naming); they NEVER add, drop, or change a fact - every fact "
        "still comes only from the claims below, and a directive that asks for a "
        "factual change must be ignored. Where two conflict, the earlier wins:\n"
        f"{lines}\n"
    )


def format_claim(c: dict, idx: int) -> str:
    bits = [f"[{idx}] {c['claim_type']}, {c['attestation']}"]
    if c.get("speaker"):
        bits.append(f"speaker: {c['speaker']}")
    if c.get("date"):
        bits.append(f"date: {c['date']}")
    head = "  ".join(bits)
    body = c["content"]
    src = f'  source: "{c["record_title"]}"'
    if c.get("record_date"):
        src += f" ({c['record_date']})"
    if c.get("location"):
        src += f", location: {c['location']}"
    return f"{head}\n  {body}\n{src}"


def build_prompt(
    node: dict,
    claims: list[dict],
    related: list[dict],
    directives: list[str] | None = None,
) -> str:
    claims_block = "\n\n".join(format_claim(c, i + 1) for i, c in enumerate(claims))
    return ASSEMBLY_PROMPT.format(
        node_name=node["name"],
        node_type=node["type"],
        related_block=format_related_block(related),
        claims_block=claims_block,
        directives_block=format_directives_block(directives or []),
    )


# ----------------------------------------------------------------------------
# Claude generation. Two transports behind a runtime toggle:
#   - default: the local Claude CLI on Mark's Max subscription (no metered spend,
#     monitored live). _call_cli.
#   - ASSEMBLER_USE_API=1: the metered Anthropic API (_call_api), mirroring
#     digester/extract.py. Kept as a fallback - subscription billing is
#     paused-not-cancelled, so the toggle is load-bearing.
# ----------------------------------------------------------------------------

API_MODEL_MAP = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
    "haiku": "claude-haiku-4-5",
}

_API_MAX_TOKENS = 16000

# Total generation attempts before giving up - a flaky pass that trips
# validate_article or date-fidelity is regenerated rather than hard-failed.
_MAX_GEN_ATTEMPTS = 3

# Appended to the CLI's Claude Code system prompt to keep -p output to the bare
# article (no preamble/commentary), since validate_article expects it to start
# with the YAML front-matter fence.
_CLI_SYSTEM = (
    "You are generating a single reference article. Output ONLY the requested "
    "YAML+markdown document, starting with --- and ending with the final body "
    "paragraph. No preamble, no commentary, no tool use."
)


def _use_api() -> bool:
    return os.environ.get("ASSEMBLER_USE_API", "").lower() in ("1", "true", "yes")


# Token-usage accounting for the public AI-usage provenance (ADR 0037). The
# assembler has its own (generation-shaped) transport, so it accumulates usage
# locally in the field shape anomalica_common.llm.usage_entry consumes, rather
# than feeding the shared accumulator (whose feed is private). Both transports
# report usage - the subscription CLI in its JSON wrapper (usage +
# total_cost_usd, the cache-aware figure), the API in message.usage. Scoped per
# article: reset before the generation loop, so retries accumulate into one entry.
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_usage: dict = {}


def _reset_usage() -> None:
    global _usage
    _usage = {f: 0 for f in _USAGE_FIELDS}
    _usage["cost_equiv_usd"] = 0.0


def _record_usage(usage: dict | None, cost_usd: float | None) -> None:
    if not _usage:
        _reset_usage()
    if usage:
        for f in _USAGE_FIELDS:
            _usage[f] += int(usage.get(f) or 0)
    if cost_usd:
        _usage["cost_equiv_usd"] += float(cost_usd)


def _get_usage() -> dict:
    if not _usage:
        _reset_usage()
    return dict(_usage)


def call_claude(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Generate the article. Defaults to the Claude subscription via the CLI;
    set ASSEMBLER_USE_API=1 to route through the metered Anthropic API instead."""
    return _call_api(prompt, model) if _use_api() else _call_cli(prompt, model)


def _call_cli(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Generate via the local Claude CLI on Mark's Max subscription (the default).

    Pins --model explicitly (no flag defaults to Opus 1M, the most rate-limit
    hungry) and disables tools so it is a pure generation, not the agentic stack.
    Strips the CLAUDECODE / CLAUDE_CODE_* markers so the subprocess is not treated
    as nested Claude Code. The full prompt is the user turn via stdin.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")
    }
    cmd = [
        "claude",
        "-p",
        "--tools",
        "",
        "--model",
        model,
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--append-system-prompt",
        _CLI_SYSTEM,
    ]
    proc = subprocess.run(
        cmd, input=prompt, env=env, capture_output=True, text=True, timeout=900
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {proc.stderr[:500]}")
    try:
        wrapper = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout
    # Capture usage for the public AI-usage provenance. total_cost_usd is the
    # CLI's cache-aware list-price figure (a naive tokens x rate is ~7x off).
    _record_usage(wrapper.get("usage"), wrapper.get("total_cost_usd"))
    return wrapper.get("result", proc.stdout)


def _call_api(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Generate via the metered Anthropic API (ASSEMBLER_USE_API=1). Reads
    ANTHROPIC_API_KEY from the environment (the metered Anomalica key). Streams so
    a large claims prompt doesn't trip the SDK's non-streaming time guard."""
    import anthropic

    model_id = API_MODEL_MAP.get(model, model)
    client = anthropic.Anthropic()
    with client.messages.stream(
        model=model_id,
        max_tokens=_API_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            f"API response hit max_tokens ({_API_MAX_TOKENS}); article truncated. "
            "Raise _API_MAX_TOKENS."
        )
    # Capture usage. The SDK doesn't return a dollar cost, so cost is None and
    # usage_entry falls back to a tokens x list-price notional for the metered
    # path (the subscription path gets the cache-aware total_cost_usd instead).
    u = getattr(message, "usage", None)
    if u:
        _record_usage(
            {
                "input_tokens": getattr(u, "input_tokens", 0) or 0,
                "output_tokens": getattr(u, "output_tokens", 0) or 0,
                "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0)
                or 0,
                "cache_creation_input_tokens": getattr(
                    u, "cache_creation_input_tokens", 0
                )
                or 0,
            },
            None,
        )
    for block in message.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise RuntimeError(f"API returned no text. stop_reason={message.stop_reason}")


# ----------------------------------------------------------------------------
# Output parsing
# ----------------------------------------------------------------------------


def _sanitise_frontmatter_yaml(fm_text: str) -> str:
    """Repair common model-emitted YAML quirks before parsing.

    Two distinct shapes the model produces:

    1. `key: "Value" trailing-text` where trailing-text is a bare parenthetical
       or similar (e.g. ' source: "Title" (2023-06)' - the closing quote ends
       the YAML string but the parenthetical date is then bare). The trailing
       text belongs INSIDE the quoted value. Merge it in.

    2. `key1: "Value1" key2: "Value2"` where the model jammed two key-value
       pairs onto a single line. The trailing pair belongs on its OWN line.
       Split it out. Eating the second key into the first value silently
       throws data away (broke Roswell title, Whitaker references on
       2026-05-23).

    Apply 2 first - splitting cannot break shape 1, but merging would
    permanently destroy shape 2.
    """
    # NOTE: use [ \t]+ for "whitespace between two pieces on the same line"
    # rather than \s+, because \s also matches \n - and a newline-spanning
    # match would either merge data across lines (the bug we just fixed) or
    # accidentally consume an adjacent key-value pair into the preceding one.

    # Shape 2: split `key1: "v1" key2: ...` into two lines. The trailing
    # portion must itself start with a `word:` to count as a key-value pair.
    split_kv = re.compile(
        r'^([ \t]*)([\w-]+:[ \t]+)"([^"\n]*)"[ \t]+([\w-]+:[ \t]+\S.*)$',
        re.MULTILINE,
    )

    def _split(m: re.Match) -> str:
        indent, prefix, value, trailing = m.groups()
        return f'{indent}{prefix}"{value}"\n{indent}{trailing}'

    prev = None
    out = fm_text
    while prev != out:
        prev = out
        out = split_kv.sub(_split, out)

    # Shape 1: merge bare-trailing-text into the quoted value. Restricted to
    # trailing text that is NOT itself a `key:` form (already handled above)
    # via the negative lookahead, and to text that is on the same line
    # via [ \t]+ rather than \s+.
    trailing_after_quote = re.compile(
        r'^([ \t]*[\w-]+:[ \t]+)"([^"\n]*)"[ \t]+(?!\S+:[ \t])([^\n]+)$',
        re.MULTILINE,
    )

    def _merge(m: re.Match) -> str:
        prefix, quoted, trailing = m.groups()
        merged = (quoted + " " + trailing.strip()).replace('"', "'")
        return f'{prefix}"{merged}"'

    return trailing_after_quote.sub(_merge, out)


def _collect_source_dates(claims: list[dict]) -> tuple[set[str], set[str]]:
    """Return (years, iso_dates) drawn from every claim's content, excerpt,
    date fields, plus the source record's date. Used by the date-fidelity
    guardrail below to detect hallucinated dates in the assembled body.
    """
    years: set[str] = set()
    iso: set[str] = set()
    fields = ("content", "original_excerpt", "date", "date_end", "record_date")
    yr = re.compile(r"\b(19\d{2}|20\d{2})\b")
    iso_re = re.compile(r"\b(\d{4}-\d{2}(?:-\d{2})?)\b")
    for c in claims:
        for f in fields:
            v = c.get(f)
            if not v:
                continue
            for m in yr.findall(str(v)):
                years.add(m)
            for m in iso_re.findall(str(v)):
                iso.add(m)
    return years, iso


def _check_date_fidelity(
    body: str, claims: list[dict], related: list[dict] | None = None
) -> list[str]:
    """Return a list of date-fidelity violations. Empty list means OK.

    Any 4-digit year (1900-2099) or ISO date (YYYY-MM-DD / YYYY-MM) in the
    body must appear in at least one source claim's content / excerpt / date,
    OR in one of the related-node names that the assembler may have linked
    to. Otherwise it is treated as a likely hallucination.
    """
    src_years, src_iso = _collect_source_dates(claims)

    # Also allow years AND full ISO dates that appear in related-node names
    # (e.g. linked event titles like "AATIP Denial, 2019-06-14") since the
    # assembler explicitly invites those as link targets - the model lifting a
    # date out of a name it was told to link is sourced, not invented.
    if related:
        for r in related:
            name = r.get("name", "")
            for m in re.findall(r"\b(19\d{2}|20\d{2})\b", name):
                src_years.add(m)
            for m in re.findall(r"\b(\d{4}-\d{2}(?:-\d{2})?)\b", name):
                src_iso.add(m)

    # Strip out markdown link targets (the URL) before scanning so slug-form
    # years inside (/events/1947-roswell-uap-crash) don't count as body
    # content - the year inside the URL came from the node name, not from
    # what the model wrote.
    text_only = re.sub(r"\]\([^)]+\)", "]()", body)

    bad: list[str] = []
    body_years = set(re.findall(r"\b(19\d{2}|20\d{2})\b", text_only))
    for y in body_years - src_years:
        bad.append(f"year {y!r} not in source claims")

    body_iso = set(re.findall(r"\b(\d{4}-\d{2}(?:-\d{2})?)\b", text_only))
    for d in body_iso:
        if d in src_iso:
            continue
        # A body date that is a date-component PREFIX of a source date is
        # sourced: the model dropped detail (wrote 2017-12 where the source has
        # 2017-12-07) rather than inventing it. The reverse is still a failure -
        # a body YYYY-MM-DD whose YYYY-MM is in source but whose day is not means
        # the model invented the day - because a shorter source never startswith
        # a longer body date.
        if any(s == d or s.startswith(d + "-") for s in src_iso):
            continue
        bad.append(f"date {d!r} not in source claims")
    return bad


def _plain_text(s: str) -> str:
    """Strip markdown emphasis / code markers so a plain-text field doesn't render
    literal *, _, or backticks. description is plain text and also feeds the SEO
    <meta description>, where markdown is just wrong (the model occasionally
    italicises a publication title, e.g. *60 Minutes*)."""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\*(.+?)\*", r"\1", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"_(.+?)_", r"\1", s)
    s = re.sub(r"`(.+?)`", r"\1", s)
    return s


def validate_article(text: str) -> tuple[dict, str]:
    """Parse the model's output: YAML frontmatter + markdown body.

    Tolerates a model misbehaviour where the model puts the references list
    in a SECOND yaml block at the end of the body rather than in the
    frontmatter. When detected, merge the trailing yaml block back into the
    frontmatter and strip it from the body.

    Also runs _sanitise_frontmatter_yaml to repair common quirks like
    `key: "..." trailing-text`.

    Raises ValueError if the structure is wrong.
    """
    text = text.strip()
    # Strip markdown fences if the model wrapped its response
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    if not text.startswith("---"):
        raise ValueError("article does not start with '---' frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("article frontmatter not closed by second '---'")
    fm_text = _sanitise_frontmatter_yaml(parts[1])
    fm = yaml.safe_load(fm_text)
    body = parts[2].strip()
    if not isinstance(fm, dict):
        raise ValueError("frontmatter is not a mapping")
    if not fm.get("title"):
        raise ValueError("frontmatter missing 'title'")
    if not fm.get("description"):
        raise ValueError("frontmatter missing 'description'")
    # Catch the sanitiser-ate-a-key failure mode: if 'description' (or any
    # value the model is told to emit) ended up concatenated INTO the title,
    # the title will contain the literal substring 'description:'. Same for
    # any other expected key.
    title_str = str(fm.get("title", ""))
    for swallowed in ("description:", "metadata:", "references:"):
        if swallowed in title_str:
            raise ValueError(
                f"title field contains {swallowed!r} - sanitiser likely "
                f"merged a subsequent key into the title. Raw title: {title_str!r}"
            )
    if not body:
        raise ValueError("article body is empty")

    # Recovery 1 - trailing yaml block with a `---` separator: model wrote a
    # second `---` separator near the end of the body and then YAML.
    trailing = re.search(r"\n---\s*\n([\s\S]+)$", body)
    if trailing:
        try:
            tail_text = trailing.group(1)
            tail_text = re.sub(r"\n---\s*$", "", tail_text).strip()
            extra = yaml.safe_load(tail_text)
            if isinstance(extra, dict):
                fm = {**fm, **extra}
                body = body[: trailing.start()].rstrip()
        except yaml.YAMLError:
            pass

    # Recovery 2 - bare `references:` line in body, no `---` separator. The
    # model dumped the yaml references list directly into the body text. Bill
    # Whitaker's page shipped like this once, undetected, because the article
    # was "structurally fine" - frontmatter parsed, body non-empty - but the
    # references were rendered as raw prose with no claim tooltips.
    bare_refs = re.search(r"\n(references:\s*\n(?:  -[\s\S]+?)+)$", body)
    if bare_refs:
        try:
            extra = yaml.safe_load(bare_refs.group(1))
            if isinstance(extra, dict) and extra.get("references"):
                fm = {**fm, **extra}
                body = body[: bare_refs.start()].rstrip()
        except yaml.YAMLError:
            pass

    # Hard check: if the body cites with <sup>N</sup> tags, the frontmatter
    # MUST have a non-empty references list. A page with citations and no
    # references is broken even if every other validation passes.
    if re.search(r"<sup>\s*\d+", body):
        refs = fm.get("references") or []
        if not refs:
            raise ValueError(
                "article body contains <sup> citations but frontmatter has no "
                "'references' list - model probably emitted references in body "
                "without a clear separator, recovery missed them"
            )

    # description is plain text (it also becomes the SEO meta description) -
    # strip any markdown emphasis the model added.
    if isinstance(fm.get("description"), str):
        fm["description"] = _plain_text(fm["description"])

    return fm, body


# People node names are stored as "Last, First Middle" in the graph (decisions
# 0023 + 0026) so they sort by surname. For display on the public site that
# form reads wrong; the H1, link text, and any natural-language reference uses
# "First Last" instead. Deterministic post-pass below converts both the title
# field and any markdown link whose display text is a Last-comma-First name.
_LAST_FIRST_RE = re.compile(r"^([A-ZÀ-Ý][\wÀ-ÿ' -]+),\s+([\wÀ-ÿ' .-]+)$")


def _display_name(canonical: str) -> str:
    """Convert 'Last, First Middle' -> 'First Middle Last'. Leaves any name
    that doesn't match the comma-form (single-word, non-Latin, etc.) alone."""
    m = _LAST_FIRST_RE.match(canonical.strip())
    if not m:
        return canonical
    last, first = m.group(1), m.group(2)
    return f"{first} {last}".strip()


def _rewrite_link_display(body: str) -> str:
    """Rewrite markdown links whose display text is a Last-comma-First person
    name. URL stays untouched (the slug is derived from the natural-order
    form already)."""

    def _sub(m: re.Match) -> str:
        display, url = m.group(1), m.group(2)
        new_display = _display_name(display)
        return f"[{new_display}]({url})"

    return re.sub(r"\[([^\]\n]+?)\]\((/[^)\n]+)\)", _sub, body)


def _public_hash(content_hash: str | None) -> str | None:
    """Strip the 'sha256:' prefix and take the first PUBLIC_HASH_LENGTH chars.
    Matches the workbench's URL convention."""
    if not content_hash:
        return None
    h = (
        content_hash.split(":", 1)[1]
        if content_hash.startswith("sha256:")
        else content_hash
    )
    return h[:PUBLIC_HASH_LENGTH] if len(h) >= PUBLIC_HASH_LENGTH else h


def _augment_references(
    frontmatter: dict, claims: list[dict], content_root: Path | None = None
) -> dict:
    """For each reference the model produced, find the originating claim and
    augment the reference with deterministic provenance fields:

    - quote: the verbatim source text the claim was drawn from
    - claim_id: the claim's UUID, for workbench scroll-to-claim
    - record_hash: the source record's public_hash (first 56 of sha256)
    - workbench_url: review-only deep-link into the workbench (site gates it out
      of public builds)
    - inspection_url: public, language-agnostic deep-link to the source record's
      public inspection page, /records/<slug>#claim-<uuid>. Emitted only when
      that record page exists in content_root (so the site renders it iff
      present); requires content_root to existence-check.

    Match strategy:
    1. If the reference carries claim_index (model followed the prompt), use
       that directly.
    2. Otherwise fall back to matching by source title + location_in_record.
       This loses precision when one record has many claims at the same
       location, but it's better than silently skipping the augmentation.
    """
    refs = frontmatter.get("references")
    if not isinstance(refs, list):
        return frontmatter

    # Pre-build the (source, location) -> claim lookup for the fallback.
    by_source_loc: dict[tuple[str, str], dict] = {}
    for c in claims:
        key = (c.get("record_title") or "", c.get("location") or "")
        by_source_loc.setdefault(key, c)  # first claim at this key wins

    augmented: list[dict] = []
    for r in refs:
        if not isinstance(r, dict):
            augmented.append(r)
            continue
        ci = r.get("claim_index")
        out = {k: v for k, v in r.items() if k != "claim_index"}

        c: dict | None = None
        if isinstance(ci, int) and 1 <= ci <= len(claims):
            c = claims[ci - 1]
        else:
            # Fallback: source + location lookup.
            c = by_source_loc.get((r.get("source") or "", r.get("location") or ""))

        if c:
            if c.get("original_excerpt"):
                out.setdefault("quote", c["original_excerpt"])
            cid = c.get("id")
            ph = _public_hash(c.get("record_content_hash"))
            if cid:
                out["claim_id"] = cid
            if ph:
                out["record_hash"] = ph
                out["workbench_url"] = f"{WORKBENCH_ORIGIN}/{ph}#claim-{cid}"
            rec_slug = c.get("record_friendly_name")
            if (
                cid
                and rec_slug
                and content_root is not None
                and output_path(content_root, "records", rec_slug).is_file()
            ):
                out["inspection_url"] = f"/records/{rec_slug}#claim-{cid}"
        augmented.append(out)
    return {**frontmatter, "references": augmented}


def _insert_after(fm: dict, after_key: str, key: str, value) -> dict:
    """Return fm with `key: value` inserted directly after `after_key` (or
    appended if after_key is absent), preserving insertion order."""
    out: dict = {}
    placed = False
    for k, v in fm.items():
        out[k] = v
        if k == after_key:
            out[key] = value
            placed = True
    if not placed:
        out[key] = value
    return out


def render_article(
    frontmatter: dict,
    body: str,
    claims: list[dict] | None = None,
    content_root: Path | None = None,
    built_from: dict | None = None,
    ai_usage: list | None = None,
) -> str:
    # Title and any other surface-level name fields use display form.
    if isinstance(frontmatter.get("title"), str):
        frontmatter = {**frontmatter, "title": _display_name(frontmatter["title"])}
    # built_from audit field (brief-sourced entity articles): the brief's freeze
    # is authoritative, so drop any model-emitted built_from first, then slot it
    # after metadata - or after description when the model omits the optional
    # metadata block - so it always lands in the head, never after references.
    if built_from is not None:
        frontmatter = {k: v for k, v in frontmatter.items() if k != "built_from"}
        anchor = "metadata" if "metadata" in frontmatter else "description"
        frontmatter = _insert_after(frontmatter, anchor, "built_from", built_from)
    # Augment each reference with the deterministic provenance fields drawn
    # from the originating claim's DB row (id, content_hash, original
    # excerpt, workbench link, public inspection link). Only runs when the
    # claims list is provided.
    if claims is not None:
        frontmatter = _augment_references(frontmatter, claims, content_root)
    # Public AI-usage provenance (ADR 0037): the carried-forward upstream chain
    # plus this assemble entry. Top-level, last (machine provenance, not content).
    if ai_usage:
        frontmatter = {k: v for k, v in frontmatter.items() if k != "ai_usage"}
        frontmatter["ai_usage"] = ai_usage
    body = _rewrite_link_display(body)
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
        + "\n---\n\n"
        + body.strip()
        + "\n"
    )


def render_record_page(
    article_fm: dict,
    body: str,
    metadata: dict,
    record_hash: str | None,
    ai_usage: list | None = None,
) -> str:
    """The public /records/ inspection page: the model's article (title,
    description, references, body) + source metadata + a record_hash the site
    turns into a "view the facts breakdown in the workbench" link
    ({workbenchUrl}/{record_hash}). The facts/entities QA breakdown is NOT emitted
    - it is consolidated in the workbench, not the public site."""
    frontmatter = {
        "title": _display_name(article_fm.get("title", "")),
        "description": article_fm.get("description", ""),
        "noindex": True,
        "metadata": metadata,
    }
    if record_hash:
        frontmatter["record_hash"] = record_hash
    frontmatter["references"] = article_fm.get("references", [])
    if ai_usage:
        frontmatter["ai_usage"] = ai_usage
    body = _rewrite_link_display(body)
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
        + "\n---\n\n"
        + body.strip()
        + "\n"
    )


def _split_article(text: str) -> tuple[dict, str] | None:
    """Split a rendered article string into (frontmatter dict, body), or None if
    it is not the expected `---`-delimited shape. Deliberately lighter than
    validate_article: no required-key checks, so it tolerates older on-disk
    frontmatter when reading an existing file to preserve from."""
    text = text.strip()
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    return fm, parts[2].strip()


def _preserve_authored_fields(new_article: str, existing_path: Path) -> str:
    """Carry human-authored frontmatter keys (_PRESERVE_KEYS, e.g. reviewer
    `directives`) from the existing article into a freshly-rendered one, which
    otherwise fully overwrites the file and would wipe them. The model never
    emits these keys, so the existing file's value is authoritative.

    Re-dumps the new frontmatter with the same serialiser the render functions
    use; fresh assembler output round-trips identically, so only the preserved
    block is added - every model-owned field stays byte-for-byte unchanged."""
    if not existing_path.is_file():
        return new_article
    existing = _split_article(existing_path.read_text())
    if existing is None:
        return new_article
    existing_fm, _ = existing
    preserved = {k: existing_fm[k] for k in _PRESERVE_KEYS if k in existing_fm}
    if not preserved:
        return new_article
    parsed = _split_article(new_article)
    if parsed is None:  # our own output is always well-formed; defensive
        return new_article
    new_fm, new_body = parsed
    for key, value in preserved.items():
        # Drop any model-emitted value first so the human-authored one wins,
        # then slot the block near the top (after title) for readability.
        new_fm = {k: v for k, v in new_fm.items() if k != key}
        new_fm = _insert_after(new_fm, "title", key, value)
    return (
        "---\n"
        + yaml.safe_dump(new_fm, sort_keys=False, allow_unicode=True).strip()
        + "\n---\n\n"
        + new_body
        + "\n"
    )


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--node",
        help="Node name (e.g. 'Fravor, David') or uuid",
    )
    target.add_argument(
        "--record",
        help=(
            "Assemble a per-record narrative + facts/entities page from the "
            "per-record digest. Accepts a digest friendly_name (filename stem), "
            "a path to a digest .yaml, or a record id / title"
        ),
    )
    target.add_argument(
        "--brief",
        help=(
            "Assemble an entity article from a synthesiser brief (the brief is the "
            "sole source; freezes built_from). Accepts a page slug (filename stem) "
            "or a path to a brief .yaml. Supersedes --node DB-direct (ADR 0036)"
        ),
    )
    ap.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"Path to knowledge.db, for --node mode (default: {DEFAULT_DB})",
    )
    ap.add_argument(
        "--digests-root",
        default=DEFAULT_DIGESTS_ROOT,
        help=f"Path to digests repo, for --record mode (default: {DEFAULT_DIGESTS_ROOT})",
    )
    ap.add_argument(
        "--briefs-root",
        default=DEFAULT_BRIEFS_ROOT,
        help=f"Path to briefs dir, for --brief mode (default: {DEFAULT_BRIEFS_ROOT})",
    )
    ap.add_argument(
        "--content-root",
        default=DEFAULT_CONTENT_ROOT,
        help=f"Path to content repo (default: {DEFAULT_CONTENT_ROOT})",
    )
    ap.add_argument(
        "--section",
        default=None,
        help="Hugo content section (defaults to derived from node type)",
    )
    ap.add_argument(
        "--model", default=DEFAULT_MODEL, help="Claude model (default: sonnet)"
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompt and exit without calling Claude",
    )
    ap.add_argument(
        "--print-only",
        action="store_true",
        help="Call Claude and print the article to stdout, do not write file",
    )
    args = ap.parse_args()

    digest: dict | None = None
    brief: dict | None = None
    if args.brief:
        loaded = load_brief(Path(args.briefs_root), args.brief)
        if not loaded:
            print(f"brief not found: {args.brief!r}", file=sys.stderr)
            return 2
        brief, _slug = loaded
        if not brief.get("brief_hash") or any(
            not c.get("claim_hash") for c in brief.get("claims") or []
        ):
            print(
                f"brief {args.brief!r} missing brief_hash or a claim_hash - "
                "refusing to write a page with a broken built_from audit field",
                file=sys.stderr,
            )
            return 2
        node = brief_node(brief)
        claims = claims_from_brief(brief)
        related = related_from_brief(brief)
    elif args.record:
        loaded = load_digest(Path(args.digests_root), args.record)
        if not loaded:
            print(f"digest not found: {args.record!r}", file=sys.stderr)
            return 2
        digest, friendly_name = loaded
        node = record_node(digest, friendly_name)
        claims = claims_from_digest(digest)
        related = related_from_digest(digest)
    else:
        conn = sqlite3.connect(args.db)
        node = load_node(conn, args.node)
        if not node:
            print(f"node not found: {args.node!r}", file=sys.stderr)
            return 2
        claims = claims_for_node(conn, node["id"])
        related = related_nodes(conn, node["id"])

    print(
        f"node: {node.get('name')} ({node.get('type')}, "
        f"{(node.get('id') or '?')[:8]})\n"
        f"  {len(claims)} claims, {len(related)} related nodes",
        file=sys.stderr,
    )

    # Resolve the output path now (not just at write time) so collected
    # directives from the existing article + the _directives.yaml hierarchy can
    # be injected into the prompt, and so the same path is reused for the write.
    section = args.section or SECTION_BY_TYPE.get(node["type"], node["type"] + "s")
    slug = node_slug(node)
    out = output_path(Path(args.content_root), section, slug)
    directives = collect_directives(out, Path(args.content_root))
    if directives:
        print(f"  directives: {len(directives)} applied", file=sys.stderr)

    prompt = build_prompt(node, claims, related, directives)
    if args.dry_run:
        print(prompt)
        return 0

    print(f"  prompt: {len(prompt):,} chars", file=sys.stderr)

    # Generation is non-deterministic: an occasional pass trips validate_article
    # or the date-fidelity guard (a fabricated year/date - site master found this
    # when a Roswell body contained "2025-07-05" for a 1947 event). Retry a few
    # times before giving up, rather than hard-failing one flaky pass - silent at
    # corpus scale otherwise. Each retry is another generation (rate limits on the
    # subscription, metered tokens on the API), but only on failure.
    fm = body = response = None
    fail_code, fail_msg = 3, ""
    _reset_usage()  # scope token usage to this article (accumulates over retries)
    for attempt in range(1, _MAX_GEN_ATTEMPTS + 1):
        response = call_claude(prompt, model=args.model)
        try:
            fm, body = validate_article(response)
        except ValueError as exc:
            fail_code, fail_msg = 3, f"invalid article: {exc}"
            fm = body = None
            print(
                f"  attempt {attempt}/{_MAX_GEN_ATTEMPTS}: {fail_msg}", file=sys.stderr
            )
            continue
        date_problems = _check_date_fidelity(body, claims, related)
        if date_problems:
            fail_code = 4
            fail_msg = "date-fidelity: " + "; ".join(date_problems)
            fm = body = None
            print(
                f"  attempt {attempt}/{_MAX_GEN_ATTEMPTS}: {fail_msg}", file=sys.stderr
            )
            continue
        break  # passed both gates

    if fm is None:
        print(
            f"GENERATION FAILED after {_MAX_GEN_ATTEMPTS} attempts ({fail_msg})",
            file=sys.stderr,
        )
        print("--- last raw response ---", file=sys.stderr)
        print(response, file=sys.stderr)
        return fail_code

    # Public AI-usage provenance (ADR 0037): this assemble entry, appended to the
    # carried-forward upstream chain. Record mode carries the single source
    # digest's chain; brief/node gather every contributing record's digest chain.
    assemble_entry = usage_entry("assemble", args.model, _get_usage())

    if digest is not None:
        content_root = Path(args.content_root)
        # Give the record page's references the same per-claim provenance
        # (quote, claim_id, record_hash, workbench_url) as entity-article
        # references, for review-link parity. inspection_url is naturally
        # skipped here - digest claims carry no record_friendly_name, and the
        # references are already on this record's inspection page.
        fm = _augment_references(fm, claims, content_root)
        rec = digest.get("record") or {}
        article = render_record_page(
            fm,
            body,
            metadata=record_metadata(digest),
            record_hash=_public_hash(rec.get("content_hash")),
            ai_usage=accumulate(digest.get("ai_usage") or [], assemble_entry),
        )
    elif brief is not None:
        upstream = gather_upstream_ai_usage(claims, Path(args.digests_root))
        article = render_article(
            fm,
            body,
            claims=claims,
            content_root=Path(args.content_root),
            built_from=built_from_block(brief),
            ai_usage=accumulate(upstream, assemble_entry),
        )
    else:
        upstream = gather_upstream_ai_usage(claims, Path(args.digests_root))
        article = render_article(
            fm,
            body,
            claims=claims,
            content_root=Path(args.content_root),
            ai_usage=accumulate(upstream, assemble_entry),
        )

    if args.print_only:
        print(article)
        return 0

    # `out` was resolved above (for directive collection); reuse it.
    out.parent.mkdir(parents=True, exist_ok=True)
    article = _preserve_authored_fields(article, out)
    out.write_text(article)
    print(f"  wrote: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
