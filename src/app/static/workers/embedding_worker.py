"""Fixed offline Qwen3 encoder. Private JSON lines; no tools or user paths."""
import contextlib,json,os,socket,sys
def main():
    control=sys.stdout
    boot=json.loads(sys.stdin.readline())
    def denied(*args,**kwargs):raise OSError('Project embedding is offline')
    socket.socket.connect=denied
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        import torch.nn.functional as F
        from transformers import AutoModel,AutoTokenizer
        torch.set_num_threads(4)
        tokenizer=AutoTokenizer.from_pretrained(boot['weights'],padding_side='left',local_files_only=True,trust_remote_code=False)
        model=AutoModel.from_pretrained(boot['weights'],local_files_only=True,trust_remote_code=False,torch_dtype=torch.float32).eval()
    control.write('{"ready":true,"dimensions":1024}\n');control.flush()
    for line in sys.stdin:
        try:
            body=json.loads(line);texts=body['texts']
            if not isinstance(texts,list) or not 1<=len(texts)<=16 or any(not isinstance(text,str) or len(text)>5000 for text in texts):raise ValueError('Invalid bounded embedding batch')
            if body.get('query'):texts=['Instruct: Given a Chinese project question, retrieve relevant source code and documents.\nQuery: '+text for text in texts]
            with contextlib.redirect_stdout(sys.stderr),torch.inference_mode():
                tokens=tokenizer(texts,padding=True,truncation=True,max_length=512,return_tensors='pt')
                output=model(**tokens).last_hidden_state[:,-1,:]
                vectors=F.normalize(output,p=2,dim=1).cpu().numpy()
                if vectors.shape!=(len(texts),1024):raise ValueError('Embedding dimensions mismatch')
            control.write(json.dumps({'vectors':vectors.tolist()},allow_nan=False)+'\n');control.flush()
        except Exception as error:control.write(json.dumps({'error':str(error)[:500]})+'\n');control.flush()
if __name__=='__main__':main()
