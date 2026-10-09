#!/usr/bin/env python3
"""Phase 3: read every downloaded data file into PostgreSQL staging tables, exactly as it is.

Usage:
    pip install python-calamine scipy numpy mat73 "psycopg[binary]"
    DATABASE_URL=postgresql://postgres:password@localhost:5432/battery_aging \
        python parse.py --raw-dir C:\\battery_raw [--platform mendeley] [--match wykht8y7tg ...] [--reparse]

For every file in ingest.raw_file (and every file inside downloaded zip / tar archives) it stores:
  - staging.source_table: one row per table (CSV or text file, Excel sheet, group of arrays in a .mat file),
    with guesses for the delimiter, decimal mark, header row, units row and first data row;
  - staging.cell_row: every row of the table as text, header and preamble included, so nothing is lost;
  - staging.column_profile: for each column, its name, unit, count of numeric values, min, max and samples.
Files that are not data (documents, code) or not safe to open (Python pickles) are recorded as skipped,
with the reason. Re-running only reads files not yet parsed by this parser version; --reparse reads all again.
"""
import argparse
import csv
import io
import json
import math
import os
import re
import sys
import tarfile
import zipfile
from collections import Counter
from pathlib import Path

import numpy as np
import psycopg
from psycopg import sql

PARSER_VERSION = 1
HERE = Path(__file__).resolve().parent
csv.field_size_limit(2**31 - 1)

TEXT_EXT = {"csv", "txt", "tsv", "dat"}
EXCEL_EXT = {"xlsx", "xls", "xlsm", "xlsb", "ods"}
SKIP_EXT = {
    "pkl": "Python pickle: loading one can run code, so convert it in an isolated environment first",
    "pickle": "Python pickle: loading one can run code, so convert it in an isolated environment first",
    "rar": "RAR archive: extract it with 7-Zip, then parse the extracted files",
    "7z": "7z archive: extract it with 7-Zip, then parse the extracted files",
}
NOT_DATA = {"pdf", "doc", "docx", "md", "m", "mlx", "py", "ipynb", "r", "png", "jpg", "jpeg", "gif", "tif",
            "tiff", "svg", "html", "htm", "fig", "ini", "db", "json", "xml"}
NUM_RE = re.compile(r"^[+-]?(\d+([.,]\d*)?|[.,]\d+)([eE][+-]?\d+)?$")
SAMPLE_ROWS = 300


# ---------------------------------------------------------------- cell text and number detection

def cell_text(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (float, np.floating)):
        v = float(v)
        return "NaN" if math.isnan(v) else repr(v)
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if hasattr(v, "isoformat"):
        return v.isoformat()
    s = str(v)
    return s.replace("\x00", "")


def as_number(s, decimal):
    s = s.strip()
    if not s or not NUM_RE.match(s):
        return None
    if decimal == ",":
        s = s.replace(".", "").replace(",", ".") if s.count(",") == 1 else s
    elif "," in s:
        return None
    try:
        x = float(s)
        return x if math.isfinite(x) else None
    except ValueError:
        return None


def numeric_share(row, decimal):
    vals = [c for c in row if c.strip()]
    return (sum(as_number(c, decimal) is not None for c in vals) / len(vals)) if vals else 0.0, len(vals)


def detect_layout(sample, decimal):
    """(header_row, units_row, data_start_row) from the first rows of a table; None where not found."""
    data_start = None
    for i, row in enumerate(sample):
        share, n = numeric_share(row, decimal)
        if n and share >= 0.6:
            following = [numeric_share(r, decimal) for r in sample[i + 1:i + 4]]
            if all(s >= 0.5 or k == 0 for s, k in following):
                data_start = i
                break
    if data_start is None:
        return None, None, None
    width = max(1, numeric_share(sample[data_start], decimal)[1])

    def texty(i):
        if i < 0:
            return False
        share, n = numeric_share(sample[i], decimal)
        return n >= max(1, width * 0.5) and share < 0.5

    if texty(data_start - 1) and texty(data_start - 2):
        return data_start - 2, data_start - 1, data_start
    if texty(data_start - 1):
        return data_start - 1, None, data_start
    return None, None, data_start


