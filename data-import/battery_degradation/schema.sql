-- Battery Degradation Dataset (Fixed Current Profiles & Arbitrary Uses Profiles), v2
-- https://data.mendeley.com/datasets/kw34hhw7xg/2  (CC BY 4.0)

CREATE TABLE IF NOT EXISTS batteries (
    battery_id     integer PRIMARY KEY,         -- "#N" in the dataset
    profile_group  text NOT NULL,               -- 'Cycled with Fixed Current Profiles' | 'Cycled with Arbitrary Uses Profiles'
    charge_rate    text,                        -- '1C' | '2C' | '3C' | 'Random' (from Readme.txt)
    discharge_rate text
);

CREATE TABLE IF NOT EXISTS source_files (
    file_id    serial PRIMARY KEY,
    battery_id integer NOT NULL REFERENCES batteries,
    filename   text NOT NULL,
    sheet_name text,
    sha256     text NOT NULL,
    size_bytes bigint,
    row_count  integer,
    loaded_at  timestamptz,
    UNIQUE (battery_id, filename)
);

-- One row per logged data point of a cycler export (columns of sheet 记录表).
CREATE TABLE IF NOT EXISTS measurements (
    file_id       integer NOT NULL REFERENCES source_files ON DELETE CASCADE,
    battery_id    integer NOT NULL,
    data_point    integer NOT NULL,             -- Data_Point
    test_time_s   double precision NOT NULL,    -- Test_Time(s), converted to seconds
    current_a     real,                         -- Current(A)
    capacity_ah   real,                         -- Capacity(Ah)
    voltage_v     real,                         -- Voltage(V)
    energy_wh     real,                         -- Energy(Wh)
    temperature_c real,                         -- Temperature(℃)
    date_time     timestamp,                    -- Date_Time
    cycle_index   integer,                      -- Cycle_Index (per file)
    PRIMARY KEY (file_id, data_point)
);
