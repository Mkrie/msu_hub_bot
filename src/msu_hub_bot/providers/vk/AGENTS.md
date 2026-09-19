# VK adapters

- Keep public-wall checks and copied-source reads in `api.py`/`posts.py`; Telegram and Mini App callers share this pipeline.
- Be strict about identity and privacy metadata. Optional display metadata must not hide an otherwise valid post; unsupported attachments retain a source link.
- `VkPost.body_text` supplies full raw bodies for filters and plain-text previews; `render()` supplies complete escaped text with anchors.
- Telegram chunking, preview budgets, media placement and destination policies belong in [telegram/vk.py](../../telegram/vk.py), using shared serialized delivery.
