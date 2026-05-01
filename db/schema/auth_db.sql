-- Auth Database (PostgreSQL — your enterprise/operational data store)
-- This is a READONLY database for Anansi. The bot reads from it but never writes.
--
-- Schema generated from the live production database.
-- Anansi only queries a subset of these tables — see comments below for which
-- are required for each feature. If you already have an auth/ERP database,
-- map these tables to your equivalents by updating shared/auth/auth_service.py
-- and mcp_servers/servers/customer_server/customer_mcp_server.py.
--
-- REQUIRED for core bot operation:
--   organizations, accounts, members, grids
-- REQUIRED for meter/customer MCP tools:
--   meters, connections, directives, orders, customers, dcus
--
-- Connection: asyncpg with AUTH_DB_* env vars (ssl=require, statement_cache_size=0)
-- User: a readonly user (see grant block at bottom)

-- ── Custom Enums ─────────────────────────────────────────────────────────────
-- Adapt these to your own enum values. Anansi reads but never writes these.

CREATE TYPE member_type_enum AS ENUM ('DEVELOPER', 'ADMIN', 'OPERATOR', 'VIEWER');
CREATE TYPE external_system_enum AS ENUM ('VICTRON', 'CALIN', 'EPICOLLECT', 'MANUAL');
CREATE TYPE meter_type_enum AS ENUM ('HPS', 'FS');
CREATE TYPE meter_phase_enum AS ENUM ('SINGLE_PHASE', 'THREE_PHASE');
CREATE TYPE directive_status_enum AS ENUM ('PENDING', 'IN_PROGRESS', 'SUCCESS', 'FAILED');
CREATE TYPE directive_type_enum AS ENUM ('TOPUP', 'TURN_ON', 'TURN_OFF', 'SET_POWER_LIMIT');
CREATE TYPE directive_special_status_enum AS ENUM ('STS', 'SPECIAL');
CREATE TYPE directive_error_enum AS ENUM ('TIMEOUT', 'REJECTED', 'INVALID');
CREATE TYPE order_status_enum AS ENUM ('PENDING', 'COMPLETED', 'FAILED', 'CANCELLED');
CREATE TYPE order_type_enum AS ENUM ('ENERGY_TOPUP', 'CONNECTION_FEE', 'TRANSFER');
CREATE TYPE order_actor_type_enum AS ENUM ('CUSTOMER', 'ADMIN', 'SYSTEM');
CREATE TYPE payment_method_enum AS ENUM ('CASH', 'TRANSFER', 'USSD');
CREATE TYPE payment_channel_enum AS ENUM ('MANUAL', 'USSD', 'BANK');
CREATE TYPE currency_enum AS ENUM ('NGN', 'USD', 'GHS');
CREATE TYPE gender_enum AS ENUM ('M', 'F');
CREATE TYPE generator_type_enum AS ENUM ('PETROL', 'DIESEL', 'NONE');
CREATE TYPE id_document_type_enum AS ENUM ('PASSPORT', 'NIN', 'DRIVERS_LICENSE');
CREATE TYPE communication_protocol_enum AS ENUM ('CALIN_LORAWAN', 'CALIN_GPRS', 'SIMCOM');
CREATE TYPE account_type_enum AS ENUM ('CUSTOMER', 'STAFF', 'SYSTEM');
CREATE TYPE organization_type_enum AS ENUM ('SOLAR_DEVELOPER', 'PARTNER');
CREATE TYPE weather_type_enum AS ENUM ('SUNNY', 'CLOUDY', 'RAINY');

-- ── Organizations ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS organizations (
    id                                  integer NOT NULL PRIMARY KEY,
    created_at                          timestamptz NOT NULL DEFAULT now(),
    name                                varchar NOT NULL,              -- short name used for hashtags
    formal_name                         varchar,
    email                               varchar,
    phone                               varchar,
    address                             varchar,
    developer_group_telegram_chat_id    varchar,                       -- Telegram group ID for this org's staff
    deleted_at                          timestamptz,
    organization_type                   organization_type_enum NOT NULL DEFAULT 'SOLAR_DEVELOPER'
);

-- ── User Accounts ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS accounts (
    id                                  integer NOT NULL PRIMARY KEY,
    created_at                          timestamptz NOT NULL DEFAULT now(),
    full_name                           varchar,
    email                               varchar,
    phone                               varchar,
    telegram_id                         varchar,
    telegram_link_token                 varchar,
    deleted_at                          timestamp,
    supabase_id                         uuid,
    organization_id                     integer
);

CREATE UNIQUE INDEX IF NOT EXISTS accounts_telegram_id_idx ON accounts (telegram_id);
CREATE INDEX IF NOT EXISTS accounts_email_idx ON accounts (email);

