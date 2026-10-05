# NASA Battery Aging (BatteryAgingARC) → PostgreSQL

Imports the NASA Ames Prognostics Center of Excellence Li-ion battery aging `.mat` files
(B0025–B0028, B0045–B0056) into PostgreSQL.

```bash
pip install scipy numpy "psycopg[binary]"
DATABASE_URL=postgresql://user:pass@host:5432/battery_aging python load.py \
    2._BatteryAgingARC_25_26_27_28_P1.zip 4._BatteryAgingARC_45_46_47_48.zip \
    5._BatteryAgingARC_49_50_51_52.zip 6._BatteryAgingARC_53_54_55_56.zip
```

Arguments are the original zips or folders containing the extracted files. Tables come from `schema.sql`.
Loading a battery again replaces its rows.

## Tables

| table | contents |
|---|---|
| `batteries` | one row per cell, plus README info: ambient temperature, discharge profile, cutoff voltage |
| `cycles` | one row per charge / discharge / impedance run: start time, ambient temp, capacity (discharge), Re / Rct (impedance) |
| `measurements` | charge and discharge time series: time, voltage, current, temperature, charger or load current/voltage |
| `impedance_points` | EIS sweep: sense / battery current, current ratio, battery impedance (real + imaginary) |
| `rectified_impedance_points` | calibrated and smoothed impedance (real + imaginary) |

Join on `(battery_id, cycle_index)`, where `cycle_index` is the 1-based position in the `.cycle` array.
For B0049 and B0051, some `Re`/`Rct` fits in the source are complex conjugate pairs. Their imaginary parts are
kept in `re_ohm_im` / `rct_ohm_im`. The anomalous low-voltage and low-capacity runs that the READMEs mention
(e.g. B0050, B0052) are loaded as they are.
