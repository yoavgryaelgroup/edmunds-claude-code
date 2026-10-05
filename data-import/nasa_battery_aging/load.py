#!/usr/bin/env python3
"""Import NASA Ames PCoE Li-ion Battery Aging (BatteryAgingARC) .mat files into PostgreSQL.

Usage:
    pip install scipy numpy "psycopg[binary]"
    DATABASE_URL=postgresql://user:pass@host:5432/battery_aging python load.py ZIP_OR_DIR [...]

Each argument is a BatteryAgingARC zip (e.g. 5._BatteryAgingARC_49_50_51_52.zip) or a directory
with the extracted B00NN.mat and README files. Tables are created from schema.sql. A battery that
is already in the database is replaced, so re-running is safe.
"""
import datetime as dt
import io
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import psycopg
import scipy.io as sio

HERE = Path(__file__).resolve().parent
NULL = r"\N"


def parse_readme(text):
    """Map battery id -> metadata described by one README."""
    desc = text.split("Files:")[0].replace("Data Description:", "").strip()
    ids = re.findall(r"^(B\d{4})\.mat", text, re.M)
    temp = re.search(r"(\d+(?:\.\d+)?) deg C", desc)
    if "square wave" in desc:
        m = re.search(r"([\d.]+Hz) square wave loading profile of ([\d.]+A) amplitude and (\d+%) duty", desc)
        profile = f"{m.group(2)} amplitude {m.group(1)} square wave, {m.group(3)} duty cycle" if m else "square wave"
    else:
        m = re.search(r"load current level of ([\d.]+A)", desc)
        profile = f"{m.group(1)} constant current" if m else None
    cutoffs = {}
    m = re.search(r"(?:fell to|stopped at) (.+?) for batteries (.+?) respectively", desc)
    if m:
        volts = re.findall(r"([\d.]+)V", m.group(1))
        nums = re.findall(r"\d+", m.group(2))
        cutoffs = {f"B{int(n):04d}": float(v) for n, v in zip(nums, volts)}
    return {
        b: dict(readme=desc, ambient_temp_c=float(temp.group(1)) if temp else None,
                discharge_profile=profile, cutoff_voltage_v=cutoffs.get(b))
        for b in ids
    }


def datevec(v):
    v = np.atleast_1d(v).astype(float)
    if v.size < 6 or np.isnan(v).any():
        return None
    y, mo, d, h, mi, s = v[:6]
    t = dt.datetime(int(y), int(mo), int(d)) + dt.timedelta(hours=h, minutes=mi, seconds=s)
    return t.isoformat(sep=" ")


def arr(data, key):
    v = data.get(key)
    return None if v is None else np.atleast_1d(np.asarray(v)).ravel()


def scalar(data, key):
    v = cscalar(data, key)
    return None if v is None else v.real


def cscalar(data, key):
    """Scalar that may be complex (some Re/Rct fits are); returns a complex or None."""
    v = data.get(key)
    if v is None:
        return None
    v = np.atleast_1d(np.asarray(v)).ravel()
    return complex(v[0]) if v.size and np.isfinite(v[0]) else None


def fmt(x):
    return NULL if x is None or not np.isfinite(x) else repr(float(x))


def columns(n, *series):
    """Yield per-row tuples of formatted values; missing/short series give NULL."""
    padded = []
    for s in series:
        if s is None:
            padded.append([NULL] * n)
        else:
            padded.append([fmt(x) for x in s[:n]] + [NULL] * max(0, n - len(s)))
    return zip(*padded)


def copy(conn, table, cols, text):
    if text:
        with conn.cursor().copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
            cp.write(text)


