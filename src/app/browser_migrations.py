"""旧浏览器缓存：预览、显式选择、事务提交和可恢复原文备份。"""
import hashlib
import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app import chat, migrations, stage5
from app.db import connect
from app.memory.keys import normalize_key
from app.memory import v2, v2_bridge

router = APIRouter(prefix='/v2/browser-migrations')


def fact_state(db, fact):
    if v2_bridge.active(db):
        row = db.execute('SELECT id,revision FROM memory_v2_entities WHERE scope=? AND owner_id=? AND key=?',
                         (fact['scope'], v2.owner(fact['scope']), fact['key'])).fetchone()
        return (v2.get(row['id'], db=db).get('value'), row['revision']) if row else (None, 0)
    current = db.execute('SELECT value FROM facts WHERE mode=? AND key=?', (fact['scope'], fact['key'])).fetchone()
    owner = 'global' if fact['scope'] == 'shared' else 'companion-global'
    count = db.execute('SELECT COUNT(*) FROM fact_versions WHERE scope=? AND owner_id=? AND key=?',
                       (fact['scope'], owner, fact['key'])).fetchone()[0]
    return (current['value'] if current else None, count or (1 if current else 0))


def import_fact(db, fact, revision, migration_id):
    source = {'origin': '旧浏览器缓存人工选择', 'migration_id': migration_id, 'cache_key': fact['cache_key'],
              'external_source': fact['source'], 'old_memory_id': fact['old_id'], 'old_card_id': fact['card_id']}
    if not v2_bridge.active(db):
        return stage5.set_fact(fact['key'], fact['value'], {'mode': fact['scope'], 'expected_revision': revision, **source}, db)
    row = db.execute('SELECT id,revision FROM memory_v2_entities WHERE scope=? AND owner_id=? AND key=?',
                     (fact['scope'], v2.owner(fact['scope']), fact['key'])).fetchone()
    if (row['revision'] if row else 0) != revision:
        raise HTTPException(409, '目标记忆已更新，请重新预览后选择')
    # Preserve unknown cache provenance; importing a browser fragment never
    # fabricates a verified message source or re-enables the old memory writer.
    source.update(kind='legacy', ref=f'browser-cache:{migration_id}:{fact["id"]}')
    try:
        if row:
            return v2.update(row['id'], {'expected_revision': revision, 'value': fact['value'], 'source': source}, db=db)
        return v2.create({'scope': fact['scope'], 'key': fact['key'], 'value': fact['value'],
                          'layer': 2, 'category': 'legacy', 'source': source}, db=db)
    except v2.MemoryError as error:
        raise HTTPException(error.status_code, str(error)) from None


