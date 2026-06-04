"""Unit tests for DazScriptServerClient — focuses on availability caching."""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from managers.daz_script_server import DazScriptServerClient


@pytest.fixture
def client():
    return DazScriptServerClient()


def _make_status(detected: bool) -> dict:
    return {
        "plugin_detected": detected,
        "plugin_url": "http://127.0.0.1:18811",
        "version": "1.0" if detected else None,
        "auth_enabled": False if detected else None,
        "active_requests": 0,
    }


class TestIsAvailableCache:
    def test_returns_true_when_plugin_detected(self, client):
        with patch.object(client, "status", return_value=_make_status(True)):
            assert client.is_available() is True

    def test_returns_false_when_plugin_not_detected(self, client):
        with patch.object(client, "status", return_value=_make_status(False)):
            assert client.is_available() is False

    def test_status_called_once_within_ttl(self, client):
        mock_status = MagicMock(return_value=_make_status(True))
        with patch.object(client, "status", mock_status):
            client.is_available()
            client.is_available()
            client.is_available()
        mock_status.assert_called_once()

    def test_status_recalled_after_ttl_expires(self, client):
        mock_status = MagicMock(return_value=_make_status(True))
        with patch.object(client, "status", mock_status):
            with patch("managers.daz_script_server.time") as mock_time:
                mock_time.monotonic.side_effect = [0.0, 11.0]
                client.is_available()
                client.is_available()
        assert mock_status.call_count == 2

    def test_cache_not_used_on_first_call(self, client):
        assert client._availability_cache is None
        mock_status = MagicMock(return_value=_make_status(False))
        with patch.object(client, "status", mock_status):
            client.is_available()
        mock_status.assert_called_once()

    def test_cache_populated_after_first_call(self, client):
        with patch.object(client, "status", return_value=_make_status(True)):
            client.is_available()
        assert client._availability_cache is not None
        result, ts = client._availability_cache
        assert result is True
        assert ts > 0

    def test_cached_value_returned_not_recomputed(self, client):
        with patch.object(client, "status", return_value=_make_status(True)):
            client.is_available()
        # Patch status to return False — cache should still return True
        with patch.object(client, "status", return_value=_make_status(False)):
            assert client.is_available() is True

    def test_ttl_boundary_just_inside(self, client):
        mock_status = MagicMock(return_value=_make_status(True))
        with patch.object(client, "status", mock_status):
            with patch("managers.daz_script_server.time") as mock_time:
                mock_time.monotonic.side_effect = [0.0, 9.99]
                client.is_available()
                client.is_available()
        mock_status.assert_called_once()

    def test_ttl_boundary_just_outside(self, client):
        mock_status = MagicMock(return_value=_make_status(True))
        with patch.object(client, "status", mock_status):
            with patch("managers.daz_script_server.time") as mock_time:
                mock_time.monotonic.side_effect = [0.0, 10.01]
                client.is_available()
                client.is_available()
        assert mock_status.call_count == 2

    def test_each_instance_has_independent_cache(self):
        c1 = DazScriptServerClient()
        c2 = DazScriptServerClient()
        with patch.object(c1, "status", return_value=_make_status(True)):
            c1.is_available()
        assert c2._availability_cache is None
