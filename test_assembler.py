"""Assembler output gates.

The assembler hands a model a slug list and the model writes the article, so
every guarantee about the output is a check applied after generation. These are
the checks that a human reader cannot perform: prose that reads correctly while
the href underneath points somewhere else.
"""

import assembler as a

RELATED = [
    {"name": "Luis Elizondo", "type": "person"},
    {"name": "David Fravor", "type": "person"},
    {"name": "George Knapp", "type": "person"},
]


def test_a_link_naming_one_person_and_pointing_at_another_is_caught():
    """This reached the live site: "Garry Reid" linked to /people/luis-elizondo on
    the OUSDI page. Four more sat in content/, invisible only because their target
    page was not assembled yet."""
    problems = a._check_link_targets(
        "[Garry Reid](/people/luis-elizondo) served as Director.", RELATED
    )
    assert len(problems) == 1
    assert "Garry Reid" in problems[0] and "luis-elizondo" in problems[0]

    assert a._check_link_targets("[Lex Fridman](/people/george-knapp) hosted.", RELATED)


def test_surname_shorthand_is_legitimate():
    """The majority of real links are surname-only and must not be flagged. The
    check exists to catch a link naming somebody else entirely, not to police
    phrasing."""
    assert not a._check_link_targets(
        "[Fravor](/people/david-fravor) reported it.", RELATED
    )
    assert not a._check_link_targets(
        "[Elizondo](/people/luis-elizondo) ran it.", RELATED
    )
    assert not a._check_link_targets(
        "[Luis Elizondo](/people/luis-elizondo) ran it.", RELATED
    )


def test_a_target_outside_the_linkable_list_is_not_judged():
    """Only links the assembler offered can be checked here; anything else is the
    existing link-resolution pass's business."""
    assert not a._check_link_targets(
        "[somewhere](/places/nowhere) is off-list.", RELATED
    )
    assert not a._check_link_targets("text with no links at all", RELATED)
    assert not a._check_link_targets("[Anyone](/people/anyone)", None)


def test_unparseable_frontmatter_is_a_failed_attempt_not_a_crash():
    """Every other malformation in validate_article raises ValueError, which the
    retry loop catches and regenerates from. A YAML parse error did not, so it
    escaped as a traceback and killed the run on exit 1 with nothing written.

    Uses a malformation the sanitiser cannot repair - an unclosed flow sequence -
    because the shape that originally exposed this (a source title opening with a
    quote) is now repaired before the parse and no longer reaches it.
    """
    import pytest

    bad = "---\ntitle: T\ndescription: D\nmetadata: [unclosed, flow\n---\n\nbody text\n"
    with pytest.raises(ValueError, match="not valid YAML"):
        a.validate_article(bad)


def test_a_source_title_that_starts_with_a_quote_is_repaired():
    """A title whose own text opens with a double quote produced

        source: ""Skinny Bob is Real" - Lifelong Abductee...

    which YAML reads as an empty scalar followed by junk. Re-quoted with single
    quotes, the one form that carries embedded double quotes without escaping,
    the value survives intact rather than costing a regeneration.
    """
    import yaml

    bad = (
        "title: T\n"
        "description: D\n"
        "references:\n"
        '  - text: "x"\n'
        '    source: ""Skinny Bob is Real" - Lifelong Abductee\n'
    )
    fixed = a._sanitise_frontmatter_yaml(bad)
    parsed = yaml.safe_load(fixed)
    assert (
        parsed["references"][0]["source"] == '"Skinny Bob is Real" - Lifelong Abductee'
    )


def test_ordinary_frontmatter_is_left_alone():
    """The repair must not touch values that were already valid."""
    ok = (
        "title: T\n"
        'description: "plain value"\n'
        "references:\n"
        '  - text: "y"\n'
        '    source: "Normal Title"\n'
    )
    assert a._sanitise_frontmatter_yaml(ok) == ok


ACRONYM_RELATED = [
    {"name": "Unidentified Flying Object (UFO)", "type": "topic"},
    {
        "name": "Office of the Under Secretary of Defense for Intelligence (OUSDI)",
        "type": "organisation",
    },
    {"name": "United States Department of Defense (DoD)", "type": "organisation"},
]


