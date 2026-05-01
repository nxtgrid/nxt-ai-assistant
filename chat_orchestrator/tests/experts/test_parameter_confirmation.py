"""Tests for parameter confirmation flow.

Tests the interactive parameter confirmation feature including:
- Parameter schema parsing from Google Doc format
- Parameter resolution from multiple sources
- Confirmation prompt formatting
- User response parsing
"""

from orchestrator.experts.parameter_confirmation import (
    ConfirmationAction,
    format_confirmation_prompt,
    format_param_edit_prompt,
    format_value_change_confirmation,
    parse_confirmation_response,
)
from orchestrator.experts.parameter_resolver import (
    PacketParameterSchema,
    ParameterDefinition,
    ParameterResolver,
    ResolvedParameter,
    get_schema_from_packet_state,
    parse_parameters_from_section,
)
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import get_step_registry, register_step


class TestParameterDefinition:
    """Tests for ParameterDefinition dataclass."""

    def test_default_values(self):
        """Test default values for parameter definition."""
        param = ParameterDefinition(name="test_param")

        assert param.name == "test_param"
        assert param.param_type == "string"
        assert param.description == ""
        assert param.required is False
        assert param.default is None
        assert param.editable is True

    def test_all_values(self):
        """Test setting all parameter values."""
        param = ParameterDefinition(
            name="site_id",
            param_type="int",
            description="Site submission ID",
            required=True,
            default=None,
            editable=False,
        )

        assert param.name == "site_id"
        assert param.param_type == "int"
        assert param.description == "Site submission ID"
        assert param.required is True
        assert param.editable is False


class TestPacketParameterSchema:
    """Tests for PacketParameterSchema dataclass."""

    def test_default_values(self):
        """Test default values for packet schema."""
        schema = PacketParameterSchema(packet_type="test_packet")

        assert schema.packet_type == "test_packet"
        assert schema.parameters == []
        assert schema.description == ""

    def test_with_parameters(self):
        """Test schema with parameters."""
        params = [
            ParameterDefinition(name="param1", param_type="string"),
            ParameterDefinition(name="param2", param_type="int"),
        ]
        schema = PacketParameterSchema(
            packet_type="my_packet",
            parameters=params,
            description="Test packet description",
        )

        assert len(schema.parameters) == 2
        assert schema.parameters[0].name == "param1"
        assert schema.description == "Test packet description"


class TestParseParametersFromSection:
    """Tests for parsing parameters from Google Doc section text."""

    def test_parse_full_format(self):
        """Test parsing name: type - description format."""
        section = """
        site_name: string - Name of the site
        max_connections: int - Maximum number of connections
        """
        params = parse_parameters_from_section(section)

        assert len(params) == 2
        assert params[0].name == "site_name"
        assert params[0].param_type == "string"
        assert params[0].description == "Name of the site"
        assert params[1].name == "max_connections"
        assert params[1].param_type == "int"

    def test_parse_with_list_markers(self):
        """Test parsing with bullet points or numbers."""
        section = """
        - site_name: string - Name of the site
        - site_id: int - Site ID
        """
        params = parse_parameters_from_section(section)

        assert len(params) == 2
        assert params[0].name == "site_name"
        assert params[1].name == "site_id"

    def test_parse_required_marker(self):
        """Test parsing [required] marker in description."""
        section = """
        site_name: string - Name of the site [required]
        optional_field: string - Optional field
        """
        params = parse_parameters_from_section(section)

        assert len(params) == 2
        assert params[0].required is True
        assert params[0].description == "Name of the site"  # [required] stripped
        assert params[1].required is False

    def test_parse_simple_format(self):
        """Test parsing name - description format (no type)."""
        section = """
        site_name - Name of the site
        """
        params = parse_parameters_from_section(section)

        assert len(params) == 1
        assert params[0].name == "site_name"
        assert params[0].param_type == "string"  # default
        assert params[0].description == "Name of the site"

    def test_parse_name_only(self):
        """Test parsing just parameter names."""
        section = """
        site_name
        site_id
        """
        params = parse_parameters_from_section(section)

        assert len(params) == 2
        assert params[0].name == "site_name"
        assert params[1].name == "site_id"

    def test_parse_editable_flag(self):
        """Test that editable flag is passed through."""
        section = "site_name: string - Name"

        # Inputs should be editable
        params = parse_parameters_from_section(section, editable=True)
        assert params[0].editable is True

        # State/computed values should not be editable
        params = parse_parameters_from_section(section, editable=False)
        assert params[0].editable is False

    def test_parse_empty_section(self):
        """Test parsing empty section."""
        params = parse_parameters_from_section("")
        assert len(params) == 0

        params = parse_parameters_from_section(None)  # type: ignore
        assert len(params) == 0


