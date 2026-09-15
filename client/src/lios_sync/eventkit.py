"""Native Apple Reminders access via EventKit (PyObjC).

Replaces the old osascript-based reminders.py with direct EventKit access.
Provides instant reads/writes and push-based change notifications via
EKEventStoreChangedNotification.

Threading model:
- ReminderStore is created on the main (asyncio) thread
- A dedicated daemon thread runs CFRunLoop for EventKit notifications
- All EKEventStore operations are guarded by a threading.Lock
- The change_event (asyncio.Event) bridges notifications to the async world
"""

import asyncio
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# EventKit entity type for reminders
_EK_ENTITY_REMINDER = 1

# Priority mapping: EventKit int → human-readable
PRIORITY_MAP = {0: "none", 1: "high", 5: "medium", 9: "low"}
PRIORITY_REVERSE = {"high": 1, "medium": 5, "low": 9, "none": 0}


class ReminderStore:
    """Thread-safe wrapper around EKEventStore with change notifications.

    Create once at daemon startup. The change_event asyncio.Event is set
    whenever reminders change (from any source — local, iCloud, other devices).
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
        source_emails: dict[str, str] | None = None,
    ):
        self._loop = loop or asyncio.get_event_loop()
        self._lock = threading.Lock()
        self._store = None
        self._observer = None
        self._runloop_thread = None
        self._keep_alive_timer = None
        self._access_granted = False
        # EKSource.sourceIdentifier (UUID string) → email. Used for multi-account
        # routing on Macs signed into more than one iCloud account. Empty in the
        # common single-iCloud case — daemon falls back to source title.
        self._source_emails = dict(source_emails or {})

        # Asyncio event — set by notification thread, awaited by daemon
        self.change_event = asyncio.Event()

        self._init_store()

    def _init_store(self) -> None:
        """Create EKEventStore and request access."""
        import EventKit
        import objc

        self._store = EventKit.EKEventStore.alloc().init()

        # Request access — blocks until user responds to the permission dialog
        granted_event = threading.Event()
        access_result = {"granted": False, "error": None}

        def completion(granted, error):
            access_result["granted"] = granted
            access_result["error"] = error
            granted_event.set()

        self._store.requestAccessToEntityType_completion_(
            _EK_ENTITY_REMINDER, completion
        )
        granted_event.wait(timeout=30)

        if not access_result["granted"]:
            err = access_result["error"]
            logger.error(f"EventKit access denied: {err}")
            return

        self._access_granted = True
        logger.info("EventKit access granted")

        # Start notification listener on dedicated thread
        self._start_notification_thread()

    def _start_notification_thread(self) -> None:
        """Start a background thread that pumps a CFRunLoop for EventKit notifications.

        EventKit's EKEventStoreChangedNotification requires an active Cocoa
        run loop to detect and deliver external changes. Python's asyncio
        doesn't pump the Cocoa run loop, so we run one on a dedicated thread
        with a keep-alive timer.
        """
        import Foundation

        def run_loop():
            # Register for change notifications on THIS thread's run loop
            center = Foundation.NSNotificationCenter.defaultCenter()
            self._observer = center.addObserverForName_object_queue_usingBlock_(
                "EKEventStoreChangedNotification",
                None,  # None = observe from ANY EKEventStore
                None,  # None = deliver on the posting thread (this thread)
                self._on_change,
            )
            logger.debug("EventKit notification observer registered on run loop thread")

            # Add a keep-alive timer so CFRunLoopRun() doesn't exit immediately.
            # The timer fires every 30s and calls refreshSourcesIfNecessary()
            # to nudge EventKit into detecting external changes.
            def timer_callback(timer):
                try:
                    with self._lock:
                        self._store.refreshSourcesIfNecessary()
                except Exception:
                    # A persistent failure here means EventKit has stopped
                    # noticing external changes — must not be silent.
                    logger.warning(
                        "EventKit refreshSourcesIfNecessary failed", exc_info=True
                    )

            self._keep_alive_timer = Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                30.0, True, timer_callback,
            )

            # Run forever — daemon thread is killed on process exit
            Foundation.CFRunLoopRun()

        self._runloop_thread = threading.Thread(
            target=run_loop, daemon=True, name="eventkit-runloop"
        )
        self._runloop_thread.start()

    def _on_change(self, notification) -> None:
        """Called on the run loop thread when reminders change."""
        logger.debug("EventKit change notification received")
        # Thread-safe signal to asyncio
        self._loop.call_soon_threadsafe(self.change_event.set)

    @property
    def is_available(self) -> bool:
        return self._access_granted

    def read_all_reminders(self) -> list[dict]:
        """Fetch all incomplete reminders across all lists.

        Called via asyncio.to_thread() from the daemon. Uses EventKit's
        async predicate fetch with a threading.Event to block until complete.
        """
        if not self._access_granted:
            logger.warning("EventKit not available — returning empty list")
            return []

        import EventKit

        predicate = self._store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
            None, None, None
        )

        results = []
        fetch_done = threading.Event()

        def completion(reminders):
            if reminders is not None:
                for r in reminders:
                    results.append(self._reminder_to_dict(r))
            fetch_done.set()

        with self._lock:
            self._store.fetchRemindersMatchingPredicate_completion_(
                predicate, completion
            )

        # Wait for async completion (we're in a thread, so blocking is fine)
        if not fetch_done.wait(timeout=30):
            logger.error("EventKit fetch timed out")
            return []

        logger.info(f"EventKit: read {len(results)} incomplete reminders")
        return results

    def add_reminder(
        self,
        title: str,
        list_name: str = "Reminders",
        due_date: str | None = None,
        priority: str = "none",
        notes: str | None = None,
        account_email: str | None = None,
    ) -> str:
        """Create a new reminder. Returns its calendarItemIdentifier (UUID).

        On Macs with multiple iCloud accounts a reminder list name can resolve
        to more than one calendar; pass `account_email` to disambiguate.
        """
        if not self._access_granted:
            raise RuntimeError("EventKit access not granted")

        import EventKit
        import Foundation

        with self._lock:
            # Find target calendar (prefer source matching account_email)
            calendar = self._find_calendar(list_name, account_email=account_email)
            if calendar is None:
                raise ValueError(f"Reminder list not found: {list_name}")

            reminder = EventKit.EKReminder.reminderWithEventStore_(self._store)
            reminder.setTitle_(title)
            reminder.setCalendar_(calendar)

            if notes:
                reminder.setNotes_(notes)

            if due_date:
                dt = self._parse_due_date(due_date)
                if dt:
                    # Set both dueDateComponents and startDateComponents
                    cal = Foundation.NSCalendar.currentCalendar()
                    components = cal.components_fromDate_(
                        Foundation.NSCalendarUnitYear
                        | Foundation.NSCalendarUnitMonth
                        | Foundation.NSCalendarUnitDay
                        | Foundation.NSCalendarUnitHour
                        | Foundation.NSCalendarUnitMinute,
                        dt,
                    )
                    reminder.setDueDateComponents_(components)

            prio = PRIORITY_REVERSE.get(priority, 0)
            reminder.setPriority_(prio)

            error = None
            success, error = self._store.saveReminder_commit_error_(
                reminder, True, None
            )
            if not success:
                raise RuntimeError(f"Failed to save reminder: {error}")

            uid = reminder.calendarItemIdentifier()
            logger.info(f"EventKit: created reminder '{title}' in '{list_name}' (uid={uid})")
            return uid

    def complete_reminder(self, uid: str) -> bool:
        """Mark a reminder as completed by its calendarItemIdentifier."""
        if not self._access_granted:
            raise RuntimeError("EventKit access not granted")

        import EventKit

        with self._lock:
            item = self._store.calendarItemWithIdentifier_(uid)
            if item is None:
                logger.warning(f"EventKit: reminder not found: {uid}")
                return False

            item.setCompleted_(True)

            success, error = self._store.saveReminder_commit_error_(
                item, True, None
            )
            if not success:
                logger.error(f"EventKit: failed to complete reminder: {error}")
                return False

            logger.info(f"EventKit: completed reminder {uid}")
            return True

    def update_reminder(
        self,
        uid: str,
        summary: str | None = None,
        notes: str | None = None,
        due_date: str | None = None,
        priority: str | None = None,
    ) -> bool:
        """Update fields on an existing reminder by its calendarItemIdentifier.

        Only non-None arguments are applied. Pass due_date='' to clear the due date.
        Returns True on success, False if the reminder was not found.
        """
        if not self._access_granted:
            raise RuntimeError("EventKit access not granted")

        import Foundation

        with self._lock:
            item = self._store.calendarItemWithIdentifier_(uid)
            if item is None:
                logger.warning(f"EventKit: reminder not found for update: {uid}")
                return False

            if summary is not None:
                item.setTitle_(summary)

            if notes is not None:
                item.setNotes_(notes)

            if due_date is not None:
                if due_date == "":
                    item.setDueDateComponents_(None)
                else:
                    ns_date = self._parse_due_date(due_date)
                    if ns_date:
                        cal = Foundation.NSCalendar.currentCalendar()
                        components = cal.components_fromDate_(
                            Foundation.NSCalendarUnitYear
                            | Foundation.NSCalendarUnitMonth
                            | Foundation.NSCalendarUnitDay
                            | Foundation.NSCalendarUnitHour
                            | Foundation.NSCalendarUnitMinute,
                            ns_date,
                        )
                        item.setDueDateComponents_(components)

            if priority is not None:
                item.setPriority_(PRIORITY_REVERSE.get(priority, 0))

            success, error = self._store.saveReminder_commit_error_(item, True, None)
            if not success:
                logger.error(f"EventKit: failed to update reminder {uid}: {error}")
                return False

            logger.info(f"EventKit: updated reminder {uid}")
            return True

    def get_lists(self) -> list[dict]:
        """Return all reminder lists with incomplete counts."""
        if not self._access_granted:
            return []

        import EventKit

        with self._lock:
            calendars = self._store.calendarsForEntityType_(_EK_ENTITY_REMINDER)

        results = []
        for cal in calendars:
            # Count incomplete reminders per list
            predicate = self._store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                None, None, [cal]
            )
            count_done = threading.Event()
            count_result = {"count": 0}

            def make_counter(result_dict, done_event):
                def counter(reminders):
                    result_dict["count"] = len(reminders) if reminders else 0
                    done_event.set()
                return counter

            with self._lock:
                self._store.fetchRemindersMatchingPredicate_completion_(
                    predicate, make_counter(count_result, count_done)
                )

            count_done.wait(timeout=10)
            results.append({
                "name": str(cal.title()),
                "count": count_result["count"],
                "account_email": self._account_label(cal),
            })

        return results

    def _find_calendar(self, name: str, account_email: str | None = None):
        """Find a reminder calendar by title. Must be called with lock held.

        When `account_email` is given, prefer a calendar whose source matches
        (by configured email or by source title). Falls back to any calendar
        with the right name if no source matches — better to write into the
        wrong source than to fail.
        """
        calendars = self._store.calendarsForEntityType_(_EK_ENTITY_REMINDER)
        matches = [cal for cal in calendars if str(cal.title()) == name]
        if not matches:
            return None
        if account_email and len(matches) > 1:
            for cal in matches:
                if self._account_label(cal) == account_email:
                    return cal
        return matches[0]

    def _reminder_to_dict(self, reminder) -> dict:
        """Convert an EKReminder to a dict matching the server's expected format."""
        due_date = None
        components = reminder.dueDateComponents()
        if components is not None:
            try:
                import Foundation
                cal = Foundation.NSCalendar.currentCalendar()
                ns_date = cal.dateFromComponents_(components)
                if ns_date:
                    # NSDate → Python datetime
                    timestamp = ns_date.timeIntervalSince1970()
                    due_date = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
            except Exception:
                # Reminder silently losing its due date is worth a trace.
                logger.warning(
                    "Failed to parse due date for reminder %r",
                    reminder.calendarItemIdentifier(),
                    exc_info=True,
                )

        return {
            "uid": str(reminder.calendarItemIdentifier()),
            "list_name": str(reminder.calendar().title()),
            "summary": str(reminder.title()),
            "priority": reminder.priority(),
            "completed": bool(reminder.isCompleted()),
            "due_date": due_date,
            "account_email": self._account_label(reminder.calendar()),
        }

    def _account_label(self, calendar) -> str | None:
        """Resolve a calendar to a stable per-account label.

        Returns the configured email if the source UUID is mapped, otherwise
        the source title (e.g. 'iCloud', 'Local'). Returns None if no source
        is attached (shouldn't happen but defensive).
        """
        try:
            source = calendar.source()
        except Exception:
            return None
        if source is None:
            return None
        try:
            ident = str(source.sourceIdentifier())
        except Exception:
            ident = ""
        if ident and ident in self._source_emails:
            return self._source_emails[ident]
        try:
            return str(source.title())
        except Exception:
            return None

    @staticmethod
    def _parse_due_date(date_str: str):
        """Parse an ISO date string to NSDate.

        Two bugs lived here until 2026-08-23, both about the *offset* rather
        than the parsing:

        1. The format list was `%Y-%m-%dT%H:%M:%S` / `%Y-%m-%dT%H:%M` /
           `%Y-%m-%d` with **no `%z` anywhere**, so a timezone-aware string
           (`2026-08-23T12:00:00+01:00`) matched nothing, fell through to the
           warning and returned `None` — the reminder was then created with
           **no due date and no alarm at all**. Passing a correctly qualified
           time was the one input guaranteed to fail, silently.
        2. Naive input was forced to UTC via `.replace(tzinfo=timezone.utc)`.
           Ireland is UTC+1 from late March to late October, so **every naive
           summer due time landed an hour late** — "noon" arrived at 13:00.

        Both are fixed by leaning on what `datetime.timestamp()` already does
        correctly: a **naive** datetime is interpreted in the **local** zone
        (which is what a caller saying "noon" means), and an **aware** one is
        converted from its own offset. The old `.replace(tzinfo=utc)` was
        overriding correct behaviour, not supplying it.

        ⚠️ Do not reintroduce `.replace(tzinfo=...)` here. `replace` on an
        aware datetime **clobbers** the offset instead of converting it, so
        adding `%z` support without deleting that call would turn a silent
        drop into a silent hour shift — worse, because it looks like it works.

        `fromisoformat` handles offsets, `Z` and date-only forms on the Python
        this daemon runs (3.11+), so it replaces the strptime loop; the loop
        stays only as a fallback for shapes `fromisoformat` rejects.
        """
        import Foundation

        dt = None
        try:
            dt = datetime.fromisoformat(date_str.strip())
        except ValueError:
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(date_str.strip(), fmt)
                    break
                except ValueError:
                    continue

        if dt is None:
            logger.warning(f"Could not parse due date: {date_str}")
            return None

        # Naive -> local time; aware -> converted from its offset. Both correct.
        return Foundation.NSDate.dateWithTimeIntervalSince1970_(dt.timestamp())

    def shutdown(self) -> None:
        """Clean up notification observer and timer."""
        if self._keep_alive_timer:
            self._keep_alive_timer.invalidate()
            self._keep_alive_timer = None
        if self._observer:
            import Foundation
            center = Foundation.NSNotificationCenter.defaultCenter()
            center.removeObserver_(self._observer)
            self._observer = None
            logger.debug("EventKit notification observer removed")
