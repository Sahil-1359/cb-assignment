"""Mini audio collection app (Task 3).

Two routes:
  /             GET shows the form, POST accepts a submission
  /submissions  lists everything collected, with a player and the metrics

Run from the repo root:
    python -m app.main
then open http://127.0.0.1:5000
"""

import os
import sys
import tempfile
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for
from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env")

# Reused from the merge pipeline on purpose: a submitted phone has to be
# normalised exactly the way people.phone_10 was, or the lookup silently
# misses every time.
from build_people import database_engine, norm_phone   # noqa: E402
from app import audio, storage                          # noqa: E402

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024     # 25 MB per upload

# Extensions we will accept. The list is short on purpose; ffmpeg can read far
# more, but accepting arbitrary uploads into a public bucket is not free.
ALLOWED_EXTENSIONS = {".webm", ".ogg", ".mp3", ".m4a", ".wav", ".aac", ".flac"}


def find_person_id(connection, phone_10):
    """Return the people.id whose phone matches, or None."""
    if not phone_10:
        return None
    return connection.execute(
        text("SELECT id FROM people WHERE phone_10 = :phone LIMIT 1"),
        {"phone": phone_10},
    ).scalar()


def extension_for(filename, content_type):
    suffix = Path(filename or "").suffix.lower()
    if suffix in ALLOWED_EXTENSIONS:
        return suffix
    # MediaRecorder blobs arrive with a generic name but a real MIME type.
    if content_type and "webm" in content_type:
        return ".webm"
    if content_type and "ogg" in content_type:
        return ".ogg"
    if content_type and "mp4" in content_type:
        return ".m4a"
    return None


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return render_template("index.html", error=None)

    name = (request.form.get("name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    upload_file = request.files.get("audio")

    if not name or not phone:
        return render_template("index.html", error="Name and phone are both required."), 400
    if upload_file is None or not upload_file.filename:
        return render_template("index.html", error="Record or choose an audio file first."), 400

    suffix = extension_for(upload_file.filename, upload_file.mimetype)
    if suffix is None:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        return render_template(
            "index.html", error=f"Unsupported audio type. Allowed: {allowed}"
        ), 400

    # Write to a temp file first. Metrics are extracted locally, so a storage
    # outage costs us the playable URL but never the measurements.
    temp_handle, temp_path = tempfile.mkstemp(suffix=suffix)
    os.close(temp_handle)
    try:
        upload_file.save(temp_path)

        try:
            metrics = audio.analyse(temp_path)
        except Exception as error:
            return render_template(
                "index.html",
                error=f"Could not read that audio file ({type(error).__name__}). "
                      "Try a different recording.",
            ), 400

        object_path = f"{uuid.uuid4().hex}{suffix}"
        audio_url, upload_error = storage.upload(
            temp_path, object_path, upload_file.mimetype
        )

        phone_10 = norm_phone(phone)
        engine = database_engine()
        with engine.begin() as connection:
            person_id = find_person_id(connection, phone_10)
            connection.execute(
                text(
                    "INSERT INTO audio_submissions (person_id, submitted_name,"
                    " submitted_phone, audio_url, duration_sec, sample_rate_hz,"
                    " bitrate_kbps, loudness_db, snr_db)"
                    " VALUES (:person_id, :submitted_name, :submitted_phone,"
                    " :audio_url, :duration_sec, :sample_rate_hz, :bitrate_kbps,"
                    " :loudness_db, :snr_db)"
                ),
                {
                    "person_id": person_id,
                    "submitted_name": name,
                    "submitted_phone": phone,
                    "audio_url": audio_url,
                    **metrics,
                },
            )
    finally:
        os.unlink(temp_path)

    if upload_error:
        # The row is saved. Say so plainly rather than pretending it worked.
        app.logger.warning("storage upload failed: %s", upload_error)
        return redirect(url_for("submissions", warning="storage"))

    return redirect(url_for("submissions"))


@app.route("/submissions")
def submissions():
    engine = database_engine()
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT s.id, s.submitted_name, s.submitted_phone, s.audio_url,"
            " s.duration_sec, s.sample_rate_hz, s.bitrate_kbps, s.loudness_db,"
            " s.snr_db, s.created_at, s.person_id, p.full_name AS matched_name"
            " FROM audio_submissions s"
            " LEFT JOIN people p ON p.id = s.person_id"
            " ORDER BY s.created_at DESC, s.id DESC"
        )).mappings().all()
    return render_template(
        "submissions.html", rows=rows, warning=request.args.get("warning")
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
