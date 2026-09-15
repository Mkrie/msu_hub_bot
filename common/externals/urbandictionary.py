from typing import List

import aiohttp
import bs4

from common import json
from common.externals.exceptions import BadRequestError, NotFoundError


async def urban_dictionary(query: str = None) -> List[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:95.0) Gecko/20100101 Firefox/95.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "referrer": "https://www.urbandictionary.com/",
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        url = 'https://www.urbandictionary.com/random.php'
        if query:
            url = f'https://www.urbandictionary.com/define.php?term={query}'
        async with session.get(url, headers=headers) as response:
            if response.status == 404:
                raise NotFoundError()
            if response.status != 200:
                raise BadRequestError()
            html = await response.text()

        soup = bs4.BeautifulSoup(html, features='html.parser')

        defs = {}
        for definition in soup.find_all('div', {'class': 'definition'}):
            defs[definition.find('a', {'class': 'word'}).attrs['id']] = {
                'header': definition.find('a', {'class': 'word'}).text.strip(),
                'meaning': definition.find('div', {'class': 'meaning'}).text.replace('\r', '\n').strip(),
                'example': definition.find('div', {'class': 'example'}).text.replace('\r', '\n').strip(),
            }

        url = 'https://api.urbandictionary.com/v0/uncacheable?ids=' + ','.join(defs)
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                raise BadRequestError()
            reactions = json.loads(await response.read())

        for r in reactions['thumbs']:
            defs[str(r['defid'])].update({
                'up': r['up'],
                'down': r['down'],
            })

        return list(defs.values())
