"""populate_lpp_cells splits into three callable jobs behind one step name."""

import inspect

from orchestrator.experts.handlers.package_generator import populate_cells
from orchestrator.experts.step_registry import get_step_handler

# Snapshotted at module/collection time, not inside the test function.
# test_parameter_confirmation.py::TestRegisterStepWithoutSchema.setup_method
# clears the real global step registry with no teardown, and this file's
# name sorts after it alphabetically -- a fresh get_step_handler() call
# inside the test body sees a wiped registry when the full suite runs in
# order, even though this same test passes in isolation. Same fix already
# applied to test_process_doc_edits_contract.py and others.
_HANDLER = get_step_handler("populate_lpp_cells")


def test_the_registered_step_name_is_unchanged():
    """The Google Doc workflow definition names this step — it must not move."""
    assert _HANDLER is not None


def test_the_three_jobs_are_separately_callable():
    for name in ("fill_main_input_cells", "replace_map_image", "build_bom_tab"):
        assert hasattr(populate_cells, name), f"populate_cells.{name} missing"
        assert inspect.iscoroutinefunction(getattr(populate_cells, name))


def test_the_handler_body_is_now_short():
    """If the orchestrator grew back past ~80 lines, the split leaked.

    inspect.getsource() on a decorated function includes the @register_step(
    ...) StepContract block above it -- 76 lines of registration metadata
    (params, state contract, mock spec) that is unrelated to this split and
    isn't shrinking as part of it. Measuring from the `async def` line down
    is what would actually balloon back toward ~280 lines if someone pasted
    the old monolithic logic back into this function.
    """
    source = inspect.getsource(populate_cells.populate_lpp_cells)
    lines = source.splitlines()
    body_start = next(i for i, line in enumerate(lines) if line.startswith("async def "))
    assert len(lines) - body_start < 80
