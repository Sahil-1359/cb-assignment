-- ConsultBae assignment schema.
-- Run with:  psql "$DATABASE_URL" -f schema.sql
--
-- Three layers:
--   raw_*              exact copy of each CSV, every column TEXT, zero cleaning
--   quarantine         rows that could not be trusted into a raw table, with a reason
--   people             the merged, cleaned records (Task 1 output)
--   audio_submissions  recordings collected by the Task 3 app
--
-- Dropped in dependency order so the whole file is re-runnable during development.

DROP TABLE IF EXISTS audio_submissions;
DROP TABLE IF EXISTS people;
DROP TABLE IF EXISTS quarantine;
DROP TABLE IF EXISTS raw_naukri;
DROP TABLE IF EXISTS raw_gigs;
DROP TABLE IF EXISTS raw_nexus;


-- ---------------------------------------------------------------------------
-- Raw layer. Column names match the CSV headers, lowercased and underscored.
-- Everything is TEXT on purpose: '4.2' and '417964' both live in Current CTC,
-- and deciding what they mean is a Phase 2 problem, not a load problem.
-- raw_json keeps the original header -> value mapping so nothing is lost even
-- if we later decide a column was parsed into the wrong slot.
-- ---------------------------------------------------------------------------

CREATE TABLE raw_naukri (
    row_num          INT PRIMARY KEY,   -- 1-based line number in the CSV, header is line 1
    full_name        TEXT,
    email            TEXT,
    phone            TEXT,
    city             TEXT,
    experience_years TEXT,
    current_ctc      TEXT,
    applied_date     TEXT,
    skills           TEXT,
    raw_json         JSONB NOT NULL
);

CREATE TABLE raw_gigs (
    row_num     INT PRIMARY KEY,
    email_id    TEXT,
    worker_name TEXT,
    rate        TEXT,
    location    TEXT,
    status      TEXT,
    skill_tags  TEXT,
    raw_json    JSONB NOT NULL
);

CREATE TABLE raw_nexus (
    row_num            INT PRIMARY KEY,
    name               TEXT,
    phone_number       TEXT,
    city               TEXT,
    verified           TEXT,
    projects_completed TEXT,
    raw_json           JSONB NOT NULL
);


-- ---------------------------------------------------------------------------
-- Quarantine. A row lands here instead of being dropped, so the row counts
-- always add up: rows_read = rows_loaded + rows_quarantined.
-- raw_line is the original text of the line, not a parsed version of it.
-- ---------------------------------------------------------------------------

CREATE TABLE quarantine (
    id          SERIAL PRIMARY KEY,
    source_file TEXT NOT NULL,
    row_num     INT  NOT NULL,
    raw_line    TEXT NOT NULL,
    reason      TEXT NOT NULL
);


-- ---------------------------------------------------------------------------
-- People: one row per real person after merging (Task 1).
--
-- rate_value / rate_unit are deliberately NOT collapsed into one monthly
-- number. The gig file mixes 'N/hr' (330-1483) with 'Nk/month' (15k-79k) and
-- the two populations barely overlap, so any hours-per-month factor would be
-- invented rather than derived. Stored separately, flagged in the report.
--
-- alternate_emails holds emails discarded during a merge (e.g. the 'alt.'
-- prefixed duplicate for Nikhil Chopra) so no contact detail is lost.
-- ---------------------------------------------------------------------------

CREATE TABLE people (
    id                 SERIAL PRIMARY KEY,
    full_name          TEXT,
    email              TEXT,
    alternate_emails   TEXT,      -- comma-separated; emails dropped during merge
    phone_10           TEXT,      -- last 10 digits, no country code
    city               TEXT,
    experience_years   NUMERIC,
    ctc_lpa            NUMERIC,   -- always lakhs per annum after normalisation
    rate_value         NUMERIC,   -- gig rate, in whatever unit rate_unit says
    rate_unit          TEXT,      -- 'per_hour' | 'per_month' | NULL
    status             TEXT,      -- 'active' | 'inactive' | 'paused' | NULL
    verified           BOOLEAN,
    projects_completed INT,
    skills             TEXT,
    in_naukri          BOOLEAN NOT NULL DEFAULT FALSE,
    in_gigs            BOOLEAN NOT NULL DEFAULT FALSE,
    in_nexus           BOOLEAN NOT NULL DEFAULT FALSE,
    match_method       TEXT,      -- which rule merged this record
    match_confidence   TEXT,      -- 'high' | 'low' | NULL for single-source rows
    needs_review       BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX people_email_idx ON people (email);
CREATE INDEX people_phone_idx ON people (phone_10);


-- ---------------------------------------------------------------------------
-- Audio submissions (Task 3). person_id is nullable: a walk-up submitter whose
-- phone matches nobody in people still gets their recording stored.
-- ON DELETE SET NULL so re-running the merge never destroys a submission.
-- ---------------------------------------------------------------------------

CREATE TABLE audio_submissions (
    id              SERIAL PRIMARY KEY,
    person_id       INT REFERENCES people(id) ON DELETE SET NULL,
    submitted_name  TEXT,
    submitted_phone TEXT,        -- as typed by the submitter
    audio_url       TEXT,        -- NULL if the storage upload failed
    duration_sec    NUMERIC,
    sample_rate_hz  INT,
    bitrate_kbps    NUMERIC,
    loudness_db     NUMERIC,     -- RMS, dBFS, so negative
    snr_db          NUMERIC,     -- rough estimate, see app/audio.py
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX audio_submissions_person_idx ON audio_submissions (person_id);
