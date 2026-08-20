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
