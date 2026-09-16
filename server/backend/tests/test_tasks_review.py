"""The next-action heuristic, and the three ways it was wrong first.

Every FALSE-POSITIVE case below is a real line from the live 246-task ledger
that an earlier version of `review.py` flagged incorrectly. They are the
point of this file: a heuristic gets tuned by someone who has not seen the
data, and these are the counter-examples that stop the obvious "improvement"
from being reapplied.
"""

import pytest

from app.integrations.tasks.review import FLAGS, flags_for, summarise


# --------------------------------------------------------------- true positives

@pytest.mark.parametrize("title", [
    "**Scope the headshot fundraiser**",
    "Look at office layout with IKEA Method stuff",
    "Sort out notebooks/journals on Surface",
    "**Investigate Carer's Allowance — potentially four years' worth**",
])
def test_container_verbs_are_flagged(title):
    assert "not_a_next_action" in flags_for(title)


@pytest.mark.parametrize("title", [
    "**Update the snag list, then re-export it in builder format**",
    "**Confirm the science club with Sam, then submit the sign-up**",
    "**Read Estefania's thread and reply to [[Lindsay]]**",
    "**Print [[Family Charter]] and put it up**",
])
def test_two_clauses_each_starting_with_a_verb_are_flagged(title):
    assert "several_actions" in flags_for(title)


@pytest.mark.parametrize("title", [
    "**Tech setup before leaving**",
    "Backlog dedupe + retire stale items",
])
def test_short_titles_with_no_verb_are_flagged(title):
    assert "no_action_named" in flags_for(title)


# -------------------------------------------------------------- false positives

@pytest.mark.parametrize("title", [
    # Nouns that are also imperatives: email / invoice / price / report. The
    # first version counted verb-shaped tokens anywhere in the clause and
    # called each of these several actions. All three are one action.
    "**Send the check-in email to [[David Moran]]** — draft ready, attach the list",
    "**Ask the behaviourist for an itemised invoice, not a €500 package price**",
    "**Get the verbatim March 2026 knee MRI report to replace the reconstruction**",
])
def test_nouns_that_are_also_verbs_are_not_several_actions(title):
    assert "several_actions" not in flags_for(title)


@pytest.mark.parametrize("title", [
    # A semicolon in the explanation after the em-dash is not a second action.
    "**Install the four spare cameras** — back door first; front door looks badly wired",
    "**Diagnose the STP loop warning** — triggers when TV connects; test the jack",
])
def test_prose_after_the_em_dash_is_not_a_second_action(title):
    assert "several_actions" not in flags_for(title)


@pytest.mark.parametrize("title", [
    # The ledger's own convention for deliberately-batched work.
    "**Put up the post box, the house sign, and re-jig that wall — one job**",
    "**Return the Branboll and drop the bike for service — one trip**",
])
def test_deliberately_batched_work_is_left_alone(title):
    assert "several_actions" not in flags_for(title)


@pytest.mark.parametrize("title", [
    # Real imperatives absent from the allowlist. Before the length bound
    # these were 52 flags at roughly one-in-six precision.
    "**Put the outdoor kit away before the weather ruins it**",
    "Phone Brooks and Strahan for collection prices on 18mm birch BB/BB",
    "**Source ~10 dimmable warm-white G9 LED bulbs**",
    "**Find out why Home Assistant gained 144 entities in a day**",
])
def test_long_titles_are_never_flagged_for_a_missing_verb(title):
    assert "no_action_named" not in flags_for(title)


def test_a_container_verb_never_also_reads_as_no_verb():
    """Contradictory flags. A container verb *is* a verb."""
    for title in ["Scope the fundraiser", "Own the migration", "Drive the plan"]:
        f = flags_for(title)
        assert "not_a_next_action" in f and "no_action_named" not in f


# --------------------------------------------------------------------- framing

def test_every_flag_has_a_human_explanation():
    """A flag with no sentence behind it reaches a person as jargon."""
    seen = set()
    for title in ["Scope it", "Print it and post it", "Tech setup", "x" * 500]:
        seen.update(flags_for(title))
    assert seen and seen <= set(FLAGS)


def test_summarise_carries_the_caveat_not_just_the_count():
    """The count alone overstates the problem, so it never travels alone."""
    out = summarise([("Scope the thing", ["not_a_next_action"]), ("Send it", [])])
    assert out["total"] == 2 and out["flagged"] == 1
    assert "false positive" in out["caveat"]
