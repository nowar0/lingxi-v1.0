"""阶段5候选卡包代码：资料/图片 + 独立选择的共用数据。未运行验证。"""
import base64
import hashlib
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app import sessions, stage5 as S, card_archive
from app.config import BASE_DIR, DATA_DIR
from app.db import connect

router=APIRouter(prefix='/v2')
active_imports = set()


def init_table():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS card_import_results(import_id TEXT PRIMARY KEY,result TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS card_import_jobs(import_id TEXT PRIMARY KEY,character_id TEXT NOT NULL,status TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS card_import_media_jobs(import_id TEXT PRIMARY KEY REFERENCES card_import_jobs(import_id),document TEXT NOT NULL)')
        tokens = [r['import_id'] for r in db.execute("SELECT import_id FROM card_import_jobs WHERE status!='complete'")]
    for token in tokens:
        cleanup_unfinished(token)


def cleanup_unfinished(token):
    with connect() as db:
        job = db.execute('SELECT * FROM card_import_jobs WHERE import_id=?', (token,)).fetchone()
        saved = db.execute('SELECT 1 FROM card_import_results WHERE import_id=?', (token,)).fetchone()
        if not job or saved:
            return
        cid = job['character_id']
        media_job = db.execute('SELECT document FROM card_import_media_jobs WHERE import_id=?', (token,)).fetchone()
        allocated = json.loads(media_job['document']) if media_job else []
        if any(db.execute('SELECT 1 FROM media_files WHERE id=?', (item['new_id'],)).fetchone() for item in allocated):
            raise RuntimeError('未完成导入引用已有媒体，保留资源并停止恢复')
        if db.execute('SELECT 1 FROM character_cards WHERE id=?', (cid,)).fetchone():
            raise RuntimeError('未完成导入任务引用已有卡，保留资源并停止恢复')
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,80}', token) or not re.fullmatch(r'[a-f0-9-]{36}', cid):
        raise RuntimeError('未完成导入记录路径不合法')
    for path in card_archive.imported_media_paths(DATA_DIR, allocated):
        path.unlink(missing_ok=True)
    for directory in (DATA_DIR/'card-imports'/'staging'/token, DATA_DIR/'characters'/cid):
        resolved = directory.resolve()
        if not resolved.is_relative_to(DATA_DIR.resolve()):
            raise RuntimeError('导入清理路径越界')
        if directory.exists():
            shutil.rmtree(resolved)
    with connect() as db:
        db.execute("UPDATE card_import_jobs SET status='failed' WHERE import_id=?", (token,))


def image_bytes(url):
    if url in (S.DEFAULT_AVATAR,S.DEFAULT_ART):
        path=BASE_DIR/'app/static'/url.removeprefix('/ui/').replace('assets/','ui/assets/',1)
    else:
        match=re.fullmatch(r'/v2/characters/([a-zA-Z0-9-]+)/assets/([a-f0-9]{64}\.(?:png|jpg|webp))',url)
        if not match:
            raise HTTPException(422,'无法打包的图片引用')
        path=DATA_DIR/'characters'/match[1]/'assets'/match[2]
    if not path.is_file():
        raise HTTPException(409,'缺少角色或历史头像资源')
    return path.read_bytes(),path.suffix


@router.post('/characters/{cid}/export')
def export(cid: str,fields: dict):
    card=dict(S.character(cid))
    assets={}
    media_assets, media_refs = {}, {}
    def pack_image(url):
        raw,suffix=image_bytes(url)
        key='assets/'+hashlib.sha256(raw).hexdigest()+suffix
        assets[key]=raw
        return key
    card['avatar']=pack_image(card['avatar'])
    card['art']=pack_image(card['art'])
    card.pop('id',None)
    card.pop('revision',None)
    documents={'card.json':card}
    with connect() as db:
        for scope,filename,selected in [('companion','companion-memory.json',fields.get('include_companion')),
                                        ('shared','shared-profile.json',fields.get('include_shared'))]:
            if selected:
                facts=[dict(r) for r in db.execute('SELECT * FROM facts WHERE mode=?',(scope,))]
                events=[dict(r) for r in db.execute('SELECT * FROM events WHERE mode=?',(scope,))]
                if len(facts)+len(events)>2000:
                    raise HTTPException(413,'单包记忆超过2000项，请使用完整数据备份')
                documents[filename]={'facts':facts,'events':events}
    if fields.get('include_chat'):
        choices=fields.get('session_ids',[])
        if not isinstance(choices,list) or not choices or len(choices)>50:
            raise HTTPException(422,'选择1～50个陪伴会话')
        chats=[]
        total=0
        export_budget = {'messages': 0, 'bytes': 0}
        for sid in dict.fromkeys(choices):
            session=sessions.get_session(sid)
            if not session or session['mode']!='companion':
                raise HTTPException(409,'只允许导出陪伴会话')
            messages=card_archive.export_messages(sid,DATA_DIR,media_assets,media_refs,export_budget)
            total+=len(messages)
            if total>100000:
                raise HTTPException(413,'聊天超过10万条，请使用完整数据库备份')
            for msg in messages:
                if msg.get('speaker') and msg['speaker'].get('avatar'):
                    msg['speaker']['avatar']=pack_image(msg['speaker']['avatar'])
            chats.append({'original_id':sid,'title':session['title'],'messages':messages})
        documents['chat-backup.json']={'sessions':chats}
    raw = card_archive.build(documents, assets, media_assets, media_refs, S.now())
    return Response(raw,media_type='application/zip',
                    headers={'Content-Disposition':'attachment; filename="lingxi-character.zip"'})


@router.get('/characters/{cid}/export')
def download(cid: str, include_companion: bool=False, include_shared: bool=False,
             include_chat: bool=False, session_id: int | None=None):
    return export(cid, {'include_companion': include_companion, 'include_shared': include_shared,
                        'include_chat': include_chat,
                        'session_ids': [] if session_id is None else [session_id]})


def unpack(encoded):
    if not isinstance(encoded,str) or len(encoded)>70_000_000:
        raise HTTPException(413,'卡包上传最多50MB')
    try:
        raw=base64.b64decode(encoded,validate=True)
        if len(raw)>card_archive.MAX_PACKAGE:
            raise ValueError('卡包过大')
        if not raw.startswith(b'PK'):
            data=json.loads(raw.decode('utf-8-sig'))
            if data.get('format')!='lingxi-character-card' or data.get('version')!=1:
                raise ValueError('不支持该JSON卡格式')
            documents={'card.json':data['card']}
            for scope,name in [('companion','companion-memory.json'),('shared','shared-profile.json')]:
                rows=[m for m in data.get('memories',[]) if m.get('scope')==scope]
                if rows:
                    documents[name]={'facts':[{'key':m['key'],'value':m['value']} for m in rows],'events':[]}
            return {'documents':documents,'assets':{},'legacy':True}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos=archive.infolist()
            if len(infos)>card_archive.MAX_ENTRIES or sum(i.file_size for i in infos)>card_archive.MAX_TOTAL:
                raise ValueError('资源数量或解压总大小超限')
            names=set()
            folded=set()
            for info in infos:
                name=info.filename
                path=PurePosixPath(name)
                if ('\\' in name or ':' in name or path.is_absolute() or '..' in path.parts
                        or str(path)!=name.rstrip('/') or str(path).casefold() in folded
                        or ((info.external_attr>>16)&0o170000)==0o120000):
                    raise ValueError('卡包路径不合法或重复')
                names.add(str(path)); folded.add(str(path).casefold())
                if info.file_size>card_archive.MAX_ENTRY:
                    raise ValueError('单文件过大')
            manifest=json.loads(archive.read('manifest.json'))
            if manifest.get('format')!='lingxi-character-card' or manifest.get('version') not in (2,3):
                raise ValueError('不支持该包版本')
            assets={}
            media_assets={}
            for entry in manifest.get('resources',[]):
                key=entry['path']
                if manifest['version']==3 and key.startswith('media/'):
                    if key in media_assets: raise ValueError('媒体资源重复')
                    media_assets[key]=card_archive.media_resource(archive,entry)
                    continue
                if key in assets: raise ValueError('图片资源重复')
                if not re.fullmatch(r'assets/[a-f0-9]{64}\.(png|jpg|webp)',key):
                    raise ValueError('仅支持合法静态图片资产；完整Live2D包尚未适配')
                value=archive.read(key)
                if len(value)>8_000_000 or len(value)!=entry['size'] or hashlib.sha256(value).hexdigest()!=entry['sha256']:
                    raise ValueError('图片大小或hash不符')
                suffix=key.rsplit('.',1)[-1]
                good=(suffix=='png' and value.startswith(b'\x89PNG\r\n\x1a\n')
                      or suffix=='jpg' and value.startswith(b'\xff\xd8\xff')
                      or suffix=='webp' and value[:4]==b'RIFF' and value[8:12]==b'WEBP')
                if not good:
                    raise ValueError('图片实际类型不符')
                assets[key]=base64.b64encode(value).decode('ascii')
            documents={name:json.loads(archive.read(name)) for name in
                       ('card.json','companion-memory.json','shared-profile.json','chat-backup.json') if name in names}
            chunks=card_archive.expand_chats(archive,documents,names) if manifest['version']==3 else set()
            expected=set(documents)|set(assets)|set(media_assets)|chunks|{'manifest.json'}
            actual={i.filename for i in infos if not i.is_dir()}
            if actual!=expected or any(not any(name.startswith(i.filename.rstrip('/')+'/') for name in expected) for i in infos if i.is_dir()):
                raise ValueError('卡包包含未列入清单的资源')
            refs=manifest.get('media_refs',[])
            if manifest['version']==3: card_archive.validate_media(documents,media_assets,refs)
            card=documents['card.json']
            if card.get('avatar') not in assets or card.get('art') not in assets:
                raise ValueError('缺少头像或立绘')
            return {'documents':documents,'assets':assets,'media':media_assets,'media_refs':refs,'legacy':False}
    except (ValueError,KeyError,TypeError,AttributeError,zipfile.BadZipFile,UnicodeError) as exc:
        raise HTTPException(422,'卡包校验失败：'+str(exc)) from exc


@router.post('/card-imports/preview')
def preview(fields: dict):
    payload=unpack(fields.get('data'))
    doc=payload['documents']
    # 先执行同样的字段检查，但不写库。
    c=doc['card.json']
    if not isinstance(c,dict):
        raise HTTPException(422,'人物资料必须是对象')
    if not isinstance(c.get('name'),str) or not c['name'].strip() or len(c['name'])>80 or not isinstance(c.get('persona'),str) or not 1<=len(c['persona'])<=5000:
        raise HTTPException(422,'名称或人设格式错误')
    for name in ('companion-memory.json','shared-profile.json'):
        data=doc.get(name,{'facts':[],'events':[]})
        if not isinstance(data,dict) or not isinstance(data.get('facts',[]),list) or not isinstance(data.get('events',[]),list) or len(data.get('facts',[]))+len(data.get('events',[]))>2000:
            raise HTTPException(422,'记忆结构错误或超过2000项')
        seen=set()
        for fact in data.get('facts',[]):
            if not isinstance(fact,dict) or not isinstance(fact.get('key'),str) or not isinstance(fact.get('value'),str):
                raise HTTPException(422,'记忆名称和内容必须是文字')
            normalized=S.normalize_key(fact['key'])
            if not normalized or len(normalized)>80 or normalized in seen or not fact['value'].strip() or len(fact['value'])>2000:
                raise HTTPException(422,'记忆名称重复或长度不合法')
            seen.add(normalized)
            fact['key']=normalized
        for event in data.get('events',[]):
            if not isinstance(event,dict) or not isinstance(event.get('content'),str) or len(event['content'])>4000:
                raise HTTPException(422,'记忆事件格式错误')
    history=doc.get('chat-backup.json',{'sessions':[]})
    if not isinstance(history,dict) or not isinstance(history.get('sessions'),list) or len(history['sessions'])>50:
        raise HTTPException(422,'聊天备份结构错误或超过50个会话')
    total=0
    for session in history['sessions']:
        if not isinstance(session,dict) or not isinstance(session.get('messages'),list):
            raise HTTPException(422,'聊天会话格式错误')
        total+=len(session['messages'])
        if total>100000:
            raise HTTPException(413,'聊天备份最多10万条消息')
        for message in session['messages']:
            if not isinstance(message,dict) or message.get('role') not in ('user','assistant') or not isinstance(message.get('content'),str) or len(message['content'])>500000:
                raise HTTPException(422,'聊天消息格式或大小错误')
            status=message.get('delivery_status','complete')
            if status not in ('complete','retained','stopped','failed','generating','queued','sending','cancelled'):
                raise HTTPException(422,'聊天消息状态不受支持')
            speaker=message.get('speaker')
            if speaker is not None and (not isinstance(speaker,dict) or not isinstance(speaker.get('name'),str) or speaker.get('avatar') not in payload['assets']):
                raise HTTPException(422,'历史回复缺少角色署名或头像资源')
    warnings=[]
    if any(m.get('delivery_status','complete') not in ('complete','retained') for s in history['sessions'] for m in s['messages']):
        warnings.append('未完成消息将作为已停止的历史导入，不恢复原执行或审批。')
    if not payload.get('media_refs') and any(card_archive.MEDIA_LINK.search(m['content']) for s in history['sessions'] for m in s['messages']):
        warnings.append('旧卡包的聊天附件没有原件；可只导入人物/记忆，或从原客户端重新导出聊天。')
    token=str(uuid4())
    folder=DATA_DIR/'card-imports'
    folder.mkdir(parents=True,exist_ok=True)
    payload.update(created=time.time(),result=None)
    (folder/(token+'.json')).write_text(S.dump(payload),encoding='utf-8')
    with connect() as db:
        existing={scope:{r['key']:r['value'] for r in db.execute('SELECT key,value FROM facts WHERE mode=?',(scope,))} for scope in ('companion','shared')}
    conflicts={}
    for scope,name in [('companion','companion-memory.json'),('shared','shared-profile.json')]:
        conflicts[scope]=[{'key':f['key'],'current':existing[scope].get(f['key']), 'incoming':f['value']}
                          for f in doc.get(name,{}).get('facts',[])]
    return {'import_id':token,'warnings':warnings,'card':{k:c.get(k) for k in ('name','description','persona')},
            'legacy':payload['legacy'],'included':list(doc),'conflicts':conflicts,
            'preview_assets': {} if payload['legacy'] else {kind: f'/v2/card-imports/{token}/preview-assets/{kind}' for kind in ('avatar','art')},
            'chat_count':len(doc.get('chat-backup.json',{}).get('sessions',[]))}


@router.get('/card-imports/{token}/preview-assets/{kind}')
def preview_asset(token: str, kind: str):
    from fastapi.responses import Response
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,80}', token) or kind not in ('avatar', 'art'):
        raise HTTPException(404, '预览资源不存在')
    path = DATA_DIR / 'card-imports' / (token + '.json')
    if not path.is_file():
        raise HTTPException(404, '导入预览不存在')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if time.time() - payload['created'] > 3600:
        raise HTTPException(409, '导入预览已过期')
    key = payload['documents']['card.json'].get(kind)
    encoded = payload['assets'].get(key)
    if encoded is None:
        raise HTTPException(404, '旧资料包没有预览图片')
    suffix = PurePosixPath(key).suffix
    return Response(base64.b64decode(encoded), media_type={'.png':'image/png','.jpg':'image/jpeg','.webp':'image/webp'}[suffix])