-- ── Organization Membership ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS members (
    id                                  integer NOT NULL PRIMARY KEY,
    created_at                          timestamptz NOT NULL DEFAULT now(),
    member_type                         member_type_enum NOT NULL DEFAULT 'DEVELOPER',
    account_id                          integer,
    rls_organization_id                 integer,
    hidden                              boolean NOT NULL DEFAULT false,
    training_level                      smallint NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS members_rls_organization_id_idx ON members (rls_organization_id);

-- ── Grids / Sites ─────────────────────────────────────────────────────────────
-- A "grid" is a mini-grid or energy site.

CREATE TABLE IF NOT EXISTS grids (
    id                                  integer NOT NULL PRIMARY KEY,
    created_at                          timestamptz NOT NULL DEFAULT now(),
    deleted_at                          timestamptz,
    deployed_at                         timestamptz,
    commissioned_at                     timestamptz,
    name                                varchar NOT NULL,
    timezone                            varchar NOT NULL DEFAULT 'UTC',
    kwp                                 double precision NOT NULL DEFAULT 0,
    kwh                                 double precision NOT NULL DEFAULT 0,
    is_fs_on                            boolean NOT NULL DEFAULT false,
    is_hps_on                           boolean NOT NULL DEFAULT false,
    should_fs_be_on                     boolean NOT NULL DEFAULT false,
    is_hidden_from_reporting            boolean NOT NULL DEFAULT true,
    is_three_phase_supported            boolean NOT NULL DEFAULT false,
    is_hps_on_threshold_kw              double precision NOT NULL DEFAULT 0,
    is_generation_managed_by_nxt_grid   boolean NOT NULL DEFAULT true,    -- rename to your org
    are_all_dcus_online                 boolean NOT NULL DEFAULT false,
    organization_id                     integer NOT NULL,
    internal_telegram_group_chat_id     varchar,                          -- Telegram group for O&M
    internal_telegram_group_thread_id   varchar,                          -- Topic/thread within group
    telegram_config                     jsonb DEFAULT '{}',
    feature_access_config               jsonb NOT NULL DEFAULT '{}',
    -- VRM / equipment integration
    generation_external_site_id         varchar,                          -- External site ID for REST API
    generation_external_gateway_id      varchar,                          -- External gateway ID for MQTT
    generation_external_system          external_system_enum NOT NULL DEFAULT 'VICTRON'
);

CREATE INDEX IF NOT EXISTS grids_organization_id_idx ON grids (organization_id);
CREATE INDEX IF NOT EXISTS grids_name_idx ON grids (name);

-- ── Data Concentrator Units (DCUs) ────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dcus (
    id                                  integer NOT NULL PRIMARY KEY,
    created_at                          timestamptz NOT NULL DEFAULT now(),
    external_reference                  varchar NOT NULL,
    external_system                     external_system_enum NOT NULL,
    is_online                           boolean NOT NULL DEFAULT false,
    last_online_at                      timestamptz,
    communication_protocol              communication_protocol_enum,
    queue_buffer_length                 integer NOT NULL DEFAULT 50,
    grid_id                             integer,
    rls_organization_id                 integer
);

CREATE INDEX IF NOT EXISTS dcus_rls_organization_id_idx ON dcus (rls_organization_id);

-- ── Meters ────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS meters (
    id                                  integer NOT NULL PRIMARY KEY,
    created_at                          timestamptz NOT NULL DEFAULT now(),
    external_reference                  varchar NOT NULL,
    external_system                     external_system_enum NOT NULL,
    deleted_at                          timestamp,
    balance                             double precision,
    balance_updated_at                  timestamptz,
    kwh_credit_available                double precision,
    kwh_credit_available_updated_at     timestamptz,
    is_on                               boolean DEFAULT false,
    should_be_on                        boolean DEFAULT false,
    is_manual_mode_on                   boolean NOT NULL DEFAULT false,
    meter_type                          meter_type_enum NOT NULL DEFAULT 'HPS',
    meter_phase                         meter_phase_enum NOT NULL DEFAULT 'SINGLE_PHASE',
    voltage                             double precision,
    power                               double precision,
    power_limit                         integer,
    latitude                            double precision,
    longitude                           double precision,
    nickname                            varchar,
    last_seen_at                        timestamptz,
    is_cabin_meter                      boolean NOT NULL DEFAULT false,
    is_simulated                        boolean NOT NULL DEFAULT false,
    is_starred                          boolean NOT NULL DEFAULT false,
    connection_id                       integer,
    dcu_id                              integer,
    rls_grid_id                         integer,
    rls_organization_id                 integer,
    connection_metrics                  jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS meters_external_reference_idx ON meters (external_reference, external_system);
CREATE INDEX IF NOT EXISTS meters_grid_id_idx ON meters (rls_grid_id);
CREATE INDEX IF NOT EXISTS meters_rls_organization_id_idx ON meters (rls_organization_id);
CREATE INDEX IF NOT EXISTS meters_dcu_id_idx ON meters (dcu_id);
CREATE INDEX IF NOT EXISTS meters_connection_id_idx ON meters (connection_id);

-- ── Customers ─────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS customers (
    id                                  integer NOT NULL PRIMARY KEY,
    created_at                          timestamptz NOT NULL DEFAULT now(),
    gender                              gender_enum,
    is_hidden_from_reporting            boolean NOT NULL DEFAULT false,
    lives_primarily_in_the_community    boolean NOT NULL DEFAULT true,
    latitude                            double precision,
    longitude                           double precision,
    generator_owned                     generator_type_enum,
    grid_id                             integer,
    account_id                          integer,
    rls_organization_id                 integer
);

CREATE INDEX IF NOT EXISTS customers_grid_id_idx ON customers (grid_id);
CREATE INDEX IF NOT EXISTS customers_rls_organization_id_idx ON customers (rls_organization_id);

-- ── Customer Connections ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS connections (
    id                                  integer NOT NULL PRIMARY KEY,
    created_at                          timestamptz NOT NULL DEFAULT now(),
    deleted_at                          timestamp,
    is_lifeline                         boolean,
    is_public                           boolean NOT NULL DEFAULT false,
    is_commercial                       boolean NOT NULL DEFAULT false,
    is_residential                      boolean NOT NULL DEFAULT true,
    customer_id                         integer,
    rls_organization_id                 integer
);

CREATE INDEX IF NOT EXISTS connections_customer_id_idx ON connections (customer_id);
CREATE INDEX IF NOT EXISTS connections_rls_organization_id_idx ON connections (rls_organization_id);

-- ── Orders (energy top-ups and payments) ─────────────────────────────────────

CREATE TABLE IF NOT EXISTS orders (
    id                                  integer NOT NULL PRIMARY KEY,
    created_at                          timestamptz NOT NULL DEFAULT now(),
    updated_at                          timestamptz NOT NULL DEFAULT now(),
    amount                              double precision NOT NULL,
    order_status                        order_status_enum NOT NULL DEFAULT 'PENDING',
    currency                            currency_enum NOT NULL,
    external_reference                  varchar,
    external_system                     external_system_enum,
    tariff_type                         meter_type_enum,
    tariff                              double precision NOT NULL DEFAULT -1,
    payment_method                      payment_method_enum,
    payment_channel                     payment_channel_enum,
    meta_order_type                     order_type_enum,
    meta_author_type                    account_type_enum,
    meta_author_name                    varchar,
    meta_author_id                      integer,
    meta_sender_id                      integer,
    meta_sender_name                    varchar,
    meta_receiver_id                    integer,
    meta_receiver_name                  varchar,
    meta_sender_type                    order_actor_type_enum,
    meta_receiver_type                  order_actor_type_enum,
    meta_is_hidden_from_reporting       boolean,
    historical_grid_id                  integer,
    directive_id                        bigint,
    author_id                           integer,
    rls_organization_id                 integer
);

CREATE INDEX IF NOT EXISTS orders_historical_grid_id_idx ON orders (historical_grid_id);
CREATE INDEX IF NOT EXISTS orders_rls_organization_id_idx ON orders (rls_organization_id);
CREATE INDEX IF NOT EXISTS orders_meta_receiver_id_idx ON orders (meta_receiver_id);
CREATE INDEX IF NOT EXISTS orders_updated_at_idx ON orders (updated_at DESC);

-- ── Equipment Directives ──────────────────────────────────────────────────────
-- Used by equipment-control MCP to track issued commands.

CREATE TABLE IF NOT EXISTS directives (
    id                                  bigint NOT NULL PRIMARY KEY,
    created_at                          timestamptz NOT NULL DEFAULT now(),
    updated_at                          timestamptz,
    directive_type                      directive_type_enum NOT NULL,
    directive_status                    directive_status_enum NOT NULL DEFAULT 'PENDING',
    directive_priority                  integer NOT NULL DEFAULT 0,
    external_reference                  varchar,
    is_on                               boolean,
    kwh                                 double precision,
    kwh_credit_available                double precision,
    power_limit                         integer,
    token                               varchar,
    execution_session                   varchar,
    can_be_retried                      boolean,
    meter_id                            integer,
    order_id                            integer,
    author_id                           integer,
    rls_organization_id                 integer
);

CREATE INDEX IF NOT EXISTS directives_meter_status_idx ON directives (meter_id, directive_status, created_at);
CREATE INDEX IF NOT EXISTS directives_status_idx ON directives (directive_status);
CREATE INDEX IF NOT EXISTS directives_rls_organization_id_idx ON directives (rls_organization_id);

-- ── NOTE: Read-only access ────────────────────────────────────────────────────
-- Grant your Anansi bot user SELECT-only on all tables:
--
--   CREATE ROLE anansi_readonly LOGIN PASSWORD 'replace-me'; -- pragma: allowlist secret
--   GRANT CONNECT ON DATABASE your_db TO anansi_readonly;
--   GRANT USAGE ON SCHEMA public TO anansi_readonly;
--   GRANT SELECT ON ALL TABLES IN SCHEMA public TO anansi_readonly;
--   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO anansi_readonly;
