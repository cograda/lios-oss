from app.plugin.manifest import IntegrationManifest

MANIFEST = IntegrationManifest(
    name="tasks",
    display_name="Tasks",
    version="1.0.0",
    type="capability",
    description=(
        "Task and project ledger (DB is source of truth); Task Backlog.md "
        "becomes a generated one-way view of it."
    ),
    icon="ListChecks",
    models=["TaskProgram", "TaskDomainTag", "TaskProject", "Task", "TaskLink", "TaskComment", "TaskEvent"],
    # Open tasks are embedded (title + description) so tasks_duplicates can
    # compare them for free and household search finds them. See dupes.py.
    embedding_sources=["task"],
    reads_from=[],
    writes_to=[],
    # Capture is user-gated and rendering is write-triggered; nothing to poll.
    schedule=None,
    schedule_timezone=None,
    freshness_threshold_minutes=None,
    staleness_probe=None,
    background_tasks=[],
    routes=[],
    config_schema={},
)
