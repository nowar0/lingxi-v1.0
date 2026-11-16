"""Owned local speech worker. Input config only comes from the host over stdin.

No inherited user credentials, runtime downloads, arbitrary commands or plugins.
The authenticated loopback protocol is private; model text cannot select paths.
"""
import base64
import contextlib
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import io
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import sys
import threading
import time
import wave

MAX_AUDIO=24*1024*1024


def main():
    control=sys.stdout
    configuration=json.loads(sys.stdin.readline())
    identifier=configuration['engine'];weights=Path(configuration['weights']);voices=configuration.get('voices',{})
    token=configuration['token'];device=configuration.get('device','cpu')
    for directory in configuration.get('import_paths',[]):
        sys.path.insert(0,directory)
    # Standard-library downloads and third-party telemetry are denied during
    # inference. All required assets must be provided by the package installer.
    original_connect=socket.socket.connect
    def local_connect(self,address):
        try: allowed=ipaddress.ip_address(address[0]).is_loopback
        except ValueError: allowed=address[0].lower()=='localhost'
        if not allowed: raise OSError('Speech inference is offline; required assets must be installed first')
        return original_connect(self,address)
    socket.socket.connect=local_connect
    started=time.time()
    with contextlib.redirect_stdout(sys.stderr):
        if identifier=='sensevoice-small':
            import numpy as np
            import sherpa_onnx
            model=sherpa_onnx.OfflineRecognizer.from_sense_voice(model=str(weights/'model.int8.onnx'),tokens=str(weights/'tokens.txt'),
                use_itn=True,num_threads=2,provider='cpu',language=configuration.get('language','auto'),debug=False)
            vad=None
            if (weights/'silero_vad.onnx').is_file():
                vad_config=sherpa_onnx.VadModelConfig()
                vad_config.silero_vad.model=str(weights/'silero_vad.onnx')
                vad_config.silero_vad.min_silence_duration=.25
                vad_config.silero_vad.min_speech_duration=.15
                vad_config.sample_rate=16000
                vad=sherpa_onnx.VoiceActivityDetector(vad_config,buffer_size_in_seconds=130)
            rate=16000
        elif identifier=='cosyvoice2':
            import torch
            torch.set_num_threads(4)
            from cosyvoice.cli.cosyvoice import CosyVoice2
            # Upstream picks CUDA based on availability. Honor an explicit CPU
            # test/profile without modifying another interpreter or installation.
            if device=='cpu': torch.cuda.is_available=lambda:False
            model=CosyVoice2(str(weights),load_jit=False,load_trt=False,load_vllm=False,fp16=device.startswith('cuda'))
            for key,voice in voices.items():
                model.add_zero_shot_spk(voice['reference_text'],voice['reference_audio'],key)
            rate=model.sample_rate
        elif identifier in ('indextts2','indextts2.5'):
            import torch
            torch.set_num_threads(4)
            if device.startswith('cuda') and not torch.cuda.is_available(): raise RuntimeError('GPU运行环境不可用；IndexTTS建议独立显卡')
            if identifier=='indextts2.5':
                from indextts.infer_v2_5 import IndexTTS2
                model=IndexTTS2(cfg_path=str(weights/'config.yaml'),model_dir=str(weights),device=device,
                    use_bf16=device.startswith('cuda'),use_cuda_kernel=False,use_deepspeed=False,use_qwen_emo=False)
            else:
                from indextts.infer_v2 import IndexTTS2
                model=IndexTTS2(cfg_path=str(weights/'config.yaml'),model_dir=str(weights),device=device,
                    use_fp16=device.startswith('cuda'),use_cuda_kernel=False,use_deepspeed=False)
            rate=22050
        else: raise ValueError('Unregistered owned speech engine')
    started=time.time()
    lock=threading.Lock()

    def synthesize(body):
        voice=voices.get(body.get('voice_id',''))
        if not voice: raise ValueError('此引擎没有已批准的参考音色')
        text=body.get('text','')
        if not isinstance(text,str) or not text.strip() or len(text)>20000: raise ValueError('朗读文字为空或过长')
        speed=float(body.get('speed',1))
        if not .5<=speed<=2: raise ValueError('语速超出范围')
        with contextlib.redirect_stdout(sys.stderr):
            if identifier=='cosyvoice2':
                import numpy as np
                for result in model.inference_cross_lingual(text,'',zero_shot_spk_id=body['voice_id'],
                        stream=bool(body.get('stream')),speed=speed,text_frontend=True):
                    audio=result['tts_speech'].detach().float().cpu().numpy().reshape(-1)
                    if not np.isfinite(audio).all(): raise ValueError('模型返回无效音频')
                    yield rate,(np.clip(audio,-1,1)*32767).astype('<i2').tobytes()
            else:
                import numpy as np
                kwargs={'spk_audio_prompt':voice['reference_audio'],'text':text,'output_path':None,
                        'stream_return':False,'verbose':False}
                if identifier=='indextts2.5': kwargs.update(lang='ZH',duration_factor=1/speed)
                result=model.infer(**kwargs)
                sr,audio=result
                audio=np.asarray(audio).reshape(-1)
                if not np.isfinite(audio).all(): raise ValueError('模型返回无效音频')
                if audio.dtype.kind=='f': audio=(np.clip(audio,-1,1)*32767).astype('<i2')
                else: audio=np.clip(audio,-32768,32767).astype('<i2')
                yield int(sr),audio.tobytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def permitted(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                self.send_error(403);return False
            return True
        def do_GET(self):
            if not self.permitted(): return
            if self.path!='/health': self.send_error(404);return
            raw=json.dumps({'ready':True,'engine':identifier,'model':identifier,'device':device,'loaded_at':started}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
            self.end_headers();self.wfile.write(raw)
        def do_POST(self):
            if not self.permitted(): return
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=36*1024*1024: self.send_error(413);return
            sent=False
            try:
                body=json.loads(self.rfile.read(length))
                with lock:
                    if self.path=='/transcribe' and identifier=='sensevoice-small':
                        import numpy as np
                        data=base64.b64decode(body['wav'],validate=True)
                        if len(data)>8*1024*1024: raise ValueError('语音输入过大')
                        with wave.open(io.BytesIO(data),'rb') as incoming:
                            if incoming.getnchannels()!=1 or incoming.getsampwidth()!=2 or incoming.getframerate()!=16000:
                                raise ValueError('识别需要16k单声道PCM WAV')
                            samples=np.frombuffer(incoming.readframes(incoming.getnframes()),dtype='<i2').astype('float32')/32768
                        if len(samples)>16000*120: raise ValueError('语音输入超过120秒')
                        segments=[]
                        if vad is not None:
                            vad.reset()
                            for offset in range(0,len(samples),512): vad.accept_waveform(samples[offset:offset+512])
                            vad.flush()
                            while not vad.empty():
                                segments.append(np.array(vad.front.samples,copy=True));vad.pop()
                        elif np.max(np.abs(samples),initial=0)>.005: segments=[samples]
                        texts=[];language='auto'
                        for segment in segments:
                            stream=model.create_stream();stream.accept_waveform(16000,segment);model.decode_stream(stream)
                            result=stream.result;texts.append(result.text);language=getattr(result,'lang',language)
                        raw=json.dumps({'ok':True,'text':' '.join(texts),'duration':len(samples)/16000,'lang':language},ensure_ascii=False).encode()
                        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
                        self.end_headers();self.wfile.write(raw)
                    elif self.path=='/synthesize' and identifier!='sensevoice-small':
                        chunks=synthesize(body);first=next(chunks);sample_rate=first[0]
                        if body.get('stream'):
                            meta=json.dumps({'sample_rate':sample_rate,'channels':1,'sample_width':2}).encode()
                            self.send_response(200);self.send_header('Content-Type','application/x-lingxi-pcm');self.send_header('Connection','close');self.end_headers();sent=True
                            self.wfile.write(struct.pack('!I',len(meta))+meta);self.wfile.flush()
                            import itertools
                            size=0
                            for sr,pcm in itertools.chain([first],chunks):
                                if sr!=sample_rate: raise ValueError('合成采样率发生变化')
                                size+=len(pcm)
                                if size>MAX_AUDIO: raise ValueError('音频超过24 MiB')
                                self.wfile.write(struct.pack('!I',len(pcm))+pcm);self.wfile.flush()
                            self.wfile.write(struct.pack('!I',0));self.wfile.flush();self.close_connection=True
                        else:
                            pcm=bytearray(first[1])
                            for sr,part in chunks:
                                if sr!=sample_rate: raise ValueError('合成采样率发生变化')
                                pcm.extend(part)
                                if len(pcm)>MAX_AUDIO: raise ValueError('音频超过24 MiB')
                            output=io.BytesIO()
                            with wave.open(output,'wb') as audio:
                                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(sample_rate);audio.writeframes(bytes(pcm))
                            raw=output.getvalue()
                            self.send_response(200);self.send_header('Content-Type','audio/wav');self.send_header('Content-Length',str(len(raw)))
                            self.end_headers();self.wfile.write(raw)
                    else: self.send_error(404)
            except (BrokenPipeError,ConnectionResetError): pass
            except Exception as error:
                print(type(error).__name__+': '+str(error),file=sys.stderr,flush=True)
                if sent:
                    self.close_connection=True
                    return
                try:
                    raw=json.dumps({'error':str(error)[:800]},ensure_ascii=False).encode()
                    self.send_response(422);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)))
                    self.end_headers();self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    control.write(json.dumps({'ready':True,'port':server.server_port,'engine':identifier,'sample_rate':rate})+'\n');control.flush()
    server.serve_forever()


if __name__=='__main__':
    main()
