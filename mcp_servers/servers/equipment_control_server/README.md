# Equipment Control MCP Server

## Overview

The Equipment Control MCP Server provides secure, role-based access to production equipment control operations. It allows authorized users to perform critical actions like restarting inverters and communications chains at remote sites.

## Features

- **Role-Based Access Control**: Only users with `equipment.control` permission can execute commands
- **Supabase Integration**: User permissions are verified against Supabase user table
- **Grid-to-Site Mapping**: Automatically resolves site IDs from grid names via Supabase grids table
- **Audit Logging**: All equipment control actions are logged with user information
- **Action Flags**: Can be globally disabled via `EQUIPMENT_CONTROL_ACTIONS_ENABLED` environment variable

## Available Commands

### 1. `restart_inverter`

Restarts the inverter at a specific site.

**Required Parameters:**
- `grid` (string): Grid name to identify the site
- `user_email` (string): Email address of the user requesting the action

**Permissions Required:**
- User must have `equipment.control` permission in Supabase users table
- User must be Admin role (enforced by mcp_launcher)

**Example:**
```json
{
  "grid": "site-alpha",
  "user_email": "admin@company.com"
}
```

**API Call Generated:**
```json
{
  "portalId": "<site_id>",
  "deviceType": "vebus",
  "instance": "276",
  "topicStub": "SystemReset",
  "value": "1"
}
```

### 2. `restart_comms_chain`

Restarts the communications chain at a specific site.

**Required Parameters:**
- `grid` (string): Grid name to identify the site
- `user_email` (string): Email address of the user requesting the action

**Permissions Required:**
- User must have `equipment.control` permission in Supabase users table
- User must be Admin role (enforced by mcp_launcher)

**Example:**
```json
{
  "grid": "site-alpha",
  "user_email": "admin@company.com"
}
```

**API Call Generated:**
```json
{
  "portalId": "<site_id>",
  "deviceType": "system",
  "instance": "0",
  "topicStub": "Reboot",
  "value": "1"
}
```

## Configuration

### Environment Variables

Add the following to your `.env` file:

```bash
# Equipment Control Configuration
EQUIPMENT_CONTROL_URL=https://your-equipment-control-api.example.com/control
EQUIPMENT_CONTROL_AUTH_TOKEN=your-base64-encoded-auth-token
EQUIPMENT_CONTROL_ACTIONS_ENABLED=false  # Set to true to enable equipment control
```

### Authentication Token

The `EQUIPMENT_CONTROL_AUTH_TOKEN` should be a precomputed Basic auth token (Base64 encoded `username:password`).

To generate:
```bash
echo -n "username:password" | base64
```

### Supabase Setup

#### 1. Users Table

Ensure your Supabase `users` table has the following structure:

```sql
CREATE TABLE users (
  id UUID PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL,
  permissions TEXT[] DEFAULT ARRAY[]::TEXT[]
);
```

Add `equipment.control` permission to authorized users:

```sql
UPDATE users
SET permissions = array_append(permissions, 'equipment.control')
WHERE email = 'admin@company.com';
```

#### 2. Grids Table

Ensure your Supabase `grids` table maps grid names to site IDs:

```sql
CREATE TABLE grids (
  id UUID PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  site_id TEXT NOT NULL
);
```

Example data:
```sql
INSERT INTO grids (name, site_id) VALUES
  ('site-alpha', '123456'),
  ('site-beta', '789012');
```

## Security

### Multiple Layers of Protection

1. **Action Flags**: Global enable/disable via `EQUIPMENT_CONTROL_ACTIONS_ENABLED`
2. **Permission Check**: User must have `equipment.control` permission in Supabase
3. **Role Check**: Only Admin role can execute commands (enforced by mcp_launcher)
4. **API Authentication**: All API calls use Basic auth token
5. **Audit Trail**: All actions are logged with user and grid information

### Default Security Posture

By default, equipment control is **DISABLED** (`EQUIPMENT_CONTROL_ACTIONS_ENABLED=false`). This must be explicitly enabled.

## Usage Example

### Using with Claude Code

```python
# Claude Code will automatically handle authentication
await equipment_control.restart_inverter(
    grid="site-alpha",
    user_email="admin@company.com"
)
```

### Response Format

Success response:
```json
{
  "action": "restart_inverter",
  "grid": "site-alpha",
  "site_id": "123456",
  "status": "success",
  "user": "admin@company.com",
  "command": {
    "portalId": "123456",
    "deviceType": "vebus",
    "instance": "276",
    "topicStub": "SystemReset",
    "value": "1"
  },
  "response": {
    // API response from equipment control endpoint
  }
}
```

Error response:
```json
{
  "error": "Permission denied: User user@company.com does not have equipment control permissions"
}
```

## Testing

### Test Permission Check

```bash
# Add equipment.control permission to test user
python3 << EOF
from shared_code.database.connections import db_manager
import asyncio

async def test():
    await db_manager.initialize_supabase()
    result = db_manager.supabase_client.table("users").update({
        "permissions": ["equipment.control"]
    }).eq("email", "admin@company.com").execute()
    print(result.data)

asyncio.run(test())
EOF
```

### Test Grid Lookup

```bash
# Verify grid to site_id mapping
python3 << EOF
from shared_code.database.connections import db_manager
import asyncio

async def test():
    await db_manager.initialize_supabase()
    result = db_manager.supabase_client.table("grids").select("*").eq("name", "site-alpha").execute()
    print(result.data)

asyncio.run(test())
EOF
```

## Troubleshooting

### "Equipment control actions are currently disabled"

**Solution**: Set `EQUIPMENT_CONTROL_ACTIONS_ENABLED=true` in your `.env` file.

### "Permission denied: User does not have equipment control permissions"

**Solution**: Add `equipment.control` permission to the user in Supabase:
```sql
UPDATE users
SET permissions = array_append(permissions, 'equipment.control')
WHERE email = 'user@company.com';
```

### "Site ID not found for grid"

**Solution**: Ensure the grid exists in the `grids` table:
```sql
INSERT INTO grids (name, site_id) VALUES ('grid-name', 'site-id');
```

### "Equipment control API error (401)"

**Solution**: Check that `EQUIPMENT_CONTROL_AUTH_TOKEN` is correctly base64 encoded.

## Integration with Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "equipment-control": {
      "command": "python3",
      "args": [
        "/path/to/mcp-servers/servers/equipment_control_server/equipment_control_mcp_server.py"
      ],
      "env": {
        "PYTHONPATH": "/path/to/mcp-servers"
      }
    }
  }
}
```

## Logging

All equipment control operations are logged to `servers.log` with the following information:
- Action performed (restart_inverter or restart_comms_chain)
- User email
- Grid name
- Site ID
- Timestamp
- Result

Example log entry:
```
2025-10-17 12:34:56 - equipment-control-server - INFO - Inverter restart initiated by admin@company.com for grid site-alpha (site 123456)
```

## API Endpoint Requirements

The equipment control API endpoint should:
1. Accept POST requests with JSON body
2. Require Basic authentication
3. Accept commands in the format specified above
4. Return 200 on success with optional JSON response
5. Return appropriate error codes (401, 403, 500) on failure

## Support

For issues or questions:
1. Check the troubleshooting section above
2. Review server logs in `servers.log`
3. Verify all environment variables are set correctly
4. Ensure Supabase tables are properly configured
