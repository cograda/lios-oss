"""Tests for OAuth token encryption (Fernet)."""

from unittest.mock import patch

from cryptography.fernet import Fernet

from app.auth.encryption import decrypt_token, encrypt_token, is_encrypted


# Generate a test key (don't use in production)
TEST_KEY = Fernet.generate_key().decode()


class TestIsEncrypted:
    def test_fernet_ciphertext(self):
        f = Fernet(TEST_KEY.encode())
        ct = f.encrypt(b"hello").decode()
        assert is_encrypted(ct) is True

    def test_plaintext(self):
        assert is_encrypted("ya29.some-google-access-token") is False

    def test_empty(self):
        assert is_encrypted("") is False


class TestEncryptDecryptRoundtrip:
    @patch("app.auth.encryption.settings")
    def test_roundtrip(self, mock_settings):
        mock_settings.oauth_encryption_key = TEST_KEY
        plaintext = "ya29.a0ARrdaM_this-is-a-google-access-token"

        encrypted = encrypt_token(plaintext)
        assert encrypted != plaintext
        assert is_encrypted(encrypted)

        decrypted = decrypt_token(encrypted)
        assert decrypted == plaintext

    @patch("app.auth.encryption.settings")
    def test_already_encrypted_skipped(self, mock_settings):
        mock_settings.oauth_encryption_key = TEST_KEY
        plaintext = "some-token"

        encrypted_once = encrypt_token(plaintext)
        encrypted_twice = encrypt_token(encrypted_once)
        # Should not double-encrypt
        assert encrypted_once == encrypted_twice

    @patch("app.auth.encryption.settings")
    def test_plaintext_passthrough_no_key(self, mock_settings):
        mock_settings.oauth_encryption_key = ""
        plaintext = "ya29.some-token"

        assert encrypt_token(plaintext) == plaintext
        assert decrypt_token(plaintext) == plaintext

    @patch("app.auth.encryption.settings")
    def test_empty_string(self, mock_settings):
        mock_settings.oauth_encryption_key = TEST_KEY
        assert encrypt_token("") == ""
        assert decrypt_token("") == ""

    @patch("app.auth.encryption.settings")
    def test_decrypt_plaintext_unchanged(self, mock_settings):
        """Decrypting a plaintext value returns it unchanged (not Fernet format)."""
        mock_settings.oauth_encryption_key = TEST_KEY
        plaintext = "ya29.not-encrypted"
        assert decrypt_token(plaintext) == plaintext
