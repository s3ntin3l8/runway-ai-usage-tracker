"""FleetRegistryService.get_due_providers cadence."""

from unittest.mock import patch

from app.services.fleet_registry import FleetRegistryService

PROVIDERS = [("a", 60), ("b", 300)]


def test_first_call_returns_everything_then_respects_interval():
    svc = FleetRegistryService()
    with patch("time.time", return_value=1000.0):
        assert svc.get_due_providers("s", PROVIDERS) == (["a", "b"], False)
    with patch("time.time", return_value=1030.0):
        assert svc.get_due_providers("s", PROVIDERS) == ([], False)
    with patch("time.time", return_value=1061.0):
        assert svc.get_due_providers("s", PROVIDERS) == (["a"], False)
    with patch("time.time", return_value=1301.0):
        assert svc.get_due_providers("s", PROVIDERS)[0] == ["a", "b"]


def test_history_is_per_sidecar():
    svc = FleetRegistryService()
    with patch("time.time", return_value=1000.0):
        svc.get_due_providers("s1", PROVIDERS)
        assert svc.get_due_providers("s2", PROVIDERS)[0] == ["a", "b"]


def test_pending_trigger_polls_everything_once():
    svc = FleetRegistryService()
    with patch("time.time", return_value=1000.0):
        svc.get_due_providers("s", PROVIDERS)
    svc.set_pending_trigger("s")
    with patch("time.time", return_value=1001.0):
        assert svc.get_due_providers("s", PROVIDERS) == (["a", "b"], True)
        assert svc.get_due_providers("s", PROVIDERS) == ([], False)
