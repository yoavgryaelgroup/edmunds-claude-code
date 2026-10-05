-- CALCE (University of Maryland) battery data, https://calce.umd.edu/battery-data
-- Every table is prefixed calce_ so it can live in public next to other battery tables.

-- One row per downloaded archive (one link on the CALCE page).
CREATE TABLE IF NOT EXISTS calce_datasets (
    dataset      text PRIMARY KEY,               -- archive name without .zip, e.g. 'CS2_35'
    url          text NOT NULL,
    cell_type    text,                           -- 'INR 18650-20R', 'A123', 'CS2', 'CX2', 'PL', 'PLN (storage)'
    page_section text,                           -- link text and heading it sits under on the CALCE page
    size_bytes   bigint
);

-- One row per sheet of a spreadsheet, or per text / csv / mat file, inside an archive.
CREATE TABLE IF NOT EXISTS calce_files (
    file_id      serial PRIMARY KEY,
    dataset      text NOT NULL REFERENCES calce_datasets ON DELETE CASCADE,
    member_path  text NOT NULL,                  -- path inside the zip
    sheet_name   text NOT NULL DEFAULT '',       -- '' for non-spreadsheet files
    cell         text,                           -- cell named in the path, e.g. 'CS2_35', 'PL12', 'PLN59', 'A1-007'
    kind         text NOT NULL,                  -- target table: arbin, arbin_statistics, cadex, pl_mat, impedance,
                                                 -- temperature_log, generic, text
    header       text,                           -- column header row / instrument header as found in the file
    text_content text,                           -- full content of readme-style text files
    n_rows       integer,
    loaded_at    timestamptz,
    UNIQUE (dataset, member_path, sheet_name)
);

-- Arbin tester "Channel_*" sheets: the main time series for INR, A123, CS2, CX2 and PLN cells.
CREATE TABLE IF NOT EXISTS calce_arbin_measurements (
    file_id                 integer NOT NULL REFERENCES calce_files ON DELETE CASCADE,
    row_index               integer NOT NULL,    -- 0-based data row within the sheet
    data_point              integer,
    test_time_s             double precision,
    date_time               timestamp,
    step_time_s             double precision,
    step_index              integer,
    cycle_index             integer,
    current_a               real,
    voltage_v               real,
    charge_capacity_ah      real,
    discharge_capacity_ah   real,
    charge_energy_wh        real,
    discharge_energy_wh     real,
    dv_dt_v_per_s           real,
    internal_resistance_ohm real,
    is_fc_data              real,
    ac_impedance_ohm        real,
    aci_phase_angle_deg     real,
    temperature_1_c         real,                -- Temperature (C)_1, A123 files only
    temperature_2_c         real,                -- Temperature (C)_2
    PRIMARY KEY (file_id, row_index)
);

-- Arbin "Statistics_*" sheets: one summary row per cycle.
CREATE TABLE IF NOT EXISTS calce_arbin_statistics (
    file_id                 integer NOT NULL REFERENCES calce_files ON DELETE CASCADE,
    row_index               integer NOT NULL,
    cycle_index             integer,
    test_time_s             double precision,
    date_time               timestamp,
    current_a               real,
    voltage_v               real,
    charge_capacity_ah      real,
    discharge_capacity_ah   real,
    charge_energy_wh        real,
    discharge_energy_wh     real,
    internal_resistance_ohm real,
    ac_impedance_ohm        real,
    aci_phase_angle_deg     real,
    charge_time_s           double precision,
    discharge_time_s        double precision,
    vmax_on_cycle_v         real,
    PRIMARY KEY (file_id, row_index)
);

-- CADEX tester .txt exports (CS2_8, CS2_21, CX2_4, CX2_31). Columns as named in the file.
CREATE TABLE IF NOT EXISTS calce_cadex_measurements (
    file_id          integer NOT NULL REFERENCES calce_files ON DELETE CASCADE,
    row_index        integer NOT NULL,
    time             double precision,           -- "Time" as exported
    status_code      integer,
    status_category  integer,
    status_color     integer,
    pgm_code         integer,
    pgm_step         integer,
    pgm_para         integer,
    pgm_cycle        integer,
    voltage_mv       double precision,           -- mV
    current_ma       double precision,           -- mA
    temperature      double precision,
    duration_s       double precision,           -- Duration
    charge_count     integer,
    discharge_count  integer,
    capacity         double precision,
    analog_input_1   double precision,
    analog_input_2   double precision,
    analog_input_3   double precision,
    analog_input_4   double precision,
    digital_input_1  integer,
    digital_input_2  integer,
    digital_input_3  integer,
    digital_input_4  integer,
    digital_output_1 integer,
    digital_output_2 integer,
    digital_output_3 integer,
    digital_output_4 integer,
    analog_output_1  double precision,
    analog_output_2  double precision,
    PRIMARY KEY (file_id, row_index)
);

-- PL pouch cells (.mat): each file is a list of operations, each holding an Arbin table.
CREATE TABLE IF NOT EXISTS calce_pl_measurements (
    file_id          integer NOT NULL REFERENCES calce_files ON DELETE CASCADE,
    operation_index  integer NOT NULL,           -- 1-based row of the operation list
    operation        text,                       -- e.g. '1st Charge', '101 Full Cycles'
    start_date       date,
    row_index        integer NOT NULL,
    time_s           double precision,
    date_time        timestamp,                  -- converted from MATLAB datenum
    step             integer,
    cycle            integer,
    current_a        double precision,
    voltage_v        double precision,
    charge_ah        double precision,
    discharge_ah     double precision,
    PRIMARY KEY (file_id, operation_index, row_index)
);

-- PLN storage-test EIS files (.csv without a header row; column meaning inferred from the values:
-- |Z| = sqrt(re^2 + im^2) and phase = atan(im / re) hold for every row).
CREATE TABLE IF NOT EXISTS calce_impedance_points (
    file_id      integer NOT NULL REFERENCES calce_files ON DELETE CASCADE,
    row_index    integer NOT NULL,
    frequency_hz double precision,               -- column 1
    z_real_ohm   double precision,               -- column 2
    z_imag_ohm   double precision,               -- column 3
    z_mod_ohm    double precision,               -- column 4
    phase_deg    double precision,               -- column 5
    PRIMARY KEY (file_id, row_index)
);

-- Agilent 34970A thermocouple / voltage logs (CX2_4 .csv), one row per scan and channel.
CREATE TABLE IF NOT EXISTS calce_temperature_logs (
    file_id    integer NOT NULL REFERENCES calce_files ON DELETE CASCADE,
    row_index  integer NOT NULL,
    scan       integer,
    time       timestamp,
    channel    text NOT NULL,                    -- e.g. '101 (C)', '102 (VDC)'
    value      double precision,
    alarm      integer,
    PRIMARY KEY (file_id, row_index, channel)
);

-- Everything else, cell for cell: Arbin Info sheets, Sheet1/Channel_Chart copies, the INR low-current
-- OCV sheets, the PLN storage summary, %LOSS sheets, etc.
CREATE TABLE IF NOT EXISTS calce_generic_rows (
    file_id    integer NOT NULL REFERENCES calce_files ON DELETE CASCADE,
    row_index  integer NOT NULL,                 -- 0-based, header row included
    cells      jsonb NOT NULL,                   -- the row as an array of values
    PRIMARY KEY (file_id, row_index)
);
