#!/usr/bin/env python3
"""
Unified MCP Server Launcher and List Service

This module provides a unified interface to launch, manage, and list all MCP servers
in the repository. It automatically discovers servers and provides a REST API
to enumerate available services.
"""

import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-launcher")


@dataclass
class MCPServerInfo:
    """Information about an MCP server"""

    name: str
    module_path: str
    description: str
    version: str
    capabilities: List[str]
    status: str
    tools: List[Dict[str, Any]]
    resources: List[Dict[str, Any]]
    process_id: Optional[int] = None
    port: Optional[int] = None
    startup_time: Optional[str] = None
    actions_enabled: bool = True
    user_context: Optional[Dict[str, Any]] = None
    available_tools: Optional[List[Dict[str, Any]]] = None  # Role-filtered tools


class MCPServerDiscovery:
    """Discovers and catalogs MCP servers in the repository"""

    def __init__(self, servers_dir: Path = None):
        self.servers_dir = servers_dir or (project_root / "servers")
        self.servers: Dict[str, MCPServerInfo] = {}

    def discover_servers(self) -> Dict[str, MCPServerInfo]:
        """
        Discover all MCP servers in the servers directory using pattern-based detection.

        Coding Pattern for MCP Servers:
        - Located in: servers/{server_name}/
        - Main file: {server_name}_mcp_server.py OR *mcp_server.py
        - Must contain: server = Server("name")
        - Must contain: @server.list_tools() decorator
        """
        logger.info(f"Discovering MCP servers in {self.servers_dir}")

        # Scan all server directories for MCP servers
        for server_dir in self.servers_dir.iterdir():
            if not server_dir.is_dir():
                continue

            logger.debug(f"Scanning directory: {server_dir.name}")

            # Look for MCP server files using pattern matching
            mcp_server_files = []

            # Pattern 1: {server_name}_mcp_server.py
            expected_file = server_dir / f"{server_dir.name}_mcp_server.py"
            if expected_file.exists():
                mcp_server_files.append(expected_file)

            # Pattern 2: Any *mcp_server.py file
            for py_file in server_dir.glob("*mcp_server.py"):
                if py_file not in mcp_server_files:
                    mcp_server_files.append(py_file)

            # Analyze each potential MCP server file
            for py_file in mcp_server_files:
                try:
                    if self._is_mcp_server(py_file):
                        server_name = server_dir.name
                        server_info = self._analyze_server(server_name, py_file)
                        self.servers[server_name] = server_info
                        logger.info(f"Discovered MCP server: {server_name} ({py_file.name})")
                        break  # Only register the first valid MCP server per directory
                except Exception as e:
                    logger.debug(f"Failed to analyze {py_file}: {e}")

        return self.servers

    def _is_mcp_server(self, file_path: Path) -> bool:
        """
        Check if a Python file is a valid MCP server based on coding patterns.

        Required patterns:
        1. Contains: from mcp.server import Server
        2. Contains: server = Server("name") or app = Server("name")
        3. Contains: @server.list_tools() or @app.list_tools()
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Check for required imports
            has_mcp_import = any(
                pattern in content
                for pattern in [
                    "from mcp.server import Server",
                    "import mcp.server",
                    "from mcp import server",
                ]
            )

            if not has_mcp_import:
                return False

            # Check for server instantiation
            has_server_instance = any(
                pattern in content for pattern in ["server = Server(", "app = Server("]
            )

            if not has_server_instance:
                return False

            # Check for MCP decorators
            has_mcp_decorators = any(
                pattern in content
                for pattern in [
                    "@server.list_tools(",
                    "@app.list_tools(",
                    "@server.call_tool(",
                    "@app.call_tool(",
                ]
            )

            return has_mcp_decorators

        except Exception as e:
            logger.debug(f"Error checking if {file_path} is MCP server: {e}")
            return False

    def _analyze_server(self, server_name: str, file_path: Path) -> MCPServerInfo:
        """Analyze a server file to extract metadata"""

        # Read the file to extract information
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Extract description from docstring
        description = self._extract_description(content)

        # Extract version if available
        version = self._extract_version(content)

        # Try to import and get tools/resources if possible
        tools, resources, capabilities = self._extract_capabilities(file_path, content)

        return MCPServerInfo(
            name=server_name,
            module_path=str(file_path.relative_to(project_root)),
            description=description,
            version=version,
            capabilities=capabilities,
            status="discovered",
            tools=tools,
            resources=resources,
        )

    def _extract_description(self, content: str) -> str:
        """Extract description from file docstring"""
        lines = content.split("\n")
        in_docstring = False
        description_lines = []

        for line in lines:
            line = line.strip()
            if line.startswith('"""') or line.startswith("'''"):
                if in_docstring:
                    break
                in_docstring = True
                # Get text after opening quotes
                desc_part = line[3:].strip()
                if desc_part and not desc_part.endswith('"""') and not desc_part.endswith("'''"):
                    description_lines.append(desc_part)
                continue
            elif in_docstring:
                if line and not line.startswith("#"):
                    description_lines.append(line)

        return (
            " ".join(description_lines)[:200]
            if description_lines
            else f"MCP Server: {Path(content).stem}"
        )

    def _extract_version(self, content: str) -> str:
        """Extract version from file content"""
        # Look for version patterns
        import re

        version_patterns = [
            r'__version__\s*=\s*["\']([^"\']+)["\']',
            r'version\s*=\s*["\']([^"\']+)["\']',
            r'server_version="([^"]+)"',
            r"server_version=\'([^\']+)\'",
        ]

        for pattern in version_patterns:
            match = re.search(pattern, content)
            if match:
                return match.group(1)

        return "1.0.0"

    def _extract_capabilities(self, file_path: Path, content: str) -> tuple:
        """Try to extract tools and capabilities from the server"""
        tools = []
        resources = []
        capabilities = []

        try:
            # Basic capability detection
            if "list_tools" in content:
                capabilities.append("tools")
            if "list_resources" in content:
                capabilities.append("resources")
            if "call_tool" in content:
                capabilities.append("tool_execution")
            if "read_resource" in content:
                capabilities.append("resource_reading")

            # Enhanced tool extraction with proper descriptions

            # Extract tools with their descriptions using more sophisticated patterns
            tools = self._extract_tools_with_descriptions(content, file_path)

            # Extract resources if any
            resources = self._extract_resources_with_descriptions(content, file_path)

        except Exception as e:
            logger.debug(f"Could not analyze {file_path}: {e}")
            # Fallback to basic text analysis
            if "mcp" in content.lower():
                capabilities.append("mcp_compatible")

        return tools, resources, capabilities

    def _extract_tools_with_descriptions(
        self, content: str, file_path: Path
    ) -> List[Dict[str, Any]]:
        """Extract tools with their descriptions and input schemas from code"""
        import re

        tools = []

        # Find complete Tool() definitions
        tools = self._extract_complete_tool_definitions(content, file_path)

        # Fallback: Extract just names if no complete Tool patterns found
        if not tools:
            name_patterns = [r'name\s*=\s*["\']([^"\']+)["\']', r'"name"\s*:\s*["\']([^"\']+)["\']']

            found_names = set()
            for pattern in name_patterns:
                for match in re.finditer(pattern, content):
                    name = match.group(1).strip()
                    if name and name not in found_names:
                        found_names.add(name)
                        tools.append(
                            {
                                "name": name,
                                "description": f"Tool from {file_path.name}",
                                "inputSchema": {},
                            }
                        )

        return tools

    def _extract_complete_tool_definitions(
        self, content: str, file_path: Path
    ) -> List[Dict[str, Any]]:
        """Extract complete Tool definitions including schemas"""

        tools: list[Dict[str, Any]] = []

        # More sophisticated pattern to match Tool() with balanced parentheses/braces
        # Split content to find Tool definitions
        tool_matches = self._find_tool_definitions(content)

        for tool_text in tool_matches:
            tool_info = self._parse_tool_arguments(tool_text, file_path)

            if tool_info and tool_info.get("name"):
                # Skip if we already have this tool
                if tool_info["name"] not in [t.get("name") for t in tools]:
                    tools.append(tool_info)

        return tools

    def _find_tool_definitions(self, content: str) -> List[str]:
        """Find complete Tool() definitions with proper bracket matching"""
        import re

        tool_definitions = []

        # Find all potential Tool starts
        tool_starts = []
        for match in re.finditer(r"(?:types\.)?Tool\s*\(", content):
            tool_starts.append(match.start())

        for start_pos in tool_starts:
            # Find the matching closing parenthesis
            paren_count = 0
            brace_count = 0
            bracket_count = 0
            in_string = False
            string_char = None
            pos = start_pos

            while pos < len(content):
                char = content[pos]

                # Handle string literals
                if char in ['"', "'"] and (pos == 0 or content[pos - 1] != "\\"):
                    if not in_string:
                        in_string = True
                        string_char = char
                    elif char == string_char:
                        in_string = False
                        string_char = None

                elif not in_string:
                    if char == "(":
                        paren_count += 1
                    elif char == ")":
                        paren_count -= 1
                        if paren_count == 0:
                            # Found the matching closing parenthesis
                            tool_def = content[start_pos : pos + 1]
                            # Extract just the arguments
                            args_start = tool_def.find("(") + 1
                            args_end = tool_def.rfind(")")
                            if args_start > 0 and args_end > args_start:
                                tool_definitions.append(tool_def[args_start:args_end])
                            break
                    elif char == "{":
                        brace_count += 1
                    elif char == "}":
                        brace_count -= 1
                    elif char == "[":
                        bracket_count += 1
                    elif char == "]":
                        bracket_count -= 1

                pos += 1

        return tool_definitions

    def _parse_tool_arguments(self, args_text: str, file_path: Path) -> Dict[str, Any]:
        """Parse Tool constructor arguments to extract name, description, and inputSchema"""
        import re

        tool_info = {"name": "", "description": f"Tool from {file_path.name}", "inputSchema": {}}

        try:
            # Extract name
            name_match = re.search(r'name\s*=\s*["\']([^"\']+)["\']', args_text)
            if name_match:
                tool_info["name"] = name_match.group(1).strip()

            # Extract description
            desc_match = re.search(r'description\s*=\s*["\']([^"\']*?)["\']', args_text, re.DOTALL)
            if desc_match:
                desc = desc_match.group(1).strip()
                if desc:
                    tool_info["description"] = desc

            # Extract inputSchema - find the balanced dictionary
            schema = self._extract_input_schema(args_text)
            if schema:
                tool_info["inputSchema"] = schema

        except Exception as e:
            logger.debug(f"Error parsing tool arguments: {e}")

        return tool_info

    def _extract_input_schema(self, args_text: str) -> Dict[str, Any]:
        """Extract inputSchema using bracket matching"""
        import re

        # Find inputSchema= followed by balanced braces
        match = re.search(r"inputSchema\s*=\s*(\{)", args_text, re.DOTALL)
        if not match:
            return {}

        start_pos = match.start(1)
        brace_count = 0
        in_string = False
        string_char = None
        pos = start_pos

        while pos < len(args_text):
            char = args_text[pos]

            # Handle string literals
            if char in ['"', "'"] and (pos == 0 or args_text[pos - 1] != "\\"):
                if not in_string:
                    in_string = True
                    string_char = char
                elif char == string_char:
                    in_string = False
                    string_char = None

            elif not in_string:
                if char == "{":
                    brace_count += 1
                elif char == "}":
                    brace_count -= 1
                    if brace_count == 0:
                        # Found the matching closing brace
                        schema_text = args_text[start_pos : pos + 1]
                        return self._parse_schema_dict(schema_text)

            pos += 1

        return {}

    def _parse_schema_dict(self, schema_text: str) -> Dict[str, Any]:
        """Parse inputSchema dictionary from string representation"""
        import ast
        import re

        try:
            # Clean up the schema text for parsing
            # Handle multiline dictionaries and nested structures
            cleaned = re.sub(r"#.*$", "", schema_text, flags=re.MULTILINE)  # Remove comments
            cleaned = re.sub(r"\s+", " ", cleaned)  # Normalize whitespace

            # Try to evaluate as Python literal
            schema = ast.literal_eval(cleaned)
            return schema if isinstance(schema, dict) else {}

        except (ValueError, SyntaxError) as e:
            logger.debug(f"Failed to parse schema with ast.literal_eval: {e}")

            # Fallback: Try to extract basic structure manually
            try:
                return self._parse_schema_manually(schema_text)
            except Exception as e2:
                logger.debug(f"Manual schema parsing also failed: {e2}")
                return {}

    def _parse_schema_manually(self, schema_text: str) -> Dict[str, Any]:
        """Manual parsing of schema when ast.literal_eval fails"""
        import re

        schema = {}

        # Extract type
        type_match = re.search(r'"type"\s*:\s*"([^"]+)"', schema_text)
        if type_match:
            schema["type"] = type_match.group(1)

        # Extract properties section
        props_match = re.search(
            r'"properties"\s*:\s*\{([^}]+(?:\{[^}]*\}[^}]*)*)\}', schema_text, re.DOTALL
        )
        if props_match:
            properties = {}
            props_text = props_match.group(1)

            # Extract individual property definitions
            prop_pattern = r'"([^"]+)"\s*:\s*\{([^}]+)\}'
            for prop_match in re.finditer(prop_pattern, props_text):
                prop_name = prop_match.group(1)
                prop_def = prop_match.group(2)

                prop_info = {}
                # Extract type
                prop_type_match = re.search(r'"type"\s*:\s*"([^"]+)"', prop_def)
                if prop_type_match:
                    prop_info["type"] = prop_type_match.group(1)

                # Extract description
                prop_desc_match = re.search(r'"description"\s*:\s*"([^"]*)"', prop_def)
                if prop_desc_match:
                    prop_info["description"] = prop_desc_match.group(1)

                properties[prop_name] = prop_info

            if properties:
                schema["properties"] = properties

        # Extract required fields
        required_match = re.search(r'"required"\s*:\s*\[([^\]]+)\]', schema_text)
        if required_match:
            required_text = required_match.group(1)
            required_fields = re.findall(r'"([^"]+)"', required_text)
            if required_fields:
                schema["required"] = required_fields

        return schema

    def _extract_resources_with_descriptions(
        self, content: str, file_path: Path
    ) -> List[Dict[str, str]]:
        """Extract resources with their descriptions from code"""
        import re

        resources = []

        # Pattern to match Resource() definitions
        resource_pattern = r'(?:types\.)?Resource\s*\(\s*(?:[^)]*?)uri\s*=\s*["\']([^"\']+)["\'](?:[^)]*?)name\s*=\s*["\']([^"\']*?)["\'](?:[^)]*?)description\s*=\s*["\']([^"\']*?)["\']'

        for match in re.finditer(resource_pattern, content, re.DOTALL):
            resource_uri = match.group(1).strip()
            resource_name = match.group(2).strip()
            resource_description = match.group(3).strip()

            resources.append(
                {
                    "uri": resource_uri,
                    "name": resource_name if resource_name else resource_uri,
                    "description": (
                        resource_description
                        if resource_description
                        else f"Resource from {file_path.name}"
                    ),
                }
            )

        return resources


