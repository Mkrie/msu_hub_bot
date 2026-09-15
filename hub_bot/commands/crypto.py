import asyncio
from contextlib import suppress
from typing import Tuple, List

import aiogram
import ccxt.async_support as ccxt
import pendulum
from aiocache import cached
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.markdown import hbold, hitalic

from common.tg.callbacks import CallbackCommandBase
from common.tg.filters import MetaInfo


class Crypto(CallbackCommandBase):
    callback_data = CallbackData('crypto', 'ticker')
    exchange = ccxt.binance()
    tickers = ('BTC', 'ETH', 'XRP', 'BNB', 'DOGE')

    @classmethod
    def keyboard(cls, tickers: List[str]) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup()
        for ticker in tickers:
            keyboard.insert(
                InlineKeyboardButton(text=ticker, callback_data=cls.callback_data.new(ticker))
            )
        return keyboard

    @classmethod
    @cached(ttl=10, noself=True)
    async def text(cls, ticker: str) -> str:
        text = f'⚖️ {hbold(ticker)} with 24h and 7d diffs\n\n'

        async def prices(e: ccxt.Exchange, symbol: str) -> Tuple[int, int, int]:
            diff_1d = (pendulum.now('UTC') - pendulum.duration(days=1)).int_timestamp * 1000
            diff_7d = (pendulum.now('UTC') - pendulum.duration(days=7)).int_timestamp * 1000
            curr, *prev = await asyncio.gather(e.fetch_ohlcv(symbol, timeframe='1m', limit=1),
                                               e.fetch_ohlcv(symbol, timeframe='1m', limit=1, since=diff_1d),
                                               e.fetch_ohlcv(symbol, timeframe='1m', limit=1, since=diff_7d))
            return curr[0][4], prev[0][0][4], prev[1][0][4]

        def line(p: Tuple[int, int, int], symbol: str) -> str:
            curr_p, *prev_ps = p
            percent_1 = hbold(f'{abs(1. - (curr_p / prev_ps[0])) * 100:.3f}')
            change_1 = f'📈 +{percent_1}%' if curr_p >= prev_ps[0] else f'📉 -{percent_1}%'
            percent_2 = hbold(f'{abs(1. - (curr_p / prev_ps[1])) * 100:.3f}')
            change_2 = f'📈 +{percent_2}%' if curr_p >= prev_ps[1] else f'📉 -{percent_2}%'
            return f'— {symbol} {hbold(f"{curr_p:.8f}".rstrip("0"))} | {change_1} | {change_2}\n'

        if ticker == 'BTC':
            usd = await prices(cls.exchange, f'{ticker}/USDT')
            text += line(usd, '$')
        else:
            try:
                usd, btc = await asyncio.gather(prices(cls.exchange, f'{ticker}/USDT'),
                                                prices(cls.exchange, f'{ticker}/BTC'))
                text += line(usd, '$')
                text += line(btc, '₿')
            except ccxt.BadSymbol:
                usd = await prices(cls.exchange, f'{ticker}/USDT')
                text += line(usd, '$')

        return text

    @classmethod
    async def check_tickers(cls, tickers: List[str]) -> List[str]:
        markets = await cls.exchange.load_markets()
        symbols = {f'{t}/USDT': True for t in tickers}
        return [s.partition('/')[0] for s in symbols if markets.get(s, {}).get('active', False)][:42]

    @classmethod
    async def process(cls, message: Message, meta: MetaInfo):
        command = message.get_command(pure=True)
        target, text = meta.extract_text()
        tickers = text and [t.upper() for t in text.split()] or [command.upper()]
        if tickers == ['CRYPTO']:
            tickers = cls.tickers

        reply = await message.reply(hitalic('🔄 Updating tickers...'))

        checked_tickers = await cls.check_tickers(tickers)
        if not checked_tickers:
            return await reply.edit_text(f'🤷🏻‍♂️ Unable to find ticker(s): {", ".join(hbold(t) for t in tickers)}')

        text = await cls.text(checked_tickers[0])
        return await reply.edit_text(text, reply_markup=cls.keyboard(checked_tickers))

    @classmethod
    async def process_cb(cls, query: CallbackQuery, callback_data: dict):
        await query.answer('✅ Updating', cache_time=5)

        tickers = [button.text for line in query.message.reply_markup.inline_keyboard for button in line]

        text = await cls.text(callback_data['ticker'])
        if query.message.html_text != text:
            with suppress(aiogram.exceptions.BadRequest):
                return await query.message.edit_text(text, reply_markup=cls.keyboard(tickers))

        return True
