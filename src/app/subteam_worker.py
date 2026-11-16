"""Standalone SDK worker. Only stdlib is imported before selecting its runtime."""
import asyncio
import json
import sys
import threading
import time
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer


def main():
    wire = sys.stdout
    sys.stdout = sys.stderr  # SDK logs must not corrupt the protocol.
    request = json.loads(sys.stdin.readline())
    lock = threading.Lock()

    def emit(value):
        wire.write(json.dumps(value, ensure_ascii=False) + '\n')
        wire.flush()

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with lock:
                messages = payload['messages']
                if payload.get('response_format'):
                    messages = [{'role': 'system', 'content': 'Return JSON matching this schema: ' + json.dumps(payload['response_format'])}, *messages]
                emit({'type': 'model', 'messages': messages, 'tools': payload.get('tools')})
                reply = json.loads(sys.stdin.readline())
            if 'error' in reply:
                self.send_error(502, 'Host model unavailable')
                return
            data = json.dumps({'id': 'lingxi-host', 'object': 'chat.completion',
                'created': int(time.time()), 'model': 'gpt-4o',
                'choices': [{'index': 0, 'message': reply['message'],
                             'finish_reason': 'tool_calls' if reply['message'].get('tool_calls') else 'stop'}],
                'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(('127.0.0.1', 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}/v1'

    async def run():
        task = request['task']
        if request['engine'] == 'agentscope':
            from agentscope.agent import Agent
            from agentscope.model import OpenAIChatModel
            from agentscope.credential import OpenAICredential
            from agentscope.message import Msg
            model = OpenAIChatModel(credential=OpenAICredential(api_key='host-broker', base_url=url),
                                    model='gpt-4o', stream=False, max_retries=0)
            analyst = Agent('analyst', 'Analyze the assigned task and provide an evidence-based proposal.', model)
            reviewer = Agent('reviewer', 'Review the proposal, correct errors and return the final result.', model)
            emit({'type': 'event', 'kind': 'analyst_started'})
            proposal = await analyst.reply(Msg(name='user', content=[{'type': 'text', 'text': task}], role='user'))
            emit({'type': 'event', 'kind': 'reviewer_started'})
            result = await reviewer.reply(Msg(name='user', content=[{'type': 'text', 'text': task + '\nProposal:\n' + (proposal.get_text_content() or '')}], role='user'))
            return result.get_text_content()
        if request['engine'] == 'camel':
            os.environ['TIKTOKEN_CACHE_DIR'] = str(Path(sys.executable).parent / 'tiktoken-cache')
            from camel.agents import ChatAgent
            from camel.models import ModelFactory
            from camel.types import ModelPlatformType
            from camel.societies import Workforce
            from camel.tasks import Task
            from camel.tasks.task import TaskState
            from camel.societies.workforce.workforce_callback import WorkforceCallback
            class Events(WorkforceCallback):
                def _log(self, event=None, *args, **kwargs):
                    emit({'type': 'event', 'kind': type(event).__name__,
                          'task_id': getattr(event, 'task_id', None)})
                log_all_tasks_completed = _log
                log_message = _log
                log_stream_chunk = _log
                log_task_assigned = _log
                log_task_failed = _log
                log_task_started = _log
                log_task_updated = _log
                log_worker_created = _log
                log_worker_deleted = _log
                def log_task_created(self, event):
                    emit({'type': 'event', 'kind': 'task_created', 'task_id': event.task_id})
                def log_task_decomposed(self, event):
                    emit({'type': 'event', 'kind': 'task_decomposed', 'task_id': event.parent_task_id})
                def log_task_completed(self, event):
                    emit({'type': 'event', 'kind': 'task_completed', 'task_id': event.task_id})
            def agent(prompt):
                return ChatAgent(prompt, model=ModelFactory.create(model_platform=ModelPlatformType.OPENAI,
                    model_type='gpt-4o', api_key='host-broker', url=url,
                    model_config_dict={'temperature': 0, 'stream': False}))
            workforce = Workforce('Lingxi complex task team',
                coordinator_agent=agent('Coordinate the team and assign tasks.'),
                task_agent=agent('Decompose the task into concrete subtasks.'),
                new_worker_agent=agent('Complete the assigned subtask.'), callbacks=[Events()])
            workforce.add_single_agent_worker('analysis and replanning', agent('Analyze and solve the task.'))
            emit({'type': 'event', 'kind': 'workforce_started'})
            result = await workforce.process_task_async(Task(content=task, id=request['node_id']))
            if result.state != TaskState.DONE:
                raise RuntimeError('CAMEL 未完成任务：' + str(result.state))
            return result.result or result.content
        raise ValueError('Unknown subteam engine')
    try:
        emit({'type': 'result', 'result': asyncio.run(run())})
    except Exception as exc:
        emit({'type': 'event', 'kind': 'failed', 'error': type(exc).__name__ + ': ' + str(exc)[:500]})
        emit({'type': 'error', 'error': type(exc).__name__ + ': ' + str(exc)[:500]})
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
