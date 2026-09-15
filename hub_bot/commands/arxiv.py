import random
from textwrap import shorten

import arxiv
from aiogram.types import Message
from aiogram.utils.markdown import hbold, hcode, hitalic, hlink, quote_html

from app import cpu_executor
from common.utils import one_liner


def arxiv_random():
    return arxiv.query(query='all:a', start=random.randint(0, 10_000), sort_by='lastUpdatedDate', max_results=5)


def arxiv_search(query):
    return arxiv.query(query=query, max_results=5)


async def process_arxiv(message: Message):
    help_link = hlink('q', 'https://arxiv.org/help/api/user-manual#Appendices')

    def paper_info(paper: dict) -> str:
        url = paper['arxiv_url']
        title = quote_html(one_liner(paper['title']))

        authors = ''
        for count, author in enumerate(paper['authors']):
            if count > 1:
                authors += ' et al.'
                break
            authors += (', ' if authors else '') + quote_html(one_liner(author))

        summary = quote_html(shorten(one_liner(paper['summary']), width=300))
        pdf = ''
        if 'pdf_url' in paper:
            pdf = ' (' + hlink('.pdf', paper['pdf_url']) + ')'
        return f'• {hbold(authors)}, {hlink(title, url)}{pdf}: {hitalic(summary)}'

    if query := message.get_args():
        header = hbold('📑 Search results') + f' ({help_link}):\n\n'
        search, timeouted = await cpu_executor.run(arxiv_search, query)
    else:
        header = hbold('🗞 Random papers') + ':\n\n'
        search, timeouted = await cpu_executor.run(arxiv_random)

    if timeouted:
        return await message.reply(hcode('🤷🏻‍♂️ Timeout'))
    if not search:
        return await message.reply(hcode('🤷🏻‍♂️ По запросу ничего не найдено') + f' ({help_link})')

    result = header + '\n\n'.join(paper_info(paper) for paper in search)
    return await message.reply(result)
