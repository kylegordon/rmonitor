FROM python:3.12-slim

WORKDIR /opt/app

COPY relay/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY relay/ relay/
# The release commit's VERSION is the release number, so the image can report it.
# A publish.yml re-publish must be dispatched from the release tag for this to match.
COPY VERSION .

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "relay"]
