"""Which registered step handlers a skill author may pick."""

from orchestrator.experts.step_registry import (
    get_step_registry,
    register_step,
)


def test_handlers_are_not_exposed_by_default():
    """The registry holds handlers that mutate spreadsheets and trigger BOM
    generation. None may appear in a picker without a deliberate opt-in."""

    @register_step("test_unexposed_handler")
    async def _handler(context):
        return None

    registry = get_step_registry()
    assert "test_unexposed_handler" not in registry.builder_exposed_handlers()


def test_a_handler_can_opt_in():
    @register_step("test_exposed_handler", exposed_to_builder=True)
    async def _handler(context):
        return None

    registry = get_step_registry()
    assert "test_exposed_handler" in registry.builder_exposed_handlers()


def test_exposed_handlers_are_sorted():
    names = get_step_registry().builder_exposed_handlers()
    assert names == sorted(names)
