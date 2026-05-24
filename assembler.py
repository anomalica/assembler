#!/usr/bin/env python3
"""Anomalica assembler v0 - graph-to-page generator.

Queries the digester's SQLite knowledge graph for a single node, gathers all
claims that reference or are spoken by it, formats those claims into a prompt,
and asks Claude to produce a reference-style article in the Hugo content format
the site expects.

Run via the digester container so we get python + Claude CLI + yaml/sqlite:

  docker run --rm \\
    -v /home/mark/repos/anomalica/anomalica-assembler:/work \\
    -v /home/mark/.local/share/digester:/db:ro \\
    -v /home/mark/repos/anomalica/anomalica-content:/content \\
    -v /home/mark/repos/anomalica/anomalica-digests:/digests:ro \\
    -v /home/mark/.local/bin/claude:/usr/local/bin/claude:ro \\
    -v /home/mark/.claude:/home/nonroot/.claude \\
    -v /home/mark/.claude.json:/home/nonroot/.claude.json \\
    -v /tmp/digester-sandbox/empty-CLAUDE.md:/home/nonroot/.claude/CLAUDE.md:ro \\
    -v /tmp/digester-sandbox/empty-settings.json:/home/nonroot/.claude/settings.json:ro \\
    --user $(id -u):$(id -g) --network host -e HOME=/home/nonroot \\
    -w /work \\
    anomalica-digester:development \\
    python assembler.py --node "Fravor, David" --section people
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


DEFAULT_DB = "/db/knowledge.db"
DEFAULT_CONTENT_ROOT = "/content"
DEFAULT_MODEL = "sonnet"

# Workbench origin used to build the per-claim deep-link URL stamped into
# each reference. Override via env var when the workbench is deployed
# elsewhere. Public-hash convention: first 56 chars of the SHA-256
# content_hash (no "sha256:" prefix). Matches the URL the workbench uses
# for its existing record routes.
WORKBENCH_ORIGIN = os.environ.get(
    "ANOMALICA_WORKBENCH_ORIGIN", "http://localhost:5173"
).rstrip("/")
PUBLIC_HASH_LENGTH = 56

# Section the article goes into is mapped from the node's type. The site's
# Hugo layout expects content/english/{section}/{slug}.en.md.
SECTION_BY_TYPE = {
    "person": "people",
    "organisation": "organisations",
    "place": "places",
    "event": "events",
    "matter": "matters",  # ADR 0028 deprecates; kept for unmigrated matters
    "object": "objects",
    "document": "documents",
    "concept": "concepts",
    # ADR 0028 additions:
    "programme": "programmes",
    "investigation": "investigations",
    "pattern": "patterns",
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


# ----------------------------------------------------------------------------
# Slugs and paths
# ----------------------------------------------------------------------------


def slugify(name: str) -> str:
    """Produce a kebab-case slug from a node name. Person names in 'Last,
    First' form are reordered to 'first-last'; everything else is just
    lowercased and hyphenated."""
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2:
            name = f"{parts[1]} {parts[0]}"
    s = re.sub(r"[^\w\s-]", "", name.lower())
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "untitled"


def node_slug(node: dict) -> str:
    """Return the URL slug for a node, honouring metadata.explicit_slug if set
    (ADR 0028 patterns use this to get short URLs like /patterns/shifting-
    official-accounts/ instead of slugifying the long display name). Falls
    back to slugify(node["name"]) when no override is present.
    """
    md = node.get("metadata") or {}
    explicit = md.get("explicit_slug")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return slugify(node["name"])


def output_path(content_root: Path, section: str, slug: str, lang: str = "en") -> Path:
    # Hugo mounts anomalica-content/pages/ to content/. So generated articles
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

Your ENTIRE response must be a single YAML+markdown document in this exact shape:

---
title: "<the node's display name>"
description: "<one-sentence summary, what this {node_type} is>"
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

Within the body, link to related entities using markdown links of the form [Display Name](/<section>/<slug>) when the related entity appears in the RELATED NODES list above. For example: [2004 USS Nimitz encounter](/events/2004-uss-nimitz-encounter), [Strike Fighter Squadron 41 (VFA-41)](/organisations/strike-fighter-squadron-41-vfa-41). Only link nodes that appear in the related list; do not invent slugs.

For each <sup>N</sup> citation, ensure references[N-1] in the frontmatter is the matching source. Reference numbering must be sequential starting at 1. Each reference MUST carry a `claim_index` field with the 1-based index of the originating claim in the KNOWLEDGE GRAPH CLAIMS list below - this index lets downstream tooling link the reference back to the exact source claim in the workbench.

CITATION FIDELITY RULE (this is the most important rule and the easiest to break):

Every <sup>N</sup> must cite a CLAIM in the data below whose content DIRECTLY supports the assertion you just wrote - not one that supports it via inference, temporal logic, or implied negation.

Example of the failure mode to avoid:
- You write: "The Pentagon had never previously confirmed the programme's existence."<sup>3</sup>
- Reference 3 says: "AATIP began as part of the Defense Intelligence Agency and was acknowledged by Pentagon officials in December 2017."
- That citation does NOT directly state "never previously confirmed". It states acknowledgement in December 2017, and you inferred the negation. This is a bad citation. Either find a claim that DIRECTLY states "no prior acknowledgement" / "first confirmation", or soften your assertion ("The 2017 New York Times reporting marked the Pentagon's first public acknowledgement of the programme."<sup>3</sup>) so what you wrote matches what the claim says.

The test: read the cited claim's text. Could it be paraphrased to the sentence you wrote without adding new logic or temporal inference? If not, your citation is wrong - either fix the wording, drop the assertion, or pick a different claim that does support it directly.

If you cannot find a claim that directly supports an assertion you want to make, do not make the assertion. The article must be assembled from what the graph says, not from what the graph implies.

KNOWLEDGE GRAPH CLAIMS (all available evidence for this {node_type}):

{claims_block}

OUTPUT - the article as described above, starting with --- and ending with the closing body paragraph. No commentary, no JSON wrapper.
"""