def test_an_acronym_in_the_prose_matches_its_parenthesised_name():
    """The corpus names an entity "... for Intelligence (OUSDI)" and the prose
    calls it "OUSDI", so the acronym must be a comparable word on both sides.

    Getting this wrong was expensive: the AATIP and Tom DeLonge pages each failed
    all three attempts on CORRECT links to UFO, OUSDI, AAWSAP, NASA and CE5, and
    neither page was written.
    """
    assert not a._check_link_targets(
        "[OUSDI](/organisations/office-of-the-under-secretary-of-defense-for-intelligence-ousdi)",
        ACRONYM_RELATED,
    )
    # Plural of an acronym, as the prose naturally writes it.
    assert not a._check_link_targets(
        "[UFOs](/topics/unidentified-flying-object-ufo)", ACRONYM_RELATED
    )


def test_a_wrong_acronym_is_still_caught():
    """Loosening for acronyms must not blind the check: CIA is not the DoD."""
    problems = a._check_link_targets(
        "[CIA](/organisations/united-states-department-of-defense-dod)", ACRONYM_RELATED
    )
    assert len(problems) == 1 and "CIA" in problems[0]


def test_a_hyphenated_compound_matches_its_parts():
    """ "1952-1957" has to match prose that writes "1952 and 1957". Splitting only
    on whitespace left the hyphenated form matching nothing, which rejected a
    correct Project Blue Book link and cost that page its entire attempt budget.

    Third false-positive class found in this gate after acronyms and plurals, all
    from the same mistake: assuming the display text is spelled like the node name.
    """
    related = [
        {
            "name": "1952-1957 Unidentified Anomalous Phenomena / Unidentified Aerial Phenomena (UAP) sighting spike",
            "type": "event",
        }
    ]
    assert not a._check_link_targets(
        "[1952 and 1957](/events/1952-1957-unidentified-anomalous-phenomena-unidentified-aerial-phenomena-uap-sighting-spike)",
        related,
    )


def test_the_hyphen_split_does_not_blind_the_check():
    """Splitting compounds must not make everything match everything."""
    related = [{"name": "USS Nimitz (CVN-68)", "type": "object"}]
    assert a._check_link_targets(
        "[Ticonderoga-class](/objects/uss-nimitz-cvn-68)", related
    )


CLAIM = {
    "content": "The object accelerated away.",
    "record_title": "Some Recording",
    "attribution_mode": "unverified",
    "claim_type": "observation",
    "attestation": "firsthand",
}


def test_bracketed_speaker_is_a_description_not_a_name():
    """Square brackets are the notation for "we do not know who this is". Real
    names, and titles that merely start with the word, must not be caught."""
    for described in (
        "[interviewer 2]",
        "[audience member]",
        "Speaker 1",
        "speaker_3",
        "speaker-1",
        "unnamed 1976 Bolton abductee",
        "unnamed-1976-bolton-abductee",
    ):
        assert a.is_described_speaker(described), described
    for named in (
        "David Fravor",
        "david-fravor",
        "Speaker of the House",
        "speaker-of-the-house",
        "USS Nimitz",
    ):
        assert not a.is_described_speaker(named), named


def test_a_described_speaker_is_never_offered_as_a_link_target():
    """A page for "[audience member]" would be a page about nobody, assembled from
    unrelated people in unrelated recordings."""
    block = a.format_related_block(
        [
            {"name": "David Fravor", "type": "person", "shared_claims": 5},
            {"name": "[interviewer 2]", "type": "person", "shared_claims": 3},
            {"name": "Speaker 1", "type": "person", "shared_claims": 2},
        ]
    )
    assert "david-fravor" in block
    assert "interviewer" not in block
    assert "speaker-1" not in block


def test_described_speaker_gets_its_own_attribution_branch():
    """Reproduced verbatim, brackets included, and never spoken of as a named
    person - the brackets ARE the signal that this is a description."""
    out = a.format_claim({**CLAIM, "speaker": "[interviewer 2]"}, 1)
    assert "SPEAKER DESCRIBED NOT NAMED" in out
    assert "[interviewer 2]" in out
    assert "Do NOT link it" in out


