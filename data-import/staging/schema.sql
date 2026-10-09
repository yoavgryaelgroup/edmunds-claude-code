-- Phase 3 of the battery data platform: every downloaded data file, read into the database exactly as it is.
-- Nothing is interpreted here: cells are stored as the text found in the file. The parse hints (delimiter,
-- decimal mark, header / units / first data row) are guesses that phase 4 mapping files can confirm or override.

-- One row per file read: a downloaded file, or a file inside a downloaded archive.
CREATE TABLE IF NOT EXISTS parse_unit (
    unit_id         serial PRIMARY KEY,
    file_id         integer NOT NULL,              -- ingest.remote_file / ingest.raw_file
    member_path     text NOT NULL DEFAULT '',      -- path inside the archive, '' for a plain file
    extension       text,
    parser          text,                          -- delimited, excel, mat, mat73, document
    parser_version  integer,
    status          text NOT NULL CHECK (status IN ('parsed', 'skipped', 'failed')),
    detail          text,                          -- why skipped / the error
    n_tables        integer,
    n_rows          bigint,
    parsed_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (file_id, member_path)
);

-- One row per table found: a CSV / text file, an Excel sheet, or a group of equal-length arrays in a .mat file.
CREATE TABLE IF NOT EXISTS source_table (
    table_id        serial PRIMARY KEY,
    unit_id         integer NOT NULL REFERENCES parse_unit ON DELETE CASCADE,
    name            text NOT NULL DEFAULT '',      -- sheet name, or the variable path inside a .mat file
    kind            text NOT NULL,                 -- delimited, excel, mat, document
    n_rows          bigint,                        -- rows stored in staging.cell_row (all of them, header included)
    n_cols          integer,                       -- widest row
    encoding        text,
    delimiter       text,                          -- ',', ';', 'tab', 'whitespace', ...
    decimal_mark    text,                          -- '.' or ','
    header_row      integer,                       -- 0-based row_index of the column names (NULL: none found)
    units_row       integer,                       -- row_index of a units row under the header, if any
    data_start_row  integer,                       -- first row_index that looks like data
    columns         text[],                        -- cells of header_row (for .mat: the field names)
    units           text[],                        -- cells of units_row
    attributes      jsonb,                         -- .mat: scalars and short strings next to the arrays
    text_content    text                           -- documents (readme files)
);

-- Every row of every table, as text exactly as found (Excel numbers as Python repr, dates as ISO text).
CREATE TABLE IF NOT EXISTS cell_row (
    table_id   integer NOT NULL REFERENCES source_table ON DELETE CASCADE,
    row_index  integer NOT NULL,
    cells      text[] NOT NULL,
    PRIMARY KEY (table_id, row_index)
);

-- What each column holds, from the rows after data_start_row. Used to write phase 4 mapping files.
CREATE TABLE IF NOT EXISTS column_profile (
    table_id    integer NOT NULL REFERENCES source_table ON DELETE CASCADE,
    col_index   integer NOT NULL,
    name        text,
    unit        text,
    n_values    bigint,                            -- non-empty cells
    n_numeric   bigint,
    min_value   double precision,
    max_value   double precision,
    samples     text[],                            -- first few distinct values
    PRIMARY KEY (table_id, col_index)
);

-- Tables with the dataset they came from.
CREATE OR REPLACE VIEW v_tables AS
SELECT t.table_id, s.source_id, s.platform,
       (SELECT string_agg(DISTINCT d.local_ref, ', ') FROM catalog.dataset d WHERE d.dataset_id = ANY (s.dataset_ids)) AS local_refs,
       f.path AS file_path, u.member_path, t.name, t.kind, t.n_rows, t.n_cols,
       t.header_row, t.units_row, t.data_start_row, t.delimiter, t.decimal_mark, t.columns, t.units
FROM source_table t
JOIN parse_unit u USING (unit_id)
JOIN ingest.remote_file f ON f.file_id = u.file_id
JOIN ingest.source s ON s.source_id = f.source_id;

-- Column names across all tables, most common first: the vocabulary phase 4 has to map.
CREATE OR REPLACE VIEW v_column_names AS
SELECT lower(trim(p.name)) AS column_name, count(*) AS tables, count(DISTINCT f.source_id) AS sources,
       array_agg(DISTINCT p.unit) FILTER (WHERE p.unit IS NOT NULL AND p.unit <> '') AS units,
       min(p.min_value) AS min_value, max(p.max_value) AS max_value
FROM column_profile p
JOIN source_table t USING (table_id)
JOIN parse_unit u USING (unit_id)
JOIN ingest.remote_file f ON f.file_id = u.file_id
WHERE p.name IS NOT NULL AND trim(p.name) <> ''
GROUP BY 1;

-- Progress and problems per source.
CREATE OR REPLACE VIEW v_parse_status AS
SELECT f.source_id, u.status, count(*) AS files, sum(u.n_tables) AS tables, sum(u.n_rows) AS rows,
       string_agg(DISTINCT u.detail, ' | ') FILTER (WHERE u.status <> 'parsed') AS reasons
FROM parse_unit u JOIN ingest.remote_file f ON f.file_id = u.file_id
GROUP BY 1, 2;
