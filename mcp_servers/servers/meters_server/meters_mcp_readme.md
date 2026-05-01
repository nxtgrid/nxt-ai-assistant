# Meters API MCP Server

This MCP (Model Context Protocol) server provides a unified interface to interact with different types of smart meters via various communication protocols and backend systems.

The server automatically detects the meter type from Supabase 'meters' table and routes API calls to the appropriate backend implementation.

## Features

### Unified Interface
- **Automatic Type Detection**: Queries Supabase to determine meter type
- **Transparent Routing**: Single set of tools works across all meter types
- **Consistent API**: Same tool signatures regardless of underlying meter technology

### Read-Only Operations (Always Available)
- **DCU/Base Station Status**: Get online status of DCU (V1/V2) or LoRaWAN gateway
- **Remote Reading Tasks**: Create reading requests for voltage, current, energy, etc.
- **Task Status Checks**: Monitor completion status of reading tasks

### Write Operations (Gated by METERS_ACTIONS_ENABLED)
- **Power Limit Control**: Send power limit tokens to meters
- **Token Delivery**: Send STS tokens (top-up, clear credit, etc.)

### Reliability Features
- **Automatic Retry**: All API calls retry once after 5 seconds on failure
- **Error Handling**: Clear error messages indicating API availability
- **Credential Management**: All credentials stored securely in .env file
- **OAuth Caching**: V2 tokens cached per (base_url, username, company) combination

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure environment variables in `.env`:
```bash
# Chat Database Configuration (for meter type detection with dynamic RLS)
CHAT_DB_URL=https://your-project.supabase.co
CHAT_DB_ANON_KEY=your-chat-db-anon-key

# Chat Database JWT Generation (enables dynamic Row Level Security)
# Service role key is used only to query auth.users table
# JWT secret is used to sign tokens for specific users
CHAT_DB_SERVICE_ROLE_KEY=your-chat-db-service-role-key
CHAT_DB_JWT_SECRET=your-jwt-secret

# Default user email for chat database authentication (optional)
# If not specified in tool calls, this user's RLS permissions will be used
CHAT_DB_DEFAULT_USER_EMAIL=meters-service@yourdomain.com

# Meters Server Actions Control
METERS_ACTIONS_ENABLED=false
```

3. Set up Supabase 'meters' table and authentication:

**Create the meters table:**
```sql
CREATE TABLE meters (
  meter_no TEXT PRIMARY KEY,
  meter_type TEXT NOT NULL,
  dcu_id TEXT,
  customer_id TEXT,
  dev_eui TEXT,
  gateway_id TEXT,
  user_id UUID REFERENCES auth.users(id) -- For Row Level Security
);
```

**Set up Row Level Security (RLS) policies:**
```sql
-- Enable RLS on the meters table
ALTER TABLE meters ENABLE ROW LEVEL SECURITY;

-- Policy: Users can only see their own meters
CREATE POLICY "Users can view their own meters"
  ON meters FOR SELECT
  USING (auth.uid() = user_id);

-- Policy: Users can only update their own meters
CREATE POLICY "Users can update their own meters"
  ON meters FOR UPDATE
  USING (auth.uid() = user_id);
```

**Create users in Supabase Auth:**
1. Go to Supabase Dashboard → Authentication → Users
2. Create users with email and password for each person/service that needs access
3. Set a default user email in `.env` as `SUPABASE_DEFAULT_USER_EMAIL`
4. Assign meters to users by setting `user_id` in the meters table
5. (Optional) Pass `user_email` parameter in tool calls to use a different user's permissions
6. Get service role key from Settings → API and add to `.env` as `SUPABASE_SERVICE_ROLE_KEY`
7. Get JWT secret from Settings → API and add to `.env` as `SUPABASE_JWT_SECRET`

4. Run the MCP server:
```bash
python meters_mcp_server.py
```

## Unified Interface Tools

**Important Notes:**
- All credentials are read from `.env` file - no authentication parameters need to be provided in tool calls
- **Timing Constraints**: Reading tasks are TWO-STEP operations (downlink command + 15 second wait + uplink status check) taking approximately 15-20 seconds total
- **Batching Restriction**: Only call ONE reading/writing tool per conversation response - do NOT batch multiple meters in a single response
- Status check tools (`get_meter_dcu_status`, `get_meter_reading_task_status`, `meters_debug_info`) are quick and can be called multiple times

