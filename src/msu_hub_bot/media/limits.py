"""Decoded-image limits shared by native media entry points."""

MAX_IMAGE_PIXELS = 16_000_000
MAX_IMAGE_SIDE = 8192
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class MediaDimensionsError(ValueError):
    """The input exceeds the media worker's decoded-image budget."""


def validate_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
        raise MediaDimensionsError("Изображение слишком большое: максимум 16 Мп и 8192 пикселя по стороне.")