def test_named_speaker_attribution_is_unchanged():
    out = a.format_claim({**CLAIM, "speaker": "David Fravor"}, 1)
    assert "SPEAKER DESCRIBED NOT NAMED" not in out
    assert "David Fravor said that" in out


def test_missing_speaker_still_falls_through_to_the_source_record():
    out = a.format_claim({**CLAIM, "speaker": ""}, 1)
    assert "the source record below" in out


def test_link_index_refuses_a_described_speaker_even_with_a_page_on_disk(tmp_path):
    """Belt and braces: a page may exist from before this rule, and must still not
    be linkable."""
    pages = tmp_path / "pages" / "people"
    pages.mkdir(parents=True)
    (pages / "speaker-1.en.md").write_text("---\ntitle: Speaker 1\n---\n\nbody\n")
    (pages / "david-fravor.en.md").write_text("---\ntitle: David Fravor\n---\n\nbody\n")
    idx = a.build_link_index(None, tmp_path)
    assert "/people/david-fravor" in idx["exact"]
    assert "/people/speaker-1" not in idx["exact"]


def test_a_de_bracketed_slug_is_caught_by_the_page_title(tmp_path):
    """slugify strips brackets, so "[interviewer 2]" lands on disk as
    interviewer-2 - indistinguishable from a real slug. The title still carries
    them, so the gate tests that instead of the lossy derivative."""
    pages = tmp_path / "pages" / "people"
    pages.mkdir(parents=True)
    (pages / "interviewer-2.en.md").write_text(
        '---\ntitle: "[interviewer 2]"\n---\n\nbody\n'
    )
    (pages / "david-fravor.en.md").write_text("---\ntitle: David Fravor\n---\n\nbody\n")
    idx = a.build_link_index(None, tmp_path)
    assert "/people/david-fravor" in idx["exact"]
    assert "/people/interviewer-2" not in idx["exact"]


def test_real_pages_still_index_by_their_title(tmp_path):
    pages = tmp_path / "pages" / "organisations"
    pages.mkdir(parents=True)
    (pages / "united-states-navy-usn.en.md").write_text(
        '---\ntitle: "United States Navy (USN)"\ndescription: x\n---\n\nbody\n'
    )
    idx = a.build_link_index(None, tmp_path)
    # The acronym stem still resolves, so prose dropping "(USN)" still links.
    assert (
        idx["stems"].get("united-states-navy")
        == "/organisations/united-states-navy-usn"
    )


def test_a_person_carrying_an_acronym_is_a_corrupted_merge():
    """A person does not have an acronym in their name; an organisation does. This
    exact node is page-worthy today at 36 claims and 8 independent sources, and is
    two people fused with a surname eaten by acronym expansion."""
    assert a.suspect_entity_name("Unidentified Aerial Phenomena (UAP) Gerb", "person")
    assert a.suspect_entity_name("[interviewer 2]", "person")
    assert a.suspect_entity_name("", "person") == "no name"


def test_an_organisation_carrying_an_acronym_is_normal():
    """The naming convention writes them this way - 90 current proposals do."""
    for org in (
        "United States Air Force (USAF)",
        "National Aeronautics and Space Administration (NASA)",
        "Advanced Aerospace Threat Identification Program (AATIP)",
    ):
        assert a.suspect_entity_name(org, "organisation") is None
    assert a.suspect_entity_name("Unidentified Flying Object (UFO)", "topic") is None


def test_a_real_person_passes():
    for who in ("David Fravor", "Luis Elizondo", "Jacques Vallée", "J. Allen Hynek"):
        assert a.suspect_entity_name(who, "person") is None


