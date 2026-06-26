import asyncio
import sys
from unittest.mock import MagicMock, patch

# Use sys.modules to get the actual module object (the package __init__ may
# shadow the module attribute with a same-named function).
import orchestrator.experts.handlers.package_generator.copy_template  # noqa: F401

ct = sys.modules["orchestrator.experts.handlers.package_generator.copy_template"]


class _Ctx:
    def __init__(self, inputs=None, state=None):
        self._inputs = inputs or {}
        self._state = state or {}

    def get_input(self, k, d=None):
        return self._inputs.get(k, d)

    def get_state(self, k, d=None):
        return self._state.get(k, d)

    async def send_progress_to_user(self, *a, **k):
        return True


def _ok_result():
    return type(
        "R",
        (),
        {
            "success": True,
            "error_message": None,
            "final_title": "Pankshin LPP",
            "document_id": "doc1",
            "document_url": "http://x/doc1",
            "template_type": "sheet",
        },
    )()


async def _fake_create(**_kwargs):
    return _ok_result()


def test_community_route_skips_submission_validation():
    # Route B (GPS anchor → GRID3 boundary) has no site_submissions row.
    # The handler must NOT do the submission lookup, which would fail with a
    # misleading "site wasn't found in your submissions" message.
    ctx = _Ctx(
        inputs={"template_id": "tmpl", "site_name": "Pankshin"},
        state={"geo_source": "community", "site_folder_id": "folder1"},
    )
    lookup = MagicMock()
    with (
        patch.object(ct, "_get_db_config", return_value={"host": "configured"}),
        patch.object(ct, "_lookup_site_by_name", lookup),
        patch.object(ct, "create_from_template", side_effect=_fake_create),
    ):
        result = asyncio.run(ct.copy_lpp_template(ctx))

    lookup.assert_not_called()  # community route must not hit site_submissions
    assert result.error is None
    assert result.state_updates["template_copied"] is True
    assert result.state_updates["document_id"] == "doc1"


def test_submission_route_still_validates_site():
    # Non-community route with an unknown site → fails as before, never
    # reaching document creation.
    ctx = _Ctx(
        inputs={"template_id": "tmpl", "site_name": "GhostSite"},
        state={"site_folder_id": "folder1"},
    )
    with (
        patch.object(ct, "_get_db_config", return_value={"host": "configured"}),
        patch.object(ct, "_lookup_site_by_name", return_value={"found": False}),
        patch.object(ct, "create_from_template", side_effect=_fake_create) as create,
    ):
        result = asyncio.run(ct.copy_lpp_template(ctx))

    assert result.error is not None
    assert "not found in site submissions" in result.error
    create.assert_not_called()
