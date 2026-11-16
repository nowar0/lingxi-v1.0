"""Owned, lazy GPT-SoVITS V2 service. No weights or external package are modified."""
import asyncio
import base64
import binascii
import io
import json
import os
from pathlib import Path
from typing import Literal
import socket
import subprocess
import time
import wave
from uuid import uuid4

import httpx2
import yaml
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app import catalog, config

MODEL = 'gpt-sovits-v2'
MAX_REFERENCE = 8 * 1024 * 1024
MAX_AUDIO = 24 * 1024 * 1024


class Voice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    name: str = Field(min_length=1, max_length=100)
    prompt_lang: str = Field(pattern=r'^(zh|ja|en|ko|yue)$')
    text_lang: str = Field(default='auto', pattern=r'^(auto|zh|all_zh|ja|all_ja|en|ko|yue)$')
    gpt_weights: str = Field(max_length=2000)
    sovits_weights: str = Field(max_length=2000)
    reference_audio: str = Field(default='', max_length=2000)
    reference_text: str = Field(default='', max_length=2000)


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    engine: str = Field(default=MODEL,max_length=100)
    package_dir: str = Field(min_length=1, max_length=2000)
    default_voice: str = Field(max_length=60)
    speed: float = Field(default=1, ge=0.5, le=2)
    device: Literal['cpu', 'cuda'] = 'cpu'
    voices: list[Voice] = Field(min_length=1, max_length=30)
    expected_revision: int = Field(default=0, ge=0)


def settings():
    try:
        return catalog.get('local-tts', 'settings')
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    value = json.loads((config.BASE_DIR / 'config' / 'local_tts.json').read_text(encoding='utf-8'))
    for voice in value['voices']:
        reference = voice.get('reference_audio', '')
        if reference and not Path(reference).is_absolute():
            voice['reference_audio'] = str(config.BASE_DIR / reference)
    return {**value, 'revision': 0}


def native_module():
    return {'enabled': False, 'base_url': 'http://127.0.0.1', 'model': MODEL,
            'credential_ref': '', 'local': True, 'capabilities': ['tts'],
            'voice': settings()['default_voice'], 'timeout': 180,
            'difficulty': 1, 'temperature': 0.7}


def is_native(item):
    return item.get('model') == MODEL and item.get('local') is True


def validate_synthesis(data):
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            frames = audio.readframes(audio.getnframes())
            silence = b'\x80' if audio.getsampwidth() == 1 else b'\x00'
            if not frames or not frames.strip(silence):
                raise HTTPException(502, '语音整合包返回了静音，未判定为成功；请改用 CPU 并查看语音服务日志')
    except (wave.Error, EOFError) as error:
        raise HTTPException(502, '本地语音结果不是有效 PCM WAV') from error


def wav_duration(data):
    if not 0 < len(data) <= MAX_REFERENCE:
        raise ValueError('参考录音限 8 MiB')
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if audio.getcomptype() != 'NONE' or audio.getnchannels() not in (1, 2):
                raise ValueError('请使用单声道或双声道 PCM WAV 录音')
            duration = audio.getnframes() / audio.getframerate()
            expected = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
            if len(audio.readframes(audio.getnframes())) != expected:
                raise ValueError('录音文件不完整')
    except (wave.Error, EOFError, ZeroDivisionError) as exc:
        raise ValueError('请使用有效的 PCM WAV 录音') from exc
    if not 3 <= duration <= 10:
        raise ValueError('参考录音需要 3～10 秒，请先裁剪录音')
    return round(duration, 3)


def voice_status(voice):
    errors = []
    if not voice.get('reference_text', '').strip():
        errors.append('待补参考录音原文')
    for key, label, suffix in (('gpt_weights', 'GPT 权重', '.ckpt'),
                                ('sovits_weights', 'SoVITS 权重', '.pth')):
        path = Path(voice[key])
        if path.suffix.lower() != suffix or not path.is_file():
            errors.append(label + '不存在或格式不匹配')
    duration = None
    ref = voice.get('reference_audio', '')
    if not ref:
        errors.append('待补参考录音（3～10 秒 WAV）')
    else:
        try:
            path = Path(ref)
            if path.stat().st_size > MAX_REFERENCE:
                raise ValueError('参考录音限 8 MiB')
            duration = wav_duration(path.read_bytes())
        except (OSError, ValueError) as exc:
            errors.append(str(exc) if isinstance(exc, ValueError) else '参考录音文件不存在或无法读取')
    return {'id': voice['id'], 'ready': not errors, 'issues': errors, 'duration': duration}


