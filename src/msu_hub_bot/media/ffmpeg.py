import io
import json
import logging
import math
import subprocess
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

from msu_hub_bot.execution.process import ProcessOutputTooLarge, run_process
from msu_hub_bot.media.limits import MediaDimensionsError, validate_dimensions
from msu_hub_bot.utils import FakeBytesIO


FFMPEG_TIMEOUT = 120
FFPROBE_TIMEOUT = 10
MAX_REVERSE_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024 * 1024
logger = logging.getLogger(__name__)


class ReverseMediaError(ValueError):
    """A safe explanation when a whole-clip reverse cannot fit its budget."""


def _positive(value: object) -> float:
    try:
        number = float(Fraction(str(value)))
        return number if math.isfinite(number) and number > 0 else 0
    except ValueError, ZeroDivisionError, OverflowError:
        return 0


def _validate_source(source: Path, *, reverse: bool) -> None:
    data = json.loads(
        run_process(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,width,height,avg_frame_rate,r_frame_rate,duration,nb_frames,sample_rate,channels:format=duration",
                "-of",
                "json",
                str(source),
            ],
            timeout=FFPROBE_TIMEOUT,
            max_output_bytes=1024 * 1024,
        )
    )
    if not isinstance(data, dict) or not isinstance(data.get("streams"), list):
        raise ValueError("Invalid native media metadata")
    format_info = data.get("format", {})
    format_duration = _positive(format_info.get("duration")) if isinstance(format_info, dict) else 0
    buffered = 0.0
    for stream in data["streams"]:
        if not isinstance(stream, dict):
            raise ValueError("Invalid native stream metadata")
        kind = stream.get("codec_type")
        if kind not in {"audio", "video"}:
            continue
        if kind == "video":
            width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
            validate_dimensions(width, height)
        if not reverse:
            continue
        duration = max(_positive(stream.get("duration")), format_duration)
        if not duration:
            raise ReverseMediaError("Не удалось безопасно определить длину файла для разворота. Попробуй другой файл.")
        if kind == "video":
            rate = max(_positive(stream.get("avg_frame_rate")), _positive(stream.get("r_frame_rate")))
            if not rate:
                raise ReverseMediaError("Не удалось определить частоту кадров для разворота. Попробуй другой файл.")
            frames = max(_positive(stream.get("nb_frames")), math.ceil(duration * rate) + 1)
            buffered += width * height * frames * 4
        else:
            sample_rate, channels = _positive(stream.get("sample_rate")), _positive(stream.get("channels"))
            if not sample_rate or not channels:
                raise ReverseMediaError("Не удалось определить параметры звука для разворота. Попробуй другой файл.")
            buffered += duration * sample_rate * channels * 8
        if buffered > MAX_REVERSE_BYTES:
            raise ReverseMediaError("Файл слишком большой для разворота. Попробуй более короткий фрагмент или меньшее разрешение.")


def ffmpeg(
    file: io.BytesIO, parameters: list[str] | None = None, out_suffix: str | None = None, *, reverse: bool = False
) -> io.BytesIO | None:
    return ffmpeg2(file, parameters2=parameters, out_suffix=out_suffix, reverse=reverse)


def ffmpeg2(
    file: io.BytesIO,
    parameters1: list[str] | None = None,
    parameters2: list[str] | None = None,
    out_suffix: str | None = None,
    *,
    reverse: bool = False,
) -> io.BytesIO | None:
    """Convert in a disposable workspace, killing native work at its own deadline."""
    with TemporaryDirectory(prefix="hub-media-") as directory:
        source = Path(directory) / "source"
        output = Path(directory) / ("result" + (out_suffix or ""))
        command = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-filter_threads",
            "1",
            "-filter_complex_threads",
            "1",
            "-threads",
            "2",
            *(parameters1 or []),
            "-i",
            str(source),
            *(parameters2 or []),
            "-threads",
            "2",
            str(output),
        ]
        try:
            # Close the writer before FFmpeg opens even a very small input.
            source.write_bytes(file.read())
            _validate_source(source, reverse=reverse)
            run_process(command, timeout=FFMPEG_TIMEOUT)
            if output.stat().st_size > MAX_OUTPUT_BYTES:
                logger.warning("FFmpeg output exceeded its size limit")
                return None
            result = FakeBytesIO(output.read_bytes())
        except subprocess.TimeoutExpired:
            logger.warning("FFmpeg conversion exceeded its deadline")
            return None
        except MediaDimensionsError, ReverseMediaError:
            raise
        except OSError, ValueError, TypeError, OverflowError, subprocess.CalledProcessError, ProcessOutputTooLarge:
            # Native diagnostics can include input text/URLs; do not print them.
            logger.warning("FFmpeg conversion failed")
            return None
        result.name = output.name
        return result


def to_ogg_opus(file: io.BytesIO) -> io.BytesIO | None:
    parameters = [
        "-f",
        "ogg",
        "-codec:a",
        "libopus",
        "-vn",
    ]
    return ffmpeg(file, out_suffix=".ogg", parameters=parameters)
