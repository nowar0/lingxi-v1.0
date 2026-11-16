"""Lazy owned inference, authenticated loopback, cancellable without replay."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import wave

import httpx2
from fastapi import HTTPException
from app import config, model_packages
from app.speech.contract import check_wav, MAX_AUDIO


def voice_configuration(identifier=None):
    from app import local_tts
    value=local_tts.settings()
    if identifier in ('cosyvoice2','indextts2.5'):
        configured=options(identifier)
        value={**value,**{key:configured[key] for key in ('voices','default_voice','speed') if configured.get(key) is not None}}
    result={}
    for voice in value['voices']:
        path=Path(voice.get('reference_audio',''))
        if path.is_file() and voice.get('reference_text','').strip():
            local_tts.wav_duration(path.read_bytes())
            result[voice['id']]={'reference_audio':str(path.resolve()),'reference_text':voice['reference_text']}
    return value,result


def options(identifier):
    from app import catalog
    try: return catalog.get('speech-engine',identifier.replace('.','_'))
    except HTTPException as error:
        if error.status_code!=404: raise
    return {'device':'cpu' if identifier!='indextts2.5' else 'cuda:0','revision':0,'hotwords':[]}


class OwnedEngine:
    def __init__(self,identifier):
        self.id=identifier;self.lock=asyncio.Lock();self.process=None;self.port=None;self.signature=None
        self.loaded_at=None;self.loading=False;self.last_error='';self.log=None;self.token=None
        self.epoch=0

    def health(self):
        issues=[];installed=False;runtime=True
        try:
            _,item=model_packages.model_root(self.id);installed=True
            if item.get('runtime')=='speech-pytorch': model_packages.runtime_root()
        except HTTPException as error: issues.append(str(error.detail));runtime=False
        value=options(self.id)
        running=bool(self.process and self.process.poll() is None and self.port)
        return {'engine':self.id,'model':self.id,'installed':installed,'runtime_ready':runtime,
                'ready':running,'running':running,'loading':self.loading,'device':value['device'],
                'loaded_at':self.loaded_at,'issues':issues,'last_error':self.last_error}

    async def _stop(self):
        self.epoch+=1
        process,self.process=self.process,None
        self.port=self.loaded_at=self.signature=self.token=None
        if process and process.poll() is None:
            process.terminate()
            try: await asyncio.to_thread(process.wait,5)
            except subprocess.TimeoutExpired:
                process.kill();await asyncio.to_thread(process.wait,5)
        if self.log: self.log.close();self.log=None

    async def close(self):
        # Cancellation/explicit stop must not wait for a model response to end.
        await self._stop()

    async def _start(self):
        root,item=model_packages.model_root(self.id);value=options(self.id)
        voice_settings,voices=voice_configuration(self.id) if self.id!='sensevoice-small' else ({}, {})
        signature=json.dumps([str(root),value,voices],sort_keys=True)
        if self.process and self.process.poll() is None and self.signature==signature and self.port: return
        await self._stop();self.loading=True
        launch_epoch=self.epoch
        try:
            root,item=await asyncio.to_thread(model_packages.verify,self.id)
            if launch_epoch!=self.epoch:raise asyncio.CancelledError('用户停止了语音加载')
            if self.id=='sensevoice-small':
                argv=[sys.executable,'--speech-worker'] if getattr(sys,'frozen',False) else [sys.executable,'-u',str(config.BASE_DIR/'app/static/workers/speech_worker.py')]
                runtime=None;paths=[]
            else:
                runtime,python=model_packages.runtime_root()
                argv=[str(python),'-I','-B','-u',str(config.BASE_DIR/'app/static/workers/speech_worker.py')]
                paths=[str(root/path) for path in item.get('import_paths',[])]
            directory=config.DATA_DIR/'local-speech'/self.id;directory.mkdir(parents=True,exist_ok=True)
            self.log=(directory/'service.log').open('ab')
            env={key:os.environ[key] for key in ('SYSTEMROOT','WINDIR','COMSPEC','PATH','TEMP','TMP','NUMBER_OF_PROCESSORS','USERPROFILE') if key in os.environ}
            env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',
                HF_HOME=str(directory/'hf-cache'),NLTK_DATA=str(runtime/'nltk_data') if runtime else str(directory/'nltk_data'),
                TOKENIZERS_PARALLELISM='false',CUDA_MODULE_LOADING='LAZY',PYTHONUTF8='1',
                MPLCONFIGDIR=str(directory/'matplotlib'),XDG_CACHE_HOME=str(directory/'cache'),TORCH_HOME=str(directory/'torch-cache'),MODELSCOPE_CACHE=str(directory/'modelscope-cache'))
            self.token=secrets.token_urlsafe(32)
            boot={'engine':self.id,'weights':str(root/item.get('weights_path','.')),'token':self.token,
                  'device':value['device'],'import_paths':paths,'voices':voices}
            self.process=subprocess.Popen(argv,cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=self.log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.process.stdin.write((json.dumps(boot,ensure_ascii=False)+'\n').encode());self.process.stdin.flush();self.process.stdin.close()
            line=await asyncio.wait_for(asyncio.to_thread(self.process.stdout.readline,8192),240)
            if not line:
                with (directory/'service.log').open('rb') as log:
                    log.seek(0,2);size=log.tell();log.seek(max(0,size-3000))
                    detail=log.read().decode('utf-8',errors='replace').strip().splitlines()
                raise ValueError(detail[-1][:500] if detail else '语音进程提前退出，请检查模型和设备设置')
            state=json.loads(line)
            if not state.get('ready') or not isinstance(state.get('port'),int) or not 1024<=state['port']<=65535: raise ValueError('语音进程未返回有效就绪状态')
            self.port=state['port'];self.signature=signature;self.loaded_at=time.time();self.last_error=''
        except BaseException as error:
            version=item.get('version')
            await self._stop()
            if version: model_packages.rollback_failed(self.id,version)
            self.last_error='加载 '+self.id+' 失败：'+str(getattr(error,'detail',error))[:400]+'；详见 local-speech/'+self.id+'/service.log'
            if isinstance(error,asyncio.CancelledError): raise
            raise HTTPException(503,self.last_error) from error
        finally: self.loading=False

    async def _request(self,path,body,limit):
        await self._start()
        chunks=[];size=0
        try:
            async with httpx2.AsyncClient(timeout=httpx2.Timeout(__import__('app.modules',fromlist=['configured']).request_timeout('stt' if self.id=='sensevoice-small' else 'tts',240),connect=5),trust_env=False) as client:
                async with client.stream('POST',f'http://127.0.0.1:{self.port}/{path}',json=body,
                    headers={'Authorization':'Bearer '+self.token}) as response:
                    async for chunk in response.aiter_bytes():
                        size+=len(chunk)
                        if size>limit: raise HTTPException(502,'本地语音结果超过限制')
                        chunks.append(chunk)
                    raw=b''.join(chunks)
                    if response.status_code!=200:
                        try: detail=json.loads(raw).get('error','语音推理失败')
                        except ValueError: detail='本地语音服务返回 HTTP '+str(response.status_code)
                        raise HTTPException(502,detail)
                    return raw
        except BaseException as error:
            self.last_error=str(getattr(error,'detail',error))[:500]
            await self._stop()
            if isinstance(error,(asyncio.CancelledError,HTTPException)): raise
            raise HTTPException(502,self.id+' 合成或识别失败，未自动重放；'+self.last_error) from error

    async def synthesize(self,request):
        if self.id=='sensevoice-small': raise HTTPException(422,'此引擎用于语音输入')
        if request.ref_audio or request.ref_text or request.lang: raise HTTPException(422,'参考音色由用户保存的配置提供，不接受逐次路径覆盖')
        value,voices=voice_configuration(self.id);voice=request.voice_id or value['default_voice']
        if voice not in voices: raise HTTPException(409,'请先保存3～10秒参考录音及原文')
        if request.stream: raise HTTPException(422,'流式请求请使用专用 synthesize-stream 接口；普通接口返回完整 WAV')
        async with self.lock:
            raw=await self._request('synthesize',{'text':request.text,'voice_id':voice,'speed':request.speed or value['speed'],'stream':False},MAX_AUDIO)
            from app.local_tts import validate_synthesis
            check_wav(raw);validate_synthesis(raw)
            return raw

    async def stream(self,request):
        value,voices=voice_configuration(self.id);voice=request.voice_id or value['default_voice']
        if self.id=='sensevoice-small' or voice not in voices or request.ref_audio or request.ref_text or request.lang:
            raise HTTPException(422,'请选择已保存的合成引擎和参考音色')
        async with self.lock:
            await self._start();finished=False
            try:
                async with httpx2.AsyncClient(timeout=__import__('app.modules',fromlist=['configured']).request_timeout('tts',240),trust_env=False) as client:
                    async with client.stream('POST',f'http://127.0.0.1:{self.port}/synthesize',
                        json={'text':request.text,'voice_id':voice,'speed':request.speed or value['speed'],'stream':True},
                        headers={'Authorization':'Bearer '+self.token}) as response:
                        if response.status_code!=200 or response.headers.get('content-type')!='application/x-lingxi-pcm': raise HTTPException(502,self.id+' 流式合成失败')
                        size=0
                        async for chunk in response.aiter_bytes():
                            size+=len(chunk)
                            if size>MAX_AUDIO+65536: raise HTTPException(502,'合成流超过限制')
                            yield chunk
                        finished=True
            finally:
                if not finished: await self._stop()

    async def transcribe(self,path):
        if self.id!='sensevoice-small': raise HTTPException(422,'此引擎用于语音输出')
        from app.media import LIMIT
        if path.stat().st_size>LIMIT: raise HTTPException(422,'录音超过15 MiB')
        data=await asyncio.to_thread(path.read_bytes)
        try:
            with wave.open(io.BytesIO(data),'rb') as incoming:
                valid=incoming.getnchannels()==1 and incoming.getframerate()==16000 and incoming.getsampwidth()==2 and incoming.getcomptype()=='NONE'
        except (wave.Error,EOFError): valid=False
        if not valid:
            from app.model_media_dependencies import resolve_media_binary
            process=await asyncio.create_subprocess_exec(str(resolve_media_binary('ffmpeg')),'-nostdin','-v','error','-i',str(path),'-t','121','-ac','1','-ar','16000','-f','wav','pipe:1',
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                data,stderr=await asyncio.wait_for(process.communicate(),30)
                if process.returncode or len(data)>4*1024*1024: raise HTTPException(422,'录音解码失败或超过120秒')
            finally:
                if process.returncode is None: process.kill();await process.wait()
        async with self.lock:
            result=json.loads(await self._request('transcribe',{'wav':base64.b64encode(data).decode()},65536))
            raw=result.get('text','');result['raw_text']=raw;result['corrections']=[]
            # SenseVoice CTC has no decoder hotword bias. Explicit user entries
            # are applied only as visible exact homophone post-corrections.
            hotwords=options(self.id).get('hotwords',[])
            for entry in hotwords:
                before,after=entry.split('=>',1)
                if before and before in result['text']:
                    result['text']=result['text'].replace(before,after)
                    result['corrections'].append({'from':before,'to':after})
            return result


async def shutdown():
    from app.speech.registry import _instances
    for item in list(_instances.values()):
        if isinstance(item,OwnedEngine): await item.close()
