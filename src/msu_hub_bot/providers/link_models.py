"""Downloaded public media ready for Telegram, independent of provider payloads."""

from dataclasses import dataclass
from typing import Literal

type LinkSite = Literal["youtube", "instagram", "tiktok"]


@dataclass(frozen=True, slots=True)
class LinkAsset:
    kind: Literal["photo", "video"]
    data: bytes
    width: int
    height: int
    duration: float | None = None


@dataclass(frozen=True, slots=True)
class LinkPost:
    site: LinkSite
    url: str
    author: str
    username: str | None = None
    author_url: str | None = None
    title: str = ""
    text: str = ""
    assets: tuple[LinkAsset, ...] = ()
