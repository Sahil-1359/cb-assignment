"""Merge the raw_* tables into one row per person in the people table.

Usage:  python build_people.py
Needs:  DATABASE_URL in .env, and load_raw.py already run.

Matching rules, applied in this order for every incoming record:

  1. exact normalised email matches an existing record   -> confidence high
  2. exact normalised phone matches an existing record   -> confidence high
  3. name matches, with no conflicting email or phone    -> confidence low,
                                                            needs_review = true
  4. name matches but email or phone conflicts           -> do NOT merge, keep
                                                            both, both flagged
                                                            needs_review = true

Records are fed in file order: naukri, then gigs, then nexus. Naukri is the
only source with both an email and a phone, so it is the hub every other file
joins onto, and processing it first means the "prefer the naukri value" rule
for field conflicts is simply "prefer the value that is already there".
"""

import os
import re
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------

CITY_ALIASES = {
    "Bangalore": "Bengaluru",
    "Gurgaon": "Gurugram",
    "New Delhi": "Delhi",
    "Delhi Ncr": "Delhi",
    "Delhi NCR": "Delhi",
}

STATUS_VALUES = {"active": "active", "inactive": "inactive", "paused": "paused"}

VERIFIED_TRUE = {"y", "yes"}
VERIFIED_FALSE = {"n", "no"}


def norm_email(value):
    return (value or "").strip().lower()


