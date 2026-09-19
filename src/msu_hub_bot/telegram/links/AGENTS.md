# Link previews

- `service.py` owns URL dispatch and automatic eligibility; `native.py` presents downloaded YouTube/Instagram/TikTok posts. Keep provider protocols in `providers/`.
- `rich.py` owns shared Telegram limits, splitting and delivery. Preserve full text, source order, actual media geometry, reply/topic context and the author's message.
- A recognized site's unavailable route stays quiet; never fall through to another extractor or retry an ambiguous send. Ordinary yt-dlp sites keep standard emojis.
- VK's custom logo applies only to parsed links. Its default publisher is also used for reposts; retain the explicit distinction.
