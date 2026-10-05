#!/usr/bin/env python3
"""Import all battery data linked from https://calce.umd.edu/battery-data into PostgreSQL.

Usage:
    pip install python-calamine mat-io numpy "psycopg[binary]"
    DATABASE_URL=postgresql://user:pass@host:5432/dbname python load.py [--data-dir DIR] [--only NAME ...]

Downloads every .zip linked from the CALCE battery data page (about 4 GB) to --data-dir, checks each
download is complete, creates the calce_* tables from schema.sql (in the public schema unless DB_SCHEMA
is set) and loads every file. Each archive member is loaded in its own transaction and skipped on
re-runs, so an interrupted import can just be started again.
"""
import argparse
import datetime as dt
import html
import io
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse
import zipfile
from pathlib import Path

import numpy as np
import psycopg
from psycopg import sql
from python_calamine import CalamineWorkbook

HERE = Path(__file__).resolve().parent
PAGE = "https://calce.umd.edu/battery-data"
DATA_PREFIX = "https://web.calce.umd.edu/batteries/data/"
NULL = r"\N"


# ---------------------------------------------------------------- download

def curl(*args):
    return subprocess.run(["curl", "-sSL", "--retry", "5", *args], check=True, capture_output=True).stdout


def list_datasets():
    """(dataset, url, cell_type, page_section) for every zip linked from the CALCE page, in page order."""
    page = curl(PAGE).decode("utf-8", "replace")
    page = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    out, seen, context = [], set(), ""
    for m in re.finditer(r'<a [^>]*href="([^"]+\.zip)"[^>]*>(.*?)</a>|>([^<>]+)(?=<)', page, re.S):
        if m.group(1) is None:
            text = html.unescape(m.group(3)).strip()
            if text and not text.startswith("Data for"):
                context = text
            continue
        url = urllib.parse.urljoin(PAGE, m.group(1))
        if url in seen or not url.startswith(DATA_PREFIX):
            continue
        seen.add(url)
        rel = urllib.parse.unquote(url[len(DATA_PREFIX):])
        name = rel.rsplit("/", 1)[-1][:-4]
        if rel.startswith("pln/"):
            cell_type = "PLN (storage)"
        elif rel.startswith("pl/"):
            cell_type = "PL"
        elif name.startswith("SP"):
            cell_type = "INR 18650-20R"
        else:
            cell_type = name.split("_")[0]
        link_text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
        out.append((name, url, cell_type, f"{context} / {link_text}" if context else link_text, rel))
    if not out:
        sys.exit("no data links found on the CALCE page")
    return out


def remote_size(url):
    head = curl("-I", url).decode("latin1")
    sizes = re.findall(r"(?im)^content-length:\s*(\d+)", head)
    return int(sizes[-1]) if sizes else None


def download(url, path):
    size = remote_size(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(6):
        if path.exists() and (size is None or path.stat().st_size == size):
            try:
                with zipfile.ZipFile(path) as z:
                    if z.testzip() is None:
                        return size
            except zipfile.BadZipFile:
                pass
            path.unlink()  # complete-looking but corrupt: start over
        print(f"  downloading {url} (attempt {attempt + 1})", flush=True)
        subprocess.run(["curl", "-sSL", "--retry", "5", "-C", "-", "-o", str(path), url])
    sys.exit(f"could not download {url} completely")


# ---------------------------------------------------------------- value formatting for COPY

def esc(s):
    return str(s).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def fnum(v):
    if v is None or v == "":
        return NULL
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return NULL if isinstance(v, float) and not math.isfinite(v) else repr(v)
    v = str(v).strip()
    if v == "" or v == "-":
        return NULL
    f = float(v)
    return repr(f) if math.isfinite(f) else NULL


def fint(v):
    s = fnum(v)
    if s == NULL:
        return s
    f = float(s)
    if f != int(f):
        raise ValueError(f"expected an integer, got {v!r}")
    return str(int(f))


EXCEL_EPOCH = dt.datetime(1899, 12, 30)


def fts(v):
    if v is None or v == "":
        return NULL
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat() + " 00:00:00"
    if isinstance(v, (int, float)):
        return (EXCEL_EPOCH + dt.timedelta(days=v)).isoformat(sep=" ")
    return esc(str(v).strip())  # let PostgreSQL parse other date strings


def jsonable(v):
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, dt.timedelta):
        return v.total_seconds()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def copy(conn, table, cols, lines):
    if not lines:
        return
    with conn.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(
            sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, cols)))) as cp:
        for i in range(0, len(lines), 50000):
            cp.write("".join(lines[i:i + 50000]))


