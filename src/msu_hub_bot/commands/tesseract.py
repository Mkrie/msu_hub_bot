from aiogram import html
import io
import subprocess
from contextlib import closing, suppress
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from aiogram.utils.markdown import hcode

from msu_hub_bot.execution.executor import TPExecutor
from msu_hub_bot.execution.process import ProcessOutputTooLarge, run_process
from msu_hub_bot.media.limits import MediaDimensionsError, validate_dimensions
from msu_hub_bot.telegram.constants import TELEGRAM_MESSAGE_MAX_LEN
from msu_hub_bot.telegram.filters import MetaInfo
from msu_hub_bot.telegram.files import input_file
from msu_hub_bot.telegram.media_jobs import DownloadUnavailable, run_downloaded
from msu_hub_bot.utils import strip_blank_rows, valid_filename

OCR_TIMEOUT = 60
OCR_MAX_TEXT_BYTES = 1024 * 1024


class OCRError(ValueError):
    """A safe, actionable OCR failure for the command reply."""


def to_text(file: io.BytesIO) -> str:
    with TemporaryDirectory(prefix="hub-ocr-") as directory:
        source = Path(directory) / "source.png"
        try:
            with closing(Image.open(file)) as image:
                validate_dimensions(*image.size)
                if "A" in image.getbands():
                    # Match OCR's white background for transparent screenshots.
                    with closing(Image.new("RGB", image.size, "white")) as background, closing(image.getchannel("A")) as alpha:
                        background.paste(image, (0, 0), alpha)
                        background.save(source, format="PNG")
                else:
                    image.save(source, format="PNG")
            output = run_process(
                ["tesseract", str(source), "stdout", "-l", "rus+eng", "txt"],
                timeout=OCR_TIMEOUT,
                max_output_bytes=OCR_MAX_TEXT_BYTES,
            )
            return output.decode("utf-8")
        except MediaDimensionsError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise OCRError("Распознавание заняло слишком много времени. Попробуй картинку поменьше.") from exc
        except ProcessOutputTooLarge as exc:
            raise OCRError("На картинке слишком много текста. Попробуй распознать её по частям.") from exc
        except (OSError, ValueError, subprocess.CalledProcessError, Image.DecompressionBombError) as exc:
            raise OCRError("Не удалось распознать картинку. Попробуй другой файл.") from exc


async def process_image_to_text(message: Message, meta: MetaInfo, cpu_executor: TPExecutor) -> Message | bool:
    target, media = await meta.extract_image()
    if media is None:
        return True

    try:
        text, timeouted = await run_downloaded(cpu_executor, media, to_text, bot=message.bot)
    except DownloadUnavailable:
        return True
    except OCRError as exc:
        return await target.reply(str(exc))
    if timeouted:
        return await message.reply(hcode("🤷🏻‍♂️ Timeout"))
    if not text:
        return await message.reply(hcode("🤷🏻‍♂️ Текст не найден"))

    if len(text) > TELEGRAM_MESSAGE_MAX_LEN:
        txt = io.BytesIO(bytes(text, encoding="utf-8"))
        txt.seek(0)
        txt.name = f"ocr_{valid_filename(text, 20).lower()}.txt"
        return await target.reply_document(input_file(txt, txt.name))

    with suppress(TelegramBadRequest):
        return await target.reply(html.quote(strip_blank_rows(text)))

    return await message.reply(hcode("🤷🏻‍♂️ Текст не найден"))
