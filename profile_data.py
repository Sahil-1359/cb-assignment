"""Profile the three source CSVs without changing their data."""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
FILES = [
    BASE_DIR / "files" / "source1_naukri_applicants.csv",
    BASE_DIR / "files" / "source2_gig_workers.csv",
    BASE_DIR / "files" / "source3_cbnexus_contacts.csv",
]
REPORT_PATH = BASE_DIR / "profile_report.txt"
CUTOFF_DATE = date(2026, 8, 12)


def display(value: object) -> str:
    """Make whitespace and empty strings visible in a plain-text report."""
    text = str(value)
    return "<EMPTY>" if text == "" else repr(text)


def normalized(value: object) -> str:
    return str(value).strip().casefold()


def column_kind(column: str) -> str:
    name = column.casefold()
    if "email" in name or name in {"email_id", "emailid"}:
        return "email"
    if "phone" in name or "mobile" in name or name in {"contact", "contact number"}:
        return "phone"
    if "skill" in name or "tag" in name:
        return "skills"
    if "date" in name or "dob" in name or "joined" in name:
        return "date"
    if any(word in name for word in ("ctc", "rate", "experience", "salary", "projects completed")):
        return "numeric"
    if any(word in name for word in ("city", "location", "status", "name")):
        return "text"
    return "other"


def usable_email(value: object) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(value).strip()))


def phone_digits(value: object) -> str:
    return re.sub(r"\D", "", str(value).strip())


def usable_phone(value: object) -> bool:
    digits = phone_digits(value)
    return len(digits) >= 10 and len(digits) <= 15


def phone_shape(value: str) -> str:
    raw = value.strip()
    digits = phone_digits(raw)
    if raw.startswith("+91"):
        prefix = "leading +91"
    elif raw.startswith("91"):
        prefix = "leading 91"
    elif raw.startswith("0"):
        prefix = "leading 0"
    else:
        prefix = "no recognized prefix"
    hyphens = "with hyphens" if "-" in raw else "without hyphens"
    return f"{prefix}; {hyphens}; digit length {len(digits)}"


