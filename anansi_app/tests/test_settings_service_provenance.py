"""An unset flag and a flag deliberately set to its default look identical in
`settings_defaults()`. Operators need to tell them apart, and secrets must never
round-trip their value to the browser."""

from services.settings_service import SettingsService, ValueSource


class FakeBackend:
    name = "fake"

    def __init__(self, values):
        self._values = values

    def available(self):
        return True

    def get_all(self):
        return dict(self._values)

    def update(self, settings, restart=True):
        return True, None


def _service(backend_values, monkeypatch, env=None):
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    service = SettingsService()
    service.backend = FakeBackend(backend_values)
    return service


def test_unset_flag_is_reported_as_default(monkeypatch):
    monkeypatch.delenv("MAX_TOOL_ROUNDS", raising=False)
    values = _service({}, monkeypatch).get_settings_with_provenance()
    assert values["MAX_TOOL_ROUNDS"].value == 5
    assert values["MAX_TOOL_ROUNDS"].source is ValueSource.DEFAULT


def test_env_value_equal_to_the_default_is_still_reported_as_set(monkeypatch):
    values = _service({}, monkeypatch, env={"MAX_TOOL_ROUNDS": "5"}).get_settings_with_provenance()
    assert values["MAX_TOOL_ROUNDS"].source is ValueSource.ENVIRONMENT


def test_backend_value_wins_and_is_labelled_backend(monkeypatch):
    service = _service({"MAX_TOOL_ROUNDS": "9"}, monkeypatch, env={"MAX_TOOL_ROUNDS": "5"})
    values = service.get_settings_with_provenance(fetch_from_do=True)
    assert values["MAX_TOOL_ROUNDS"].value == 9
    assert values["MAX_TOOL_ROUNDS"].source is ValueSource.BACKEND


def test_secret_value_is_never_returned(monkeypatch):
    service = _service({"GRAFANA_PASSWORD": "hunter2"}, monkeypatch)
    values = service.get_settings_with_provenance(fetch_from_do=True)
    entry = values["GRAFANA_PASSWORD"]
    assert entry.value == ""
    assert entry.secret_is_set is True


def test_unset_secret_reports_not_set(monkeypatch):
    monkeypatch.delenv("GRAFANA_PASSWORD", raising=False)
    values = _service({}, monkeypatch).get_settings_with_provenance()
    assert values["GRAFANA_PASSWORD"].secret_is_set is False
