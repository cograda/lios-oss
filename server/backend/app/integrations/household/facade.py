"""Facade for the `household` domains capability.

`tasks` (the loops layer, 2026-09-01) is the first consumer: domain tags on
programs, projects and tasks reference `Domain` rows by name and never create
them. `manifest.py`'s `provides=[]` is still deliberate — a fixed 1:1
dependency imports this module directly, per the last paragraph — and the
module existed before that consumer for two narrower reasons:

  - `has_data(session, user_id)` — the optional hook
    `app.mcp.instructions.render_instructions_for_user` calls, if present,
    to decide whether to mention an integration's tools in a user's
    personalized "Your Setup" instructions (see `google_mail`/`coffee`/
    `lastfm`/`apple_health`/`whatsapp` facades for the same cheap-COUNT
    shape). A user who owns no domain shouldn't be told about
    `household_*` tools they'll never use.
  - A stable read surface (`list_domains`, `get_domain`) for a future
    consumer — a `system` morning-briefing line, or Phase C's handoff
    view — to call through rather than reaching into `household.models`
    directly. `tests/test_capability_boundaries.py` forbids that cross-
    package import the moment anything else needs Domains data, so this
    seam exists in advance rather than being bolted on under time pressure.

Never `from household.models import Domain` and use it directly from
another package — go through `get_capability("household.domains")` (once a
consumer exists and `provides` is populated) or import this facade module
directly for a fixed 1:1 dependency.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.integrations.household.models import Domain


class HouseholdFacade:
    def has_data(self, session: Session, user_id: int) -> bool:
        return (
            session.query(Domain).filter(Domain.owner_id == user_id).first()
            is not None
        )

    def list_domains(self, session: Session) -> list[Domain]:
        return session.query(Domain).order_by(Domain.name).all()

    def domain_names_by_id(self, session: Session) -> dict[int, str]:
        """id → name, one query. For a consumer that stores `domain_id`
        foreign keys (tasks' `task_domain_tags`) and needs to render names
        without joining a model it is not allowed to import."""
        return dict(session.query(Domain.id, Domain.name).all())

    def get_domain(self, session: Session, name: str) -> Domain | None:
        return (
            session.query(Domain)
            .filter(Domain.name.ilike((name or "").strip()))
            .one_or_none()
        )


FACADE = HouseholdFacade()
