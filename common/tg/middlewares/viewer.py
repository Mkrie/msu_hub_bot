from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import Message, MessageEntityType, MediaGroup, InputFile
from yarl import URL

from common.externals.exceptions import ExternalServiceError
from common.externals.instagram import InstagramViewer
from common.externals.tiktok import tiktok_text_with_preview
from common.externals.topdf import convert_to_pdf
from common.externals.ydl import YDL
from common.tg.utils import download, extract_urls
from common.tg.wrapper import BotWrapper
from common.utils import valid_filename, megabytes
from common.vk.api import VkApi
from common.vk.posts import VkPost
from common.vk.publish import publish_vk_post


class ViewerMiddleware(BaseMiddleware):
    def __init__(self, bot: BotWrapper, vk_api: VkApi, executor=None):
        super().__init__()

        self.bot = bot
        self.vk_api = vk_api

        self.executor = executor

    async def handle_vk_posts(self, message: Message, url: URL):
        matches = VkPost.pattern_vk_post.findall(str(url))[:2]
        paths = ','.join(set(matches))
        if not paths:
            return

        posts = await VkPost.from_api_by_id(self.vk_api, paths)
        for post in posts:
            await publish_vk_post(post, self.bot, message.chat.id, message.message_id)

    @staticmethod
    async def handle_instagram(message: Message, url: URL):
        result = await InstagramViewer.links(url)
        if result is None:
            return
        result, prefix = result
        media_photo, media_video = MediaGroup(), MediaGroup()
        for link, caption in result:
            *_, ext = URL(link).name.rpartition('.')
            filename = valid_filename(f'{prefix}.{ext}')
            if ext != 'mp4':
                media_photo.attach_document(InputFile.from_url(link, filename=filename), caption=caption)
            else:
                media_video.attach_video(InputFile.from_url(link, filename=filename), caption=caption)
        if media_photo.media:
            await message.reply_media_group(media_photo)
        if media_video.media:
            await message.reply_media_group(media_video)

    async def handle_video(self, message: Message, url: URL):
        result, timeouted = await self.executor.run(YDL.text_with_preview, str(url), timeout=60)
        if timeouted:
            return
        if result is None:
            return

        text, preview = result
        if preview:
            url, width, height = preview
            return await message.reply_video(InputFile.from_url(url, 'video.mp4'), caption=text, width=width, height=height)
        return await message.reply(text, disable_web_page_preview=not preview)

    @staticmethod
    async def handle_tiktok(message: Message, url: URL):
        result = await tiktok_text_with_preview(str(url))
        if not result:
            return
        text, preview = result
        return await message.reply_video(InputFile.from_url(preview, 'video.mp4'), caption=text)

    async def on_post_process_message(self, message: Message, _results, data: dict):
        settings = data['settings']

        for url, t in extract_urls(message)[:2]:
            if t == MessageEntityType.URL:
                await self.handle_vk_posts(message, url)

            if url.host:
                if url.host.endswith('instagram.com'):
                    await self.handle_instagram(message, url)
                elif url.host.endswith('tiktok.com'):
                    if settings.auto_video_links:
                        await self.handle_tiktok(message, url)
                else:
                    if settings.auto_video_links:
                        await self.handle_video(message, url)

        if dest := message.document:
            convert = tuple(f'.{ext}' for ext in ('azw', 'azw3', 'azw4', 'cbr', 'cbz', 'cgm', 'chm', 'djv', 'djvu',
                                                  'doc', 'docx', 'epub', 'fb2', 'lit', 'lrf', 'mobi', 'odg', 'odm',
                                                  'odp', 'ppt', 'pptx', 'rb', 'sda', 'sdc', 'sdd', 'sdp', 'sdw',
                                                  'uof', 'uop', 'uos', 'wks', 'wmf', 'wpd', 'wps', 'xbm', 'xps',))
            if dest.file_name and dest.file_name.endswith(convert) and dest.file_size < megabytes(20):
                try:
                    file = await download(dest)
                    url, thumb, convert_name = await convert_to_pdf(file, dest.file_name, dest.mime_type)
                except ExternalServiceError:
                    pass
                else:
                    await message.reply_document(InputFile.from_url(url, convert_name), thumb=InputFile.from_url(thumb))