### 1. **get_meter_dcu_status**
   - Get DCU/concentrator/base station online status for any meter type
   - Required Parameters: `meter_no`
   - Optional Parameters:
     - `dcu_id` (auto-retrieved from Supabase if not provided)
     - `gateway_id` (auto-retrieved from Supabase if not provided)
     - `user_email` (for RLS - uses default from `.env` if not provided)
   - Auto-detects meter type from Supabase

### 2. **create_meter_reading_task**
   - Create remote reading task for any meter type
   - **Operation**: TWO-STEP sequence
     1. Sends downlink command to physical meter
     2. Waits 15 seconds for meter to process
     3. Checks uplink response/status
   - **Timing**: Approximately 15-20 seconds total
   - **Important**: Only call ONCE per conversation response - do NOT batch multiple meters
   - Required Parameters:
     - `meter_no`: Meter number
     - `reading_type`: Type of reading to request (see Reading Types below)
   - Optional Parameters:
     - `customer_id` (auto-retrieved from Supabase if not provided)
     - `dev_eui` (auto-retrieved from Supabase if not provided)
     - `user_email` (for RLS - uses default from `.env` if not provided)
   - **Returns**: Complete meter reading result with data from the physical meter

   **Supported Reading Types:**
   - `voltage` - Line voltage measurement
   - `current` - Current draw measurement
   - `power` - Active power consumption
   - `energy` - Accumulated energy (kWh)
   - `current_credit` - Remaining prepaid credit
   - `power_limit` - Maximum power threshold setting
   - `relay_status` - Meter relay state (on/off)
   - `power_down_count` - Number of power outages
   - `maximum_power_threshold` - Maximum power limit
   - `special_status` - Meter error/tamper flags
   - `meter_version` - Firmware version

### 3. **get_meter_reading_task_status**
   - Get reading task status for any meter type
   - Required Parameters: `meter_no`, `task_id`
   - Optional Parameters: `user_email` (for RLS - uses default from `.env` if not provided)
   - Auto-detects meter type from Supabase

### 4. **send_meter_power_limit** (Write Operation - Requires METERS_ACTIONS_ENABLED=true)
   - Send power limit token to a meter
   - **Timing**: Takes 5-10 seconds to complete
   - **Important**: Only call ONCE per conversation response - do NOT batch multiple meters
   - Required Parameters: `meter_no`, `power_limit`
   - Optional Parameters:
     - `customer_id` (auto-retrieved from Supabase if not provided)
     - `issue_date` (required for token generation)
     - `user_email` (for RLS - uses default from `.env` if not provided)

### 5. **send_meter_token** (Write Operation - Requires METERS_ACTIONS_ENABLED=true)
   - Send STS token to a meter (top-up credit, clear credit, etc.)
   - **Timing**: Takes 5-10 seconds to complete
   - **Important**: Only call ONCE per conversation response - do NOT batch multiple meters
   - Required Parameters: `meter_no`, `token`
   - Optional Parameters:
     - `customer_id` (auto-retrieved from Supabase if not provided)
     - `dev_eui` (auto-retrieved from Supabase if not provided)
     - `user_email` (for RLS - uses default from `.env` if not provided)

### 6. **track_topup_status**
   - Track complete status of topup payment from customer payment through token delivery
   - **Stages Checked:**
     1. Payment status on Payment processor API
     2. Order record in auth database (orders table)
     3. Token generation (directives table)
     4. Token delivery status and outcome
   - Required Parameters: `transaction_id` (Payment processor transaction ID or tx_ref)
   - **Returns**: Detailed breakdown of which stage the payment is in with recommendations
   - **Use case**: When customers ask "Where is my topup?" or "Has my payment gone through?"
   - **Example scenarios**:
     - Payment pending on mobile money/USSD
     - Payment successful on Payment processor but order not created
     - Order created but token not generated
     - Token generated but delivery failed
     - Token successfully delivered to meter

### 7. **meters_debug_info**
   - Get debug information (token cache status, configuration)
   - No parameters required

## Reading Type Mappings

