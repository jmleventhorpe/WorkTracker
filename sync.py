"""Sync the community VFX/animation job sheet into the Notion Job Tracker.

Stateless: every run reads existing Posting URL + Role pairs back from Notion
and skips anything already there, including rows marked Not Interested.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import requests

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
SOURCE_LABEL = "Community Sheet"

# Header names as they appear in the sheet. Matched case-insensitively on the
# first row that contains both "studio" and "job title".
COL_STUDIO = "studio"
COL_CITY = "city"
COL_REGION = "province/state/region"
COL_COUNTRY = "country"
COL_TITLE = "job title"
COL_ONSITE = "on-site/remote/hybrid"
COL_DATE = "date"
COL_SOURCE = "source/contact"
COL_SOFTWARE = "software"

REQUIRED_COLS = [COL_STUDIO, COL_TITLE, COL_SOURCE]

DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%Y-%m-%d", "%d/%m/%Y"]


class Fatal(Exception):
    pass


@dataclass
class Posting:
    role: str
    studio: str
    locations: list[str] = field(default_factory=list)
    date_issued: str | None = None
    url: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (normalise_url(self.url), self.role.strip().lower())

    @property
    def location(self) -> str:
        seen, out = set(), []
        for loc in self.locations:
            if loc and loc.lower() not in seen:
                seen.add(loc.lower())
                out.append(loc)
        return ", ".join(out)


def normalise_url(url: str) -> str:
    u = url.strip().lower().rstrip("/")
    for prefix in ("https://", "http://", "www."):
        if u.startswith(prefix):
            u = u[len(prefix):]
    return u


def load_config(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


# --------------------------------------------------------------------------
# Sheet
# --------------------------------------------------------------------------

def fetch_sheet(spreadsheet_id: str, gid: int) -> list[list[str]]:
    # gviz endpoint: still works when the owner has disabled downloads for
    # viewers, which blocks /export with a 401. headers=0 stops Google guessing
    # header rows and merging them; find_header() locates the real one.
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        f"/gviz/tq?tqx=out:csv&gid={gid}&headers=0"
    )
    resp = requests.get(url, timeout=60, allow_redirects=True)
    if resp.status_code != 200:
        raise Fatal(f"Sheet download failed: HTTP {resp.status_code}")
    if "text/csv" not in resp.headers.get("content-type", ""):
        raise Fatal(
            "Sheet did not return CSV. It is probably no longer public; "
            "switch to the Sheets API with an API key."
        )
    return list(csv.reader(io.StringIO(resp.text)))


def normalise_header(raw: str) -> str:
    """'Studio\\n(Featured listings in blue)' -> 'studio';
    ' On-Site/\\nRemote/Hybrid' -> 'on-site/remote/hybrid'."""
    text = re.sub(r"\([^)]*\)", " ", raw)
    text = re.sub(r"\s*/\s*", "/", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def find_date_column(rows: list[list[str]], skip: set[int]) -> int | None:
    """The sheet's date column has no header, so pick it by content."""
    sample = rows[:40]
    best, best_hits = None, 0
    width = max((len(r) for r in sample), default=0)
    for i in range(width):
        if i in skip:
            continue
        hits = sum(1 for r in sample if i < len(r) and parse_date(r[i]))
        if hits > best_hits:
            best, best_hits = i, hits
    return best if best_hits >= max(3, len(sample) // 2) else None


def find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]]:
    """Locate the header row and map column name -> index.

    Headers are matched by prefix after normalising, so the notes Google
    appends inside header cells don't break the match.
    """
    wanted = [COL_STUDIO, COL_CITY, COL_REGION, COL_COUNTRY, COL_TITLE,
              COL_ONSITE, COL_DATE, COL_SOURCE, COL_SOFTWARE]
    for idx, row in enumerate(rows[:15]):
        headers = [normalise_header(c) for c in row]
        if not (any(h.startswith(COL_STUDIO) for h in headers)
                and any(h.startswith(COL_TITLE) for h in headers)):
            continue
        mapping: dict[str, int] = {}
        for name in wanted:
            for i, h in enumerate(headers):
                if h and h.startswith(name) and i not in mapping.values():
                    mapping[name] = i
                    break
        missing = [c for c in REQUIRED_COLS if c not in mapping]
        if missing:
            raise Fatal(f"Header row is missing columns: {missing}")
        if COL_DATE not in mapping:
            date_col = find_date_column(rows[idx + 1:], set(mapping.values()))
            if date_col is not None:
                mapping[COL_DATE] = date_col
            else:
                print("  warning: no date column found; Date Issued left blank")
        return idx, mapping
    raise Fatal(
        "No header row found in the first 15 rows. The sheet layout has "
        "changed; update the column names in sync.py before running again."
    )


def cell(row: list[str], mapping: dict[str, int], name: str) -> str:
    i = mapping.get(name)
    if i is None or i >= len(row):
        return ""
    return row[i].strip()


