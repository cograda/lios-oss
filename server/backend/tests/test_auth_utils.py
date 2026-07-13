"""Tests for auth security utilities — constant-time comparison and encryption."""

from app.auth.utils import safe_token_check


class TestSafeTokenCheck:
    def test_matching_tokens(self):
        assert safe_token_check("abc123", "abc123") is True

    def test_mismatched_tokens(self):
        assert safe_token_check("abc123", "xyz789") is False

    def test_empty_provided(self):
        assert safe_token_check("", "abc123") is False

    def test_empty_expected(self):
        assert safe_token_check("abc123", "") is False

    def test_both_empty(self):
        assert safe_token_check("", "") is False

    def test_none_provided(self):
        assert safe_token_check(None, "abc123") is False

    def test_none_expected(self):
        assert safe_token_check("abc123", None) is False

    def test_long_tokens(self):
        token = "a" * 1000
        assert safe_token_check(token, token) is True
        assert safe_token_check(token, token + "x") is False