class Profiler:
    """Per-column counts, min, max and samples over the data rows."""

    def __init__(self, decimal, start):
        self.decimal, self.start, self.cols = decimal, start, {}

    def add(self, index, row):
        if self.start is None or index < self.start:
            return
        for j, c in enumerate(row):
            if not c.strip():
                continue
            p = self.cols.setdefault(j, [0, 0, None, None, []])
            p[0] += 1
            x = as_number(c, self.decimal)
            if x is not None:
                p[1] += 1
                p[2] = x if p[2] is None or x < p[2] else p[2]
                p[3] = x if p[3] is None or x > p[3] else p[3]
            if len(p[4]) < 5 and c not in p[4]:
                p[4].append(c[:100])

    def rows(self, table_id, names, units):
        for j, (n, k, lo, hi, samples) in sorted(self.cols.items()):
            name = names[j] if names and j < len(names) else None
            unit = units[j] if units and j < len(units) else None
            yield (table_id, j, (name or "")[:300] or None, (unit or "")[:100] or None, n, k, lo, hi, samples)


# ---------------------------------------------------------------- writer

class Writer:
    def __init__(self, conn, unit_id):
        self.conn, self.unit_id, self.n_tables, self.n_rows = conn, unit_id, 0, 0

    def table(self, name, kind, rows, decimal=".", encoding=None, delimiter=None, columns=None, units=None,
              layout=None, attributes=None, text_content=None):
        """Store a table. rows is an iterable of lists of str; the first SAMPLE_ROWS may be inspected."""
        rows = iter(rows)
        sample = []
        for r in rows:
            sample.append(r)
            if len(sample) >= SAMPLE_ROWS:
                break
        if layout is None:
            layout = detect_layout(sample, decimal) if sample else (None, None, None)
        header, units_row, start = layout
        if columns is None and header is not None:
            columns = sample[header]
        if units is None and units_row is not None:
            units = sample[units_row]
        if columns is not None and start is None:
            start = 0
        table_id = self.conn.execute(
            """INSERT INTO source_table (unit_id, name, kind, encoding, delimiter, decimal_mark, header_row,
                   units_row, data_start_row, columns, units, attributes, text_content)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING table_id""",
            (self.unit_id, name[:500], kind, encoding, delimiter, decimal, header, units_row, start,
             [cell_text(c)[:300] for c in columns] if columns is not None else None,
             [cell_text(c)[:100] for c in units] if units is not None else None,
             json.dumps(attributes, default=str) if attributes else None, text_content)).fetchone()[0]
        prof = Profiler(decimal, start)
        n, width = 0, 0
        with self.conn.cursor().copy("COPY cell_row (table_id, row_index, cells) FROM STDIN (FORMAT BINARY)") as cp:
            cp.set_types(["int4", "int4", "text[]"])
            for chain in (sample, rows):
                for r in chain:
                    cp.write_row((table_id, n, r))
                    prof.add(n, r)
                    width = max(width, len(r))
                    n += 1
        self.conn.execute("UPDATE source_table SET n_rows = %s, n_cols = %s WHERE table_id = %s", (n, width, table_id))
        with self.conn.cursor().copy("COPY column_profile (table_id, col_index, name, unit, n_values, n_numeric, "
                                     "min_value, max_value, samples) FROM STDIN") as cp:
            for p in prof.rows(table_id, columns, units):
                cp.write_row(p)
        self.n_tables += 1
        self.n_rows += n


# ---------------------------------------------------------------- delimited text

