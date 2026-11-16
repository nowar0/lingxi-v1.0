"""Restore only memory changes caused by one reversible message removal."""
import json
from app import stage5, timeline
from app.memory import stage7, summarizer


def capture(db, sid):
    facts = [dict(row) for row in db.execute("SELECT id,scope,owner_id,key,status,source FROM fact_versions WHERE status!='withdrawn'")
             if not timeline.source_archived(db, json.loads(row['source']))]
    heads = {(row['scope'], row['owner_id'], row['key']): row['head'] for row in db.execute(
        "SELECT scope,owner_id,key,MAX(id) AS head FROM fact_versions WHERE status!='withdrawn' GROUP BY scope,owner_id,key")}
    events = []
    for row in db.execute('SELECT * FROM memory7_event_versions'):
        doc = json.loads(row['document'])
        if doc.get('status') != 'withdrawn' and not timeline.source_archived(db, doc.get('source', {})):
            events.append({'event_id': row['event_id'], 'revision': row['revision'], 'document': doc})
    return {'facts': facts, 'heads': heads, 'events': events,
            'event_rows': {row['id']: dict(row) for row in db.execute('SELECT * FROM events')},
            'summaries': [dict(row) for row in db.execute("SELECT id,source FROM memory7_summaries WHERE session_id=? AND status='active'", (sid,))
                          if summarizer.source_valid(db, sid, json.loads(row['source']))]}


def changed(db, before):
    groups = {}
    for row in before['facts']:
        current = db.execute('SELECT status FROM fact_versions WHERE id=?', (row['id'],)).fetchone()
        if not current or current['status'] != 'withdrawn':
            continue
        key = (row['scope'], row['owner_id'], row['key'])
        if key not in groups:
            head = db.execute('SELECT MAX(id) FROM fact_versions WHERE scope=? AND owner_id=? AND key=?', key).fetchone()[0]
            groups[key] = {'scope': key[0], 'owner_id': key[1], 'key': key[2],
                           'expected_head': head, 'before_head': before['heads'][key], 'versions': []}
        groups[key]['versions'].append({'id': row['id'], 'status': row['status']})
    events = []
    for row in before['events']:
        current = db.execute('SELECT document FROM memory7_event_versions WHERE event_id=? AND revision=?',
                             (row['event_id'], row['revision'])).fetchone()
        if current and json.loads(current['document']).get('status') == 'withdrawn':
            events.append(row)
    event_ids = {row['event_id'] for row in events}
    event_heads = {str(eid): db.execute('SELECT MAX(revision) FROM memory7_event_versions WHERE event_id=?', (eid,)).fetchone()[0]
                   for eid in event_ids}
    summaries = [row for row in before['summaries'] if db.execute(
        "SELECT 1 FROM memory7_summaries WHERE id=? AND status='withdrawn'", (row['id'],)).fetchone()]
    return {'facts': list(groups.values()), 'events': events, 'event_heads': event_heads,
            'event_rows': {str(eid): before['event_rows'][eid] for eid in event_ids if eid in before['event_rows']},
            'summaries': summaries}


def restore(db, sid, document, action_id):
    report = {'facts_restored': 0, 'events_restored': 0, 'summaries_restored': 0, 'skipped_newer': 0}
    groups = []
    for group in document.get('facts', []):
        key = (group['scope'], group['owner_id'], group['key'])
        head = db.execute('SELECT MAX(id) FROM fact_versions WHERE scope=? AND owner_id=? AND key=?', key).fetchone()[0]
        if head != group['expected_head']:
            report['skipped_newer'] += 1
            continue
        groups.append(group)
    # Follow shared-source dependencies without reviving independently withdrawn sources.
    pending = [version for group in groups for version in group['versions']]
    while pending:
        remaining, progress = [], False
        for version in pending:
            row = db.execute('SELECT status,source FROM fact_versions WHERE id=?', (version['id'],)).fetchone()
            if row and row['status'] == 'withdrawn' and not timeline.source_archived(db, json.loads(row['source'])):
                db.execute('UPDATE fact_versions SET status=? WHERE id=?', (version['status'], version['id']))
                progress = True
            else:
                remaining.append(version)
        if not progress:
            break
        pending = remaining
    remaining = groups
    while remaining:
        waiting, progress = [], False
        for group in remaining:
            row = db.execute('SELECT * FROM fact_versions WHERE id=?', (group['before_head'],)).fetchone()
            if not row or row['status'] != 'active' or not stage7.source_valid(db, json.loads(row['source'])):
                waiting.append(group)
                continue
            key = (group['scope'], group['owner_id'], group['key'])
            if row['id'] != group['expected_head']:
                source = {**json.loads(row['source']), 'restored_from_version_id': row['id'], 'undo_action_id': action_id}
                db.execute("""INSERT INTO fact_versions(scope,owner_id,key,value,source,at,status,category,occurred_at,confirmed_at,pinned)
                    VALUES(?,?,?,?,?,?,'active',?,?,?,?)""",
                    (*key, row['value'], stage5.dump(source), stage5.now(), row['category'], row['occurred_at'], row['confirmed_at'], row['pinned']))
            if group['scope'] == 'work':
                db.execute('INSERT INTO work_facts VALUES(?,?,?,?) ON CONFLICT(session_id,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                           (group['owner_id'], group['key'], row['value'], stage5.now()))
            else:
                db.execute('INSERT INTO facts VALUES(?,?,?,?) ON CONFLICT(mode,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                           (group['key'], row['value'], group['scope'], stage5.now()))
            report['facts_restored'] += 1
            progress = True
        if not progress:
            break
        remaining = waiting
    valid_event_ids = set()
    for event in document.get('events', []):
        eid = event['event_id']
        head = db.execute('SELECT MAX(revision) FROM memory7_event_versions WHERE event_id=?', (eid,)).fetchone()[0]
        if head != document.get('event_heads', {}).get(str(eid)):
            continue
        doc = event['document']
        if stage7.source_valid(db, doc.get('source', {})):
            db.execute('UPDATE memory7_event_versions SET document=? WHERE event_id=? AND revision=?',
                       (stage5.dump(doc), eid, event['revision']))
            valid_event_ids.add(eid)
    for eid in valid_event_ids:
        row = document.get('event_rows', {}).get(str(eid))
        head = db.execute('SELECT revision,document FROM memory7_event_versions WHERE event_id=? ORDER BY revision DESC LIMIT 1', (eid,)).fetchone()
        if not row or not head:
            continue
        doc = json.loads(head['document'])
        if doc.get('status') != 'active' or not stage7.source_valid(db, doc.get('source', {})):
            continue
        db.execute('INSERT OR IGNORE INTO events(id,content,mode,at) VALUES(?,?,?,?)', (eid, row['content'], row['mode'], row['at']))
        db.execute('INSERT INTO memory7_event_versions VALUES(?,?,?)', (eid, head['revision'] + 1, stage5.dump(doc)))
        report['events_restored'] += 1
    for row in document.get('summaries', []):
        if summarizer.source_valid(db, sid, json.loads(row['source'])):
            report['summaries_restored'] += db.execute("UPDATE memory7_summaries SET status='active' WHERE id=? AND source=? AND status='withdrawn'",
                                                     (row['id'], row['source'])).rowcount
    return report
