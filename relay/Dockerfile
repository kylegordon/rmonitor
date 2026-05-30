FROM python:3.12-slim

WORKDIR /opt/app

COPY relay/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY relay/ relay/

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "relay"]
