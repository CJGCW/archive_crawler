#!/usr/bin/env python3
"""
archive_crawler.py — search archive.org for items and download their files.

Uses archive.org's public APIs:
  - Advanced Search API (https://archive.org/advancedsearch.php) to find identifiers
  - Metadata API (https://archive.org/metadata/{identifier}) to list an item's files
  - Download endpoint (https://archive.org/download/{identifier}/{filename})

Examples:
  # Search for identifiers and save them to a file
  python archive_crawler.py search "my little pony" --mediatype movies --rows 50 -o ids.txt

  # Download every file for one or more known identifiers
  python archive_crawler.py download MyLittlePonyFull

  # Download from a list of identifiers, only .mp4/.srt files
  python archive_crawler.py download --list ids.txt --ext mp4,srt

  # Search and download in one shot, filtering by extension
  python archive_crawler.py run "my little pony" --mediatype movies --rows 20 --ext mp4
"""

import argparse
import fnmatch
import html
import json
import re
import sys
import textwrap
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import requests

# Windows consoles often default to a legacy codepage (e.g. cp1252) that
# can't encode titles containing non-Latin characters (e.g. Japanese).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"

USER_AGENT = "archive-crawler/1.0 (+https://archive.org)"

# Archive/container formats are always worth surfacing when filtering by
# media type, since the media you're after may be bundled inside one.
ARCHIVE_EXTENSIONS = {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2"}


def search_items(query, rows=50, mediatype=None, page=1, sort_by_downloads=False):
    """Query the advanced search API and return a list of dicts with
    identifier, title, mediatype and downloads for each match.

    The query is scoped to the title field as a phrase (title:("...")) rather
    than an unscoped bag-of-words OR search — an unscoped query lets unrelated
    but heavily-downloaded items (e.g. a different, more popular show) outrank
    the actual title match. Relevance ranking (the API default) is kept unless
    sort_by_downloads is requested, since relevance is what keeps results on-topic.
    """
    q = f'title:("{query}")'
    if mediatype:
        q += f" AND mediatype:{mediatype}"
    params = [
        ("q", q),
        ("fl[]", "identifier"),
        ("fl[]", "title"),
        ("fl[]", "mediatype"),
        ("fl[]", "downloads"),
        ("rows", rows),
        ("page", page),
        ("output", "json"),
    ]
    if sort_by_downloads:
        params.append(("sort[]", "downloads desc"))
    resp = requests.get(SEARCH_URL, params=params, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    docs = data.get("response", {}).get("docs", [])
    return [doc for doc in docs if "identifier" in doc]


def search_identifiers(query, rows=50, mediatype=None, page=1, sort_by_downloads=False):
    """Convenience wrapper returning just the identifiers."""
    return [doc["identifier"] for doc in search_items(query, rows=rows, mediatype=mediatype, page=page, sort_by_downloads=sort_by_downloads)]


def get_item_metadata(identifier):
    """Return the full metadata API response for an archive.org item."""
    url = METADATA_URL.format(identifier=identifier)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    if not data.get("metadata") and not data.get("files"):
        raise ValueError(f"No such identifier '{identifier}' (does it exist?)")
    return data


def get_item_files(identifier):
    """Return the list of file dicts for an archive.org item."""
    data = get_item_metadata(identifier)
    if not data.get("files"):
        raise ValueError(f"No files found for identifier '{identifier}' (does it exist?)")
    return data["files"]


class HTTPRangeFile:
    """A read-only, seekable file-like object backed by HTTP Range requests.

    Lets zipfile read just the central directory at the end of a remote zip
    (a handful of small range reads) instead of downloading the whole file.
    Raises RuntimeError if the server doesn't support Range requests.
    """

    def __init__(self, url, session=None):
        self.url = url
        self.session = session or requests
        self._pos = 0
        head = self.session.head(url, headers={"User-Agent": USER_AGENT}, allow_redirects=True)
        head.raise_for_status()
        if "Accept-Ranges" not in head.headers or head.headers.get("Accept-Ranges") == "none":
            probe = self.session.get(url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
            if probe.status_code != 206:
                raise RuntimeError("server does not support HTTP Range requests")
        length = head.headers.get("Content-Length")
        if length is None:
            raise RuntimeError("server did not report Content-Length")
        self._size = int(length)

    def seek(self, offset, whence=0):
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = self._size + offset
        return self._pos

    def tell(self):
        return self._pos

    def read(self, size=-1):
        if size is None or size < 0:
            end = self._size - 1
        else:
            end = min(self._pos + size, self._size) - 1
        if self._pos > end:
            return b""
        headers = {"User-Agent": USER_AGENT, "Range": f"bytes={self._pos}-{end}"}
        resp = self.session.get(self.url, headers=headers)
        resp.raise_for_status()
        data = resp.content
        self._pos += len(data)
        return data

    def seekable(self):
        return True


def peek_zip_contents(url):
    """Return a list of (name, size) for entries inside a remote zip file,
    without downloading it — only its central directory is fetched."""
    remote = HTTPRangeFile(url)
    with zipfile.ZipFile(remote) as zf:
        return [(info.filename, info.file_size) for info in zf.infolist()]


def dedupe_ia_variants(files):
    """archive.org often stores two copies of a video: the original upload
    and an internal re-encode named identically but with an extra '.ia'
    before the extension (e.g. 'Foo.mp4' and 'Foo.ia.mp4'). Keep only the
    larger of each such pair so we don't download the same content twice."""
    best = {}
    order = []
    for f in files:
        canonical = re.sub(r"\.ia(\.[^./\\]+)$", r"\1", f["name"])
        size = int(f.get("size") or 0)
        if canonical not in best:
            best[canonical] = f
            order.append(canonical)
        elif size > int(best[canonical].get("size") or 0):
            best[canonical] = f
    return [best[key] for key in order]


def matches_filters(filename, extensions, patterns):
    if extensions:
        ext = Path(filename).suffix.lstrip(".").lower()
        if ext not in extensions:
            return False
    if patterns:
        if not any(fnmatch.fnmatch(filename.lower(), p.lower()) for p in patterns):
            return False
    return True


def download_file(identifier, filename, dest_dir, expected_size=None):
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if dest_path.exists() and expected_size is not None:
        if dest_path.stat().st_size == int(expected_size):
            print(f"  [skip] {filename} (already downloaded)")
            return

    url = DOWNLOAD_URL.format(identifier=identifier, filename=quote(filename))
    print(f"  [get]  {filename}")
    with requests.get(url, stream=True, headers={"User-Agent": USER_AGENT}) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def download_identifier(identifier, output_dir, extensions=None, patterns=None, dry_run=False):
    print(f"[{identifier}] fetching file list...")
    try:
        files = get_item_files(identifier)
    except (requests.RequestException, ValueError) as e:
        print(f"  [error] {e}", file=sys.stderr)
        return

    selected = [f for f in files if matches_filters(f["name"], extensions, patterns)]
    selected = dedupe_ia_variants(selected)
    if not selected:
        print(f"  [skip] no files matched filters ({len(files)} total files on item)")
        return

    dest_dir = Path(output_dir) / identifier
    for f in selected:
        if dry_run:
            print(f"  [dry-run] would download {f['name']} ({f.get('size', '?')} bytes)")
            continue
        try:
            download_file(identifier, f["name"], dest_dir, f.get("size"))
        except requests.RequestException as e:
            print(f"  [error] failed to download {f['name']}: {e}", file=sys.stderr)


def load_identifiers_from_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def parse_ext_list(ext_arg):
    if not ext_arg:
        return None
    return {e.strip().lstrip(".").lower() for e in ext_arg.split(",") if e.strip()}


def parse_pattern_list(pattern_arg):
    if not pattern_arg:
        return None
    return [p.strip() for p in pattern_arg.split(",") if p.strip()]


def format_result_line(doc):
    ident = doc.get("identifier", "?")
    title = doc.get("title", "(no title)")
    mediatype = doc.get("mediatype", "?")
    downloads = doc.get("downloads", 0)
    return f"{ident:<40} | {title}  [{mediatype}, {downloads} downloads]"


def clean_html_text(value):
    """archive.org descriptions are often HTML fragments (possibly a list of
    them). Strip tags and unescape entities so they read as plain text."""
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value)
    text = re.sub(r"<br\s*/?>", "\n", str(value))
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def cmd_info(args):
    """Show an item's description/creator/subjects and a file-type summary
    so its actual contents can be sanity-checked without downloading it."""
    identifier = args.identifier
    try:
        data = get_item_metadata(identifier)
    except (requests.RequestException, ValueError) as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)

    meta = data.get("metadata", {})
    files = data.get("files", [])

    title = meta.get("title", "(no title)")
    print(f"{title}")
    print(f"  identifier : {identifier}")
    print(f"  url        : https://archive.org/details/{identifier}")
    if meta.get("mediatype"):
        print(f"  mediatype  : {meta['mediatype']}")
    if meta.get("creator"):
        print(f"  creator    : {', '.join(as_list(meta['creator']))}")
    if meta.get("date") or meta.get("year"):
        print(f"  date       : {meta.get('date') or meta.get('year')}")
    if meta.get("collection"):
        print(f"  collection : {', '.join(as_list(meta['collection']))}")
    if meta.get("subject"):
        print(f"  subjects   : {', '.join(as_list(meta['subject']))}")
    if meta.get("language"):
        print(f"  language   : {meta['language']}")

    description = clean_html_text(meta.get("description"))
    if description:
        print("\n  description:")
        for line in description.splitlines():
            wrapped = textwrap.wrap(line, width=100) or [""]
            for w in wrapped:
                print(f"    {w}")

    if files:
        ext_counts = Counter(Path(f["name"]).suffix.lstrip(".").lower() or "(none)" for f in files)
        summary = ", ".join(f"{ext}: {count}" for ext, count in sorted(ext_counts.items(), key=lambda kv: -kv[1]))
        print(f"\n  files      : {len(files)} total ({summary})")

    extensions = parse_ext_list(args.ext)
    if extensions:
        keep_exts = extensions | ARCHIVE_EXTENSIONS
        matching = [f for f in files if Path(f["name"]).suffix.lstrip(".").lower() in extensions]
        print(f"\n  matching '{','.join(sorted(extensions))}' files ({len(matching)}):")
        for f in matching:
            print(f"    {f['name']}  ({f.get('size', '?')} bytes)")
        files = [f for f in files if Path(f["name"]).suffix.lstrip(".").lower() in keep_exts]

    if not args.no_peek_zip:
        zip_files = [f for f in files if f["name"].lower().endswith(".zip")]
        for f in zip_files:
            zip_url = DOWNLOAD_URL.format(identifier=identifier, filename=quote(f["name"]))
            print(f"\n  contents of {f['name']} (peeked via HTTP range request, not downloaded):")
            try:
                entries = peek_zip_contents(zip_url)
            except (requests.RequestException, RuntimeError, zipfile.BadZipFile) as e:
                print(f"    [could not peek inside zip: {e}]")
                continue
            if extensions:
                entries = [(name, size) for name, size in entries if Path(name).suffix.lstrip(".").lower() in extensions]
                if not entries:
                    print(f"    [no entries matching '{','.join(sorted(extensions))}']")
                    continue
            shown = entries[:50]
            for name, size in shown:
                print(f"    {name}  ({size} bytes)")
            if len(entries) > len(shown):
                print(f"    ... and {len(entries) - len(shown)} more entries")


def cmd_search(args):
    items = search_items(args.query, rows=args.rows, mediatype=args.mediatype, sort_by_downloads=args.sort_by_downloads)
    for doc in items:
        print(format_result_line(doc))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("\n".join(doc["identifier"] for doc in items) + "\n")
        print(f"\nSaved {len(items)} identifiers to {args.output}", file=sys.stderr)


def cmd_download(args):
    identifiers = list(args.identifiers)
    if args.list:
        identifiers.extend(load_identifiers_from_file(args.list))
    if not identifiers:
        print("No identifiers given (pass names as args or use --list).", file=sys.stderr)
        sys.exit(1)

    extensions = parse_ext_list(args.ext)
    patterns = parse_pattern_list(args.pattern)

    for ident in identifiers:
        download_identifier(ident, args.output_dir, extensions, patterns, args.dry_run)


def cmd_run(args):
    items = search_items(args.query, rows=args.rows, mediatype=args.mediatype, sort_by_downloads=args.sort_by_downloads)
    if args.limit:
        items = items[: args.limit]

    print(f"Found {len(items)} item(s) for query '{args.query}':", file=sys.stderr)
    for doc in items:
        print("  " + format_result_line(doc), file=sys.stderr)

    if args.list_only:
        return

    extensions = parse_ext_list(args.ext)
    patterns = parse_pattern_list(args.pattern)

    for doc in items:
        download_identifier(doc["identifier"], args.output_dir, extensions, patterns, args.dry_run)


def build_parser():
    parser = argparse.ArgumentParser(description="Search and download files from archive.org")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Search archive.org for item identifiers")
    p_search.add_argument("query", help="Search query, e.g. 'my little pony'")
    p_search.add_argument("--mediatype", help="Filter by mediatype, e.g. movies, texts, audio")
    p_search.add_argument("--rows", type=int, default=50, help="Max results to fetch (default 50)")
    p_search.add_argument("--sort-by-downloads", action="store_true", help="Sort by popularity instead of relevance")
    p_search.add_argument("-o", "--output", help="Write identifiers to this file, one per line")
    p_search.set_defaults(func=cmd_search)

    p_info = sub.add_parser("info", help="Show description/creator/subjects for one identifier, to verify contents before downloading")
    p_info.add_argument("identifier", help="Archive.org identifier, e.g. MyLittlePonyFull")
    p_info.add_argument("--ext", help="Comma-separated file extensions to focus on, e.g. mp4,srt (archive types like zip/rar/tar are always kept, since the media may be bundled inside)")
    p_info.add_argument("--no-peek-zip", action="store_true", help="Don't peek inside zip files on the item via HTTP range requests")
    p_info.set_defaults(func=cmd_info)

    p_download = sub.add_parser("download", help="Download files for one or more identifiers")
    p_download.add_argument("identifiers", nargs="*", help="Archive.org identifier(s), e.g. MyLittlePonyFull")
    p_download.add_argument("--list", help="Path to a text file of identifiers, one per line")
    p_download.add_argument("--ext", help="Comma-separated list of file extensions to keep, e.g. mp4,srt")
    p_download.add_argument("--pattern", help="Comma-separated glob patterns to keep, e.g. *.mp4,*subtitle*")
    p_download.add_argument("-d", "--output-dir", default="./downloads", help="Directory to save files into")
    p_download.add_argument("--dry-run", action="store_true", help="List what would be downloaded without downloading")
    p_download.set_defaults(func=cmd_download)

    p_run = sub.add_parser("run", help="Search and download in one step")
    p_run.add_argument("query", help="Search query, e.g. 'my little pony'")
    p_run.add_argument("--mediatype", help="Filter by mediatype, e.g. movies, texts, audio")
    p_run.add_argument("--rows", type=int, default=50, help="Max search results to fetch (default 50)")
    p_run.add_argument("--sort-by-downloads", action="store_true", help="Sort by popularity instead of relevance")
    p_run.add_argument("--limit", type=int, help="Only download the top N results (after ranking/sorting)")
    p_run.add_argument("--ext", help="Comma-separated list of file extensions to keep, e.g. mp4,srt")
    p_run.add_argument("--pattern", help="Comma-separated glob patterns to keep, e.g. *.mp4,*subtitle*")
    p_run.add_argument("-d", "--output-dir", default="./downloads", help="Directory to save files into")
    p_run.add_argument("--dry-run", action="store_true", help="List what would be downloaded without downloading")
    p_run.add_argument("--list-only", action="store_true", help="Just show matching items, don't download")
    p_run.set_defaults(func=cmd_run)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
