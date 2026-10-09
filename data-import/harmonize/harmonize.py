#!/usr/bin/env python3
"""Phase 4: build the harmonized core tables from staging, one dataset per mapping file.

Usage:
    pip install pyyaml "psycopg[binary]"
    DATABASE_URL=postgresql://postgres:password@localhost:5432/battery_aging \
        python harmonize.py mappings/*.yaml

Each mapping file (see mappings/ and README.md) says which staging tables of one dataset hold time series,
impedance spectra, per-cycle values or cell metadata, which column is which, and how to convert units and signs.
Loading a mapping replaces everything previously loaded for that dataset, so re-running is safe. After each
dataset, per-cycle summaries are derived from the time series and physical-range checks are recorded in
core.quality_issue.
"""
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import psycopg
import yaml
from psycopg import sql

HERE = Path(__file__).resolve().parent

TIMESERIES = ["t_s", "step_time_s", "ts", "cycle_number", "step_number", "step_label", "current_a", "voltage_v", "temperature_c",
              "chamber_temp_c", "capacity_ah", "energy_wh", "power_w", "soc"]
EIS = ["frequency_hz", "z_re_ohm", "z_im_ohm"]
CYCLE = ["cycle_number", "charge_ah", "discharge_ah", "soh_pct", "temp_max_c"]
CHECKUP = ["checkup_index", "elapsed_h", "efc", "capacity_ah", "resistance_ohm", "soh_pct"]
INT_FIELDS = {"cycle_number", "step_number"}
TEXT_FIELDS = {"step_label"}
TIME_FIELDS = {"ts"}
NUM_RE = re.compile(r"^[+-]?(\d+([.,]\d*)?|[.,]\d+)([eE][+-]?\d+)?$")
DATETIME_FORMATS = ["%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                    "%Y/%m/%d %H:%M:%S", "%m/%d/%Y %I:%M %p"]


# ---------------------------------------------------------------- value parsing

def number(s, decimal="."):
    s = (s or "").strip()
    if not s or not NUM_RE.match(s):
        return None
    if decimal == "," and s.count(",") == 1:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        return None
    x = float(s)
    return x if math.isfinite(x) else None


def hms_seconds(s):
    """'31:19:10.876', '0:00:09:000' (h:m:s:ms) or '1-03:58:38' (d-h:m:s) -> seconds."""
    s = (s or "").strip()
    m = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d+):(\d+)(?:[.:](\d+))?", s)
    if not m:
        return number(s)
    d, h, mi, sec, frac = m.groups()
    fraction = int(frac) / 10 ** len(frac) if frac else 0.0
    return int(d or 0) * 86400 + int(h) * 3600 + int(mi) * 60 + int(sec) + fraction


