-- Phase 1 of the battery data platform: an inventory of every file behind every dataset link,
-- listed from the repositories without downloading anything.

-- One row per unique dataset URL from catalog.dataset.
CREATE TABLE IF NOT EXISTS source (
    source_id    serial PRIMARY KEY,
    url          text NOT NULL UNIQUE,
    platform     text,
    dataset_ids  integer[],                      -- catalog.dataset entries that use this URL
    status       text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'listed', 'empty', 'manual', 'failed')),
    detail       text,                           -- why it is manual / failed, or notes on the listing
    title        text,                           -- title reported by the repository
    n_files      integer,
    total_bytes  bigint,                         -- sum of known file sizes
    unsized      integer,                        -- files whose size the repository did not report
    listed_at    timestamptz
);

-- One row per file the repository lists for a source.
CREATE TABLE IF NOT EXISTS remote_file (
    file_id        serial PRIMARY KEY,
    source_id      integer NOT NULL REFERENCES source ON DELETE CASCADE,
    path           text NOT NULL,                -- path or name within the dataset
    size_bytes     bigint,
    checksum       text,
    checksum_algo  text,                         -- md5, sha256, ...
    download_url   text,
    extension      text,                         -- lower-case, without the dot ('mat', 'csv', 'tar.gz')
    format_family  text                          -- tabular, matlab, archive, hdf5, pickle, document, ...
);
CREATE INDEX IF NOT EXISTS remote_file_source_idx ON remote_file (source_id);

-- Per-source summary next to the catalog's own description.
CREATE OR REPLACE VIEW v_inventory AS
SELECT s.source_id, s.platform, s.status, s.n_files, pg_size_pretty(s.total_bytes) AS total_size, s.total_bytes,
       s.unsized, s.title, s.detail, s.url,
       (SELECT string_agg(DISTINCT p.name, '; ')
          FROM catalog.dataset d JOIN catalog.publisher p USING (publisher_id)
         WHERE d.dataset_id = ANY (s.dataset_ids)) AS publisher,
       (SELECT string_agg(DISTINCT d.local_ref, ', ')
          FROM catalog.dataset d WHERE d.dataset_id = ANY (s.dataset_ids)) AS local_refs
FROM source s;

-- Which file formats the downloads contain, and how much of each.
CREATE OR REPLACE VIEW v_formats AS
SELECT format_family, extension, count(*) AS files, count(DISTINCT source_id) AS sources,
       pg_size_pretty(sum(size_bytes)) AS total_size, sum(size_bytes) AS total_bytes
FROM remote_file GROUP BY 1, 2;
