-- Phase 2 of the battery data platform: downloaded files (the raw store) and what is inside archives.
-- Lives in the same schema as the phase 1 inventory (ingest.source, ingest.remote_file).

-- One row per remote file that has been downloaded (or attempted).
CREATE TABLE IF NOT EXISTS raw_file (
    file_id        integer PRIMARY KEY REFERENCES remote_file ON DELETE CASCADE,
    status         text NOT NULL CHECK (status IN ('verified', 'unverified', 'failed')),
                   -- verified: checksum matches the repository's; unverified: the repository gave no
                   -- checksum we can check (size matched where known); failed: see error
    local_path     text,                          -- relative to the raw folder
    size_bytes     bigint,
    sha256         text,                          -- computed locally for every downloaded file
    error          text,
    downloaded_at  timestamptz NOT NULL DEFAULT now()
);

-- Contents of downloaded archives (zip and tar), so later phases know which formats are inside.
CREATE TABLE IF NOT EXISTS archive_member (
    file_id          integer NOT NULL REFERENCES remote_file ON DELETE CASCADE,
    member_path      text NOT NULL,
    size_bytes       bigint,
    extension        text,
    format_family    text,
    PRIMARY KEY (file_id, member_path)
);

-- Download progress per source.
CREATE OR REPLACE VIEW v_download_progress AS
SELECT s.source_id, s.platform, s.title, s.n_files,
       count(r.file_id) FILTER (WHERE r.status = 'verified') AS verified,
       count(r.file_id) FILTER (WHERE r.status = 'unverified') AS unverified,
       count(r.file_id) FILTER (WHERE r.status = 'failed') AS failed,
       s.n_files - count(r.file_id) FILTER (WHERE r.status <> 'failed') AS remaining,
       pg_size_pretty(sum(r.size_bytes) FILTER (WHERE r.status <> 'failed')) AS downloaded,
       pg_size_pretty(s.total_bytes) AS total
FROM source s
LEFT JOIN remote_file f USING (source_id)
LEFT JOIN raw_file r USING (file_id)
WHERE s.status = 'listed'
GROUP BY s.source_id;

-- Formats including what is inside archives (archives themselves are counted by their members).
CREATE OR REPLACE VIEW v_formats_unpacked AS
SELECT format_family, extension, count(*) AS files, pg_size_pretty(sum(size_bytes)) AS total_size
FROM (
    SELECT f.format_family, f.extension, f.size_bytes FROM remote_file f WHERE f.format_family <> 'archive'
    UNION ALL
    SELECT m.format_family, m.extension, m.size_bytes FROM archive_member m
) x
GROUP BY 1, 2;
