"""Generic template copy: no site validation, naming from the template's filename."""

import pytest

from orchestrator.experts.step_registry import get_step_contract


def test_contract_declares_template_and_folder_as_parameters():
    contract = get_step_contract("create_from_template")
    assert contract is not None
    names = {p.name for p in contract.params}
    assert {"template_id", "output_folder_id"} <= names


def test_contract_declares_the_document_id_it_produces():
    contract = get_step_contract("create_from_template")
    assert "document_id" in {o.name for o in contract.outputs}
    assert contract.mutates is True
    assert contract.mutation_kind == "external_write"


def test_mock_populates_document_id_so_downstream_steps_survive():
    """A mock returning nothing collapses the next step's precondition."""
    contract = get_step_contract("create_from_template")
    assert contract.mock is not None
    assert contract.mock.state_updates.get("document_id")


@pytest.mark.asyncio
async def test_accepts_a_pasted_url_as_well_as_a_bare_id(monkeypatch):
    import importlib

    mod = importlib.import_module(
        "orchestrator.experts.handlers.templates.create_from_template"
    )

    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)

        class R:
            success = True
            document_id = "new-doc"
            document_url = "https://docs.google.com/spreadsheets/d/new-doc"
            final_title = "ExampleGrid Site Package"
            template_type = "spreadsheet"
            error_message = None

        return R()

    monkeypatch.setattr(mod, "create_from_template_file", fake_create)

    class Ctx:
        packet_state = {}
        accumulated_results = {}

        def get_parameter_value(self, name, default=None):
            return {
                "template_id": "https://docs.google.com/spreadsheets/d/1TplRealisticFakeId123/edit#gid=0",
                "output_folder_id": "FOLDER1",
            }.get(name, default)

        def get_input(self, key, default=None):
            return default

        def get_state(self, key, default=None):
            return default

        async def send_progress_to_user(self, *_a, **_k):
            return None

    result = await mod.create_from_template(Ctx())
    assert captured["template_id"] == "1TplRealisticFakeId123"
    assert result.state_updates["document_id"] == "new-doc"


@pytest.mark.asyncio
async def test_no_folder_given_falls_back_to_the_templates_own_parent(monkeypatch):
    """GoogleTemplateCreator.copy_template forwards folder_id straight into
    the Drive API's parents list -- an empty string there 400s, so this
    step must resolve a real folder itself rather than pass "" through."""
    import importlib

    mod = importlib.import_module(
        "orchestrator.experts.handlers.templates.create_from_template"
    )

    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)

        class R:
            success = True
            document_id = "new-doc"
            document_url = "https://docs.google.com/document/d/new-doc"
            final_title = "Untitled Copy"
            template_type = "document"
            error_message = None

        return R()

    async def fake_resolve_parent(template_id):
        return "TEMPLATES-OWN-PARENT-FOLDER"

    monkeypatch.setattr(mod, "create_from_template_file", fake_create)
    monkeypatch.setattr(mod, "_resolve_default_output_folder", fake_resolve_parent)

    class Ctx:
        packet_state = {}
        accumulated_results = {}

        def get_parameter_value(self, name, default=None):
            return {"template_id": "TPL123"}.get(name, default)

        def get_input(self, key, default=None):
            return default

        def get_state(self, key, default=None):
            return default

        async def send_progress_to_user(self, *_a, **_k):
            return None

    await mod.create_from_template(Ctx())
    assert captured["output_folder_id"] == "TEMPLATES-OWN-PARENT-FOLDER"


@pytest.mark.asyncio
async def test_parent_lookup_failure_does_not_crash_the_step(monkeypatch):
    """Best-effort: if the fallback lookup itself fails, fall through to the
    same behaviour as before this fix existed rather than hard-failing."""
    import importlib

    mod = importlib.import_module(
        "orchestrator.experts.handlers.templates.create_from_template"
    )

    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)

        class R:
            success = True
            document_id = "new-doc"
            document_url = "https://docs.google.com/document/d/new-doc"
            final_title = "Untitled Copy"
            template_type = "document"
            error_message = None

        return R()

    async def fake_resolve_parent(template_id):
        return ""

    monkeypatch.setattr(mod, "create_from_template_file", fake_create)
    monkeypatch.setattr(mod, "_resolve_default_output_folder", fake_resolve_parent)

    class Ctx:
        packet_state = {}
        accumulated_results = {}

        def get_parameter_value(self, name, default=None):
            return {"template_id": "TPL123"}.get(name, default)

        def get_input(self, key, default=None):
            return default

        def get_state(self, key, default=None):
            return default

        async def send_progress_to_user(self, *_a, **_k):
            return None

    result = await mod.create_from_template(Ctx())
    assert captured["output_folder_id"] == ""
    assert result.data["document_id"] == "new-doc"
