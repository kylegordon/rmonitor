FROM python:3.12-slim

WORKDIR /opt/rmonitor

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

EXPOSE 8080

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "app.main"]
