-- TimescaleDB Schema (Optional — for time-series energy data)
-- Only required if you use the Grafana / historical energy features.
-- Create this in a TimescaleDB instance and point TIMESCALE_* env vars at it.
--
-- Anansi reads this database but never writes to it.
-- Your monitoring/SCADA system is responsible for inserting records.

-- ── Grid energy snapshots ──────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS grid_energy_snapshot_15_min (
    created_at                          timestamp NOT NULL,    -- UTC, no tz (TimescaleDB convention)
    grid_id                             integer NOT NULL,
    grid_name                           varchar,
    organization_id                     integer,
    organization_name                   varchar,
    -- State flags
    is_fs_active                        boolean,   -- Full Service active
    is_hps_on                           boolean,   -- HPS (High Power Supply) active
    should_fs_be_on                     boolean,
    is_curtailing                       boolean,
    -- Battery
    battery_soc_bs_pct                  double precision,
    battery_voltage_bv_v                double precision,
    battery_current_bc_a                double precision,
    battery_charging_state_bst_enum     double precision,
    battery_charge_current_limit_mcc_a  double precision,
    battery_capacity_ca_ah              double precision,
    battery_temperature_bt_c            double precision,
    -- PV / Solar
    pv_power_dc_pdc_w                   double precision,
    pv_energy_to_battery_pb_kwh         double precision,
    pv_energy_to_grid_pc_kwh            double precision,
    -- Grid consumption (output phases)
    grid_consumption_total_kwh          double precision,
    grid_l1_power_consumption_output_o1_w double precision,
    grid_l2_power_consumption_output_o2_w double precision,
    grid_l3_power_consumption_output_o3_w double precision
    -- Add additional columns from your monitoring system as needed
);

-- Convert to hypertable (TimescaleDB)
SELECT create_hypertable(
    'grid_energy_snapshot_15_min',
    'created_at',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS grid_energy_grid_id_time_idx
    ON grid_energy_snapshot_15_min (grid_id, created_at DESC);

-- ── Full Service / HPS events ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS fs_events (
    created_at      timestamp NOT NULL,
    grid_id         integer NOT NULL,
    grid_name       varchar,
    event_type      varchar NOT NULL,   -- fs_on | fs_off | hps_on | hps_off
    triggered_by    varchar             -- auto | manual | scheduled
);

SELECT create_hypertable('fs_events', 'created_at', if_not_exists => TRUE);

-- ── NOTE ──────────────────────────────────────────────────────────────────────
-- TimescaleDB is optional. If you do not have time-series energy data,
-- leave TIMESCALE_HOST unset and the Grafana / historical features will
-- gracefully degrade or be disabled.
