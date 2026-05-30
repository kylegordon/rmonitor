"""Allow running the app package directly with ``python -m app``."""

from app.main import app, WEB_HOST, WEB_PORT, log, RMONITOR_HOST, RMONITOR_PORT

from aiohttp import web

log.info(
    "Starting rMonitor web display – feed=%s:%s  web=%s:%s",
    RMONITOR_HOST, RMONITOR_PORT, WEB_HOST, WEB_PORT,
)
web.run_app(app, host=WEB_HOST, port=WEB_PORT)
