"""Persistent offline FunASR worker run by the user's existing model environment."""
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

if sys.platform == 'win32':
    import ctypes
    ctypes.windll.kernel32.SetDllDirectoryW(None)


def deny_network(*args, **kwargs):
    raise OSError('Offline speech recognition does not allow network connections')


socket.socket.connect = deny_network
socket.create_connection = deny_network
root = Path(sys.argv[1]).resolve()
scratch = Path(sys.argv[2]).resolve()
device = sys.argv[3]
paths = root / 'tools/asr/models'
scratch.mkdir(parents=True, exist_ok=True)
with contextlib.redirect_stdout(sys.stderr):
    # The embeddable Python package skips setuptools' distutils activation at
    # startup; FunASR's punctuation model still imports that compatibility layer.
    os.environ['SETUPTOOLS_USE_DISTUTILS'] = 'local'
    import setuptools
    from funasr import AutoModel
    model = AutoModel(
        model=str(paths / 'speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch'),
        vad_model=str(paths / 'speech_fsmn_vad_zh-cn-16k-common-pytorch'),
        punc_model=str(paths / 'punc_ct-transformer_zh-cn-common-vocab272727-pytorch'),
        device=device, disable_update=True, disable_pbar=True, check_latest=False,
        ncpu=4, trust_remote_code=False,
    )
print(json.dumps({'ready': True}), flush=True)
for line in sys.stdin:
    try:
        request = json.loads(line)
        source = Path(request['path']).resolve(strict=True)
        runtime = root / 'runtime'
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        info = subprocess.run([str(runtime / 'ffprobe.exe'), '-v', 'error', '-show_entries',
            'format=duration', '-of', 'json', str(source)], capture_output=True, timeout=20,
            creationflags=flags, check=True)
        duration = float(json.loads(info.stdout)['format']['duration'])
        if not 0 < duration <= 120:
            raise ValueError('录音需在 120 秒以内')
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            wav = Path(folder) / 'input.wav'
            subprocess.run([str(runtime / 'ffmpeg.exe'), '-nostdin', '-v', 'error', '-i', str(source),
                '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(wav)], capture_output=True,
                timeout=25, creationflags=flags, check=True)
            with contextlib.redirect_stdout(sys.stderr):
                result = model.generate(input=str(wav), cache={}, batch_size_s=120, disable_pbar=True)
        text = ''.join(str(item.get('text', '')) for item in result).strip()
        print(json.dumps({'ok': True, 'text': text, 'duration': duration}, ensure_ascii=False), flush=True)
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)[:1500]}, ensure_ascii=False), flush=True)
