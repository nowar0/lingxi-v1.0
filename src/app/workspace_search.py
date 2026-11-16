
"""Bounded project-only search and expanded-directory change fingerprints."""
import fnmatch
import hashlib
import itertools
import os
import time
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from app import workspace, workspace_ui, work_settings

router=APIRouter(prefix='/v9/workspace')
IGNORED={'.git','.env','.venv','venv','node_modules','__pycache__','.next','dist','build','target'}


def ignored(path, patterns):
    return any(p.casefold() in IGNORED or p.startswith('.') for p in path.parts) or '.lingxi-backup-' in path.name or any(fnmatch.fnmatch(path.as_posix(), p) or fnmatch.fnmatch(path.name, p) for p in patterns)


def snapshot_binding(sid, path):
    key=workspace_ui.binding(sid)
    root,target=workspace.resolve(key,sid,path)
    if not target.is_dir():
        raise HTTPException(422,'检索范围必须是项目内目录')
    return key,root,target


def recheck(sid,key,root):
    if workspace_ui.binding(sid)!=key or Path(work_settings.get(sid)['path']).resolve()!=root:
        raise HTTPException(409,'检索期间项目目录已改变，请重新搜索')


@router.get('/search')
def search(session_id:int=Query(gt=0),q:str=Query(min_length=1,max_length=200),path:str=Query('.',max_length=2000),
           mode:str=Query('filename',pattern='^(filename|content)$'),offset:int=Query(0,ge=0,le=1000),
           limit:int=Query(50,ge=1,le=100),ignore:list[str]=Query(default=[],max_length=20)):
    if any(len(p)>200 for p in ignore): raise HTTPException(422,'每条忽略模式最多200字符')
    try:
        key,root,scope=snapshot_binding(session_id,path)
        pattern=q.casefold(); stack=[scope]; matches=[]; visited=0; read_bytes=0; unreadable=0; skipped=0
        deadline=time.monotonic()+3; reason=''; more=False
        while stack and not reason and not more:
            directory=stack.pop()
            # Re-resolve each directory and file to reject symlinks/junctions
            # introduced after its parent was enumerated.
            workspace.resolve(key,session_id,directory.relative_to(root).as_posix())
            try:
                with os.scandir(directory) as entries:
                    for entry in sorted(itertools.islice(entries,max(1,10001-visited)),key=lambda e:e.name.casefold()):
                        visited+=1
                        if visited>10000 or time.monotonic()>deadline:
                            reason='已达到本次扫描的文件数或3秒期限，请缩小范围';break
                        rel=Path(entry.path).relative_to(root)
                        if ignored(rel,ignore): continue
                        try:
                            _,target=workspace.resolve(key,session_id,rel.as_posix())
                            if target.is_dir(): stack.append(target); continue
                            if not target.is_file(): continue
                            found=[]
                            if mode=='filename':
                                if pattern in rel.as_posix().casefold(): found=[{'path':rel.as_posix(),'line':None,'column':None,'preview':entry.name}]
                            else:
                                if target.stat().st_size>2*1024*1024: skipped+=1;continue
                                with target.open('rb') as stream: raw=stream.read(2*1024*1024+1)
                                read_bytes+=len(raw)
                                if read_bytes>24*1024*1024: reason='已读取24MiB文本，请缩小范围';break
                                if len(raw)>2*1024*1024 or b'\0' in raw: skipped+=1;continue
                                try: text=raw.decode('utf-8')
                                except UnicodeError: skipped+=1;continue
                                fingerprint=hashlib.sha256(raw).hexdigest()
                                for number,line in enumerate(text.splitlines(),1):
                                    column=line.casefold().find(pattern)
                                    if column>=0:
                                        start=max(0,column-50)
                                        found.append({'path':rel.as_posix(),'line':number,'column':column+1,'preview':line[start:start+240],'sha256':fingerprint})
                                        if len(found)>offset+limit: break
                            for match in found:
                                matches.append(match)
                                if len(matches)>offset+limit: more=True;break
                            if more:break
                        except (OSError,ValueError): unreadable+=1
            except OSError: unreadable+=1
        recheck(session_id,key,root)
        return {'items':matches[offset:offset+limit],'more':more,'next_offset':offset+limit if more else None,
                'partial':bool(reason),'reason':reason,'visited':visited,'skipped':skipped,'unreadable':unreadable,
                'scope':path,'mode':mode,'ignored':'隐藏项、常见依赖/构建目录、应用旧文件副本及本次忽略模式'}
    except (OSError,ValueError) as error: raise HTTPException(422,str(error)) from error


@router.get('/watch')
def watch(session_id:int=Query(gt=0),directories:list[str]=Query(default=['.'],max_length=50)):
    items=[]
    for path in dict.fromkeys(directories):
        if len(path)>2000:raise HTTPException(422,'目录路径过长')
        try:
            key,root,directory=snapshot_binding(session_id,path)
            rows=[]
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(rows)>=500:break
                    rel=Path(entry.path).relative_to(root)
                    if ignored(rel,[]):continue
                    try:
                        _,target=workspace.resolve(key,session_id,rel.as_posix())
                        stat=target.stat()
                        rows.append((entry.name,target.is_dir(),stat.st_size,stat.st_mtime_ns))
                    except (OSError,ValueError):continue
            signature=hashlib.sha256(repr(sorted(rows)).encode()).hexdigest()
            recheck(session_id,key,root)
            items.append({'path':path,'signature':signature})
        except (OSError,ValueError,HTTPException) as error:
            items.append({'path':path,'error':str(getattr(error,'detail',error))})
    return {'items':items,'interval_ms':2500,'limit':500}
