"""Upload a file to the Supabase Storage 'audio' bucket.

Uses the Storage REST endpoint directly instead of the supabase-py SDK. The
whole interaction is one POST and one URL to build, and going direct keeps the
failure mode visible: we get the status code and body back and can decide what
to do, rather than catching an SDK exception and guessing what happened.
"""

import os

import requests

BUCKET = "audio"
TIMEOUT_SECONDS = 30


def _config():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_ANON_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env")
    return url, key


def public_url(object_path):
    url, _ = _config()
    return f"{url}/storage/v1/object/public/{BUCKET}/{object_path}"


def upload(local_path, object_path, content_type):
    """Upload one file. Returns (public_url, None) or (None, error_message).

    Never raises. The caller stores the submission either way -- losing a
    recording's metadata because a network call failed would be worse than
    having a row with no playable URL.
    """
    try:
        url, key = _config()
        endpoint = f"{url}/storage/v1/object/{BUCKET}/{object_path}"
        with open(local_path, "rb") as handle:
            response = requests.post(
                endpoint,
                data=handle,
                # No x-upsert header: Supabase treats an upsert as needing an
                # UPDATE policy as well as INSERT, and the bucket only grants
                # INSERT to anon. Object names are uuid4 anyway, so there is
                # nothing to overwrite.
                headers={
                    "Authorization": f"Bearer {key}",
                    "apikey": key,
                    "Content-Type": content_type or "application/octet-stream",
                },
                timeout=TIMEOUT_SECONDS,
            )
    except Exception as error:                      # network down, DNS, timeout
        return None, f"upload failed: {type(error).__name__}: {error}"

    if response.status_code in (200, 201):
        return public_url(object_path), None

    return None, f"upload failed: HTTP {response.status_code}: {response.text[:300]}"
