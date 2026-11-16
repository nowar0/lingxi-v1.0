"""File-tool activity derived from existing receipts, separate from Git diff."""
import json
import os
from pathlib import Path
from fastapi import APIRouter,HTTPException,Query
from app import sessions,work_settings
from app.db import connect

router=APIRouter(prefix='/v9/workspace')
NAMES={'workspace_read','workspace_list','workspace_write','workspace_edit','workspace_command'}


@router.get('/activity')
def activity(session_id:int=Query(gt=0),query:str=Query('',max_length=200),offset:int=Query(0,ge=0,le=100000),limit:int=Query(50,ge=1,le=100),
             snapshot_at:float|None=Query(None,ge=0),snapshot_event:int|None=Query(None,ge=0),snapshot_message:int|None=Query(None,ge=0)):
    session=sessions.get_session(session_id)
    if not session or session['mode']!='work':raise HTTPException(404,'请选择有效工作会话')
    current=work_settings.get(session_id)['path']
    if any(value is None for value in (snapshot_at,snapshot_event,snapshot_message)) and any(value is not None for value in (snapshot_at,snapshot_event,snapshot_message)):
        raise HTTPException(422,'活动分页需使用同一份完整快照游标')
    sql="""WITH activities AS (
      SELECT 'chat:'||b.id||':'||printf('%010d',t.key) AS id,'chat' AS origin,b.id AS run_id,'' AS step_id,
        COALESCE(json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.started_at'),unixepoch((SELECT at FROM messages WHERE id=b.assistant_id))) AS at,
        json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.tool') AS tool,json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.args.path') AS path,
        json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.workspace.root') AS root,json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.status') AS status,
        b.status AS run_status,json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.ok') AS ok,
        substr(json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.result'),1,2000) AS result,
        json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.duration_ms') AS duration_ms,
        CASE WHEN json_valid(json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.result')) THEN json_extract(json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.result'),'$.exit_code') END AS exit_code,
        (SELECT json_extract(a.value,'$.status') FROM json_each(r.document,'$.approvals') a WHERE json_extract(a.value,'$.activity_id')=json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.activity_id') ORDER BY a.key DESC LIMIT 1) AS approval_status,
        (SELECT json_extract(a.value,'$.expires_at') FROM json_each(r.document,'$.approvals') a WHERE json_extract(a.value,'$.activity_id')=json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.activity_id') ORDER BY a.key DESC LIMIT 1) AS expires_at
      FROM stage6_batches b JOIN stage6_batch_records r ON r.batch_id=b.id,
        json_each(CASE WHEN json_valid(r.document) THEN r.document ELSE '{}' END,'$.tools') t
      WHERE b.session_id=? AND b.assistant_id<=? AND json_extract(CASE WHEN json_valid(t.value) THEN t.value ELSE '{}' END,'$.tool') IN ('workspace_read','workspace_list','workspace_write','workspace_edit','workspace_command')
      UNION ALL
      SELECT 'job:'||printf('%020d',s.seq),'task',s.job_id,s.node_id,unixepoch(s.at),
        json_extract(CASE WHEN json_valid(s.detail) THEN s.detail ELSE '{}' END,'$.name'),json_extract(CASE WHEN json_valid(s.detail) THEN s.detail ELSE '{}' END,'$.args.path'),json_extract(CASE WHEN json_valid(s.detail) THEN s.detail ELSE '{}' END,'$.workspace'),
        CASE WHEN e.event='tool_result' THEN 'complete' WHEN e.event IN ('tool_interrupted','tool_started') THEN 'unknown' ELSE 'running' END,
        n.status,CASE WHEN e.event='tool_result' THEN 1 ELSE 0 END,substr(e.detail,1,2000),NULL,
        CASE WHEN json_valid(e.detail) THEN json_extract(e.detail,'$.exit_code') END,NULL,NULL
      FROM work_events s JOIN work_jobs j ON j.id=s.job_id JOIN work_nodes n ON n.job_id=s.job_id AND n.id=s.node_id
      LEFT JOIN work_events e ON e.seq=(SELECT MIN(x.seq) FROM work_events x WHERE x.job_id=s.job_id AND x.node_id=s.node_id
        AND x.seq>s.seq AND x.event IN ('tool_result','tool_interrupted','tool_started'))
      WHERE j.session_id=? AND s.seq<=? AND s.event='tool_started' AND json_valid(s.detail)
        AND json_extract(CASE WHEN json_valid(s.detail) THEN s.detail ELSE '{}' END,'$.name') IN ('workspace_read','workspace_list','workspace_write','workspace_edit','workspace_command')
    ) SELECT * FROM activities WHERE instr(lower(COALESCE(path,'')||' '||tool),lower(?))>0 AND COALESCE(at,0)<=?
      ORDER BY at DESC,id DESC LIMIT ? OFFSET ?"""
    with connect() as db:
        db.execute('BEGIN')
        if snapshot_at is None:
            import time
            snapshot_at=time.time()
            snapshot_event=db.execute('SELECT COALESCE(MAX(e.seq),0) FROM work_events e JOIN work_jobs j ON j.id=e.job_id WHERE j.session_id=?',(session_id,)).fetchone()[0]
            snapshot_message=db.execute('SELECT COALESCE(MAX(assistant_id),0) FROM stage6_batches WHERE session_id=?',(session_id,)).fetchone()[0]
        rows=[dict(row) for row in db.execute(sql,(session_id,snapshot_message,session_id,snapshot_event,query,snapshot_at,limit+1,offset))]

    items=[]
    for row in rows[:limit]:
        status=row['status'] or ('complete' if row['ok'] else 'failed')
        if status=='waiting_approval':
            import time
            if row['approval_status']=='approved':status='running'
            elif row['approval_status']=='denied':status='denied'
            elif row['expires_at'] and row['expires_at']<=time.time():status='expired'
        if status in ('running','waiting_approval') and row['run_status'] not in ('running','generating'):
            status='unknown' if row['tool'] in ('workspace_write','workspace_edit','workspace_command') else 'stopped'
        summary={'workspace_read':'读取文件','workspace_list':'列出目录','workspace_write':'写入文件','workspace_edit':'编辑片段','workspace_command':'执行命令'}[row['tool']]
        if status=='complete' and row['tool']=='workspace_command' and row['exit_code'] not in (None,0):
            status='failed';summary+=' · 退出码 '+str(row['exit_code'])
        path=row['path'] or ''
        matches=bool(current and row['root'] and os.path.normcase(str(Path(current).resolve()))==os.path.normcase(str(Path(row['root']).resolve())))
        relative=bool(path and not Path(path).is_absolute() and not Path(path).drive and '..' not in Path(path).parts and ':' not in path)
        row.update(status=status,summary=summary,path=path,root=row['root'] or '',at=row['at'] or 0,
                   locatable=matches and relative and row['tool'] not in ('workspace_list','workspace_command'),
                   location_reason='' if matches else '目录已改变' if row['root'] else '旧活动未登记原目录',
                   result=(row['result'] or '') if status not in ('complete','running','waiting_approval') else '')
        items.append(row)
    return {'items':items,'more':len(rows)>limit,'next_offset':offset+limit if len(rows)>limit else None,
            'snapshot':{'snapshot_at':snapshot_at,'snapshot_event':snapshot_event,'snapshot_message':snapshot_message}}
