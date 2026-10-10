-- Phase 0 of the battery data platform: the catalog of datasets, loaded from
-- Battery_Datasets_with_Scraped_Data.xlsx (Sheet1, Scraped Data, Scrape Notes).
-- load_workbook.py drops and recreates every table here except workbook_import (the import history),
-- so the catalog always matches the last workbook loaded.

CREATE TABLE IF NOT EXISTS workbook_import (
    import_id    serial PRIMARY KEY,
    file_name    text NOT NULL,
    sha256       text NOT NULL,
    sheet_names  text[],
    imported_at  timestamptz NOT NULL DEFAULT now()
);

-- Every non-empty cell of every sheet exactly as found, so nothing in the workbook is lost.
CREATE TABLE IF NOT EXISTS sheet_cell (
    sheet         text NOT NULL,
    row_num       integer NOT NULL,
    col_num       integer NOT NULL,
    col_letter    text NOT NULL,
    header        text,                          -- row 1 of that column
    value         text,
    hyperlink     text,
    merged_range  text,                          -- e.g. 'C10:C25' when the cell heads a merged block
    PRIMARY KEY (sheet, row_num, col_num)
);

-- Sheet1 column A/B: the publisher or lab ("Sr. No" groups).
CREATE TABLE IF NOT EXISTS publisher (
    publisher_id serial PRIMARY KEY,
    sr_no        text,
    name         text NOT NULL,
    first_row    integer NOT NULL,
    last_row     integer NOT NULL
);

-- One row per dataset entry in Sheet1: a block of rows sharing the same local reference, description,
-- chemistry and dataset link (blocks come from the sheet's merged cells; extra rows hold more papers).
CREATE TABLE IF NOT EXISTS dataset (
    dataset_id             serial PRIMARY KEY,
    publisher_id           integer REFERENCES publisher,
    local_ref              text,                 -- 'Dataset - 14' (size note split off into listed_size)
    listed_size            text,                 -- 'Size: 182 Mb' as written in the local reference
    listed_size_bytes      bigint,
    first_row              integer NOT NULL,     -- Sheet1 rows covered by this entry
    last_row               integer NOT NULL,
    basic_information      text,
    objective              text,
    additional_information text,
    chemistry              text,
    form_factor            text,
    manufacturer           text,
    capacity               text,
    cells_tested           text,
    cells_tested_num       integer,              -- only when the sheet value is a plain whole number
    unplaced_notes         text,                 -- "Something that we have found but we don't know where this fits"
    dataset_link_label     text,                 -- text shown in the Dataset Link cell
    dataset_url            text,                 -- its hyperlink (NULL when the cell has no link)
    platform               text,                 -- hosting platform derived from the URL
    assignees              text[],               -- names of the Mihir / Irina / Sarah / Harshita columns that are filled in
    scraped_id             integer               -- matching row of scraped_metadata (by URL)
);

-- Every link in Sheet1, one per URL per cell.
CREATE TABLE IF NOT EXISTS dataset_link (
    link_id      serial PRIMARY KEY,
    dataset_id   integer NOT NULL REFERENCES dataset ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('dataset', 'paper', 'code', 'reference', 'other')),
    sheet_row    integer NOT NULL,
    col_letter   text NOT NULL,
    label        text,
    url          text NOT NULL,                 -- the cell's hyperlink, or a URL written in its text
    alt_url      text,                          -- URL shown as the cell text when it differs from the hyperlink
    platform     text,
    doi          text                           -- from url or alt_url when either contains a DOI
);

-- "Scraped Data" sheet: one row per unique dataset URL, scraped from its landing page.
CREATE TABLE IF NOT EXISTS scraped_metadata (
    scraped_id          integer PRIMARY KEY,     -- '#' column
    source_rows         integer[],               -- Sheet1 rows that use this link
    publisher           text,
    local_ref           text,
    url                 text,
    platform            text,
    scrape_status       text,                    -- as written, e.g. 'Blocked (login required)'
    status_class        text,                    -- ok, partial, blocked, failed
    title               text,
    chemistry           text,
    form_factor         text,
    manufacturer_model  text,
    nominal_capacity    text,
    voltage             text,
    cells_units         text,
    test_temperatures   text,
    tests_performed     text,
    test_equipment      text,
    data_size_format    text,
    size_bytes          bigint,                  -- parsed from data_size_format when it states a size
    license             text,
    published           text,
    summary             text
);

-- "Scrape Notes" sheet: summary counts, reading notes and proposed corrections to Sheet1.
CREATE TABLE IF NOT EXISTS scrape_note (
    note_id     serial PRIMARY KEY,
    section     text,
    note        text NOT NULL,
    value       text,
    sheet_row   integer,                         -- Sheet1 row a correction refers to ('Row 26 ...')
    dataset_id  integer REFERENCES dataset ON DELETE SET NULL
);

-- One row per dataset entry with the sheet's and the scrape's view side by side.
CREATE OR REPLACE VIEW v_dataset AS
SELECT d.dataset_id, p.name AS publisher, d.local_ref, d.first_row, d.last_row,
       d.dataset_url, d.platform, s.status_class AS scrape_status, s.title AS scraped_title,
       d.chemistry, s.chemistry AS scraped_chemistry,
       d.form_factor, s.form_factor AS scraped_form_factor,
       d.manufacturer, s.manufacturer_model AS scraped_manufacturer_model,
       d.capacity, s.nominal_capacity AS scraped_nominal_capacity,
       d.cells_tested, s.cells_units AS scraped_cells,
       s.test_temperatures, s.tests_performed, s.test_equipment,
       coalesce(s.size_bytes, d.listed_size_bytes) AS size_bytes,
       s.data_size_format, s.license, s.published,
       (SELECT count(*) FROM dataset_link l WHERE l.dataset_id = d.dataset_id AND l.kind = 'paper') AS papers,
       (SELECT count(*) FROM dataset_link l WHERE l.dataset_id = d.dataset_id AND l.kind = 'code') AS code_repos,
       d.assignees
FROM dataset d
LEFT JOIN publisher p USING (publisher_id)
LEFT JOIN scraped_metadata s USING (scraped_id);
