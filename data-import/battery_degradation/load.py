#!/usr/bin/env python3
"""Import the Mendeley "Battery Degradation Dataset (Fixed Current Profiles &
Arbitrary Uses Profiles)" v2 (doi:10.17632/kw34hhw7xg.2) into PostgreSQL.

Usage:
    pip install python-calamine "psycopg[binary]"
    DATABASE_URL=postgresql://user:pass@host:5432/db python load.py [--data-dir DIR]

Downloads every file of the dataset (skipping ones already present and
checksum-verified), creates the schema in schema.sql, and loads each Excel
file with COPY. Re-running is safe: files already loaded are skipped.
"""
import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import psycopg
from python_calamine import CalamineWorkbook

DATASET = "kw34hhw7xg"
VERSION = 2
API = "https://data.mendeley.com/public-api/datasets"
HERE = Path(__file__).resolve().parent

def get_json(url):
    # curl rather than urllib: Mendeley rejects some default user agents.
    return json.loads(subprocess.check_output(["curl", "-sSfL", "--retry", "5", url]))


def list_files():
    folders = get_json(f"{API}/{DATASET}/folders/{VERSION}")
    by_id = {f["id"]: f for f in folders}
    out = []
    for folder_id, group, battery in [("root", None, None)] + [
        (f["id"], by_id[f["parent_id"]]["name"], f["name"])
        for f in folders
        if f.get("parent_id") in by_id
    ]:
        for x in get_json(f"{API}/{DATASET}/files?folder_id={folder_id}&version={VERSION}"):
            out.append(
                dict(
                    group=group,
                    battery=battery,
                    name=x["filename"],
                    size=x["size"],
                    sha256=x["content_details"]["sha256_hash"],
                    url=x["content_details"]["download_url"],
                )
            )
    return out


def local_path(data_dir, f):
    if f["group"] is None:
        return data_dir / f["name"]
    return data_dir / f["group"].replace(" ", "_") / f["battery"].replace("#", "b") / f["name"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(data_dir, files):
    for f in files:
        p = local_path(data_dir, f)
        if p.exists() and p.stat().st_size == f["size"] and sha256(p) == f["sha256"]:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {p.relative_to(data_dir)}", flush=True)
        subprocess.check_call(["curl", "-sSfL", "--retry", "5", "-o", str(p), f["url"]])
        if sha256(p) != f["sha256"]:
            sys.exit(f"checksum mismatch: {p}")


def test_time_seconds(v):
    """Test_Time is an Excel time (<24h) or a 'D-HH:MM:SS' string (>=24h)."""
    if isinstance(v, dt.time):
        return v.hour * 3600 + v.minute * 60 + v.second + v.microsecond / 1e6
    if isinstance(v, dt.timedelta):
        return v.total_seconds()
    if isinstance(v, (int, float)):
        return float(v) * 86400  # raw Excel day fraction
    m = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d+):(\d+(?:\.\d+)?)", str(v).strip())
    if not m:
        raise ValueError(f"unparseable Test_Time {v!r}")
    d, h, mi, s = m.groups()
    return int(d or 0) * 86400 + int(h) * 3600 + int(mi) * 60 + float(s)


def date_time(v):
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat() + " 00:00:00"
    if isinstance(v, (int, float)):
        return (dt.datetime(1899, 12, 30) + dt.timedelta(days=v)).isoformat(sep=" ")
    return str(v)


EXPECTED_HEADER = [
    "Data_Point", "Test_Time(s)", "Current(A)", "Capacity(Ah)", "Voltage(V)",
    "Energy(Wh)", "Temperature(℃)", "Date_Time", "Cycle_Index",
]


