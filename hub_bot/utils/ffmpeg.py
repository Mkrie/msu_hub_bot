import io
import os
import subprocess
from tempfile import NamedTemporaryFile
from typing import List, Optional

from common.utils import FakeBytesIO


def ffmpeg(file: io.BytesIO, parameters: List[str] = None, out_suffix: str = None) -> Optional[io.BytesIO]:
    parameters = parameters or []

    f_input = NamedTemporaryFile(delete=False)
    f_output = NamedTemporaryFile(suffix=out_suffix, delete=False)

    f_input.write(file.read())

    command = [
        'ffmpeg',
        '-y',
        '-i', f_input.name,
        *parameters,
        f_output.name,
    ]

    with open(os.devnull, 'rb') as devnull:
        p = subprocess.Popen(command, stdin=devnull, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p_out, p_err = p.communicate()

    if p.returncode != 0:
        print(p_out.decode(errors='ignore'))
        print(p_err.decode(errors='ignore'))
        return None

    f_output.seek(0)
    result = FakeBytesIO(f_output.read())
    result.name = f_output.name
    result.seek(0)

    f_input.close()
    f_output.close()
    os.unlink(f_input.name)
    os.unlink(f_output.name)

    return result


def ffmpeg2(file: io.BytesIO, parameters1: List[str] = None, parameters2: List[str] = None, out_suffix: str = None) -> Optional[io.BytesIO]:
    parameters1 = parameters1 or []
    parameters2 = parameters2 or []

    f_input = NamedTemporaryFile(delete=False)
    f_output = NamedTemporaryFile(suffix=out_suffix, delete=False)

    f_input.write(file.read())

    command = [
        'ffmpeg',
        '-y',
        *parameters1,
        '-i', f_input.name,
        *parameters2,
        f_output.name,
    ]

    with open(os.devnull, 'rb') as devnull:
        p = subprocess.Popen(command, stdin=devnull, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p_out, p_err = p.communicate()

    if p.returncode != 0:
        print(p_out.decode(errors='ignore'))
        print(p_err.decode(errors='ignore'))
        return None

    f_output.seek(0)
    result = FakeBytesIO(f_output.read())
    result.name = f_output.name
    result.seek(0)

    f_input.close()
    f_output.close()
    os.unlink(f_input.name)
    os.unlink(f_output.name)

    return result


def to_ogg_opus(file: io.BytesIO) -> io.BytesIO:
    parameters = [
        '-f', 'ogg',
        '-codec:a', 'libopus',
        '-vn',
    ]
    return ffmpeg(file, out_suffix='.ogg', parameters=parameters)