class TestRegisterStepWithoutSchema:
    """Tests for @register_step decorator (schema-less, general approach)."""

    def setup_method(self):
        """Clear registry before each test."""
        get_step_registry().clear()

    def test_register_simple_handler(self):
        """Test registering a handler without step-level schema."""

        @register_step("simple_handler")
        async def simple_handler(context: StepContext) -> StepResult:
            return StepResult()

        registry = get_step_registry()
        assert registry.has_handler("simple_handler")
        # No step-level schema - parameters come from expert config
        assert not registry.has_schema("simple_handler")


class TestParameterResolver:
    """Tests for ParameterResolver."""

    def test_resolve_from_packet_inputs(self):
        """Test resolving parameter from packet inputs."""
        resolver = ParameterResolver()
        schema = PacketParameterSchema(
            packet_type="test",
            parameters=[
                ParameterDefinition(
                    name="site_name",
                    param_type="string",
                )
            ],
        )
        context = StepContext(
            packet_id="test-1",
            packet_type="test",
            packet_goal="Test goal",
            packet_inputs={"site_name": "ExampleSite"},
            packet_state={},
            current_step="test",
            steps_completed=[],
        )

        resolved = resolver.resolve_parameters(schema, context)

        assert len(resolved) == 1
        assert resolved[0].name == "site_name"
        assert resolved[0].current_value == "ExampleSite"
        assert resolved[0].source == "input"

    def test_resolve_from_packet_state(self):
        """Test resolving parameter from packet state."""
        resolver = ParameterResolver()
        schema = PacketParameterSchema(
            packet_type="test",
            parameters=[
                ParameterDefinition(
                    name="site_id",
                    param_type="int",
                )
            ],
        )
        context = StepContext(
            packet_id="test-1",
            packet_type="test",
            packet_goal="Test goal",
            packet_inputs={},
            packet_state={"site_id": 206},
            current_step="test",
            steps_completed=[],
        )

        resolved = resolver.resolve_parameters(schema, context)

        assert len(resolved) == 1
        assert resolved[0].current_value == 206
        assert resolved[0].source == "state"

    def test_resolve_from_default(self):
        """Test resolving parameter from default value."""
        resolver = ParameterResolver()
        schema = PacketParameterSchema(
            packet_type="test",
            parameters=[
                ParameterDefinition(
                    name="design_type",
                    param_type="string",
                    default="standard",
                )
            ],
        )
        context = StepContext(
            packet_id="test-1",
            packet_type="test",
            packet_goal="Test goal",
            packet_inputs={},
            packet_state={},
            current_step="test",
            steps_completed=[],
        )

        resolved = resolver.resolve_parameters(schema, context)

        assert len(resolved) == 1
        assert resolved[0].current_value == "standard"
        assert resolved[0].source == "default"

    def test_resolve_with_override(self):
        """Test that user overrides take priority."""
        resolver = ParameterResolver()
        schema = PacketParameterSchema(
            packet_type="test",
            parameters=[
                ParameterDefinition(
                    name="max_connections",
                    param_type="int",
                    default=24,
                )
            ],
        )
        context = StepContext(
            packet_id="test-1",
            packet_type="test",
            packet_goal="Test goal",
            packet_inputs={},
            packet_state={
                "max_connections": 24,  # Normal value
                "pending_param_overrides": {"max_connections": 50},  # User override
            },
            current_step="test",
            steps_completed=[],
        )

        resolved = resolver.resolve_parameters(schema, context)

        assert len(resolved) == 1
        assert resolved[0].current_value == 50
        assert resolved[0].source == "override"
        assert resolved[0].is_modified is True

    def test_resolve_unset_parameter(self):
        """Test resolving parameter that has no value anywhere."""
        resolver = ParameterResolver()
        schema = PacketParameterSchema(
            packet_type="test",
            parameters=[
                ParameterDefinition(
                    name="optional_field",
                    param_type="string",
                )
            ],
        )
        context = StepContext(
            packet_id="test-1",
            packet_type="test",
            packet_goal="Test goal",
            packet_inputs={},
            packet_state={},
            current_step="test",
            steps_completed=[],
        )

        resolved = resolver.resolve_parameters(schema, context)

        assert len(resolved) == 1
        assert resolved[0].current_value is None
        assert resolved[0].source == "unset"


