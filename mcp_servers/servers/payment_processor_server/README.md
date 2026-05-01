# Payment processor MCP Server

MCP server for checking Payment processor transaction status. This server provides tools to verify payment transactions using either Payment processor's transaction ID or merchant transaction reference.

## Features

- **Transaction Status Verification**: Check status of pending or completed transactions
- **Dual Lookup Methods**: Query by Payment processor transaction ID or merchant reference (tx_ref)
- **Customer Visible**: Tool is marked as visible to customers for self-service transaction checking
- **Comprehensive Response**: Returns transaction status, amount, currency, payment type, fees, and customer details

## Configuration

### Environment Variables

```bash
# Payment processor API Configuration
PAYMENT_PROCESSOR_API_URL=https://api.your-payment-processor.com/v3
PAYMENT_PROCESSOR_SECRET_KEY=your_secret_key_here
```

**Important**: Use your Payment processor **Secret Key** (not Public Key) for server-side verification.

### Getting Payment processor Credentials

1. Log in to your [Payment processor Dashboard](https://dashboard.payment_processor.com)
2. Navigate to **Settings** → **API Keys**
3. Copy your **Secret Key**
4. For production, use **Live Mode** keys
5. For testing, use **Test Mode** keys

## Tools

### check_transaction_status

Check the status of a Payment processor transaction.

**Visibility**: ✅ Visible to customers (`visible_to_customer=True`)

**Input Parameters** (provide one):
- `transaction_id` (string, optional): Payment processor's internal transaction ID
- `tx_ref` (string, optional): Merchant transaction reference

**Returns**:
```json
{
  "verification_status": "success",
  "message": "Transaction fetched successfully",
  "transaction": {
    "id": 123456,
    "tx_ref": "ORG_NAME+MTR123__2024-11-19T10:30:00.000Z",
    "flw_ref": "FLW-M03K-abc123...",
    "status": "successful",
    "amount": 3000,
    "currency": "NGN",
    "charged_amount": 3000,
    "app_fee": 90,
    "merchant_fee": 0,
    "amount_settled": 2910,
    "payment_type": "card",
    "processor_response": "Approved",
    "created_at": "2024-11-19T10:30:00.000Z",
    "customer": {
      "id": 252759,
      "name": "John Doe",
      "email": "[email protected]",
      "phone_number": "0813XXXXXXX"
    },
    "card": {
      "first_6digits": "553188",
      "last_4digits": "2950",
      "issuer": "CREDIT",
      "country": "NIGERIA NG",
      "type": "MASTERCARD"
    }
  }
}
```

**Transaction Status Values**:
- `successful` - Payment completed successfully
- `pending` - Payment is still being processed
- `failed` - Payment failed

## Integration with Skyfox

This server is designed to work seamlessly with the Skyfox order system:

### Transaction Reference Format

Skyfox stores transaction references in the format:
```
[DEV__]{organization_name}+{meter_external_reference}__{ISO_timestamp}
```

Example: `DEV__YourOrg+MTR123__2024-11-19T10:30:00.000Z`

### Database Schema

The reference is stored in Skyfox's `orders.external_reference` field and can be used to query transaction status:

```typescript
// Query Skyfox orders table
const order = await db.orders.findOne({ external_reference: tx_ref });

// Check Payment processor status via MCP
const status = await mcp.call_tool('check_transaction_status', {
  tx_ref: order.external_reference
});
```

### Use Cases

1. **Order Verification**: Confirm payment before processing order
2. **Customer Support**: Check transaction status for customer inquiries
3. **Webhook Validation**: Verify webhook notifications against actual transaction status
4. **Payment Reconciliation**: Match payments with orders
5. **Failed Payment Debugging**: Investigate why a payment failed

## Usage Examples

### Check by Transaction ID

```json
{
  "tool": "check_transaction_status",
  "arguments": {
    "transaction_id": "123456"
  }
}
```

### Check by Transaction Reference

```json
{
  "tool": "check_transaction_status",
  "arguments": {
    "tx_ref": "DEV__YourOrg+MTR123__2024-11-19T10:30:00.000Z"
  }
}
```

## Error Handling

### Transaction Not Found
```json
{
  "status": "error",
  "message": "No transaction was found for this id"
}
```

### Invalid Credentials
```json
{
  "status": "error",
  "message": "merchant secret key required"
}
```

### Missing Configuration
```
Error: Payment processor client not configured. Set PAYMENT_PROCESSOR_API_URL and PAYMENT_PROCESSOR_SECRET_KEY environment variables.
```

## Testing

### Local Testing

```bash
# Set environment variables
export PAYMENT_PROCESSOR_API_URL=https://api.your-payment-processor.com/v3
export PAYMENT_PROCESSOR_SECRET_KEY=your_test_secret_key
export PYTHONPATH=/path/to/mcp_servers

# Run server directly
python3 servers/payment_processor_server/payment_processor_mcp_server.py
```

### Test with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "payment_processor": {
      "command": "/full/path/to/.venv/bin/python3",
      "args": ["/full/path/to/servers/payment_processor_server/payment_processor_mcp_server.py"],
      "env": {
        "PAYMENT_PROCESSOR_API_URL": "https://api.your-payment-processor.com/v3",
        "PAYMENT_PROCESSOR_SECRET_KEY": "your_secret_key",
        "PYTHONPATH": "/full/path/to/mcp_servers"
      }
    }
  }
}
```

### Verify Server Discovery

```bash
python3 mcp_launcher.py --list
```

Should show `payment_processor-server` in the list of available servers.

## Resources

### payment_processor://config

Returns current server configuration:
```json
{
  "api_url": "https://api.payment_processor.com/v3",
  "configured": true,
  "server_name": "payment_processor-server",
  "server_version": "1.0.0"
}
```

### payment_processor://status

Returns connection status:
```json
{
  "configured": true,
  "api_url": "https://api.payment_processor.com/v3",
  "has_secret_key": true
}
```

## Security Considerations

1. **Secret Key Protection**: Never expose your secret key in client-side code or logs
2. **Environment Variables**: Store credentials in `.env` file (not in version control)
3. **Customer Visibility**: This tool is marked as customer-visible - it only performs read operations
4. **Rate Limiting**: Payment processor API has rate limits - implement caching if needed
5. **Amount Validation**: Always validate transaction amounts match expected values

## API Reference

- [Payment processor Transaction Verification](https://developer.payment_processor.com/v3.0/docs/transaction-verification)
- [Verify Transaction Endpoint](https://developer.payment_processor.com/v3.0/reference/verify-transaction)
- [Verify by Reference](https://developer.payment_processor.com/v3.0/reference/verify-transaction-with-tx_ref)

## Troubleshooting

### Server Not Starting

1. Check Python path is correct
2. Verify virtual environment is activated
3. Ensure `PYTHONPATH` includes `mcp_servers` directory
4. Check stderr logs for detailed error messages

### Transaction Not Found

1. Verify transaction ID or reference is correct
2. Check if using test vs live mode credentials
3. Ensure transaction was created in the same environment (test/live)

### Authentication Errors

1. Confirm using Secret Key (not Public Key)
2. Verify key matches environment (test/live)
3. Check key hasn't been rotated/revoked in dashboard

## Development

### Project Structure

```
payment_processor_server/
├── payment_processor_mcp_server.py   # Main server implementation
└── README.md                    # This file
```

### Dependencies

- `mcp` - MCP SDK for tool definitions
- `httpx` - HTTP client (via HTTPClientMixin)
- `python-dotenv` - Environment variable loading
- Shared utilities from `shared_code/`

### Code Patterns

- Inherits from `HTTPClientMixin` for session management
- Uses `compose_json_response()` and `compose_error_response()` for consistent formatting
- Logs to stderr for Claude Desktop visibility
- Auto-configures from environment variables
- Async/await for all API operations

## License

Part of the Anansi MCP server collection.
