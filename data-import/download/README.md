# Raw file download (phase 2)

Downloads the files listed by the phase 1 inventory (`ingest.remote_file`) into a raw folder, checks each one
against the checksum the repository published, and records the result in the database. Run the catalog
(`../catalog`) and inventory (`../inventory`) steps first.

```bash
pip install requests "psycopg[binary]"
DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/battery_aging \
    python download.py --raw-dir C:\battery_raw --platform mendeley
```

| option | meaning |
|---|---|
| `--raw-dir DIR` | where the files go. On Windows use a short path such as `C:\battery_raw` (Windows limits path length) |
| `--platform mendeley zenodo …` | only sources on these platforms |
| `--source 19 34 …` | only these `ingest.source` ids |
| `--workers N` | parallel downloads, default 4 |

Files are saved as `RAW_DIR/<platform>/<source id>_<dataset id>/<path inside the dataset>`, for example
`C:\battery_raw\mendeley\034_wykht8y7tg-v1\...`. Downloads use `curl` (included in Windows 10 and later),
resume after an interruption, and are retried up to three times. A file whose checksum doesn't match is deleted
and downloaded again. Re-running the command skips everything already downloaded and retries failures. The
script checks free disk space before it starts.

## Tables and views (in `ingest`)

| name | contents |
|---|---|
| `raw_file` | one row per downloaded file: `verified` (checksum matches), `unverified` (repository gave no checksum) or `failed` (with the error), local path, size, sha256 |
| `archive_member` | every file inside each downloaded zip / tar archive, with extension and format family |
| `v_download_progress` | per source: files verified, failed and remaining, size downloaded vs total |
| `v_formats_unpacked` | file formats counting what is inside archives |

## Things the archives revealed (Mendeley test batch)

- Dataset-50 (Tsinghua, `Train.zip` / `Test.zip`) holds about 40,000 Python pickle (`.pkl`) files. Loading a
  pickle can run arbitrary code, so convert them in an isolated environment, never with `pickle.load` on a
  normal machine.
- Dataset-73 (Xi'an Jiaotong) contains `BatteryAgingARC-FY08Q4.zip`, NASA's battery data re-uploaded. Don't
  count it as a separate dataset.
