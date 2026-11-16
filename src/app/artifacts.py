"""One identity for attachment originals, previews and task outputs."""
import base64
import hashlib
from datetime import datetime,timezone
from pathlib import Path
from fastapi import APIRouter,HTTPException,Query
from pydantic import BaseModel,ConfigDict,Field
from app import catalog,media,sessions,workspace,workspace_ui
from app.db import connect

router=APIRouter(prefix='/v9/artifacts')
EXTENSIONS={'.txt':'text/plain','.md':'text/markdown','.markdown':'text/markdown','.csv':'text/csv','.json':'application/json',
            '.html':'text/html','.htm':'text/html','.pdf':'application/pdf','.zip':'application/zip',
            '.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp',
            **{extension:mime for mime,extension in media.attachment_store.MODEL_INPUT_TYPES.items()}}

CODE_EXTENSIONS={'.py','.js','.jsx','.ts','.tsx','.css','.scss','.toml','.yaml','.yml','.xml','.sql','.sh','.ps1','.bat','.ini','.cfg','.log','.rs','.go','.java','.c','.h','.cpp','.cs','.vue','.svelte','.jsonl'}
EXTENSIONS.update({extension:'text/plain' for extension in CODE_EXTENSIONS})


def file_mime(path,raw):
    mime=EXTENSIONS.get(path.suffix.lower(),'application/octet-stream')
    if mime in media.attachment_store.MODEL_INPUT_TYPES:
        # Known new formats must not be silently downgraded after a failed safety
        # check: a workspace .docx/.mp4 is the same upload boundary as a client file.
        media.verify_type(raw,mime,path.name)
    else:
        try:media.verify_type(raw,mime)
        except HTTPException:mime='application/octet-stream'
    return mime


def kind(mime):
    if mime.startswith('image/'):return 'image'
    if mime.startswith('audio/'):return 'audio'
    return {'text/markdown':'markdown','text/html':'html','application/pdf':'pdf','application/zip':'zip',
            'text/plain':'text','text/csv':'text','application/json':'text'}.get(mime,'download')


def owner(sid):
    session=sessions.get_session(sid)
    if not session:raise HTTPException(404,'会话不存在')
    return session


def describe(row,verified=False):
    try:meta=catalog.get('artifact',row['id'])
    except HTTPException as error:
        if error.status_code!=404:raise
        meta={'revision':0,'version':1,'family_id':row['id'],'origin':{'kind':'unknown'}}
    if meta.get('session_id',row['session_id'])!=row['session_id']:raise HTTPException(409,'产物来源归属异常，请保留原件并核对')
    try:family=catalog.get('artifact-family',meta['family_id'])
    except HTTPException as error:
        if error.status_code!=404:raise
        family={'revision':0,'current_id':row['id']}
    return {**row,**meta,'id':row['id'],'session_id':row['session_id'],'preview_kind':kind(row['mime']),
            'url':f"/v6/media/{row['id']}?session_id={row['session_id']}",'status':'ready' if verified else 'registered',
            'integrity':'verified' if verified else 'check_on_open','family_revision':family['revision'],'latest_id':family['current_id'],
            **media.attachment_store.model_input_status(row['mime'])}


@router.get('')
def listing(session_id:int=Query(gt=0),offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=100),query:str=Query('',max_length=200)):
    owner(session_id)
    with connect() as db:
        rows=db.execute("SELECT * FROM media_files WHERE session_id=? AND instr(lower(name),lower(?))>0 ORDER BY rowid DESC LIMIT ? OFFSET ?",(session_id,query,limit+1,offset)).fetchall()
    return {'items':[describe(dict(row)) for row in rows[:limit]],'more':len(rows)>limit,'next_offset':offset+limit if len(rows)>limit else None}


@router.get('/{fid}')
def get(fid:str,session_id:int=Query(gt=0)):
    row,_=media.read(fid,session_id)
    return describe(row,True)


def register(db,saved,name,origin,*,family_id=None,version=1,expected_family=None):
    family=family_id or saved['id']
    if family_id:
        try:current=catalog.get('artifact-family',family)
        except HTTPException as error:
            if error.status_code!=404:raise
            current={'revision':0,'version':1,'session_id':saved['session_id']}
        if current['revision']!=expected_family or current['session_id']!=saved['session_id']:raise HTTPException(409,'产物版本已变化，请重新读取后保存')
        version=current['version']+1
    doc={'session_id':saved['session_id'],'media_id':saved['id'],'family_id':family_id or saved['id'],
         'version':version,'name':name,'origin':origin,'created_at':datetime.now(timezone.utc).isoformat()}
    catalog.put_in_transaction(db,'artifact',saved['id'],doc,0)
    catalog.put_in_transaction(db,'artifact-family',family,{'session_id':saved['session_id'],'version':version,'current_id':saved['id']},expected_family if family_id else 0)


def store(data,mime,sid,name,origin,*,family_id=None,version=1,expected_family=None):
    created=[]
    try:
        with catalog.transaction() as db:
            saved=media.save(data,mime,sid,name,db=db,created=created)
            register(db,saved,name,origin,family_id=family_id,version=version,expected_family=expected_family)
        return get(saved['id'],sid)
    except BaseException:
        for path in created:path.unlink(missing_ok=True)
        raise


