# Dataset file inventory (phase 1)

Lists every file behind every dataset link in the catalog, without downloading anything, and stores the result
in an `ingest` schema. Run it after loading the workbook with `../catalog/load_workbook.py`.

```bash
pip install requests "psycopg[binary]"
DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/battery_aging python list_files.py
```

Options: `--only mendeley zenodo` (platforms or URLs), `--refresh` (list again sources already listed).
Re-running retries every source that is not yet `listed`. Set `DB_SCHEMA` to use a schema other than `ingest`.

## How each platform is listed

| platform | method |
|---|---|
| Mendeley Data | public API (`/public-api/datasets/{id}/files`, every folder), sha256 per file; falls back to `curl` because Mendeley refuses Python's HTTP client |
| Zenodo | REST API `/api/records/{id}`; DOI links are resolved to the record first |
| Figshare (UCL, Iowa State, Singapore Tech, CMU KiltHub) and 4TU.ResearchData | Figshare-style API, including collections |
| Dryad, OSF, TU Graz (InvenioRDM), DepositOnce (DSpace), GitHub | their JSON APIs |
| NASA PCoE | battery files from the repository page, plus `5. Battery Data Set.zip`, which is still on NASA's server but no longer linked |
| others (CALCE, PNNL, …) | data-file links on the landing page, sized with HEAD requests |
| login, bot-check or JavaScript-only sites | marked `manual` with the reason, for the team to check in a browser |

## Tables and views

| name | contents |
|---|---|
| `ingest.source` | one row per dataset URL: platform, status (`listed`, `empty`, `manual`, `failed`), reason, file count, total size |
| `ingest.remote_file` | one row per file: path, size, checksum, download link, extension, format family |
| `ingest.v_inventory` | each source with its catalog publisher and local references |
| `ingest.v_formats` | file count and size per format |

```sql
-- What still needs a person to look at it
SELECT local_refs, publisher, platform, detail, url FROM ingest.v_inventory WHERE status <> 'listed';

-- Largest datasets
SELECT local_refs, publisher, n_files, total_size FROM ingest.v_inventory ORDER BY total_bytes DESC NULLS LAST;
```

Archives (`.zip`, `.tar`, …) are listed as single files; their contents are inventoried when they are
downloaded in phase 2.
