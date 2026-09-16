"""Kernel-owned tables for the algo harness — predictions, model versions, runs.

Three tables, owned by the kernel rather than by any integration, because they
are *shared* by every deriver. The manifest contract (`app.plugin.validate.
_check_models`) requires an integration's declared models to resolve inside its
own `models.py`, so a per-integration table would mean one predictions table
per algo — and then scoring, backtesting and the dashboard would each need
per-algo code. One shared grain instead: a new deriver declares
`models=[]`, writes rows here through `app.algo`, and gets scoring for free
with zero kernel edits. Building the harness is a kernel change; adding an
algo is not.

Household-shared (no `UserOwnedMixin`), for the same reason `commute_decisions`
and `ha_*` are: rows are produced by the unattended scheduler with no
`current_user_id()` context, so there is no user to scope to. A per-user
prediction (say a sleep forecast) would need its own decision about ownership;
until one exists, adding the mixin speculatively would give every row a NULL
owner and a misleading column.

The shape that matters is `made_at` vs `target_at`. A solver's output is
verifiable the moment it is made; a predictor's output is only verifiable
later, so the row has to say *when it was claimed* and *what moment it claims
about*. Everything scoring does — error by horizon, skill against a baseline,
drift over time — is a join on those two columns.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class AlgoModelVersion(Base):
    """One fitted model, stored as JSON parameters rather than a pickle.

    The JSON-only rule is deliberate and enforced in `app.algo.estimators`: a
    pickle is bound to the exact library version that wrote it, and this repo
    already caps every dependency's major *because* an unpinned bump broke the
    tool surface once (see requirements.txt). A pickled estimator would make a
    scikit-learn upgrade a silent prediction outage instead of a loud import
    error. JSON parameters also mean a model is diffable in a code review and
    is carried by the nightly `pg_dump` with no extra plumbing.

    `is_active` picks the version that serves. Training writes a new row
    inactive; activation is a separate, explicit step, so a bad fit is a row
    nobody reads rather than a live regression.
    """

    __tablename__ = "algo_model_versions"
    __table_args__ = (
        UniqueConstraint("algo", "version", name="uq_algo_model_versions_algo_version"),
        Index("ix_algo_model_versions_algo_active", "algo", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    algo: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)

    #: Estimator class name from `app.algo.estimators` — the key used to
    #: reconstruct the model from `params`.
    kind: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSONB, server_default="{}")

    #: Ordered feature names the params are indexed against. Stored because
    #: a fitted coefficient vector is meaningless without the column order
    #: that produced it — this is the artifact half of the train/serve-skew
    #: defence (`app.algo.features` is the other half).
    feature_names: Mapped[list] = mapped_column(JSONB, server_default="[]")

    trained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    trained_rows: Mapped[int] = mapped_column(Integer, default=0)
    train_window_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    train_window_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Fit-time metrics (in-sample and holdout). Not the same numbers as the
    #: live scoring pass produces — these are what the fit thought of itself,
    #: kept so the two can be compared when live error drifts.
    metrics: Mapped[dict] = mapped_column(JSONB, server_default="{}")

    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class AlgoPrediction(Base):
    """One claim about one quantity at one future moment.

    Uniqueness is `(algo, quantity, target_at, horizon_min)`, not something
    keyed on `made_at`. Two different rows for the same target moment at
    different horizons is the *point* — it is what makes "how good are we 6
    hours out versus 1 hour out" answerable. But re-running the same cycle
    (a retry, a manual trigger, a misfire catch-up) must not stack duplicate
    rows at the same horizon, so that combination upserts. Same idempotency
    reasoning as financier's content-addressed transaction ids.

    `actual`/`scored_at`/`error` start NULL and are filled in later by the
    kernel scoring job once reality is observable. A NULL `scored_at` on an
    old row is therefore a real signal: either the algo's `observe()` cannot
    see that moment, or the scoring job is not running.
    """

    __tablename__ = "algo_predictions"
    __table_args__ = (
        UniqueConstraint(
            "algo",
            "quantity",
            "target_at",
            "horizon_min",
            name="uq_algo_predictions_target",
        ),
        # The scoring job's query: unscored rows whose target is now in the
        # past, oldest first.
        Index("ix_algo_predictions_unscored", "algo", "scored_at", "target_at"),
        Index("ix_algo_predictions_made", "algo", "made_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    algo: Mapped[str] = mapped_column(String(64), index=True)
    quantity: Mapped[str] = mapped_column(String(64))

    made_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    target_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    horizon_min: Mapped[int] = mapped_column(Integer)

    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lower: Mapped[float | None] = mapped_column(Float, nullable=True)
    upper: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: The model that produced it, and a hash of the exact feature vector it
    #: saw. Together these make a bad prediction reproducible: you can refit
    #: the same version and confirm the same input gives the same output.
    algo_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    features_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    #: The comparison every forecast has to beat to be worth running. Recorded
    #: at prediction time, not at scoring time, so it cannot be chosen after
    #: the fact to flatter the model.
    baseline: Mapped[float | None] = mapped_column(Float, nullable=True)

    actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[float | None] = mapped_column(Float, nullable=True)
    scored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AlgoRun(Base):
    """One execution of a deriver — predict, train, or score.

    This is the deriver equivalent of `SyncState`/`SyncHistory` for a source,
    plus the bit those do not need: LLM cost. An algo may be numeric, or it may
    be a prompt; either way its runs land in one ledger, so "what did the
    predictive layer cost this month" is a query rather than a guess. The token
    fields come from `coglib.llm.Response`, which is the only place in the
    house that adds Google's separate `thoughtsTokenCount` correctly.
    """

    __tablename__ = "algo_runs"
    __table_args__ = (Index("ix_algo_runs_algo_started", "algo", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    algo: Mapped[str] = mapped_column(String(64), index=True)

    #: predict | train | score
    kind: Mapped[str] = mapped_column(String(16))

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    n_predictions: Mapped[int] = mapped_column(Integer, default=0)
    n_scored: Mapped[int] = mapped_column(Integer, default=0)
    model_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    llm_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    detail: Mapped[dict] = mapped_column(JSONB, server_default="{}")
