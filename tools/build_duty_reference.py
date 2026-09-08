"""Build the full duty-reference SQLite database from customs open datasets.

Feature context
---------------
The packaged ``duty_rates_20260101.json`` snapshot covers a handful of demo
HS codes only.  This tool ingests the real Korea Customs Service open data
into one SQLite file so the Backend can serve the duty ladder for *every*
HSK 10-digit code:

1. 관세청_품목번호별_관세율표_YYYYMMDD.xlsx  — Korea import tariff rows
   (품목번호 × 관세율구분코드 × 세율 × 적용기간), ~380k rows.
2. 산업통상부_무역구제_자유무역협정_목록자료_YYYYMMDD.csv — FTA agreement list.
3. 관세청_국가별_관세율표_YYYYMMDD.zip — foreign countries' own tariff tables
   (what each partner country charges on imports; export-side reference).

The output DB is a *reference artefact*, never committed: point the Backend
at it with ``CUSTOMS_BACKEND_DUTY_REFERENCE_DB=<path>``.  Without that env
the Backend keeps using the packaged JSON snapshot, so tests and dev-fixture
behaviour are unchanged.

Run with any Python that has ``openpyxl`` (e.g. the customs-data-platform
venv)::

    vpython tools/build_duty_reference.py \
        --item-rates  ~/Downloads/관세청_품목번호별_관세율표_20260211.xlsx \
        --fta-list    ~/Downloads/산업통상부_무역구제_자유무역협정_목록자료_20250825.csv \
        --country-zip ~/Downloads/관세청_국가별_관세율표_20251231.zip \
        --out         ../_local/data/duty_reference.sqlite3
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import openpyxl
except ImportError:  # pragma: no cover - guidance for the operator
    raise SystemExit(
        "openpyxl is required. Run this tool with the customs-data-platform venv, e.g.\n"
        "  ..\\customs-data-platform\\.venv\\Scripts\\vpython.exe tools\\build_duty_reference.py"
    )

_DATE8 = re.compile(r"(20\d{6})")
_SHEET_MD = re.compile(r"^(\d{1,2})\.(\d{1,2})$")
_HS10 = re.compile(r"^\d{10}$")

# Korean country-table file names → ISO 3166-1 alpha-2 (curated).
_COUNTRY_NAME_ISO2 = {
    "남아프리카공화국": "ZA", "노르웨이": "NO", "뉴질랜드": "NZ", "니카라과": "NI",
    "대만": "TW", "라오스": "LA", "러시아": "RU", "말레이시아": "MY",
    "멕시코": "MX", "몽골": "MN", "미국": "US", "미얀마": "MM",
    "방글라데시": "BD", "베트남": "VN", "브라질": "BR", "브루나이": "BN",
    "사우디아라비아": "SA", "스위스": "CH", "싱가포르": "SG",
    "아랍에메리트": "AE", "아랍에미리트": "AE", "아이슬란드": "IS",
    "엘살바도르": "SV", "온두라스": "HN", "우즈베키스탄": "UZ",
    "이스라엘": "IL", "인도": "IN", "인도네시아": "ID", "일본": "JP",
    "중국": "CN", "칠레": "CL", "캄보디아": "KH", "캐나다": "CA",
    "코스타리카": "CR", "콜롬비아": "CO", "태국": "TH", "튀르키예": "TR",
    "터키": "TR", "파나마": "PA", "페루": "PE", "필리핀": "PH",
    "호주": "AU", "영국": "GB", "EU": "EU",
}


def _fixed_zip_name(raw: str) -> str:
    """Zip entries are CP949 names mangled through CP437; undo when possible."""
    try:
        return raw.encode("cp437").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _derive_as_of(item_rates: Path, sheet_name: str) -> str:
    match = _DATE8.search(item_rates.name)
    year = match.group(1)[:4] if match else "2026"
    md = _SHEET_MD.match(sheet_name)
    if md:
        return f"{year}-{int(md.group(1)):02d}-{int(md.group(2)):02d}"
    if match:
        raw = match.group(1)
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return f"{year}-01-01"


def _latest_sheet(names: list[str]) -> str:
    """Pick the sheet representing the latest 적용시점 (names like '1.1', '2.12')."""

    def key(name: str) -> tuple[int, int]:
        md = _SHEET_MD.match(name.strip())
        return (int(md.group(1)), int(md.group(2))) if md else (0, 0)

    return max(names, key=key)


def ingest_item_rates(conn: sqlite3.Connection, path: Path) -> str:
    wb = openpyxl.load_workbook(path, read_only=True)
    sheet = _latest_sheet(wb.sheetnames)
    as_of = _derive_as_of(path, sheet)
    ws = wb[sheet]
    print(f"[1/3] {path.name} · sheet {sheet} · as_of {as_of}")

    rows = ws.iter_rows(min_row=2, values_only=True)
    inserted = skipped = 0
    batch: list[tuple] = []
    as_of_compact = as_of.replace("-", "")
    for row in rows:
        hs = str(row[0] or "").strip()
        kind = str(row[1] or "").strip()
        if not _HS10.match(hs) or not kind:
            skipped += 1
            continue
        applied_from = str(row[7] or "").strip()
        applied_to = str(row[8] or "").strip()
        if applied_from and applied_from > as_of_compact:
            skipped += 1
            continue
        if applied_to and applied_to < as_of_compact:
            skipped += 1
            continue
        rate = _as_float(row[2])
        if rate is None:
            skipped += 1
            continue
        batch.append(
            (
                hs,
                kind,
                rate,
                _as_float(row[3]),
                _as_float(row[4]),
                str(row[5] or "").strip(),
                str(row[6] or "").strip(),
                applied_from,
                applied_to,
            )
        )
        inserted += 1
        if len(batch) >= 20_000:
            conn.executemany("INSERT INTO kr_rates VALUES (?,?,?,?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO kr_rates VALUES (?,?,?,?,?,?,?,?,?)", batch)
    conn.execute("CREATE INDEX idx_kr_rates_hs ON kr_rates(hs_code)")
    print(f"      {inserted:,} rows kept (valid on {as_of}), {skipped:,} skipped")
    return as_of


def ingest_fta_list(conn: sqlite3.Connection, path: Path) -> None:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SystemExit(f"cannot decode {path}")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    count = 0
    for row in reader:
        if len(row) < 3:
            continue
        conn.execute(
            "INSERT INTO fta_agreements VALUES (?,?,?)",
            (row[0].strip(), re.sub(r"\s+", " ", row[1]).strip(), row[2].strip()),
        )
        count += 1
    print(f"[2/3] {path.name} · {count} agreements (header: {header})")


def _country_from_filename(name: str) -> tuple[str, int]:
    """('베트남', 0) or ('EU', 202507) from '2025년07월_EU_관세율표.xlsx'."""
    stem = Path(name).stem
    parts = stem.split("_")
    month = 0
    md = re.match(r"(\d{4})년(\d{2})월?", parts[0])
    if md:
        month = int(md.group(1)) * 100 + int(md.group(2))
    country = parts[1] if len(parts) > 1 else stem
    return country.strip(), month


def ingest_country_tables(conn: sqlite3.Connection, path: Path) -> None:
    zf = zipfile.ZipFile(path)
    # Keep only the newest table per country when several revisions exist.
    latest: dict[str, tuple[int, zipfile.ZipInfo, str]] = {}
    for info in zf.infolist():
        name = _fixed_zip_name(info.filename)
        if not name.lower().endswith(".xlsx"):
            continue
        country, month = _country_from_filename(name)
        current = latest.get(country)
        if current is None or month >= current[0]:
            latest[country] = (month, info, name)

    total = 0
    for country, (_, info, name) in sorted(latest.items()):
        iso2 = _COUNTRY_NAME_ISO2.get(country, "")
        wb = openpyxl.load_workbook(io.BytesIO(zf.read(info)), read_only=True)
        file_rows = 0
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            header: list[str] | None = None
            rate_start = None
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c).strip() for c in row]
                if header is None:
                    if "세번" in cells:
                        header = cells
                        rate_start = cells.index("세번") + 1
                        for label in ("단위", "한글품명", "영문품명"):
                            if label in cells:
                                rate_start = max(rate_start, cells.index(label) + 1)
                    continue
                hs_idx = header.index("세번")
                hs = cells[hs_idx] if hs_idx < len(cells) else ""
                if not hs or not hs[0].isdigit():
                    continue
                rates = {
                    header[i]: cells[i]
                    for i in range(rate_start, min(len(header), len(cells)))
                    if header[i] and cells[i] not in ("", " ")
                }
                if not rates:
                    continue

                def col(label: str) -> str:
                    return cells[header.index(label)] if label in header else ""

                conn.execute(
                    "INSERT INTO foreign_rates VALUES (?,?,?,?,?,?,?)",
                    (
                        iso2,
                        country,
                        hs,
                        col("영문품명"),
                        col("한글품명"),
                        col("단위"),
                        json.dumps(rates, ensure_ascii=False),
                    ),
                )
                file_rows += 1
        total += file_rows
        print(f"[3/3] {name} → {country} ({iso2 or '??'}) · {file_rows:,} rows")
    conn.execute("CREATE INDEX idx_foreign_hs ON foreign_rates(country_iso2, hs_code)")
    print(f"      {total:,} foreign tariff rows across {len(latest)} countries")


def main() -> None:
    downloads = Path.home() / "Downloads"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item-rates", type=Path, default=None)
    parser.add_argument("--fta-list", type=Path, default=None)
    parser.add_argument("--country-zip", type=Path, default=None)
    parser.add_argument("--skip-country-zip", action="store_true")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "_local" / "data" / "duty_reference.sqlite3",
    )
    args = parser.parse_args()

    def pick(explicit: Path | None, pattern: str, label: str) -> Path:
        if explicit is not None:
            if not explicit.exists():
                raise SystemExit(f"{label} not found: {explicit}")
            return explicit
        candidates = sorted(downloads.glob(pattern))
        if not candidates:
            raise SystemExit(f"{label}: no file matching {pattern} in {downloads}")
        return candidates[-1]

    item_rates = pick(args.item_rates, "관세청_품목번호별_관세율표_*.xlsx", "item rates xlsx")
    fta_list = pick(args.fta_list, "*자유무역협정_목록자료_*.csv", "FTA list csv")
    country_zip = None
    if not args.skip_country_zip:
        country_zip = pick(args.country_zip, "관세청_국가별_관세율표_*.zip", "country tables zip")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".building")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE kr_rates (
            hs_code TEXT NOT NULL, kind TEXT NOT NULL, rate_pct REAL NOT NULL,
            aux1 REAL, aux2 REAL, country_flag TEXT, usage_flag TEXT,
            applied_from TEXT, applied_to TEXT
        );
        CREATE TABLE fta_agreements (agreement_id TEXT, name_ko TEXT, in_use TEXT);
        CREATE TABLE foreign_rates (
            country_iso2 TEXT, country_name_ko TEXT, hs_code TEXT,
            name_en TEXT, name_ko TEXT, unit TEXT, rates_json TEXT
        );
        """
    )

    as_of = ingest_item_rates(conn, item_rates)
    ingest_fta_list(conn, fta_list)
    if country_zip is not None:
        ingest_country_tables(conn, country_zip)

    meta = {
        "as_of": as_of,
        "source": "관세청 품목번호별 관세율표 · 산업통상부 FTA 목록 · 관세청 국가별 관세율표 (공공데이터포털)",
        "item_rates_file": item_rates.name,
        "fta_list_file": fta_list.name,
        "country_zip_file": country_zip.name if country_zip else "",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    conn.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    if args.out.exists():
        args.out.unlink()
    tmp.rename(args.out)
    size_mb = args.out.stat().st_size / 1_048_576
    print(f"done → {args.out} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
