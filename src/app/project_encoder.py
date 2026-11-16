"""Optional encoder in the owned PyTorch interpreter, never personal memory."""
import hashlib,json,os,queue,subprocess,threading
from app import config,model_packages
class QwenEncoder:
    dimensions=1024
    def __init__(self):
        directory,item=model_packages.verify('project-qwen3-embedding-0.6b')
        runtime,python=model_packages.runtime_root()
        self.identity='qwen3:'+item['version'];self.process=None;self.log=None;self.output=queue.Queue()
        env={key:os.environ[key] for key in ('SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE') if key in os.environ}
        env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false')
        folder=config.DATA_DIR/'project-docs';folder.mkdir(exist_ok=True)
        self.log=(folder/'qwen-encoder.log').open('ab')
        try:
            self.process=subprocess.Popen([str(python),'-I','-B','-u',str(config.BASE_DIR/'app/static/workers/embedding_worker.py')],
                cwd=directory,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.process.stdin.write((json.dumps({'weights':str(directory)})+'\n').encode());self.process.stdin.flush()
            def receive():
                try:
                    for line in iter(self.process.stdout.readline,b''):self.output.put(line)
                finally:self.output.put(b'')
            threading.Thread(target=receive,daemon=True).start()
            if not self.read().get('ready'):raise ValueError('Qwen3 嵌入模型未就绪')
        except BaseException:self.close();raise
    def read(self):
        try:raw=self.output.get(timeout=120)
        except queue.Empty:self.close();raise ValueError('项目嵌入进程超时，已停止；请查看 project-docs/qwen-encoder.log')
        if not raw:raise ValueError('项目嵌入进程退出，请查看 project-docs/qwen-encoder.log')
        return json.loads(raw)
    def encode(self,texts,query=False):
        import numpy as np
        if self.process.poll() is not None:raise ValueError('项目嵌入进程已停止，请重新索引')
        self.process.stdin.write((json.dumps({'texts':texts,'query':query},ensure_ascii=False)+'\n').encode());self.process.stdin.flush()
        value=self.read()
        if value.get('error'):raise ValueError(value['error'])
        vectors=np.array(value['vectors'],dtype='float32')
        if vectors.shape!=(len(texts),1024) or not np.isfinite(vectors).all():raise ValueError('嵌入向量无效')
        return vectors
    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        if self.log:self.log.close();self.log=None