class TestConfirmationPromptFormatting:
    """Tests for confirmation prompt formatting."""

    def test_format_basic_prompt(self):
        """Test basic prompt formatting."""
        params = [
            ResolvedParameter(
                name="site_id",
                current_value=206,
                source="state",
                param_type="int",
                description="Site submission ID",
                required=True,
                editable=False,
            ),
        ]

        prompt = format_confirmation_prompt(
            step_name="generate_design",
            step_index=1,
            total_steps=5,
            description="Generate design and BOM",
            parameters=params,
        )

        assert "Step 2/5" in prompt
        assert "Generate Design" in prompt
        assert "Site Id" in prompt  # snake_case formatted as Title Case
        assert "206" in prompt
        assert "[required]" in prompt
        assert "(read-only)" in prompt

    def test_format_with_modified_parameter(self):
        """Test prompt shows modified indicator."""
        params = [
            ResolvedParameter(
                name="max_connections",
                current_value=50,
                source="override",
                param_type="int",
                description="Max connections",
                required=False,
                editable=True,
                is_modified=True,
            ),
        ]

        prompt = format_confirmation_prompt(
            step_name="test_step",
            step_index=0,
            total_steps=3,
            description="Test step",
            parameters=params,
        )

        assert "*" in prompt  # Modified indicator

    def test_format_param_edit_prompt(self):
        """Test parameter edit prompt formatting."""
        param = ResolvedParameter(
            name="max_connections",
            current_value=24,
            source="default",
            param_type="int",
            description="Maximum number of connections",
            required=False,
            editable=True,
        )

        prompt = format_param_edit_prompt(param)

        # Name may be formatted as Title Case
        assert "Max Connections" in prompt or "max_connections" in prompt
        assert "24" in prompt
        assert "cancel" in prompt.lower()

    def test_format_value_change_confirmation(self):
        """Test value change confirmation message."""
        msg = format_value_change_confirmation(
            param_name="max_connections",
            old_value=24,
            new_value=50,
        )

        # Name may be formatted as Title Case
        assert "Max Connections" in msg or "max_connections" in msg
        assert "24" in msg
        assert "50" in msg