# ---------------------------------------------------------------- per-format loaders

ARBIN_COLS = {
    "Data_Point": ("data_point", fint), "Test_Time(s)": ("test_time_s", fnum), "Date_Time": ("date_time", fts),
    "Step_Time(s)": ("step_time_s", fnum), "Step_Index": ("step_index", fint), "Cycle_Index": ("cycle_index", fint),
    "Current(A)": ("current_a", fnum), "Voltage(V)": ("voltage_v", fnum),
    "Charge_Capacity(Ah)": ("charge_capacity_ah", fnum), "Discharge_Capacity(Ah)": ("discharge_capacity_ah", fnum),
    "Charge_Energy(Wh)": ("charge_energy_wh", fnum), "Discharge_Energy(Wh)": ("discharge_energy_wh", fnum),
    "dV/dt(V/s)": ("dv_dt_v_per_s", fnum), "Internal_Resistance(Ohm)": ("internal_resistance_ohm", fnum),
    "Is_FC_Data": ("is_fc_data", fnum), "AC_Impedance(Ohm)": ("ac_impedance_ohm", fnum),
    "ACI_Phase_Angle(Deg)": ("aci_phase_angle_deg", fnum),
    "Temperature (C)_1": ("temperature_1_c", fnum), "Temperature (C)_2": ("temperature_2_c", fnum),
}
STAT_COLS = {
    "Cycle_Index": ("cycle_index", fint), "Test_Time(s)": ("test_time_s", fnum), "Date_Time": ("date_time", fts),
    "Current(A)": ("current_a", fnum), "Voltage(V)": ("voltage_v", fnum),
    "Charge_Capacity(Ah)": ("charge_capacity_ah", fnum), "Discharge_Capacity(Ah)": ("discharge_capacity_ah", fnum),
    "Charge_Energy(Wh)": ("charge_energy_wh", fnum), "Discharge_Energy(Wh)": ("discharge_energy_wh", fnum),
    "Internal_Resistance(Ohm)": ("internal_resistance_ohm", fnum), "AC_Impedance(Ohm)": ("ac_impedance_ohm", fnum),
    "ACI_Phase_Angle(Deg)": ("aci_phase_angle_deg", fnum), "Charge_Time(s)": ("charge_time_s", fnum),
    "DisCharge_Time(s)": ("discharge_time_s", fnum), "Vmax_On_Cycle(V)": ("vmax_on_cycle_v", fnum),
}
CADEX_COLS = [
    ("Time", "time", fnum), ("Status code", "status_code", fint), ("Status category", "status_category", fint),
    ("Status color", "status_color", fint), ("Pgm code", "pgm_code", fint), ("Pgm step", "pgm_step", fint),
    ("Pgm para", "pgm_para", fint), ("Pgm cycle", "pgm_cycle", fint), ("mV", "voltage_mv", fnum),
    ("mA", "current_ma", fnum), ("Temperature", "temperature", fnum), ("Duration", "duration_s", fnum),
    ("Charge count", "charge_count", fint), ("Discharge count", "discharge_count", fint),
    ("Capacity", "capacity", fnum),
    *[(f"Analog input {i}", f"analog_input_{i}", fnum) for i in range(1, 5)],
    *[(f"Digital input {i}", f"digital_input_{i}", fint) for i in range(1, 5)],
    *[(f"Digital output {i}", f"digital_output_{i}", fint) for i in range(1, 5)],
    *[(f"Analog output {i}", f"analog_output_{i}", fnum) for i in range(1, 3)],
]


