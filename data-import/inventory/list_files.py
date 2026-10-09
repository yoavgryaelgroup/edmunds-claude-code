#!/usr/bin/env python3
"""Phase 1: list every file behind every dataset link in the catalog, without downloading.

Usage:
    pip install requests "psycopg[binary]"
    DATABASE_URL=postgresql://postgres:password@localhost:5432/battery_aging python list_files.py [options]

Reads the dataset URLs from catalog.dataset (load the workbook with ../catalog/load_workbook.py first),
asks each hosting repository for its file list, and stores the result in the schema named by DB_SCHEMA
(default: ingest): ingest.source has one row per URL with its status and total size, ingest.remote_file
one row per file with size, checksum, download link and format.

Options:
    --only PLATFORM_OR_URL ...   list only these platforms (e.g. mendeley zenodo) or URLs
    --refresh                    list sources again even if they were listed before

Sources that need a login, block scripts, or need an API key are marked 'manual' with the reason, so the
team can check them by hand. Re-running retries everything that is not yet 'listed'.
"""
import argparse
import html
import os
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import psycopg
import requests
from psycopg import sql

HERE = Path(__file__).resolve().parent
UA = "battery-data-catalog/1.0 (dataset inventory; contact via repository owner)"
DATA_EXT = r"zip|7z|rar|tar|gz|tgz|bz2|xz|mat|csv|tsv|txt|xlsx|xls|xlsm|h5|hdf5|hdf|nc|parquet|pkl|pickle|json|feather|npz|npy|mpr|nda|ndax|res"

session = requests.Session()
session.headers["User-Agent"] = UA


class Manual(Exception):
    """The source cannot be listed by a script; the message says why."""


# ---------------------------------------------------------------- helpers

def get(url, **kw):
    for attempt in range(4):
        try:
            r = session.get(url, timeout=60, **kw)
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(int(r.headers.get("Retry-After", 2 ** (attempt + 1))))
            continue
        return r
    return r