def test_article_tokens_reads_the_stamp_back(tmp_path):
    """The only surviving record of a run: the ledger ADR 0037 specifies has no
    table in either database, so two unexplained runs were only reconstructable
    from these stamps."""
    good = tmp_path / "a.en.md"
    good.write_text(
        "---\ntitle: X\nbuilt_by:\n  model: claude-sonnet-5\n"
        "  tokens:\n    input: 80946\n    output: 25728\n---\n\nbody\n"
    )
    assert a.article_tokens(good) == {"input": 80946, "output": 25728}

    bare = tmp_path / "b.en.md"
    bare.write_text("---\ntitle: X\n---\n\nbody\n")
    assert a.article_tokens(bare) is None

    assert a.article_tokens(tmp_path / "missing.en.md") is None


def test_article_tokens_survives_broken_front_matter(tmp_path):
    """A malformed article must not crash a spend report - the report is what you
    reach for when something has already gone wrong."""
    bad = tmp_path / "c.en.md"
    bad.write_text("---\ntitle: [unclosed\n---\n\nbody\n")
    assert a.article_tokens(bad) is None
    assert a.article_tokens(tmp_path / "d.en.md") is None


def test_citations_are_renumbered_by_first_appearance():
    """Readers met "8, 9, 17, 15, 16" because the model numbered by the reference
    array's position, not by where the citation lands in the prose. 32 of 70
    published pages were out of order."""
    body = "Alpha.<sup>3</sup> Beta.<sup>1</sup> Gamma.<sup>3, 2</sup>"
    refs = [{"text": "one"}, {"text": "two"}, {"text": "three"}, {"text": "spare"}]
    nb, nr = a.renumber_citations(body, refs)
    assert nb == "Alpha.<sup>1</sup> Beta.<sup>2</sup> Gamma.<sup>1, 3</sup>"
    # every citation still points at the reference it did before
    assert (
        nr[0]["text"] == "three" and nr[1]["text"] == "one" and nr[2]["text"] == "two"
    )
    # an uncited reference is kept, not dropped
    assert nr[3]["text"] == "spare" and len(nr) == len(refs)


def test_already_ordered_citations_are_left_alone():
    body = "A.<sup>1</sup> B.<sup>2</sup>"
    refs = [{"text": "x"}, {"text": "y"}]
    assert a.renumber_citations(body, refs) == (body, refs)


def test_a_citation_pointing_at_nothing_is_rejected():
    """<sup>199</sup> against 60 references is a sentence claiming a source it
    does not have. Ten live pages carried this; the only check was that SOME
    references existed."""
    bad = (
        "---\ntitle: T\ndescription: d\nreferences:\n- text: a\n- text: b\n---\n\n"
        "X.<sup>1</sup> Y.<sup>7</sup>\n"
    )
    try:
        a.validate_article(bad)
    except ValueError as exc:
        assert "pointing at nothing" in str(exc)
    else:
        raise AssertionError("dangling citation was accepted")


def test_in_range_citations_still_validate():
    ok = (
        "---\ntitle: T\ndescription: d\nreferences:\n- text: a\n- text: b\n---\n\n"
        "X.<sup>1</sup> Y.<sup>2</sup>\n"
    )
    fm, _ = a.validate_article(ok)
    assert len(fm["references"]) == 2


def test_aliases_are_regenerated_not_preserved(tmp_path):
    """Preserving is what failed: a hand-written alias survives only until its
    page is next rebuilt, and two pages 404'd that way."""
    art = (
        "---\ntitle: X\naliases:\n- /people/stale/\ndescription: d\n"
        "references:\n- text: a\n---\n\nBody.<sup>1</sup>\n"
    )
    out = a.stamp_aliases(art, ["/people/new/", "/en/people/new/"])
    assert "/people/stale/" not in out
    assert "/people/new/" in out and "/en/people/new/" in out


def test_no_aliases_leaves_the_article_untouched():
    art = (
        "---\ntitle: X\ndescription: d\nreferences:\n- text: a\n---\n\nB.<sup>1</sup>\n"
    )
    assert a.stamp_aliases(art, []) == art


