from __future__ import annotations

import logging
import time

from bot import main as bot_main
from runtime_preflight import FatalStartupError, PreflightIntegrityError
from signer.errors import SignerConfigurationError


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger("run_bot")


def _is_fatal_startup_error(exc: Exception) -> bool:
    return isinstance(exc, (PreflightIntegrityError, SignerConfigurationError, FatalStartupError))


def main() -> None:
    LOGGER.info("starting telegram bot service")
    startup_complete = False
    while True:
        try:
            bot_main()
            LOGGER.info("telegram bot service stopped cleanly")
            return
        except KeyboardInterrupt:
            LOGGER.info("bot interrupted; shutting down")
            return
        except SystemExit:
            LOGGER.info("bot received system exit; shutting down")
            return
        except Exception as exc:
            if not startup_complete and _is_fatal_startup_error(exc):
                # WARNING: Fatal startup integrity/configuration errors intentionally stop restart loops to fail closed.
                LOGGER.error("fatal startup error; refusing restart loop: %s", exc)
                raise
            startup_complete = True
            LOGGER.exception("bot crashed; restarting in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