def curl_json(url, params=None):
    """Some sites (Mendeley Data) refuse Python's HTTP client but answer curl, which ships with Windows 10+."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    out = subprocess.run(["curl", "-sSfL", "--retry", "3", "-A", UA, url], capture_output=True, check=True)
    return json.loads(out.stdout)


def get_json(url, **kw):
    r = get(url, **kw)
    if r.status_code == 403 and shutil.which("curl"):
        return curl_json(url, kw.get("params"))
    r.raise_for_status()
    return r.json()


def resolve(url):
    """Final URL after redirects (used for DOI links)."""
    r = get(url, allow_redirects=True, stream=True)
    r.close()
    return r.url


def extension(path):
    name = path.rsplit("/", 1)[-1].lower()
    for double in ("tar.gz", "tar.bz2", "tar.xz"):
        if name.endswith("." + double):
            return double
    return name.rsplit(".", 1)[-1] if "." in name else ""


FAMILIES = {
    "tabular": {"csv", "tsv", "txt", "xlsx", "xls", "xlsm", "dat"},
    "matlab": {"mat", "m", "fig"},
    "archive": {"zip", "7z", "rar", "tar", "gz", "tgz", "bz2", "xz", "tar.gz", "tar.bz2", "tar.xz"},
    "hdf5": {"h5", "hdf5", "hdf", "nc"},
    "pickle": {"pkl", "pickle", "npz", "npy", "joblib"},
    "json": {"json"},
    "columnar": {"parquet", "feather", "arrow"},
    "cycler_native": {"mpr", "mpt", "nda", "ndax", "res", "cyc", "001"},
    "document": {"pdf", "md", "docx", "doc", "rtf", "html", "htm"},
    "image": {"png", "jpg", "jpeg", "tif", "tiff", "svg", "gif"},
    "code": {"py", "ipynb", "r", "jl", "sh"},
}


def family(ext):
    for name, exts in FAMILIES.items():
        if ext in exts:
            return name
    return "other"


def f(path, size=None, checksum=None, algo=None, url=None):
    return dict(path=path, size=int(size) if size not in (None, "") else None, checksum=checksum,
                algo=algo, url=url)


# ---------------------------------------------------------------- adapters
# Each takes the dataset URL and returns (title, [file dicts]).

def mendeley(url):
    m = re.search(r"datasets/([a-z0-9]{10})(?:/(\d+))?", url) or re.search(r"/([a-z0-9]{10})-(\d+)\.zip", url)
    if not m:
        raise ValueError("no Mendeley dataset id in URL")
    ds, ver = m.group(1), m.group(2)
    api = "https://data.mendeley.com/public-api/datasets"
    meta = get_json(f"{api}/{ds}" + (f"?version={ver}" if ver else ""))
    ver = ver or str(meta.get("version"))
    folders = get_json(f"{api}/{ds}/folders/{ver}")
    by_id = {x["id"]: x for x in folders}

    def folder_path(fid):
        parts = []
        while fid in by_id:
            parts.append(by_id[fid]["name"])
            fid = by_id[fid].get("parent_id")
        return "/".join(reversed(parts))

    files = []
    for fid in ["root"] + list(by_id):
        for x in get_json(f"{api}/{ds}/files", params={"folder_id": fid, "version": ver}):
            cd = x.get("content_details") or {}
            prefix = folder_path(fid) if fid != "root" else ""
            files.append(f(f"{prefix}/{x['filename']}" if prefix else x["filename"], x.get("size"),
                           cd.get("sha256_hash"), "sha256", cd.get("download_url")))
    return meta.get("name"), files


def zenodo(url):
    m = re.search(r"zenodo\.org/(?:records?|deposit)/(\d+)", url)
    if not m:
        m = re.search(r"zenodo\.org/(?:records?)/(\d+)", resolve(url))
    if not m:
        raise ValueError("could not find a Zenodo record id")
    rec = get_json(f"https://zenodo.org/api/records/{m.group(1)}")
    files = []
    for x in rec.get("files", []):
        algo, _, digest = (x.get("checksum") or "").partition(":")
        files.append(f(x["key"], x.get("size"), digest or None, algo or None,
                       (x.get("links") or {}).get("self")))
    return (rec.get("metadata") or {}).get("title"), files


def figshare_article_files(api, article_id, prefix=""):
    out = []
    for x in get_json(f"{api}/articles/{article_id}/files", params={"page_size": 1000}):
        out.append(f(prefix + x["name"], x.get("size"), x.get("computed_md5") or x.get("supplied_md5"), "md5",
                     x.get("download_url")))
    return out


def figshare(url):
    final = url
    if "doi.org" in url:
        final = resolve(url)
    api = "https://data.4tu.nl/v2" if "4tu.nl" in final else "https://api.figshare.com/v2"
    m = re.search(r"/collections/[^/]+/(\d+|[0-9a-f-]{36})", final)
    if m:
        coll = get_json(f"{api}/collections/{m.group(1)}")
        files = []
        for a in get_json(f"{api}/collections/{m.group(1)}/articles", params={"page_size": 1000}):
            aid = a.get("uuid") or a["id"]
            files += figshare_article_files(api, aid, prefix=a.get("title", str(aid)).strip() + "/")
        return coll.get("title"), files
    m = re.search(r"/articles/(?:[^/]+/)*?(\d+|[0-9a-f-]{36})(?:/\d+)?/?$", final.split("?")[0])
    if not m:
        raise ValueError(f"no Figshare article id in {final}")
    art = get_json(f"{api}/articles/{m.group(1)}")
    return art.get("title"), figshare_article_files(api, art.get("uuid") or art["id"])


def dryad(url):
    m = re.search(r"(doi:10\.\d+/[^\s?#]+)", urllib.parse.unquote(url))
    if not m:
        raise ValueError("no DOI in Dryad URL")
    base = "https://datadryad.org"
    ds = get_json(f"{base}/api/v2/datasets/{urllib.parse.quote(m.group(1), safe='')}")
    ver = ds["_links"]["stash:version"]["href"]
    files, nxt = [], f"{base}{ver}/files"
    while nxt:
        page = get_json(nxt)
        for x in page.get("_embedded", {}).get("stash:files", []):
            dl = x.get("_links", {}).get("stash:download", {}).get("href")
            files.append(f(x["path"], x.get("size"), x.get("digest"), x.get("digestType"),
                           base + dl if dl else None))
        n = page.get("_links", {}).get("next", {}).get("href")
        nxt = base + n if n else None
    return ds.get("title"), files


def osf(url):
    m = re.search(r"osf\.io/([a-z0-9]{5})", url)
    view = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("view_only", [None])[0]
    params = {"view_only": view} if view else {}
    node = get_json(f"https://api.osf.io/v2/nodes/{m.group(1)}/", params=params)
    files = []

    def walk(link, prefix):
        while link:
            page = get_json(link, params=params)
            for x in page["data"]:
                a = x["attributes"]
                if a["kind"] == "folder":
                    walk(x["relationships"]["files"]["links"]["related"]["href"], prefix + a["name"] + "/")
                else:
                    files.append(f(prefix + a["name"], a.get("size"), (a.get("extra") or {}).get("hashes", {}).get("sha256"),
                                   "sha256", x["links"].get("download")))
            link = page["links"].get("next")

    walk(f"https://api.osf.io/v2/nodes/{m.group(1)}/files/osfstorage/", "")
    return node["data"]["attributes"].get("title"), files


def invenio_rdm(url):
    u = urllib.parse.urlparse(url)
    rec = re.search(r"/records/([^/?#]+)", u.path).group(1)
    base = f"{u.scheme}://{u.netloc}"
    meta = get_json(f"{base}/api/records/{rec}")
    files = []
    for x in get_json(f"{base}/api/records/{rec}/files").get("entries", []):
        algo, _, digest = (x.get("checksum") or "").partition(":")
        files.append(f(x["key"], x.get("size"), digest or None, algo or None, (x.get("links") or {}).get("content")))
    return (meta.get("metadata") or {}).get("title"), files


def dspace_depositonce(url):
    uuid = re.search(r"/items/([0-9a-f-]{36})", url).group(1)
    api = "https://api-depositonce.tu-berlin.de/server/api/core/items"
    item = get_json(f"{api}/{uuid}")
    bundles = get_json(f"{api}/{uuid}/bundles", params={"embed": "bitstreams"})
    files = []
    for b in bundles["_embedded"]["bundles"]:
        if b["name"] != "ORIGINAL":
            continue
        for x in b["_embedded"]["bitstreams"]["_embedded"]["bitstreams"]:
            cs = x.get("checkSum") or {}
            files.append(f(x["name"], x.get("sizeBytes"), cs.get("value"), (cs.get("checkSumAlgorithm") or "").lower(),
                           x["_links"]["content"]["href"]))
    return item.get("name"), files


def github(url):
    m = re.search(r"github\.com/([^/]+)/([^/#?]+)", url)
    owner, repo = m.group(1), m.group(2).removesuffix(".git")
    r = get(f"https://api.github.com/repos/{owner}/{repo}")
    if r.status_code == 404:
        raise Manual("repository not found at this address (the Scrape Notes say it moved)")
    info = r.json()
    branch = info.get("default_branch", "main")
    tree = get_json(f"https://api.github.com/repos/{info['full_name']}/git/trees/{branch}", params={"recursive": 1})
    files = [f(x["path"], x.get("size"), x.get("sha"), "git-sha1",
               f"https://raw.githubusercontent.com/{info['full_name']}/{branch}/{x['path']}")
             for x in tree.get("tree", []) if x["type"] == "blob"]
    return info.get("full_name"), files


def html_links(url):
    """Generic fallback: data-file links on the landing page, sized with HEAD requests."""
    r = get(url)
    if r.status_code in (401, 403):
        raise Manual(f"the site refuses scripted access (HTTP {r.status_code})")
    r.raise_for_status()
    page = r.text
    if "fast-challenge" in page or "cf-challenge" in page or "captcha" in page.lower()[:5000]:
        raise Manual("the site shows a browser check (JavaScript challenge) to scripts")
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    links, seen = [], set()
    for href in re.findall(r'href=["\']([^"\']+)["\']', page):
        full = urllib.parse.urljoin(r.url, html.unescape(href))
        path = urllib.parse.urlparse(full).path
        if re.search(rf"\.({DATA_EXT})$", path, re.I) and full not in seen:
            seen.add(full)
            links.append(full)
    files = []
    for link in links[:500]:
        size = None
        try:
            h = session.head(link, allow_redirects=True, timeout=60)
            size = h.headers.get("Content-Length") if h.ok else None
        except requests.RequestException:
            pass
        files.append(f(urllib.parse.unquote(urllib.parse.urlparse(link).path.rsplit("/", 1)[-1]), size, url=link))
        time.sleep(0.2)
    return (html.unescape(re.sub(r"\s+", " ", title.group(1))).strip() if title else None), files


MANUAL = {
    "ieee_dataport": "IEEE DataPort needs a signed-in IEEE account to see and download files",
    "onedrive": "OneDrive folder needs a Microsoft sign-in",
    "google_drive": "Google Drive folder: list it in a browser (or with gdown) and download by hand",
    "dataverse": "Borealis (Dataverse) file needs a signed-in account",
    "rwth_publications": "RWTH Publications shows a JavaScript browser check to scripts; open the record in a browser",
    "deepblue": "Deep Blue Data refuses scripted access (HTTP 403); open the record in a browser (large files via Globus)",
    "matr_io": "data.matr.io lists files only through its web app, which needs an API key",
    "sciencedirect": "ScienceDirect article: the data is in the article's supplementary files, reachable in a browser",
}
ADAPTERS = {
    "mendeley": mendeley, "zenodo": zenodo, "figshare": figshare, "dryad": dryad, "osf": osf,
    "invenio_rdm": invenio_rdm, "dspace": dspace_depositonce, "github": github,
}


def list_source(url, platform):
    if platform in MANUAL:
        raise Manual(MANUAL[platform])
    return ADAPTERS.get(platform, html_links)(url)


# ---------------------------------------------------------------- driver

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="platforms or URLs to list")
    ap.add_argument("--refresh", action="store_true", help="list sources again even if already listed")
    args = ap.parse_args()
    db = os.environ.get("DATABASE_URL")
    if not db:
        sys.exit("set DATABASE_URL, e.g. postgresql://postgres:password@localhost:5432/battery_aging")
    schema = os.environ.get("DB_SCHEMA", "ingest")

    with psycopg.connect(db) as conn:
        if not conn.execute("SELECT to_regclass('catalog.dataset')").fetchone()[0]:
            sys.exit("catalog.dataset not found: load the workbook first with data-import/catalog/load_workbook.py")
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema)))
        conn.execute((HERE / "schema.sql").read_text(encoding="utf-8"))
        # Register every dataset URL from the catalog (new ones are added, existing ones keep their status).
        conn.execute("""
            INSERT INTO source (url, platform, dataset_ids)
            SELECT dataset_url, min(platform), array_agg(dataset_id ORDER BY dataset_id)
            FROM catalog.dataset WHERE dataset_url IS NOT NULL GROUP BY dataset_url
            ON CONFLICT (url) DO UPDATE SET platform = EXCLUDED.platform, dataset_ids = EXCLUDED.dataset_ids""")
        conn.commit()

        rows = conn.execute("SELECT source_id, url, platform, status FROM source ORDER BY source_id").fetchall()
        todo = [r for r in rows if (args.refresh or r[3] != "listed")
                and (not args.only or r[2] in args.only or r[1] in args.only)]
        print(f"{len(todo)} of {len(rows)} sources to list", flush=True)

        for i, (sid, url, platform, _) in enumerate(todo, 1):
            label = f"[{i}/{len(todo)}] {platform}: {url[:90]}"
            try:
                title, files = list_source(url, platform)
                status, detail = ("listed", None) if files else ("empty", "the repository returned no files")
            except Manual as e:
                title, files, status, detail = None, [], "manual", str(e)
            except Exception as e:  # keep going; the error is stored and the source retried next run
                title, files, status, detail = None, [], "failed", f"{type(e).__name__}: {e}"[:500]
            conn.execute("DELETE FROM remote_file WHERE source_id = %s", (sid,))
            with conn.cursor().copy("COPY remote_file (source_id, path, size_bytes, checksum, checksum_algo, "
                                    "download_url, extension, format_family) FROM STDIN") as cp:
                for x in files:
                    ext = extension(x["path"])
                    cp.write_row((sid, x["path"], x["size"], x["checksum"], x["algo"], x["url"], ext, family(ext)))
            sizes = [x["size"] for x in files if x["size"] is not None]
            conn.execute(
                """UPDATE source SET status = %s, detail = %s, title = %s, n_files = %s, total_bytes = %s,
                       unsized = %s, listed_at = now() WHERE source_id = %s""",
                (status, detail, title, len(files), sum(sizes) if sizes else None, len(files) - len(sizes), sid))
            conn.commit()
            size = f"{sum(sizes) / 1e9:.2f} GB" if sizes else "-"
            print(f"{label}\n    {status}: {len(files)} files, {size}" + (f" ({detail})" if detail else ""), flush=True)
            time.sleep(0.5)

        summary = conn.execute("""SELECT status, count(*), sum(n_files), sum(total_bytes) FROM source
                                  GROUP BY 1 ORDER BY 1""").fetchall()
    print("\nsummary:")
    for status, n, nf, tb in summary:
        print(f"  {status:8} {n:3} sources  {nf or 0:6} files  {float(tb or 0) / 1e9:8.2f} GB")
    print(f"done: tables are in schema {schema!r}")


if __name__ == "__main__":
    main()
