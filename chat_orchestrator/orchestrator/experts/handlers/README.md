# Expert Step Handlers

Step handlers implement the individual steps of expert workflows. Each handler is registered with `@register_step("handler_name")` and called by the workflow executor.

## Editable Parameters

The parameter confirmation flow allows users to review and modify certain values before a workflow step executes. To make a parameter editable:

### 1. Add `editable_` prefix in state_updates

In your step handler, add parameters with the `editable_` prefix to `state_updates`:

```python
@register_step("my_step")
async def my_step(context: StepContext) -> StepResult:
    # ... do work ...

    return StepResult(
        data={...},
        state_updates={
            # Internal state (not shown to user)
            "step_completed": True,
            "internal_value": 42,

            # Editable parameters (shown in confirmation prompt)
            "editable_total_count": calculated_count,
            "editable_max_limit": 100,
        },
    )
```

### 2. Display to user

The confirmation prompt will show:
- **Total Count**: 42
- **Max Limit**: 100

The `editable_` prefix is stripped automatically for display, and snake_case is converted to Title Case.

### 3. Read overrides in downstream steps

In later steps that use these values, check for user overrides:

```python
@register_step("downstream_step")
async def downstream_step(context: StepContext) -> StepResult:
    # Check for user override, fall back to original value
    total_count = context.get_parameter_value("editable_total_count")
    if total_count is None:
        total_count = context.get_previous_result("my_step").get("calculated_count", 0)

    # Use the value...
```

### 4. Example: LPP Package Generator

The LPP workflow makes these parameters editable:

| Parameter | Set By | Used By |
|-----------|--------|---------|
| `editable_total_buildings` | generate_distribution_map | populate_lpp_cells |
| `editable_served_building_count` | generate_distribution_map | populate_lpp_cells |
| `editable_total_kwp` | generate_powerplant_design | populate_lpp_cells |
| `editable_total_kwh` | generate_powerplant_design | populate_lpp_cells |

### Notes

- Only `editable_` prefixed parameters in `packet_state` are shown for confirmation
- Non-prefixed state values are internal and hidden from users
- The confirmation flow is disabled for interactive packet types (see `INTERACTIVE_PACKET_TYPES` in workflow_executor.py)
- Enum parameters can show options by storing a list: `"editable_status": ["pending", "active", "closed"]`
