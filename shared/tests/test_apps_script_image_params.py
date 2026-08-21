"""replace_sheet_image sends the target/fit_range params the .gs now reads."""

import pytest

from shared.utils import apps_script_client

# Real 1x1 PNG (same fixture the .gs file's own testReplaceSheetImage uses) --
# resize_image_for_sheets decodes eagerly with PIL before the payload is even
# built, so a placeholder string like "AAAA" fails before reaching the
# assertion this test is actually about.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIA"
    "X8jx0gAAAABJRU5ErkJggg=="
)


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def call_action(self, action, params=None):
        self.calls.append((action, params or {}))
        return apps_script_client.AppsScriptResult(success=True, action=action, data={})


@pytest.mark.asyncio
async def test_target_and_fit_range_reach_the_action_payload(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(apps_script_client, "AnansiAppsScriptClient", lambda *a, **k: fake)

    await apps_script_client.replace_sheet_image(
        sheet_id="s1", worksheet_name="Main Input", image_base64=_TINY_PNG_B64,
        target="{{site_map}}", fit_range="B6:F20",
    )

    action, params = fake.calls[0]
    assert action == "replace_sheet_image"
    assert params["target"] == "{{site_map}}"
    assert params["fit_range"] == "B6:F20"


@pytest.mark.asyncio
async def test_omitting_them_keeps_the_legacy_payload_shape(monkeypatch):
    """The LPP call site passes neither — its payload must not change."""
    fake = _FakeClient()
    monkeypatch.setattr(apps_script_client, "AnansiAppsScriptClient", lambda *a, **k: fake)

    await apps_script_client.replace_sheet_image(
        sheet_id="s1", worksheet_name="Proposed Budget",
        image_base64=_TINY_PNG_B64, min_height_px=100,
    )

    _, params = fake.calls[0]
    assert "target" not in params
    assert "fit_range" not in params
    assert params["min_height"] == 100