def init_table():
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS browser_migration_previews(id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, snapshot TEXT NOT NULL, document TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS browser_migration_results(id TEXT PRIMARY KEY REFERENCES browser_migration_previews(id), choices TEXT NOT NULL, report TEXT NOT NULL)')


def raw_preview(snapshot):
    if not isinstance(snapshot, dict) or not snapshot or len(snapshot) > 40:
        raise HTTPException(422, '缓存快照应是有限的键与原文对象')
    raw = stage5.dump(snapshot)
    if len(raw) > 10_000_000:
        raise HTTPException(413, '缓存快照最多10MB')
    cards, drafts, facts, pending, warnings = [], [], [], [], []
    known_cards = {c['id'] for c in stage5.cards()['cards']}
    seen = set()
    for cache_key, value in snapshot.items():
        if not isinstance(cache_key, str) or not cache_key.startswith('lingxi-') or not isinstance(value, str):
            raise HTTPException(422, '只接收零汐缓存的键与原始文字')
        if cache_key.startswith('lingxi-stage5-draft-backup-'):
            drafts.append({'id': str(len(drafts)), 'cache_key': cache_key, 'thread_id': '冲突备份', 'card_id': None, 'session_id': None, 'content': value})
            continue
        try:
            state = json.loads(value)
        except ValueError:
            warnings.append(cache_key + '无法解析，仅保留原文')
            continue
        if not isinstance(state, dict):
            warnings.append(cache_key + '不是状态对象，仅保留原文')
            continue
        for card in state.get('cards', []) if isinstance(state.get('cards', []), list) else []:
            if not isinstance(card, dict):
                continue
            # 已在服务器上的身份不复制；浏览器本地卡必须人工选择。
            signature = stage5.dump(card)
            if (card.get('id') in known_cards and isinstance(card.get('revision'), int)) or signature in seen:
                continue
            seen.add(signature)
            cards.append({'id': str(len(cards)), 'cache_key': cache_key, 'old_id': card.get('id'), 'card': card,
                          'warning': '旧本地图片引用不执行；导入资料采用默认静态图，原资源引用保留在备份。'})
        for thread in state.get('companions', []) if isinstance(state.get('companions', []), list) else []:
            if not isinstance(thread, dict):
                continue
            link = thread.get('realChat') if isinstance(thread.get('realChat'), dict) else {}
            if link.get('pending'):
                pending.append({'cache_key': cache_key, 'thread_id': thread.get('id'), 'session_id': link.get('sessionId'), 'request': link['pending']})
            text = thread.get('draft')
            if isinstance(text, str) and text:
                drafts.append({'id': str(len(drafts)), 'cache_key': cache_key, 'thread_id': thread.get('id'),
                               'card_id': thread.get('cardId'), 'session_id': link.get('sessionId'), 'content': text})
        for fact in state.get('memories', []) if isinstance(state.get('memories', []), list) else []:
            if not isinstance(fact, dict) or fact.get('scope') not in ('companion', 'shared'):
                continue
            key, value = fact.get('key'), fact.get('value')
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            key = normalize_key(key)
            if not key or len(key) > 80 or not value.strip() or len(value) > 2000:
                warnings.append('记忆字段过长或为空，仅备份：' + key[:80])
                continue
            facts.append({'id': str(len(facts)), 'cache_key': cache_key, 'scope': fact['scope'], 'card_id': fact.get('cardId'),
                          'key': key, 'value': value, 'source': fact.get('source'), 'old_id': fact.get('id')})
    return raw, {'cards': cards, 'drafts': drafts, 'facts': facts, 'pending': pending, 'warnings': warnings,
                 'notice': '所有项目默认不采用。演示数据不自动入库；旧聊天、工作预览和未选项完整保留在原文备份，真实会话ID与消息ID不改动。'}


def result(mid):
    with connect() as db:
        row = db.execute('SELECT report FROM browser_migration_results WHERE id=?', (mid,)).fetchone()
    return json.loads(row[0]) if row else None


@router.post('/preview')
def preview(fields: dict):
    raw, document = raw_preview(fields.get('snapshot'))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    with connect() as db:
        row = db.execute('SELECT id,document FROM browser_migration_previews WHERE digest=?', (digest,)).fetchone()
        if row:
            mid, document = row['id'], json.loads(row['document'])
        else:
            mid = str(uuid4())
            db.execute('INSERT INTO browser_migration_previews VALUES(?,?,?,?)', (mid, digest, raw, stage5.dump(document)))
    # 每次预览读取最新服务端版本；取消不会修改任何业务表。
    for fact in document['facts']:
        with connect() as db:
            fact['current'], fact['expected_revision'] = fact_state(db, fact)
    return {'migration_id': mid, **document, 'report': result(mid)}


@router.get('/{mid}/backup')
def download_snapshot(mid: str):
    from fastapi.responses import Response
    with connect() as db:
        row = db.execute('SELECT snapshot FROM browser_migration_previews WHERE id=?', (mid,)).fetchone()
    if not row:
        raise HTTPException(404, '迁移预览不存在')
    return Response(row[0], media_type='application/json', headers={'Content-Disposition': 'attachment; filename="lingxi-browser-original.json"'})


@router.post('/{mid}/commit')
def commit(mid: str, fields: dict):
    # 成功结果固定，重复提交不同选择不能伪称已按新选择处理。
    with connect() as db:
        existing = db.execute('SELECT choices,report FROM browser_migration_results WHERE id=?', (mid,)).fetchone()
        row = db.execute('SELECT * FROM browser_migration_previews WHERE id=?', (mid,)).fetchone()
    choices = stage5.dump(fields)
    if existing:
        if existing['choices'] != choices:
            raise HTTPException(409, '此快照已经按另一组选择提交，请查看原报告')
        return json.loads(existing['report'])
    if not row:
        raise HTTPException(404, '迁移预览不存在')
    doc = json.loads(row['document'])
    selected_cards, selected_facts = fields.get('cards', []), fields.get('facts', [])
    if not isinstance(selected_cards, list) or not isinstance(selected_facts, list):
        raise HTTPException(422, '迁移选择格式错误')
    allowed_cards = {c['id']: c for c in doc['cards']}
    allowed_facts = {f['id']: f for f in doc['facts']}
    if len(selected_cards) != len(set(str(x) for x in selected_cards)) or any(str(x) not in allowed_cards for x in selected_cards):
        raise HTTPException(422, '人物卡选择无效或重复')
    chosen, targets = [], set()
    for item in selected_facts:
        if not isinstance(item, dict) or str(item.get('id')) not in allowed_facts or not isinstance(item.get('expected_revision'), int):
            raise HTTPException(422, '记忆选择或版本无效')
        fact = allowed_facts[str(item['id'])]
        target = (fact['scope'], fact['key'])
        if target in targets:
            raise HTTPException(422, '同名记忆只能明确选择一个有效版本，其余版本保留原文')
        targets.add(target)
        chosen.append((fact, item['expected_revision']))
    draft = None
    choice = fields.get('draft')
    if choice is not None:
        if not isinstance(choice, dict):
            raise HTTPException(422, '草稿选择格式错误')
        draft = next((d for d in doc['drafts'] if d['id'] == str(choice.get('id'))), None)
        if not draft or len(draft['content']) > 8000 or not isinstance(choice.get('session_id'), int) or not isinstance(choice.get('expected_revision'), int):
            raise HTTPException(422, '草稿选择、目标会话或版本无效；超长原文保留备份')
    if chat.active or chat.migrating:
        raise HTTPException(409, '生成或迁移正在执行，请先停止并处理待重试请求')
    # 已成功的旧缓存pending可安全备份；任何未成功的旧请求必须先恢复原请求。
    with connect() as db:
        if db.execute("SELECT 1 FROM stage5_attempts WHERE status='pending' AND request_id NOT IN (SELECT request_id FROM completed_requests)").fetchone():
            raise HTTPException(409, '服务端有未确认请求，请先按原请求重试')
        for p in doc['pending']:
            req = p['request']
            body = req.get('body') if isinstance(req, dict) and isinstance(req.get('body'), dict) else {}
            rid = (req.get('requestId') or body.get('request_id')) if isinstance(req, dict) else None
            if not rid or not db.execute('SELECT 1 FROM completed_requests WHERE request_id=?', (rid,)).fetchone():
                raise HTTPException(409, '旧缓存有未完成请求；下载原文，先在原会话恢复，不能迁移时丢弃')
    chat.migrating = True
    try:
        backup = migrations.backup(stage5.DATA_DIR, row['snapshot'])
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT choices,report FROM browser_migration_results WHERE id=?', (mid,)).fetchone()
            if existing:
                if existing['choices'] != choices:
                    raise HTTPException(409, '此快照已经提交')
                return json.loads(existing['report'])
            message_count = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            imported = []
            for cid in selected_cards:
                old = allowed_cards[str(cid)]
                card = stage5.save_card(old['card'], db=db)
                imported.append({'cache_key': old['cache_key'], 'old_id': old['old_id'], 'new_id': card['id']})
            for fact, revision in chosen:
                import_fact(db, fact, revision, mid)
            if draft:
                sid, expected = choice['session_id'], choice['expected_revision']
                session = db.execute('SELECT mode FROM sessions WHERE id=?', (sid,)).fetchone()
                if not session or session['mode'] != 'companion':
                    raise HTTPException(409, '旧陪伴草稿必须合并到明确的陪伴会话')
                current = db.execute('SELECT revision FROM session_drafts WHERE session_id=?', (sid,)).fetchone()
                if (current['revision'] if current else 0) != expected:
                    raise HTTPException(409, '目标草稿已更新，重新预览后选择；原文没有丢失')
                db.execute('INSERT INTO session_drafts VALUES(?,?,1) ON CONFLICT(session_id) DO UPDATE SET content=excluded.content,revision=session_drafts.revision+1', (sid, draft['content']))
            if list(db.execute('PRAGMA foreign_key_check')) or db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] != message_count:
                raise HTTPException(409, '迁移检查失败，事务回滚')
            report = {'migration_id': mid, 'at': stage5.now(), 'backup': backup, 'cards': imported,
                      'selected_facts': [f['id'] for f, _ in chosen], 'selected_draft': draft['id'] if draft else None,
                      'unselected_cards': len(doc['cards']) - len(imported), 'unselected_facts': len(doc['facts']) - len(chosen),
                      'unselected_drafts': len(doc['drafts']) - bool(draft), 'message_count': message_count,
                      'original_snapshot': '/v2/browser-migrations/' + mid + '/backup',
                      'notice': '未选择的草稿、记忆、聊天和资源引用完整保留原文，不进入模型上下文。'}
            db.execute('INSERT INTO browser_migration_results VALUES(?,?,?)', (mid, choices, stage5.dump(report)))
        return report
    except (OSError, ValueError) as exc:
        raise HTTPException(409, '迁移未提交，原数据保留：' + str(exc)) from exc
    finally:
        chat.migrating = False
