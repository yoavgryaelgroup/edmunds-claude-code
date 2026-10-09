# Harmonized core tables (phase 4)

Turns the staging tables (phase 3) into one model shared by all datasets, in the `core` schema:

| table | contents |
|---|---|
| `dataset` | one row per mapped dataset, with its catalog references, mapping file and notes |
| `cell` | one row per physical cell: manufacturer, model, chemistry, form factor, nominal capacity, extra attributes |
| `test` | one row per test of a cell: type (`drive_cycle`, `eis`, `characterization`, `cycle_aging`, `ocv`, …), ambient temperature, attributes (SOC, profile, aging stage …), and the staging tables it came from |
| `timeseries` | samples: time, step time, timestamp, cycle, step, current, voltage, temperatures, capacity, energy, power, SOC |
| `cycle` | per-cycle values, either `reported` by the dataset or `derived` from the time series |
| `eis_point` | impedance spectra: frequency, Re(Z), Im(Z) |
| `quality_issue` | rows that failed a physical-range check, per test |
| `v_cell_summary` | per cell: number of tests, samples, cycles and EIS points |

Units are A, V, Ah, Wh, W, °C, Ω and seconds. Current is positive while charging and negative while
discharging. Im(Z) is stored as measured (negative where the cell behaves capacitively).

```bash
pip install pyyaml "psycopg[binary]"
DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/battery_aging python harmonize.py mappings/*.yaml
```

Each mapping file is loaded in its own transaction and replaces whatever was loaded before for that dataset,
so re-running is safe. A mapping whose dataset hasn't been parsed into staging yet fails with "matched no
staging tables" and the others still load.

## Mapping files

One YAML file per dataset in `mappings/`. Adding a dataset means writing one of these, not code:

```yaml
dataset_key: mendeley:rnvb47fp4t      # platform:id
source_match: rnvb47fp4t              # text contained in the dataset's URL in ingest.source
title: ...
notes: >                              # assumptions, quirks, what is not loaded and why
  ...
cell_defaults: {manufacturer: ..., chemistry: ..., nominal_capacity_ah: ...}
cell_overrides:                       # optional, first match on the cell label wins
  - {match: '^LFP', chemistry: LFP, nominal_capacity_ah: 2.5}
tables:
  - label: what this rule loads
    produce: timeseries               # timeseries | eis | cycle | cell_attributes
    where: {kind: mat, path: '\.mat$', has_columns: [t, volt, current], table: '^Sheet', exclude_path: ...}
    data_start: 0                     # optional, override staging's first data row
    cell: {path: 'regex with (group)'}  # or {const: X}, {col: LocID} (one cell per row value), template: 'Cell{}'
    test:
      name: {const: capacity test}    # default: file path (+ sheet / variable)
      type: {const: drive_cycle}      # or path_map: [[regex, type], ...] with default
      ambient_temp_c: {path: '(-?\d+)degC', number: true}
      attributes: {soc_pct: {path: '(\d+)%SOC', number: true}}
    columns:                          # core field: source column by name (col) or position (index)
      t_s: {col: t}
      current_a: {col: current, sign: -1}       # sign, scale, offset; parse: hms | datetime | text
      voltage_v: {col: Vol.mV, scale: 0.001}
```

`produce: cycle` also accepts a `wide:` layout, with one column group per cell and a units row naming the
fields. See the Warwick mapping for an example.

## Mapped so far

| catalog ref | dataset | what is loaded |
|---|---|---|
| Dataset - 14 | Panasonic 18650PF (Wisconsin) | drive cycles, HPPC, charges, OCV and 1C tests at −20…25 °C (EIS not yet: units unconfirmed) |
| Dataset - 49 | Indiana impedance data | 6 EIS spectra at 3.2–4.2 V |
| Dataset - 58 | LG M50 degradation (Warwick) | capacity checks, EIS at 3 temperatures × SOCs, capacity and SOH per aging cycle |
| Dataset - 69 | Chang'an drive cycles | LFP / NCA / NMC cells under DST, FUDS, US06, UDDS, BJDST and mixed profiles |
| Dataset - 71 | CMU second-life LiFePO4 | cell metadata, capacity tests, EIS |
| Dataset - 79 | EIS vs SOC and temperature | 1,102 spectra on 106 cells |

Checks on these: current signs agree with SOC change (Chang'an, correlation +0.98 after flipping the source's
sign), every impedance spectrum has negative Im(Z) at its lowest frequency, and derived capacities match what
the files report (CMU maximum 2.288 Ah = 2,288 mAh in the source). The only remaining quality flags are 9 rows
in Warwick capacity checks where the tester logged a time 2 s earlier than the row before.

```sql
-- Capacity fade per cell, all mapped datasets
SELECT c.cell_uid, c.chemistry, y.cycle_number, y.discharge_ah, y.soh_pct
FROM core.cycle y JOIN core.test t USING (test_id) JOIN core.cell c USING (cell_uid)
WHERE y.origin = 'reported' ORDER BY 1, 3;

-- Impedance spectra at 25 °C, 50 % SOC
SELECT c.cell_uid, e.frequency_hz, e.z_re_ohm, e.z_im_ohm
FROM core.eis_point e JOIN core.test t USING (test_id) JOIN core.cell c USING (cell_uid)
WHERE t.ambient_temp_c = 25 AND (t.attributes->>'soc_pct')::numeric = 50;
```
