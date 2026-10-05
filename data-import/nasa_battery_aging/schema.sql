-- NASA Ames PCoE Li-ion Battery Aging datasets (BatteryAgingARC), batteries B0025-B0028, B0045-B0056.
-- Source .mat layout: <name>.cycle(i) with type / ambient_temperature / time / data.

CREATE TABLE IF NOT EXISTS batteries (
    battery_id         text PRIMARY KEY,          -- 'B0025'
    source_archive     text,                      -- zip the file came from
    readme             text,                      -- the matching README's data description
    ambient_temp_c     real,                      -- nominal test temperature from the README
    discharge_profile  text,                      -- e.g. '4A 0.05Hz square wave', '2A constant'
    cutoff_voltage_v   real,                      -- discharge stopped at this voltage
    loaded_at          timestamptz
);

-- One row per entry of the .cycle struct array (charge, discharge or impedance run).
CREATE TABLE IF NOT EXISTS cycles (
    battery_id      text NOT NULL REFERENCES batteries ON DELETE CASCADE,
    cycle_index     integer NOT NULL,             -- 1-based position in the .cycle array
    type            text NOT NULL CHECK (type IN ('charge', 'discharge', 'impedance')),
    type_index      integer NOT NULL,             -- 1-based count within this battery and type
    ambient_temp_c  real,
    start_time      timestamp,                    -- from the MATLAB date vector
    n_points        integer,
    capacity_ah     double precision,             -- discharge only
    re_ohm          double precision,             -- impedance only: electrolyte resistance
    re_ohm_im       double precision,             -- imaginary part; non-zero only where the source fit is complex
    rct_ohm         double precision,             -- impedance only: charge transfer resistance
    rct_ohm_im      double precision,
    PRIMARY KEY (battery_id, cycle_index)
);

-- Time series of charge and discharge cycles.
CREATE TABLE IF NOT EXISTS measurements (
    battery_id       text NOT NULL,
    cycle_index      integer NOT NULL,
    point_index      integer NOT NULL,            -- 0-based sample index within the cycle
    time_s           double precision,            -- Time
    voltage_v        double precision,            -- Voltage_measured (battery terminal)
    current_a        double precision,            -- Current_measured (battery output)
    temperature_c    double precision,            -- Temperature_measured
    current_charge_a double precision,            -- Current_charge (charger), charge cycles
    voltage_charge_v double precision,            -- Voltage_charge (charger), charge cycles
    current_load_a   double precision,            -- Current_load (load), discharge cycles
    voltage_load_v   double precision,            -- Voltage_load (load), discharge cycles
    PRIMARY KEY (battery_id, cycle_index, point_index),
    FOREIGN KEY (battery_id, cycle_index) REFERENCES cycles ON DELETE CASCADE
);

-- EIS sweep of impedance cycles (complex values split into real / imaginary parts).
CREATE TABLE IF NOT EXISTS impedance_points (
    battery_id             text NOT NULL,
    cycle_index            integer NOT NULL,
    point_index            integer NOT NULL,
    sense_current_re       double precision,
    sense_current_im       double precision,
    battery_current_re     double precision,
    battery_current_im     double precision,
    current_ratio_re       double precision,
    current_ratio_im       double precision,
    battery_impedance_re   double precision,      -- ohms, computed from raw data
    battery_impedance_im   double precision,
    PRIMARY KEY (battery_id, cycle_index, point_index),
    FOREIGN KEY (battery_id, cycle_index) REFERENCES cycles ON DELETE CASCADE
);

-- Rectified_Impedance: calibrated and smoothed impedance (its length differs from the raw sweep).
CREATE TABLE IF NOT EXISTS rectified_impedance_points (
    battery_id   text NOT NULL,
    cycle_index  integer NOT NULL,
    point_index  integer NOT NULL,
    impedance_re double precision,
    impedance_im double precision,
    PRIMARY KEY (battery_id, cycle_index, point_index),
    FOREIGN KEY (battery_id, cycle_index) REFERENCES cycles ON DELETE CASCADE
);
