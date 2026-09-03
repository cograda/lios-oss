"""Tests for the `notify.email` capability (`email_client.py` + `facade.py`).

Mirrors `test_notifications.py`'s style for the push sibling: stub the one
external dependency (`plugin_config`, and here `smtplib.SMTP_SSL` in place
of the HA notify call) and pin behaviour rather than actually sending mail.
No test in this module may open a real SMTP connection — `_FakeSMTP` below
stands in for `smtplib.SMTP_SSL` in every case that reaches the transport.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.errors import PermanentError, TransientError
from app.integrations.notifications import email_client
from app.integrations.notifications.email_client import EmailAttachment, EmailConfigError
from app.integrations.notifications.facade import NotificationsFacade


def _config(**overrides):
    base = {
        "smtp_host": "mail-eu.smtp2go.com",
        "smtp_port": 465,
        "smtp_username": "smtp2go-user",
        "smtp_password": "smtp2go-pass",
        "smtp_from_address": "alex@comar.ie",
        "email_targets": {"1": "alex@comar.ie", "2": "sam@comar.ie"},
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeSMTP:
    """Stands in for `smtplib.SMTP_SSL` as a context manager.

    Records every login/send_message call; `login_error` / `send_error`, if
    set, are raised from the corresponding call so tests can exercise
    failure classification without touching a socket.
    """

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, login_error=None, send_error=None):
        self.host = host
        self.port = port
        self.login_calls: list[tuple[str, str]] = []
        self.sent_messages: list = []
        self._login_error = login_error
        self._send_error = send_error

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def login(self, username, password):
        if self._login_error:
            raise self._login_error
        self.login_calls.append((username, password))

    def send_message(self, message):
        if self._send_error:
            raise self._send_error
        self.sent_messages.append(message)


@pytest.fixture
def fake_smtp_factory(monkeypatch):
    """Returns a factory; tests set `.login_error` / `.send_error` on the
    returned holder before calling `email_client.send()`."""
    holder = SimpleNamespace(instance=None, login_error=None, send_error=None)

    def _factory(host, port, timeout=None):
        holder.instance = _FakeSMTP(
            host, port, timeout, login_error=holder.login_error, send_error=holder.send_error
        )
        return holder.instance

    monkeypatch.setattr(email_client.smtplib, "SMTP_SSL", _factory)
    return holder


class TestUnconfigured:
    def test_no_smtp_credentials_is_a_quiet_no_op(self, monkeypatch):
        monkeypatch.setattr(
            email_client, "plugin_config", lambda name: _config(smtp_username="", smtp_password="")
        )
        facade = NotificationsFacade()
        result = facade.send_email(1, "subject", "<p>hi</p>", "hi")
        assert result is False

    def test_unknown_recipient_is_a_quiet_no_op(self, monkeypatch):
        monkeypatch.setattr(email_client, "plugin_config", lambda name: _config())
        facade = NotificationsFacade()
        result = facade.send_email(999, "subject", "<p>hi</p>", "hi")
        assert result is False

    def test_send_raises_email_config_error_directly(self, monkeypatch):
        """The lower-level `email_client.send()` (not the facade) raises —
        the facade is what swallows it. A caller reaching for the transport
        directly (which the capability-boundary test forbids from another
        package, but is fine for a same-package unit test) sees the typed
        error, not a bare KeyError/AttributeError."""
        monkeypatch.setattr(email_client, "plugin_config", lambda name: _config())
        with pytest.raises(EmailConfigError):
            email_client.send(999, "subject", "<p>hi</p>", "hi")


class TestConfiguredSend:
    def test_builds_message_with_to_from_subject_and_attachment(self, monkeypatch, fake_smtp_factory):
        monkeypatch.setattr(email_client, "plugin_config", lambda name: _config())
        attachment = EmailAttachment(
            filename="transcript.txt", content_type="text/plain", content=b"hello world"
        )
        facade = NotificationsFacade()
        result = facade.send_email(
            1, "Transcript complete: Test", "<p>Test</p>", "Test", attachments=[attachment]
        )

        assert result is True
        sent = fake_smtp_factory.instance.sent_messages
        assert len(sent) == 1
        message = sent[0]
        assert message["To"] == "alex@comar.ie"
        assert message["From"] == "alex@comar.ie"
        assert message["Subject"] == "Transcript complete: Test"
        assert fake_smtp_factory.instance.login_calls == [("smtp2go-user", "smtp2go-pass")]

        attachment_parts = list(message.iter_attachments())
        assert len(attachment_parts) == 1
        assert attachment_parts[0].get_filename() == "transcript.txt"
        assert attachment_parts[0].get_content_type() == "text/plain"
        assert attachment_parts[0].get_content() == "hello world"

    def test_routes_to_second_users_address(self, monkeypatch, fake_smtp_factory):
        monkeypatch.setattr(email_client, "plugin_config", lambda name: _config())
        facade = NotificationsFacade()
        facade.send_email(2, "subject", "<p>hi</p>", "hi")
        assert fake_smtp_factory.instance.sent_messages[0]["To"] == "sam@comar.ie"

    def test_uses_configured_host_and_port(self, monkeypatch, fake_smtp_factory):
        monkeypatch.setattr(
            email_client, "plugin_config", lambda name: _config(smtp_host="custom.smtp.example", smtp_port=587)
        )
        facade = NotificationsFacade()
        facade.send_email(1, "subject", "<p>hi</p>", "hi")
        assert fake_smtp_factory.instance.host == "custom.smtp.example"
        assert fake_smtp_factory.instance.port == 587


class TestFailureClassificationAndSwallowing:
    def test_auth_error_is_permanent_and_swallowed_by_facade(self, monkeypatch, fake_smtp_factory):
        import smtplib

        monkeypatch.setattr(email_client, "plugin_config", lambda name: _config())
        fake_smtp_factory.login_error = smtplib.SMTPAuthenticationError(535, b"bad creds")

        with pytest.raises(PermanentError):
            email_client.send(1, "subject", "<p>hi</p>", "hi")

        facade = NotificationsFacade()
        result = facade.send_email(1, "subject", "<p>hi</p>", "hi")
        assert result is False

    def test_connection_error_is_transient_and_swallowed_by_facade(self, monkeypatch, fake_smtp_factory):
        monkeypatch.setattr(email_client, "plugin_config", lambda name: _config())
        fake_smtp_factory.send_error = ConnectionRefusedError("connection refused")

        with pytest.raises(TransientError):
            email_client.send(1, "subject", "<p>hi</p>", "hi")

        facade = NotificationsFacade()
        result = facade.send_email(1, "subject", "<p>hi</p>", "hi")
        assert result is False

    def test_unexpected_exception_is_also_swallowed_by_facade(self, monkeypatch):
        """Belt-and-braces: even a bug in the transport must not take down
        whatever called `send_email()` alongside real work."""

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(email_client, "send", _boom)
        facade = NotificationsFacade()
        result = facade.send_email(1, "subject", "<p>hi</p>", "hi")
        assert result is False
