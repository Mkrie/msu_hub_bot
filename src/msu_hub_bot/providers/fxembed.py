"""Public FxEmbed v2 posts, with explicit source and media URL boundaries."""

import asyncio
import ipaddress
import json
import re
from typing import Annotated, Literal, TypeVar
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator, model_validator

from msu_hub_bot.providers.exceptions import BadRequestError, NotFoundError
from msu_hub_bot.providers.http import USER_AGENT, read_limited

API_ROOT = "https://api.fxtwitter.com/2"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 20
_POST_PATH = re.compile(
    r"/(?:[A-Za-z0-9_]{1,15}|i/web)/status/(?P<id>[1-9][0-9]{0,19})(?:/(?P<kind>photo|video)/(?P<index>[1-9][0-9]?))?/?$"
)
_ORIGINAL_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "m.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "m.twitter.com"}
_FIXED_HOSTS = {
    f"{prefix}{domain}"
    for domain in ("fixupx.com", "fxtwitter.com", "twittpr.com", "xfixup.com")
    for prefix in ("", "www.", "i.", "d.", "g.", "t.")
}
_MEDIA_HOSTS = {"pbs.twimg.com", "video.twimg.com"}
_SNOWFLAKE = re.compile(r"[1-9][0-9]{0,19}$")
ModelT = TypeVar("ModelT", bound=BaseModel)
Snowflake = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]


def safe_public_url(value: object) -> str | None:
    """Allow public HTTP links for display; this is not a download authorization."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 4096
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
        or "\\" in value
    ):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
            return None
        if (
            parsed.port not in {None, 80, 443}
            or host == "localhost"
            or "." not in host
            or host.endswith((".localhost", ".local", ".internal"))
        ):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
    except ValueError:
        return None
    return value


def safe_media_url(value: object) -> str | None:
    """Only X's image/video CDN may supply downloadable native attachments."""
    url = safe_public_url(value)
    if url is None:
        return None
    parsed = urlsplit(url)
    return url if parsed.scheme == "https" and parsed.hostname in _MEDIA_HOSTS and parsed.port in {None, 443} else None


class FxModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, allow_inf_nan=False, hide_input_in_errors=True)


class PostLink(FxModel):
    id: Snowflake
    url: str
    media_kind: Literal["photo", "video"] | None = None
    media_index: Annotated[int, Field(strict=True, ge=1, le=99)] | None = None


def _parse_post_url(value: str, hosts: set[str]) -> PostLink | None:
    url = safe_public_url(value)
    if url is None:
        return None
    parsed = urlsplit(url)
    if parsed.hostname not in hosts or parsed.port is not None:
        return None
    match = _POST_PATH.fullmatch(parsed.path)
    if match is None:
        return None
    selector = f"/{match['kind']}/{match['index']}" if match["kind"] else ""
    return PostLink.model_validate(
        {
            "id": match["id"],
            "url": f"https://x.com/i/status/{match['id']}{selector}",
            "media_kind": match["kind"],
            "media_index": int(match["index"]) if match["index"] else None,
        }
    )


def parse_post_url(value: str) -> PostLink | None:
    return _parse_post_url(value, _ORIGINAL_HOSTS)


def is_x_url(value: str) -> bool:
    """Claim X and FxEmbed links before generic automatic video extraction."""
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and parsed.hostname in _ORIGINAL_HOSTS | _FIXED_HOSTS
    except ValueError:
        return False


def _optional(model: type[ModelT], value: object) -> ModelT | None:
    try:
        return model.model_validate(value) if value is not None else None
    except ValidationError, RecursionError:
        return None


def _items(model: type[ModelT], value: object) -> list[ModelT]:
    if not isinstance(value, list):
        return []
    return [item for raw in value if (item := _optional(model, raw)) is not None]


class FxFacet(FxModel):
    type: str
    indices: tuple[Annotated[int, Field(strict=True, ge=0)], Annotated[int, Field(strict=True, ge=0)]]
    original: str | None = None
    replacement: str | None = None
    display: str | None = None
    id: str | None = None


