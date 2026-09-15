"""Mutation-check for `conftest.py::_isolated_integration_registry`.

Several test files replace the whole `app.integrations.INTEGRATIONS` dict
(`INTEGRATIONS.clear(); register_all()`) with no fixture reverting it, which
is safe only because pytest happens to run files in a fixed order and
nothing downstream ever notices the leftover state. That stops being true
under `pytest-xdist -n auto`: worker scheduling interleaves tests from
different files non-deterministically, so a test relying on "whatever the
last mutator left behind" becomes a real, unreproducible flake.

This module proves the autouse registry-snapshot fixture actually closes
that hole, using the exact shape of the bug it fixes: one test plants a
fake registration and never cleans it up (deliberately — this is the
mutator under test, not a bug in it), and a second test — which must run
*after* it for this to mean anything, hence the explicit ordering below —
asserts the registry is back to whatever it was before the first test ran.

Disable the fixture (comment out its `autouse=True` in conftest.py, or run
with `-p no:cacheprovider` tricks aside — simplest is temporarily renaming
it) and `test_leftover_registration_is_not_visible_to_the_next_test` fails,
proving this isn't a test that would pass regardless.
"""

from app.integrations import INTEGRATIONS
from app.integrations.base import BaseIntegration


class _FakeIntegration(BaseIntegration):
    """A minimal stand-in — just enough to satisfy `BaseIntegration`'s ABC
    surface and the registry's dict value type. Never registered for real;
    this module plants it directly into `INTEGRATIONS` to simulate what
    `INTEGRATIONS.clear(); register_all()` (or a direct
    `INTEGRATIONS[name] = ...`) leaves behind when nothing reverts it.
    """

    @property
    def name(self) -> str:
        return "wave5_mutation_check_leftover"

    @property
    def display_name(self) -> str:
        return "Wave 5.4 mutation-check fixture"

    def sync(self) -> None:
        pass

    def mcp_tools(self):
        return []

    async def dashboard_data(self):
        return {}


def test_a_plant_a_leftover_registration_and_never_clean_it_up():
    """Deliberately pollutes the registry, mirroring the real bug shape
    (a test that clears/replaces `INTEGRATIONS` with no teardown). Must run
    before the assertion test below — the two are named `test_a_*`/`test_b_*`
    so alphabetic/file-order collection keeps them adjacent and in order
    even under plugins that don't otherwise reorder within a module.
    """
    INTEGRATIONS["wave5_mutation_check_leftover"] = _FakeIntegration()
    assert "wave5_mutation_check_leftover" in INTEGRATIONS


def test_b_leftover_registration_is_not_visible_to_the_next_test():
    """With the autouse snapshot/restore fixture active, the previous
    test's plant must not have survived into this one — `conftest.py`
    restores `INTEGRATIONS` to its pre-test snapshot at every test's
    teardown, regardless of what the test did to it.

    Disable `_isolated_integration_registry` (or its `autouse=True`) and
    this fails: the fake entry planted above is still sitting in the
    shared, never-reassigned `INTEGRATIONS` dict.
    """
    assert "wave5_mutation_check_leftover" not in INTEGRATIONS
