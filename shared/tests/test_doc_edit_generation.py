"""generate_replacement_markdown's tool loop."""

import pytest

from shared.llm.types import GenerateResult, ToolCall
from shared.utils import doc_editing
from shared.utils.doc_edit_tools import ToolOutcome


class _Gateway:
    """Answers with a tool call first, then with text."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []

    async def generate(self, messages, options, **kwargs):
        self.calls.append(kwargs)
        return self.scripted.pop(0)


def _install(monkeypatch, gateway):
    monkeypatch.setattr(
        doc_editing, "_generation_gateway", lambda: gateway, raising=False
    )


@pytest.mark.asyncio
async def test_no_runner_means_no_tools_are_offered(monkeypatch):
    """The default stays a single untooled call -- Sheets and simple
    editorial rewrites must not pay for a tool round trip."""
    gateway = _Gateway([GenerateResult(text="rewritten", tool_calls=[])])
    _install(monkeypatch, gateway)

    out = await doc_editing.generate_replacement_markdown("tidy this", "old text")

    assert out == "rewritten"
    assert gateway.calls[0].get("tools") is None


@pytest.mark.asyncio
async def test_a_tool_call_is_executed_and_fed_back(monkeypatch):
    gateway = _Gateway(
        [
            GenerateResult(
                text="",
                tool_calls=[
                    ToolCall(id="1", name="customer_customer_get_grid_status", args={"grid": "X"})
                ],
            ),
            GenerateResult(text="L1 is at 3.2 kW.", tool_calls=[]),
        ]
    )
    _install(monkeypatch, gateway)
    seen = []

    async def _runner(name, args):
        seen.append((name, args))
        return ToolOutcome(text='{"inverter_l1_power_kw": 3.2}')

    out = await doc_editing.generate_replacement_markdown(
        "state the current power per phase at X", "{{Data}}", tool_runner=_runner
    )

    assert seen == [("customer_customer_get_grid_status", {"grid": "X"})]
    assert out == "L1 is at 3.2 kW."


@pytest.mark.asyncio
async def test_an_undeclared_tool_is_refused_not_executed(monkeypatch):
    gateway = _Gateway(
        [
            GenerateResult(
                text="",
                tool_calls=[
                    ToolCall(id="1", name="equipment_control_restart_inverter", args={})
                ],
            ),
            GenerateResult(text="I could not get that.", tool_calls=[]),
        ]
    )
    _install(monkeypatch, gateway)
    executed = []

    async def _runner(name, args):
        executed.append(name)
        return ToolOutcome(text="{}")

    await doc_editing.generate_replacement_markdown(
        "restart it", "{{Data}}", tool_runner=_runner
    )

    assert executed == [], "a tool outside the whitelist was executed"


@pytest.mark.asyncio
async def test_the_loop_is_bounded(monkeypatch):
    """A model that keeps calling tools must still terminate."""
    forever = [
        GenerateResult(
            text="", tool_calls=[ToolCall(id=str(i), name="grid_design_find_grid", args={})]
        )
        for i in range(10)
    ]
    forever.append(GenerateResult(text="done", tool_calls=[]))
    gateway = _Gateway(forever)
    _install(monkeypatch, gateway)
    rounds = []

    async def _runner(name, args):
        rounds.append(name)
        return ToolOutcome(text="{}")

    await doc_editing.generate_replacement_markdown(
        "find it", "{{Data}}", tool_runner=_runner
    )

    assert len(rounds) <= 3, f"loop ran {len(rounds)} rounds, expected at most 3"
