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