def date_pattern(value: str) -> tuple[str, date | None, bool]:
    raw = value.strip()
    patterns = [
        (r"^(\d{4})-(\d{1,2})-(\d{1,2})$", "%Y-%m-%d", "YYYY-MM-DD"),
        (r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$", None, "DD/MM/YYYY or MM/DD/YYYY"),
        (r"^(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})$", None, "D Mon YYYY"),
        (r"^(\d{1,2})[-/]([A-Za-z]{3,9})[-/](\d{4})$", None, "D-Mon-YYYY"),
    ]
    for regex, fmt, label in patterns:
        match = re.fullmatch(regex, raw)
        if not match:
            continue
        ambiguous = False
        try:
            if fmt:
                parsed = datetime.strptime(raw, fmt).date()
            elif label.startswith("DD/"):
                first, second, year = map(int, match.groups())
                ambiguous = first <= 12 and second <= 12 and first != second
                parsed = date(year, second, first)
            else:
                day, month, year = match.groups()
                parsed = datetime.strptime(f"{day} {month} {year}", "%d %B %Y").date()
        except ValueError:
            try:
                if not fmt and label.startswith("DD/"):
                    first, second, year = map(int, match.groups())
                    parsed = date(year, first, second)
                else:
                    parsed = None
            except ValueError:
                parsed = None
        return label, parsed, ambiguous
    return "unrecognized date format", None, False


def numeric_value(value: str) -> float | None:
    cleaned = value.strip().casefold().replace(",", "")
    cleaned = re.sub(r"(?:/hr|/hour|per hour|/month|per month|k/month)$", "", cleaned).strip()
    cleaned = re.sub(r"[^0-9.+-]", "", cleaned)
    if not cleaned or cleaned in {"+", "-", "."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def raw_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def row_text(headers: list[str], row: Iterable[str]) -> str:
    values = list(row)
    pairs = [f"{headers[i] if i < len(headers) else '<EXTRA>'}={display(value)}"
             for i, value in enumerate(values)]
    return "{" + ", ".join(pairs) + "}"


def report_file(path: Path, lines: list[str]) -> tuple[pd.DataFrame, list[str], list[list[str]]]:
    rows = raw_rows(path)
    headers = rows[0] if rows else []
    # Read with strings and disabled NA inference so values remain observable exactly as supplied.
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    lines.extend(["", "=" * 78, f"FILE: {path.name}", "=" * 78])
    lines.append(f"Rows read by pandas: {len(frame)}")
    lines.append(f"Column names: {headers}")

    lines.append("\nPer-column value profile:")
    for column in frame.columns:
        series = frame[column].astype(str)
        counts = Counter(series)
        empty_count = sum(not value.strip() for value in series)
        lines.append(f"  {column}: empty/whitespace-only={empty_count}; distinct={len(counts)}")
        lines.append("    top 10: " + ", ".join(f"{display(value)} ({count})"
                                                for value, count in counts.most_common(10)))

    # Look for malformed rows and values that look shifted into the wrong column.
    lines.append("\nCHECK: malformed field counts and likely wrong-column values")
    found = False
    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) != len(headers):
            lines.append(f"  Row {row_number}: {len(row)} fields, expected {len(headers)}: {row}")
            found = True
        for index, value in enumerate(row):
            kind = column_kind(headers[index]) if index < len(headers) else "other"
            reason = None
            if "@" in value and kind != "email":
                reason = "contains @ outside an email column"
            elif "," in value and kind != "skills":
                reason = "contains a comma-separated value outside a skill column"
            elif usable_email(value) and kind != "email":
                reason = "looks like an email outside an email column"
            elif usable_phone(value) and kind not in {"phone", "numeric"}:
                reason = "looks like a phone number outside a phone column"
            if reason:
                lines.append(f"  Row {row_number}, column {headers[index]}: {reason}: {display(value)}")
                found = True
    if not found:
        lines.append("  None found.")

    # Detect records that duplicate the header or contain no values at all.
    lines.append("\nCHECK: repeated header rows")
    repeated = [number for number, row in enumerate(rows[1:], 2) if row == headers]
    lines.append("  " + (", ".join(f"row {n}" for n in repeated) if repeated else "None found."))
    lines.append("\nCHECK: completely empty rows")
    empty_rows = [number for number, row in enumerate(rows[1:], 2) if not any(value.strip() for value in row)]
    lines.append("  " + (", ".join(f"row {n}" for n in empty_rows) if empty_rows else "None found."))

    for column in frame.columns:
        kind = column_kind(column)
        series = frame[column].astype(str)
        if kind == "phone":
            # Group phone values by representation shape without normalizing the source values.
            groups: defaultdict[str, list[str]] = defaultdict(list)
            for value in series:
                if usable_phone(value):
                    groups[phone_shape(value)].append(value)
            lines.append(f"\nCHECK: phone shapes in {column}")
            if groups:
                for shape, values in groups.items():
                    lines.append(f"  {shape}: {len(values)}; example {display(values[0])}")
            else:
                lines.append("  No usable phone values found.")
        elif kind == "date":
            # Group date values by detected format, flag ambiguous numeric dates, and check the cutoff.
            groups: defaultdict[str, list[str]] = defaultdict(list)
            ambiguous_values: list[str] = []
            late_values: list[str] = []
            for value in series:
                if not value.strip():
                    continue
                pattern, parsed, ambiguous = date_pattern(value)
                groups[pattern].append(value)
                if ambiguous:
                    ambiguous_values.append(value)
                if parsed and parsed > CUTOFF_DATE:
                    late_values.append(value)
            lines.append(f"\nCHECK: date formats in {column}")
            for pattern, values in groups.items():
                lines.append(f"  {pattern}: {len(values)}; example {display(values[0])}")
            lines.append("  Ambiguous DD/MM vs MM/DD: " + (", ".join(map(display, ambiguous_values)) if ambiguous_values else "none"))
            lines.append(f"  Later than {CUTOFF_DATE.isoformat()}: " + (", ".join(map(display, late_values)) if late_values else "none"))
        elif kind == "numeric":
            # Check numeric ranges and unit markers that may indicate mixed measurement units.
            parsed = [(value, numeric_value(value)) for value in series if value.strip()]
            numbers = [number for _, number in parsed if number is not None]
            lines.append(f"\nCHECK: numeric range and units in {column}")
            if numbers:
                lines.append(f"  min={min(numbers):g}; max={max(numbers):g}; numeric values={len(numbers)}/{len(parsed)}")
                if any(number < 100 for number in numbers) and any(number > 100000 for number in numbers):
                    lines.append("  FLAG: values span under 100 and over 100000, suggesting mixed units.")
                hourly = [value for value, _ in parsed if re.search(r"/hr|/hour|per hour", value.casefold())]
                monthly = [value for value, _ in parsed if re.search(r"/month|per month|k/month", value.casefold())]
                if hourly and monthly:
                    lines.append(f"  FLAG: mixed rate units (/hr={len(hourly)}, monthly={len(monthly)}).")
            else:
                lines.append("  No numeric values found.")

    # Show case-folded/trimmed text groups so raw spelling variants remain visible.
    for column in frame.columns:
        if column_kind(column) != "text":
            continue
        # Report raw variants that collapse to the same case-folded, whitespace-trimmed value.
        variants: defaultdict[str, Counter[str]] = defaultdict(Counter)
        for value in frame[column].astype(str):
            if value.strip():
                variants[normalized(value)][value] += 1
        lines.append(f"\nCHECK: normalized text variants in {column}")
        for key, raw_variants in sorted(variants.items()):
            raw_text = ", ".join(f"{display(raw)} ({count})" for raw, count in raw_variants.items())
            lines.append(f"  {display(key)}: {raw_text}")

    # Find within-file duplicate candidates using each requested identity signal separately.
    lines.append("\nCHECK: within-file duplicate candidates")
    for identity, groups in duplicate_groups(frame):
        lines.append(f"  By {identity}:")
        if not groups:
            lines.append("    None found.")
        for key, indexes in groups.items():
            lines.append(f"    {display(key)}: rows {', '.join(str(index + 2) for index in indexes)}")
            for index in indexes:
                lines.append(f"      {row_text(headers, rows[index + 1])}")
    return frame, headers, rows


def duplicate_groups(frame: pd.DataFrame) -> list[tuple[str, dict[str, list[int]]]]:
    email_columns = [column for column in frame.columns if column_kind(column) == "email"]
    phone_columns = [column for column in frame.columns if column_kind(column) == "phone"]
    name_columns = [column for column in frame.columns if column_kind(column) == "text" and "name" in column.casefold()]
    definitions = [
        ("normalized email", lambda row: next((normalized(row[column]) for column in email_columns if usable_email(row[column])), "")),
        ("last 10 phone digits", lambda row: next((phone_digits(row[column])[-10:] for column in phone_columns if usable_phone(row[column])), "")),
        ("case-folded name", lambda row: next((normalized(row[column]) for column in name_columns if normalized(row[column])), "")),
    ]
    result = []
    for label, key_function in definitions:
        grouped: defaultdict[str, list[int]] = defaultdict(list)
        for index, row in frame.iterrows():
            key = key_function(row)
            if key:
                grouped[key].append(index)
        result.append((label, {key: indexes for key, indexes in grouped.items() if len(indexes) > 1}))
    return result


def cross_file_report(frames: list[tuple[Path, pd.DataFrame]], lines: list[str]) -> None:
    # Count usable join keys in each file to show which cross-source joins are possible.
    lines.extend(["", "=" * 78, "CROSS-FILE JOIN KEY AVAILABILITY", "=" * 78])
    for path, frame in frames:
        emails = sum(usable_email(value) for column in frame.columns if column_kind(column) == "email" for value in frame[column])
        phones = sum(usable_phone(value) for column in frame.columns if column_kind(column) == "phone" for value in frame[column])
        lines.append(f"{path.name}: usable email rows={emails}; usable phone rows={phones}")

    # Find same names across files whose contact details disagree, as possible distinct people sharing a name.
    lines.append("\nCHECK: cross-file names with conflicting emails or phones")
    people: defaultdict[str, list[tuple[str, str, str, list[str]]]] = defaultdict(list)
    for path, frame in frames:
        name_columns = [column for column in frame.columns if column_kind(column) == "text" and "name" in column.casefold()]
        email_columns = [column for column in frame.columns if column_kind(column) == "email"]
        phone_columns = [column for column in frame.columns if column_kind(column) == "phone"]
        for _, row in frame.iterrows():
            name = next((normalized(row[column]) for column in name_columns if normalized(row[column])), "")
            if not name:
                continue
            email = next((normalized(row[column]) for column in email_columns if usable_email(row[column])), "")
            phone = next((phone_digits(row[column])[-10:] for column in phone_columns if usable_phone(row[column])), "")
            people[name].append((path.name, email, phone, [f"{column}={display(row[column])}" for column in frame.columns]))
    found = False
    for name, records in sorted(people.items()):
        emails = {record[1] for record in records if record[1]}
        phones = {record[2] for record in records if record[2]}
        if len(emails) > 1 or len(phones) > 1:
            found = True
            lines.append(f"  {display(name)}:")
            if len(emails) > 1:
                lines.append("    Conflicting emails: " + ", ".join(display(value) for value in sorted(emails)))
            if len(phones) > 1:
                lines.append("    Conflicting phones: " + ", ".join(display(value) for value in sorted(phones)))
            for filename, email, phone, values in records:
                lines.append(f"    {filename}: " + ", ".join(values))
    if not found:
        lines.append("  None found.")


def main() -> None:
    lines = ["CSV DATA PROFILE REPORT", f"Generated for cutoff date {CUTOFF_DATE.isoformat()}"]
    frames: list[tuple[Path, pd.DataFrame]] = []
    for path in FILES:
        frame, _, _ = report_file(path, lines)
        frames.append((path, frame))
    cross_file_report(frames, lines)
    report = "\n".join(lines) + "\n"
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