def test_an_alias_never_shadows_a_live_page(tmp_path):
    """Redirecting onto a published page is worse than the dead link it fixes."""
    pages = tmp_path / "pages" / "people"
    pages.mkdir(parents=True)
    (pages / "taken.en.md").write_text("---\ntitle: Taken\n---\n\nb\n")
    node = {
        "id": None,
        "type": "person",
        "name": "Someone",
        "aliases": ["Taken", "Free"],
    }
    out = a.slug_aliases(node, "people", None, tmp_path)
    assert "/people/taken/" not in out
    assert "/people/free/" in out and "/en/people/free/" in out


def test_an_alias_equal_to_the_current_slug_is_dropped():
    node = {"id": None, "type": "person", "name": "Jane Doe", "aliases": ["Jane Doe"]}
    assert a.slug_aliases(node, "people", None, None) == []


def test_related_slugs_follow_a_rename(tmp_path):
    """A brief freezes slugs at synthesise time; the graph moved three times in one
    evening. Matched on node id, so a rename is followed rather than guessed."""
    import sqlite3

    db = tmp_path / "g.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE nodes (id TEXT, name TEXT, node_type TEXT, retired_at TEXT)"
    )
    conn.execute("INSERT INTO nodes VALUES ('n1','alien abduction','topic',NULL)")
    conn.execute("INSERT INTO nodes VALUES ('n2','Gone','topic','2026-08-22')")
    conn.commit()
    conn.close()
    related = [
        {
            "id": "n1",
            "name": "alien abduction phenomenon",
            "type": "topic",
            "metadata": {"explicit_slug": "alien-abduction-phenomenon"},
        },
        {"id": "n2", "name": "Gone", "type": "topic", "metadata": None},
    ]
    out = a.refresh_related_slugs(related, str(db))
    assert len(out) == 1, "a node merged away has no page and must be dropped"
    assert out[0]["name"] == "alien abduction"
    assert out[0]["metadata"] is None, "the frozen explicit_slug must not survive"


def test_related_slugs_survive_an_unreadable_graph():
    """A graph we cannot read is not a reason to write nothing."""
    related = [{"id": "x", "name": "N", "type": "topic"}]
    assert a.refresh_related_slugs(related, "/nonexistent/g.db") == related
    assert a.refresh_related_slugs(related, None) == related


def test_aliases_cover_a_type_change_not_just_a_rename(tmp_path, monkeypatch):
    """A page's URL is (section, slug) and the two move independently: section
    comes from the node TYPE, so a type change relocates the page without
    touching its name. AATIP went organisation -> project and 404'd twice,
    because name history cannot see a type change."""
    import subprocess as sp

    calls = {}

    class R:
        stdout = "pages/organisations/x.en.md\npages/projects/x.en.md\n"

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return R()

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(a.subprocess, "run", fake_run)
    node = {"id": None, "type": "project", "name": "X", "aliases": []}
    out = a.slug_aliases(node, "projects", None, tmp_path)
    assert "/organisations/x/" in out and "/en/organisations/x/" in out
    assert "/projects/x/" not in out, "the current path is not an alias of itself"


def test_tags_are_closed_not_generated():
    """metadata.role is populated on 137 of 144 people and holds 131 DISTINCT
    values. Free text that is near-unique per entity groups nothing, so a tag
    outside the vocabulary is dropped rather than passed through."""
    kept = a.filter_tags(
        ["pilot", "FastEagle 02", "witness", "naval aviator"], "person"
    )
    assert kept == ["pilot", "witness"]


def test_tags_are_capped():
    kept = a.filter_tags(
        ["pilot", "witness", "author", "scientist", "engineer"], "person"
    )
    assert len(kept) == a.MAX_TAGS


def test_tags_come_out_in_vocabulary_order_not_model_order():
    """Stable ordering across pages, so a filter UI and a diff both behave."""
    assert a.filter_tags(["witness", "pilot"], "person") == ["pilot", "witness"]


def test_a_type_with_no_vocabulary_gets_no_tags():
    assert a.filter_tags(["anything"], "nonesuch") == []
    assert a.filter_tags("pilot", "person") == [], "a bare string is not a tag list"


def test_all_invented_tags_drop_the_field(tmp_path):
    art = (
        "---\ntitle: X\ntags:\n- made up\ndescription: d\n"
        "references:\n- text: x\n---\n\nB.<sup>1</sup>\n"
    )
    assert "tags" not in a.stamp_tags(art, "person")


