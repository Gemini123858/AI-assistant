"""Synthetic QQ companion flows; no accounts, cloud APIs or private fixtures."""
import copy
import io
import json
import re
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from assistant.companion import CompanionMemory
from assistant.config import Config
from assistant.errors import AssistantError
from assistant.llm import APIModel, validate_tool_message
from assistant.storage import Store
from assistant.web import create_app
from .test_conversations import FlowModel, finish
from .test_qqbot import speaking
from .test_tasks import call
from assistant.qqbot import QQChat


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'data')
        self.key = self.store.create_conversation(channel='qq')['id']
        self.memory = CompanionMemory(self.store, self.key)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_memory_revision_survives_reopen_and_scope_is_separate(self):
        first = self.memory.write('memory', '那晚终于跑通数据映射实验。', importance='important',
                                  refs=[{'kind': 'task', 'id': 'synthetic-task'}])
        updated = self.memory.write('memory', '那晚跑通实验，后来确认是数据映射问题。', id=first['id'])
        reopened = CompanionMemory(self.store, self.key)
        self.assertEqual(reopened.search(query='数据映射')['matches'][0]['text'], updated['text'])
        self.assertEqual(updated['created_at'], first['created_at'])
        self.assertEqual(updated['version'], 2)
        self.assertEqual(reopened.search(ref={'kind': 'task', 'id': 'synthetic-task'})['total'], 1)
        self.assertTrue((self.memory.root / f"{first['id']}-r1.md").exists())
        other = CompanionMemory(self.store, self.store.create_conversation(channel='qq')['id'])
        self.assertEqual(other.search(query='数据映射')['matches'], [])

    def test_decay_deep_recall_partial_recovery_and_no_false_fallback(self):
        item = self.memory.write('memory', '那次数据映射实验终于跑通了。', importance='important')
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=180)
        data = self.memory.load()
        data['items'][item['id']]['active_at'] = old.isoformat()
        self.memory._save(data)
        self.assertAlmostEqual(self.memory.activity(data['items'][item['id']], now), .0625)
        self.assertEqual(self.memory.search(query='数据映射')['matches'], [])
        self.assertEqual(len(self.memory.search(query='数据映射', deep=True)['matches']), 1)
        recovered = datetime.fromisoformat(self.memory.load()['items'][item['id']]['active_at'])
        self.assertAlmostEqual((now - recovered).total_seconds() / 86400, 90, places=4)
        self.assertEqual(self.memory.search(query='完全无关的报名通知')['matches'], [])
        self.memory.write('memory', item['text'], id=item['id'], pinned=True)
        self.assertEqual(self.memory.activity(self.memory.load()['items'][item['id']], now + timedelta(days=10000)), 4)

    def test_notes_stay_active_then_close_without_creating_memory(self):
        note = self.memory.write('note', '材料还没查完，等用户回来接着看。')
        self.assertEqual(self.memory.unfinished()['total'], 1)
        self.memory.close_note(note['id'])
        self.assertEqual(self.memory.unfinished()['total'], 0)
        self.assertEqual(self.memory.search(kind='note')['matches'], [])
        self.assertEqual(self.memory.search(kind='memory')['matches'], [])
        self.assertEqual(len(self.memory.search(kind='note', deep=True)['matches']), 1)

    def test_local_day_lookup_and_pagination(self):
        a = self.memory.write('memory', '合成实验甲。')
        b = self.memory.write('memory', '合成实验乙。')
        data = self.memory.load()
        for item in data['items'].values():
            item['created_at'] = '2026-11-04T18:00:00+00:00'
        self.memory._save(data)
        self.assertEqual(self.memory.search(date='2026-11-04')['total'], 0)
        first = self.memory.search(date='2026-11-05', limit=1)
        second = self.memory.search(date='2026-11-05', limit=1, offset=1)
        self.assertTrue(first['has_more'])
        self.assertNotEqual(first['matches'][0]['id'], second['matches'][0]['id'])
        self.assertEqual({first['matches'][0]['id'], second['matches'][0]['id']}, {a['id'], b['id']})

    def test_uncommitted_body_does_not_replace_active_memory(self):
        first = self.memory.write('memory', '原来的回忆。')
        with patch.object(self.memory, '_save', side_effect=OSError('synthetic disk error')):
            with self.assertRaises(OSError):
                self.memory.write('memory', '尚未提交的回忆。', id=first['id'])
        current = self.memory.search(id=first['id'])['matches'][0]
        self.assertEqual(current['text'], '原来的回忆。')
        self.assertEqual(current['version'], 1)


class CompanionFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.temp.name) / 'data', mode='api', api_key='synthetic', daily_calls=100)
        self.model = FlowModel([])
        self.app = create_app(self.config, self.model)
        self.controller = self.app.extensions['conversations']
        self.store = self.app.extensions['store']
        self.sent = []
        self.chat = QQChat(self.controller, 'synthetic-app', 'synthetic-owner', self.sent.append)
        self.client = self.app.test_client()
        token = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get('/').text).group(1)
        self.headers = {'X-CSRF-Token': token}

    def tearDown(self):
        for name in ('qqbot', 'conversations', 'mail', 'tasks', 'store'):
            self.app.extensions[name].close()
        self.temp.cleanup()

    def receive(self, message='合成交流'):
        identity = 'message-' + str(len(self.model.calls))
        self.chat.receive({'kind': 'c2c', 'senderId': 'synthetic-owner', 'messageId': identity, 'text': message})
        for turn, (key, _) in list(self.chat.pending.items()):
            self.controller.wait(key, turn)
        self.chat.poll()
        return self.store.get_conversation(self.chat.state['conversation_id'])

    def test_dynamic_tools_autonomous_memory_and_reset_per_turn(self):
        self.model.outputs = [speaking('list_tools', {'groups': ['memory']}),
                              speaking('remember', {'text': '那晚我们跑通了数据映射实验。', 'importance': 'important'}),
                              speaking('write_note', {'text': '还有一个对照实验没做。'}), finish('记下啦。'),
                              speaking('list_tools', {'groups': ['memory']}),
                              speaking('recall', {'query': '数据映射'}), finish('想起来了。')]
        counted = []
        self.model.count_tokens = lambda messages, tools, timeout: counted.append(copy.deepcopy(tools)) or 100
        record = self.receive('终于跑通了')
        self.assertEqual(record['turns'][-1]['status'], 'completed')
        self.assertEqual([t['function']['name'] for t in self.model.tools[0]], ['list_tools', 'finish_turn'])
        self.assertIn('remember', [t['function']['name'] for t in self.model.tools[1]])
        self.assertEqual(counted, self.model.tools)
        self.assertEqual(self.store.memory()['profile'], '')
        memory = self.controller.companion(record['id'])
        self.assertEqual(memory.unfinished()['total'], 1)
        self.assertEqual(memory.search(kind='memory')['total'], 1)
        self.receive('上次数据映射那个呢')
        self.assertEqual([t['function']['name'] for t in self.model.tools[4]], ['list_tools', 'finish_turn'])
        self.assertIn('还有一个对照实验没做', json.dumps(self.model.calls[4], ensure_ascii=False))
        self.assertIn('那晚我们跑通了', json.dumps(self.model.calls[-1], ensure_ascii=False))

    def test_unloaded_tool_cannot_execute_and_empty_commentary_can_recover(self):
        self.model.outputs = [speaking('create_notice', {'title': '不会执行'}),
                              call('list_tools', {'groups': ['notices']}),
                              speaking('list_tools', {'groups': ['notices']}),
                              speaking('create_notice', {'title': '可以执行'}), finish('完成啦。')]
        record = self.receive()
        self.assertEqual(record['turns'][-1]['status'], 'completed')
        self.assertEqual([n['content']['title'] for n in self.store.list_notices()], ['可以执行'])
        failures = [e for e in record['events'] if e['type'] == 'tool_result' and e['output'].get('failed')]
        self.assertEqual(len(failures), 2)

    def test_public_words_arrive_while_tool_is_running_and_final_is_not_duplicated(self):
        entered, release = threading.Event(), threading.Event()
        original = self.controller.memory_action
        def blocked(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        first = speaking('list_tools', {'groups': ['memory']}, '我想把这件事记下来。')
        second = speaking('remember', {'text': '那晚实验终于跑通了。'}, '这下可以松口气啦，我记一笔。')
        second['content'] = '这下可以松口气啦，我记一笔。'
        second['reasoning_content'] = 'SYNTHETIC_PRIVATE_REASONING'
        last = finish('记好啦。')
        last['content'] = '记好啦。'
        self.model.outputs = [first, second, last]
        with patch.object(self.controller, 'memory_action', side_effect=blocked):
            try:
                self.chat.receive({'kind': 'c2c', 'senderId': 'synthetic-owner', 'messageId': 'live', 'text': '跑通了！'})
                self.assertTrue(entered.wait(2))
                self.assertEqual([m['text'] for m in self.sent], ['我想把这件事记下来。', '这下可以松口气啦，我记一笔。'])
                key = self.chat.state['conversation_id']
                self.assertEqual(self.controller.companion(key).search()['total'], 0)
                self.assertNotIn('SYNTHETIC_PRIVATE_REASONING', json.dumps(self.sent))
                self.chat.poll()
                self.assertEqual(len(self.sent), 2)
            finally:
                release.set()
                for turn, (key, _) in list(self.chat.pending.items()):
                    self.controller.wait(key, turn)
                self.chat.poll()
        self.assertEqual([m['text'] for m in self.sent][-1], '记好啦。')
        self.assertEqual(len(self.sent), 3)
        normalized = validate_tool_message(second)
        self.assertNotIn('reasoning_content', normalized)
        self.assertEqual(normalized['content'], second['content'])

    def test_web_list_details_mutations_and_attachment_header_are_isolated(self):
        self.model.outputs = [finish('QQ私人交流')]
        record = self.receive()
        key = record['id']
        self.assertEqual(self.client.get('/api/conversations').json['conversations'], [])
        base = '/api/conversations/' + key
        for path in [base, base + '/events']:
            self.assertEqual(self.client.get(path).status_code, 404)
        for method, path, body in [('patch', base, {'title': '改名'}), ('delete', base, {}),
                                   ('post', base + '/turns', {'message': '网页插话'}),
                                   ('post', base + '/turns/' + record['turns'][-1]['id'] + '/cancel', {})]:
            self.assertEqual(getattr(self.client, method)(path, json=body, headers=self.headers).status_code, 404)
        headers = {**self.headers, 'X-Conversation-ID': key}
        self.assertEqual(self.client.post('/api/notices', json={'title': 'x'}, headers=headers).status_code, 404)
        web = self.client.post('/api/conversations', json={}, headers=self.headers).json['conversation']
        self.assertEqual(self.client.get('/api/conversations/' + web['id']).status_code, 200)

    def test_compaction_retains_original_and_can_read_back_exact_history(self):
        self.config.qq_recent_turns = 1
        self.config.qq_context_chars = 2000
        self.model.outputs = [finish('第一轮回答'), call('save_summary', {'text': '先前一起讨论了合成实验，关联细节可回查原话。'}),
                              finish('继续聊')]
        first = self.receive('独特的旧原话。' + '合成文字' * 80)
        second = self.receive('继续刚才那个')
        self.assertEqual(second['turns'][-1]['status'], 'completed')
        summaries = self.controller.companion(second['id']).load()['summaries']
        self.assertEqual(len(summaries), 1)
        self.assertIn('先前一起讨论了', json.dumps(self.model.calls[-1], ensure_ascii=False))
        self.assertNotIn('独特的旧原话', json.dumps(self.model.calls[-1], ensure_ascii=False))
        self.assertEqual(second['events'][0]['text'], first['events'][0]['text'])
        found = self.controller.memory_action(second['id'], 'read_chat', {'query': '独特的旧原话'}, {})
        self.assertEqual(found['events'][0]['text'], first['events'][0]['text'])
        self.assertEqual(second['turns'][-1]['calls'], 2)
        self.assertEqual([t['function']['name'] for t in self.model.tools[-1]], ['list_tools', 'finish_turn'])

    def test_failed_compaction_keeps_checkpoint_and_original(self):
        self.config.qq_recent_turns = 1
        self.config.qq_context_chars = 2000
        self.model.outputs = [finish('旧回答'), AssistantError('合成整理超时', 502)]
        first = self.receive('原话一直保留' * 100)
        record = self.receive('继续')
        self.assertEqual(record['turns'][-1]['status'], 'failed')
        self.assertEqual(self.controller.companion(record['id']).load()['summaries'], [])
        self.assertEqual(record['events'][0]['text'], first['events'][0]['text'])
        self.assertEqual(record['turns'][-1]['calls'], 1)

    def test_api_request_loads_real_schemas_and_keeps_public_words(self):
        self.config.token_count_mode = 'off'
        api = APIModel(self.config)
        self.app.extensions['service'].model = api
        outputs = [speaking('list_tools', {'groups': ['memory']}),
                   speaking('remember', {'text': '那晚终于跑通了实验。'}, '这件事值得记一笔。'), finish('记好啦。')]
        outputs[1]['content'] = '这件事值得记一笔。'
        responses = [io.BytesIO(json.dumps({'choices': [{'finish_reason': 'tool_calls', 'message': out}]}).encode())
                     for out in outputs]
        with patch.object(api.opener, 'open', side_effect=responses) as opened:
            record = self.receive()
        self.assertEqual(record['turns'][-1]['status'], 'completed')
        bodies = [json.loads(request.args[0].data) for request in opened.call_args_list]
        self.assertEqual([t['function']['name'] for t in bodies[0]['tools']], ['list_tools', 'finish_turn'])
        self.assertIn('remember', [t['function']['name'] for t in bodies[1]['tools']])
        self.assertIn('这件事值得记一笔。', [m['text'] for m in self.sent])
        self.assertIn('调皮', bodies[1]['messages'][0]['content'])
        self.assertEqual(self.controller.companion(record['id']).search()['total'], 1)

    def test_chat_search_uses_original_not_its_own_query_or_recall_output(self):
        self.model.outputs = [finish('已经看过了。')]
        record = self.receive('合成暗号：蓝鲸映射。' + '背景文字' * 100)
        self.model.outputs = [speaking('list_tools', {'groups': ['memory']}),
                              speaking('read_chat', {'query': '蓝鲸映射'}, '我翻翻原话。'), finish('找到了。')]
        self.receive('想起那句话了吗')
        result = json.loads(self.model.calls[-1][-1]['content'])
        self.assertEqual(result['events'][0]['type'], 'user')
        self.assertEqual(result['events'][0]['text'], record['events'][0]['text'])

    def test_legacy_mapping_and_delivered_history_are_hidden_after_startup(self):
        from assistant.conversations import Conversations
        old = self.store.create_conversation('合成旧入口')
        self.controller.event(old['id'], 'qq_delivery', turn_id=None, status='sent')
        mapped = self.store.create_conversation('合成映射入口')
        self.store._write_json(self.store.root / 'qqbot.json', {'schema_version': 1, 'conversation_id': mapped['id']})
        migrated = Conversations(self.app.extensions['service'], self.app.extensions['tasks'],
                                 self.app.extensions['mail'], self.app.extensions['drafts'])
        try:
            self.assertEqual(migrated.list(), [])
            self.assertEqual(self.store.get_conversation(old['id'])['channel'], 'qq')
            self.assertEqual(self.store.get_conversation(mapped['id'])['channel'], 'qq')
        finally:
            migrated.close()

    def test_repeated_progress_is_sent_once_but_new_turn_can_repeat(self):
        words = '我来收好这件小事。'
        self.model.outputs = [speaking('list_tools', {'groups': ['memory']}, words),
                              speaking('write_note', {'text': '合成未完事项'}, words), finish('收好了。')]
        self.receive()
        self.assertEqual([m['text'] for m in self.sent], [words, '收好了。'])
        self.model.outputs = [finish(words)]
        self.receive('再说一遍')
        self.assertEqual(self.sent[-1]['text'], words)

    def test_protocol_fragments_are_not_delivered_or_stored(self):
        bad = '合成片段</think><｜DSML｜parameter>'
        self.model.outputs = [speaking('list_tools', {'groups': ['memory']}, bad),
                              speaking('list_tools', {'groups': ['memory']}),
                              speaking('remember', {'text': bad}),
                              speaking('remember', {'text': '虚构练习里修好了一个脚本。'}), finish('收好了。')]
        record = self.receive()
        self.assertEqual(record['turns'][-1]['status'], 'completed')
        self.assertNotIn('DSML', json.dumps(self.sent))
        found = self.controller.companion(record['id']).search()
        self.assertEqual(found['total'], 1)
        self.assertNotIn('DSML', found['matches'][0]['text'])

    def test_companion_reference_finishes_and_refreshes_next_turn(self):
        self.model.outputs = [speaking('list_tools', {'groups': ['memory']}),
                              speaking('remember', {'text': '合成回忆：纸船演练中的缓存冲突。'})]
        def cite(messages):
            memory = json.loads(messages[-1]['content'])['memory']
            return finish('记好了。', [memory['ref']])
        self.model.outputs += [cite, finish('下一轮也正常。')]
        first = self.receive()
        snapshot = next(e for e in first['events'] if e['type'] == 'answer')['records'][0]
        self.assertEqual(snapshot['ref']['kind'], 'companion_memory')
        self.assertEqual(self.receive('继续')['turns'][-1]['status'], 'completed')
        old_style = self.controller.read_reference(first['id'], {'kind': 'memory', 'id': snapshot['ref']['id']})
        self.assertEqual(old_style['ref']['kind'], 'companion_memory')
        web = self.store.create_conversation()
        with self.assertRaises(AssistantError):
            self.controller.read_reference(web['id'], snapshot['ref'])

    def test_overlong_progress_is_rejected_before_send_or_tool_execution(self):
        bad = '合成的冗长中途发言。' * 40
        self.model.outputs = [speaking('list_tools', {'groups': ['memory']}),
                              speaking('write_note', {'text': '这张不应写入。'}, bad),
                              speaking('write_note', {'text': '这张正常保留。'}, '简短说一句，接着做。'),
                              finish('已收好。' * 100)]
        record = self.receive()
        self.assertEqual(record['turns'][-1]['status'], 'completed')
        self.assertNotIn(bad, [m['text'] for m in self.sent])
        found = self.controller.companion(record['id']).search(kind='note')['matches']
        self.assertEqual([item['text'] for item in found], ['这张正常保留。'])
        self.assertEqual(self.sent[-1]['text'], '已收好。' * 100)

    def test_last_call_offers_only_finish_and_persona_is_qq_only(self):
        self.config.turn_calls = 1
        self.model.outputs = [finish('你好呀。')]
        self.receive()
        names = [t['function']['name'] for t in self.model.tools[0]]
        self.assertEqual(names, ['finish_turn'])
        system = self.model.calls[0][0]['content']
        self.assertIn('调皮', system)
        self.assertIn('反骨', system)
        self.assertNotIn('严禁', system)
        web = self.store.create_conversation()
        self.assertNotIn('调皮', self.controller.context(web['id'])[0]['content'])