def parse_date(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def keyword_hit(text: str, keywords: list[str]) -> bool:
    text = text.lower()
    return any(re.search(rf"\b{re.escape(k.lower())}\b", text) for k in keywords)


def location_ok(country: str, region: str, onsite: str, cfg: dict) -> bool:
    allowed = {a.lower() for a in cfg["locations"]["allowed"]}
    values = {country.strip().lower(), region.strip().lower()}
    if values & allowed:
        return True
    if cfg["filters"].get("keep_remote_without_country") and not any(values):
        return "remote" in onsite.lower()
    return False


def tidy_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://")):
        return raw
    if "@" in raw and " " not in raw:
        return f"mailto:{raw}"
    return ""


def collect_postings(rows: list[list[str]], cfg: dict) -> list[Posting]:
    header_idx, mapping = find_header(rows)
    merged: dict[tuple[str, str], Posting] = {}
    skipped_no_url = 0
    skipped_old = 0
    max_age = int(cfg["filters"].get("max_age_days", 0) or 0)
    # ISO date strings compare correctly as strings.
    cutoff = (date.today() - timedelta(days=max_age)).isoformat() if max_age else None

    for row in rows[header_idx + 1:]:
        if not any(c.strip() for c in row):
            continue
        title = cell(row, mapping, COL_TITLE)
        studio = cell(row, mapping, COL_STUDIO)
        if not title or not studio:
            continue

        country = cell(row, mapping, COL_COUNTRY)
        region = cell(row, mapping, COL_REGION)
        onsite = cell(row, mapping, COL_ONSITE)
        if not location_ok(country, region, onsite, cfg):
            continue

        exclusions = cfg["filters"].get("title_exclusions", [])
        if exclusions and keyword_hit(title, exclusions):
            continue

        software = cell(row, mapping, COL_SOFTWARE)
        if not keyword_hit(title, cfg["filters"]["title_keywords"]):
            sw_keys = cfg["filters"].get("software_keywords", [])
            if not (sw_keys and keyword_hit(software, sw_keys)):
                continue

        url = tidy_url(cell(row, mapping, COL_SOURCE))
        if not url:
            skipped_no_url += 1
            continue

        city = cell(row, mapping, COL_CITY)
        loc = ", ".join(p for p in (city, country or region) if p)
        date_issued = parse_date(cell(row, mapping, COL_DATE))
        if cutoff and date_issued and date_issued < cutoff:
            skipped_old += 1
            continue
        posting = Posting(
            role=title,
            studio=studio,
            locations=[loc] if loc else [],
            date_issued=date_issued,
            url=url,
        )
        existing = merged.get(posting.key)
        if existing:
            existing.locations.extend(posting.locations)
            if not existing.date_issued:
                existing.date_issued = posting.date_issued
        else:
            merged[posting.key] = posting

    if skipped_no_url:
        print(f"  skipped {skipped_no_url} matching rows with no usable link")
    if skipped_old:
        print(f"  skipped {skipped_old} matching rows older than {max_age} days")
    return list(merged.values())


# --------------------------------------------------------------------------
# Notion
# --------------------------------------------------------------------------

class Notion:
    def __init__(self, token: str, database_id: str):
        self.database_id = database_id
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def existing_keys(self) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        payload: dict = {"page_size": 100}
        while True:
            resp = self.session.post(
                f"{NOTION_API}/databases/{self.database_id}/query",
                json=payload, timeout=60,
            )
            if resp.status_code == 404:
                raise Fatal(
                    "Notion returned 404. The database is probably not shared "
                    "with the integration (Notion database -> ... -> "
                    "Connections)."
                )
            resp.raise_for_status()
            data = resp.json()
            for page in data["results"]:
                props = page["properties"]
                url = (props.get("Posting URL") or {}).get("url") or ""
                title_prop = (props.get("Role") or {}).get("title") or []
                role = "".join(t.get("plain_text", "") for t in title_prop)
                keys.add((normalise_url(url), role.strip().lower()))
            if not data.get("has_more"):
                return keys
            payload["start_cursor"] = data["next_cursor"]

    def create(self, posting: Posting) -> None:
        props: dict = {
            "Role": {"title": [{"text": {"content": posting.role[:2000]}}]},
            "Studio": {
                "rich_text": [{"text": {"content": posting.studio[:2000]}}]
            },
            "Source": {"select": {"name": SOURCE_LABEL}},
        }
        if posting.location:
            props["Location"] = {
                "rich_text": [{"text": {"content": posting.location[:2000]}}]
            }
        if posting.url:
            props["Posting URL"] = {"url": posting.url[:2000]}
        if posting.date_issued:
            props["Date Issued"] = {"date": {"start": posting.date_issued}}

        resp = self.session.post(
            f"{NOTION_API}/pages",
            json={
                "parent": {"database_id": self.database_id},
                "properties": props,
            },
            timeout=60,
        )
        if resp.status_code >= 400:
            raise Fatal(f"Notion create failed ({resp.status_code}): {resp.text}")


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would be added without writing to Notion",
    )
    args = parser.parse_args()

    token = os.environ.get("NOTION_TOKEN")
    database_id = os.environ.get("NOTION_DATABASE_ID")
    if not args.dry_run and not (token and database_id):
        raise Fatal("NOTION_TOKEN and NOTION_DATABASE_ID must be set")

    cfg = load_config(args.config)
    print("Fetching sheet...")
    rows = fetch_sheet(cfg["sheet"]["spreadsheet_id"], cfg["sheet"]["gid"])
    postings = collect_postings(rows, cfg)
    print(f"  {len(postings)} postings match the filters")

    if args.dry_run:
        for p in sorted(postings, key=lambda p: p.studio.lower()):
            print(
                f"  - {p.studio} | {p.role} | {p.location} | "
                f"{p.date_issued or 'NO DATE'} | {p.url}"
            )
        return 0

    notion = Notion(token, database_id)
    existing = notion.existing_keys()
    print(f"  {len(existing)} rows already in Notion")

    added = 0
    for posting in postings:
        if posting.key in existing:
            continue
        notion.create(posting)
        added += 1
        print(f"  + {posting.studio} | {posting.role}")
        time.sleep(0.35)  # stay under Notion's ~3 requests/second limit

    print(f"Done. {added} added, {len(postings) - added} already present.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fatal as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)