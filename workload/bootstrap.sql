\set ON_ERROR_STOP on

DO $bootstrap$
DECLARE
    schema_marker constant text := 'aurora_tuning_simulator_owned:v1';
    existing_marker text;
BEGIN
    SELECT obj_description(oid, 'pg_namespace')
      INTO existing_marker
      FROM pg_namespace
     WHERE nspname = 'aurora_tuning_simulator';

    IF FOUND AND existing_marker IS DISTINCT FROM schema_marker THEN
        RAISE EXCEPTION
            'schema aurora_tuning_simulator exists without simulator ownership marker';
    END IF;

    IF NOT FOUND THEN
        CREATE SCHEMA aurora_tuning_simulator;
        COMMENT ON SCHEMA aurora_tuning_simulator IS 'aurora_tuning_simulator_owned:v1';
    END IF;
END
$bootstrap$;

CREATE TABLE IF NOT EXISTS aurora_tuning_simulator.simulator_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    format_version integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    target_bytes bigint NOT NULL DEFAULT 0,
    batches_completed bigint NOT NULL DEFAULT 0,
    rows_inserted bigint NOT NULL DEFAULT 0,
    last_application_prefix text
);

INSERT INTO aurora_tuning_simulator.simulator_state (singleton, format_version)
VALUES (true, 1)
ON CONFLICT (singleton) DO UPDATE
SET format_version = EXCLUDED.format_version,
    updated_at = clock_timestamp();

CREATE TABLE IF NOT EXISTS aurora_tuning_simulator.customers (
    customer_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id integer NOT NULL,
    status text NOT NULL DEFAULT 'active',
    mutable_counter bigint NOT NULL DEFAULT 0,
    indexed_score integer NOT NULL DEFAULT 0,
    profile jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
) WITH (fillfactor = 70, autovacuum_vacuum_scale_factor = 0.03,
        autovacuum_analyze_scale_factor = 0.02);

ALTER TABLE aurora_tuning_simulator.customers
    ALTER COLUMN payload SET STORAGE EXTERNAL;

CREATE INDEX IF NOT EXISTS customers_tenant_status_idx
    ON aurora_tuning_simulator.customers (tenant_id, status);
CREATE INDEX IF NOT EXISTS customers_score_idx
    ON aurora_tuning_simulator.customers (indexed_score);
CREATE INDEX IF NOT EXISTS customers_created_brin_idx
    ON aurora_tuning_simulator.customers USING brin (created_at);

CREATE TABLE IF NOT EXISTS aurora_tuning_simulator.events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id bigint NOT NULL,
    tenant_id integer NOT NULL,
    event_type text NOT NULL,
    amount numeric(12, 2) NOT NULL,
    searchable_token text NOT NULL,
    payload text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
) WITH (fillfactor = 90, autovacuum_vacuum_scale_factor = 0.02,
        autovacuum_analyze_scale_factor = 0.01);

ALTER TABLE aurora_tuning_simulator.events
    ALTER COLUMN payload SET STORAGE EXTERNAL;

CREATE INDEX IF NOT EXISTS events_customer_created_idx
    ON aurora_tuning_simulator.events (customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS events_tenant_type_idx
    ON aurora_tuning_simulator.events (tenant_id, event_type);
CREATE INDEX IF NOT EXISTS events_created_brin_idx
    ON aurora_tuning_simulator.events USING brin (created_at);

CREATE TABLE IF NOT EXISTS aurora_tuning_simulator.lock_targets (
    lock_id integer PRIMARY KEY,
    touches bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO aurora_tuning_simulator.lock_targets (lock_id)
SELECT value FROM generate_series(1, 8) AS value
ON CONFLICT (lock_id) DO NOTHING;

CREATE OR REPLACE FUNCTION aurora_tuning_simulator.try_bounded_lock(p_lock_id integer)
RETURNS boolean
LANGUAGE plpgsql
AS $function$
BEGIN
    PERFORM set_config('lock_timeout', '750ms', true);
    UPDATE aurora_tuning_simulator.lock_targets
       SET touches = touches + 1,
           updated_at = clock_timestamp()
     WHERE lock_id = p_lock_id;
    RETURN true;
EXCEPTION
    WHEN lock_not_available OR query_canceled THEN
        RETURN false;
END
$function$;
