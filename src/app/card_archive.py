"""Bounded card archives with portable media and chunked chat documents."""
import base64
import hashlib
import io
import json
import re
import zipfile
from fastapi import HTTPException

MAX_PACKAGE = 50_000_000
MAX_TOTAL = 200_000_000
MAX_ENTRY = 32_000_000
MAX_ENTRIES = 1000
CHUNK_BYTES = 4_000_000
MEDIA_LINK = re.compile(r'/v6/media/([a-f0-9]{32})\?session_id=(\d+)')
DOCUMENTS = ('card.json', 'companion-memory.json', 'shared-profile.json', 'chat-backup.json')


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def metadata(path, raw):
    return {'path': path, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def export_messages(sid, root, media_assets, references, budget):
    from app.db import connect
    from app import media
    result = []
    with connect() as db:
        rows = db.execute("""SELECT m.* FROM messages m WHERE m.session_id=? AND m.archived=0
            ORDER BY COALESCE((SELECT MIN(message_id) FROM stage6_inputs WHERE batch_id=m.batch_id),m.id),m.id""", (sid,))
        for row in rows:
            value = {key: row[key] for key in ('id', 'role', 'content', 'at', 'delivery_status')}
            if value['role'] not in ('user', 'assistant') or not isinstance(value['content'], str) or len(value['content']) > 500000:
                raise HTTPException(409, '原聊天含不适合卡包的消息，请核对后使用完整业务数据导出')
            value['speaker'] = json.loads(row['speaker_snapshot']) if row['speaker_snapshot'] else None
            size = len(encoded(value))
            budget['messages'] += 1; budget['bytes'] += size
            if budget['messages'] > 100000 or budget['bytes'] > MAX_TOTAL:
                raise HTTPException(413, '聊天超出卡包容量，请使用完整业务数据导出')
            for match in MEDIA_LINK.finditer(value['content'] or ''):
                fid, owner = match.group(1), int(match.group(2))
                if owner != sid:
                    raise HTTPException(409, '聊天引用了其他会话的附件，请先核对引用，或不导出这段聊天')
                identity = (sid, fid)
                if identity in references:
                    continue
                record = db.execute('SELECT * FROM media_files WHERE id=? AND session_id=?', (fid, sid)).fetchone()
                if not record or record['mime'] not in media.TYPES:
                    raise HTTPException(409, '聊天附件记录缺失，无法制作可迁移卡包')
                path = root / 'attachments' / (fid + media.TYPES[record['mime']])
                if (not path.is_file() or path.is_symlink() or path.parent.is_symlink() or path.parent.is_junction()
                        or not 0 < path.stat().st_size <= media.LIMIT):
                    raise HTTPException(409, '聊天附件已丢失、为链接或超限')
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                if len(raw) != record['size'] or digest != record['sha256']:
                    raise HTTPException(409, '聊天附件已变化，请先核对原件')
                media.verify_type(raw, record['mime'])
                name = 'media/' + digest + media.TYPES[record['mime']]
                if name not in media_assets:
                    budget['bytes'] += len(raw)
                    if budget['bytes'] > MAX_TOTAL:
                        raise HTTPException(413, '聊天附件超出卡包容量，请分开导出')
                    media_assets[name] = raw
                references[identity] = {'session_id': sid, 'id': fid, 'path': name,
                                         'mime': record['mime'], 'name': record['name']}
            result.append(value)
    return result


def build(documents, assets, media_assets, references, exported_at):
    if any(len(raw) > 8_000_000 for raw in assets.values()):
        raise HTTPException(413, '人物或历史头像超过卡包的8MB单图限制')
    files = {name: encoded(value) for name, value in documents.items() if name != 'chat-backup.json'}
    chats = documents.get('chat-backup.json')
    chat_size = sum(len(encoded(message)) + 1 for session in chats['sessions'] for message in session['messages']) if chats is not None else 0
    version = 3 if media_assets or chat_size > CHUNK_BYTES else 2
    if chats is not None and version == 3:
        index = {'format': 'lingxi-chat-chunks', 'sessions': []}
        for number, session in enumerate(chats['sessions']):
            header = {key: value for key, value in session.items() if key != 'messages'}
            header['chunks'] = []
            chunk, size = [], 0
            def flush():
                if not chunk:
                    return
                name = f'chats/{number:04d}/{len(header["chunks"]):05d}.json'
                raw = b'{"messages":[' + b','.join(chunk) + b']}'
                files[name] = raw
                header['chunks'].append(metadata(name, raw))
                chunk.clear()
            for message in session['messages']:
                raw = encoded(message)
                if len(raw) > MAX_ENTRY - 20:
                    raise HTTPException(413, '单条聊天超过卡包限制，请使用完整业务数据导出')
                if chunk and (size + len(raw) > CHUNK_BYTES or len(chunk) >= 1000):
                    flush(); size = 0
                chunk.append(raw); size += len(raw) + 1
            flush()
            index['sessions'].append(header)
        files['chat-backup.json'] = encoded(index)
    elif chats is not None:
        files['chat-backup.json'] = encoded(chats)
    resources = [metadata(name, raw) for name, raw in {**assets, **media_assets}.items()]
    manifest = {'format': 'lingxi-character-card', 'version': version, 'exported_at': exported_at,
        'included': list(documents), 'resources': resources,
        'live2d_status': '本版本打包静态资源，Live2D与声音引用需要在目标设备核对'}
    if version == 3:
        manifest['media_refs'] = list(references.values())
    files.update(assets); files.update(media_assets)
    files['manifest.json'] = encoded(manifest)
    if len(files) > MAX_ENTRIES or any(len(raw) > MAX_ENTRY for raw in files.values()) or sum(map(len, files.values())) > MAX_TOTAL:
        raise HTTPException(413, '卡包超出导入允许的数量或解压容量，请分开导出或使用完整业务数据导出')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    raw = output.getvalue()
    if len(raw) > MAX_PACKAGE:
        raise HTTPException(413, '卡包超过50MB，请分开导出或使用完整业务数据导出')
    return raw


def media_resource(archive, entry):
    from app import media
    path = entry['path']
    extensions = '|'.join(re.escape(value) for value in set(media.TYPES.values()))
    if not re.fullmatch(r'media/[a-f0-9]{64}(?:' + extensions + ')', path):
        raise ValueError('媒体资源路径不合法')
    raw = archive.read(path)
    if not 0 < len(raw) <= media.LIMIT or len(raw) != entry['size'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
        raise ValueError('媒体资源容量或hash不符')
    return base64.b64encode(raw).decode('ascii')


def expand_chats(archive, documents, names):
    history = documents.get('chat-backup.json')
    used = set()
    if not history or history.get('format') != 'lingxi-chat-chunks':
        return used
    if not isinstance(history.get('sessions'), list) or len(history['sessions']) > 50:
        raise ValueError('聊天分块索引格式错误')
    total = 0
    expanded = []
    for session in history['sessions']:
        chunks = session.get('chunks')
        if not isinstance(chunks, list):
            raise ValueError('聊天分块索引缺少清单')
        output = {key: value for key, value in session.items() if key != 'chunks'}
        output['messages'] = []
        for entry in chunks:
            path = entry['path']
            if not re.fullmatch(r'chats/\d{4}/\d{5}\.json', path) or path not in names or path in used:
                raise ValueError('聊天分块引用非法、重复或缺失')
            raw = archive.read(path)
            if len(raw) != entry['size'] or hashlib.sha256(raw).hexdigest() != entry['sha256']:
                raise ValueError('聊天分块校验失败')
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get('messages'), list):
                raise ValueError('聊天分块内容错误')
            total += len(value['messages'])
            if total > 100000:
                raise ValueError('聊天分块超过10万条')
            output['messages'].extend(value['messages']); used.add(path)
        expanded.append(output)
    documents['chat-backup.json'] = {'sessions': expanded}
    return used


def validate_media(documents, media_assets, references):
    from app import media
    if not isinstance(references, list) or len(references) > MAX_ENTRIES:
        raise ValueError('媒体引用清单错误')
    rows = documents.get('chat-backup.json', {}).get('sessions', [])
    sessions = {s.get('original_id'): s for s in rows}
    if references and (len(sessions) != len(rows) or any(not isinstance(sid, int) or isinstance(sid, bool) or sid <= 0 for sid in sessions)):
        raise ValueError('带媒体的聊天需要唯一的原始会话编号')
    indexed = {}
    for item in references:
        sid, fid, mime = item['session_id'], item['id'], item['mime']
        if (not isinstance(sid, int) or isinstance(sid, bool) or sid not in sessions
                or not isinstance(fid, str) or not re.fullmatch(r'[a-f0-9]{32}', fid)
                or mime not in media.TYPES or item['path'] not in media_assets
                or not item['path'].endswith(media.TYPES[mime]) or (sid, fid) in indexed
                or not isinstance(item.get('name'), str) or len(item['name']) > 200):
            raise ValueError('媒体引用、类型或所属会话错误')
        media.verify_type(base64.b64decode(media_assets[item['path']]), mime)
        indexed[sid, fid] = item
    for sid, session in sessions.items():
        for message in session.get('messages', []):
            for match in MEDIA_LINK.finditer(message.get('content', '')):
                if int(match.group(2)) != sid or (sid, match.group(1)) not in indexed:
                    raise ValueError('聊天媒体引用缺少原件或所属会话不符')
    if {item['path'] for item in references} != set(media_assets):
        raise ValueError('媒体资源与引用清单不一致')
    return indexed


def prepare_import_media(db, token, payload, selected):
    from uuid import uuid4
    old = db.execute('SELECT document FROM card_import_media_jobs WHERE import_id=?', (token,)).fetchone()
    if old:
        allocated = json.loads(old['document'])
        if allocated or not selected or not payload.get('media_refs'):
            return allocated
        allocated = [{**item, 'new_id': uuid4().hex} for item in payload['media_refs']]
        db.execute('UPDATE card_import_media_jobs SET document=? WHERE import_id=?', (json.dumps(allocated), token))
        return allocated
    allocated = [{**item, 'new_id': uuid4().hex} for item in payload.get('media_refs', [])] if selected else []
    db.execute('INSERT INTO card_import_media_jobs VALUES(?,?)', (token, json.dumps(allocated)))
    return allocated


def imported_media_paths(root, allocated):
    from app import media
    folder = root / 'attachments'
    if folder.is_symlink() or folder.is_junction():
        raise ValueError('附件目录不能是链接')
    result = []
    for item in allocated:
        if not re.fullmatch(r'[a-f0-9]{32}', item['new_id']) or item['mime'] not in media.TYPES:
            raise ValueError('导入媒体恢复记录不合法')
        path = folder / (item['new_id'] + media.TYPES[item['mime']])
        if path.resolve().parent != folder.resolve() or path.is_symlink():
            raise ValueError('导入媒体恢复路径不合法')
        result.append(path)
    return result


def import_message_media(db, text, old_sid, sid, root, allocated, media_assets, created):
    from app import media
    indexed = {(item['session_id'], item['id']): item for item in allocated}
    def rewrite(match):
        fid, owner = match.group(1), int(match.group(2))
        item = indexed.get((owner, fid))
        if owner != old_sid or not item:
            raise HTTPException(409, '卡包未携带这条聊天的附件原件；请重新导出，或取消导入聊天')
        fresh = item['new_id']
        previous = db.execute('SELECT session_id FROM media_files WHERE id=?', (fresh,)).fetchone()
        if previous and previous['session_id'] != sid:
            raise HTTPException(422, '卡包原始会话编号重复')
        if not previous:
            raw = base64.b64decode(media_assets[item['path']], validate=True)
            destination = imported_media_paths(root, [item])[0]
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open('xb') as stream:
                stream.write(raw)
            created.append(destination)
            db.execute('INSERT INTO media_files VALUES(?,?,?,?,?,?)',
                       (fresh, sid, item['mime'], len(raw), hashlib.sha256(raw).hexdigest(), item['name']))
        return f'/v6/media/{fresh}?session_id={sid}'
    return MEDIA_LINK.sub(rewrite, text)
