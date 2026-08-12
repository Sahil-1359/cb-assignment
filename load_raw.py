"""Load the three source CSVs into the raw_* tables with zero cleaning.

Every row from every file ends up in exactly one place: its raw table, or
quarantine. Nothing is dropped, so rows_read == rows_loaded + rows_quarantined
for each file.

Usage:  python load_raw.py
Needs:  DATABASE_URL in .env, and schema.sql already applied.

Note on the CSV reader: the brief for this script said pandas with
dtype=str, keep_default_na=False. This uses csv.reader instead, because the
point of the script is to catch malformed rows and pandas cannot hand them to
us -- a row with the wrong field count either raises or gets silently padded,
and either way the original line text is gone. csv.reader gives the same
"everything is a string, nothing becomes NaN" behaviour that dtype=str plus
keep_default_na=False was asking for, and it also gives the exact line we need
for quarantine.raw_line.
"""

import csv
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

BASE_DIR = Path(__file__).resolve().parent
FILES_DIR = BASE_DIR / "files"


# Per-file configuration. db_columns lines up positionally with the CSV header.
# anchor is the column index whose content identifies a correctly aligned row,
# and anchor_test says what a good value in that column looks like.
SOURCES = [
    {
        "filename": "source1_naukri_applicants.csv",
        "table": "raw_naukri",
        "db_columns": [
            "full_name", "email", "phone", "city",
            "experience_years", "current_ctc", "applied_date", "skills",
        ],
        "anchor": 1,           # Email
        "anchor_test": "email",
    },
    {
        "filename": "source2_gig_workers.csv",
        "table": "raw_gigs",
        "db_columns": [
            "email_id", "worker_name", "rate", "location", "status", "skill_tags",
        ],
        "anchor": 0,           # email_id
        "anchor_test": "email",
    },
    {
        "filename": "source3_cbnexus_contacts.csv",
        "table": "raw_nexus",
        "db_columns": [
            "name", "phone_number", "city", "verified", "projects_completed",
        ],
        "anchor": 1,           # Phone Number
        "anchor_test": "phone",
    },
]


def looks_like_email(value):
    return "@" in value


def looks_like_phone(value):
    digits = [character for character in value if character.isdigit()]
    return len(digits) >= 10


ANCHOR_TESTS = {"email": looks_like_email, "phone": looks_like_phone}


def find_quarantine_reason(row, header, source):
    """Return a reason string if this row should be quarantined, else None.

    Only the four agreed categories quarantine a row. Anything else -- odd
    casing, mixed units, an implausible date -- is a Phase 2 problem and gets
    loaded as-is.
    """
    if not any(field.strip() for field in row):
        return "completely empty row"

    if row == header:
        return "repeated header row"

    if len(row) != len(header):
        return f"field count mismatch: got {len(row)}, expected {len(header)}"

    # Same field count but the values sat down in the wrong seats. We detect it
    # by checking whether the anchor column holds the kind of value it should,
    # and whether that value turned up in some other column instead.
    test = ANCHOR_TESTS[source["anchor_test"]]
    anchor_index = source["anchor"]
    if not test(row[anchor_index]):
        for index, value in enumerate(row):
            if index != anchor_index and test(value):
                anchor_name = header[anchor_index]
                return (
                    f"field shifted: {anchor_name!r} holds {row[anchor_index]!r}, "
                    f"while {header[index]!r} holds the {source['anchor_test']}"
                )

    return None


def read_rows(path):
    """Return (header, [(row_num, fields, raw_line), ...]).

    Lines are read once into memory and parsed from that list, so raw_line and
    the parsed fields always come from the same text. Files are small.
    """
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        lines = handle.readlines()

    reader = csv.reader(lines)
    parsed = []
    for fields in reader:
        row_num = reader.line_num          # 1-based, header is line 1
        raw_line = lines[row_num - 1].rstrip("\r\n")
        parsed.append((row_num, fields, raw_line))

    header = parsed[0][1]
    return header, parsed[1:]


def load_source(connection, source):
    path = FILES_DIR / source["filename"]
    header, data_rows = read_rows(path)

    db_columns = source["db_columns"]
    column_list = ", ".join(["row_num"] + db_columns + ["raw_json"])
    placeholders = ", ".join([":row_num"] + [f":{name}" for name in db_columns] + [":raw_json"])
    insert_sql = text(
        f"INSERT INTO {source['table']} ({column_list}) VALUES ({placeholders})"
    )
    quarantine_sql = text(
        "INSERT INTO quarantine (source_file, row_num, raw_line, reason) "
        "VALUES (:source_file, :row_num, :raw_line, :reason)"
    )

    good_rows = []
    bad_rows = []
    for row_num, fields, raw_line in data_rows:
        reason = find_quarantine_reason(fields, header, source)
        if reason:
            bad_rows.append({
                "source_file": source["filename"],
                "row_num": row_num,
                "raw_line": raw_line,
                "reason": reason,
            })
            continue

        # raw_json keeps the original CSV header names, not our column names.
        parameters = {"row_num": row_num}
        for position, name in enumerate(db_columns):
            parameters[name] = fields[position]
        parameters["raw_json"] = json.dumps(dict(zip(header, fields)))
        good_rows.append(parameters)

    if good_rows:
        connection.execute(insert_sql, good_rows)
    if bad_rows:
        connection.execute(quarantine_sql, bad_rows)

    print(f"{source['filename']}")
    print(f"  rows read:        {len(data_rows)}")
    print(f"  rows loaded:      {len(good_rows)}  -> {source['table']}")
    print(f"  rows quarantined: {len(bad_rows)}")
    for row in bad_rows:
        print(f"    line {row['row_num']}: {row['reason']}")
    print()

    return len(data_rows), len(good_rows), len(bad_rows)


def database_engine():
    """Build the engine from DATABASE_URL.

    Supabase hands out a plain 'postgresql://' URL, which SQLAlchemy resolves to
    psycopg2. We use psycopg 3, so the scheme is pointed at that driver here
    rather than making .env carry a SQLAlchemy-specific prefix.
    """
    load_dotenv(BASE_DIR / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set. Put it in .env next to this script.")
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(database_url)


def main():
    engine = database_engine()
    total_read = total_loaded = total_quarantined = 0

    with engine.begin() as connection:
        # Re-runnable: clear the raw layer, leave people/audio_submissions alone.
        connection.execute(text("TRUNCATE raw_naukri, raw_gigs, raw_nexus, quarantine"))
        for source in SOURCES:
            read, loaded, quarantined = load_source(connection, source)
            total_read += read
            total_loaded += loaded
            total_quarantined += quarantined

    print(f"TOTAL  read={total_read}  loaded={total_loaded}  quarantined={total_quarantined}")


if __name__ == "__main__":
    main()
