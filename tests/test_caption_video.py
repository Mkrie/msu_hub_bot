import io
import json
import shutil
import subprocess
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from msu_hub_bot.media import caption_video as media
from msu_hub_bot.media.caption_layout import caption_frame
from msu_hub_bot.media.limits import MediaDimensionsError


NATIVE_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.mark.parametrize(
    "stream, expected",
    [
        ({"width": 640, "height": 360}, (640, 360)),
        ({"width": 333, "height": 481}, (332, 480)),
        ({"width": 320, "height": 240, "sample_aspect_ratio": "2:1"}, (640, 240)),
        ({"width": 320, "height": 240, "sample_aspect_ratio": "N/A"}, (320, 240)),
        ({"width": 320, "height": 240, "sample_aspect_ratio": "0:1"}, (320, 240)),
        ({"width": 320, "height": 240, "side_data_list": [{"rotation": -90}]}, (240, 320)),
        ({"width": 320, "height": 240, "sample_aspect_ratio": "2:1", "side_data_list": [{"rotation": 90}]}, (240, 640)),
        ({"width": 320, "height": 240, "tags": {"rotate": "270"}}, (240, 320)),
        ({"width": 640, "height": 360, "side_data_list": [{"rotation": 45}]}, (708, 708)),
        ({"width": 8192, "height": 1}, (1600, 2)),
        ({"width": 1, "height": 8192}, (2, 1600)),
        ({"width": 31, "height": 23}, (320, 238)),
    ],
)
def test_native_display_dimensions_control_scale_and_padding(stream, expected):
    assert media._display_size(stream) == expected


@pytest.mark.parametrize("size", [(8193, 1), (5000, 4000), (0, 320)])
def test_oversized_native_dimensions_are_rejected_before_decode(size):
    with pytest.raises(MediaDimensionsError):
        media._display_size({"width": size[0], "height": size[1]})


@pytest.mark.parametrize("failure", ["error", "timeout", "missing-output", "oversized", "success"])
def test_caption_is_rendered_outside_filter_syntax_and_cleanup_owns_every_file(monkeypatch, caplog, failure):
    text = 'Привет: "it\'s fine"; [100%], \\path\\.'
    paths = []
    overlays = []
    real_caption_frame = media.caption_frame

    def make_frame(*args):
        frame = real_caption_frame(*args)
        overlays.append(frame.overlay)
        return frame

    def convert(command, **kwargs):
        assert text not in " ".join(command)
        source = Path(command[command.index("-i") + 1])
        overlay = source.parent / "caption.png"
        output = Path(command[-1])
        paths.extend((source, overlay, output))
        assert source.read_bytes() == b"synthetic input"
        with Image.open(overlay) as image:
            assert image.getbbox() is not None
        assert kwargs["timeout"] == media.FFMPEG_TIMEOUT
        assert command[command.index("-fs") + 1] == str(media.MAX_OUTPUT_BYTES + 1)
        assert "-shortest" not in command and "-t" not in command
        if failure == "missing-output":
            return b""
        if failure == "oversized":
            with output.open("wb") as file:
                file.truncate(media.MAX_OUTPUT_BYTES + 1)
            return b""
        output.write_bytes(b"converted")
        if failure == "error":
            raise subprocess.CalledProcessError(1, command, stderr=b"private-native-details")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], stderr=b"private-native-details")
        return b""

    monkeypatch.setattr(media, "_probe_stream", lambda source: {"width": 320, "height": 240})
    monkeypatch.setattr(media, "caption_frame", make_frame)
    monkeypatch.setattr(media, "run_process", convert)
    source = io.BytesIO(b"synthetic input")
    result = media.caption_video(source, text, "demotivator")
    assert not source.closed
    if failure == "success":
        assert result is not None and result.getvalue() == b"converted" and result.name == "result.mp4"
        result.close()
        assert result.closed
    else:
        assert result is None
    assert not any(path.exists() or path.parent.exists() for path in paths)
    for image in overlays:
        with pytest.raises(ValueError, match="closed"):
            image.getpixel((0, 0))
    assert "private-native-details" not in caplog.text


def test_dimension_rejection_cleans_source(monkeypatch):
    paths = []

    def reject(source):
        paths.append(source)
        raise MediaDimensionsError("oversized")

    monkeypatch.setattr(media, "_probe_stream", reject)
    with pytest.raises(MediaDimensionsError):
        media.caption_video(io.BytesIO(b"synthetic input"), "Подпись", "lobster")
    assert not paths[0].parent.exists()


def _native(command):
    return subprocess.run(command, check=True, capture_output=True, timeout=20).stdout


