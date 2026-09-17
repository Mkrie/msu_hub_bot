"""OCR owns native deadlines, safe failures and disposable image files."""

import io
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw, ImageFont

from msu_hub_bot.commands import tesseract as ocr
from msu_hub_bot.execution import process as native
from msu_hub_bot.media import limits
from telegram_helpers import make_bot, make_message


def image_bytes(mode="RGB", color="white"):
    output = io.BytesIO()
    with Image.new(mode, (2, 2), color) as image:
        image.save(output, format="PNG")
    output.seek(0)
    return output


@pytest.mark.parametrize("mode,color", [("RGB", "white"), ("RGBA", (0, 0, 0, 0))])
def test_ocr_normalization_languages_image_close_and_workspace_cleanup(monkeypatch, mode, color):
    paths = []
    opened = []
    image_open = Image.open

    def capture_image(*args, **kwargs):
        image = image_open(*args, **kwargs)
        opened.append(image)
        return image

    def recognize(command, **kwargs):
        assert command[0] == "tesseract"
        assert command[2:] == ["stdout", "-l", "rus+eng", "txt"]
        assert kwargs == {"timeout": ocr.OCR_TIMEOUT, "max_output_bytes": ocr.OCR_MAX_TEXT_BYTES}
        source = Path(command[1])
        paths.append(source)
        with image_open(source) as image:
            assert image.size == (2, 2)
            assert image.convert("RGB").getpixel((0, 0)) == (255, 255, 255)
        return "Привет, world!\n".encode()

    monkeypatch.setattr(ocr.Image, "open", capture_image)
    monkeypatch.setattr(ocr, "run_process", recognize)
    assert ocr.to_text(image_bytes(mode, color)) == "Привет, world!\n"
    assert all(not path.parent.exists() for path in paths)
    for image in opened:
        with pytest.raises(ValueError, match="closed"):
            image.getpixel((0, 0))


@pytest.mark.parametrize("finish", ["success", "error", "timeout"])
def test_native_ocr_cleanup_and_reap_on_every_exit(monkeypatch, finish):
    popen = subprocess.Popen
    children = []
    inputs = []
    scripts = {
        "success": "import sys; sys.stdout.write('recognized text')",
        "error": "import sys; sys.stderr.write('private native details'); sys.exit(2)",
        "timeout": "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(5)",
    }

    def synthetic_tool(command, **kwargs):
        source = Path(command[1])
        assert source.exists()
        inputs.append(source)
        process = popen([sys.executable, "-c", scripts[finish]], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(native.subprocess, "Popen", synthetic_tool)
    monkeypatch.setattr(ocr, "OCR_TIMEOUT", 0.5)
    if finish == "success":
        assert ocr.to_text(image_bytes()) == "recognized text"
    else:
        with pytest.raises(ocr.OCRError) as failure:
            ocr.to_text(image_bytes())
        assert "private native details" not in str(failure.value)
        if finish == "timeout":
            assert "времени" in str(failure.value)
            assert children[0].returncode == -signal.SIGKILL
    assert len(children) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0].pid, os.WNOHANG)
    assert all(not path.parent.exists() for path in inputs)


@pytest.mark.parametrize("failure", ["missing", "oversized-output", "invalid-encoding"])
def test_ocr_failures_are_safe_and_remove_files(monkeypatch, failure):
    inputs = []

    def fail(command, **kwargs):
        inputs.append(Path(command[1]))
        if failure == "missing":
            raise FileNotFoundError("private file path")
        if failure == "oversized-output":
            raise native.ProcessOutputTooLarge("private native output")
        return b"\xff"

    monkeypatch.setattr(ocr, "run_process", fail)
    with pytest.raises(ocr.OCRError) as error:
        ocr.to_text(image_bytes())
    assert "private" not in str(error.value)
    assert all(not path.parent.exists() for path in inputs)


@pytest.mark.parametrize("bound,value", [("MAX_IMAGE_PIXELS", 3), ("MAX_IMAGE_SIDE", 1)])
def test_oversized_images_are_rejected_before_decoding(monkeypatch, bound, value):
    data = image_bytes()
    monkeypatch.setattr(limits, bound, value)

    def decode(*args, **kwargs):
        pytest.fail("image pixels were decoded before checking dimensions")

    monkeypatch.setattr(Image.Image, "load", decode)
    with pytest.raises(limits.MediaDimensionsError, match="слишком большое"):
        ocr.to_text(data)


def test_invalid_image_is_a_safe_failure():
    with pytest.raises(ocr.OCRError, match="Не удалось распознать"):
        ocr.to_text(io.BytesIO(b"not an image"))


async def test_command_replies_with_safe_ocr_failure(monkeypatch):
    bot = make_bot()
    target = make_message(bot, message_id=7)
    message = make_message(bot, message_id=8)
    meta = SimpleNamespace(extract_image=AsyncMock(return_value=(target, object())))
    executor = SimpleNamespace()
    monkeypatch.setattr(ocr, "run_downloaded", AsyncMock(side_effect=ocr.OCRError("Попробуй картинку поменьше.")))
    await ocr.process_image_to_text(message, meta, executor)
    reply = bot.session.methods[-1]
    assert reply.text == "Попробуй картинку поменьше."
    assert reply.reply_parameters.message_id == target.message_id


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract is not installed")
def test_native_ocr_recognizes_synthetic_text():
    output = io.BytesIO()
    with Image.new("RGB", (600, 120), "white") as image:
        ImageDraw.Draw(image).text((20, 15), "Hello 123", font=ImageFont.load_default(size=64), fill="black")
        image.save(output, format="PNG")
    output.seek(0)
    text = ocr.to_text(output)
    assert "Hello" in text and "123" in text
