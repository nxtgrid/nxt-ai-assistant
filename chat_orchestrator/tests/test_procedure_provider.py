"""ProcedureProvider reads procedures from context modules."""

from orchestrator.services.procedure_provider import ProcedureProvider


class _Store:
    def __init__(self, modules):
        self._modules = modules

    def all_modules(self):
        return self._modules


def _module(slug, title, summary, body):
    from shared.prompts.knowledge import KnowledgeModule

    return KnowledgeModule(
        id=slug, slug=slug, title=title, summary=summary, body=body,
        tags=["procedure"], mode="on_demand",
    )


def test_reads_procedures_from_tagged_modules():
    store = _Store([
        _module("procedure-comms", "Comms Loss", "Meter offline.", "## Steps\n1. Check link"),
    ])
    procedures = ProcedureProvider(store=store).get_procedures()

    assert len(procedures) == 1
    assert procedures[0].title == "Comms Loss"
    assert "Check link" in procedures[0].full_text


def test_purpose_comes_from_the_module_summary():
    store = _Store([_module("procedure-comms", "Comms Loss", "Meter offline.", "body")])
    assert ProcedureProvider(store=store).get_procedures()[0].purpose == "Meter offline."


def test_ignores_modules_not_tagged_as_procedures():
    from shared.prompts.knowledge import KnowledgeModule

    store = _Store([
        KnowledgeModule(id="x", slug="hps-tiers", title="HPS", summary="s",
                        body="b", tags=["reference"]),
    ])
    assert ProcedureProvider(store=store).get_procedures() == []


def test_procedure_ids_are_stable_across_reordering():
    """The id must not be a positional index -- chunk_procedure_map persists it."""
    store = _Store([
        _module("procedure-b", "B", "s", "b"),
        _module("procedure-a", "A", "s", "a"),
    ])
    ids = {p.title: p.id for p in ProcedureProvider(store=store).get_procedures()}

    store2 = _Store([
        _module("procedure-a", "A", "s", "a"),
        _module("procedure-b", "B", "s", "b"),
    ])
    ids2 = {p.title: p.id for p in ProcedureProvider(store=store2).get_procedures()}

    assert ids == ids2


def test_a_failing_store_yields_no_procedures():
    class _Boom:
        def all_modules(self):
            raise RuntimeError("db down")

    assert ProcedureProvider(store=_Boom()).get_procedures() == []
