-- 0032: single-use enforcement for the MCP gateway's OAuth authorization
-- codes. The code itself is a self-contained, HMAC-signed value (see
-- gateway/oauth_codes.py) carrying everything needed to validate it without
-- a DB lookup -- this table exists purely to answer "has this exact code
-- already been redeemed", which a signature alone can never answer.
--
-- Row lifetime is minutes, not persistent state: a code that expired
-- (has already had its TTL checked at the signature-verification step)
-- never needs its row again. periodic cleanup can delete rows past
-- expires_at; nothing about correctness depends on that cleanup running.

CREATE TABLE IF NOT EXISTS mcp_gateway_oauth_codes (
    code_id text PRIMARY KEY,          -- the code's own embedded jti, not the code itself
    expires_at timestamptz NOT NULL,
    redeemed_at timestamptz            -- NULL until first (and only) redemption
);

CREATE INDEX IF NOT EXISTS mcp_gateway_oauth_codes_expires_at_idx
    ON mcp_gateway_oauth_codes (expires_at);
