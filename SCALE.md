# Task 5 — Launching the audio app to 5,000 gig workers over one weekend

Draft. Numbers assume ~5,000 workers submitting one recording each over roughly
48 hours, arriving unevenly — a large fraction in the first few hours after the
announcement goes out, not spread evenly.

## What breaks first

**The database connection pool, within minutes.** `app/main.py` calls
`database_engine()` inside each request handler, so every page view and every
submission constructs a brand new SQLAlchemy engine with its own connection
pool, uses one connection, and leaves the pool to be garbage collected. Under
one user this is invisible. Under fifty concurrent submitters it opens
connections faster than Supabase closes them, and the free tier's pooler starts
refusing new ones. Everything else on this list is a slower burn; this one takes
the whole app down early and looks like a Supabase outage rather than a bug in
our code. Fix before launch: build the engine once at import time and share it.

## Storage volume and cost

Recording size varies by an order of magnitude depending on what the browser
hands us. A 60-second browser recording in WebM/Opus is roughly 240 KB. The same
minute uploaded as an uncompressed WAV from a phone's voice-memo app is about
5 MB. We accept both.

- All Opus, 60s each: ~1.2 GB
- Mixed reality, averaging ~2 MB: ~10 GB
- All WAV: ~25 GB

Supabase's free tier includes 1 GB of storage and 5 GB of egress per month. Even
the optimistic case exceeds storage; the realistic case exceeds it roughly
tenfold, and a single pass by a reviewer playing back every file blows through
egress on top of that. The app would begin failing uploads partway through
Saturday, and because those failures are handled gracefully we would accumulate
thousands of rows with metrics and no audio — quietly, without anyone noticing
until someone tries to listen.

Before launch: transcode server-side to a fixed codec and bitrate on receipt so
size per submission is predictable, cap duration in the UI and enforce it
server-side, and move to a paid storage tier with a billing alert. Predictable
size per submission matters more than the tier, because it turns cost from a
question into arithmetic.

## Upload failures and retries

The current failure handling is one-directional: if the upload to Supabase
fails, we keep the database row and set `audio_url` to null. Nothing ever
retries, and the audio itself is gone — it lived in a temp file that is deleted
in a `finally` block. The worker sees a page that looks broadly successful and
has no way to know their recording did not survive, and no way to resubmit
except by recording again.

On a mobile network across a weekend, a few percent of uploads failing is
normal, not exceptional. At 5,000 submissions that is well over a hundred lost
recordings, each belonging to a worker who believes they are done.

Before launch: upload first and only write the row once storage confirms, or
keep the file in a durable spool and retry from a background worker. Show the
worker an unambiguous success or failure. Add a "your recording did not save,
try again" path rather than a note on a table they will never look at.

## Duplicate submissions

There is no uniqueness constraint of any kind on `audio_submissions` and no
idempotency on the form. Three separate sources of duplicates:

- A worker submits twice because the first attempt looked like it failed, or
  because they were unsure it worked. This is the common case and the retry
  problem above actively encourages it.
- A double-clicked submit button fires two POSTs, uploading the file twice under
  two different UUIDs and inserting two rows.
- A worker who was already paid for a submission simply submits again.

At 5,000 workers, if payment is tied to submissions, duplicates are a direct
financial leak rather than a tidiness problem. Before launch: disable the submit
button on click, add an idempotency key per form render, and decide the business
rule — one recording per phone number, enforced in the database, or many
recordings allowed with explicit numbering. That is a decision to be made
deliberately, not left to whatever the code happens to do.

Related: `person_id` matches on normalised phone against `people`, which holds
55 rows. 5,000 gig workers will overwhelmingly not be in it, so nearly every
submission lands with a null `person_id`. That is correct behaviour but it means
the link is close to useless at this scale; the workers need to be loaded into
`people` first, or the submission flow needs to create them.

## Supabase free tier limits

Beyond storage and egress: the free tier pauses projects after a week of
inactivity, caps database size at 500 MB, and limits pooler connections in a way
that the per-request engine problem above will hit long before row count
matters. The row data itself is trivial — 5,000 rows of metrics is well under a
megabyte. Storage, egress, and connections are the binding constraints, in that
order. None of them are visible until they are exceeded, so the practical
requirement before launch is a dashboard someone actually watches, plus billing
alerts, not just a higher tier.

## The anon INSERT policy is an open door

This is the one I would fix first regardless of load, because it is not a
capacity problem and it does not get better if the launch goes well.

The bucket grants INSERT to the `anon` role for `bucket_id = 'audio'`, and the
bucket is public-read. The anon key is not a secret in any meaningful sense — it
is designed to be shipped to browsers, and anyone who obtains it can POST
directly to the storage API without going anywhere near our form. That means:

- Our 25 MB limit and our extension allowlist are enforced in Flask, so they are
  enforced only for people who choose to use the form. A direct API call ignores
  both.
- Anyone can fill the bucket, which is the storage cost problem above except
  adversarial and unbounded.
- Anyone can host arbitrary files, of any content, on a public URL under our
  Supabase domain, and we would be serving them.
- Public-read plus predictable URL structure means every worker's recording is
  world-readable to anyone holding the URL.

Separately, `/submissions` has no authentication at all. It lists every
submitter's name, phone number, and a playable recording. At 5,000 workers that
is a substantial pile of personal data on an open page.

Before launch: uploads should go through the server with a service-role key held
server-side, with the anon INSERT policy removed entirely; the bucket should be
private with time-limited signed URLs for playback; and `/submissions` needs to
be behind a login. The current arrangement is fine for a demo with three rows in
it and is not fine for real people's phone numbers.

## Summary of what I would change before launch

1. One shared database engine instead of one per request — this is the launch blocker.
2. Uploads through the server with a service-role key; drop the anon INSERT policy.
3. Private bucket, signed playback URLs, authentication on `/submissions`.
4. Server-side transcode to a fixed bitrate and an enforced duration cap.
5. Durable spool and retry for uploads, with honest success/failure shown to the worker.
6. A decided-and-enforced rule on duplicate submissions per phone number.
7. Paid storage tier, billing alerts, and a dashboard someone is actually watching.
