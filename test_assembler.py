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