@router.post('/upload')
def upload(body:media.Upload):
    try:raw=base64.b64decode(body.data,validate=True)
    except ValueError:raise HTTPException(422,'附件内容不是有效Base64') from None
    return store(raw,body.mime,body.session_id,body.name,{'kind':'upload'})


class WorkspaceFile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:int=Field(gt=0)
    path:str=Field(min_length=1,max_length=2000)
    expected_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    expected_project_path:str=Field(min_length=1,max_length=2000)


@router.post('/from-workspace')
def from_workspace(body:WorkspaceFile):
    created=[]
    try:
        with catalog.transaction() as db:
            key=workspace_ui.binding(body.session_id)
            root,path=workspace.resolve(key,body.session_id,body.path)
            import os
            if os.path.normcase(str(root))!=os.path.normcase(str(Path(body.expected_project_path).resolve())):raise HTTPException(409,'项目目录已改变，请重新预览源文件')
            if not path.is_file() or path.stat().st_size>media.LIMIT:raise HTTPException(413,'只能登记15MiB以内的项目文件')
            with path.open('rb') as stream:raw=stream.read(media.LIMIT+1)
            if len(raw)>media.LIMIT:raise HTTPException(413,'文件在读取期间超过15MiB')
            if hashlib.sha256(raw).hexdigest()!=body.expected_sha256:raise HTTPException(409,'源文件已改变，请重新预览后登记')
            workspace.resolve(key,body.session_id,body.path)
            mime=file_mime(path,raw)
            origin={'kind':'workspace','root':str(root),'path':body.path,'sha256':body.expected_sha256}
            prior=db.execute("""SELECT m.* FROM capability_documents d JOIN media_files m ON m.id=d.id
                WHERE d.kind='artifact' AND m.session_id=? AND json_extract(d.document,'$.origin.kind')='workspace'
                AND json_extract(d.document,'$.origin.root')=? AND json_extract(d.document,'$.origin.path')=?
                ORDER BY m.rowid DESC LIMIT 1""",(body.session_id,str(root),body.path)).fetchone()
            family={}
            if prior:
                if prior['sha256']==body.expected_sha256:return get(prior['id'],body.session_id)
                before=get(prior['id'],body.session_id)
                family={'family_id':before['family_id'],'expected_family':before['family_revision']}
            saved=media.save(raw,mime,body.session_id,path.name,db=db,created=created)
            register(db,saved,path.name,origin,**family)
            row=dict(db.execute('SELECT * FROM media_files WHERE id=?',(saved['id'],)).fetchone())
            media.attachment_store.read_path(row,media.TYPES[mime])
            result=describe(row,True)
        return result
    except BaseException as error:
        for path in created:path.unlink(missing_ok=True)
        if isinstance(error,(OSError,ValueError)):raise HTTPException(422,str(error)) from error
        raise


class Version(media.Upload):
    expected_revision:int=Field(ge=0)


@router.post('/{fid}/versions')
def new_version(fid:str,body:Version):
    before=get(fid,body.session_id)
    if before['family_revision']!=body.expected_revision:raise HTTPException(409,'产物已更新，请先刷新')
    try:raw=base64.b64decode(body.data,validate=True)
    except ValueError:raise HTTPException(422,'附件内容不是有效Base64') from None
    # New immutable identity; historical links keep the approved original.
    return store(raw,body.mime,body.session_id,body.name or before['name'],
                 {'kind':'revision','previous_id':fid},family_id=before['family_id'],expected_family=body.expected_revision)


@router.get('/{fid}/versions')
def versions(fid:str,session_id:int=Query(gt=0),offset:int=Query(0,ge=0)):
    current=get(fid,session_id)
    with connect() as db:
        rows=[dict(row) for row in db.execute("SELECT m.* FROM media_files m LEFT JOIN capability_documents d ON d.kind='artifact' AND d.id=m.id WHERE m.session_id=? AND COALESCE(json_extract(d.document,'$.family_id'),m.id)=? ORDER BY m.rowid LIMIT 101 OFFSET ?",(session_id,current['family_id'],offset))]
    items=[describe(row) for row in rows]
    return {'items':sorted(items[:100],key=lambda i:i['version']),'more':len(items)>100,'next_offset':offset+100 if len(items)>100 else None}


class RestoreVersion(BaseModel):
    model_config=ConfigDict(extra='forbid')
    session_id:int=Field(gt=0)
    expected_revision:int=Field(ge=0)


@router.post('/{fid}/restore')
def restore_version(fid:str,body:RestoreVersion):
    source=get(fid,body.session_id)
    if source['family_revision']!=body.expected_revision:raise HTTPException(409,'产物版本已变化，请重新比较后再恢复')
    row,path=media.read(fid,body.session_id)
    with path.open('rb') as stream:raw=stream.read(media.LIMIT+1)
    if len(raw)!=row['size'] or hashlib.sha256(raw).hexdigest()!=row['sha256']:raise HTTPException(409,'原件在读取期间已变化，未恢复')
    return store(raw,row['mime'],body.session_id,row['name'],{'kind':'restore','restored_from':fid,'previous_id':source['latest_id']},
                 family_id=source['family_id'],expected_family=body.expected_revision)
