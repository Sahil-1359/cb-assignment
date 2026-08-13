# The app shells out to ffmpeg/ffprobe for every metric it extracts, and no
# platform's stock Python runtime ships ffmpeg. That is the whole reason this
# deploys as a container rather than as a plain Python service.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY app/ ./app/
COPY build_people.py load_raw.py ./

# Two workers, two threads each: metric extraction is a blocking subprocess
# call, so threads let one request decode audio while another is served.
ENV PORT=8000
CMD gunicorn --bind "0.0.0.0:$PORT" --workers 2 --threads 2 --timeout 120 app.main:app