def norm_phone(value):
    """Digits only, country code and trunk zero removed, last 10 kept."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 10:
        return ""
    return digits[-10:]


def norm_city(value):
    city = " ".join((value or "").split()).title()
    return CITY_ALIASES.get(city, city)


def norm_name(value):
    """Display form: collapse whitespace, title case only if the source shouted."""
    name = " ".join((value or "").split())
    if name.isupper():
        name = name.title()
    return name


def name_key(value):
    """The form used for matching. Case and spacing are not identity."""
    return " ".join((value or "").split()).casefold()


def norm_status(value):
    return STATUS_VALUES.get((value or "").strip().lower())


def norm_verified(value):
    text_value = (value or "").strip().lower()
    if text_value in VERIFIED_TRUE:
        return True
    if text_value in VERIFIED_FALSE:
        return False
    return None


def norm_ctc_lpa(value):
    """Current CTC is either lakhs per annum or plain rupees. Return LPA."""
    text_value = (value or "").strip().replace(",", "")
    if not text_value:
        return None
    try:
        number = float(text_value)
    except ValueError:
        return None
    return number if number < 100 else number / 100000


def norm_rate(value):
    """Return (rate_value, rate_unit).

    Deliberately NOT converted to a single monthly figure. The file mixes
    'N/hr' (330-1483) with 'Nk/month' (15k-79k); those two ranges do not
    reconcile under any sane hours-per-month factor, so picking one would be
    inventing data. Both parts are stored and the ambiguity is reported.
    """
    text_value = (value or "").strip().lower()
    if not text_value:
        return None, None
    hourly = re.fullmatch(r"([\d.]+)\s*/\s*hr", text_value)
    if hourly:
        return float(hourly.group(1)), "per_hour"
    monthly = re.fullmatch(r"([\d.]+)\s*k\s*/\s*month", text_value)
    if monthly:
        return float(monthly.group(1)) * 1000, "per_month"
    return None, None


def norm_number(value, cast=float):
    text_value = (value or "").strip().replace(",", "")
    if not text_value:
        return None
    try:
        return cast(float(text_value))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tiebreaks for two rows from the same source, where "prefer naukri" says
# nothing because both rows are naukri.
# ---------------------------------------------------------------------------

def name_completeness(name):
    """Higher is better. An initial never beats a spelled-out first name."""
    parts = name.split()
    spelled_out = sum(1 for part in parts if len(part.strip(".")) > 1)
    return (spelled_out, len(name))


def better_name(existing, incoming):
    if name_completeness(incoming) > name_completeness(existing):
        return incoming
    return existing


def better_email(existing, incoming, emails_in_other_sources):
    """Keep the email that another source also knows about; else the unprefixed one."""
    existing_known = existing in emails_in_other_sources
    incoming_known = incoming in emails_in_other_sources
    if existing_known != incoming_known:
        return incoming if incoming_known else existing

    existing_prefixed = existing.startswith("alt.")
    incoming_prefixed = incoming.startswith("alt.")
    if existing_prefixed != incoming_prefixed:
        return existing if incoming_prefixed else incoming

    return existing


# ---------------------------------------------------------------------------
# Reading the raw layer into a single flat list of records
# ---------------------------------------------------------------------------

def load_records(connection):
    """One dict per raw row, normalised, in naukri -> gigs -> nexus order."""
    records = []

    rows = connection.execute(text(
        "SELECT row_num, full_name, email, phone, city, experience_years,"
        " current_ctc, applied_date, skills FROM raw_naukri ORDER BY row_num"
    )).mappings().all()
    for row in rows:
        records.append({
            "source": "naukri",
            "row_num": row["row_num"],
            "full_name": norm_name(row["full_name"]),
            "email": norm_email(row["email"]),
            "phone_10": norm_phone(row["phone"]),
            "city": norm_city(row["city"]),
            "experience_years": norm_number(row["experience_years"]),
            "ctc_lpa": norm_ctc_lpa(row["current_ctc"]),
            "rate_value": None,
            "rate_unit": None,
            "status": None,
            "verified": None,
            "projects_completed": None,
            "skills": (row["skills"] or "").strip(),
        })

    rows = connection.execute(text(
        "SELECT row_num, email_id, worker_name, rate, location, status, skill_tags"
        " FROM raw_gigs ORDER BY row_num"
    )).mappings().all()
    for row in rows:
        rate_value, rate_unit = norm_rate(row["rate"])
        records.append({
            "source": "gigs",
            "row_num": row["row_num"],
            "full_name": norm_name(row["worker_name"]),
            "email": norm_email(row["email_id"]),
            "phone_10": "",
            "city": norm_city(row["location"]),
            "experience_years": None,
            "ctc_lpa": None,
            "rate_value": rate_value,
            "rate_unit": rate_unit,
            "status": norm_status(row["status"]),
            "verified": None,
            "projects_completed": None,
            "skills": (row["skill_tags"] or "").strip(),
        })

    rows = connection.execute(text(
        "SELECT row_num, name, phone_number, city, verified, projects_completed"
        " FROM raw_nexus ORDER BY row_num"
    )).mappings().all()
    for row in rows:
        records.append({
            "source": "nexus",
            "row_num": row["row_num"],
            "full_name": norm_name(row["name"]),
            "email": "",
            "phone_10": norm_phone(row["phone_number"]),
            "city": norm_city(row["city"]),
            "experience_years": None,
            "ctc_lpa": None,
            "rate_value": None,
            "rate_unit": None,
            "status": None,
            "verified": norm_verified(row["verified"]),
            "projects_completed": norm_number(row["projects_completed"], int),
            "skills": "",
        })

    return records


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

# Fields where a merge just fills a gap or hits a conflict. full_name and
# email are handled separately because they have their own tiebreaks.
MERGE_FIELDS = [
    "phone_10", "city", "experience_years", "ctc_lpa", "rate_value",
    "rate_unit", "status", "verified", "projects_completed", "skills",
]


def new_person(record):
    person = {
        "full_name": record["full_name"],
        "email": record["email"],
        "alternate_emails": [],
        "emails": {record["email"]} if record["email"] else set(),
        "phones": {record["phone_10"]} if record["phone_10"] else set(),
        "name_keys": {name_key(record["full_name"])},
        "in_naukri": record["source"] == "naukri",
        "in_gigs": record["source"] == "gigs",
        "in_nexus": record["source"] == "nexus",
        "methods": [],
        "confidences": [],
        "needs_review": False,
        "sources": [f"{record['source']}:{record['row_num']}"],
    }
    for field in MERGE_FIELDS:
        person[field] = record[field]
    return person


def conflicts_with(person, record):
    """True if this record disagrees with the person on a hard identifier."""
    if record["email"] and person["emails"] and record["email"] not in person["emails"]:
        return True
    if record["phone_10"] and person["phones"] and record["phone_10"] not in person["phones"]:
        return True
    return False


def merge_into(person, record, method, confidence, emails_by_source, conflict_log):
    person[f"in_{record['source']}"] = True
    person["methods"].append(method)
    person["confidences"].append(confidence)
    person["sources"].append(f"{record['source']}:{record['row_num']}")
    person["name_keys"].add(name_key(record["full_name"]))

    # Name: more complete wins, regardless of source.
    if record["full_name"]:
        if not person["full_name"]:
            person["full_name"] = record["full_name"]
        elif name_key(record["full_name"]) != name_key(person["full_name"]):
            kept = better_name(person["full_name"], record["full_name"])
            dropped = record["full_name"] if kept == person["full_name"] else person["full_name"]
            conflict_log.append(
                f"name: kept {kept!r}, dropped {dropped!r} "
                f"({' + '.join(person['sources'])})"
            )
            person["full_name"] = kept

    # Email: the loser is kept in alternate_emails rather than thrown away.
    if record["email"]:
        person["emails"].add(record["email"])
        if not person["email"]:
            person["email"] = record["email"]
        elif record["email"] != person["email"]:
            other_source_emails = emails_by_source["gigs"] | emails_by_source["naukri"]
            kept = better_email(person["email"], record["email"], other_source_emails)
            dropped = record["email"] if kept == person["email"] else person["email"]
            if dropped not in person["alternate_emails"]:
                person["alternate_emails"].append(dropped)
            conflict_log.append(
                f"email: kept {kept!r}, moved {dropped!r} to alternate_emails "
                f"({' + '.join(person['sources'])})"
            )
            person["email"] = kept

    if record["phone_10"]:
        person["phones"].add(record["phone_10"])

    # Everything else: fill a blank, otherwise keep what is there. Because
    # naukri is processed first, "what is there" is the naukri value.
    for field in MERGE_FIELDS:
        incoming = record[field]
        if incoming is None or incoming == "":
            continue
        current = person[field]
        if current is None or current == "":
            person[field] = incoming
        elif current != incoming:
            conflict_log.append(
                f"{field}: kept {current!r}, dropped {incoming!r} "
                f"(from {record['source']} row {record['row_num']})"
            )


def build_people(records):
    people = []
    conflict_log = []

    # Used by the email tiebreak: which emails does more than one file know?
    emails_by_source = {"naukri": set(), "gigs": set(), "nexus": set()}
    for record in records:
        if record["email"]:
            emails_by_source[record["source"]].add(record["email"])

    for record in records:
        match = None
        method = None
        confidence = None

        # Rule 1: email
        if record["email"]:
            for person in people:
                if record["email"] in person["emails"]:
                    match, method, confidence = person, "email", "high"
                    break

        # Rule 2: phone
        if match is None and record["phone_10"]:
            for person in people:
                if record["phone_10"] in person["phones"]:
                    match, method, confidence = person, "phone", "high"
                    break

        # Rules 3 and 4: name
        if match is None:
            key = name_key(record["full_name"])
            blocked = False
            for person in people:
                if key not in person["name_keys"]:
                    continue
                if conflicts_with(person, record):
                    # Rule 4: same name, different person (or the same person
                    # with data we cannot reconcile). Keep them apart and flag
                    # both sides for a human.
                    person["needs_review"] = True
                    blocked = True
                    continue
                match, method, confidence = person, "name", "low"
                break
            if match is None and blocked:
                person = new_person(record)
                person["needs_review"] = True
                person["methods"].append("name_conflict_kept_separate")
                people.append(person)
                continue

        if match is None:
            people.append(new_person(record))
            continue

        merge_into(match, record, method, confidence, emails_by_source, conflict_log)
        if confidence == "low":
            match["needs_review"] = True

    return people, conflict_log


# ---------------------------------------------------------------------------
# Writing and reporting
# ---------------------------------------------------------------------------

def summarise(person):
    """Collapse the per-merge lists into the two columns the table holds."""
    if not person["methods"]:
        return "single_source", None
    method = "+".join(sorted(set(person["methods"])))
    confidence = "low" if "low" in person["confidences"] else None
    if confidence is None and person["confidences"]:
        confidence = "high"
    return method, confidence


def write_people(connection, people):
    connection.execute(text("TRUNCATE people RESTART IDENTITY CASCADE"))
    insert_sql = text(
        "INSERT INTO people (full_name, email, alternate_emails, phone_10, city,"
        " experience_years, ctc_lpa, rate_value, rate_unit, status, verified,"
        " projects_completed, skills, in_naukri, in_gigs, in_nexus,"
        " match_method, match_confidence, needs_review)"
        " VALUES (:full_name, :email, :alternate_emails, :phone_10, :city,"
        " :experience_years, :ctc_lpa, :rate_value, :rate_unit, :status, :verified,"
        " :projects_completed, :skills, :in_naukri, :in_gigs, :in_nexus,"
        " :match_method, :match_confidence, :needs_review)"
    )

    rows = []
    for person in people:
        method, confidence = summarise(person)
        rows.append({
            "full_name": person["full_name"] or None,
            "email": person["email"] or None,
            "alternate_emails": ", ".join(person["alternate_emails"]) or None,
            "phone_10": person["phone_10"] or None,
            "city": person["city"] or None,
            "experience_years": person["experience_years"],
            "ctc_lpa": person["ctc_lpa"],
            "rate_value": person["rate_value"],
            "rate_unit": person["rate_unit"],
            "status": person["status"],
            "verified": person["verified"],
            "projects_completed": person["projects_completed"],
            "skills": person["skills"] or None,
            "in_naukri": person["in_naukri"],
            "in_gigs": person["in_gigs"],
            "in_nexus": person["in_nexus"],
            "match_method": method,
            "match_confidence": confidence,
            "needs_review": person["needs_review"],
        })
    connection.execute(insert_sql, rows)


def report(records, people, conflict_log):
    print(f"raw records in:  {len(records)}")
    print(f"people out:      {len(people)}")
    print(f"records merged:  {len(records) - len(people)}")
    print()

    counts = {}
    for person in people:
        method, confidence = summarise(person)
        label = method if confidence is None else f"{method} ({confidence})"
        counts[label] = counts.get(label, 0) + 1
    print("by match_method:")
    for label in sorted(counts):
        print(f"  {label:<40} {counts[label]}")
    print()

    print("source coverage:")
    for flags, name in [
        (("in_naukri",), "naukri only"),
        (("in_gigs",), "gigs only"),
        (("in_nexus",), "nexus only"),
    ]:
        total = sum(
            1 for person in people
            if person[flags[0]]
            and sum([person["in_naukri"], person["in_gigs"], person["in_nexus"]]) == 1
        )
        print(f"  {name:<40} {total}")
    all_three = sum(
        1 for person in people
        if person["in_naukri"] and person["in_gigs"] and person["in_nexus"]
    )
    two = sum(
        1 for person in people
        if sum([person["in_naukri"], person["in_gigs"], person["in_nexus"]]) == 2
    )
    print(f"  {'in two sources':<40} {two}")
    print(f"  {'in all three sources':<40} {all_three}")
    print()

    needs_review = [person for person in people if person["needs_review"]]
    print(f"needs_review: {len(needs_review)}")
    for person in needs_review:
        print(f"  {person['full_name']:<20} {person['email'] or '-':<40} "
              f"{'+'.join(person['sources'])}")
    print()

    print(f"field conflicts logged: {len(conflict_log)}")
    for line in conflict_log:
        print(f"  {line}")


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
    with engine.begin() as connection:
        records = load_records(connection)
        people, conflict_log = build_people(records)
        write_people(connection, people)
    report(records, people, conflict_log)


if __name__ == "__main__":
    main()
