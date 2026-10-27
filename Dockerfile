FROM python:3.12-slim@sha256:5b8b1421ccb6a525860a77863c049eab5847076cda34e030c75a80c58faf3f3c

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    STUDYNAV_MODE=offline
WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --gid 10001 study \
    && useradd --create-home --uid 10001 --gid 10001 study

COPY studynavigator/ ./studynavigator/
COPY web/ ./web/
COPY data/ ./data/
COPY *.pdf ./
RUN mkdir -p /app/var && chown study:study /app/var

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "studynavigator"]