class FxText(FxModel):
    """Complete text with validated UTF-16 facets, independent of upstream offset units."""

    text: str
    facets: list[FxFacet] = Field(default_factory=list)
    display_text_range: tuple[int, int] | None = None

    @field_validator("facets", mode="before")
    @classmethod
    def valid_facets(cls, value: object) -> list[FxFacet]:
        return _items(FxFacet, value)

    @field_validator("display_text_range", mode="before")
    @classmethod
    def valid_display_range(cls, value: object) -> tuple[int, int] | None:
        if isinstance(value, (tuple, list)) and len(value) == 2 and all(type(item) is int and item >= 0 for item in value):
            return value[0], value[1]
        return None

    @model_validator(mode="after")
    def valid_offsets(self) -> "FxText":
        boundaries = {0}
        offset = 0
        for char in self.text:
            offset += 2 if ord(char) > 0xFFFF else 1
            boundaries.add(offset)
        self.facets = [facet for facet in self.facets if facet.indices[0] < facet.indices[1] and set(facet.indices) <= boundaries]
        if self.display_text_range and (
            self.display_text_range[0] > self.display_text_range[1] or not set(self.display_text_range) <= boundaries
        ):
            self.display_text_range = None
        return self


def _note_text(value: object) -> object:
    """X Note Tweet facets use code points; its display range already uses UTF-16."""
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        return value
    text = value["text"]
    facets = [facet for facet in _items(FxFacet, value.get("facets")) if facet.indices[0] < facet.indices[1] <= len(text)]
    requested = {position for facet in facets for position in facet.indices}
    offsets = {0: 0}
    offset = 0
    for index, char in enumerate(text, 1):
        offset += 2 if ord(char) > 0xFFFF else 1
        if index in requested:
            offsets[index] = offset
    return {
        **value,
        "facets": [facet.model_copy(update={"indices": (offsets[facet.indices[0]], offsets[facet.indices[1]])}) for facet in facets],
    }


class FxAuthor(FxModel):
    name: str
    screen_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{1,15}$")]
    protected: StrictBool = False

    @property
    def url(self) -> str:
        return f"https://x.com/{self.screen_name}"


class FxMediaFormat(FxModel):
    url: str
    container: str | None = None
    codec: str | None = None
    bitrate: float | None = None
    size: Annotated[int, Field(strict=True, ge=0)] | None = None
    width: Annotated[int, Field(strict=True, ge=0)] | None = None
    height: Annotated[int, Field(strict=True, ge=0)] | None = None

    @field_validator("url")
    @classmethod
    def check_url(cls, value: str) -> str:
        if (url := safe_media_url(value)) is None:
            raise ValueError("unsupported media source")
        return url