The server supports various reading types that map to specific meter protocols. Consult your meter documentation for supported reading types and their protocol mappings.

## Usage Examples

### Using with Claude Desktop

Add to your Claude Desktop MCP configuration:

```json
{
  "servers": {
    "meters-api": {
      "command": "python",
      "args": ["/path/to/meters_mcp_server.py"]
    }
  }
}
```

### Example Tool Calls

**Note:** All examples assume credentials are configured in `.env` file and meter types are registered in Supabase.

1. **Get DCU status for any meter**:
```json
{
  "tool": "get_meter_dcu_status",
  "arguments": {
    "meter_no": "12345"
  }
}
```

2. **Create voltage reading task**:
```json
{
  "tool": "create_meter_reading_task",
  "arguments": {
    "meter_no": "12345",
    "reading_type": "voltage",
    "customer_id": "1"
  }
}
```

3. **Check reading task status**:
```json
{
  "tool": "get_meter_reading_task_status",
  "arguments": {
    "meter_no": "12345",
    "task_id": "123456"
  }
}
```

4. **Send power limit (write operation)**:
```json
{
  "tool": "send_meter_power_limit",
  "arguments": {
    "meter_no": "12345",
    "power_limit": 5000,
    "customer_id": "1",
    "issue_date": "2024-01-01T00:00:00Z"
  }
}
```

5. **Send token (write operation)**:
```json
{
  "tool": "send_meter_token",
  "arguments": {
    "meter_no": "12345",
    "token": "12345678901234567890",
    "customer_id": "1"
  }
}
```

6. **Track topup status**:
```json
{
  "tool": "track_topup_status",
  "arguments": {
    "transaction_id": "FLW-123456789"
  }
}
```

**Example Response:**
```json
{
  "transaction_id": "FLW-123456789",
  "current_stage": "completed",
  "status": "success",
  "stages": {
    "payment_processor": {
      "found": true,
      "paid": true,
      "amount": 5000,
      "currency": "KES",
      "payment_processor_status": "successful"
    },
    "order": {
      "found": true,
      "order_id": "ORD-456",
      "status": "success",
      "meter_no": "12345"
    },
    "directive": {
      "found": true,
      "token": "1234567890123456",
      "meter_no": "12345",
      "status": "success"
    },
    "delivery": {
      "token": "1234567890123456",
      "meter_no": "12345",
      "directive_status": "success"
    }
  },
  "recommendations": [
    "✅ Topup complete! Token 1234567890123456 delivered to meter 12345"
  ]
}
```

## How It Works

### Type Detection Flow
1. User calls unified tool with `meter_no`
2. Server queries Supabase `meters` table for `meter_type`
3. Server routes to appropriate backend implementation
4. Backend executes operation and returns result

### Implementation Details
- Backend implementations handle protocol-specific communication
- Token-based correlation for asynchronous operations
- Proper authentication and session management
- Command encoding according to meter specifications

## Error Handling

The server includes comprehensive error handling:
- **Automatic Retry**: All API calls retry once after 5 seconds on failure
- **Availability Messages**: Clear messages when API is unavailable
- **Automatic Token Refresh**: OAuth tokens automatically refresh when expired
- **Token Caching**: Tokens cached to minimize authentication calls
- **Detailed Error Messages**: Context-aware error messages with relevant details
- **Timeout Handling**: Proper timeout handling for all API operations
- **Parameter Validation**: Validation of required parameters before API calls
- **Meter Type Validation**: Returns error if meter type is unknown or not found in Supabase

## Security Notes

- **Environment Variables**: All credentials stored securely in `.env` file
- **No Hardcoded Credentials**: Authentication parameters never exposed in tool calls
- **OAuth Token Security**: Tokens stored in memory only, cleared on server restart
- **Credential Isolation**: Separate credentials for different backend APIs
- **Access Control**: Write operations only enabled when `METERS_ACTIONS_ENABLED=true` in `.env`
- **Supabase Row Level Security (RLS)**:
  - Uses **dynamic JWT generation** for flexible user permissions
  - Service role key used only to query `auth.users` table
  - Generates JWT tokens for specific users with their permissions
  - All database queries respect RLS policies for the specified user
  - Users can only access meters assigned to their `user_id`
  - Automatic JWT refresh with 60-second buffer
  - Per-user JWT caching to minimize token generation
  - Optional `user_email` parameter in all tools (uses default from `.env` if not provided)
  - Each user sees only their authorized meters based on RLS policies

