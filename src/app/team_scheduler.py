"""One Agno supervisor delegates persisted Lingxi nodes through the host executor."""
import asyncio
import json
from contextlib import aclosing


async def run(jid):
    from agno.agent import Agent
    from agno.team import Team
    from app import jobs
    from app.db import connect
    from app.team_model import LingxiTeamModel

    role, _ = jobs.select_model()
    locks = {}
    workers = set()

    def inspect_tasks() -> str:
        """Read task dependencies, committed results and current execution states."""
        state = jobs.snapshot(jid)
        return json.dumps({'status': state['status'], 'nodes': state['nodes']}, ensure_ascii=False)

    async def execute_task(node_id: str) -> str:
        """Execute an existing ready task by its exact ID, or return its saved result."""
        async with locks.setdefault(node_id, asyncio.Lock()):
            state = jobs.snapshot(jid)
            if state['status'] != 'running':
                raise asyncio.CancelledError('Lingxi task paused or cancelled')
            node = next((n for n in state['nodes'] if n['id'] == node_id), None)
            if node is None:
                return 'Unknown task ID'
            if node['status'] == 'complete':
                return node['result']
            completed = {n['id'] for n in state['nodes'] if n['status'] == 'complete'}
            if node['status'] != 'pending' or not set(node['document']['depends_on']) <= completed:
                return 'Task is not ready; failed or interrupted tasks require explicit retry'
            child = asyncio.create_task(jobs.worker(jid, node_id))
            workers.add(child)
            try:
                await child
            finally:
                workers.discard(child)
            updated = next(n for n in jobs.snapshot(jid)['nodes'] if n['id'] == node_id)
            return json.dumps({'id': node_id, 'status': updated['status'],
                               'result': updated['result'], 'error': updated['error']}, ensure_ascii=False)

    def member(node):
        definition = node['document']
        nid = node['id']
        async def execute_task(node_id: str) -> str:
            """Execute this member's persisted node by exact node ID."""
            if node_id != nid:
                return 'This member can only execute node ' + nid
            return await dispatch(node_id)
        return Agent(name=definition['title'], id=nid,
            model=LingxiTeamModel(id=definition.get('model') or role, job_id=jid, member_id=nid),
            tools=[execute_task], instructions=[
                'You are the persisted Lingxi member ' + nid + '. Role: ' + definition.get('role', ''),
                'Call execute_task with your exact node ID. Do not redo the work yourself or claim success without its saved result.'],
            telemetry=False, tool_call_limit=6)

    dispatch = execute_task
    team = Team(name='Lingxi supervisor', model=LingxiTeamModel(id=role, job_id=jid),
                members=[member(n) for n in jobs.snapshot(jid)['nodes']], tools=[inspect_tasks], telemetry=False,
                instructions=['Inspect the persisted task graph. Each member ID is its node ID. Delegate only ready pending nodes '
                    'to their matching member; instruct the member to call execute_task with its exact node ID. '
                    'Collect saved results and continue with newly ready nodes until no pending ready nodes remain. '
                    'Never claim a task complete without its persisted result. Do not retry failed/interrupted tasks or change dependencies.'],
                tool_call_limit=100)
    with connect() as db:
        jobs.event(db, jid, None, 'team_started', 'agno==3.1.12')
    try:
        async with aclosing(team.arun(inspect_tasks(), stream=True, stream_events=True)) as updates:
            async for update in updates:
                kind = str(getattr(update, 'event', 'team_event'))
                detail = {key: getattr(update, key, None) for key in ('agent_id', 'team_id', 'run_id')}
                with connect() as db:
                    jobs.event(db, jid, detail['agent_id'] if detail['agent_id'] in {n['id'] for n in jobs.snapshot(jid)['nodes']} else None,
                               'team_event', json.dumps({'kind': kind, **detail}))
                current = jobs.snapshot(jid)
                if current['status'] == 'pausing' and not any(n['status'] == 'running' for n in current['nodes']):
                    break
    finally:
        for child in list(workers):
            child.cancel()
        if workers:
            await asyncio.gather(*list(workers), return_exceptions=True)
    state = jobs.snapshot(jid)
    if state['status'] != 'running':
        return
    if all(n['status'] == 'complete' for n in state['nodes']):
        # Existing reviewer handles the final report and explicit acceptance.
        await jobs._schedule(jid)
    else:
        with connect() as db:
            db.execute("UPDATE work_jobs SET status='blocked',revision=revision+1 WHERE id=? AND status='running'", (jid,))
            jobs.event(db, jid, None, 'team_incomplete', '团队已返回，但仍有未完成节点')