def test_vocabularies_are_lowercase_and_unique():
    for node_type, vocab in a.TAG_VOCABULARY.items():
        assert len(set(vocab)) == len(vocab), f"{node_type} has a duplicate"
        assert all(v == v.lower() for v in vocab), f"{node_type} has a capital"


GOOD_ARTICLE = (
    "---\ntitle: David Fravor\ntags:\n- pilot\ndescription: d\n"
    "references:\n- text: a\n- text: b\n---\n\nX.<sup>1</sup> Y.<sup>2</sup>\n"
)


def test_score_article_passes_a_clean_article():
    r = a.score_article(GOOD_ARTICLE, claims=[], related=[], node_type="person")
    assert r["ok"] and r["structure_ok"]
    assert r["reference_count"] == 2


def test_score_article_reports_skipped_gates_rather_than_passing_them():
    """A missing input must never look like a clean score - a comparison harness
    would read it as the model doing well."""
    r = a.score_article(GOOD_ARTICLE, node_type="person")
    assert set(r["gates_skipped"]) == {"date_fidelity", "link_targets"}


def test_score_article_catches_a_dangling_citation():
    bad = (
        "---\ntitle: X\ndescription: d\nreferences:\n- text: a\n---\n\nY.<sup>9</sup>\n"
    )
    r = a.score_article(bad, claims=[], related=[], node_type="person")
    assert not r["ok"] and r["citation_findings"]


def test_score_article_catches_an_uncited_reference():
    art = (
        "---\ntitle: X\ndescription: d\nreferences:\n- text: a\n- text: b\n---\n\n"
        "Y.<sup>1</sup>\n"
    )
    r = a.score_article(art, claims=[], related=[], node_type="person")
    assert not r["ok"] and "never cited" in r["citation_findings"][0]


def test_score_article_catches_a_corrupted_name_and_a_bad_tag():
    art = (
        "---\ntitle: Unidentified Aerial Phenomena (UAP) Gerb\ntags:\n- pilot\n"
        "- made up\ndescription: d\nreferences:\n- text: a\n---\n\nY.<sup>1</sup>\n"
    )
    r = a.score_article(art, claims=[], related=[], node_type="person")
    assert r["name_findings"] and r["tag_findings"] and not r["ok"]


def test_score_article_never_rewrites_its_input():
    before = GOOD_ARTICLE
    a.score_article(GOOD_ARTICLE, claims=[], related=[], node_type="person")
    assert GOOD_ARTICLE == before, "scoring must measure, never repair"


def test_source_display_is_driven_by_copyright_and_fails_closed():
    assert a.source_display_mode("public_domain", "pdf") == "text"
    assert a.source_display_mode("public_domain", "video") == "embed"
    assert a.source_display_mode("publicly_accessible", "video") == "embed"
    # Publicly reachable is NOT redistributable - link, never reproduce.
    assert a.source_display_mode("publicly_accessible", "pdf") == "link"
    assert a.source_display_mode("restricted", "video") == "none"
    assert a.source_display_mode(None, "video") == "none", "unknown fails closed"


def test_a_record_summary_is_shorter_than_a_biography():
    """Assert the INTENT, not the wording - the entity instruction has already
    been rewritten once and a test pinned to its phrasing broke without the
    behaviour changing."""
    import re

    def target(node_type):
        text = a.format_length_block(node_type)
        return max(int(n.replace(",", "")) for n in re.findall(r"[\d,]{3,}", text))

    assert target("source") < target("person")
    assert target("source") <= 400, "a record page is a summary"


def test_the_entity_instruction_asks_for_citation_breadth():
    """A page can get longer without citing more, and the citation count is the
    one that matters - measured 13 -> 38 references when this was added."""
    text = a.format_length_block("person").lower()
    assert "distinct claims" in text
    assert "thrown away" in text or "broadly" in text


