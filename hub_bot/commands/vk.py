import traceback
from collections import defaultdict
from operator import attrgetter
from typing import List, Tuple, Dict

import aiohttp
from aiogram.types import Message
from aiogram.utils.markdown import hbold, hcode, hpre
from pendulum import UTC, now
from tabulate import tabulate

from app import bot, events_chat_id, logger, redis, vk_api, db
from db import VkWallPosting
from common.tg.storage import RedisStorage
from common.utils import cut_long_text, one_liner, clear_html
from common.vk.posts import VkPost
from common.vk.publish import publish_vk_post


async def post_to_fb(text, url):
    # Facebook posting was already disabled in production.
    return


async def process_list_vk_wall(message: Message):
    headers = ['owner_id', 'chat_id', 'post', 'reposts', 'header', 'susp', 'comment']

    def f(v: VkWallPosting) -> list:
        return [v.owner_id, v.chat_id, v.last_post_id, v.with_reposts, v.with_header, v.is_suspended, v.description]

    vwps = await VkWallPosting.query(db).get_all()
    text = tabulate([f(v) for v in vwps], headers=headers)

    for t in cut_long_text(text):
        await message.reply(hpre(t))

    return True


async def _push_posts(posts: List[VkPost], chat_id: int, vwps: Dict[Tuple[int, int], VkWallPosting], notify_chat_id: int):
    for post in posts:
        v = vwps[(post.owner_id, chat_id)]

        if not post.id > v.last_post_id:
            continue

        if not v.with_reposts and post.is_repost:
            continue

        try:
            await publish_vk_post(post, bot, v.chat_id, with_header=v.with_header)
            await VkWallPosting.query(db).update2('owner_id', v.owner_id, 'chat_id', v.chat_id, last_post_id=post.id)


        except Exception as e:
            text = f'☢️ ' \
                   f'{hbold("VK Exception")}: {hcode(e)}\n\n' \
                   f'{hbold("Traceback")}: {hcode(traceback.format_exc(limit=-2))}\n' \
                   f'{hbold("Post")}: {post.url}'
            await bot.send_message(notify_chat_id, text)


async def process_vk_wall(message: Message):
    args = one_liner(message.get_args()).split()
    if len(args) < 2:
        return await message.reply('Usage: ' + hcode('/vk_wall owner_id chat_id from_id with_reposts with_header suspended comment'))

    owner_id = int(args[0])
    chat_id = int(args[1])
    last_post_id = int(args[2]) if len(args) > 2 else None
    with_reposts = bool(int(args[3])) if len(args) > 3 else None
    with_header = bool(int(args[4])) if len(args) > 4 else None
    is_suspended = bool(int(args[5])) if len(args) > 5 else None
    description = args[6] if len(args) > 6 else None

    v = await VkWallPosting.query(db).upsert2('owner_id', 'chat_id',
                                              owner_id=owner_id,
                                              chat_id=chat_id,
                                              last_post_id=last_post_id,
                                              with_reposts=with_reposts,
                                              with_header=with_header,
                                              is_suspended=is_suspended,
                                              description=description)

    if is_suspended:
        return await message.reply(f'ℹ️ Выгрузка стены заморожена')

    posts = await VkPost.from_api_wall(vk_api, v.owner_id)
    await _push_posts(posts, chat_id, {(owner_id, chat_id): v}, message.chat.id)
    return await message.reply(f'ℹ️ Выгрузка стены окончена')


async def process_vk_post(message: Message):
    args = one_liner(message.get_args()).split()
    if len(args) < 2:
        return await message.reply('Usage: ' + hcode('/vk_post url chat_id with_header'))

    url = args[0]
    chat_id = int(args[1])
    with_header = bool(int(args[2])) if len(args) > 2 else True

    matches = VkPost.pattern_vk_post.findall(str(url))
    if not matches:
        return True

    posts = await VkPost.from_api_by_id(vk_api, matches[0])
    if not posts:
        return True

    await publish_vk_post(posts[0], bot, chat_id, None, with_header)
    return True


class LastCheckUpdater:
    def __init__(self, r: RedisStorage, name: str):
        self.r = r
        self.name = name
        self.curr_dt = now(UTC)

    async def __aenter__(self):
        return await self.r.get_dt(self.name, self.curr_dt.subtract(days=10))

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            await self.r.set_dt(self.name, self.curr_dt)


async def process_vk_wall_posting():
    vwps = {(v.owner_id, v.chat_id): v
            for v in await VkWallPosting.query(db).get_all()
            if not v.is_suspended}
    owner_ids = {v.owner_id for v in vwps.values()}
    if not owner_ids:
        return True

    async with LastCheckUpdater(redis, 'vk_newsfeed_last_check') as dt:
        posts = await VkPost.from_api_newsfeed(vk_api, owner_ids=owner_ids, from_ts=dt.int_timestamp)
        if not posts:
            return True

        posts_by_owner_id = defaultdict(list)

        for post in posts:
            posts_by_owner_id[post.owner_id].append(post)

        posts_by_chat_id = defaultdict(list)

        for owner_id, chat_id in vwps:
            if posts_by_owner_id.get(owner_id):
                posts_by_chat_id[chat_id] += posts_by_owner_id[owner_id]

        for p in posts_by_chat_id.values():
            p.sort(key=attrgetter('date'))

        logger.info(f'[VK] Extracted {len(posts)} post(s) from {len(posts_by_owner_id)} wall(s) to {len(posts_by_chat_id)} chat(s)')

        for chat_id, p in posts_by_chat_id.items():
            await _push_posts(p, chat_id, vwps, events_chat_id)

    return True