def package_issues(value):
    root = Path(value['package_dir'])
    required = ['runtime/python.exe', 'api_v2.py',
                'GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large',
                'GPT_SoVITS/pretrained_models/chinese-hubert-base']
    return ['安装目录缺少 ' + name for name in required if not (root / name).exists()]


def select_voice(value, voice_id=''):
    selected = next((v for v in value['voices'] if v['id'] == (voice_id or value['default_voice'])), None)
    if selected is None:
        raise HTTPException(422, '音色不存在，请在本地音色设置中选择有效音色编号')
    issues = package_issues(value) + voice_status(selected)['issues']
    if issues:
        raise HTTPException(409, '；'.join(issues))
    return selected


class Service:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.process = None
        self.port = None
        self.loaded = None
        self.loaded_at = None
        self.root = None
        self.log = None
        self.last_error = ''

    async def close(self):
        async with self.lock:
            await self._stop()

    async def _stop(self):
        process, self.process = self.process, None
        self.loaded, self.root, self.port = None, None, None
        self.loaded_at = None
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, 5)
        finally:
            if self.log:
                self.log.close()
                self.log = None

    async def _start(self, value, voice):
        signature = (value['package_dir'], value.get('device', 'cpu'))
        if self.process is not None and self.process.poll() is None and self.root == signature:
            return
        await self._stop()
        root = Path(value['package_dir']).resolve()
        work = config.DATA_DIR / 'local-tts'
        work.mkdir(parents=True, exist_ok=True)
        device = value.get('device', 'cpu')
        custom = {'version': 'v2', 'device': device, 'is_half': device == 'cuda',
                  't2s_weights_path': voice['gpt_weights'], 'vits_weights_path': voice['sovits_weights'],
                  'bert_base_path': str(root / 'GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large'),
                  'cnhuhbert_base_path': str(root / 'GPT_SoVITS/pretrained_models/chinese-hubert-base')}
        runtime_config = work / 'runtime.yaml'
        runtime_config.write_text(yaml.safe_dump({'custom': custom}, allow_unicode=True), encoding='utf-8')
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            self.port = reserved.getsockname()[1]
        self.log = (work / 'service.log').open('ab')
        try:
            self.process = subprocess.Popen(
                [str(root / 'runtime/python.exe'), '-I', '-B', '-u', '-X', 'utf8',
                 str(config.BASE_DIR / 'app/static/workers/local_tts_launcher.py'), str(root),
                 '-a', '127.0.0.1', '-p', str(self.port), '-c', str(runtime_config)],
                cwd=root, stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
                env={**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
                     'HF_HUB_DISABLE_TELEMETRY': '1', 'LINGXI_TTS_DEVICE': device},
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.root = signature
            deadline = time.monotonic() + 120
            async with httpx2.AsyncClient(timeout=2, follow_redirects=False) as client:
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        raise HTTPException(503, '本地语音进程启动失败，请查看 local-tts/service.log')
                    try:
                        response = await client.get(f'http://127.0.0.1:{self.port}/openapi.json')
                        if response.is_success:
                            self.loaded = (voice['gpt_weights'], voice['sovits_weights'])
                            self.loaded_at = time.time()
                            self.last_error = ''
                            return
                    except httpx2.HTTPError:
                        pass
                    await asyncio.sleep(0.3)
            raise HTTPException(504, '本地语音启动超过 120 秒，请检查模型和设备')
        except BaseException:
            await self._stop()
            raise

    async def synthesize(self, text, voice_id=''):
        if not text.strip():
            raise HTTPException(422, '请输入朗读文字')
        async with self.lock:
            value = settings()
            voice = select_voice(value, voice_id)
            try:
                await self._start(value, voice)
                async with httpx2.AsyncClient(timeout=__import__('app.modules',fromlist=['configured']).request_timeout('tts',180), follow_redirects=False) as client:
                    target = (voice['gpt_weights'], voice['sovits_weights'])
                    if self.loaded != target:
                        for endpoint, weights in zip(('set_gpt_weights', 'set_sovits_weights'), target):
                            response = await client.get(f'http://127.0.0.1:{self.port}/{endpoint}',
                                                        params={'weights_path': weights})
                            response.raise_for_status()
                        self.loaded = target
                        self.loaded_at = time.time()
                    payload = {'text': text, 'text_lang': voice.get('text_lang', 'auto'),
                               'ref_audio_path': voice['reference_audio'],
                               'prompt_text': voice['reference_text'], 'prompt_lang': voice['prompt_lang'],
                               'media_type': 'wav', 'streaming_mode': False,
                               'batch_size': 1, 'parallel_infer': False,
                               'text_split_method': 'cut5', 'speed_factor': value['speed']}
                    async with client.stream('POST', f'http://127.0.0.1:{self.port}/tts', json=payload) as response:
                        response.raise_for_status()
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_AUDIO:
                                raise HTTPException(502, '语音结果超过 24 MiB，请缩短文字')
                            chunks.append(chunk)
                    data = b''.join(chunks)
                    if not data.startswith(b'RIFF') or data[8:12] != b'WAVE':
                        raise HTTPException(502, '本地语音服务未返回有效 WAV 音频')
                    validate_synthesis(data)
                    self.last_error = ''
                    return data
            except asyncio.CancelledError:
                await self._stop()
                raise
            except (OSError, httpx2.HTTPError) as exc:
                self.last_error = '本地语音连接或合成失败，请检查权重、参考录音和服务日志'
                await self._stop()
                raise HTTPException(502, self.last_error) from exc
            except HTTPException as exc:
                self.last_error = str(exc.detail)
                await self._stop()
                raise


service = Service()


@catalog.router.get('/local-tts')
def status(include_engines=True):
    value = settings()
    try:
        module = catalog.get('module', 'tts')
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        module = {**native_module(), 'revision': 0}
    result={'settings': {**value,'engine':module.get('model',MODEL)}, 'voices': [voice_status(v) for v in value['voices']],
            'package_issues': package_issues(value), 'module_revision': module['revision'],
            'active': is_native(module) and module['enabled'],
            'running': service.process is not None and service.process.poll() is None,
            'last_error': service.last_error}
    if include_engines:
        from app.speech.registry import engines
        result['engines']=[item for item in engines()['items'] if item['kind']=='tts']
    return result


@catalog.router.put('/local-tts')
def save_settings(body: Settings):
    value = body.model_dump(exclude={'expected_revision','engine'})
    ids = [v['id'] for v in value['voices']]
    if len(set(ids)) != len(ids) or value['default_voice'] not in ids:
        raise HTTPException(422, '音色编号不能重复，默认音色必须存在')
    removed={v['id'] for v in settings()['voices']}-set(ids)
    if removed:
        for item in catalog.listing('module'):
            if item.get('voice') in removed:raise HTTPException(409,'音色仍被功能分配使用，请先更换')
        with catalog.read_connection() as db:
            for row in db.execute('SELECT document FROM character_cards'):
                if json.loads(row['document']).get('voice_ref') in removed:raise HTTPException(409,'音色仍被人物使用，请先更换人物音色')
    # Incomplete reference data may be saved, but never reported as synthesis-ready.
    return catalog.put('local-tts', 'settings', value, body.expected_revision)


class Activation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)


@catalog.router.post('/local-tts/activate')
def activate(body: Activation):
    value = settings()
    select_voice(value)
    return catalog.put('module', 'tts', {**native_module(), 'enabled': True}, body.expected_revision)


class Reference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    data: str = Field(max_length=12 * 1024 * 1024)


@catalog.router.post('/local-tts/reference')
def upload_reference(body: Reference):
    try:
        data = base64.b64decode(body.data, validate=True)
        duration = wav_duration(data)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(422, str(exc)) from exc
    folder = config.DATA_DIR / 'local-tts' / 'references'
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (uuid4().hex + '.wav')
    destination.write_bytes(data)
    return {'path': str(destination), 'duration': duration}


class Preview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    voice: str = Field(default='', max_length=60)
    text: str = Field(default='你好，我在这里。今天想聊些什么？', min_length=1, max_length=500)


@catalog.router.post('/local-tts/preview')
async def preview(body: Preview):
    from app.speech.contract import SynthesisRequest
    from app.speech.registry import engine
    data = await engine(MODEL).synthesize(SynthesisRequest(text=body.text, voice_id=body.voice))
    return Response(data, media_type='audio/wav', headers={'Cache-Control': 'no-store'})