class MCPServerManager:
    """Manages running MCP servers"""

    def __init__(self):
        self.discovery = MCPServerDiscovery()
        self.running_servers: Dict[str, subprocess.Popen] = {}

    def get_server_list(self, user_id: str = None, actions_enabled: bool = True) -> Dict[str, Any]:
        """Get list of all available MCP servers with user context"""
        servers = self.discovery.discover_servers()

        # Apply user context if provided
        if user_id:
            servers = self._apply_user_context(servers, user_id, actions_enabled)

        # Show action flags for each server
        action_flags_summary = {}
        try:
            from shared_code.config.action_flags import ActionFlags

            action_flags_summary = ActionFlags.get_all_action_flags()
        except Exception as e:
            logger.warning(f"Could not get action flags: {e}")

        return {
            "total_servers": len(servers),
            "discovery_time": datetime.now().isoformat(),
            "global_actions_enabled": actions_enabled,  # Original global flag (deprecated)
            "server_action_flags": action_flags_summary,  # New per-server flags
            "user_context": {"user_id": user_id} if user_id else None,
            "servers": {name: asdict(info) for name, info in servers.items()},
        }

    def _apply_user_context(
        self, servers: Dict[str, MCPServerInfo], user_id: str, actions_enabled: bool
    ) -> Dict[str, MCPServerInfo]:
        """Apply user context and role-based filtering to servers"""
        try:
            # Import here to avoid circular imports
            import sys

            sys.path.insert(0, str(project_root))
            from shared_code.config.action_flags import ActionFlags

            # This would be async in real implementation
            # For now, we'll create a mock context
            user_context = self._get_mock_user_context(user_id, actions_enabled)

            for server_name, server_info in servers.items():
                # Check server-specific action flag instead of global flag
                server_actions_enabled = ActionFlags.is_actions_enabled(server_name)
                server_info.actions_enabled = server_actions_enabled
                server_info.user_context = user_context.to_dict() if user_context else None

                # Filter tools based on user permissions and server-specific actions_enabled
                filtered_tools = self._filter_tools_by_permissions(
                    server_info.tools, server_name, user_context, server_actions_enabled
                )
                server_info.available_tools = filtered_tools

        except Exception as e:
            logger.warning(f"Could not apply user context: {e}")

        return servers

    def _get_mock_user_context(self, user_id: str, actions_enabled: bool):
        """Get mock user context for testing"""
        try:
            from shared_code.auth.user_context import UserContext, UserRole

            # Mock user contexts for demonstration
            user_configs = {
                "admin": {"role": UserRole.ADMIN, "permissions": {"*"}},
                "manager": {
                    "role": UserRole.MANAGER,
                    "permissions": {
                        "jira.read",
                        "jira.write",
                        "meters.read",
                        "grafana.read",
                    },
                },
                "analyst": {
                    "role": UserRole.ANALYST,
                    "permissions": {"jira.read", "meters.read", "grafana.read"},
                },
                "viewer": {
                    "role": UserRole.VIEWER,
                    "permissions": {"jira.read", "meters.read", "grafana.read"},
                },
            }

            config = user_configs.get(
                user_id, {"role": UserRole.VIEWER, "permissions": {"jira.read"}}
            )

            role = config["role"]
            permissions = config["permissions"]

            # Type assertions for mypy
            assert isinstance(role, UserRole)
            assert isinstance(permissions, set)

            return UserContext(
                user_id=user_id,
                username=f"{user_id}@company.com",
                role=role,
                permissions=permissions,
                grid_access=["grid1"] if user_id != "admin" else ["*"],
                actions_enabled=actions_enabled,
            )
        except Exception as e:
            logger.warning(f"Could not create user context: {e}")
            return None

    def _filter_tools_by_permissions(
        self, tools: List[Dict[str, Any]], server_name: str, user_context, actions_enabled: bool
    ) -> List[Dict[str, Any]]:
        """Filter tools based on user permissions and action flags"""
        if not user_context:
            return tools

        filtered_tools = []

        for tool in tools:
            tool_name = tool.get("name", "")

            # Check if tool is read or write operation
            is_write_operation = self._is_write_operation(tool_name, server_name)

            # Apply action filtering
            if is_write_operation and not actions_enabled:
                continue  # Skip write operations when actions are disabled

            # Apply role-based filtering
            if self._user_can_access_tool(tool_name, server_name, user_context):
                # Mark tool with action type for reference
                tool_copy = tool.copy()
                tool_copy["action_type"] = "write" if is_write_operation else "read"
                tool_copy["requires_actions_enabled"] = is_write_operation
                filtered_tools.append(tool_copy)

        return filtered_tools

    def _is_write_operation(self, tool_name: str, server_name: str) -> bool:
        """Determine if a tool performs write operations"""
        write_keywords = [
            "insert",
            "update",
            "delete",
            "create",
            "send",
            "generate",
            "configure",
            "set",
        ]
        tool_lower = tool_name.lower()

        # Check for explicit write operations first
        if any(keyword in tool_lower for keyword in write_keywords):
            return True

        # Special cases by server
        server_write_tools = {
            "jira_server": ["jira_configure"],
            "equipment_control_server": ["restart_inverter", "restart_comms_chain"],
        }

        return tool_name in server_write_tools.get(server_name, [])

    def _user_can_access_tool(self, tool_name: str, server_name: str, user_context) -> bool:
        """Check if user can access a specific tool"""
        try:
            from shared_code.auth.user_context import UserRole

            # Admin can access everything
            if user_context.role == UserRole.ADMIN:
                return True

            # Server-specific role restrictions
            server_role_restrictions = {
                "jira_server": {
                    "jira_configure": [UserRole.ADMIN, UserRole.MANAGER],
                    "jira_search_issues": [
                        UserRole.ADMIN,
                        UserRole.MANAGER,
                        UserRole.ANALYST,
                        UserRole.VIEWER,
                    ],
                    "jira_get_issue": [
                        UserRole.ADMIN,
                        UserRole.MANAGER,
                        UserRole.ANALYST,
                        UserRole.VIEWER,
                    ],
                    "jira_analyze_comments": [UserRole.ADMIN, UserRole.MANAGER, UserRole.ANALYST],
                },
                "equipment_control_server": {
                    "restart_inverter": [UserRole.ADMIN],
                    "restart_comms_chain": [UserRole.ADMIN],
                },
            }

            # Check server-specific restrictions
            if server_name in server_role_restrictions:
                restrictions = server_role_restrictions[server_name]
                tool_roles = restrictions.get(
                    tool_name,
                    restrictions.get(
                        "default",
                        [UserRole.ADMIN, UserRole.MANAGER, UserRole.ANALYST, UserRole.VIEWER],
                    ),
                )
                return bool(user_context.role in tool_roles)

            # Default: allow if user has permission for the server
            server_permission = f"{server_name.replace('_server', '')}.read"
            return bool(
                user_context.has_permission(server_permission) or user_context.has_permission("*")
            )

        except Exception as e:
            logger.warning(f"Error checking tool access: {e}")
            return True  # Default to allowing access

    def get_server_info(self, server_name: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a specific server"""
        servers = self.discovery.discover_servers()
        if server_name in servers:
            return asdict(servers[server_name])
        return None

    def start_server(self, server_name: str) -> Dict[str, Any]:
        """Start a specific MCP server"""
        servers = self.discovery.discover_servers()

        if server_name not in servers:
            return {"error": f"Server {server_name} not found"}

        if server_name in self.running_servers:
            return {"error": f"Server {server_name} already running"}

        server_info = servers[server_name]

        try:
            # Start the server process
            cmd = [sys.executable, server_info.module_path]
            # Ensure child process can import project packages
            env = os.environ.copy()
            existing_py_path = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{str(project_root)}" + (
                f":{existing_py_path}" if existing_py_path else ""
            )
            process = subprocess.Popen(
                cmd, cwd=project_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            self.running_servers[server_name] = process
            server_info.process_id = process.pid
            server_info.startup_time = datetime.now().isoformat()
            server_info.status = "running"

            return {
                "status": "started",
                "server_name": server_name,
                "process_id": process.pid,
                "startup_time": server_info.startup_time,
            }

        except Exception as e:
            return {"error": f"Failed to start server {server_name}: {e}"}

    def stop_server(self, server_name: str) -> Dict[str, Any]:
        """Stop a specific MCP server"""
        if server_name not in self.running_servers:
            return {"error": f"Server {server_name} is not running"}

        try:
            process = self.running_servers[server_name]
            process.terminate()
            process.wait(timeout=10)

            del self.running_servers[server_name]

            return {"status": "stopped", "server_name": server_name}

        except Exception as e:
            return {"error": f"Failed to stop server {server_name}: {e}"}

    def get_running_servers(self) -> Dict[str, Any]:
        """Get list of currently running servers"""
        running = {}

        for name, process in self.running_servers.items():
            poll_result = process.poll()
            status = "running" if poll_result is None else "stopped"

            running[name] = {
                "process_id": process.pid,
                "status": status,
                "return_code": poll_result,
            }

        return {
            "running_count": len([s for s in running.values() if s["status"] == "running"]),
            "servers": running,
        }


# FastAPI integration for REST API
try:
    import uvicorn
    from fastapi import FastAPI, HTTPException

    # Create FastAPI app for list service
    app = FastAPI(
        title="MCP Server List Service",
        description="REST API to discover and manage MCP servers",
        version="1.0.0",
    )

    manager = MCPServerManager()

    @app.get("/")
    async def root():
        """Root endpoint with service information"""
        return {
            "service": "MCP Server List Service",
            "version": "1.0.0",
            "description": "REST API to discover and manage MCP servers",
            "endpoints": {
                "list_servers": "/servers",
                "server_info": "/servers/{server_name}",
                "start_server": "/servers/{server_name}/start",
                "stop_server": "/servers/{server_name}/stop",
                "running_servers": "/running",
            },
        }

    @app.get("/servers")
    async def list_servers():
        """List all available MCP servers"""
        return manager.get_server_list()

    @app.get("/servers/{server_name}")
    async def get_server_info(server_name: str):
        """Get detailed information about a specific server"""
        info = manager.get_server_info(server_name)
        if not info:
            raise HTTPException(status_code=404, detail=f"Server {server_name} not found")
        return info

    @app.post("/servers/{server_name}/start")
    async def start_server(server_name: str):
        """Start a specific MCP server"""
        result = manager.start_server(server_name)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.post("/servers/{server_name}/stop")
    async def stop_server(server_name: str):
        """Stop a specific MCP server"""
        result = manager.stop_server(server_name)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result

    @app.get("/running")
    async def get_running_servers():
        """Get list of currently running servers"""
        return manager.get_running_servers()

    FASTAPI_AVAILABLE = True

except ImportError:
    logger.warning("FastAPI not available. REST API features disabled.")
    FASTAPI_AVAILABLE = False
    app = None


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="MCP Server Launcher and List Service")
    parser.add_argument("--list", action="store_true", help="List all available servers")
    parser.add_argument("--info", type=str, help="Get info about a specific server")
    parser.add_argument("--start", type=str, help="Start a specific server")
    parser.add_argument("--stop", type=str, help="Stop a specific server")
    parser.add_argument("--running", action="store_true", help="List running servers")
    parser.add_argument("--api", action="store_true", help="Start REST API service")
    parser.add_argument("--port", type=int, default=8000, help="Port for REST API (default: 8000)")
    parser.add_argument(
        "--host", type=str, default="localhost", help="Host for REST API (default: localhost)"
    )

    # User context and action control
    parser.add_argument("--user-id", type=str, help="User ID for role-based filtering")
    parser.add_argument(
        "--actions-enabled",
        action="store_true",
        default=True,
        help="Enable write/action operations (default: True)",
    )
    parser.add_argument(
        "--read-only", action="store_true", help="Disable all write/action operations"
    )

    args = parser.parse_args()

    # Handle read-only flag
    actions_enabled = args.actions_enabled and not args.read_only

    manager = MCPServerManager()

    if args.list:
        result = manager.get_server_list(user_id=args.user_id, actions_enabled=actions_enabled)
        print(json.dumps(result, indent=2))

    elif args.info:
        result = manager.get_server_info(args.info)
        if result:
            print(json.dumps(result, indent=2))
        else:
            print(f"Server {args.info} not found")
            sys.exit(1)

    elif args.start:
        result = manager.start_server(args.start)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)

    elif args.stop:
        result = manager.stop_server(args.stop)
        print(json.dumps(result, indent=2))
        if "error" in result:
            sys.exit(1)

    elif args.running:
        result = manager.get_running_servers()
        print(json.dumps(result, indent=2))

    elif args.api:
        if not FASTAPI_AVAILABLE:
            print("Error: FastAPI not available. Install with: pip install fastapi uvicorn")
            sys.exit(1)

        print(f"🚀 Starting MCP Server List Service on {args.host}:{args.port}")
        print(f"📖 API documentation available at: http://{args.host}:{args.port}/docs")

        uvicorn.run(app, host=args.host, port=args.port)

    else:
        # Default: show available servers
        result = manager.get_server_list()
        print("MCP Server Discovery Results:")
        print("=" * 40)
        for name, info in result["servers"].items():
            print(f"\n📦 {name}")
            print(f"   📝 {info['description']}")
            print(f"   📁 {info['module_path']}")
            print(f"   🏷️  v{info['version']}")
            print(f"   ⚡ {', '.join(info['capabilities'])}")
            if info["tools"]:
                print(f"   🔧 Tools: {', '.join([t['name'] for t in info['tools']])}")


if __name__ == "__main__":
    main()
