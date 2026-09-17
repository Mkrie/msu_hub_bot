"""Measured captions for image overlays and framed image/video demotivators."""

import re
import math
import unicodedata
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from io import BytesIO
from functools import lru_cache
from typing import Literal, cast

from PIL import Image, ImageDraw, ImageFont, ImageOps

from msu_hub_bot.resources import lobster_font, meme_font, times_new_roman_font
from msu_hub_bot.media.limits import validate_dimensions

MAX_IMAGE_SIDE = 1600
CaptionStyle = Literal["lobster", "demotivator", "meme"]


class CaptionLayoutError(ValueError):
    pass


@dataclass
class Caption:
    text: str
    font: ImageFont.FreeTypeFont
    bounds: tuple[int, int, int, int]
    spacing: int
    stroke: int = 0
    scale: float = 1

    @property
    def width(self) -> int:
        return max(1, round((self.bounds[2] - self.bounds[0]) * self.scale))

    @property
    def height(self) -> int:
        return max(1, round((self.bounds[3] - self.bounds[1]) * self.scale))

    def draw(self, image: Image.Image, left: int, top: int, color: str = "white") -> None:
        if self.scale != 1:
            size = (max(1, self.bounds[2] - self.bounds[0]), max(1, self.bounds[3] - self.bounds[1]))
            with closing(Image.new("RGBA", size)) as layer:
                self._draw(layer, 0, 0, color)
                with closing(layer.resize((self.width, self.height), Image.Resampling.LANCZOS)) as scaled:
                    if image.mode == "RGBA":
                        image.alpha_composite(scaled, (left, top))
                    else:
                        image.paste(scaled, (left, top), scaled)
            return
        self._draw(image, left, top, color)

    def _draw(self, image: Image.Image, left: int, top: int, color: str) -> None:
        ImageDraw.Draw(image).multiline_text(
            (left - self.bounds[0], top - self.bounds[1]),
            self.text,
            font=self.font,
            spacing=self.spacing,
            align="center",
            fill=color,
            stroke_width=self.stroke,
            stroke_fill="black",
        )


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int, stroke: int) -> str:
    @lru_cache(maxsize=1024)
    def fits(value: str) -> bool:
        left, _, right, _ = font.getbbox(value, stroke_width=stroke)
        return right - left <= width

    def cluster_boundary(value: str, length: int) -> int:
        def continues(index: int) -> bool:
            character = value[index]
            return (
                unicodedata.category(character).startswith("M")
                or character == "\u200d"
                or value[index - 1] == "\u200d"
                or "\U0001f3fb" <= character <= "\U0001f3ff"
            )

        while 0 < length < len(value) and continues(length):
            length -= 1
        if length:
            return length
        # Keep an oversized first cluster intact so fitting can shrink it.
        length = 1
        while length < len(value) and continues(length):
            length += 1
        return length

    def prefix_length(value: str) -> int:
        # Bracket one line before measuring it. Repeatedly shaping the entire
        # suffix of a long URL/word makes wrapping quadratic in its length.
        low, high = 0, min(16, len(value))
        while fits(value[:high]):
            if high == len(value):
                return high
            low, high = high, min(high * 2, len(value))
        while low < high:
            middle = (low + high + 1) // 2
            if fits(value[:middle]):
                low = middle
            else:
                high = middle - 1
        return cluster_boundary(value, low)

    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            if line:
                candidate = line + " " + word
                if prefix_length(candidate) == len(candidate):
                    line = candidate
                    continue
                lines.append(line)
                line = ""
            while word:
                length = max(1, prefix_length(word))
                if length == len(word):
                    line = word
                    break
                lines.append(word[:length])
                word = word[length:]
        if line or not paragraph.strip():
            lines.append(line)
    return "\n".join(lines)


