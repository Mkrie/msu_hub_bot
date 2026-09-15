import logging
import os
from pathlib import Path

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration, ignore_logger

from msu_hub_bot.redaction import RedactingFormatter, before_send


class LoggerBuilder:
    default_filename: str = None
    default_sentry_url: str = None

    @classmethod
    def set_defaults(cls, filename: str = None, sentry_url: str = None):
        if filename:
            cls.default_filename = filename

        if sentry_url:
            cls.default_sentry_url = sentry_url

    @staticmethod
    def init_sentry(sentry_url):
        sentry_logging = LoggingIntegration(
            # Breadcrumbs level
            level=logging.INFO,
            # Events level
            event_level=logging.INFO
        )
        ignore_logger('kafka.conn')
        sentry_sdk.init(dsn=sentry_url, integrations=[sentry_logging], before_send=before_send,
                        before_breadcrumb=before_send, include_local_variables=False, send_default_pii=False)

    @classmethod
    def get_logger(cls, component_name: str, level=None, filename=None, sentry_url=None) -> logging.Logger:
        sentry_url = sentry_url or cls.default_sentry_url
        if sentry_url:
            cls.init_sentry(sentry_url)

        logger = logging.Logger(component_name)
        logger.setLevel(logging.DEBUG)
        formatter = RedactingFormatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

        if level is None:
            level = os.getenv('LOG_LEVEL', logging.DEBUG)

        handler_console = logging.StreamHandler()
        handler_console.setFormatter(formatter)
        handler_console.setLevel(level)
        logger.addHandler(handler_console)

        filename = filename or cls.default_filename
        if filename:
            Path(filename.format(name=component_name)).parent.mkdir(parents=True, exist_ok=True)
            handler_file = logging.FileHandler(filename.format(name=component_name), encoding='utf-8')
            handler_file.setFormatter(formatter)
            handler_file.setLevel(logging.WARNING)
            logger.addHandler(handler_file)

        return logger
