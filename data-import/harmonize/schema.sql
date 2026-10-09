-- Phase 4 of the battery data platform: one model for all datasets.
-- Units: A, V, Ah, Wh, W, °C, Ω, seconds. Current is positive while charging, negative while discharging.
-- Every test points back to the staging tables (and so to the raw files) it was built from.

CREATE TABLE IF NOT EXISTS dataset (
    dataset_key    text PRIMARY KEY,               -- 'mendeley:wykht8y7tg'
    source_ids     integer[],                      -- ingest.source rows
    catalog_refs   text,                           -- local references in the workbook, e.g. 'Dataset - 14'
    title          text,
    mapping_file   text,
    mapping_sha256 text,
    notes          text,                           -- assumptions and caveats from the mapping file
    loaded_at      timestamptz
);
-- Added after the first release; ALTER keeps databases created earlier in step.
ALTER TABLE dataset ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'harmonized';

CREATE TABLE IF NOT EXISTS cell (
    cell_uid             text PRIMARY KEY,         -- dataset_key + ':' + the source's own cell label
    dataset_key          text NOT NULL REFERENCES dataset ON DELETE CASCADE,
    source_label         text NOT NULL,
    manufacturer         text,
    model                text,
    chemistry            text,
    form_factor          text,
    nominal_capacity_ah  real,
    is_synthetic         boolean NOT NULL DEFAULT false,
    attributes           jsonb                     -- anything else the dataset says about the cell
);

CREATE TABLE IF NOT EXISTS test (
    test_id         serial PRIMARY KEY,
    cell_uid        text NOT NULL REFERENCES cell ON DELETE CASCADE,
    name            text NOT NULL,                 -- usually the source file and table
    test_type       text NOT NULL CHECK (test_type IN ('cycle_aging', 'calendar_aging', 'drive_cycle', 'ocv',
                        'eis', 'rate_capability', 'characterization', 'synthetic')),
    ambient_temp_c  real,
    attributes      jsonb,                         -- e.g. soc_pct, profile, aging stage
    staging_tables  integer[],                     -- staging.source_table ids this test was built from
    UNIQUE (cell_uid, name)
);

CREATE TABLE IF NOT EXISTS timeseries (
    test_id         integer NOT NULL REFERENCES test ON DELETE CASCADE,
    seq             integer NOT NULL,              -- order within the test
    t_s             double precision,              -- seconds since the start of the test
    step_time_s     double precision,              -- seconds since the start of the step, when that's all the source gives
    ts              timestamp,                     -- wall-clock time when the source gives it
    cycle_number    integer,
    step_number     integer,
    step_label      text,                          -- source's step status, e.g. CHA / DCH / PAU
    current_a       real,
    voltage_v       real,
    temperature_c   real,                          -- cell temperature
    chamber_temp_c  real,
    capacity_ah     real,                          -- accumulated capacity as reported by the tester
    energy_wh       real,
    power_w         real,
    soc             real,                          -- 0..1, when the source reports it
    PRIMARY KEY (test_id, seq)
);

CREATE TABLE IF NOT EXISTS cycle (
    test_id          integer NOT NULL REFERENCES test ON DELETE CASCADE,
    cycle_number     integer NOT NULL,
    origin           text NOT NULL CHECK (origin IN ('reported', 'derived')),
                     -- reported: the dataset gives this number; derived: computed from core.timeseries
    charge_ah        real,
    discharge_ah     real,
    soh_pct          real,
    temp_max_c       real,
    duration_s       double precision,
    n_samples        integer,
    PRIMARY KEY (test_id, cycle_number, origin)
);

CREATE TABLE IF NOT EXISTS eis_point (
    test_id        integer NOT NULL REFERENCES test ON DELETE CASCADE,
    seq            integer NOT NULL,
    frequency_hz   double precision,
    z_re_ohm       double precision,
    z_im_ohm       double precision,             -- Im(Z) as measured: negative for capacitive behaviour
    PRIMARY KEY (test_id, seq)
);

-- Capacity / resistance at checkpoints of an aging test (calendar or cycle aging), as reported by the dataset.
CREATE TABLE IF NOT EXISTS checkup (
    test_id         integer NOT NULL REFERENCES test ON DELETE CASCADE,
    checkup_index   integer NOT NULL,              -- order of the checkpoint within the test
    origin          text NOT NULL DEFAULT 'reported',
    elapsed_h       double precision,              -- storage / test time at the checkpoint
    efc             double precision,              -- equivalent full cycles at the checkpoint
    capacity_ah     real,
    resistance_ohm  real,
    soh_pct         real,
    PRIMARY KEY (test_id, checkup_index, origin)
);

-- Results of the physical-range checks run after each dataset is loaded.
CREATE TABLE IF NOT EXISTS quality_issue (
    dataset_key  text NOT NULL REFERENCES dataset ON DELETE CASCADE,
    test_id      integer REFERENCES test ON DELETE CASCADE,
    check_name   text NOT NULL,
    n_rows       bigint,
    detail       text
);

-- Per cell: what is loaded for it.
CREATE OR REPLACE VIEW v_cell_summary AS
SELECT c.cell_uid, c.dataset_key, d.catalog_refs, c.manufacturer, c.model, c.chemistry, c.nominal_capacity_ah,
       count(DISTINCT t.test_id) AS tests,
       string_agg(DISTINCT t.test_type, ', ') AS test_types,
       (SELECT count(*) FROM timeseries s WHERE s.test_id = ANY (array_agg(t.test_id))) AS samples,
       (SELECT count(*) FROM cycle y WHERE y.test_id = ANY (array_agg(t.test_id))) AS cycles,
       (SELECT count(*) FROM eis_point e WHERE e.test_id = ANY (array_agg(t.test_id))) AS eis_points
FROM cell c JOIN dataset d USING (dataset_key) LEFT JOIN test t USING (cell_uid)
GROUP BY c.cell_uid, d.catalog_refs;

-- Every mapped dataset: harmonized or not (and why), with what was loaded.
CREATE OR REPLACE VIEW v_dataset_status AS
SELECT d.dataset_key, d.catalog_refs, d.title, d.status,
       (SELECT count(*) FROM cell c WHERE c.dataset_key = d.dataset_key) AS cells,
       (SELECT count(*) FROM test t JOIN cell c USING (cell_uid) WHERE c.dataset_key = d.dataset_key) AS tests,
       d.notes, d.loaded_at
FROM dataset d;