def fit_caption(text: str, font_path: Path, width: int, height: int, preferred_size: int, stroke: int = 0) -> Caption:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    # Preserve paragraph breaks without spending the whole canvas on blank rows.
    text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)
    if not text:
        raise CaptionLayoutError("Добавь текст для подписи.")
    if width <= 0 or height <= 0:
        raise ValueError("Caption rectangle must have positive dimensions")

    def layout(size: int) -> Caption:
        font = ImageFont.truetype(str(font_path), size)
        spacing = max(1, size // 5)
        outline = min(stroke, size // 8)
        wrapped = _wrap(text, font, width, outline)
        with closing(Image.new("L", (1, 1))) as measuring:
            bounds = ImageDraw.Draw(measuring).multiline_textbbox(
                (0, 0), wrapped, font=font, spacing=spacing, align="center", stroke_width=outline
            )
        pixel_bounds = (math.floor(bounds[0]), math.floor(bounds[1]), math.ceil(bounds[2]), math.ceil(bounds[3]))
        return Caption(wrapped, font, pixel_bounds, spacing, outline)

    low, high = 1, max(1, preferred_size)
    preferred = layout(high)
    if preferred.width <= width and preferred.height <= height:
        best = preferred
        high = 0
    else:
        best = None
    high -= 1
    while low <= high:
        size = (low + high) // 2
        caption = layout(size)
        if caption.width <= width and caption.height <= height:
            best = caption
            low = size + 1
        else:
            high = size - 1
    if best is None:
        # Many explicit line breaks can exceed even a one-pixel layout. Scale
        # the complete text layer instead of dropping lines or rejecting text.
        best = layout(1)
        best.scale = min(1, width / best.width, height / best.height)
    # Font metrics include invisible ascender/italic margins. Center visible
    # ink, especially for short lowercase captions in the demotivator panel.
    raw_width = max(1, best.bounds[2] - best.bounds[0])
    raw_height = max(1, best.bounds[3] - best.bounds[1])
    with closing(Image.new("RGBA", (raw_width, raw_height))) as mask:
        best._draw(mask, 0, 0, "white")
        with closing(mask.getchannel("A")) as alpha:
            ink = alpha.getbbox()
    if ink:
        x, y = best.bounds[:2]
        best.bounds = (x + ink[0], y + ink[1], x + ink[2], y + ink[3])
    return best


def base_image(file: BytesIO, preserve_alpha: bool = False) -> Image.Image:
    with ExitStack() as images:
        # Preserve caller ownership while Pillow closes its own image buffer.
        source = images.enter_context(BytesIO(file.getvalue()))
        original = images.enter_context(closing(Image.open(source)))
        validate_dimensions(*original.size)
        oriented = images.enter_context(closing(ImageOps.exif_transpose(original)))
        image = images.enter_context(closing(oriented.convert("RGBA")))
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
        if max(image.size) < 320:
            scale = 320 / max(image.size)
            size = cast(tuple[int, int], tuple(max(1, round(side * scale)) for side in image.size))
            image = images.enter_context(closing(image.resize(size, Image.Resampling.LANCZOS)))
        canvas = Image.new("RGBA" if preserve_alpha else "RGB", (max(320, image.width), max(240, image.height)), "black")
        canvas.paste(image, ((canvas.width - image.width) // 2, (canvas.height - image.height) // 2), None if preserve_alpha else image)
        return canvas


def _lobster_overlay(size: tuple[int, int], text: str) -> Image.Image:
    width, height = size
    margin = max(12, round(width * 0.04))
    bottom = max(12, round(height * 0.08))
    caption = fit_caption(
        text,
        lobster_font,
        width - margin * 2,
        height - bottom - max(12, height // 5),
        preferred_size=min(100, round(0.0669 * width + 4.2772)),
        stroke=1,
    )
    image = Image.new("RGBA", size)
    caption.draw(image, (width - caption.width) // 2, height - bottom - caption.height)
    return image


def frame_border(width: int) -> int:
    return max(round((width + 12) * 0.07), 20)


def _caption_panel(width: int, text: str, border: int, *, meme: bool = False, parity: int = 0) -> Image.Image:
    caption = fit_caption(
        text,
        meme_font if meme else times_new_roman_font,
        width - border * 2,
        min(1200, max(240, width)),
        preferred_size=min(96, round(width * 0.075)),
    )
    height = caption.height + border * 2
    height += (height + parity) % 2
    panel = Image.new("RGBA", (width, height), "white" if meme else "black")
    caption.draw(panel, (width - caption.width) // 2, (height - caption.height) // 2, "black" if meme else "white")
    return panel


def demotivator_caption(width: int, text: str, border: int) -> Image.Image:
    with closing(_caption_panel(width, text, border)) as panel:
        return panel.convert("RGB")


@dataclass
class CaptionFrame:
    """Shared image/video geometry; the caller owns the RGBA overlay."""

    overlay: Image.Image
    media_position: tuple[int, int]
    media_size: tuple[int, int]


def caption_frame(size: tuple[int, int], text: str, style: CaptionStyle) -> CaptionFrame:
    width, height = size
    if style == "lobster":
        return CaptionFrame(_lobster_overlay(size, text), (0, 0), size)
    if style == "meme":
        with closing(_caption_panel(width, text, max(12, round(width * 0.04)), meme=True)) as panel:
            overlay = Image.new("RGBA", (width, height + panel.height))
            overlay.paste(panel, (0, 0))
            return CaptionFrame(overlay, (0, panel.height), size)
    border = frame_border(width)
    frame_width = width + 12 + border * 2
    panel_top = border + height + 12
    with closing(_caption_panel(frame_width, text, border, parity=panel_top)) as panel:
        overlay = Image.new("RGBA", (frame_width, panel_top + panel.height), "black")
        ImageDraw.Draw(overlay).rectangle((border, border, frame_width - border - 1, panel_top - 1), outline="white", width=3)
        position = (border + 6, border + 6)
        overlay.paste((0, 0, 0, 0), (*position, position[0] + width, position[1] + height))
        overlay.paste(panel, (0, panel_top))
        return CaptionFrame(overlay, position, size)


def caption_image(file: BytesIO, text: str, style: CaptionStyle) -> Image.Image:
    with closing(base_image(file, preserve_alpha=style == "lobster")) as source:
        frame = caption_frame(source.size, text, style)
        with closing(frame.overlay):
            result = Image.new("RGBA", frame.overlay.size)
            try:
                result.paste(source, frame.media_position)
                result.alpha_composite(frame.overlay)
                if style == "lobster":
                    return result
                return result.convert("RGB")
            except BaseException:
                result.close()
                raise
            finally:
                if style != "lobster":
                    result.close()


def lobster_image(file: BytesIO, text: str) -> Image.Image:
    return caption_image(file, text, "lobster")


def demotivator_image(file: BytesIO, text: str) -> Image.Image:
    return caption_image(file, text, "demotivator")


def meme_image(file: BytesIO, text: str) -> Image.Image:
    return caption_image(file, text, "meme")
