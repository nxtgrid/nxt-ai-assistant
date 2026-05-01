# Customer MCP Server

MCP server for customer-facing operations. This is the **only server accessible in customer mode** (when user's organization_id ≠ 2). Provides tools for checking payment completion status and meter commissioning status.

## Features

- **Payment Completion Checker**: Verify payment status across Payment processor, orders table, and directive status
- **Commissioning Status Checker**: Track meter commissioning progress and history
- **Customer Visibility**: Both tools marked as `visible_to_customer=True`
- **RLS-Aware**: Respects Row Level Security policies for data access
- **Staff Override**: Users with organization_id = 2 (staff) can access all data across organizations

## Configuration

### Environment Variables

```bash
# AUTH Supabase Database Configuration (contains orders, directives, meters, connections, customers, grids)
AUTH_SUPABASE_URL=https://your-auth-project.supabase.co
AUTH_SUPABASE_KEY=your_auth_service_role_key
AUTH_SUPABASE_ANON_KEY=your_auth_anon_key

# Payment processor API Configuration (shared)
PAYMENT_PROCESSOR_API_URL=https://api.your-payment-processor.com/v3
PAYMENT_PROCESSOR_SECRET_KEY=your_secret_key
```

**Note**: This server uses the AUTH database which contains all customer-related tables (orders, directives, meters, connections, customers, grids).

## Tools

### check_payment_completion

Check the completion status of a payment transaction.

**Visibility**: ✅ Visible to customers (`visible_to_customer=True`)

**Input Parameters**:
- `transaction_reference` (string, required): Transaction reference in format `OrgName+MeterRef__timestamp`
  - Example: `YourOrg+MTR123__2024-11-24T10:00:00.000Z`
- `user_email` (string, required): User email for access verification and organization lookup
- `organization_id` (integer, optional): Organization ID (auto-looked up if not provided)

**Returns**:
```json
{
  "order_found": true,
  "transaction_reference": "YourOrg+MTR123__2024-11-24T10:00:00.000Z",
  "status_in_payment_processor": "successful",
  "status_in_orders_table": "COMPLETED",
  "directive_status": "delivered",
  "directive_token": "12345-67890-12345",
  "order_id": 456,
  "recipient": {
    "type": "meter",
    "meter_id": 789,
    "meter_no": "MTR123456",
    "customer_name": "John Doe",
    "customer_id": 101,
    "connection_type": "Residential",
    "connection_id": 202,
    "grid_name": "Grid North",
    "grid_id": 303,
    "grid_status": "online"
  }
}
```

**Response Fields**:
- `order_found` (boolean): Whether order was found for user's organization
- `status_in_payment_processor` (string): Payment status from Payment processor ("successful", "pending", "failed", "not found", or error message)
- `status_in_orders_table` (string): Order status from orders table
- `directive_status` (string): Status of associated directive ("pending", "processing", "delivered", "failed", "not found")
- `directive_token` (string|null): Meter token from directive if available
- `order_id` (integer): Order ID for reference
- `recipient` (object): **NEW** Enriched recipient information
  - If `type` is "meter":
    - `meter_id`, `meter_no`: Meter identification
    - `customer_name`, `customer_id`: Customer information
    - `connection_type`, `connection_id`: Connection details
    - `grid_name`, `grid_id`, `grid_status`: Grid information (online/offline)
  - If `type` is "agent":
    - `agent_id`, `agent_email`, `agent_telegram_id`: Agent information

**Error Response**:
```json
{
  "error": "Invalid transaction reference format. Expected format: OrgName+MeterRef__timestamp"
}
```
or
```json
{
  "order_found": false,
  "message": "Order not found for your organization",
  "transaction_reference": "..."
}
```

**Use Cases**:
1. Customer wants to know if their payment went through
2. Customer checking why meter token hasn't been delivered
3. Support staff investigating payment/order issues
4. Automated status checks in payment workflows

### meter_information

Get comprehensive information about a meter including customer details, connection info, grid status, meter power state, credit balance, and directive history.

**Visibility**: ✅ Visible to customers (`visible_to_customer=True`)

**Input Parameters**:
- `meter_number` (string, required): Meter number to check
- `user_email` (string, required): User email for access verification and organization lookup
- `organization_id` (integer, optional): Organization ID (auto-looked up if not provided)

**Returns**:
```json
{
  "meter_found": true,
  "meter_number": "47003332997",
  "customer_name": "John Doe",
  "connection_type": "Residential",
  "grid_name": "Grid North",
  "grid_status": "grid is energized",
  "dcu_status": "dcu is online",
  "is_on": true,
  "is_on_updated_at": "2024-11-28T14:23:15.000Z",
  "kwh_credit_available": 45.2,
  "kwh_credit_available_updated_at": "2024-11-28T13:45:30.000Z",
  "power_limit": 5000,
  "connection_metrics": {
    "signal_strength": -65,
    "last_seen": "2024-11-28T10:30:00.000Z"
  },
  "directives_count": 5,
  "directives": [
    {
      "directive_id": 101,
      "type": "TOKEN",
      "status": "COMPLETED",
      "created_at": "2024-11-28T10:00:00.000Z",
      "updated_at": "2024-11-28T10:05:00.000Z"
    },
    {
      "directive_id": 98,
      "type": "COMMISSIONING",
      "status": "COMPLETED",
      "created_at": "2024-11-27T14:00:00.000Z",
      "updated_at": "2024-11-27T14:15:00.000Z"
    },
    {
      "directive_id": 95,
      "type": "CONFIGURATION",
      "status": "PENDING",
      "created_at": "2024-11-26T09:00:00.000Z",
      "updated_at": "2024-11-26T09:00:00.000Z"
    }
  ],
  "last_error_directive": {
    "directive_id": 88,
    "type": "TOKEN",
    "status": "FAILED",
    "error": "Communication timeout with meter",
    "created_at": "2024-11-25T16:00:00.000Z",
    "updated_at": "2024-11-25T16:10:00.000Z"
  },
  "message": "Found 5 directive(s)"
}
```

**Response Fields**:
- `meter_found` (boolean): Whether meter was found for user's organization
- `meter_number` (string): Meter number queried
- `customer_name` (string): Customer name from customers table
- `connection_type` (string): Connection type (e.g., "Residential", "Commercial", "Public")
- `grid_name` (string): Grid name
- `grid_status` (string): Grid status ("grid is energized" or "grid is down")
- `dcu_status` (string): DCU connectivity status ("dcu is online" or "dcu is offline")
- `is_on` (boolean): Whether meter is currently powered on
- `kwh_credit_available` (number): Available kWh credit balance
- `power_limit` (number): Power limit in watts
- `connection_metrics` (object): Connection quality metrics (JSON)
- `directives_count` (integer): Number of directives found (max 5)
- `directives` (array): List of most recent directives (any type), most recent first
  - `directive_id` (integer): Directive ID
  - `type` (string): Directive type ("TOKEN", "COMMISSIONING", "CONFIGURATION", etc.)
  - `status` (string): Current status ("PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", etc.)
  - `created_at` (string): ISO timestamp when directive was created
  - `updated_at` (string): ISO timestamp of last update
- `last_error_directive` (object|null): Most recent failed directive, if any
  - `directive_id` (integer): Directive ID
  - `type` (string): Directive type
  - `status` (string): Error status ("FAILED")
  - `error` (string): Error message/details
  - `created_at` (string): ISO timestamp when directive was created
  - `updated_at` (string): ISO timestamp of last update
- `last_successful_token` (object|null): Most recent successful token directive, if any
  - `directive_id` (integer): Directive ID
  - `token` (string): The token value that was successfully delivered
  - `token_type` (string): Type of token directive (always "TOKEN")
  - `created_at` (string): ISO timestamp when directive was created
  - `updated_at` (string): ISO timestamp when directive was completed
- `message` (string): Human-readable summary

**Error Response**:
```json
{
  "meter_found": false,
  "message": "Meter not found for your organization",
  "meter_number": "MTR123456"
}
```

**Use Cases**:
1. Customer checking installation/commissioning progress
2. Customer investigating why meter isn't working yet
3. Support staff troubleshooting commissioning issues
4. Historical commissioning audit trail

## Organization-Based Access Control

### For Customers (organization_id ≠ 2)

- **Data Scoping**: Users can only access data belonging to their organization
- **RLS Enforcement**: Row Level Security policies filter database queries automatically
- **Organization Lookup**: User's organization_id is looked up from `accounts` table using `user_email`

### For Staff (organization_id = 2)

- **Full Access**: Staff can access data across all organizations
- **No Filtering**: Organization filters are skipped when org_id = 2
- **Troubleshooting**: Enables support staff to investigate issues for any customer

## Database Schema Requirements

### Tables Used

All tables are in the **AUTH database** (AUTH_SUPABASE_URL).

**accounts**:
- `email` (text): User email address
- `organization_id` (integer): User's organization
- `telegram_id` (text): Telegram user ID (for agent recipients)
- `deleted_at` (timestamp): Soft delete marker

**orders**:
- `id` (integer): Order ID
- `external_reference` (text): Transaction reference (format: OrgName+MeterRef__timestamp)
- `status` (text): Order status (PENDING, PROCESSING, COMPLETED, FAILED, etc.)
- `organization_id` (integer): Organization that owns this order
- `receiving_meter_id` (integer): Meter receiving the order (nullable)
- `receiving_type` (text): Recipient type ("meter" or "agent")
- `receiving_agent_id` (integer): Agent receiving the order if type is "agent" (nullable)

**directives**:
- `id` (integer): Directive ID
- `order_id` (integer): Associated order
- `meter_id` (integer): Associated meter
- `status` (text): Directive status
- `token` (text): Meter token if applicable
- `commissioning_id` (text): Commissioning reference (nullable)
- `created_at` (timestamp): Creation timestamp
- `updated_at` (timestamp): Last update timestamp

**meters**:
- `id` (integer): Meter ID
- `meter_no` (text): Meter number
- `organization_id` (integer): Organization that owns this meter
- `connection_id` (integer): Reference to connections table
- `grid_id` (integer): Reference to grids table (or rls_grid_id as alternative)

**connections**:
- `id` (integer): Connection ID
- `customer_id` (integer): Reference to customers table
- `connection_type` (text): Type of connection (e.g., "Residential", "Commercial")

**customers**:
- `id` (integer): Customer ID
- `name` (text): Customer name

**grids**:
- `id` (integer): Grid ID
- `name` (text): Grid name
- `is_hps_on` (boolean): Grid online/offline status (or alternative: is_online, status)

## Transaction Reference Format

The `transaction_reference` follows this format:

```
[DEV__]{OrganizationName}+{MeterReference}__{ISO_Timestamp}
```

**Examples**:
- `YourOrg+MTR123__2024-11-24T10:00:00.000Z`
- `DEV__TestOrg+METER456__2024-11-23T15:30:00.000Z`
- `ACME+john_doe__2025-10-20T18:32:00.373Z`

**Components**:
- `DEV__` (optional): Development environment prefix
- `OrganizationName`: Organization short name
- `MeterReference`: Meter or order reference (may contain underscores)
- `__` (required): Double underscore separator before timestamp
- `ISO_Timestamp`: ISO 8601 timestamp

**Validation**:
- Must contain `+` separator
- Format is validated before lookup

**OCR Tolerance**:
The server automatically handles OCR misreads:
- If reference contains single `_` before date: `ACME+Name_2025-11-17...`
- Server tries: `ACME+Name__2025-11-17...` (converts to double underscore)
- This handles cases where OCR reads `__` as `_`
- References with correct `__` format are not modified

## Integration with Payment processor

This server integrates with the Payment processor API to verify payment transaction status:

- **API Endpoint**: `/transactions/verify_by_reference`
- **Authentication**: Uses `PAYMENT_PROCESSOR_SECRET_KEY`
- **Response Mapping**: Extracts transaction status from Payment processor response
- **Error Handling**: Returns "not found" or error message if verification fails

## Error Handling

All tools return structured error responses:

```json
{
  "error": "Error message explaining what went wrong"
}
```

Common errors:
- Invalid transaction reference format
- User organization not found
- Order/meter not found for organization
- Database connection errors
- Payment processor API errors

## Usage Examples

### Example 1: Check Payment Completion

```json
{
  "tool": "check_payment_completion",
  "arguments": {
    "transaction_reference": "YourOrg+MTR123__2024-11-24T10:00:00.000Z",
    "user_email": "[email protected]"
  }
}
```

Response shows payment succeeded on Payment processor, order completed, and token delivered.

### Example 2: Get Meter Information

```json
{
  "tool": "meter_information",
  "arguments": {
    "meter_number": "47003332997",
    "user_email": "[email protected]"
  }
}
```

Response shows meter commissioning history with 3 recent directives, status of each.

### Example 3: Staff Override

```json
{
  "tool": "check_payment_completion",
  "arguments": {
    "transaction_reference": "CustomerOrg+MTR789__2024-11-20T09:00:00.000Z",
    "user_email": "[email protected]",
    "organization_id": 2
  }
}
```

Staff user with org_id = 2 can check any customer's payment status.

## Testing

### Local Testing

```bash
# Set environment variables
export AUTH_SUPABASE_URL=https://your-auth-project.supabase.co
export AUTH_SUPABASE_ANON_KEY=your_auth_anon_key
export PAYMENT_PROCESSOR_API_URL=https://api.your-payment-processor.com/v3
export PAYMENT_PROCESSOR_SECRET_KEY=your_payment_processor_key
export PYTHONPATH=/path/to/mcp_servers

# Run server directly
python3 servers/customer_server/customer_mcp_server.py
```

### Test with MCP Bridge

Add to bridge registry and test via REST API:

```bash
# List tools
curl -X GET http://localhost:8080/servers/customer/tools \
  -H "X-API-Key: your_api_key"

# Call check_payment_completion
curl -X POST http://localhost:8080/servers/customer/tools/check_payment_completion \
  -H "X-API-Key: your_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "transaction_reference": "YourOrg+MTR123__2024-11-24T10:00:00.000Z",
    "user_email": "[email protected]"
  }'
```

### Verify Server Discovery

```bash
python3 mcp_launcher.py --list
```

Should show `customer-server` in the list of available servers.

## Resources

### customer://config

Returns current server configuration:
```json
{
  "auth_supabase_url": "https://your-auth-project.supabase.co",
  "auth_supabase_configured": true,
  "payment_processor_url": "https://api.payment_processor.com/v3",
  "payment_processor_configured": true,
  "server_name": "customer-server",
  "server_version": "1.0.0"
}
```

### customer://status

Returns connection status:
```json
{
  "auth_supabase_configured": true,
  "payment_processor_configured": true
}
```

## Security Considerations

1. **RLS Enforcement**: All database queries respect Row Level Security policies
2. **Organization Scoping**: Users can only access their organization's data (except staff)
3. **User Email Required**: All tools require user_email for authentication and authorization
4. **No Write Operations**: This is a read-only server - no data modifications
5. **API Key Protection**: Payment processor secret key stored securely in environment
6. **Input Validation**: Transaction references validated before lookup

## Troubleshooting

### Server Not Starting

1. Check Python path is correct
2. Verify virtual environment is activated
3. Ensure `PYTHONPATH` includes `mcp_servers` directory
4. Check stderr logs for detailed error messages

### Order Not Found

1. Verify transaction reference format is correct
2. Check if order belongs to user's organization
3. Confirm organization_id is set correctly for user
4. For staff: ensure organization_id = 2 is passed

### Payment processor Errors

1. Verify `PAYMENT_PROCESSOR_SECRET_KEY` is set correctly
2. Check if using test vs live mode credentials
3. Ensure transaction exists in Payment processor system
4. Check API rate limits

### Meter Not Found

1. Verify meter number is correct
2. Check if meter belongs to user's organization
3. Confirm RLS policies allow access
4. For staff: ensure organization_id = 2 is passed

## License

Part of the Anansi MCP server collection.
