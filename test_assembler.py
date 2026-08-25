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