def test_openrouter_is_selected_by_model_id_not_a_flag(monkeypatch):
    """No env toggle on purpose: a provider-qualified model IS the request to
    spend, so the metered path cannot be reached by setting a flag and forgetting
    which model is configured."""
    from anomalica_common.llm import is_openrouter_model

    assert is_openrouter_model("openai/gpt-5.6-luna")
    assert not is_openrouter_model("sonnet")

    calls = {}
    monkeypatch.setattr(a, "_call_openrouter", lambda p, m: calls.setdefault("or", m))
    monkeypatch.setattr(a, "_call_cli", lambda p, m=None: calls.setdefault("cli", m))
    monkeypatch.setattr(a, "_call_api", lambda p, m=None: calls.setdefault("api", m))

    a.call_claude("x", model="openai/gpt-5.6-luna")
    assert calls == {"or": "openai/gpt-5.6-luna"}, "must not touch the other paths"

    calls.clear()
    monkeypatch.setattr(a, "_use_api", lambda: False)
    a.call_claude("x", model="sonnet")
    assert "cli" in calls and "or" not in calls


def test_openrouter_refuses_without_a_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    try:
        a._call_openrouter("x", "openai/gpt-5.6-luna")
    except RuntimeError as exc:
        assert "OPENROUTER_API_KEY" in str(exc)
    else:
        raise AssertionError("spent without a key")


def test_claim_fingerprint_survives_a_re_mint():
    """claim_id is a fresh uuid on every emission, so a re-digest orphans every
    page built before it. The fingerprint is derived from the claim's own text."""
    c = {
        "content": "The object accelerated away.",
        "claim_type": "observation",
        "original_excerpt": "it accelerated",
        "location_in_record": "char:10-24",
    }
    assert a._claim_fingerprint({**c, "id": "one"}) == a._claim_fingerprint(
        {**c, "id": "two"}
    )
    assert a._claim_fingerprint({**c, "content": "different"}) != a._claim_fingerprint(
        c
    )
    assert a._claim_fingerprint({"content": "x"}) is None, "needs a claim_type"


def test_fingerprint_uses_the_shared_field_mapping():
    """A digest names these text/type/quote/location; the hash takes
    content/claim_type/original_excerpt/location_in_record. Both spellings must
    produce the same key or two consumers silently disagree."""
    digest_shape = {
        "text": "X.",
        "type": "observation",
        "quote": "q",
        "location": "char:1-2",
    }
    graph_shape = {
        "content": "X.",
        "claim_type": "observation",
        "original_excerpt": "q",
        "location_in_record": "char:1-2",
    }
    assert a._claim_fingerprint(digest_shape) == a._claim_fingerprint(graph_shape)


def test_a_restricted_source_still_carries_its_quote():
    """A short attributed quotation beside the claim it supports is ordinary
    citation, and the project's quotation policy is explicit that such quotes
    "are published in full... and are NOT capped, truncated to a length limit, or
    gated". This was briefly gated on copyright status, stripping 5,449 quotes at
    a measured median of 26 words.

    The harm was not only legal over-caution: a claim with no quote beside it
    looks unevidenced, so suppressing them publishes a false statement about our
    own evidence. This test exists so that re-imposing the gate breaks a test
    rather than shipping.
    """
    claims = [
        {
            "id": "c1",
            "content": "The object accelerated away.",
            "original_excerpt": "it just accelerated away",
            "record_content_hash": "d" * 64,
            "record_title": "A Copyrighted Book",
        }
    ]
    fm = {"references": [{"text": "The object accelerated away.", "claim_index": 1}]}
    out = a._augment_references(fm, claims, None, None)
    ref = out["references"][0]
    assert ref["quote"] == "it just accelerated away", (
        "a quote must be emitted whatever the source's copyright status"
    )


def test_quote_is_not_body():
    """Nothing about quotes un-gates a full body or transcript. The
    source-display rule is separate and still fails closed."""
    assert a.source_display_mode("restricted", "pdf") == "none"
    assert a.source_display_mode(None, "video") == "none"
    assert a.source_display_mode("public_domain", "pdf") == "text"


