# CALCE battery data → PostgreSQL

Imports every dataset linked from the [CALCE battery data page](https://calce.umd.edu/battery-data)
(University of Maryland): INR 18650-20R, A123, CS2, CX2, PL and PLN storage cells. That's 89 zip archives,
about 4 GB.

```bash
pip install python-calamine mat-io numpy "psycopg[binary]"
DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/battery_aging python load.py
```

The script reads the CALCE page, downloads each archive into `raw/` (resuming and re-checking any
incomplete download), creates the tables in `schema.sql` and loads every file. Tables go in the `public`
schema (set `DB_SCHEMA` to change it), and all of them are prefixed `calce_`. Use `--only CS2_35 SP2_0C_DST`
to load specific archives. Each file inside an archive is committed on its own, so an interrupted run can
just be started again.

## Tables

| table | contents |
|---|---|
| `calce_datasets` | one row per archive: URL, cell type, where it sits on the page |
| `calce_files` | one row per sheet / file inside an archive: path, cell name, which table it went to, header |
| `calce_arbin_measurements` | Arbin tester time series (`Channel_*` sheets): test time, date, step, cycle, current, voltage, capacities, energies, resistance, temperatures |
| `calce_arbin_statistics` | Arbin per-cycle summaries (`Statistics_*` sheets) |
| `calce_cadex_measurements` | CADEX tester `.txt` exports (CS2_8, CS2_21, CX2_4, CX2_31) |
| `calce_pl_measurements` | PL pouch-cell `.mat` files: one table per charge/discharge operation |
| `calce_impedance_points` | PLN storage-test EIS sweeps (`.csv`) |
| `calce_temperature_logs` | CX2_4 thermocouple logs (Agilent 34970A `.csv`) |
| `calce_generic_rows` | everything else, row by row as JSON arrays: Arbin `Info` sheets, the `Sheet1` / `Channel_Chart` copies, INR low-current OCV sheets, PLN storage summary, `%LOSS` sheets, PL operation lists |
| `calce_files.text_content` | readme-style `.txt` files |

Join measurement tables to `calce_files` on `file_id` to get the archive, file, sheet and cell, for example:

```sql
SELECT f.cell, m.cycle_index, max(m.discharge_capacity_ah) AS capacity_ah
FROM calce_arbin_measurements m JOIN calce_files f USING (file_id)
WHERE f.cell = 'CS2_35'
GROUP BY 1, 2 ORDER BY 2;
```

Notes:
- Excel lock files (`~$…`), `Thumbs.db` and `desktop.ini` in the archives are skipped.
- The PLN impedance `.csv` files have no header row. The column names (frequency, Z real, Z imaginary, |Z|,
  phase) are inferred from the values, which satisfy |Z| = √(re² + im²) and phase = atan(im / re).
- PL `.mat` files store MATLAB `table` objects, read with `mat-io`. `Date_Time` (MATLAB datenum) is converted
  to a timestamp rounded to the millisecond.