def format_related_block(related: list[dict]) -> str:
    if not related:
        return ""
    lines = ["", "RELATED NODES YOU MAY LINK TO (only these slugs are valid):"]
    for r in related:
        section = SECTION_BY_TYPE.get(r["type"], r["type"] + "s")
        slug = node_slug(r)
        lines.append(
            f"  - [{r['name']}](/{section}/{slug}) - {r['type']}, "
            f"co-appears in {r['shared_claims']} claims"
        )
    return "\n".join(lines)


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


def build_prompt(node: dict, claims: list[dict], related: list[dict]) -> str:
    claims_block = "\n\n".join(format_claim(c, i + 1) for i, c in enumerate(claims))
    return ASSEMBLY_PROMPT.format(
        node_name=node["name"],
        node_type=node["type"],
        related_block=format_related_block(related),
        claims_block=claims_block,
    )


# ----------------------------------------------------------------------------
# Claude CLI - mirrors the pattern in digester/extract.py
# ----------------------------------------------------------------------------


def call_claude_cli(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Send the prompt to the local Claude CLI; return the text response.

    Pipes the prompt via stdin rather than @file so the CLI doesn't try to
    "read" the prompt as a tool-Read input (which prompts a clarifying
    question instead of generating when the file is large).
    """
    cmd = [
        "claude",
        "--model",
        model,
        "--effort",
        "low",
        "--output-format",
        "json",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--dangerously-skip-permissions",
        "--print",
    ]
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=900
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {proc.stderr[:500]}")
    try:
        data = json.loads(proc.stdout)
        if "result" in data:
            return data["result"]
        if "content" in data:
            return data["content"]
        return proc.stdout
    except json.JSONDecodeError:
        return proc.stdout


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

    # Also allow years that appear in related-node names (e.g. linked event
    # titles) since the assembler explicitly invites those as link targets.
    if related:
        for r in related:
            for m in re.findall(r"\b(19\d{2}|20\d{2})\b", r.get("name", "")):
                src_years.add(m)

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
        # A YYYY-MM-DD whose year is sourced AND whose YYYY-MM prefix is
        # sourced is borderline-acceptable (the model may have promoted a
        # known year-month to a specific day). Treat that as failure too -
        # if the day isn't in source, the model invented it.
        if d not in src_iso:
            bad.append(f"date {d!r} not in source claims")
    return bad


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


def _augment_references(frontmatter: dict, claims: list[dict]) -> dict:
    """For each reference the model produced, find the originating claim and
    augment the reference with deterministic provenance fields:

    - quote: the verbatim source text the claim was drawn from
    - claim_id: the claim's UUID, for workbench scroll-to-claim
    - record_hash: the source record's public_hash (first 56 of sha256)
    - workbench_url: the full clickable link into the workbench

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
        augmented.append(out)
    return {**frontmatter, "references": augmented}


def render_article(
    frontmatter: dict, body: str, claims: list[dict] | None = None
) -> str:
    # Title and any other surface-level name fields use display form.
    if isinstance(frontmatter.get("title"), str):
        frontmatter = {**frontmatter, "title": _display_name(frontmatter["title"])}
    # Augment each reference with the deterministic provenance fields drawn
    # from the originating claim's DB row (id, content_hash, original
    # excerpt, workbench link). Only runs when the claims list is provided.
    if claims is not None:
        frontmatter = _augment_references(frontmatter, claims)
    body = _rewrite_link_display(body)
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
        + "\n---\n\n"
        + body.strip()
        + "\n"
    )


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument(
        "--node",
        required=True,
        help="Node name (e.g. 'Fravor, David') or uuid",
    )
    ap.add_argument(
        "--db",
        default=DEFAULT_DB,
        help=f"Path to knowledge.db (default: {DEFAULT_DB})",
    )
    ap.add_argument(
        "--content-root",
        default=DEFAULT_CONTENT_ROOT,
        help=f"Path to anomalica-content repo (default: {DEFAULT_CONTENT_ROOT})",
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

    conn = sqlite3.connect(args.db)
    node = load_node(conn, args.node)
    if not node:
        print(f"node not found: {args.node!r}", file=sys.stderr)
        return 2

    claims = claims_for_node(conn, node["id"])
    related = related_nodes(conn, node["id"])
    print(
        f"node: {node['name']} ({node['type']}, {node['id'][:8]})\n"
        f"  {len(claims)} claims, {len(related)} related nodes",
        file=sys.stderr,
    )

    prompt = build_prompt(node, claims, related)
    if args.dry_run:
        print(prompt)
        return 0

    print(f"  prompt: {len(prompt):,} chars", file=sys.stderr)
    response = call_claude_cli(prompt, model=args.model)

    try:
        fm, body = validate_article(response)
    except ValueError as exc:
        print(f"INVALID ARTICLE: {exc}", file=sys.stderr)
        print("--- raw response ---", file=sys.stderr)
        print(response, file=sys.stderr)
        return 3

    # Date-fidelity guardrail: any year or ISO date in the assembled body
    # must trace back to a source claim. Fails loud if the model fabricated
    # a year/date. Site master found this when the Roswell body contained
    # "2025-07-05" for a 1947 event.
    date_problems = _check_date_fidelity(body, claims, related)
    if date_problems:
        print("DATE FIDELITY VIOLATION - article will not be written:", file=sys.stderr)
        for p in date_problems:
            print(f"  {p}", file=sys.stderr)
        print("--- body (head) ---", file=sys.stderr)
        print(body[:1200], file=sys.stderr)
        return 4

    article = render_article(fm, body, claims=claims)

    if args.print_only:
        print(article)
        return 0

    section = args.section or SECTION_BY_TYPE.get(node["type"], node["type"] + "s")
    slug = node_slug(node)
    out = output_path(Path(args.content_root), section, slug)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(article)
    print(f"  wrote: {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
