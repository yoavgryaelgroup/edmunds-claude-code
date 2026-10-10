# Battery Degradation Dataset → PostgreSQL

Imports [Battery Degradation Dataset (Fixed Current Profiles & Arbitrary Uses Profiles), v2](https://data.mendeley.com/datasets/kw34hhw7xg/2)
(Lu, Xiong, Tian et al., doi:10.17632/kw34hhw7xg.2, CC BY 4.0) into PostgreSQL.

77 18650 Li-ion cells (no data files for #10, #13, #16, #19 in v2); 145 cycler exports (`.xlsx`, ~2.4 GB) plus `Readme.txt` listing each cell's charge/discharge rate.

```bash
pip install python-calamine "psycopg[binary]"
DATABASE_URL=postgresql://user:pass@host:5432/dbname python load.py
```

The script downloads all files to `raw/` (sha256-verified), creates the tables in `schema.sql`, and
COPYs each sheet into `measurements`. It is resumable: files already loaded are skipped.

## Tables

| table | contents |
|---|---|
| `batteries` | one row per cell: group (fixed / arbitrary profile), charge rate, discharge rate |
| `source_files` | one row per Excel file, with checksum and row count |
| `measurements` | every logged data point: test time (s), current, capacity, voltage, energy, temperature, timestamp, cycle index |

`Test_Time(s)` is stored in the source as `HH:MM:SS` or `D-HH:MM:SS`; it's converted to seconds.
`cycle_index` restarts in each file (some cells have a "first 20 cycles" file and a later cycling file).
The dataset's `desktop.ini` (a Windows artifact) is ignored.
