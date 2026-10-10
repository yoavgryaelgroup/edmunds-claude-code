# Staging: downloaded files read into the database (phase 3)

Reads every file downloaded in phase 2 (and every file inside downloaded zip / tar archives) into a `staging`
schema, exactly as it is. Nothing is interpreted yet: deciding that a column is voltage in volts is phase 4's job.

```bash
pip install python-calamine scipy numpy mat73 "psycopg[binary]"
DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/battery_aging \
    python parse.py --raw-dir C:\battery_raw --platform mendeley
```

| option | meaning |
|---|---|
| `--raw-dir DIR` | the folder used for `download.py` |
| `--platform …` / `--source …` | limit to some sources |
| `--only-ext csv txt …` | limit to some file types |
| `--reparse` | read files again even if already read by this parser version |

Each file is committed on its own; an interrupted run continues with the files not yet read.

## What is stored

| table | contents |
|---|---|
| `parse_unit` | one row per file read: `parsed`, `skipped` (with the reason) or `failed` (with the error) |
| `source_table` | one row per table: a CSV/text file, an Excel sheet, or a group of arrays in a `.mat` file. Holds the parse hints: delimiter, decimal mark, header row, units row, first data row, column names and units |
| `cell_row` | every row of every table as an array of text (`cells[1]`, `cells[2]` …), header and preamble lines included |
| `column_profile` | per column: name, unit, number of values, how many are numeric, min, max, sample values |
| `v_tables` | tables with the dataset they belong to |
| `v_column_names` | every column name with how many tables and datasets use it: the vocabulary phase 4 maps |
| `v_parse_status` | parsed / skipped / failed counts and reasons per source |

How formats are read:

- **CSV / text**: encoding detected (UTF-8, UTF-16, Windows-1252). The delimiter is a comma, semicolon, tab, pipe
  or spaces. A tab or semicolon on nearly every line wins over commas, and `0,894` style numbers set the decimal
  mark to `,`. Files with no data rows (readmes) are stored as documents.
- **Header detection**: the first data row is the first of several mostly-numeric rows. The text row just above it
  is the header, and a second text row in between is taken as units. Metadata blocks above the header (e.g. the
  15-line Digatron preamble) stay in `cell_row` as rows before `header_row`.
- **Excel**: every sheet becomes a table. Numbers are stored as Python `repr` text, dates as ISO text.
- **MATLAB** (classic and v7.3/HDF5): inside each struct, equal-length vectors become one table whose columns are
  the field names. 2-D arrays become their own table, and scalars and short strings become `attributes` of the
  struct's table. For example, NASA's `B0005.cycle[0]` has attributes `{"type": "charge", "ambient_temperature": 24}`
  and its measurements are in `B0005.cycle[0].data`.
- **Skipped on purpose**: Python pickles (loading one can run code, so convert them in an isolated environment),
  RAR/7z archives (extract with 7-Zip first), archives inside archives, documents and code.

## Checks

On a test set of 20 Mendeley datasets (573 downloaded files, 2,883 data files after unpacking archives):
0 failures, 33.3 million rows, and the stored row count matched the source for all 400 files checked at random
(lines for text files, rows per sheet for Excel). Storage was about 8 GB for 1.5 GB of downloads.

```sql
-- Look at one table: its rows as stored
SELECT row_index, cells FROM staging.cell_row WHERE table_id = 123 ORDER BY row_index LIMIT 20;

-- Tables of one dataset with their detected layout
SELECT file_path, member_path, name, n_rows, header_row, data_start_row, columns
FROM staging.v_tables WHERE local_refs = 'Dataset - 14';
```
