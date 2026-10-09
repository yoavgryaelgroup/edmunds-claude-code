#!/usr/bin/env python3
"""Phase 2: download the files listed in the phase 1 inventory into a raw folder, verified by checksum.

Usage:
    DATABASE_URL=postgresql://postgres:password@localhost:5432/battery_aging \
        python download.py --raw-dir C:\\battery_raw --platform mendeley

Downloads every file of ingest.remote_file for the chosen sources with curl (included in Windows 10 and later),
resuming partial downloads, checks each against the repository's checksum (sha256 or md5), records it in
ingest.raw_file, and lists the contents of zip/tar archives in ingest.archive_member. Files land in
RAW_DIR/<platform>/<source id>_<dataset id>/<path inside the dataset>.

Options:
    --raw-dir DIR          where to keep the files (required; use a short path such as C:\\battery_raw on Windows)
    --platform NAME ...    only sources on these platforms (e.g. mendeley)
    --source ID ...        only these ingest.source ids
    --workers N            parallel downloads (default 4)

Re-running skips files already downloaded, retries the ones that failed, and continues where it stopped. Nothing is ever deleted except a
download whose checksum did not match.
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import psycopg
from psycopg import sql

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "inventory"))
from list_files import UA, extension, family  # noqa: E402

BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


def safe_rel_path(path):
    """A relative path that stays inside the target folder and is valid on Windows."""
    parts = []
    for part in re.split(r"[\\/]+", path):
        part = BAD_CHARS.sub("_", part).strip().rstrip(".")
        if part in ("", ".", ".."):
            continue
        parts.append(part[:150])
    return Path(*parts) if parts else Path("unnamed")


def long_path(p):
    """Windows needs the \\\\?\\ prefix for paths longer than 260 characters."""
    p = os.path.abspath(p)
    if os.name == "nt" and len(p) > 240 and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + p
    return p


def file_hash(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_folder(platform, source_id, url):
    m = re.search(r"datasets/([a-z0-9]{10})(?:/(\d+))?", url) or re.search(r"/([a-z0-9]{10})-(\d+)\.zip", url)
    if platform == "mendeley" and m:
        key = m.group(1) + (f"-v{m.group(2)}" if m.group(2) else "")
    else:
        key = re.sub(r"[^A-Za-z0-9]+", "-", re.sub(r"^https?://(www\.)?", "", url)).strip("-")[:40]
    return Path(platform) / f"{source_id:03d}_{key}"


def download_one(job, raw_dir):
    """Download and verify one file. Runs in a worker thread; returns a result dict (no database access)."""
    rel = job["folder"] / safe_rel_path(job["path"])
    dest = long_path(raw_dir / rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    want_size, algo, want_sum = job["size"], (job["algo"] or "").lower(), (job["checksum"] or "").lower()
    algo = {"sha-256": "sha256", "sha1": "sha1", "sha-1": "sha1"}.get(algo, algo)
    checkable = algo in ("sha256", "md5", "sha1") and want_sum

    def verify():
        if want_size is not None and os.path.getsize(dest) != want_size:
            return False
        return not checkable or file_hash(dest, algo) == want_sum

    if not (os.path.exists(dest) and verify()):
        last_err = None
        for attempt in range(3):
            part = dest + ".part"
            if attempt and os.path.exists(part):
                os.remove(part)  # resume failed or produced a bad file: start this file over
            proc = subprocess.run(["curl", "-sSfL", "--retry", "5", "--retry-delay", "3", "-A", UA,
                                   "-C", "-", "-o", part, job["url"]], capture_output=True, text=True)
            if proc.returncode == 0 or (proc.returncode == 33 and os.path.exists(part)):
                os.replace(part, dest)
                if verify():
                    break
                last_err = "checksum or size did not match the repository's"
                os.remove(dest)
            else:
                last_err = (proc.stderr or f"curl exit code {proc.returncode}").strip()[:300]
        else:
            return dict(job, status="failed", error=last_err, rel=str(rel))

    members = []
    ext = extension(job["path"])
    try:
        if ext == "zip":
            with zipfile.ZipFile(dest) as z:
                members = [(i.filename, i.file_size) for i in z.infolist() if not i.is_dir()]
        elif ext in ("tar", "tar.gz", "tgz", "tar.bz2", "tar.xz"):
            with tarfile.open(dest) as t:
                members = [(m.name, m.size) for m in t.getmembers() if m.isfile()]
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as e:
        members = [(f"(could not read archive: {e})"[:200], None)]
    return dict(job, status="verified" if checkable else "unverified", error=None, rel=str(rel),
                bytes=os.path.getsize(dest), sha256=want_sum if algo == "sha256" else file_hash(dest, "sha256"),
                members=members)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--platform", nargs="*")
    ap.add_argument("--source", nargs="*", type=int)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    db = os.environ.get("DATABASE_URL")
    if not db:
        sys.exit("set DATABASE_URL, e.g. postgresql://postgres:password@localhost:5432/battery_aging")
    if not shutil.which("curl"):
        sys.exit("curl was not found; it is included in Windows 10 and later, and in macOS and Linux")
    schema = os.environ.get("DB_SCHEMA", "ingest")
    raw_dir = Path(args.raw_dir).resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)

    with psycopg.connect(db) as conn:
        if not conn.execute("SELECT to_regclass(%s)", (f"{schema}.remote_file",)).fetchone()[0]:
            sys.exit(f"{schema}.remote_file not found: run data-import/inventory/list_files.py first")
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute((HERE / "schema.sql").read_text(encoding="utf-8"))
        conn.commit()

        rows = conn.execute("""
            SELECT f.file_id, f.path, f.size_bytes, f.checksum, f.checksum_algo, f.download_url,
                   s.source_id, s.platform, s.url
            FROM remote_file f JOIN source s USING (source_id)
            LEFT JOIN raw_file r USING (file_id)
            WHERE s.status = 'listed' AND f.download_url IS NOT NULL
              AND (r.file_id IS NULL OR r.status = 'failed')
              AND (%(platforms)s::text[] IS NULL OR s.platform = ANY(%(platforms)s))
              AND (%(sources)s::int[] IS NULL OR s.source_id = ANY(%(sources)s))
            ORDER BY s.source_id, f.file_id""",
            {"platforms": args.platform, "sources": args.source}).fetchall()
        jobs = [dict(file_id=r[0], path=r[1], size=r[2], checksum=r[3], algo=r[4], url=r[5],
                     folder=source_folder(r[7], r[6], r[8])) for r in rows]
        need = sum(j["size"] or 0 for j in jobs)
        free = shutil.disk_usage(raw_dir).free
        print(f"{len(jobs)} files to download, {need / 1e9:.2f} GB; {free / 1e9:.1f} GB free in {raw_dir}", flush=True)
        if need > free * 0.95:
            sys.exit("not enough free disk space for this batch; free some space or pick fewer sources "
                     "with --source / --platform")

        done = failed = done_bytes = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(download_one, j, raw_dir) for j in jobs]
            for fut in as_completed(futures):
                r = fut.result()
                conn.execute(
                    """INSERT INTO raw_file (file_id, status, local_path, size_bytes, sha256, error)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON CONFLICT (file_id) DO UPDATE SET status = EXCLUDED.status, local_path = EXCLUDED.local_path,
                         size_bytes = EXCLUDED.size_bytes, sha256 = EXCLUDED.sha256, error = EXCLUDED.error,
                         downloaded_at = now()""",
                    (r["file_id"], r["status"], r["rel"], r.get("bytes"), r.get("sha256"), r["error"]))
                conn.execute("DELETE FROM archive_member WHERE file_id = %s", (r["file_id"],))
                for name, size in r.get("members") or []:
                    ext = extension(name)
                    conn.execute("""INSERT INTO archive_member (file_id, member_path, size_bytes, extension, format_family)
                                    VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                                 (r["file_id"], name, size, ext, family(ext)))
                conn.commit()
                if r["status"] == "failed":
                    failed += 1
                    print(f"  FAILED {r['path']}: {r['error']}", flush=True)
                else:
                    done += 1
                    done_bytes += r.get("bytes") or 0
                if (done + failed) % 100 == 0 or done + failed == len(jobs):
                    print(f"  {done + failed}/{len(jobs)} files, {done_bytes / 1e9:.2f} GB, {failed} failed", flush=True)

        summary = conn.execute("""SELECT r.status, count(*), sum(r.size_bytes) FROM raw_file r
                                  JOIN remote_file USING (file_id) JOIN source s USING (source_id)
                                  WHERE (%(platforms)s::text[] IS NULL OR s.platform = ANY(%(platforms)s))
                                    AND (%(sources)s::int[] IS NULL OR s.source_id = ANY(%(sources)s))
                                  GROUP BY 1 ORDER BY 1""",
                               {"platforms": args.platform, "sources": args.source}).fetchall()
    print("\nsummary for this selection:")
    for status, n, b in summary:
        print(f"  {status:10} {n:6} files  {float(b or 0) / 1e9:8.2f} GB")
    print(f"done: files are in {raw_dir}")


if __name__ == "__main__":
    main()