class TestConfirmationResponseParsing:
    """Tests for parsing user responses to confirmation prompts."""

    def test_parse_continue_commands(self):
        """Test parsing continue commands."""
        for cmd in ["c", "continue", "y", "yes", "ok"]:
            response = parse_confirmation_response(cmd, num_parameters=4)
            assert response.action == ConfirmationAction.CONTINUE

    def test_parse_auto_commands(self):
        """Test parsing auto commands."""
        for cmd in ["a", "auto", "all"]:
            response = parse_confirmation_response(cmd, num_parameters=4)
            assert response.action == ConfirmationAction.AUTO

    def test_parse_parameter_selection(self):
        """Test parsing parameter number selection."""
        response = parse_confirmation_response("2", num_parameters=4)
        assert response.action == ConfirmationAction.SELECT_PARAM
        assert response.param_index == 1  # 0-based

    def test_parse_invalid_parameter_number(self):
        """Test parsing invalid parameter number."""
        response = parse_confirmation_response("10", num_parameters=4)
        assert response.action == ConfirmationAction.INVALID
        assert "1-4" in response.error_message

    def test_parse_invalid_input(self):
        """Test parsing unrecognized input."""
        response = parse_confirmation_response("something random", num_parameters=4)
        assert response.action == ConfirmationAction.INVALID
        assert "something random" in response.error_message

    def test_parse_value_input_integer(self):
        """Test parsing integer value when editing parameter."""
        response = parse_confirmation_response(
            "50",
            num_parameters=4,
            is_editing_param=True,
            editing_param_type="int",
        )
        assert response.action == ConfirmationAction.SET_VALUE
        assert response.new_value == 50

    def test_parse_value_input_boolean(self):
        """Test parsing boolean value."""
        for value, expected in [("yes", True), ("no", False), ("true", True), ("0", False)]:
            response = parse_confirmation_response(
                value,
                num_parameters=4,
                is_editing_param=True,
                editing_param_type="bool",
            )
            assert response.action == ConfirmationAction.SET_VALUE
            assert response.new_value == expected

    def test_parse_value_input_cancel(self):
        """Test canceling value edit."""
        response = parse_confirmation_response(
            "cancel",
            num_parameters=4,
            is_editing_param=True,
            editing_param_type="int",
        )
        assert response.action == ConfirmationAction.CONTINUE

    def test_parse_invalid_type_conversion(self):
        """Test invalid type conversion."""
        response = parse_confirmation_response(
            "not-a-number",
            num_parameters=4,
            is_editing_param=True,
            editing_param_type="int",
        )
        assert response.action == ConfirmationAction.INVALID
        assert "not a valid integer" in response.error_message


class TestStepContextParameterMethods:
    """Tests for StepContext parameter-related methods."""

    def test_get_parameter_value_from_override(self):
        """Test getting parameter value from user override."""
        context = StepContext(
            packet_id="test-1",
            packet_type="test",
            packet_goal="Test goal",
            packet_inputs={"max_connections": 24},
            packet_state={"pending_param_overrides": {"max_connections": 50}},
            current_step="test",
            steps_completed=[],
        )

        value = context.get_parameter_value("max_connections")
        assert value == 50  # Override takes priority

    def test_get_parameter_value_from_inputs(self):
        """Test getting parameter value from packet inputs."""
        context = StepContext(
            packet_id="test-1",
            packet_type="test",
            packet_goal="Test goal",
            packet_inputs={"site_name": "ExampleSite"},
            packet_state={},
            current_step="test",
            steps_completed=[],
        )

        value = context.get_parameter_value("site_name")
        assert value == "ExampleSite"

    def test_get_parameter_value_with_default(self):
        """Test getting parameter value returns default when not found."""
        context = StepContext(
            packet_id="test-1",
            packet_type="test",
            packet_goal="Test goal",
            packet_inputs={},
            packet_state={},
            current_step="test",
            steps_completed=[],
        )

        value = context.get_parameter_value("missing_param", default="default_value")
        assert value == "default_value"

    def test_set_and_clear_parameter_override(self):
        """Test setting and clearing parameter overrides."""
        context = StepContext(
            packet_id="test-1",
            packet_type="test",
            packet_goal="Test goal",
            packet_inputs={},
            packet_state={},
            current_step="test",
            steps_completed=[],
        )

        # Set override
        context.set_parameter_override("max_connections", 50)
        assert context.get_parameter_value("max_connections") == 50

        # Clear overrides
        context.clear_parameter_overrides()
        assert context.get_parameter_value("max_connections") is None


