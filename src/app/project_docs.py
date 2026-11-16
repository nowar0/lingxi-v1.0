"""Local project vector database, separate files/models from personal memory."""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import struct
import threading
from uuid import uuid4
from typing import Literal
import zipfile
import xml.etree.ElementTree as ET

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app import catalog, config, sessions, workspace

EXCLUDED = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.cache', 'dist', 'build', 'target', 'vendor'}
TEXT_TYPES = {'.txt','.md','.py','.js','.mjs','.ts','.tsx','.jsx','.json','.yaml','.yml','.toml','.csv','.sql','.html','.css','.rs','.go','.java','.c','.h','.cpp','.ps1','.sh','.log','.xml','.ini','.cfg','.rst'}
_encoders = {}
_encoder_lock = threading.RLock()
_locks = {}
_jobs = {}
_cancel_indices={}


@contextmanager
def connect():
    path = config.DATA_DIR / 'project-docs.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        import sqlite_vec
        db.enable_load_extension(True)
        try:
            sqlite_vec.load(db)
        finally:
            db.enable_load_extension(False)
        db.execute('PRAGMA foreign_keys=ON')
        yield db
        db.commit()
    except BaseException:
        db.rollback(); raise
    finally:
        db.close()


def init_table():
    with connect() as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS project_docs(
            id TEXT PRIMARY KEY, session_id INTEGER NOT NULL,path TEXT NOT NULL,workspace_id TEXT,
            workspace_revision INTEGER,external INTEGER NOT NULL,hash TEXT NOT NULL,status TEXT NOT NULL,
            size INTEGER NOT NULL,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(session_id,path));
            CREATE TABLE IF NOT EXISTS project_chunks(id INTEGER PRIMARY KEY,doc_id TEXT NOT NULL REFERENCES project_docs(id) ON DELETE CASCADE,
            session_id INTEGER NOT NULL,line_start INTEGER NOT NULL,line_end INTEGER NOT NULL,text TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS project_embeddings(chunk_id INTEGER PRIMARY KEY REFERENCES project_chunks(id) ON DELETE CASCADE,
            session_id INTEGER NOT NULL,model TEXT NOT NULL,dimension INTEGER NOT NULL,vector BLOB NOT NULL);
            CREATE INDEX IF NOT EXISTS project_docs_session ON project_docs(session_id);
            CREATE INDEX IF NOT EXISTS project_embeddings_scope ON project_embeddings(session_id,model);''')
        if 'mtime_ns' not in {row['name'] for row in db.execute('PRAGMA table_info(project_docs)')}:
            db.execute('ALTER TABLE project_docs ADD COLUMN mtime_ns INTEGER NOT NULL DEFAULT 0')


class Encoder:
    dimensions=512
    def __init__(self):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.root = config.BUNDLED_MODEL_ROOT / 'bge-small-zh-v1.5'
        manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        for name in ('model.onnx', 'tokenizer.json'):
            if hashlib.sha256((self.root / name).read_bytes()).hexdigest() != manifest['files'][name]:
                raise ValueError('项目嵌入模型指纹不符，请重新安装模型包')
        self.identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        self.prefix = manifest['query_prefix']
        self.tokenizer = Tokenizer.from_file(str(self.root / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=0, pad_token='[PAD]')
        options = ort.SessionOptions(); options.intra_op_num_threads=2; options.inter_op_num_threads=1
        self.model = ort.InferenceSession(str(self.root / 'model.onnx'), options, providers=['CPUExecutionProvider'])
    def encode(self, texts, query=False):
        import numpy as np
        values = self.tokenizer.encode_batch([self.prefix+t if query else t for t in texts])
        inputs = {'input_ids': np.array([x.ids for x in values],dtype='int64'),
                  'attention_mask': np.array([x.attention_mask for x in values],dtype='int64'),
                  'token_type_ids': np.array([x.type_ids for x in values],dtype='int64')}
        result = self.model.run(None, {x.name: inputs[x.name] for x in self.model.get_inputs()})[0]
        result = (result[:,0,:] if result.ndim==3 else result).astype('float32')
        result /= np.maximum(np.linalg.norm(result,axis=1,keepdims=True),1e-12)
        if result.shape[1]!=512 or not np.isfinite(result).all():
            raise ValueError('项目嵌入模型返回无效向量')
        return result


def encoder(identifier='project-bge-small-zh-v1.5'):
    with _encoder_lock:
        if identifier not in _encoders:
            from app.project_encoder import QwenEncoder
            _encoders[identifier] = QwenEncoder() if identifier=='project-qwen3-embedding-0.6b' else Encoder()
        return _encoders[identifier]


class Settings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    enabled: bool=False
    embedding_model:Literal['project-bge-small-zh-v1.5','project-qwen3-embedding-0.6b']='project-bge-small-zh-v1.5'
    workspace_id: str=Field(default='',max_length=100)
    external_files: list[str]=Field(default_factory=list,max_length=50)
    expected_revision: int=Field(default=0,ge=0)


def settings(sid):
    session=sessions.get_session(sid)
    if not session or session['mode']!='work':
        raise HTTPException(409,'项目资料库仅用于当前工作会话')
    try:
        return {**Settings().model_dump(exclude={'expected_revision'}),**catalog.get('project-docs',f'session_{sid}')}
    except HTTPException as error:
        if error.status_code!=404: raise
    return {**Settings().model_dump(exclude={'expected_revision'}),'revision':0}


def safe_external(value):
    path=Path(value)
    if not path.is_absolute() or not path.is_file():
        raise HTTPException(422,'外部资料必须是用户明确选择的绝对文件路径')
    for candidate in (path,*path.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise HTTPException(422,'外部资料不通过链接或联接点读取')
    return path.resolve()


@catalog.router.get('/project-docs/settings')
def read_settings(session_id:int):
    value=settings(session_id)
    return {'settings':value,'model':value['embedding_model'],'database':'project-docs.sqlite3',
            'index':status(session_id)}


@catalog.router.put('/project-docs/settings')
def save_settings(session_id:int,body:Settings):
    previous=settings(session_id)
    if body.workspace_id:
        item=catalog.get('workspace',body.workspace_id)
        if item['session_id']!=session_id: raise HTTPException(409,'工作目录不属于当前会话')
    external=[str(safe_external(value)) for value in body.external_files]
    if len(set(external))!=len(external): raise HTTPException(422,'外部资料路径重复')
    document={**body.model_dump(exclude={'expected_revision'}),'external_files':external}
    if previous['revision']!=body.expected_revision:raise HTTPException(409,'资料范围已变化，请刷新')
    if all(previous[k]==v for k,v in document.items()):return {'settings':previous,'index':status(session_id)}
    saved=catalog.put('project-docs',f'session_{session_id}',document,body.expected_revision)
    if any(previous[k]!=document[k] for k in ('embedding_model','workspace_id','external_files')):
        for value in _jobs.values():
            if value['session_id']==session_id and value['status'] in ('queued','indexing'):_cancel_indices[value['id']].set()
        with connect() as db:
            db.execute('DELETE FROM project_docs WHERE session_id=?',(session_id,))
    return {'settings':saved,'index':status(session_id)}


def source_files(sid,cfg,counts):
    for value in cfg['external_files']:
        yield safe_external(value),'',0,True
    if cfg['workspace_id']:
        item=catalog.get('workspace',cfg['workspace_id'])
        if item['session_id']!=sid: raise ValueError('工作目录作用域变化')
        root=Path(item['path']).resolve()
        seen=0
        for directory,dirs,files in os.walk(root,followlinks=False):
            dirs[:]=[name for name in dirs if name not in EXCLUDED and not (Path(directory)/name).is_symlink()
                     and not (Path(directory)/name).is_junction()]
            for name in files:
                seen+=1
                if seen>2000:
                    counts['limited']=True; return
                path=Path(directory)/name
                relative=path.relative_to(root).as_posix()
                try:
                    _,path=workspace.resolve(item['id'],sid,relative)
                    yield path,item['id'],item['revision'],False
                except (ValueError,OSError): counts['skipped']+=1


def read_text(path):
    if path.stat().st_size>2*1024*1024: raise ValueError('文件超过2 MiB，已跳过')
    data=path.read_bytes(); suffix=path.suffix.lower()
    if suffix=='.pdf':
        import io
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(data))
        if len(reader.pages)>200: raise ValueError('PDF超过200页')
        text='\n'.join(page.extract_text() or '' for page in reader.pages)
    elif suffix=='.docx':
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entry=archive.getinfo('word/document.xml')
            if entry.file_size>20*1024*1024: raise ValueError('DOCX正文超过限制')
            root=ET.fromstring(archive.read(entry))
            text='\n'.join(''.join(node.itertext()) for node in root.iter('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'))
    elif suffix in TEXT_TYPES:
        if b'\0' in data[:4096]: raise ValueError('二进制内容已跳过')
        text=data.decode('utf-8-sig')
    else: raise ValueError('不支持的资料类型，已跳过')
    if len(text)>200000: raise ValueError('正文超过20万字符')
    return data,text


def chunks(text):
    lines=text.splitlines(); current=[]; start=1; length=0
    for index,line in enumerate(lines,1):
        # Oversized code/minified lines remain bounded with the same source line.
        for part in [line[i:i+1200] for i in range(0,len(line),1200)] or ['']:
            if current and length+len(part)>1200:
                yield start,index-1 if index>start else index,'\n'.join(current)
                current=[];length=0;start=index
            if not current: start=index
            current.append(part);length+=len(part)+1
    if current: yield start,len(lines),'\n'.join(current)


def index(sid,job_id):
    lock=_locks.setdefault(sid,threading.Lock())
    with lock:
        state=_jobs[job_id];state.update(status='indexing',indexed=0,skipped=0,limited=False,issues=[])
        try:
            cfg=settings(sid)
            if not cfg['enabled']: raise ValueError('资料库未开启')
            embed=encoder(cfg['embedding_model']);seen=set();total_chunks=0
            for path,wid,revision,external in source_files(sid,cfg,state):
                try:
                    if _cancel_indices.get(job_id,threading.Event()).is_set():raise InterruptedError('索引已停止，已有片段保留')
                    if settings(sid)['revision']!=cfg['revision']: raise RuntimeError('资料范围已修改，请重新索引')
                    if not state.get('full',True):
                        with connect() as db:
                            cached=db.execute('SELECT * FROM project_docs WHERE session_id=? AND path=?',(sid,str(path))).fetchone()
                            valid=db.execute('SELECT 1 FROM project_embeddings WHERE session_id=? AND model=? LIMIT 1',(sid,embed.identity)).fetchone()
                        stat=path.stat();seen.add(str(path))
                        if cached and valid and cached['mtime_ns']==stat.st_mtime_ns and cached['size']==stat.st_size and cached['workspace_revision']==revision:continue
                    data,text=read_text(path); fingerprint=hashlib.sha256(data).hexdigest(); seen.add(str(path))
                    with connect() as db:
                        old=db.execute('SELECT * FROM project_docs WHERE session_id=? AND path=?',(sid,str(path))).fetchone()
                        valid=db.execute('SELECT 1 FROM project_embeddings e JOIN project_chunks c ON e.chunk_id=c.id WHERE c.doc_id=? AND e.model=? LIMIT 1',(old['id'] if old else '',embed.identity)).fetchone()
                    if old and old['hash']==fingerprint and old['workspace_revision']==revision and valid:
                        with connect() as db:db.execute('UPDATE project_docs SET mtime_ns=? WHERE id=?',(path.stat().st_mtime_ns,old['id']))
                        continue
                    pieces=list(chunks(text))[:200]
                    total_chunks+=len(pieces)
                    if total_chunks>20000:
                        state['limited']=True;break
                    vectors=[]
                    for offset in range(0,len(pieces),16):
                        with _encoder_lock:
                            vectors.extend(embed.encode([part[2] for part in pieces[offset:offset+16]]))
                    if hashlib.sha256(path.read_bytes()).hexdigest()!=fingerprint: raise ValueError('资料在索引期间变化')
                    with connect() as db:
                        identifier=old['id'] if old else uuid4().hex
                        if old: db.execute('DELETE FROM project_docs WHERE id=?',(identifier,))
                        db.execute('INSERT INTO project_docs(id,session_id,path,workspace_id,workspace_revision,external,hash,status,size,mtime_ns) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (identifier,sid,str(path),wid,revision,int(external),fingerprint,'indexed',len(data),path.stat().st_mtime_ns))
                        for (first,last,part),vector in zip(pieces,vectors):
                            cid=db.execute('INSERT INTO project_chunks(doc_id,session_id,line_start,line_end,text) VALUES(?,?,?,?,?)',
                                (identifier,sid,first,last,part)).lastrowid
                            db.execute('INSERT INTO project_embeddings VALUES(?,?,?,?,?)',(cid,sid,embed.identity,embed.dimensions,struct.pack('<'+str(embed.dimensions)+'f',*vector)))
                    state['indexed']+=1
                except (RuntimeError,InterruptedError): raise
                except Exception as error:
                    state['skipped']+=1
                    if len(state['issues'])<30: state['issues'].append({'path':str(path),'reason':str(error)[:200]})
            # Remove cached sources that are no longer authorized or present.
            if not state['limited']:
                with connect() as db:
                    for row in db.execute('SELECT id,path FROM project_docs WHERE session_id=?',(sid,)).fetchall():
                        if row['path'] not in seen: db.execute('DELETE FROM project_docs WHERE id=?',(row['id'],))
            state['status']='complete'
        except Exception as error:
            state.update(status='cancelled' if isinstance(error,InterruptedError) else 'failed',error=str(error)[:500])


@catalog.router.post('/project-docs/index')
async def start_index(session_id:int,full:bool=True):
    cfg=settings(session_id)
    if not cfg['enabled']: raise HTTPException(409,'请先开启当前工作会话的资料库')
    existing=next((value for value in _jobs.values() if value['session_id']==session_id and value['status'] in ('queued','indexing')),None)
    if existing: return existing
    for key,value in list(_jobs.items()):
        if value['session_id']==session_id and value['status'] not in ('queued','indexing'):
            _jobs.pop(key,None);_cancel_indices.pop(key,None)
    identifier=uuid4().hex
    state={'id':identifier,'session_id':session_id,'status':'queued','indexed':0,'skipped':0,'issues':[],'full':full}
    _jobs[identifier]=state
    _cancel_indices[identifier]=threading.Event()
    asyncio.create_task(asyncio.to_thread(index,session_id,identifier))
    return dict(state)


@catalog.router.get('/project-docs/status')
def status(session_id:int):
    with connect() as db:
        rows=[dict(row) for row in db.execute('SELECT * FROM project_docs WHERE session_id=? ORDER BY path LIMIT 2000',(session_id,))]
    latest=next((value for value in reversed(list(_jobs.values())) if value['session_id']==session_id),None)
    return {'documents':rows,'job':dict(latest) if latest else None}


def search(sid,query,top_k=5):
    cfg=settings(sid)
    if not cfg['enabled']: raise ValueError('当前工作会话的项目资料库未开启')
    if not query.strip() or len(query)>500 or not 1<=top_k<=20: raise ValueError('检索词限500字，结果数限1–20')
    embed=encoder(cfg['embedding_model'])
    with _encoder_lock: vector=struct.pack('<'+str(embed.dimensions)+'f',*embed.encode([query],query=True)[0])
    with connect() as db:
        semantic=db.execute('''SELECT c.*,d.path,d.hash,d.workspace_id,d.workspace_revision,d.external,
            vec_distance_cosine(e.vector,?) AS distance FROM project_embeddings e
            JOIN project_chunks c ON c.id=e.chunk_id JOIN project_docs d ON d.id=c.doc_id
            WHERE e.session_id=? AND e.model=? ORDER BY distance LIMIT 50''',(vector,sid,embed.identity)).fetchall()
        terms=re.findall(r'[A-Za-z_][\w.-]{1,60}|[\u4e00-\u9fff]{2,12}',query)[:12]
        lexical=[]
        if terms:
            clauses=' OR '.join('instr(lower(c.text),lower(?))>0' for _ in terms)
            lexical=db.execute(f'''SELECT c.*,d.path,d.hash,d.workspace_id,d.workspace_revision,d.external,1.0 AS distance
                FROM project_chunks c JOIN project_docs d ON d.id=c.doc_id WHERE c.session_id=? AND ({clauses}) LIMIT 50''',(sid,*terms)).fetchall()
    candidates={row['id']:dict(row) for row in [*lexical,*semantic]}
    authorized={str(Path(value)) for value in cfg['external_files']}
    hits=[]
    for row in candidates.values():
        try:
            if row['external']:
                if row['path'] not in authorized: continue
                path=safe_external(row['path'])
            else:
                if row['workspace_id']!=cfg['workspace_id']: continue
                item=catalog.get('workspace',row['workspace_id'])
                if item['session_id']!=sid or item['revision']!=row['workspace_revision']: continue
                _,path=workspace.resolve(item['id'],sid,Path(row['path']).relative_to(item['path']).as_posix())
            if not path.is_file() or path.stat().st_size>2*1024*1024 or hashlib.sha256(path.read_bytes()).hexdigest()!=row['hash']: continue
            matches=sum(term.lower() in row['text'].lower() for term in terms)
            row['score']=.75*(1-float(row['distance']))+.25*min(1,matches/max(1,len(terms)))
            hits.append({key:row[key] for key in ('id','path','hash','line_start','line_end','text','score')})
        except (ValueError,OSError,HTTPException): continue
    if settings(sid)['revision']!=cfg['revision']: raise ValueError('检索范围已变化，请重新检索')
    return {'hits':sorted(hits,key=lambda value:-value['score'])[:top_k],
            'scope':'仅当前工作会话资料，不读取或写入陪伴记忆','model':cfg['embedding_model'],'hybrid':True}


@catalog.router.get('/project-docs/search')
def search_api(session_id:int,query:str,top_k:int=5):
    return search(session_id,query,top_k)


async def tick():
    for item in catalog.listing('project-docs'):
        if item.get('enabled'):
            try: await start_index(int(item['id'].removeprefix('session_')),full=False)
            except (ValueError,HTTPException): pass


def shutdown():
    for event in list(_cancel_indices.values()):event.set()
    for item in list(_encoders.values()):
        close=getattr(item,'close',None)
        if close:close()
    _encoders.clear()
