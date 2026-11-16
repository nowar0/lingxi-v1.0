"""Persistent ordering for every visible chat snapshot, independent of rewinds."""

def init(db):
    db.execute('CREATE TABLE IF NOT EXISTS session_event_revisions(session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,revision INTEGER NOT NULL)')
    for table,reference in (('messages','session_id'),('stage6_batches','session_id'),('stage6_batch_records',None)):
        for operation in ('INSERT','UPDATE','DELETE'):
            row='OLD' if operation=='DELETE' else 'NEW'
            if reference:
                statement=f'INSERT INTO session_event_revisions(session_id,revision) VALUES({row}.session_id,1) ON CONFLICT(session_id) DO UPDATE SET revision=revision+1;'
            else:
                # INSERT..SELECT avoids a null owner during parent deletion.
                statement=f'INSERT INTO session_event_revisions(session_id,revision) SELECT session_id,1 FROM stage6_batches WHERE id={row}.batch_id ON CONFLICT(session_id) DO UPDATE SET revision=revision+1;'
            db.execute(f'CREATE TRIGGER IF NOT EXISTS event_revision_{table}_{operation.lower()} AFTER {operation} ON {table} BEGIN {statement} END')


def get(db,sid):
    row=db.execute('SELECT revision FROM session_event_revisions WHERE session_id=?',(sid,)).fetchone()
    return row['revision'] if row else 0