## Backend API Integration

The server integrates with various backend APIs for meter management. Specific API endpoints and implementations are abstracted through the unified interface. Consult your backend API documentation for detailed endpoint specifications.

## Topup Tracker Configuration

The `track_topup_status` tool requires additional configuration to access Payment processor and your auth database:

### Required Environment Variables

```bash
# Payment processor API credentials
PAYMENT_PROCESSOR_SECRET_KEY=your_payment_processor_secret_key
PAYMENT_PROCESSOR_PUBLIC_KEY=your_payment_processor_public_key  # Optional
PAYMENT_PROCESSOR_ENCRYPTION_KEY=your_payment_processor_encryption_key  # Optional

# Auth Supabase (for orders and directives tables)
AUTH_SUPABASE_URL=https://your-auth-project.supabase.co
AUTH_SUPABASE_KEY=your_auth_service_role_key
```

### Database Schema Requirements

The tracker expects the following tables in your auth database:

**orders table:**
```sql
CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  transaction_id TEXT,  -- Payment processor transaction ID
  tx_ref TEXT,          -- Payment processor tx_ref (alternative)
  flw_transaction_id TEXT,  -- Another common field name
  status TEXT,          -- Order status (pending, success, failed)
  amount NUMERIC,
  meter_no TEXT,
  customer_email TEXT,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
);
```

**directives table:**
```sql
CREATE TABLE directives (
  id SERIAL PRIMARY KEY,
  order_id INTEGER REFERENCES orders(id),
  token TEXT,           -- Generated STS token
  meter_no TEXT,        -- Target meter number
  status TEXT,          -- Delivery status (pending, success, failed, delivered)
  error_message TEXT,   -- Error details if failed
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
);
```

### Payment Pipeline Stages

The tracker identifies which stage of the payment-to-token pipeline is complete:

1. **not_found**: Transaction not found on Payment processor
2. **payment_pending**: Payment received by Payment processor but order not in database
3. **order_pending_token**: Order exists but token not generated
4. **delivery_pending**: Token generated but not yet delivered
5. **delivery_failed**: Token delivery failed
6. **completed**: Token successfully delivered to meter

### Troubleshooting

**Transaction not found:**
- Verify transaction_id is correct
- Check if using tx_ref vs transaction_id

**Order not found:**
- Payment may still be processing
- Check webhook from Payment processor is configured
- Verify orders table column names match (transaction_id, tx_ref, or flw_transaction_id)

**No directive found:**
- Order status may not be "success"
- Check if token generation service is running
- Verify order_id linkage

**Delivery failed:**
- Check error_message field in directives table
- Meter may be offline or unreachable
- Token format may be invalid

## Architecture

```
┌─────────────────────────────────────────────────┐
│         Unified Meters MCP Server               │
├─────────────────────────────────────────────────┤
│  Unified Tools (Auto-routing by meter type)     │
│  - get_meter_dcu_status                         │
│  - create_meter_reading_task                    │
│  - get_meter_reading_task_status                │
│  - send_meter_power_limit                       │
│  - send_meter_token                             │
└────────────┬────────────────────────────────────┘
             │
             ├── Supabase Query (meter type detection)
             │
      ┌──────┴───────┬──────────────┐
      ▼              ▼              ▼
┌──────────┐  ┌──────────┐  ┌──────────────┐
│ Backend  │  │ Backend  │  │   Backend    │
│  API 1   │  │  API 2   │  │    API 3     │
└──────────┘  └──────────┘  └──────────────┘
     │              │              │
     ▼              ▼              ▼
┌──────────┐  ┌──────────┐  ┌──────────────┐
│  Meters  │  │  Meters  │  │    Meters    │
│  Type 1  │  │  Type 2  │  │    Type 3    │
└──────────┘  └──────────┘  └──────────────┘
```

## Contributing

This MCP server provides a unified interface for meter management. Updates should reflect changes in the underlying API implementations and ensure backward compatibility.
