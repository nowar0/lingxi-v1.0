"""Offline Chinese STT through an existing GPT-SoVITS/FunASR installation."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import time

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app import catalog, config, local_tts

MODEL = 'paraformer-zh-offline'
MODEL_DIRS = ('speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch',
              'speech_fsmn_vad_zh-cn-16k-common-pytorch',
              'punc_ct-transformer_zh-cn-common-vocab272727-pytorch')


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    engine:str=Field(default=MODEL,max_length=100)
    package_dir: str = Field(min_length=1, max_length=2000)
    device: str = Field(default='cpu', pattern=r'^(cpu|cuda)$')
    expected_revision: int = Field(default=0, ge=0)


def settings():
    try:
        return catalog.get('local-stt', 'settings')
    except HTTPException as error:
        if error.status_code != 404:
            raise
    return {'package_dir': local_tts.settings()['package_dir'], 'device': 'cpu', 'revision': 0}


def issues(value):
    root = Path(value['package_dir'])
    required = ['runtime/python.exe', 'runtime/ffmpeg.exe', 'runtime/ffprobe.exe']
    required += ['tools/asr/models/' + model + '/model.pt' for model in MODEL_DIRS]
    return ['缺少 ' + name for name in required if not (root / name).is_file()]


def native_module():
    return {'enabled': False, 'base_url': 'http://127.0.0.1', 'model': MODEL,
            'credential_ref': '', 'local': True, 'capabilities': ['stt'], 'voice': '',
            'timeout': 180, 'difficulty': 1, 'temperature': 0.7}


def is_native(item):
    return item.get('local') and item.get('model') == MODEL


class Service:
    def __init__(self):
        self.process = None
        self.log = None
        self.configuration = None
        self.loaded_at = None
        self.lock = asyncio.Lock()

    async def close(self):
        process, self.process = self.process, None
        log, self.log = self.log, None
        self.configuration = None
        self.loaded_at = None
        if process and process.poll() is None:
            if os.name == 'nt':
                await asyncio.to_thread(subprocess.run, ['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
            else:
                process.kill()
            await asyncio.to_thread(process.wait, 10)
        if log:
            log.close()

    async def read(self, timeout=None):
        if timeout is None:
            from app.modules import request_timeout
            timeout=request_timeout('stt',180)
        line = await asyncio.wait_for(asyncio.to_thread(self.process.stdout.readline), timeout)
        if not line:
            raise RuntimeError('离线语音进程已退出，请查看 local-stt/service.log')
        return json.loads(line)

    async def start(self, value):
        identity = (value['package_dir'], value['device'])
        if self.process and self.process.poll() is None and self.configuration == identity:
            return
        await self.close()
        root = Path(value['package_dir']).resolve()
        folder = config.DATA_DIR / 'local-stt'
        folder.mkdir(parents=True, exist_ok=True)
        self.log = (folder / 'service.log').open('ab')
        env = {**os.environ, 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
               'HF_HUB_DISABLE_TELEMETRY': '1', 'MODELSCOPE_OFFLINE': '1',
               'PATH': str(root / 'runtime') + os.pathsep + os.environ.get('PATH', '')}
        worker = config.BASE_DIR / 'app/static/workers/local_stt_worker.py'
        self.process = subprocess.Popen([str(root / 'runtime/python.exe'), '-I', '-B', '-u', '-X', 'utf8',
            str(worker), str(root), str(folder), value['device']], cwd=folder, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.configuration = identity
        if not (await self.read()).get('ready'):
            raise RuntimeError('离线语音模型未能加载')
        self.loaded_at = time.time()

    async def transcribe(self, path):
        async with self.lock:
            value = settings()
            missing = issues(value)
            if missing:
                raise HTTPException(409, '；'.join(missing))
            try:
                await self.start(value)
                self.process.stdin.write(json.dumps({'path': str(path)}, ensure_ascii=False) + '\n')
                self.process.stdin.flush()
                result = await self.read()
                if not result.get('ok'):
                    raise HTTPException(422, result.get('error', '离线识别失败'))
                return result
            except asyncio.CancelledError:
                # readline runs in a thread; terminate its own process before reusing the protocol.
                await asyncio.shield(self.close())
                raise
            except HTTPException:
                raise
            except (OSError, RuntimeError, ValueError, TimeoutError) as error:
                await self.close()
                raise HTTPException(502, str(error) or '离线语音超时，请查看 local-stt/service.log') from error


service = Service()


@catalog.router.get('/local-stt')
def status(include_engines=True):
    value = settings()
    try:
        module = catalog.get('module', 'stt')
    except HTTPException as error:
        if error.status_code != 404:
            raise
        module = {**native_module(), 'revision': 0}
    result={'settings': {**value,'engine':module.get('model',MODEL)}, 'issues': issues(value), 'active': bool(is_native(module) and module['enabled']),
            'module_revision': module['revision'], 'running': bool(service.process and service.process.poll() is None)}
    if include_engines:
        from app.speech.registry import engines
        result['engines']=[item for item in engines()['items'] if item['kind']=='stt']
    return result


@catalog.router.put('/local-stt')
def save(body: Settings):
    return catalog.put('local-stt', 'settings', body.model_dump(exclude={'expected_revision','engine'}), body.expected_revision)


class Activation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)


@catalog.router.post('/local-stt/activate')
def activate(body: Activation):
    missing = issues(settings())
    if missing:
        raise HTTPException(409, '；'.join(missing))
    return catalog.put('module', 'stt', {**native_module(), 'enabled': True}, body.expected_revision)