def test_licensed_without_evidence_is_treated_as_restricted():
    """A `licensed` record with no evidence of the licence is indistinguishable
    from a mislabelled `restricted` one. All six in the store carry none."""
    assert a.effective_copyright_status({"status": "licensed"}) == "restricted"
    assert a.effective_copyright_status({"status": "licensed", "holder": "X"}) == (
        "licensed"
    )


def test_effective_status_is_unresolved_when_nothing_is_known():
    assert a.effective_copyright_status(None) == "unresolved"
    assert a.effective_copyright_status({}) == "unresolved"
    assert a.effective_copyright_status({"status": None}) == "unresolved"


def test_the_ingest_lookup_searches_both_store_roots(tmp_path):
    """store/v1/ holds 163 older records and every `licensed` status. A lookup
    that misses it returns 'unresolved' rather than failing - which is the same
    treatment licensed gets today, so the bug would have stayed invisible."""
    v1 = tmp_path / "store" / "v1"
    v1.mkdir(parents=True)
    (tmp_path / "store" / ("f" * 64 + ".v2.md")).write_text(
        "---\ncopyright:\n  status: public_domain\n---\n\nb\n"
    )
    (v1 / ("e" * 64 + ".md")).write_text(
        "---\ncopyright:\n  status: licensed\n---\n\nb\n"
    )
    top = a.load_ingest_meta(tmp_path, "f" * 64)
    old = a.load_ingest_meta(tmp_path, "e" * 64)
    assert top.get("status") == "public_domain"
    assert old.get("status") == "licensed", "store/v1 must resolve"
    assert old.get("effective_status") == "restricted", "no licence evidence"


def test_dry_run_returns_before_the_spend_gate():
    """--dry-run prints the prompt and calls nothing, so refusing it as a metered
    run is false - and it made the only way to check that a slug resolves to the
    right digest be to authorise real spend.

    The exemption is POSITIONAL: the dry-run return precedes the gate, so a run
    that spends nothing cannot reach it by construction rather than by a flag
    test somebody has to remember to keep correct.
    """
    import inspect

    src = inspect.getsource(a.main)
    assert src.index("if args.dry_run:") < src.index("spend_confirmed(")


def test_the_estimate_is_sized_on_this_article_not_a_constant():
    """It took only the model, so a 9 KiB digest and a 2.4 MiB one were both
    quoted at $0.05. A gate printing a number it did not compute manufactures the
    confidence that skips the check."""
    small = a._estimate_article_cost("openai/gpt-5.6-luna", 10_000)
    large = a._estimate_article_cost("openai/gpt-5.6-luna", 2_400_000)
    assert large["est_input_tokens"] > small["est_input_tokens"] * 100
    assert large["usd"] > small["usd"]


def test_a_record_page_is_estimated_on_its_own_output_ceiling():
    """A record page is capped at 300-400 words; an entity page is not. One
    ceiling for both overstates a record page's output roughly fourfold."""
    rec = a._estimate_article_cost("openai/gpt-5.6-luna", 50_000, is_record=True)
    ent = a._estimate_article_cost("openai/gpt-5.6-luna", 50_000, is_record=False)
    assert rec["est_output_tokens"] < ent["est_output_tokens"]
    assert rec["usd"] < ent["usd"]


def test_an_unsized_estimate_still_returns_the_old_constant():
    """Callers that cannot supply a length keep working rather than crashing -
    but they get the constant, which is why the gate now always supplies one."""
    est = a._estimate_article_cost("openai/gpt-5.6-luna")
    assert est["est_input_tokens"] == a.FIXED_INPUT_TOKENS


def test_the_refusal_names_this_component_s_flag():
    """The shared message hardcoded --confirm; this component's flag is
    --confirm-spend, so the instruction sent the reader to an argparse error at
    the moment they were trying to authorise correctly."""
    from anomalica_common.llm import spend_confirmed

    out = []
    spend_confirmed(
        a._estimate_article_cost("openai/gpt-5.6-luna"),
        "openai/gpt-5.6-luna",
        confirm=False,
        echo=out.append,
        use_api=True,
        flag="--confirm-spend",
    )
    assert any("--confirm-spend" in line for line in out)
    assert not any("with --confirm " in line for line in out)
