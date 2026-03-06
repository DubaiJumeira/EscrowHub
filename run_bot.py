from __future__ import annotations

import logging
import time

from bot import main as bot_main


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("run_bot")


def main() -> None:
    LOGGER.info("starting telegram bot service")
    while True:
        try:
            bot_main()
            LOGGER.warning("bot exited unexpectedly; restarting in 5s")
            time.sleep(5)
        except KeyboardInterrupt:
            LOGGER.info("bot interrupted; shutting down")
            raise
        except Exception:
            LOGGER.exception("bot crashed; restarting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
