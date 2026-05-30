import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from relay.main import main  # noqa: E402 – after basicConfig

asyncio.run(main())
