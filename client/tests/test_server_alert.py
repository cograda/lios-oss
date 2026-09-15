"""Unit tests for the server-silent detector (server_alert.py).

All driven by a fake clock and a fake notifier — no real osascript, no real
sockets, no daemon wiring. `network_check` and the AuthError classification
are stubbed directly so each test isolates exactly one branch of the state
machine.
"""

import pytest

from lios_sync.server_alert import ServerSilenceMonitor, STATE_OK, STATE_SERVER_SILENT, STATE_AUTH_INVALID, STATE_NETWORK_DOWN
from lios_sync.server_client import AuthError


class FakeClock:
    """Monotonic-ish fake clock, advanced explicitly by the test."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeNotifier:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, title: str, message: str) -> None:
        self.calls.append((title, message))


def _monitor(tmp_path, clock, notifier, *, threshold_minutes=20, refire_hours=6, network_up=True):
    return ServerSilenceMonitor(
        threshold_minutes=threshold_minutes,
        refire_hours=refire_hours,
        notifier=notifier,
        network_check=lambda: network_up,
        clock=clock,
        state_path=tmp_path / "server_alert_state.json",
    )


class TestThreshold:
    def test_threshold_not_reached_notifies_nothing(self, tmp_path):
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = _monitor(tmp_path, clock, notifier, threshold_minutes=20)

        monitor.record_result(ConnectionError("no route"))
        clock.advance(60 * 10)  # 10 minutes — under the 20-minute threshold
        monitor.record_result(ConnectionError("no route"))

        assert notifier.calls == []

    def test_threshold_reached_fires_exactly_one_notification(self, tmp_path):
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = _monitor(tmp_path, clock, notifier, threshold_minutes=20)

        monitor.record_result(ConnectionError("no route"))
        clock.advance(60 * 25)  # past the 20-minute threshold
        state = monitor.record_result(ConnectionError("no route"))

        assert state == STATE_SERVER_SILENT
        assert len(notifier.calls) == 1
        title, message = notifier.calls[0]
        assert title == "lios"
        assert "unreachable" in message

    def test_zero_threshold_disables_the_detector(self, tmp_path):
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = _monitor(tmp_path, clock, notifier, threshold_minutes=0)

        monitor.record_result(ConnectionError("no route"))
        clock.advance(60 * 60 * 24)  # a whole day of silence
        monitor.record_result(ConnectionError("no route"))

        assert notifier.calls == []


class TestRefire:
    def test_still_silent_inside_refire_window_does_not_renotify(self, tmp_path):
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = _monitor(tmp_path, clock, notifier, threshold_minutes=20, refire_hours=6)

        monitor.record_result(ConnectionError("no route"))  # silence starts
        clock.advance(60 * 25)
        monitor.record_result(ConnectionError("no route"))
        assert len(notifier.calls) == 1

        clock.advance(60 * 60 * 2)  # 2h later — still inside the 6h refire window
        monitor.record_result(ConnectionError("no route"))

        assert len(notifier.calls) == 1  # unchanged

    def test_still_silent_past_refire_window_fires_again(self, tmp_path):
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = _monitor(tmp_path, clock, notifier, threshold_minutes=20, refire_hours=6)

        monitor.record_result(ConnectionError("no route"))  # silence starts
        clock.advance(60 * 25)
        monitor.record_result(ConnectionError("no route"))
        assert len(notifier.calls) == 1

        clock.advance(60 * 60 * 7)  # past the 6h refire window
        monitor.record_result(ConnectionError("no route"))

        assert len(notifier.calls) == 2


class TestRecovery:
    def test_recovery_after_notified_silence_fires_one_back_notification(self, tmp_path):
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = _monitor(tmp_path, clock, notifier, threshold_minutes=20)

        monitor.record_result(ConnectionError("no route"))  # silence starts
        clock.advance(60 * 25)
        monitor.record_result(ConnectionError("no route"))
        assert len(notifier.calls) == 1

        state = monitor.record_result(None)

        assert state == STATE_OK
        assert len(notifier.calls) == 2
        title, message = notifier.calls[1]
        assert "back" in message.lower()

    def test_recovery_before_any_notification_is_silent(self, tmp_path):
        """Never crossed the threshold, so there was nothing to announce as back."""
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = _monitor(tmp_path, clock, notifier, threshold_minutes=20)

        monitor.record_result(ConnectionError("no route"))
        clock.advance(60 * 5)  # under threshold
        monitor.record_result(None)

        assert notifier.calls == []


class TestFailureClassification:
    def test_auth_error_fires_the_auth_message_not_the_silent_message(self, tmp_path):
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = _monitor(tmp_path, clock, notifier, threshold_minutes=20, network_up=True)

        state = monitor.record_result(AuthError("token rejected"))

        assert state == STATE_AUTH_INVALID
        assert len(notifier.calls) == 1
        _, message = notifier.calls[0]
        assert "token" in message.lower()
        assert "unreachable" not in message.lower()

    def test_own_network_down_fires_the_network_message(self, tmp_path):
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = _monitor(tmp_path, clock, notifier, threshold_minutes=20, network_up=False)

        state = monitor.record_result(ConnectionError("no route"))

        assert state == STATE_NETWORK_DOWN
        assert len(notifier.calls) == 1
        _, message = notifier.calls[0]
        assert "network" in message.lower()
        assert "unreachable" not in message.lower()

    def test_auth_error_does_not_wait_for_the_silent_threshold(self, tmp_path):
        """Auth failures are unambiguous immediately — no need to wait 20 minutes."""
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = _monitor(tmp_path, clock, notifier, threshold_minutes=20)

        monitor.record_result(AuthError("token rejected"))

        assert len(notifier.calls) == 1


class TestRestartSafety:
    def test_restart_after_a_fired_alert_does_not_immediately_renotify(self, tmp_path):
        state_path = tmp_path / "server_alert_state.json"
        clock = FakeClock()
        notifier = FakeNotifier()
        monitor = ServerSilenceMonitor(
            threshold_minutes=20, refire_hours=6, notifier=notifier,
            network_check=lambda: True, clock=clock, state_path=state_path,
        )
        monitor.record_result(ConnectionError("no route"))  # silence starts
        clock.advance(60 * 25)
        monitor.record_result(ConnectionError("no route"))
        assert len(notifier.calls) == 1

        # Simulate a daemon restart: a fresh monitor loading the same state file.
        notifier2 = FakeNotifier()
        monitor2 = ServerSilenceMonitor(
            threshold_minutes=20, refire_hours=6, notifier=notifier2,
            network_check=lambda: True, clock=clock, state_path=state_path,
        )
        monitor2.record_result(ConnectionError("no route"))  # still silent, no time passed

        assert notifier2.calls == []

    def test_state_persists_across_instances(self, tmp_path):
        state_path = tmp_path / "server_alert_state.json"
        clock = FakeClock()
        monitor = ServerSilenceMonitor(
            threshold_minutes=20, refire_hours=6, notifier=FakeNotifier(),
            network_check=lambda: True, clock=clock, state_path=state_path,
        )
        monitor.record_result(ConnectionError("no route"))

        monitor2 = ServerSilenceMonitor(
            threshold_minutes=20, refire_hours=6, notifier=FakeNotifier(),
            network_check=lambda: True, clock=clock, state_path=state_path,
        )

        assert monitor2.state()["state"] == STATE_SERVER_SILENT
        assert monitor2.state()["since"] == monitor.state()["since"]


class TestStateSnapshot:
    def test_state_reports_ok_initially(self, tmp_path):
        monitor = _monitor(tmp_path, FakeClock(), FakeNotifier())
        snapshot = monitor.state()
        assert snapshot["state"] == STATE_OK
        assert snapshot["threshold_minutes"] == 20
        assert snapshot["refire_hours"] == 6
