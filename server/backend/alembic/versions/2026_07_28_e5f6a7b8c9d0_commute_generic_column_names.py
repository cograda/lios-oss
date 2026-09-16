"""commute_decisions — rename place-specific columns to generic ones.

Part of separating platform code from personalisation (2026-07-28). Three
columns encoded this household's specific commute in the schema:

    target_bus_arr_howth     -> target_bus_arr_interchange
    target_train_howth_dep   -> target_train_interchange_dep
    target_train_central_arr -> target_train_dest_arr
    howth_delay_min          -> interchange_delay_min

The solver was always direction- and route-agnostic (`domain.Route` is a
dataclass); only the two Route *instances* and these column names named a
place. The instances moved to config (`commute/routing.py`), and these names
follow.

Renames only — no type changes, no data movement. In Postgres
`ALTER TABLE ... RENAME COLUMN` is a catalogue-only operation: instant, no
table rewrite, no lock beyond the brief ACCESS EXCLUSIVE.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "commute_decisions"

# old -> new
_RENAMES = {
    "target_bus_arr_howth": "target_bus_arr_interchange",
    "target_train_howth_dep": "target_train_interchange_dep",
    "target_train_central_arr": "target_train_dest_arr",
    "howth_delay_min": "interchange_delay_min",
}


def upgrade() -> None:
    for old, new in _RENAMES.items():
        op.alter_column(_TABLE, old, new_column_name=new)


def downgrade() -> None:
    for old, new in _RENAMES.items():
        op.alter_column(_TABLE, new, new_column_name=old)
