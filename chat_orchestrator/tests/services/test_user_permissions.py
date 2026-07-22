from orchestrator.models.schemas import UserContext
from orchestrator.services.user_permissions import UserPermissionsService

STAFF = UserContext(user_id="1", user_email="staff@example.com", is_staff=True)


def _tool(name, **extra):
    return {"name": name, "description": "d", "inputSchema": {"type": "object"}, **extra}


class TestInternalOnlyFilter:
    def test_internal_only_tool_excluded_from_llm_list(self):
        service = UserPermissionsService()
        tools_by_server = {
            "jira": [_tool("get_fields", internal_only=True), _tool("get_transitions")]
        }
        result = service._filter_and_convert_tools(tools_by_server, STAFF)
        names = [t["name"] for t in result]
        assert names == ["jira_get_transitions"]

    def test_tool_without_internal_only_key_is_kept(self):
        service = UserPermissionsService()
        tools_by_server = {"jira": [_tool("get_transitions")]}
        result = service._filter_and_convert_tools(tools_by_server, STAFF)
        assert [t["name"] for t in result] == ["jira_get_transitions"]

    def test_internal_only_and_persistent_only_both_excluded(self):
        service = UserPermissionsService()
        tools_by_server = {
            "jira": [
                _tool("get_fields", internal_only=True),
                _tool("background_task", persistent_only=True),
                _tool("get_transitions"),
            ]
        }
        result = service._filter_and_convert_tools(tools_by_server, STAFF)
        assert [t["name"] for t in result] == ["jira_get_transitions"]
