"""Apply the shared caption canvas to every frame of bounded local video."""

import json
import logging
import math
import subprocess
from contextlib import closing
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from msu_hub_bot.execution.process import ProcessOutputTooLarge, run_process
from msu_hub_bot.media.caption_layout import MAX_IMAGE_SIDE, CaptionLayoutError, CaptionStyle, caption_frame
from msu_hub_bot.media.ffmpeg import FFMPEG_TIMEOUT, FFPROBE_TIMEOUT, MAX_OUTPUT_BYTES
from msu_hub_bot.media.limits import LOCAL_MEDIA_FORMATS, MediaDimensionsError, validate_dimensions


logger = logging.getLogger(__name__)


def _rotation(stream: dict[str, Any]) -> float:
    rotation = 0.0
    tags = stream.get("tags")
    if isinstance(tags, dict) and "rotate" in tags:
        rotation = float(tags["rotate"])
    for side_data in stream.get("side_data_list", []):
        if isinstance(side_data, dict) and "rotation" in side_data:
            rotation = float(side_data["rotation"])
            break
    if not math.isfinite(rotation):
        raise ValueError("Invalid video rotation")
    return rotation % 360


def _display_size(stream: dict[str, Any], *, rotated: bool = True) -> tuple[int, int]:
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    validate_dimensions(width, height)
    try:
        sar = float(Fraction(str(stream.get("sample_aspect_ratio", "1:1")).replace(":", "/")))
    except ValueError, ZeroDivisionError, OverflowError:
        sar = 1.0
    if not math.isfinite(sar) or sar <= 0:
        sar = 1.0
    displayed_width, displayed_height = width * sar, float(height)
    rotation = _rotation(stream) if rotated else 0
    # FFmpeg autorotation applies the display matrix before the explicit scale.
    # A transpose also inverts the sample aspect ratio.
    if rotation % 90:
        angle = math.radians(rotation)
        cosine, sine = abs(math.cos(angle)), abs(math.sin(angle))
        displayed_width, displayed_height = (
            displayed_width * cosine + displayed_height * sine,
            displayed_width * sine + displayed_height * cosine,
        )
    elif rotation % 180 == 90:
        displayed_width, displayed_height = displayed_height, displayed_width
    scale = min(MAX_IMAGE_SIDE / max(displayed_width, displayed_height), 1.0)
    if max(displayed_width, displayed_height) < 320:
        scale = 320 / max(displayed_width, displayed_height)

    def even(value: float) -> int:
        return max(2, round(value * scale / 2) * 2)

    return even(displayed_width), even(displayed_height)


def _probe_stream(source: Path) -> dict[str, Any]:
    data = json.loads(
        run_process(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                LOCAL_MEDIA_FORMATS,
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,sample_aspect_ratio:stream_side_data=rotation:stream_tags=rotate",
                "-of",
                "json",
                str(source),
            ],
            timeout=FFPROBE_TIMEOUT,
            max_output_bytes=1024 * 1024,
        )
    )
    if not isinstance(data, dict) or not isinstance(data.get("streams"), list) or not data["streams"]:
        raise ValueError("Video stream is missing")
    stream = data["streams"][0]
    if not isinstance(stream, dict):
        raise ValueError("Invalid video metadata")
    return stream


def _probe_size(source: Path) -> tuple[int, int]:
    return _display_size(_probe_stream(source))


def caption_video(file: BytesIO, text: str, style: CaptionStyle) -> BytesIO | None:
    """Keep the clip's timeline and audio; callers own both input and output.

    Caption text is rendered by Pillow and never interpolated into native syntax.
    The video is the primary overlay input, so a one-frame caption image stays
    visible without ending or extending the source video.
    """
    with TemporaryDirectory(prefix="hub-caption-") as directory:
        source = Path(directory) / "source"
        overlay = Path(directory) / "caption.png"
        output = Path(directory) / "result.mp4"
        try:
            source.write_bytes(file.getvalue())
            stream = _probe_stream(source)
            width, height = _display_size(stream)
            rotation = _rotation(stream)
            rotation_options: list[str] = []
            rotation_filter = ""
            if rotation % 90:
                # Native autorotation crops oblique matrices to the unrotated
                # rectangle. Normalize pixels first and expand the rotated
                # bounds explicitly, then fit the resulting canvas below.
                pre_width, pre_height = _display_size(stream, rotated=False)
                angle = -math.radians(rotation)
                rotation_options = ["-noautorotate", "-display_rotation", "0"]
                rotation_filter = (
                    f"scale={pre_width}:{pre_height}:flags=lanczos,setsar=1,format=rgba,"
                    f"rotate={angle}:ow=ceil(rotw({angle})):oh=ceil(roth({angle})):c=black,"
                )
            media_width, media_height = max(320, width), max(240, height)
            frame = caption_frame((media_width, media_height), text, style)
            with closing(frame.overlay):
                frame.overlay.save(overlay, "PNG")
                output_width, output_height = frame.overlay.size
            # Symmetric padding preserves extreme aspect ratios and leaves room
            # for captions, matching the shared still-image renderer.
            x = frame.media_position[0] + (media_width - width) // 2
            y = frame.media_position[1] + (media_height - height) // 2
            output_width += output_width % 2
            output_height += output_height % 2
            filters = (
                f"[0:v:0]{rotation_filter}scale={width}:{height}:flags=lanczos,setsar=1,format=rgba,"
                f"pad={output_width}:{output_height}:{x}:{y}:color=black[video];"
                "[video][1:v:0]overlay=0:0:eof_action=repeat:repeatlast=1:shortest=0:format=auto,format=yuv420p[out]"
            )
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
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                LOCAL_MEDIA_FORMATS,
                *rotation_options,
                "-i",
                str(source),
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                str(overlay),
                "-filter_complex",
                filters,
                "-map",
                "[out]",
                "-map",
                "0:a?",
                "-map_metadata",
                "-1",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-threads",
                "2",
                "-fps_mode",
                "passthrough",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                # Limit temporary disk use as well as the returned buffer. A file
                # stopped by this limit is oversized and is never sent truncated.
                "-fs",
                str(MAX_OUTPUT_BYTES + 1),
                str(output),
            ]
            run_process(command, timeout=FFMPEG_TIMEOUT)
            if output.stat().st_size > MAX_OUTPUT_BYTES:
                logger.warning("Caption video exceeded its size limit")
                return None
            result = BytesIO(output.read_bytes())
        except CaptionLayoutError, MediaDimensionsError:
            raise
        except subprocess.TimeoutExpired:
            logger.warning("Caption video exceeded its native deadline")
            return None
        except OSError, ValueError, TypeError, OverflowError, subprocess.CalledProcessError, ProcessOutputTooLarge:
            # Native diagnostics can contain input paths or user-supplied text.
            logger.warning("Caption video conversion failed")
            return None
        result.name = output.name
        return result
