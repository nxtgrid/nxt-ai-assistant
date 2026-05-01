# Equipment Control Server Setup Guide

## Quick Start

### 1. Configure Environment Variables

Add to your `.env` file:

```bash
# Equipment Control Configuration
EQUIPMENT_CONTROL_URL=https://your-api-endpoint.example.com/control
EQUIPMENT_CONTROL_AUTH_TOKEN=base64_encoded_username_password
EQUIPMENT_CONTROL_ACTIONS_ENABLED=false  # Set to true to enable

# Required for permission checks
CHAT_DB_URL=https://your-project.supabase.co
CHAT_DB_SERVICE_KEY=your-chat-db-service-key
```

### 2. Generate Auth Token

```bash
echo -n "username:password" | base64
```

### 3. Setup Supabase Tables

#### Users Table (for permissions)

```sql
-- Add equipment.control permission to authorized users
UPDATE users
SET permissions = array_append(permissions, 'equipment.control')
WHERE email = 'admin@company.com';
```

#### Grids Table (for site mapping)

```sql
-- Ensure grids table has site_id column
ALTER TABLE grids ADD COLUMN IF NOT EXISTS site_id TEXT;

-- Add site_id mappings
UPDATE grids SET site_id = '123456' WHERE name = 'site-alpha';
UPDATE grids SET site_id = '789012' WHERE name = 'site-beta';
```

### 4. Enable Equipment Control

```bash
# In .env file
EQUIPMENT_CONTROL_ACTIONS_ENABLED=true
```

### 5. Test the Server

```bash
# Run discovery
python3 mcp_launcher.py --info equipment_control_server

# Expected output should show:
# - restart_inverter tool
# - restart_comms_chain tool
```

## Security Checklist

- [ ] Auth token is properly base64 encoded
- [ ] EQUIPMENT_CONTROL_ACTIONS_ENABLED is set to false by default
- [ ] Only admin users have equipment.control permission in Supabase
- [ ] All grids have site_id mappings in Supabase
- [ ] API endpoint uses HTTPS
- [ ] Server logs are monitored for equipment control actions

## Usage

Equipment control commands require:
1. Valid user_email with equipment.control permission
2. Valid grid name that exists in Supabase grids table
3. EQUIPMENT_CONTROL_ACTIONS_ENABLED=true

Example:
```json
{
  "grid": "site-alpha",
  "user_email": "admin@company.com"
}
```

## Troubleshooting

### Actions Disabled
- Check: `EQUIPMENT_CONTROL_ACTIONS_ENABLED=true` in .env

### Permission Denied
- Check: User has `equipment.control` in permissions array
- Check: User role is admin (enforced by mcp_launcher)

### Site ID Not Found
- Check: Grid exists in grids table with valid site_id

### API Authentication Failed
- Check: Auth token is correct base64 encoding
- Check: Username and password are valid

## Files Created

```
servers/equipment_control_server/
├── equipment_control_mcp_server.py  # Main server implementation
├── Dockerfile                        # Docker container config
├── README.md                         # Full documentation
├── SETUP.md                          # This file
└── __init__.py                       # Package marker
```

## Integration

The server is automatically discovered by mcp_launcher.py and:
- Registered as write-operation tools
- Restricted to Admin role only
- Controlled by EQUIPMENT_CONTROL_ACTIONS_ENABLED flag
- Included in Claude Desktop configuration

## Support

See README.md for:
- Detailed API documentation
- Full troubleshooting guide
- Integration examples
- Logging information
