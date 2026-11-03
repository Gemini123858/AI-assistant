"""Offline contracts for the code-browser model loop and container command."""
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant.browser import BrowserScope
from assistant.browser_runtime import BrowserRuntime
from assistant.config import Config
from assistant.service import Service
from assistant.storage import Store
from assistant.tasks import Tasks, validate_result
from assistant.web import create_app
from .test_tasks import FixtureBrowser, ScriptModel, call


IMAGE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aVioAAAAASUVORK5CYII='


class CodeFixture(FixtureBrowser):
    python_browser = True
    image = None

    def read(self, url):
        page = super().read(url)
        self.image = IMAGE
        return {**page, 'observation_status': 'success', 'execution_status': 'success',
                'stdout': '', 'stderr': '', 'execution_error': None}

    def run(self, code, timeout=30, observe=True):
        self.image = IMAGE if observe else None
        execution = {'execution_status': 'failed' if code == 'fail' else 'success',
                     'execution_error': 'Synthetic exception after action' if code == 'fail' else None,
                     'stdout': 'x' * 9000, 'stderr': 'diagnostic', 'observation_status': 'success' if observe else 'skipped'}
        return {**copy.deepcopy(self.current), **execution} if observe else execution


class BrowserRuntimeTests(unittest.TestCase):
    def test_screenshot_save_failure_does_not_erase_successful_execution(self):
        from assistant.errors import AssistantError
        model = ScriptModel([
            call('browser_run', {'code': 'print result'}),
            call('finish_task', {'summary': '已取得正文', 'questions': [], 'outcome': 'completed',
                                'findings': [{'page_id': 'p2', 'text': '正文证据', 'line_start': 1,
                                              'line_end': 1, 'uncertainties': []}]})])
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'data')
            tasks = Tasks(Service(store, Config(), model), CodeFixture)
            try:
                with patch.object(store, 'save_task_screenshot', side_effect=AssistantError('合成截图保存失败', 500)):
                    task = tasks.start({'goal': '合成查询', 'start_url': 'https://catalog.example'})
                    tasks.wait()
                task = store.get_task(task['id'])
                action = task['actions'][1]
                self.assertEqual(action['execution']['execution_status'], 'success')
                self.assertEqual(action['observation_status'], 'incomplete')
                self.assertEqual(action['status'], 'incomplete')
                self.assertEqual(task['status'], 'completed')
                self.assertIn('合成截图保存失败', action['observation_errors'][0])
            finally:
                tasks.close()
                store.close()

    def test_screenshot_route_is_authenticated_and_does_not_accept_file_paths(self):
        model = ScriptModel([call('finish_task', {'summary': '合成结果', 'questions': []})])
        with tempfile.TemporaryDirectory() as directory:
            token = 'synthetic-browser-access-token-long-enough'
            app = create_app(Config(data_dir=Path(directory) / 'data', access_token=token), model, CodeFixture)
            try:
                tasks = app.extensions['tasks']
                task = tasks.start({'goal': '合成观察', 'start_url': 'https://catalog.example'})
                tasks.wait()
                client = app.test_client()
                path = f'/api/tasks/{task["id"]}/screenshots/1'
                self.assertEqual(client.get(path).status_code, 401)
                login = client.get('/login').get_data(as_text=True)
                csrf = re.search(r'name="csrf_token" value="([^"]+)"', login).group(1)
                client.post('/login', data={'token': token, 'csrf_token': csrf})
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, 'image/png')
                self.assertTrue(response.data.startswith(b'\x89PNG'))
                self.assertEqual(client.get(f'/api/tasks/{task["id"]}/screenshots/0').status_code, 400)
            finally:
                app.extensions['mail'].close()
                tasks.close()
                app.extensions['store'].close()

    def test_docker_command_has_no_host_network_or_credentials_or_application_mount(self):
        runtime = BrowserRuntime(lambda: None, config=Config(), scope=BrowserScope('https://catalog.example'))
        command = runtime.docker_command('/tmp/synthetic-gateway')
        self.assertIn('--network=none', command)
        self.assertIn('--cap-drop=ALL', command)
        self.assertIn('--read-only', command)
        self.assertIn('BROWSER_SANDBOX=true', command)
        self.assertIn('--pull=never', command)
        mounts = [command[i + 1] for i, argument in enumerate(command) if argument == '--mount']
        self.assertEqual(mounts, ['type=bind,source=/tmp/synthetic-gateway,target=/gateway,readonly'])
        self.assertNotIn('--privileged', command)
        self.assertNotIn('--network=host', command)
        self.assertNotIn('ASSISTANT_API_KEY', ' '.join(command))

    def test_model_gets_images_errors_and_pageable_output_without_replaying_code(self):
        model = ScriptModel([
            call('browser_run', {'code': 'fail'}),
            call('read_browser_output', {'step': 2, 'stream': 'stdout', 'start': 8000, 'characters': 16000}),
            call('finish_task', {'summary': '合成观察结果', 'questions': [], 'outcome': 'completed',
                                'findings': [{'page_id': 'p2', 'text': '截图中的配置',
                                              'evidence_type': 'screenshot', 'uncertainties': []}]})])
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'data')
            tasks = Tasks(Service(store, Config(), model), CodeFixture)
            try:
                task = tasks.start({'goal': '合成配置查询', 'start_url': 'https://catalog.example'})
                tasks.wait()
                task = store.get_task(task['id'])
                self.assertEqual(task['status'], 'completed')
                self.assertEqual(task['actions'][1]['status'], 'failed')
                self.assertEqual(task['actions'][1]['execution']['stdout'], 'x' * 9000)
                self.assertEqual(task['actions'][1]['observation_status'], 'success')
                tools = {t['function']['name'] for t in model.tools[0]}
                self.assertIn('browser_run', tools)
                self.assertNotIn('click_element', tools)
                for messages in model.calls[:2]:
                    self.assertTrue(any(isinstance(m.get('content'), list) and any(
                        b.get('type') == 'image_url' for b in m['content']) for m in messages))
                feedback = next(json.loads(m['content']) for m in reversed(model.calls[1]) if m['role'] == 'tool')
                self.assertEqual(feedback['execution']['execution_status'], 'failed')
                self.assertEqual(len(feedback['execution']['stdout']), 8000)
                output = json.loads(model.calls[2][-1]['content'])
                self.assertEqual(output['text'], 'x' * 1000)
                self.assertIsNone(output['next_start'])
                self.assertEqual(store.task_screenshot(task['id'], 2), IMAGE)
                self.assertFalse(store._read_json(store.root / 'screenshots' / f'{task["id"]}-2.json')['masked'])
                self.assertEqual(len([a for a in task['actions'] if a['tool'] == 'browser_run']), 1)
            finally:
                tasks.close()
                store.close()

    def test_visual_evidence_requires_a_saved_screenshot(self):
        result = {'summary': '合成', 'questions': [], 'findings': [
            {'page_id': 'p1', 'text': '视觉结果', 'evidence_type': 'screenshot', 'uncertainties': []}]}
        task = {'pages': [{'id': 'p1', 'text': '正文', 'ready': True}], 'memory': {}}
        from assistant.errors import AssistantError
        with self.assertRaises(AssistantError):
            validate_result(result, task)
