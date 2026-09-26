FROM python:3.12-slim

WORKDIR /opt/app

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ server/
# The release commit's VERSION is the release number, so the image can report it.
# A publish.yml re-publish must be dispatched from the release tag for this to match.
COPY VERSION .

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1

CMD ["python", "-m", "server"]
