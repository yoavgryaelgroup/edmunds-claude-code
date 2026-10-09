# Battery dataset catalog → PostgreSQL (phase 0)

Loads `Battery_Datasets_with_Scraped_Data.xlsx` (sheets **Sheet1**, **Scraped Data**, **Scrape Notes**) into a
`catalog` schema. It is the first phase of the battery data platform plan: one queryable list of every dataset,
its links, papers and scrape results, before anything is downloaded.

```bash
pip install openpyxl "psycopg[binary]"
DATABASE_URL=postgresql://postgres:PASSWORD@localhost:5432/battery_aging python load_workbook.py Battery_Datasets_with_Scraped_Data.xlsx
```

Set `DB_SCHEMA` to use a schema other than `catalog`. Each run rebuilds the catalog from the workbook you give it.
`catalog.workbook_import` keeps a history of every import (file name, sha256, time).

## Tables

| table | contents |
|---|---|
| `publisher` | Sheet1 publisher / lab blocks (column A/B) |
| `dataset` | one row per dataset entry in Sheet1: local reference, description, objective, chemistry, form factor, manufacturer, capacity, cells tested, dataset link and its hosting platform, assignees |
| `dataset_link` | every link in Sheet1, typed `dataset`, `paper`, `code`, `reference` or `other`, with platform and DOI |
| `scraped_metadata` | the Scraped Data sheet: one row per unique dataset URL, with scrape status, title, specs, size, licence |
| `scrape_note` | the Scrape Notes sheet; proposed corrections are linked to the dataset entry they refer to |
| `sheet_cell` | every non-empty cell of every sheet as found (value, hyperlink, merged range) |
| `v_dataset` | view: each dataset entry with the sheet's values and the scraped values side by side |

## How Sheet1 is read

Sheet1 groups rows with merged cells. A publisher block (columns A/B) contains one or more dataset entries, and
a new entry starts wherever the local reference, a description column, the chemistry/spec columns or the dataset
link starts a new value. Rows that only add a paper in column G stay with the entry above them. The sheet has
102 such entries. A merged cell that spans several entries (for example NASA's single repository link for
datasets 1–4 and 63) applies to each of them.

When a cell's text is itself a URL and the cell also has a different hyperlink (typically a DOI shown as text,
linking to the publisher's page), the two are stored as one link: `url` is the hyperlink and `alt_url` the
shown URL.

## Useful queries

```sql
-- Dataset entries with their scrape result, largest first
SELECT publisher, local_ref, platform, scrape_status, pg_size_pretty(size_bytes), dataset_url
FROM catalog.v_dataset ORDER BY size_bytes DESC NULLS LAST;

-- How many datasets each hosting platform serves (decides which downloaders to build first)
SELECT platform, count(DISTINCT dataset_url) FROM catalog.dataset GROUP BY 1 ORDER BY 2 DESC;

-- Corrections proposed during scraping, next to the current sheet values
SELECT n.note, d.chemistry, d.form_factor, d.capacity
FROM catalog.scrape_note n JOIN catalog.dataset d USING (dataset_id);
```