class FxMedia(FxModel):
    id: str | None = None
    source_index: int = 0
    type: Literal["photo", "video", "gif"]
    url: str
    width: int = 0
    height: int = 0
    duration: float | None = None
    thumbnail_url: str | None = None
    alt_text: str | None = Field(default=None, alias="altText")
    filesize: int | None = None
    formats: list[FxMediaFormat] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def check_url(cls, value: str) -> str:
        if (url := safe_media_url(value)) is None:
            raise ValueError("unsupported media source")
        return url

    @field_validator("thumbnail_url", mode="before")
    @classmethod
    def check_thumbnail(cls, value: object) -> str | None:
        return safe_media_url(value)

    @field_validator("width", "height", mode="before")
    @classmethod
    def optional_dimension(cls, value: object) -> int:
        return value if type(value) is int and 0 < value < 100000 else 0

    @field_validator("filesize", mode="before")
    @classmethod
    def optional_size(cls, value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None

    @field_validator("duration", mode="before")
    @classmethod
    def optional_duration(cls, value: object) -> float | None:
        return float(value) if isinstance(value, (float, int)) and not isinstance(value, bool) and 0 <= value < 1e9 else None

    @field_validator("id", "alt_text", mode="before")
    @classmethod
    def optional_string(cls, value: object) -> str | None:
        return value if isinstance(value, str) else None

    @field_validator("formats", mode="before")
    @classmethod
    def valid_formats(cls, value: object) -> list[FxMediaFormat]:
        return _items(FxMediaFormat, value)


class FxMediaContainer(FxModel):
    all: list[FxMedia] = Field(default_factory=list)
    unsupported_count: int = 0
    external_url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def ordered_media(cls, value: object) -> object:
        if not isinstance(value, dict):
            return {}
        # `all` is authoritative, including when empty: regrouping photos/videos loses order.
        raw = value.get("all")
        if not isinstance(raw, list):
            raw = (
                [*(value.get("photos") or []), *(value.get("videos") or [])]
                if all(isinstance(value.get(k, []), list) for k in ("photos", "videos"))
                else []
            )
        media = []
        for index, entry in enumerate(raw, 1):
            item = _optional(FxMedia, entry)
            if item is not None:
                item.source_index = entry.source_index or index if isinstance(entry, FxMedia) else index
                media.append(item)
        external = value.get("external")
        external_url = safe_public_url(external.get("url")) if isinstance(external, dict) else safe_public_url(value.get("external_url"))
        previous_count = value.get("unsupported_count", 0)
        previous_count = previous_count if type(previous_count) is int and previous_count >= 0 else 0
        return {"all": media, "unsupported_count": previous_count + len(raw) - len(media) + bool(external), "external_url": external_url}


class FxPollChoice(FxModel):
    label: str
    count: Annotated[int, Field(strict=True, ge=0)]
    percentage: Annotated[float, Field(ge=0, le=100)]


class FxPoll(FxModel):
    choices: Annotated[list[FxPollChoice], Field(min_length=1)]
    total_votes: Annotated[int, Field(strict=True, ge=0)]
    ends_at: str = ""
    time_left_en: str = ""

    @field_validator("ends_at", "time_left_en", mode="before")
    @classmethod
    def optional_string(cls, value: object) -> str:
        return value if isinstance(value, str) else ""


class FxReply(FxModel):
    screen_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{1,15}$")]
    status: Snowflake

    @property
    def url(self) -> str:
        return f"https://x.com/{self.screen_name}/status/{self.status}"


class FxStyleRange(FxModel):
    offset: Annotated[int, Field(strict=True, ge=0)]
    length: Annotated[int, Field(strict=True, ge=0)]
    style: str


class FxEntityRange(FxModel):
    offset: Annotated[int, Field(strict=True, ge=0)]
    length: Annotated[int, Field(strict=True, ge=0)]
    key: StrictInt


class FxArticleBlock(FxModel):
    key: str = ""
    type: str = "unstyled"
    text: str
    styles: list[FxStyleRange] = Field(default_factory=list, alias="inlineStyleRanges")
    entities: list[FxEntityRange] = Field(default_factory=list, alias="entityRanges")
    facets: list[FxFacet] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def inline_links(cls, value: object) -> object:
        if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
            return value
        facets = []
        for key, kind, prefix in (
            ("mentions", "mention", "https://x.com/"),
            ("urls", "url", ""),
            ("hashtags", "hashtag", "https://x.com/hashtag/"),
            ("cashtags", "cashtag", "https://x.com/search?q=%24"),
        ):
            records = value["data"].get(key)
            if not isinstance(records, list):
                continue
            for record in records:
                if isinstance(record, dict) and isinstance(record.get("text"), str):
                    facets.append(
                        {"type": kind, "indices": [record.get("fromIndex"), record.get("toIndex")], "replacement": prefix + record["text"]}
                    )
        return {**value, "facets": facets}

    @field_validator("styles", mode="before")
    @classmethod
    def valid_styles(cls, value: object) -> list[FxStyleRange]:
        return _items(FxStyleRange, value)

    @field_validator("entities", mode="before")
    @classmethod
    def valid_entities(cls, value: object) -> list[FxEntityRange]:
        return _items(FxEntityRange, value)

    @field_validator("facets", mode="before")
    @classmethod
    def valid_facets(cls, value: object) -> list[FxFacet]:
        return _items(FxFacet, value)


class FxArticleEntity(FxModel):
    key: str
    kind: Literal["MARKDOWN", "MEDIA", "TWEET", "UNKNOWN"] = "UNKNOWN"
    markdown: str = ""
    media_ids: list[str] = Field(default_factory=list)
    tweet_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def unpack_entity(cls, value: object) -> object:
        if not isinstance(value, dict) or not isinstance(value.get("value"), dict):
            return value
        entity = value["value"]
        data = entity.get("data")
        data = data if isinstance(data, dict) else {}
        media = data.get("mediaItems")
        kind = entity.get("type")
        return {
            "key": value.get("key"),
            "kind": kind if isinstance(kind, str) and kind in {"MARKDOWN", "MEDIA", "TWEET"} else "UNKNOWN",
            "markdown": data.get("markdown") if isinstance(data.get("markdown"), str) else "",
            "media_ids": [entry["mediaId"] for entry in media if isinstance(entry, dict) and isinstance(entry.get("mediaId"), str)]
            if isinstance(media, list)
            else [],
            "tweet_id": data.get("tweetId") if isinstance(data.get("tweetId"), str) and _SNOWFLAKE.fullmatch(data["tweetId"]) else None,
        }


class FxArticleContent(FxModel):
    blocks: list[FxArticleBlock] = Field(default_factory=list)
    entity_map: list[FxArticleEntity] = Field(default_factory=list, alias="entityMap")
    incomplete: bool = False

    @model_validator(mode="before")
    @classmethod
    def valid_blocks(cls, value: object) -> object:
        if not isinstance(value, dict):
            return {"incomplete": True}
        raw = value.get("blocks")
        blocks = _items(FxArticleBlock, raw)
        entities = _items(FxArticleEntity, value.get("entityMap", value.get("entity_map", [])))
        return {
            "blocks": blocks,
            "entity_map": entities,
            "incomplete": value.get("incomplete") is True or not isinstance(raw, list) or len(blocks) != len(raw),
        }


def _article_media(value: object) -> FxMedia | None:
    if isinstance(value, FxMedia):
        return value
    if not isinstance(value, dict) or not isinstance(value.get("media_info"), dict):
        return None
    info = value["media_info"]
    media_id = value.get("media_id") or value.get("id")
    if info.get("__typename") == "ApiImage":
        return _optional(
            FxMedia,
            {
                "id": media_id,
                "type": "photo",
                "url": info.get("original_img_url"),
                "width": info.get("original_img_width"),
                "height": info.get("original_img_height"),
            },
        )
    if info.get("__typename") not in {"ApiVideo", "ApiGif"}:
        return None
    video = info.get("video_info")
    if not isinstance(video, dict) or not isinstance(video.get("variants"), list):
        return None
    variants = [
        variant
        for variant in video["variants"]
        if isinstance(variant, dict) and variant.get("content_type") == "video/mp4" and safe_media_url(variant.get("url"))
    ]
    if not variants:
        return None

    def bitrate(variant: dict[str, object]) -> int:
        value = variant.get("bitrate")
        return value if type(value) is int else 0

    best = max(variants, key=bitrate)
    original = info.get("original_info")
    original = original if isinstance(original, dict) else {}
    millis = video.get("duration_millis")
    return _optional(
        FxMedia,
        {
            "id": media_id,
            "type": "gif" if info.get("__typename") == "ApiGif" else "video",
            "url": best["url"],
            "width": original.get("width"),
            "height": original.get("height"),
            "duration": millis / 1000 if isinstance(millis, (int, float)) else None,
            "thumbnail_url": info.get("media_url_https"),
            "formats": [{"url": variant["url"], "container": "mp4", "bitrate": variant.get("bitrate")} for variant in variants],
        },
    )


class FxArticle(FxModel):
    title: str = ""
    preview_text: str = ""
    content: FxArticleContent = Field(default_factory=FxArticleContent)
    media_entities: list[FxMedia] = Field(default_factory=list)
    cover_media: FxMedia | None = None

    @field_validator("title", "preview_text", mode="before")
    @classmethod
    def optional_string(cls, value: object) -> str:
        return value if isinstance(value, str) else ""

    @field_validator("content", mode="before")
    @classmethod
    def valid_content(cls, value: object) -> FxArticleContent:
        return FxArticleContent.model_validate(value)

    @field_validator("media_entities", mode="before")
    @classmethod
    def valid_media(cls, value: object) -> list[FxMedia]:
        return [media for raw in value if (media := _article_media(raw)) is not None] if isinstance(value, list) else []

    @field_validator("cover_media", mode="before")
    @classmethod
    def valid_cover(cls, value: object) -> FxMedia | None:
        return _article_media(value)

    @property
    def has_full_content(self) -> bool:
        return bool(self.content.blocks) and not self.content.incomplete


class FxTombstone(FxModel):
    type: Literal["tombstone"] = "tombstone"
    reason: Literal["deleted", "suspended", "private", "blocked", "unavailable"] = "unavailable"
    id: Snowflake | None = None
    url: str | None = None

    @field_validator("url", mode="before")
    @classmethod
    def valid_url(cls, value: object) -> str | None:
        link = parse_post_url(value) if isinstance(value, str) else None
        return link.url if link else None


class FxPost(FxModel):
    type: Literal["status"] = "status"
    provider: Literal["twitter"] = "twitter"
    id: Snowflake
    text: str
    raw_text: FxText
    author: FxAuthor
    media: FxMediaContainer = Field(default_factory=FxMediaContainer)
    quote: "FxPost | FxTombstone | None" = None
    poll: FxPoll | None = None
    community_note: FxText | None = None
    article: FxArticle | None = None
    possibly_sensitive: StrictBool = False
    replying_to: FxReply | None = None

    @property
    def url(self) -> str:
        return f"https://x.com/{self.author.screen_name}/status/{self.id}"

    @model_validator(mode="before")
    @classmethod
    def body_fallback(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        source = value.get("raw_text")
        if value.get("is_note_tweet") is True:
            source = _note_text(source)
        raw = _optional(FxText, source)
        if raw is None and isinstance(value.get("text"), str):
            raw = FxText(text=value["text"])
        return {**value, "raw_text": raw}

    @model_validator(mode="after")
    def public_author(self) -> "FxPost":
        if self.author.protected:
            raise ValueError("private post")
        return self

    @field_validator("poll", mode="before")
    @classmethod
    def valid_poll(cls, value: object) -> FxPoll | None:
        return _optional(FxPoll, value)

    @field_validator("community_note", mode="before")
    @classmethod
    def valid_note(cls, value: object) -> FxText | None:
        return _optional(FxText, value)

    @field_validator("article", mode="before")
    @classmethod
    def valid_article(cls, value: object) -> FxArticle | None:
        return _optional(FxArticle, value)

    @field_validator("replying_to", mode="before")
    @classmethod
    def valid_reply(cls, value: object) -> FxReply | None:
        return _optional(FxReply, value)

    @field_validator("quote", mode="before")
    @classmethod
    def valid_quote(cls, value: object) -> "FxPost | FxTombstone | None":
        if value is None or isinstance(value, (FxPost, FxTombstone)):
            return value
        if isinstance(value, dict):
            model = _optional(FxPost, value) if value.get("type") == "status" else _optional(FxTombstone, value)
            if model is not None:
                return model
            link = parse_post_url(value["url"]) if isinstance(value.get("url"), str) else None
            post_id = value.get("id")
            return FxTombstone(
                id=post_id if isinstance(post_id, str) and _SNOWFLAKE.fullmatch(post_id) else None, url=link.url if link else None
            )
        return FxTombstone()


class FxEmbed:
    """One bounded, credential-free request; no background sessions or retries."""

    async def get_post(self, link: PostLink) -> FxPost:
        try:
            async with (
                asyncio.timeout(REQUEST_TIMEOUT),
                aiohttp.ClientSession(
                    headers={"User-Agent": f"{USER_AGENT} FxEmbed-native-preview", "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, connect=5),
                    trust_env=False,
                ) as session,
            ):
                async with session.get(f"{API_ROOT}/status/{link.id}", allow_redirects=False) as response:
                    if response.status in {401, 403, 404}:
                        raise NotFoundError()
                    raw = await read_limited(response, MAX_RESPONSE_BYTES)
                    payload = json.loads(raw)
                if not isinstance(payload, dict) or type(payload.get("code")) is not int:
                    raise BadRequestError()
                if payload["code"] in {401, 403, 404}:
                    raise NotFoundError()
                status = payload.get("status")
                if (
                    payload["code"] != 200
                    or not isinstance(status, dict)
                    or status.get("type") != "status"
                    or status.get("provider") != "twitter"
                    or status.get("id") != link.id
                ):
                    raise BadRequestError()
                return FxPost.model_validate(status)
        except aiohttp.ClientError, TimeoutError, ValueError, RecursionError:
            raise BadRequestError() from None