def num(v):
    """Numeric cell as COPY text; blanks and '-' (missing readings) become NULL."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    v = str(v or "").strip()
    if v in ("", "-"):
        return r"\N"
    float(v)  # raise on anything else unexpected
    return v


def to_tsv(rows, file_id, battery_id):
    buf = io.StringIO()
    for r in rows:
        buf.write(
            f"{file_id}\t{battery_id}\t{int(r[0])}\t{test_time_seconds(r[1])}\t{num(r[2])}\t{num(r[3])}\t"
            f"{num(r[4])}\t{num(r[5])}\t{num(r[6])}\t{date_time(r[7])}\t{int(r[8])}\n"
        )
    return buf.getvalue()


def battery_num(name):
    return int(name.lstrip("#"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=str(HERE / "raw"))
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("set DATABASE_URL")
    data_dir = Path(args.data_dir)

    files = list_files()
    if not args.skip_download:
        download(data_dir, files)

    readme = next(f for f in files if f["name"] == "Readme.txt")
    rates = {}
    for line in local_path(data_dir, readme).read_text().splitlines()[1:]:
        parts = line.split()
        if len(parts) == 3:
            rates[battery_num(parts[0])] = (parts[1], parts[2])

    with psycopg.connect(url, autocommit=False) as conn:
        conn.execute((HERE / "schema.sql").read_text())
        # From the folder tree, so batteries whose folder is empty (#10, #13, #16, #19 in v2) are kept too.
        folders = get_json(f"{API}/{DATASET}/folders/{VERSION}")
        by_id = {f["id"]: f for f in folders}
        groups = {f["name"]: by_id[f["parent_id"]]["name"] for f in folders if f.get("parent_id") in by_id}
        for name, group in groups.items():
            b = battery_num(name)
            charge, discharge = rates.get(b, (None, None))
            conn.execute(
                """INSERT INTO batteries (battery_id, profile_group, charge_rate, discharge_rate)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (battery_id) DO UPDATE SET profile_group = EXCLUDED.profile_group,
                     charge_rate = EXCLUDED.charge_rate, discharge_rate = EXCLUDED.discharge_rate""",
                (b, group, charge, discharge),
            )
        conn.commit()

        xlsx = sorted((f for f in files if f["name"].endswith(".xlsx")), key=lambda f: (battery_num(f["battery"]), f["name"]))
        for i, f in enumerate(xlsx, 1):
            b = battery_num(f["battery"])
            done = conn.execute(
                "SELECT 1 FROM source_files WHERE battery_id = %s AND filename = %s AND loaded_at IS NOT NULL",
                (b, f["name"]),
            ).fetchone()
            if done:
                continue
            p = local_path(data_dir, f)
            wb = CalamineWorkbook.from_path(str(p))
            rows = wb.get_sheet_by_index(0).to_python()
            header = rows[0]
            rows = [r for r in rows[1:] if r[0] not in ("", None)]  # some sheets have fully blank rows
            if [str(h).strip() for h in header[:9]] != EXPECTED_HEADER:
                sys.exit(f"unexpected header in {p}: {header}")
            conn.execute("DELETE FROM source_files WHERE battery_id = %s AND filename = %s", (b, f["name"]))
            file_id = conn.execute(
                """INSERT INTO source_files (battery_id, filename, sheet_name, sha256, size_bytes)
                   VALUES (%s, %s, %s, %s, %s) RETURNING file_id""",
                (b, f["name"], wb.sheet_names[0], f["sha256"], f["size"]),
            ).fetchone()[0]
            with conn.cursor().copy(
                """COPY measurements (file_id, battery_id, data_point, test_time_s, current_a,
                   capacity_ah, voltage_v, energy_wh, temperature_c, date_time, cycle_index)
                   FROM STDIN"""
            ) as cp:
                cp.write(to_tsv(rows, file_id, b))
            conn.execute(
                "UPDATE source_files SET row_count = %s, loaded_at = now() WHERE file_id = %s",
                (len(rows), file_id),
            )
            conn.commit()
            print(f"[{i}/{len(xlsx)}] battery #{b} {f['name']}: {len(rows)} rows", flush=True)

        # Built after the bulk load; much faster than maintaining it during COPY.
        conn.execute("CREATE INDEX IF NOT EXISTS measurements_battery_cycle_idx ON measurements (battery_id, cycle_index)")
        conn.commit()
        conn.execute("ANALYZE")
    print("done")


if __name__ == "__main__":
    main()