def load_battery(conn, mat_path, archive, meta):
    bid = mat_path.stem
    cyc = sio.loadmat(mat_path, simplify_cells=True)[bid]["cycle"]
    cyc = [cyc] if isinstance(cyc, dict) else list(cyc)

    conn.execute("DELETE FROM batteries WHERE battery_id = %s", (bid,))
    conn.execute(
        """INSERT INTO batteries (battery_id, source_archive, readme, ambient_temp_c,
               discharge_profile, cutoff_voltage_v, loaded_at)
           VALUES (%s, %s, %s, %s, %s, %s, now())""",
        (bid, archive, meta.get("readme"), meta.get("ambient_temp_c"),
         meta.get("discharge_profile"), meta.get("cutoff_voltage_v")),
    )

    cycles, meas, imp, rect = io.StringIO(), io.StringIO(), io.StringIO(), io.StringIO()
    type_counts = {}
    counts = dict(charge=0, discharge=0, impedance=0, points=0, impedance_points=0)
    for i, c in enumerate(cyc, 1):
        ctype = str(c["type"]).strip()
        type_counts[ctype] = type_counts.get(ctype, 0) + 1
        counts[ctype] += 1
        data = c.get("data") or {}
        capacity = re_ohm = rct_ohm = None
        n = 0
        if ctype in ("charge", "discharge"):
            t = arr(data, "Time")
            n = 0 if t is None else len(t)
            for k, row in enumerate(columns(
                n, t, arr(data, "Voltage_measured"), arr(data, "Current_measured"),
                arr(data, "Temperature_measured"), arr(data, "Current_charge"), arr(data, "Voltage_charge"),
                arr(data, "Current_load"), arr(data, "Voltage_load"),
            )):
                meas.write(f"{bid}\t{i}\t{k}\t" + "\t".join(row) + "\n")
            counts["points"] += n
            if ctype == "discharge":
                capacity = scalar(data, "Capacity")
        elif ctype == "impedance":
            series = [arr(data, k) for k in ("Sense_current", "Battery_current", "Current_ratio", "Battery_impedance")]
            n = max((len(s) for s in series if s is not None), default=0)
            parts = []
            for s in series:
                parts += [None, None] if s is None else [s.real, s.imag]
            for k, row in enumerate(columns(n, *parts)):
                imp.write(f"{bid}\t{i}\t{k}\t" + "\t".join(row) + "\n")
            r = arr(data, "Rectified_Impedance")
            if r is not None:
                for k, row in enumerate(columns(len(r), r.real, r.imag)):
                    rect.write(f"{bid}\t{i}\t{k}\t" + "\t".join(row) + "\n")
            counts["impedance_points"] += n
            re_ohm, rct_ohm = cscalar(data, "Re"), cscalar(data, "Rct")
        else:
            sys.exit(f"{bid} cycle {i}: unknown type {ctype!r}")
        cycles.write("\t".join([
            bid, str(i), ctype, str(type_counts[ctype]),
            fmt(scalar(c, "ambient_temperature")), datevec(c.get("time")) or NULL, str(n),
            fmt(capacity),
            *(f"{NULL}\t{NULL}" if z is None else f"{fmt(z.real)}\t{fmt(z.imag)}" for z in (re_ohm, rct_ohm)),
        ]) + "\n")

    copy(conn, "cycles", ["battery_id", "cycle_index", "type", "type_index", "ambient_temp_c",
                          "start_time", "n_points", "capacity_ah", "re_ohm", "re_ohm_im", "rct_ohm",
                          "rct_ohm_im"], cycles.getvalue())
    copy(conn, "measurements", ["battery_id", "cycle_index", "point_index", "time_s", "voltage_v", "current_a",
                                "temperature_c", "current_charge_a", "voltage_charge_v", "current_load_a",
                                "voltage_load_v"], meas.getvalue())
    copy(conn, "impedance_points", ["battery_id", "cycle_index", "point_index", "sense_current_re",
                                    "sense_current_im", "battery_current_re", "battery_current_im",
                                    "current_ratio_re", "current_ratio_im", "battery_impedance_re",
                                    "battery_impedance_im"], imp.getvalue())
    copy(conn, "rectified_impedance_points", ["battery_id", "cycle_index", "point_index", "impedance_re",
                                              "impedance_im"], rect.getvalue())
    conn.commit()
    print(f"{bid}: {len(cyc)} cycles ({counts['charge']} charge, {counts['discharge']} discharge, "
          f"{counts['impedance']} impedance), {counts['points']} samples, "
          f"{counts['impedance_points']} EIS points", flush=True)


def main():
    url = os.environ.get("DATABASE_URL")
    if not url or len(sys.argv) < 2:
        sys.exit(__doc__)
    with tempfile.TemporaryDirectory() as tmp, psycopg.connect(url) as conn:
        conn.execute((HERE / "schema.sql").read_text())
        conn.commit()
        for src in map(Path, sys.argv[1:]):
            if src.is_file() and zipfile.is_zipfile(src):
                d = Path(tmp) / src.stem
                with zipfile.ZipFile(src) as z:
                    z.extractall(d)
            else:
                d = src
            meta = {}
            for readme in sorted(d.rglob("README*.txt")):
                meta.update(parse_readme(readme.read_text(errors="replace")))
            for mat in sorted(d.rglob("B*.mat")):
                # Drop the "xxxxxxxx-" prefix uploads get.
                archive = re.sub(r"^[0-9a-f]{8}-(?=\d)", "", src.name)
                load_battery(conn, mat, archive, meta.get(mat.stem, {}))
        conn.execute("ANALYZE")
    print("done")


if __name__ == "__main__":
    main()