def mapped_rows(file_id, header, rows, colmap):
    idx = [(i, colmap[h]) for i, h in enumerate(header) if h in colmap]
    cols = ["file_id", "row_index"] + [c for _, (c, _) in idx]
    lines = []
    for r, row in enumerate(rows):
        if all(c in ("", None) for c in row):
            continue
        n = len(row)
        lines.append(f"{file_id}\t{r}\t" + "\t".join(f(row[i]) if i < n else NULL for i, (_, f) in idx) + "\n")
    return cols, lines


def generic_lines(file_id, rows):
    return [f"{file_id}\t{r}\t{esc(json.dumps([jsonable(v) for v in row], ensure_ascii=False))}\n"
            for r, row in enumerate(rows)]


def classify_sheet(header):
    if header[:2] == ["Data_Point", "Test_Time(s)"] and all(h in ARBIN_COLS for h in header if h):
        return "arbin"
    if header[:1] == ["Cycle_Index"] and "Vmax_On_Cycle(V)" in header and all(h in STAT_COLS for h in header if h):
        return "arbin_statistics"
    return "generic"


def load_spreadsheet(ctx, data):
    wb = CalamineWorkbook.from_filelike(io.BytesIO(data))
    for sheet in wb.sheet_names:
        rows = wb.get_sheet_by_name(sheet).to_python()
        header = [str(h).strip() for h in rows[0]] if rows else []
        kind = classify_sheet(header) if rows else "generic"
        file_id = ctx.add_file(sheet, kind, "\t".join(header) if rows else None)
        if kind == "arbin":
            cols, lines = mapped_rows(file_id, header, rows[1:], ARBIN_COLS)
            copy(ctx.conn, "calce_arbin_measurements", cols, lines)
        elif kind == "arbin_statistics":
            cols, lines = mapped_rows(file_id, header, rows[1:], STAT_COLS)
            copy(ctx.conn, "calce_arbin_statistics", cols, lines)
        else:
            lines = generic_lines(file_id, rows)
            copy(ctx.conn, "calce_generic_rows", ["file_id", "row_index", "cells"], lines)
        ctx.set_rows(file_id, len(lines))


def decode_text(data):
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin1")


def load_text(ctx, data):
    text = decode_text(data)
    first = text.split("\n", 1)[0]
    if first.startswith("Time\tStatus code"):
        header = [h.strip() for h in first.rstrip("\r\n").split("\t")]
        names = [h for h, _, _ in CADEX_COLS]
        if [h for h in header if h] != names:
            raise ValueError(f"unexpected CADEX header: {header}")
        file_id = ctx.add_file("", "cadex", first.strip())
        colmap = {h: (c, f) for h, c, f in CADEX_COLS}
        rows = [line.split("\t") for line in text.splitlines()[1:] if line.strip()]
        cols, lines = mapped_rows(file_id, header, rows, colmap)
        copy(ctx.conn, "calce_cadex_measurements", cols, lines)
        ctx.set_rows(file_id, len(lines))
    else:
        file_id = ctx.add_file("", "text", None, text_content=text)
        ctx.set_rows(file_id, text.count("\n") + 1)


def load_csv(ctx, data):
    text = decode_text(data)
    lines_in = [l for l in text.splitlines() if l.strip()]
    if lines_in and lines_in[0].startswith("Name:"):
        return load_temperature_log(ctx, lines_in)
    rows = [[c.strip() for c in l.split(",")] for l in lines_in]
    try:
        numeric = all(len(r) == 5 for r in rows) and all(float(c) or True for r in rows for c in r)
    except ValueError:
        numeric = False
    if numeric:
        file_id = ctx.add_file("", "impedance", "frequency_hz, z_real_ohm, z_imag_ohm, z_mod_ohm, phase_deg (inferred)")
        lines = [f"{file_id}\t{i}\t" + "\t".join(fnum(c) for c in r) + "\n" for i, r in enumerate(rows)]
        copy(ctx.conn, "calce_impedance_points",
             ["file_id", "row_index", "frequency_hz", "z_real_ohm", "z_imag_ohm", "z_mod_ohm", "phase_deg"], lines)
    else:
        file_id = ctx.add_file("", "generic", lines_in[0] if lines_in else None)
        lines = generic_lines(file_id, rows)
        copy(ctx.conn, "calce_generic_rows", ["file_id", "row_index", "cells"], lines)
    ctx.set_rows(file_id, len(lines))