def timestamp(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", ""))
    except ValueError:
        pass
    for f in DATETIME_FORMATS:
        try:
            return dt.datetime.strptime(s, f)
        except ValueError:
            continue
    return None


def convert(raw, spec, field, decimal):
    """Turn one cell into the field's value using the column spec (scale, offset, sign, parse)."""
    parse = spec.get("parse")
    if field in TEXT_FIELDS or parse == "text":
        v = (raw or "").strip()
        return v or None
    if field in TIME_FIELDS or parse == "datetime":
        return timestamp(raw)
    if parse in ("complex_re", "complex_im"):
        try:
            z = complex((raw or "").strip().replace(" ", "").strip("()"))
        except ValueError:
            return None
        x = z.real if parse == "complex_re" else z.imag
    else:
        x = hms_seconds(raw) if parse == "hms" else number(raw, decimal)
    if x is None:
        return None
    x = x * spec.get("scale", 1) * spec.get("sign", 1) + spec.get("offset", 0)
    return int(round(x)) if field in INT_FIELDS else x


# ---------------------------------------------------------------- table context values (cell, test fields)

def ctx_value(spec, ctx):
    """Evaluate a value spec against a table (or a column of a wide table):
    {const}, {path: regex}, {name: regex}, {header: regex}, {attr: key}, {path_map: [[regex, value], ...]},
    with optional default, map, number and template ('{}' or '{0}-{1}' for several regex groups)."""
    if spec is None:
        return None
    if not isinstance(spec, dict):
        return spec
    v, groups = None, ()
    if "const" in spec:
        v = spec["const"]
    elif any(k in spec for k in ("path", "name", "header")):
        key = next(k for k in ("path", "name", "header") if k in spec)
        m = re.search(spec[key], ctx.get(key) or "")
        if m:
            groups = m.groups()
            v = groups[0] if groups else m.group(0)
    elif "attr" in spec:
        v = (ctx.get("attributes") or {}).get(spec["attr"])
    elif "path_map" in spec:
        for pattern, value in spec["path_map"]:
            if re.search(pattern, ctx["path"]):
                v = value
                break
    if v is not None and "sibling" in spec and groups:
        # Value from another table of the same file: sibling names it ('{0}.cell'), the last regex group is the row.
        v = ctx["lookup"](spec["sibling"].format(*groups), int(groups[-1]))
        if v is not None and spec.get("template"):
            v = spec["template"].format(*groups, v)
            groups, spec = (), {k: x for k, x in spec.items() if k != "template"}
    if v is None:
        v = spec.get("default")
    if v is not None and "map" in spec:
        v = spec["map"].get(str(v), v)
    if v is not None and spec.get("number"):
        v = number(str(v).replace("n", "-", 1) if str(v).startswith("n") else str(v))
        if v is not None and v.is_integer():
            v = int(v)
    if v is not None and spec.get("template"):
        v = spec["template"].format(*(groups if len(groups) > 1 else (v,)))
    return v


# ---------------------------------------------------------------- loading one mapping

class Loader:
    def __init__(self, conn, mapping, path):
        self.conn, self.m, self.path = conn, mapping, path
        self.key = mapping["dataset_key"]
        self.cells, self.tests = set(), {}
        self.counts = defaultdict(int)

    def source_ids(self):
        ids = [r[0] for r in self.conn.execute("SELECT source_id FROM ingest.source WHERE url ILIKE %s",
                                               (f"%{self.m['source_match']}%",))]
        if not ids:
            sys.exit(f"{self.path}: no ingest.source URL contains {self.m['source_match']!r}")
        return ids

    def start(self):
        c = self.conn
        sha = hashlib.sha256(Path(self.path).read_bytes()).hexdigest()
        c.execute("DELETE FROM dataset WHERE dataset_key = %s", (self.key,))
        self.ids = self.source_ids()
        refs = c.execute("""SELECT string_agg(DISTINCT d.local_ref, ', ') FROM ingest.source s
                            JOIN catalog.dataset d ON d.dataset_id = ANY (s.dataset_ids)
                            WHERE s.source_id = ANY (%s)""", (self.ids,)).fetchone()[0]
        c.execute("""INSERT INTO dataset (dataset_key, source_ids, catalog_refs, title, mapping_file, mapping_sha256, notes)
                     VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                  (self.key, self.ids, refs, self.m.get("title"), Path(self.path).name, sha, self.m.get("notes")))

    def cell(self, label, extra_attrs=None):
        label = str(label).strip()
        uid = f"{self.key}:{label}"
        if uid not in self.cells:
            d = dict(self.m.get("cell_defaults", {}))
            for o in self.m.get("cell_overrides", []):  # first override whose 'match' regex fits the label
                if re.search(o["match"], label):
                    d.update({k: v for k, v in o.items() if k != "match"})
                    break
            self.conn.execute(
                """INSERT INTO cell (cell_uid, dataset_key, source_label, manufacturer, model, chemistry, form_factor,
                       nominal_capacity_ah, is_synthetic, attributes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (cell_uid) DO NOTHING""",
                (uid, self.key, label, d.get("manufacturer"), d.get("model"), d.get("chemistry"), d.get("form_factor"),
                 d.get("nominal_capacity_ah"), d.get("is_synthetic", False),
                 json.dumps(extra_attrs) if extra_attrs else None))
            self.cells.add(uid)
        return uid

    def test(self, cell_uid, name, rule, ctx):
        key = (cell_uid, name)
        if key not in self.tests:
            t = rule.get("test", {})
            attrs = {k: ctx_value(v, ctx) for k, v in (t.get("attributes") or {}).items()}
            attrs = {k: v for k, v in attrs.items() if v is not None}
            ttype = ctx_value(t.get("type"), ctx) or "characterization"
            self.tests[key] = self.conn.execute(
                """INSERT INTO test (cell_uid, name, test_type, ambient_temp_c, attributes, staging_tables)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING test_id""",
                (cell_uid, name, ttype, ctx_value(t.get("ambient_temp_c"), ctx), json.dumps(attrs) if attrs else None,
                 [ctx["table_id"]])).fetchone()[0]
            self.counts["tests"] += 1
        else:
            self.conn.execute("UPDATE test SET staging_tables = array_append(staging_tables, %s) WHERE test_id = %s",
                              (ctx["table_id"], self.tests[key]))
        return self.tests[key]

    def tables(self, rule):
        w = rule.get("where", {})
        rows = self.conn.execute("""
            SELECT v.table_id, coalesce(nullif(v.member_path, ''), v.file_path), v.name, v.kind, v.columns, v.units,
                   v.data_start_row, v.decimal_mark, v.attributes, s.unit_id
            FROM staging.v_tables v JOIN staging.source_table s USING (table_id)
            WHERE v.source_id = ANY (%s) ORDER BY v.table_id""", (self.ids,)).fetchall()
        out = []
        for tid, path, name, kind, cols, units, start, dec, attrs, unit_id in rows:
            if w.get("kind") and kind != w["kind"]:
                continue
            if w.get("path") and not re.search(w["path"], path):
                continue
            if w.get("exclude_path") and re.search(w["exclude_path"], path):
                continue
            if w.get("table") and not re.search(w["table"], name or ""):
                continue
            norm = [(c or "").strip().lower() for c in (cols or [])]
            if any(h.strip().lower() not in norm for h in w.get("has_columns", [])):
                continue
            out.append(dict(table_id=tid, path=path, name=name or "", kind=kind, columns=cols or [], units=units or [],
                            data_start=rule.get("data_start", start), decimal=dec or ".", attributes=attrs,
                            lookup=lambda tname, row, u=unit_id: self.lookup(u, tname, row)))
        return out

    def lookup(self, unit_id, table_name, row):
        """First cell of row `row` (0-based, after the header) of another table in the same file."""
        r = self.conn.execute("""SELECT c.cells[1] FROM staging.source_table t JOIN staging.cell_row c USING (table_id)
                                 WHERE t.unit_id = %s AND t.name = %s AND c.row_index >= coalesce(t.data_start_row, 0)
                                 ORDER BY c.row_index OFFSET %s LIMIT 1""",
                              (unit_id, table_name, row)).fetchone()
        return r[0].strip() if r and r[0] else None

    def rows(self, t):
        start = t["data_start"] or 0
        return self.conn.execute("SELECT cells FROM staging.cell_row WHERE table_id = %s AND row_index >= %s "
                                 "ORDER BY row_index", (t["table_id"], start)).fetchall()

    def column_index(self, t, spec, field):
        if "index" in spec:
            return spec["index"]
        names = [(c or "").strip().lower() for c in t["columns"]]
        want = spec["col"].strip().lower()
        if want not in names:
            raise KeyError(f"column {spec['col']!r} (for {field}) not in table {t['path']} {t['name']}: {t['columns']}")
        return names.index(want)

    # -------------------------------------------------- produce: timeseries / eis / cycle (long)

    def load_long(self, rule, t, fields_allowed, target):
        cols = rule["columns"]
        unknown = set(cols) - set(fields_allowed)
        if unknown:
            raise ValueError(f"{self.path}: fields {sorted(unknown)} are not valid for {rule['produce']}")
        # Each field comes from a column (col / index), a value of the table itself (ctx, e.g. a cycle number
        # in the table name), or the row's position (row_number).
        getters = []
        for f, spec in cols.items():
            if "col" in spec or "index" in spec:
                getters.append(("col", self.column_index(t, spec, f), spec, f))
            elif "ctx" in spec:
                v = ctx_value(spec["ctx"], t)
                getters.append(("const", int(v) if f in INT_FIELDS and v is not None else v, spec, f))
            elif spec.get("row_number"):
                getters.append(("row", spec.get("start", 0), spec, f))
            else:
                raise ValueError(f"{self.path}: field {f} needs col, index, ctx or row_number")
        cell_spec = rule["cell"]
        cell_col = self.column_index(t, cell_spec, "cell") if "col" in cell_spec or "index" in cell_spec else None
        ctx = t
        per_test = defaultdict(list)
        fixed_cell = None if cell_col is not None else ctx_value(cell_spec, ctx)
        if cell_col is None and fixed_cell is None:
            raise ValueError(f"could not work out the cell for {t['path']} {t['name']}")
        for r, (cells,) in enumerate(self.rows(t)):
            label = fixed_cell if cell_col is None else (cells[cell_col].strip() if cell_col < len(cells) else "")
            if not label:
                continue
            rec = []
            for kind, arg, spec, f in getters:
                if kind == "col":
                    rec.append(convert(cells[arg] if arg < len(cells) else "", spec, f, t["decimal"]))
                elif kind == "const":
                    rec.append(arg)
                else:
                    rec.append(r + arg)
            if all(rec[k] is None for k, g in enumerate(getters) if g[0] == "col"):
                continue
            per_test[label].append(rec)
        fields = [g[3] for g in getters]
        for label, recs in per_test.items():
            cell_uid = self.cell(label)
            name = ctx_value(rule.get("test", {}).get("name"), ctx) or (t["path"] + (f" :: {t['name']}" if t["name"] and t["name"] != t["path"].rsplit("/", 1)[-1] else ""))
            test_id = self.test(cell_uid, name, rule, ctx)
            if target in ("cycle", "checkup"):
                with self.conn.cursor().copy(sql.SQL("COPY {} (test_id, origin, {}) FROM STDIN").format(
                        sql.Identifier(target), sql.SQL(", ").join(map(sql.Identifier, fields)))) as cp:
                    for r in recs:
                        cp.write_row([test_id, "reported", *r])
            else:
                base = self.conn.execute(sql.SQL("SELECT coalesce(max(seq) + 1, 0) FROM {} WHERE test_id = %s").format(
                    sql.Identifier(target)), (test_id,)).fetchone()[0]
                with self.conn.cursor().copy(sql.SQL("COPY {} (test_id, seq, {}) FROM STDIN").format(
                        sql.Identifier(target), sql.SQL(", ").join(map(sql.Identifier, fields)))) as cp:
                    for k, r in enumerate(recs):
                        cp.write_row([test_id, base + k, *r])
            self.counts[target] += len(recs)

    # -------------------------------------------------- produce: checkup (wide layout, one column per test)

    def load_wide_tests(self, rule, t):
        """Rows are checkpoints (storage time or cycles), each column one test condition / cell."""
        w = rule["wide_tests"]
        id_col = w.get("id_col", 0)
        names = [(c or "").strip() for c in t["columns"]]
        for j, header in enumerate(names):
            if j == id_col or not header or (w.get("columns") and not re.search(w["columns"], header)):
                continue
            col_ctx = dict(t, header=header)
            label = ctx_value(rule["cell"], col_ctx)
            if not label:
                continue
            recs = []
            for k, (cells,) in enumerate(self.rows(t)):
                x = number(cells[id_col] if id_col < len(cells) else "", t["decimal"])
                v = number(cells[j] if j < len(cells) else "", t["decimal"])
                if x is None or v is None:
                    continue
                recs.append((k, x * w.get("id_scale", 1), v * w.get("value_scale", 1)))
            if not recs:
                continue
            name = ctx_value(rule.get("test", {}).get("name"), col_ctx) or f"{t['path']} :: {header}"
            test_id = self.test(self.cell(label), name, rule, col_ctx)
            # Upsert: several files can each add one measure (capacity, resistance) to the same checkpoints.
            with self.conn.cursor() as cur:
                cur.executemany(sql.SQL(
                    "INSERT INTO checkup (test_id, checkup_index, origin, {a}, {b}) VALUES (%s, %s, 'reported', %s, %s) "
                    "ON CONFLICT (test_id, checkup_index, origin) DO UPDATE SET {a} = EXCLUDED.{a}, {b} = EXCLUDED.{b}"
                ).format(a=sql.Identifier(w["id_field"]), b=sql.Identifier(w["value_field"])),
                    [(test_id, k, x, v) for k, x, v in recs])
            self.counts["checkup"] += len(recs)

    # -------------------------------------------------- produce: cycle (wide layout, one column group per cell)

    def load_wide_cycles(self, rule, t):
        """Header row holds cell labels (one per column group), units row holds the field of each column."""
        wide = rule["wide"]
        groups, last = [], None
        for c in t["columns"]:
            last = c.strip() if c and c.strip() else last
            groups.append(last)
        id_col = wide.get("id_col", 0)
        field_of = {}
        for j, u in enumerate(t["units"]):
            if j == id_col or groups[j] is None:
                continue
            for pattern, field in wide["fields"].items():
                if re.search(pattern, u or "", re.I):
                    field_of[j] = field
        per_cell = defaultdict(dict)
        for (cells,) in self.rows(t):
            cyc = number(cells[id_col] if id_col < len(cells) else "", t["decimal"])
            if cyc is None:
                continue
            for j, field in field_of.items():
                v = number(cells[j] if j < len(cells) else "", t["decimal"])
                if v is not None:
                    m = re.search(rule["cell"]["group"], groups[j])
                    label = rule["cell"].get("template", "{}").format(m.group(1) if m and m.groups() else groups[j])
                    per_cell[label].setdefault(int(cyc), {})[field] = v
        for label, by_cycle in per_cell.items():
            test_id = self.test(self.cell(label), ctx_value(rule["test"].get("name"), t) or "aging", rule, t)
            with self.conn.cursor().copy("COPY cycle (test_id, cycle_number, origin, discharge_ah, soh_pct) FROM STDIN") as cp:
                for cyc, vals in sorted(by_cycle.items()):
                    cp.write_row([test_id, cyc, "reported", vals.get("discharge_ah"), vals.get("soh_pct")])
            self.counts["cycle"] += len(by_cycle)

    # -------------------------------------------------- produce: cell attributes

    def load_cell_attributes(self, rule, t):
        key = self.column_index(t, rule["cell"], "cell")
        names = [(c or "").strip() for c in t["columns"]]
        for (cells,) in self.rows(t):
            label = cells[key].strip() if key < len(cells) else ""
            if not label:
                continue
            attrs = {names[j]: cells[j] for j in range(len(cells)) if j != key and j < len(names) and names[j] and cells[j].strip()}
            uid = self.cell(label)
            self.conn.execute("UPDATE cell SET attributes = coalesce(attributes, '{}'::jsonb) || %s::jsonb WHERE cell_uid = %s",
                              (json.dumps(attrs), uid))
            self.counts["cell_attributes"] += 1

    # -------------------------------------------------- after loading

    def derive_cycles(self):
        """Per-cycle charge / discharge capacity from the time series. Capacity is integrated from current and
        test time where the test has t_s; otherwise it is the largest capacity the tester reported while
        charging / discharging in that cycle (for testers that reset capacity every step)."""
        self.conn.execute("""
            INSERT INTO cycle (test_id, cycle_number, origin, charge_ah, discharge_ah, temp_max_c, duration_s, n_samples)
            SELECT test_id, cycle_number, 'derived',
                   coalesce(sum(CASE WHEN current_a > 0 THEN current_a * dt END) / 3600,
                            max(abs(capacity_ah)) FILTER (WHERE current_a > 0)),
                   coalesce(sum(CASE WHEN current_a < 0 THEN -current_a * dt END) / 3600,
                            max(abs(capacity_ah)) FILTER (WHERE current_a < 0)),
                   max(temperature_c), max(t_s) - min(t_s), count(*)
            FROM (SELECT s.*, CASE WHEN t_s - lag(t_s) OVER w > 0 AND t_s - lag(t_s) OVER w < 3600
                                   THEN t_s - lag(t_s) OVER w END AS dt
                  FROM timeseries s JOIN test USING (test_id) JOIN cell c USING (cell_uid)
                  WHERE c.dataset_key = %s
                  WINDOW w AS (PARTITION BY s.test_id ORDER BY seq)) x
            WHERE cycle_number IS NOT NULL
            GROUP BY test_id, cycle_number""", (self.key,))

    def checks(self):
        nominal = self.m.get("cell_defaults", {}).get("nominal_capacity_ah")
        imax = 20 * nominal if nominal else 100
        q = [
            ("voltage outside -0.1..5 V", "SELECT test_id, count(*) FROM timeseries JOIN test USING (test_id) JOIN cell USING (cell_uid) "
             "WHERE dataset_key = %(k)s AND (voltage_v < -0.1 OR voltage_v > 5) GROUP BY 1"),
            (f"current above {imax:g} A", "SELECT test_id, count(*) FROM timeseries JOIN test USING (test_id) JOIN cell USING (cell_uid) "
             "WHERE dataset_key = %(k)s AND abs(current_a) > %(imax)s GROUP BY 1"),
            ("temperature outside -50..90 °C", "SELECT test_id, count(*) FROM timeseries JOIN test USING (test_id) JOIN cell USING (cell_uid) "
             "WHERE dataset_key = %(k)s AND (temperature_c < -50 OR temperature_c > 90) GROUP BY 1"),
            ("time goes backwards", "SELECT test_id, count(*) FROM (SELECT test_id, t_s, lag(t_s) OVER (PARTITION BY test_id ORDER BY seq) p "
             "FROM timeseries JOIN test USING (test_id) JOIN cell USING (cell_uid) WHERE dataset_key = %(k)s) x WHERE t_s < p GROUP BY 1"),
            ("EIS frequency not positive", "SELECT test_id, count(*) FROM eis_point JOIN test USING (test_id) JOIN cell USING (cell_uid) "
             "WHERE dataset_key = %(k)s AND NOT frequency_hz > 0 GROUP BY 1"),
        ]
        if nominal:
            q.append((f"cycle capacity above 1.3 x nominal ({nominal} Ah)",
                      "SELECT test_id, count(*) FROM cycle JOIN test USING (test_id) JOIN cell USING (cell_uid) "
                      "WHERE dataset_key = %(k)s AND discharge_ah > 1.3 * %(nom)s GROUP BY 1"))
        for name, query in q:
            for test_id, n in self.conn.execute(query, {"k": self.key, "imax": imax, "nom": nominal}):
                self.conn.execute("INSERT INTO quality_issue (dataset_key, test_id, check_name, n_rows) VALUES (%s, %s, %s, %s)",
                                  (self.key, test_id, name, n))
                self.counts["quality_issues"] += 1

    def run(self):
        self.start()
        if self.m.get("not_harmonized"):
            # Recorded with the reason, so the catalog shows why this dataset has no core rows.
            self.conn.execute("UPDATE dataset SET status = 'not_harmonized', notes = %s, loaded_at = now() "
                              "WHERE dataset_key = %s", (self.m["not_harmonized"].strip(), self.key))
            return
        for rule in self.m["tables"]:
            produce = rule["produce"]
            tables = self.tables(rule)
            if not tables and not rule.get("optional"):
                raise ValueError(f"{self.path}: rule {rule.get('label', produce)!r} matched no staging tables")
            for t in tables:
                if produce == "timeseries":
                    self.load_long(rule, t, TIMESERIES, "timeseries")
                elif produce == "eis":
                    self.load_long(rule, t, EIS, "eis_point")
                elif produce == "cycle" and rule.get("wide"):
                    self.load_wide_cycles(rule, t)
                elif produce == "cycle":
                    self.load_long(rule, t, CYCLE, "cycle")
                elif produce == "checkup" and rule.get("wide_tests"):
                    self.load_wide_tests(rule, t)
                elif produce == "checkup":
                    self.load_long(rule, t, CHECKUP, "checkup")
                elif produce == "cell_attributes":
                    self.load_cell_attributes(rule, t)
                else:
                    raise ValueError(f"unknown produce: {produce}")
        self.derive_cycles()
        self.checks()
        self.conn.execute("UPDATE dataset SET loaded_at = now() WHERE dataset_key = %s", (self.key,))


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    db = os.environ.get("DATABASE_URL")
    if not db:
        sys.exit("set DATABASE_URL, e.g. postgresql://postgres:password@localhost:5432/battery_aging")
    schema = os.environ.get("DB_SCHEMA", "core")
    with psycopg.connect(db) as conn:
        if not conn.execute("SELECT to_regclass('staging.cell_row')").fetchone()[0]:
            sys.exit("staging tables not found: run data-import/staging/parse.py first")
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute((HERE / "schema.sql").read_text(encoding="utf-8"))
        conn.commit()
        for path in sys.argv[1:]:
            mapping = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            loader = Loader(conn, mapping, path)
            try:
                loader.run()
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"FAILED {path}: {type(e).__name__}: {e}", flush=True)
                continue
            c = loader.counts
            if mapping.get("not_harmonized"):
                print(f"{mapping['dataset_key']}: recorded as not harmonized ({mapping['not_harmonized'].strip()[:80]})",
                      flush=True)
                continue
            print(f"{mapping['dataset_key']}: {len(loader.cells)} cells, {c['tests']} tests, {c['timeseries']:,} samples, "
                  f"{c['eis_point']:,} EIS points, {c['cycle']:,} reported cycles, {c['checkup']:,} checkups, {c['quality_issues']} quality issues",
                  flush=True)
    print(f"done: tables are in schema {schema!r}")


if __name__ == "__main__":
    main()