def _make_video(tmp_path, *, audio=False, odd=False, sar=False, rotation=False, variable_rate=False):
    source = tmp_path / ("source.mkv" if odd else "source.mp4")
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={'333x481' if odd else '320x240'}:rate=10:duration=0.8",
    ]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.8"]
    if sar:
        command += ["-vf", "setsar=2/1"]
    if variable_rate:
        command += ["-vf", "setpts=if(lt(N\\,4)\\,N/(10*TB)\\,(N+4)/(10*TB))", "-fps_mode", "passthrough"]
    command += ["-c:v", "ffv1" if odd else "libx264", "-pix_fmt", "yuv444p" if odd else "yuv420p", "-c:a", "aac", str(source)]
    _native(command)
    if rotation:
        rotated = tmp_path / "rotated.mp4"
        _native(["ffmpeg", "-v", "error", "-display_rotation", "90", "-i", str(source), "-c", "copy", str(rotated)])
        source = rotated
    return source


def _frame(path, timestamp):
    data = _native(
        ["ffmpeg", "-v", "error", "-ss", timestamp, "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"]
    )
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="FFmpeg/FFprobe are not installed")
@pytest.mark.parametrize("style", ["lobster", "demotivator", "meme"])
@pytest.mark.parametrize("audio", [False, True])
def test_native_all_styles_keep_long_caption_audio_and_final_frames(tmp_path, monkeypatch, style, audio):
    source = _make_video(tmp_path, audio=audio)
    text = ('Привет: "it\'s fine"; 100% [ready], \\path\\.\nОчень длинная подпись 🙂 ' * 60)[:4096]
    overlays = []

    def capture_overlay(*args):
        frame = caption_frame(*args)
        overlays.append(replace(frame, overlay=frame.overlay.copy()))
        return frame

    monkeypatch.setattr(media, "caption_frame", capture_overlay)
    with io.BytesIO(source.read_bytes()) as input_file:
        result = media.caption_video(input_file, text, style)
    assert result is not None
    with result:
        output = tmp_path / "result.mp4"
        output.write_bytes(result.getvalue())
    info = json.loads(_native(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]))
    video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
    assert any(stream["codec_type"] == "audio" for stream in info["streams"]) is audio
    assert int(video["nb_frames"]) == 8
    assert float(video["duration"]) == pytest.approx(0.8, abs=0.02)
    assert video["width"] % 2 == video["height"] % 2 == 0
    assert len(overlays) == 1
    expected = overlays.pop()
    with closing(expected.overlay):
        assert (video["width"], video["height"]) == expected.overlay.size
        # Compare the caption's complete alpha mask, including tiny antialiased
        # glyphs, against compositing the original frame with the same overlay.
        with closing(expected.overlay.getchannel("A")) as alpha:
            mask = alpha.point(lambda value: 255 if value else 0)
        with closing(mask):
            assert mask.getbbox() is not None
            for timestamp in ("0", "0.7"):
                with (
                    closing(Image.new("RGBA", expected.overlay.size, "black")) as composite,
                    closing(_frame(source, timestamp)) as original,
                ):
                    composite.paste(original, expected.media_position)
                    composite.alpha_composite(expected.overlay)
                    with closing(composite.convert("RGB")) as reference, closing(_frame(output, timestamp)) as image:
                        with closing(ImageChops.difference(image, reference)) as difference, closing(difference.convert("L")) as luminance:
                            # Caption glyphs are monochrome; compare luminance
                            # independently of H.264's chroma subsampling.
                            assert ImageStat.Stat(luminance, mask).mean[0] < 12


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="FFmpeg/FFprobe are not installed")
@pytest.mark.parametrize(
    "options, expected",
    [
        ({"odd": True}, (332, 480)),
        ({"sar": True}, (640, 240)),
        ({"rotation": True}, (240, 320)),
        ({"sar": True, "rotation": True}, (240, 640)),
    ],
)
def test_native_geometry_uses_source_rotation_and_pixel_shape(tmp_path, options, expected):
    source = _make_video(tmp_path, **options)
    assert media._probe_size(source) == expected
    with io.BytesIO(source.read_bytes()) as input_file:
        result = media.caption_video(input_file, "Подпись", "meme")
    assert result is not None
    with result:
        output = tmp_path / "result.mp4"
        output.write_bytes(result.getvalue())
    info = json.loads(_native(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]))
    video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
    expected_frame = caption_frame((max(320, expected[0]), max(240, expected[1])), "Подпись", "meme")
    with closing(expected_frame.overlay):
        assert (video["width"], video["height"]) == expected_frame.overlay.size
    assert video["sample_aspect_ratio"] == "1:1"
    assert int(video["nb_frames"]) == 8
    assert not any(side.get("rotation", 0) for side in video.get("side_data_list", []))


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="FFmpeg/FFprobe are not installed")
def test_uploaded_playlist_is_rejected_before_opening_referenced_file(tmp_path):
    source = tmp_path / "source"
    source.write_text("ffconcat version 1.0\nfile 'unavailable'\n")
    with pytest.raises(subprocess.CalledProcessError):
        media._probe_size(source)


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="FFmpeg/FFprobe are not installed")
def test_native_variable_frame_times_are_preserved(tmp_path):
    source = _make_video(tmp_path, variable_rate=True)
    with io.BytesIO(source.read_bytes()) as input_file:
        result = media.caption_video(input_file, "Кадры на месте", "meme")
    assert result is not None
    with result:
        output = tmp_path / "result.mp4"
        output.write_bytes(result.getvalue())

    def timestamps(path):
        data = json.loads(
            _native(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "frame=best_effort_timestamp_time",
                    "-of",
                    "json",
                    str(path),
                ]
            )
        )
        return [float(frame["best_effort_timestamp_time"]) for frame in data["frames"]]

    expected = timestamps(source)
    assert expected == pytest.approx([0, 0.1, 0.2, 0.3, 0.8, 0.9, 1.0, 1.1])
    assert timestamps(output) == pytest.approx(expected)


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="FFmpeg/FFprobe are not installed")
def test_native_size_limit_never_returns_a_truncated_clip(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    _native(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=5",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    process = media.run_process
    observed = []

    def capture_result(command, **kwargs):
        result = process(command, **kwargs)
        if command[0] == "ffmpeg":
            output = Path(command[-1])
            data = json.loads(
                _native(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames", "-of", "json", str(output)]
                )
            )
            observed.append((output.stat().st_size, int(data["streams"][0]["nb_frames"])))
        return result

    monkeypatch.setattr(media, "run_process", capture_result)
    monkeypatch.setattr(media, "MAX_OUTPUT_BYTES", 1000)
    with io.BytesIO(source.read_bytes()) as file:
        assert media.caption_video(file, "Подпись", "meme") is None
    assert len(observed) == 1
    size, frames = observed[0]
    assert size > media.MAX_OUTPUT_BYTES
    assert 0 < frames < 150  # The native cap really stopped this clip early.


@pytest.mark.skipif(not NATIVE_AVAILABLE, reason="FFmpeg/FFprobe are not installed")
def test_native_oblique_rotation_preserves_all_four_corners(tmp_path):
    source_image = tmp_path / "corners.png"
    with closing(Image.new("RGB", (640, 360), "white")) as image:
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((0, 0, 39, 39), fill=(255, 0, 0))
        drawing.rectangle((600, 0, 639, 39), fill=(0, 255, 0))
        drawing.rectangle((0, 320, 39, 359), fill=(0, 0, 255))
        drawing.rectangle((600, 320, 639, 359), fill=(255, 255, 0))
        image.save(source_image)
    source = tmp_path / "source.mp4"
    rotated = tmp_path / "rotated.mp4"
    _native(
        [
            "ffmpeg",
            "-v",
            "error",
            "-loop",
            "1",
            "-framerate",
            "10",
            "-i",
            str(source_image),
            "-t",
            "0.4",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libx264",
            str(source),
        ]
    )
    _native(["ffmpeg", "-v", "error", "-display_rotation", "45", "-i", str(source), "-c", "copy", str(rotated)])
    with io.BytesIO(rotated.read_bytes()) as file:
        result = media.caption_video(file, "Все углы на месте", "meme")
    assert result is not None
    output = tmp_path / "result.mp4"
    with result:
        output.write_bytes(result.getvalue())
    assert media._probe_size(rotated) == (708, 708)
    layout = caption_frame((708, 708), "Все углы на месте", "meme")
    with closing(layout.overlay), closing(_frame(output, "0")) as frame:
        x, y = layout.media_position
        with closing(frame.crop((x, y, x + 708, y + 708))) as picture:
            colors = [(red > 160, green > 160, blue > 160) for red, green, blue in picture.get_flattened_data()]
        for color in ((True, False, False), (False, True, False), (False, False, True), (True, True, False)):
            assert colors.count(color) > 1000
    info = json.loads(_native(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]))
    video = info["streams"][0]
    assert int(video["nb_frames"]) == 4
    assert not any(side.get("rotation", 0) for side in video.get("side_data_list", []))
