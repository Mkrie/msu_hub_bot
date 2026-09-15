from msu_hub_bot.settings import settings
from msu_hub_bot.integrations import UnavailableClient

import asyncio

from dataclasses import dataclass

from api2ch import Api2chAsync

from common.applets import AppBase
from common.db.edb import EdgeDB
from common.externals.orfogrammka import Orfogrammka
from common.tg.exc_tracker import TelegramExceptionsTrackerAPI
from utils.jdoodle import ManyJDoodle
from utils.wit import Wit
from utils.wolfram import WolframAPI


@dataclass
class AppEdgeDB(AppBase):
    edgedb: EdgeDB = None

    def init(self):
        self.edgedb = EdgeDB(self.config.pg_user)
        return self

    async def on_shutdown(self):
        await self.edgedb.close()

    async def on_startup(self):
        await self.edgedb.client.query_single('SELECT 1')


@dataclass
class AppWolfram(AppBase):
    wolfram: WolframAPI = None

    def init(self):
        self.wolfram = WolframAPI(self.config.wolfram_token)
        return self

    async def on_shutdown(self):
        await self.wolfram.close()


@dataclass
class AppExcTracker(AppBase):
    exc_tracker: TelegramExceptionsTrackerAPI = None

    def init(self):
        self.exc_tracker = TelegramExceptionsTrackerAPI()
        return self

    async def on_startup(self):
        self.exc_tracker.patch_aiogram()
        await self.exc_tracker.start_loop()

    async def on_shutdown(self):
        await self.exc_tracker.close()


@dataclass
class AppWit(AppBase):
    wit: Wit = None

    def init(self):
        self.wit = Wit(self.config.wit_tokens)
        return self

    async def on_shutdown(self):
        await self.wit.close()


@dataclass
class AppJDoodle(AppBase):
    jdoodle: ManyJDoodle = None

    def init(self):
        self.jdoodle = ManyJDoodle(self.config.jdoodle_tokens)
        return self

    async def on_shutdown(self):
        await self.jdoodle.close()


@dataclass
class AppDvach(AppBase):
    dvach: Api2chAsync = None

    def init(self):
        self.dvach = Api2chAsync()
        return self

    async def on_shutdown(self):
        await self.dvach.close()


@dataclass
class AppOrfogrammka(AppBase):
    orfogrammka: Orfogrammka = None

    def init(self):
        self.orfogrammka = (Orfogrammka(settings.orfogrammka_email, settings.orfogrammka_password)
                           if settings.orfogrammka_email and settings.orfogrammka_password
                           else UnavailableClient('orfogrammka_email', 'orfogrammka_password'))
        return self

    async def on_startup(self):
        if isinstance(self.orfogrammka, UnavailableClient):
            return
        try:
            await asyncio.wait_for(self.orfogrammka.login(), timeout=15)
        except Exception:
            self.logger.warning('Orfogrammka login unavailable; it will retry when used')

    async def on_shutdown(self):
        await self.orfogrammka.close()