@router.post('/card-imports/{token}/commit')
def commit(token: str,fields: dict):
    if token in active_imports:
        raise HTTPException(409, '本导入正在提交；完成后使用同一编号重试')
    active_imports.add(token)
    created=[]
    try:
        cleanup_unfinished(token)
        return commit_impl(token,fields,created)
    except Exception:
        # 仅清理本次新建卡的文件，不删除旧资源或其他导入数据。
        for file in reversed(created):
            if file.resolve().is_relative_to(DATA_DIR.resolve()) and file.is_file():
                file.unlink()
        cleanup_unfinished(token)
        raise
    finally:
        active_imports.discard(token)


def commit_impl(token,fields,created):
    if not re.fullmatch(r'[a-zA-Z0-9-]{1,80}',token):
        raise HTTPException(404,'导入编号错误')
    path=DATA_DIR/'card-imports'/(token+'.json')
    if not path.is_file():
        raise HTTPException(404,'导入预览不存在')
    payload=json.loads(path.read_text(encoding='utf-8'))
    with connect() as db:
        db.execute("INSERT OR IGNORE INTO card_import_jobs VALUES(?,?,'prepared')", (token, str(uuid4())))
        card_archive.prepare_import_media(db,token,payload,bool(fields.get('include_chat')))
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS card_import_results(import_id TEXT PRIMARY KEY,result TEXT NOT NULL)')
        saved=db.execute('SELECT result FROM card_import_results WHERE import_id=?',(token,)).fetchone()
        if saved:
            return json.loads(saved[0])
        if time.time()-payload['created']>3600:
            raise HTTPException(409,'导入预览过期，请重新预览')
        doc=payload['documents']
        allocated=json.loads(db.execute('SELECT document FROM card_import_media_jobs WHERE import_id=?',(token,)).fetchone()[0])
        job = db.execute('SELECT character_id FROM card_import_jobs WHERE import_id=?', (token,)).fetchone()
        c=S.save_card(doc['card.json'],db=db,new_identity=job['character_id'])
        folder=DATA_DIR/'characters'/c['id']/'assets'
        staging=DATA_DIR/'card-imports'/'staging'/token/'assets'
        staging.mkdir(parents=True,exist_ok=True)
        mapped={}
        for key,encoded in payload['assets'].items():
            filename=PurePosixPath(key).name
            file=staging/filename
            file.write_bytes(base64.b64decode(encoded))
            created.append(file)
            mapped[key]=f"/v2/characters/{c['id']}/assets/{filename}"
        folder.parent.mkdir(parents=True,exist_ok=True)
        staging.replace(folder)
        for key in ('avatar','art'):
            if doc['card.json'].get(key) in mapped:
                c[key]=mapped[doc['card.json'][key]]
        db.execute('UPDATE character_cards SET document=? WHERE id=?',(S.dump(c),c['id']))
        db.execute('UPDATE character_versions SET document=? WHERE id=? AND revision=1',(S.dump(c),c['id']))
        imported_sessions=[]
        if fields.get('include_chat'):
            for session in doc.get('chat-backup.json',{}).get('sessions',[]):
                sid=db.execute('INSERT INTO sessions(title,mode,created_at,updated_at) VALUES(?,?,?,?)',
                               (str(session.get('title','导入聊天'))[:80],'companion',S.now(),S.now())).lastrowid
                imported_sessions.append(sid)
                for msg in session.get('messages',[]):
                    if msg.get('role') not in ('user','assistant') or not isinstance(msg.get('content'),str):
                        raise HTTPException(422,'聊天消息格式错误')
                    speaker=msg.get('speaker')
                    if speaker:
                        speaker=dict(speaker)
                        if speaker.get('avatar') not in mapped:
                            raise HTTPException(422,'历史头像资源缺失')
                        speaker['avatar']=mapped[speaker['avatar']]
                        speaker['origin']='外部卡包历史'
                    content=card_archive.import_message_media(db,msg['content'],session.get('original_id'),sid,DATA_DIR,allocated,payload.get('media',{}),created)
                    status=msg.get('delivery_status','complete')
                    status=status if status in ('complete','retained') else 'stopped'
                    db.execute('INSERT INTO messages(session_id,role,content,at,speaker_snapshot,delivery_status) VALUES(?,?,?,?,?,?)',
                               (sid,msg['role'],content,msg.get('at',S.now()),S.dump(speaker) if speaker else None,status))
        for scope,name,enabled in [('companion','companion-memory.json',fields.get('include_companion')),
                                  ('shared','shared-profile.json',fields.get('include_shared'))]:
            if not enabled:
                continue
            choices=fields.get(scope+'_choices',{})
            for f in doc.get(name,{}).get('facts',[]):
                choice=choices.get(f['key'],'skip')
                if choice!='import':
                    continue
                if 'expected' not in f and f['key'] not in fields.get(scope+'_expected',{}):
                    raise HTTPException(409,'缺少冲突预览值')
                existing=db.execute('SELECT value FROM facts WHERE mode=? AND key=?',(scope,f['key'])).fetchone()
                expected=fields.get(scope+'_expected',{}).get(f['key'])
                if (existing['value'] if existing else None)!=expected:
                    raise HTTPException(409,'记忆已经变化，请重新预览')
                S.set_fact(f['key'],f['value'],{'mode':scope,'origin':'外部卡包','import_id':token},db)
            # 事件默认也不导入，需要单独明确勾选。
            if fields.get(scope+'_events'):
                for e in doc.get(name,{}).get('events',[]):
                    if not isinstance(e.get('content'),str) or len(e['content'])>4000:
                        raise HTTPException(422,'事件格式错误')
                    db.execute('INSERT INTO events(content,mode,at) VALUES(?,?,?)',(e['content'],scope,e.get('at',S.now())))
        result={'card':c,'imported_sessions':imported_sessions}
        db.execute('INSERT INTO card_import_results VALUES(?,?)',(token,S.dump(result)))
        db.execute("UPDATE card_import_jobs SET status='complete' WHERE import_id=?", (token,))
    return result