class TestGetSchemaFromPacketState:
    """Tests for get_schema_from_packet_state - the general approach.

    Only shows editable configuration parameters (strings, integers, floats),
    not internal status flags, booleans, or complex objects.
    """

    def test_basic_state_to_schema(self):
        """Test converting packet state to parameter schema.

        Only editable_ prefixed parameters are included.
        Names are stored with the editable_ prefix (stripped at display time).
        """
        packet_state = {
            "editable_site_name": "ExampleSite",
            "editable_max_connections": 24,
            "editable_total_kwp": 45.5,
            # Not prefixed - excluded
            "design_generated": True,
        }
        schema = get_schema_from_packet_state(packet_state, "generate_design")

        assert schema.packet_type == "generate_design"
        # Only editable_ prefixed params are included
        assert len(schema.parameters) == 3

        # Check types are inferred correctly (name keeps editable_ prefix)
        param_map = {p.name: p for p in schema.parameters}
        assert param_map["editable_site_name"].param_type == "string"
        assert param_map["editable_max_connections"].param_type == "int"
        assert param_map["editable_total_kwp"].param_type == "float"

    def test_excludes_internal_keys(self):
        """Test that only editable_ prefixed keys are included."""
        packet_state = {
            "editable_site_name": "ExampleSite",
            # These should be excluded (no editable_ prefix)
            "pending_param_overrides": {"max": 50},
            "awaiting_param_confirmation": True,
            "auto_continue_enabled": False,
            "tool_calls": [],
            "error": "some error",
            "steps_completed": ["step1"],
        }
        schema = get_schema_from_packet_state(packet_state, "test_step")

        assert len(schema.parameters) == 1
        assert schema.parameters[0].name == "editable_site_name"

    def test_excludes_keys_by_prefix_suffix(self):
        """Test that only editable_ prefixed keys are included, regardless of other patterns."""
        packet_state = {
            "editable_site_name": "ExampleSite",
            # These should be excluded (no editable_ prefix)
            "awaiting_response": True,
            "is_valid": True,
            "has_error": False,
            "needs_review": True,
            "step_completed": True,
            "process_status": "done",
            "created_time": "2026-01-01",
            "site_id": 206,
        }
        schema = get_schema_from_packet_state(packet_state, "test_step")

        assert len(schema.parameters) == 1
        assert schema.parameters[0].name == "editable_site_name"

    def test_excludes_none_and_empty(self):
        """Test that None and empty collections are excluded even with editable_ prefix."""
        packet_state = {
            "editable_site_name": "ExampleSite",
            "editable_empty_list": [],
            "editable_empty_dict": {},
            "editable_none_value": None,
        }
        schema = get_schema_from_packet_state(packet_state, "test_step")

        assert len(schema.parameters) == 1
        assert schema.parameters[0].name == "editable_site_name"

    def test_excludes_complex_types(self):
        """Test that complex types (dict, list) are excluded even with editable_ prefix.

        Bool is allowed for editable_ params (e.g., toggleable settings).
        """
        packet_state = {
            "editable_simple_string": "value",
            "editable_simple_int": 42,
            "editable_complex_dict": {"nested": "data"},
            "editable_complex_list": [1, 2, 3],
            "editable_bool_flag": True,
        }
        schema = get_schema_from_packet_state(packet_state, "test_step")

        # dict and list are excluded; string, int, bool are included
        param_names = {p.name for p in schema.parameters}
        assert "editable_simple_string" in param_names
        assert "editable_simple_int" in param_names
        assert "editable_bool_flag" in param_names
        assert "editable_complex_dict" not in param_names
        assert "editable_complex_list" not in param_names
        assert len(schema.parameters) == 3

    def test_empty_state_returns_empty_schema(self):
        """Test that empty state returns schema with no parameters."""
        schema = get_schema_from_packet_state({}, "test_step")

        assert len(schema.parameters) == 0
        assert schema.packet_type == "test_step"

    def test_packet_inputs_included(self):
        """Test that only editable_ prefixed state params are included.

        packet_inputs are no longer auto-included; only editable_ state keys matter.
        """
        packet_state = {
            "internal_flag": True,
            "editable_site_name": "ExampleSite",
            "editable_max_connections": 24,
        }
        packet_inputs = {
            "site_name": "ExampleSite",
            "max_connections": 24,
        }
        schema = get_schema_from_packet_state(
            packet_state, "test_step", packet_inputs=packet_inputs
        )

        # Only editable_ prefixed state params are included
        assert len(schema.parameters) == 2
        param_names = {p.name for p in schema.parameters}
        assert "editable_site_name" in param_names
        assert "editable_max_connections" in param_names
