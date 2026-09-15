import time

from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import Update

from common.mixins import LoggerMixin
from common.tg.utils import is_handled, parse_update


class LoggingMiddleware(BaseMiddleware, LoggerMixin, logger_name='Log'):
    @staticmethod
    def set_timeout(update: Update) -> None:
        update.conf['_start'] = time.monotonic()

    @staticmethod
    def get_timeout(update: Update) -> int:
        start = update.conf.get('_start', None)
        if start:
            return round((time.monotonic() - start) * 1000)
        return -1

    def update_log(self, update: Update):
        timeout = self.get_timeout(update)

        f, user, chat, info = parse_update(update)
        chat = f' | {chat}' if chat else ''
        user = f' | {user}' if user else ''
        timeout = f' [{timeout:>4} ms]'

        return f'{f.__class__.__name__}{timeout}{chat}{user} | {info}'

    async def on_pre_process_update(self, update: Update, _data: dict):
        self.set_timeout(update)

    async def on_post_process_update(self, update: Update, results, data: dict):
        log = self.update_log(update)
        if is_handled(results, data):
            self.logger.info(log)
        else:
            self.logger.debug(log)