def load_temperature_log(ctx, lines_in):
    start = next(i for i, l in enumerate(lines_in) if l.startswith("Scan,Time"))
    header = lines_in[start].split(",")
    channels = [(i, header[i]) for i in range(2, len(header), 2)]
    file_id = ctx.add_file("", "temperature_log", "\n".join(lines_in[:start + 1]))
    out = []
    for r, line in enumerate(lines_in[start + 1:]):
        c = line.split(",")
        t = dt.datetime.strptime(c[1].strip(), "%m/%d/%Y %H:%M:%S:%f").isoformat(sep=" ")
        for i, name in channels:
            alarm = c[i + 1] if i + 1 < len(c) else ""
            out.append(f"{file_id}\t{r}\t{fint(c[0])}\t{t}\t{esc(name)}\t{fnum(c[i])}\t{fint(alarm)}\n")
    copy(ctx.conn, "calce_temperature_logs",
         ["file_id", "row_index", "scan", "time", "channel", "value", "alarm"], out)
    ctx.set_rows(file_id, len(lines_in) - start - 1)


def mat_str(v):
    v = np.ravel(np.asarray(v, dtype=object))
    return str(v[0]).strip() if v.size else ""


def datenum(d):
    if not math.isfinite(d):
        return NULL
    # Rounded to the millisecond: the day fraction carries float noise (e.g. 14:25:13.000004).
    t = dt.datetime.fromordinal(int(d)) - dt.timedelta(days=366) + dt.timedelta(seconds=round(d % 1 * 86400, 3))
    return t.isoformat(sep=" ")


PL_VARS = {"Time_sec": "time_s", "Date_Time": "date_time", "Step": "step", "Cycle": "cycle",
           "Current_Amp": "current_a", "Voltage_Volt": "voltage_v", "Charge_Ah": "charge_ah",
           "Discharge_Ah": "discharge_ah"}


def load_mat(ctx, data):
    import tempfile
    import warnings
    import matio
    with tempfile.NamedTemporaryFile(suffix=".mat", delete=False) as fh:
        fh.write(data)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            m = matio.load_from_mat(fh.name)
    finally:
        os.unlink(fh.name)
    for var, arr in m.items():
        if var.startswith("__"):
            continue
        file_id = ctx.add_file(var, "pl_mat", "Operation\tStart Date\tData (MATLAB table)")
        ops, lines = [], []
        for k, row in enumerate(np.asarray(arr, dtype=object)[1:], 1):  # row 0 is the header
            op, start = mat_str(row[0]), mat_str(row[1])
            props = getattr(row[2], "properties", None)
            n = 0
            if props is not None:
                names = [mat_str(v) for v in np.ravel(props["varnames"])]
                cols = {PL_VARS[nm]: np.ravel(np.asarray(v, dtype=float)) for nm, v in zip(names, np.ravel(props["data"]))}
                n = len(cols["time_s"])
                sdate = dt.datetime.strptime(start, "%B %d, %Y").date().isoformat() if start else NULL
                pre = f"{file_id}\t{k}\t{esc(op)}\t{sdate}\t"
                t, d, st, cy, cu, vo, ch, di = (cols[c] for c in PL_VARS.values())
                for i in range(n):
                    lines.append(pre + f"{i}\t{fnum(float(t[i]))}\t{datenum(d[i])}\t{fint(float(st[i]))}\t"
                                 f"{fint(float(cy[i]))}\t{fnum(float(cu[i]))}\t{fnum(float(vo[i]))}\t"
                                 f"{fnum(float(ch[i]))}\t{fnum(float(di[i]))}\n")
            ops.append([op, start, n])
        copy(ctx.conn, "calce_pl_measurements",
             ["file_id", "operation_index", "operation", "start_date", "row_index",
              *PL_VARS.values()], lines)
        # The operation list itself (including 'Missing Data' entries without a table).
        copy(ctx.conn, "calce_generic_rows", ["file_id", "row_index", "cells"],
             generic_lines(file_id, [["Operation", "Start Date", "Rows"]] + ops))
        ctx.set_rows(file_id, len(lines))


