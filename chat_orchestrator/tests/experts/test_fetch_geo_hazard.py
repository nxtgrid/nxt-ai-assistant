import json

import pytest

from orchestrator.experts.handlers.package_generator.fetch_geo_hazard import fetch_geo_hazard


class _McpExecutor:
    def __init__(self, responses):
        # responses: list of str (JSON or "Error: ...") or Exception, consumed in order.
        self._responses = list(responses)
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class _Ctx:
    def __init__(self, state=None, previous_results=None, mcp_executor=None):
        self._state = state or {}
        self._previous = previous_results or {}
        self.mcp_executor = mcp_executor

    def get_state(self, key, default=None):
        return self._state.get(key, default)

    def get_previous_result(self, step_name):
        return self._previous.get(step_name)

    async def send_progress_to_user(self, *a, **k):
        return True


def _hazard_json(flood_m=1.2, elev_m=245.0, warnings=None):
    return json.dumps(
        {
            "flood_worst_case_depth_m": flood_m,
            "flood_riverine_rp1000_m": flood_m,
            "flood_coastal_rp1000_m": 0.0,
            "flood_rp100_historical_m": 0.3,
            "flood_rp100_rcp85_2050_median_m": 0.5,
            "flood_rp100_rcp85_2050_max_m": 0.7,
            "site_elevation_m": elev_m,
            "data_warnings": warnings or [],
        }
    )


@pytest.mark.asyncio
async def test_idempotency_guard_returns_cached_data():
    ctx = _Ctx(
        state={
            "geo_hazard_fetched": True,
            "geo_hazard_per_candidate": [{"rank": 1, "flood_worst_case_depth_m": 2.0}],
        }
    )
    result = await fetch_geo_hazard(ctx)
    assert result.data["flood_worst_case_depth_m"] == 2.0


@pytest.mark.asyncio
async def test_no_candidates_and_no_map_fails():
    ctx = _Ctx(previous_results={"generate_distribution_layout": {}})
    result = await fetch_geo_hazard(ctx)
    assert not result.is_success
    assert "generate_distribution_layout" in result.error


@pytest.mark.asyncio
async def test_falls_back_to_map_center_when_no_candidates():
    executor = _McpExecutor([_hazard_json()])
    ctx = _Ctx(
        previous_results={
            "generate_distribution_layout": {},
            "generate_distribution_map": {"center": {"lat": 6.5, "lon": 3.4}},
        },
        mcp_executor=executor,
    )
    result = await fetch_geo_hazard(ctx)
    assert result.is_success
    assert executor.calls[0][1] == {"latitude": 6.5, "longitude": 3.4}


@pytest.mark.asyncio
async def test_partial_data_warnings_surface_in_progress_message():
    """A single candidate with a degraded (but non-fatal) data source still succeeds,
    and the specific warning text reaches the progress message the user sees."""
    executor = _McpExecutor(
        [
            _hazard_json(
                warnings=["Riverine RP1000 flood depth unavailable (data source unreachable)"]
            )
        ]
    )
    ctx = _Ctx(
        previous_results={
            "generate_distribution_layout": {
                "site_candidates": [{"rank": 1, "lat": 9.39, "lon": 9.31}]
            }
        },
        mcp_executor=executor,
    )
    result = await fetch_geo_hazard(ctx)
    assert result.is_success
    assert "Riverine RP1000 flood depth unavailable" in result.progress_message


@pytest.mark.asyncio
async def test_one_candidate_fails_others_succeed():
    """One candidate is a total loss (tool returns Error:), the rest succeed — the
    step should still succeed overall and name the skipped candidate specifically."""
    executor = _McpExecutor(
        [
            "Error: Geo hazard lookup failed: all data sources unreachable",
            _hazard_json(),
        ]
    )
    ctx = _Ctx(
        previous_results={
            "generate_distribution_layout": {
                "site_candidates": [
                    {"rank": 1, "lat": 9.39, "lon": 9.31},
                    {"rank": 2, "lat": 9.40, "lon": 9.32},
                ]
            }
        },
        mcp_executor=executor,
    )
    result = await fetch_geo_hazard(ctx)
    assert result.is_success
    assert len(result.data["geo_hazard_per_candidate"]) == 1
    assert "Rank 1" in result.progress_message
    assert "all data sources unreachable" in result.progress_message


@pytest.mark.asyncio
async def test_all_candidates_fail_names_specific_reasons():
    executor = _McpExecutor(
        [
            "Error: Geo hazard lookup failed: flood data unavailable, terrain data unavailable",
        ]
    )
    ctx = _Ctx(
        previous_results={
            "generate_distribution_layout": {
                "site_candidates": [{"rank": 1, "lat": 9.39, "lon": 9.31}]
            }
        },
        mcp_executor=executor,
    )
    result = await fetch_geo_hazard(ctx)
    assert not result.is_success
    assert "flood data unavailable" in result.error
