"""`scripts/reembed.py::drain` percentage logging — "honest numbers" pass.

Same shape as the `finance`/`lastfm`/`google_mail` coverage bugs: `pct = (total
/ expected * 100) if expected else 100` printed "100.0%" for an `expected == 0`
re-enqueue, which reads as "fully drained" for a queue that was never told to
expect anything. Unit tier — `drain` only calls `EmbeddingService.process_queue`
(mocked here) and logs; no real DB needed.
"""

import logging
from unittest.mock import patch

from app.scripts.reembed import drain


def _fake_process_queue(batches):
    """Return `batches` in order, then 0 forever (queue drained)."""
    it = iter(batches)

    def _fn(session, batch_size, **kwargs):
        return next(it, 0)

    return _fn


class TestDrainPercentageLogging:
    def test_zero_expected_logs_na_not_100_percent(self, mock_session, caplog):
        with patch(
            "app.services.embedding.EmbeddingService.process_queue",
            side_effect=_fake_process_queue([5, 0]),
        ):
            with caplog.at_level(logging.INFO):
                total = drain(mock_session, batch_size=100, expected=0)

        assert total == 5
        [record] = [r for r in caplog.records if "drained" in r.message]
        assert "n/a" in record.message
        assert "100.0%" not in record.message

    def test_nonzero_expected_still_logs_a_real_percentage(self, mock_session, caplog):
        with patch(
            "app.services.embedding.EmbeddingService.process_queue",
            side_effect=_fake_process_queue([50, 50, 0]),
        ):
            with caplog.at_level(logging.INFO):
                total = drain(mock_session, batch_size=100, expected=100)

        assert total == 100
        [record] = [r for r in caplog.records if "100/100" in r.message]
        assert "100.0%" in record.message