# ---------------------------------------------------------------- driver

CELL_RE = re.compile(r"(CS2_\d+|CX2_\d+|PLN\d+(?![\d_]|to)|PL\d+|A1-\d{3}|SP20-\d)")


class Member:
    def __init__(self, conn, dataset, path):
        self.conn, self.dataset, self.path = conn, dataset, path
        m = CELL_RE.search(path) or CELL_RE.search(dataset)
        self.cell = m.group(1) if m else None
        self.file_ids = []

    def add_file(self, sheet, kind, header, text_content=None):
        file_id = self.conn.execute(
            """INSERT INTO calce_files (dataset, member_path, sheet_name, cell, kind, header, text_content)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING file_id""",
            (self.dataset, self.path, sheet, self.cell, kind, header, text_content)).fetchone()[0]
        self.file_ids.append(file_id)
        return file_id

    def set_rows(self, file_id, n):
        self.conn.execute("UPDATE calce_files SET n_rows = %s WHERE file_id = %s", (n, file_id))


SKIP = re.compile(r"(^|/)(~\$[^/]*|Thumbs\.db|desktop\.ini|\.DS_Store)$", re.I)
LOADERS = {".xls": load_spreadsheet, ".xlsx": load_spreadsheet, ".txt": load_text,
           ".csv": load_csv, ".mat": load_mat}


def load_zip(conn, dataset, zpath):
    with zipfile.ZipFile(zpath) as z:
        members = [n for n in z.namelist() if not n.endswith("/") and not SKIP.search(n)]
        for name in members:
            ext = os.path.splitext(name)[1].lower()
            if ext not in LOADERS:
                print(f"  skipping {name}: unknown file type", flush=True)
                continue
            done = conn.execute(
                "SELECT 1 FROM calce_files WHERE dataset = %s AND member_path = %s AND loaded_at IS NOT NULL LIMIT 1",
                (dataset, name)).fetchone()
            if done:
                continue
            conn.execute("DELETE FROM calce_files WHERE dataset = %s AND member_path = %s", (dataset, name))
            m = Member(conn, dataset, name)
            LOADERS[ext](m, z.read(name))
            conn.execute("UPDATE calce_files SET loaded_at = now() WHERE file_id = ANY(%s)", (m.file_ids,))
            conn.commit()
        print(f"  {dataset}: {len(members)} files", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=str(HERE / "raw"), help="where the downloaded zips are kept")
    ap.add_argument("--only", nargs="*", help="load only these datasets (e.g. CS2_35 SP2_0C_DST)")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("set DATABASE_URL, e.g. postgresql://postgres:password@localhost:5432/battery_aging")
    schema = os.environ.get("DB_SCHEMA", "public")
    data_dir = Path(args.data_dir)

    datasets = list_datasets()
    if args.only:
        datasets = [d for d in datasets if d[0] in args.only]
    print(f"{len(datasets)} datasets", flush=True)

    with psycopg.connect(url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute((HERE / "schema.sql").read_text(encoding="utf-8"))
        conn.commit()
        for i, (name, durl, cell_type, section, rel) in enumerate(datasets, 1):
            print(f"[{i}/{len(datasets)}] {name}", flush=True)
            zpath = data_dir / rel
            size = download(durl, zpath)
            conn.execute(
                """INSERT INTO calce_datasets (dataset, url, cell_type, page_section, size_bytes)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (dataset) DO UPDATE SET url = EXCLUDED.url, cell_type = EXCLUDED.cell_type,
                     page_section = EXCLUDED.page_section, size_bytes = EXCLUDED.size_bytes""",
                (name, durl, cell_type, section, size or zpath.stat().st_size))
            conn.commit()
            load_zip(conn, name, zpath)
        conn.execute("ANALYZE")
        conn.commit()
    print(f"done: tables calce_* are in schema {schema!r}")


if __name__ == "__main__":
    main()