def decode_head(head):
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    if head[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as e:
        if e.start > len(head) - 4:  # cut in the middle of a character at the end of the sample
            return "utf-8"
        return "cp1252"


def sniff(lines):
    """(delimiter, decimal_mark) from sample lines; delimiter None means: not a table."""
    lines = [l for l in lines if l.strip()][:200]
    if not lines:
        return None, "."
    def consistency(d):
        counts = [l.count(d) for l in lines]
        nonzero = [c for c in counts if c]
        if not nonzero:
            return 0, 0.0
        mode = Counter(nonzero).most_common(1)[0][0]
        return mode, len(nonzero) / len(lines)  # share of lines that contain the delimiter at all

    best, best_score = None, 0.0
    # A tab, semicolon or pipe on (nearly) every line wins over commas: files exported with a European
    # locale use ';' or tab between columns and ',' as the decimal mark.
    for d in ["\t", ";", "|"]:
        mode, share = consistency(d)
        if mode and share >= 0.8:
            best, best_score = d, share * min(mode, 5)
            break
    if best is None:
        mode, share = consistency(",")
        if mode:
            best, best_score = ",", share * min(mode, 5)
    if best is None or best_score < 0.5:
        tokens = [len(l.split()) for l in lines]
        mode, freq = Counter(tokens).most_common(1)[0]
        if mode >= 2 and freq / len(lines) >= 0.6:
            best = "whitespace"
        else:
            return None, "."
    cells = [c for l in lines for c in (l.split() if best == "whitespace" else l.split(best))]
    comma = sum(bool(re.match(r"^-?\d+,\d+$", c.strip())) for c in cells)
    dot = sum(bool(re.match(r"^-?\d+\.\d+$", c.strip())) for c in cells)
    return best, ("," if best != "," and comma > dot else ".")


def parse_text(w, name, opener):
    with opener() as fh:
        head = fh.read(262144)
    enc = decode_head(head)
    sample_lines = head.decode(enc, errors="replace").splitlines()[:300]
    delim, decimal = sniff(sample_lines)
    if delim in (",", "whitespace"):
        # Prose (readme files) contains commas and spaces too: with no rows that look like data, it's a document.
        split = [l.split() if delim == "whitespace" else next(csv.reader([l], delimiter=",")) for l in sample_lines if l.strip()]
        if detect_layout(split, decimal) == (None, None, None):
            delim = None
    if delim is None:
        with opener() as fh:
            text = io.TextIOWrapper(fh, encoding=enc, errors="replace").read()
        w.table(name, "document", ([l] for l in text.splitlines()), encoding=enc, text_content=text[:1_000_000],
                layout=(None, None, None))
        return "document"
    with opener() as fh:
        text_io = io.TextIOWrapper(fh, encoding=enc, errors="replace", newline="")
        if delim == "whitespace":
            rows = (line.split() for line in text_io)
        else:
            rows = csv.reader(text_io, delimiter=delim)
        w.table(name, "delimited", ([c.replace("\x00", "") for c in r] for r in rows), decimal=decimal,
                encoding=enc, delimiter="tab" if delim == "\t" else delim)
    return "delimited"


# ---------------------------------------------------------------- Excel

def parse_excel(w, opener):
    from python_calamine import CalamineWorkbook
    with opener() as fh:
        wb = CalamineWorkbook.from_filelike(io.BytesIO(fh.read()))
    for sheet in wb.sheet_names:
        rows = wb.get_sheet_by_name(sheet).to_python()
        w.table(sheet, "excel", ([cell_text(c) for c in r] for r in rows))
    return "excel"


# ---------------------------------------------------------------- MATLAB

def is_vector(v):
    return isinstance(v, np.ndarray) and v.ndim == 1 and v.dtype.kind in "biufcUSO" and v.size > 1


def to_cells(v):
    if v.dtype.kind == "c":
        return [f"{x.real!r}{x.imag:+}j" for x in v]
    return [cell_text(x) for x in v.tolist()]


def scalar_value(v):
    if isinstance(v, np.ndarray):
        if v.size == 0:
            return None
        if v.size == 1:
            v = v.reshape(-1)[0]
        elif v.size <= 50 and v.dtype.kind in "biufUS":
            return [scalar_value(x) for x in v.reshape(-1)]
        else:
            return f"<array {v.dtype}{v.shape}>"
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        return None if math.isnan(float(v)) else float(v)
    if isinstance(v, (np.complexfloating, complex)):
        return [float(np.real(v)), float(np.imag(v))]
    if isinstance(v, (str, int, bool)) or v is None:
        return v
    if isinstance(v, bytes):
        return v.decode("latin1")
    return str(v)[:500]


def walk_mat(w, node, path, attrs_out=None):
    """Turn a loaded .mat structure into tables: equal-length vectors in a struct become one table,
    2-D arrays their own table, scalars and short strings become attributes of the struct's tables."""
    if isinstance(node, dict):
        vectors, attrs = {}, {}
        for k, v in node.items():
            if k.startswith("__"):
                continue
            p = f"{path}.{k}" if path else k
            if isinstance(v, list) and v and all(isinstance(x, (int, float, np.number)) for x in v):
                v = np.asarray(v)
            if isinstance(v, list) and v and all(isinstance(x, str) for x in v) and len(v) > 50:
                v = np.asarray(v, dtype=object)
            if is_vector(v):
                vectors.setdefault(len(v), {})[k] = v
            elif isinstance(v, np.ndarray) and v.ndim >= 2 and v.size > 1 and v.dtype.kind in "biufc":
                arr = v.reshape(v.shape[0], -1)
                a = {"shape": list(v.shape)} if v.ndim > 2 else None
                w.table(p, "mat", ([cell_text(x) if arr.dtype.kind != "c" else f"{x.real!r}{x.imag:+}j"
                                    for x in row] for row in arr), layout=(None, None, 0), attributes=a)
            elif isinstance(v, (dict, list, tuple)) or (isinstance(v, np.ndarray) and v.dtype.kind == "O" and v.ndim):
                walk_mat(w, v, p)
            else:
                attrs[k] = scalar_value(v)
        groups = sorted(vectors.items(), key=lambda kv: -len(kv[1]))
        for i, (length, cols) in enumerate(groups):
            names = list(cols)
            data = [to_cells(cols[c]) for c in names]
            rows = ([data[j][r] for j in range(len(names))] for r in range(length))
            w.table(path or "(root)", "mat", rows, columns=names, layout=(None, None, 0),
                    attributes=attrs if i == 0 and attrs else None)
        if not groups and attrs:
            w.table(path or "(root)", "mat", iter(()), columns=[], layout=(None, None, None), attributes=attrs)
    elif isinstance(node, (list, tuple)) or (isinstance(node, np.ndarray) and node.dtype.kind == "O"):
        items = list(node) if not isinstance(node, np.ndarray) else list(node.reshape(-1))
        if items and all(is_vector(np.asarray(x)) and np.asarray(x).dtype.kind in "biuf" for x in items) \
                and len({np.asarray(x).size for x in items}) == 1:
            w.table(path, "mat", (to_cells(np.asarray(x)) for x in items), layout=(None, None, 0),
                    attributes={"stacked_from": "list of equal-length arrays, one per row"})
            return
        if items and all(isinstance(x, str) for x in items):
            w.table(path, "mat", ([x] for x in items), columns=["value"], layout=(None, None, 0))
            return
        for i, x in enumerate(items):
            walk_mat(w, x, f"{path}[{i}]")
    elif isinstance(node, np.ndarray) and node.size > 1:
        walk_mat(w, {"value": node}, path)


def parse_mat(w, opener):
    import tempfile
    with opener() as fh:
        data = fh.read()
    if data[:128].find(b"MATLAB 7.3") >= 0 or data[:8] == b"\x89HDF\r\n\x1a\n" or data[512:520] == b"\x89HDF\r\n\x1a\n":
        import mat73
        with tempfile.NamedTemporaryFile(suffix=".mat", delete=False) as tmp:
            tmp.write(data)
        try:
            m = mat73.loadmat(tmp.name)
        finally:
            os.unlink(tmp.name)
        walk_mat(w, m, "")
        return "mat73"
    import scipy.io as sio
    m = sio.loadmat(io.BytesIO(data), simplify_cells=True)
    walk_mat(w, {k: v for k, v in m.items() if not k.startswith("__")}, "")
    return "mat"


# ---------------------------------------------------------------- driver

def extension(path):
    name = path.rsplit("/", 1)[-1].lower()
    for double in ("tar.gz", "tar.bz2", "tar.xz"):
        if name.endswith("." + double):
            return double
    return name.rsplit(".", 1)[-1] if "." in name else ""


SKIP_NAME = re.compile(r"(^|/)(~\$[^/]*|Thumbs\.db|desktop\.ini|\.DS_Store|__MACOSX/.*)$", re.I)


def parse_one(conn, file_id, member, ext, opener):
    """Parse one file in its own transaction and record the outcome in parse_unit."""
    conn.execute("DELETE FROM parse_unit WHERE file_id = %s AND member_path = %s", (file_id, member))
    unit_id = conn.execute("""INSERT INTO parse_unit (file_id, member_path, extension, parser_version, status)
                              VALUES (%s, %s, %s, %s, 'parsed') RETURNING unit_id""",
                           (file_id, member, ext, PARSER_VERSION)).fetchone()[0]
    w = Writer(conn, unit_id)
    status, detail, parser = "parsed", None, None
    try:
        if ext in SKIP_EXT:
            status, detail = "skipped", SKIP_EXT[ext]
        elif ext in NOT_DATA:
            status, detail = "skipped", "not a data file"
        elif ext in TEXT_EXT or ext == "":
            parser = parse_text(w, member.rsplit("/", 1)[-1] if member else "", opener)
        elif ext in EXCEL_EXT:
            parser = parse_excel(w, opener)
        elif ext == "mat":
            parser = parse_mat(w, opener)
        else:
            status, detail = "skipped", f"no parser for .{ext} files yet"
        conn.execute("UPDATE parse_unit SET parser = %s, status = %s, detail = %s, n_tables = %s, n_rows = %s "
                     "WHERE unit_id = %s", (parser, status, detail, w.n_tables, w.n_rows, unit_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.execute("""INSERT INTO parse_unit (file_id, member_path, extension, parser_version, status, detail)
                        VALUES (%s, %s, %s, %s, 'failed', %s)""",
                     (file_id, member, ext, PARSER_VERSION, f"{type(e).__name__}: {e}"[:1000]))
        conn.commit()
        status, detail = "failed", str(e)
    return status, w.n_rows, detail


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", required=True)
    ap.add_argument("--platform", nargs="*")
    ap.add_argument("--source", nargs="*", type=int)
    ap.add_argument("--match", nargs="*", help="only sources whose URL contains one of these (e.g. a Mendeley id)")
    ap.add_argument("--reparse", action="store_true")
    ap.add_argument("--only-ext", nargs="*", help="only files with these extensions (e.g. csv txt)")
    args = ap.parse_args()
    db = os.environ.get("DATABASE_URL")
    if not db:
        sys.exit("set DATABASE_URL, e.g. postgresql://postgres:password@localhost:5432/battery_aging")
    schema = os.environ.get("DB_SCHEMA", "staging")
    raw_dir = Path(args.raw_dir)

    with psycopg.connect(db) as conn:
        if not conn.execute("SELECT to_regclass('ingest.raw_file')").fetchone()[0]:
            sys.exit("ingest.raw_file not found: download files first with data-import/download/download.py")
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute((HERE / "schema.sql").read_text(encoding="utf-8"))
        conn.commit()
        files = conn.execute("""
            SELECT r.file_id, r.local_path, f.path, f.extension FROM ingest.raw_file r
            JOIN ingest.remote_file f USING (file_id) JOIN ingest.source s USING (source_id)
            WHERE r.status <> 'failed'
              AND (%(p)s::text[] IS NULL OR s.platform = ANY(%(p)s))
              AND (%(s)s::int[] IS NULL OR s.source_id = ANY(%(s)s))
              AND (%(m)s::text[] IS NULL OR s.url ILIKE ANY(%(m)s))
            ORDER BY s.source_id, r.file_id""",
            {"p": args.platform, "s": args.source, "m": [f"%{x}%" for x in args.match] if args.match else None}).fetchall()
        done = set() if args.reparse else {
            (fid, m) for fid, m in conn.execute(
                "SELECT file_id, member_path FROM parse_unit WHERE status <> 'failed' AND parser_version = %s",
                (PARSER_VERSION,))}
        print(f"{len(files)} downloaded files to read", flush=True)
        totals = Counter()
        for i, (file_id, local, path, ext) in enumerate(files, 1):
            full = raw_dir / local
            if not full.exists():
                print(f"  missing on disk, skipped: {full}", flush=True)
                continue
            if ext == "zip" or ext in ("tar", "tar.gz", "tgz", "tar.bz2", "tar.xz"):
                try:
                    arc = zipfile.ZipFile(full) if ext == "zip" else tarfile.open(full)
                except (zipfile.BadZipFile, tarfile.TarError) as e:
                    conn.execute("DELETE FROM parse_unit WHERE file_id = %s AND member_path = ''", (file_id,))
                    conn.execute("""INSERT INTO parse_unit (file_id, member_path, extension, parser_version, status,
                                        detail) VALUES (%s, '', %s, %s, 'failed', %s)""",
                                 (file_id, ext, PARSER_VERSION, f"could not open archive: {e}"))
                    conn.commit()
                    totals["failed"] += 1
                    continue
                with arc:
                    names = ([n.filename for n in arc.infolist() if not n.is_dir()] if ext == "zip"
                             else [m.name for m in arc.getmembers() if m.isfile()])
                    for name in names:
                        if SKIP_NAME.search(name) or (file_id, name) in done:
                            continue
                        mext = extension(name)
                        if args.only_ext and mext not in args.only_ext:
                            continue
                        if mext in ("zip", "tar", "tar.gz", "tgz"):
                            conn.execute("""INSERT INTO parse_unit (file_id, member_path, extension, parser_version,
                                                status, detail) VALUES (%s, %s, %s, %s, 'skipped', %s)
                                            ON CONFLICT (file_id, member_path) DO NOTHING""",
                                         (file_id, name, mext, PARSER_VERSION, "archive inside an archive: extract it first"))
                            conn.commit()
                            totals["skipped"] += 1
                            continue
                        opener = (lambda n=name: arc.open(n)) if ext == "zip" else (lambda n=name: arc.extractfile(n))
                        status, n, detail = parse_one(conn, file_id, name, mext, opener)
                        totals[status] += 1
                        totals["rows"] += n
                        if status == "failed":
                            print(f"  FAILED {path}!{name}: {detail[:200]}", flush=True)
            else:
                if (file_id, "") in done or (args.only_ext and ext not in args.only_ext):
                    continue
                status, n, detail = parse_one(conn, file_id, "", ext, lambda f=full: open(f, "rb"))
                totals[status] += 1
                totals["rows"] += n
                if status == "failed":
                    print(f"  FAILED {path}: {detail[:200]}", flush=True)
            if i % 50 == 0 or i == len(files):
                print(f"  {i}/{len(files)} downloaded files read; {totals['parsed']} parsed, {totals['skipped']} skipped, "
                      f"{totals['failed']} failed, {totals['rows']:,} rows", flush=True)
        for t in ("source_table", "column_profile", "cell_row"):
            conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(t)))
        conn.commit()
    print(f"done: tables are in schema {schema!r}")


if __name__ == "__main__":
    main()
