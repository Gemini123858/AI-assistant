"""QQ channel flows use synthetic identities, fake transport and offline models only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from assistant.config import Config
from assistant.errors import AssistantError
from assistant.qqbot import QQChat, QQBot, bridge_environment, credentials
from assistant.web import create_app
from .test_conversations import FlowModel, finish
from .test_tasks import call


def speaking(name, args=None, words="我看看。"):
    return call(name, {**(args or {}), "say": words})


class QQTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.temp.name) / 'data')
        self.app = create_app(self.config)
        self.store = self.app.extensions['store']
        self.conversations = self.app.extensions['conversations']
        self.sent = []
        self.chat = QQChat(self.conversations, 'synthetic-app', 'synthetic-owner', self.sent.append)

    def tearDown(self):
        for name in ('qqbot', 'conversations', 'mail', 'tasks', 'store'):
            self.app.extensions[name].close()
        self.temp.cleanup()

    def receive(self, text='你好', message_id='m1', **changes):
        self.chat.receive({'kind': 'c2c', 'senderId': 'synthetic-owner', 'messageId': message_id,
                           'text': text, **changes})

    def complete(self):
        for turn_id, (key, _) in list(self.chat.pending.items()):
            self.conversations.wait(key, turn_id)
        self.chat.poll()

    def test_owner_only_unsupported_inputs_and_duplicates(self):
        self.receive(senderId='another-user')
        self.receive(kind='group')
        self.assertEqual(self.store.list_conversations(), [])
        self.receive(hasAttachments=True)
        self.assertIn('仅支持文字', self.sent[-1]['text'])
        self.assertEqual(self.store.list_conversations(), [])
        self.receive('这条附件事件不能被重放成任务')
        self.assertEqual(self.store.list_conversations(), [])
        self.receive('x' * 12001, message_id='too-long')
        self.assertIn('12000', self.sent[-1]['text'])

    def test_same_tool_loop_history_delivery_and_restart_mapping(self):
        self.config.mode = 'api'
        model = FlowModel([speaking('list_tools', {'groups': ['notices']}),
                           speaking('create_notice', {'title': '合成通知', 'summary': '合成说明'}),
                           finish('通知已创建'), finish('第二轮回答')])
        self.app.extensions['service'].model = model
        self.receive('创建一条合成通知')
        self.complete()
        self.assertEqual(self.sent[-1]['text'], '通知已创建')
        self.assertEqual(len(self.store.list_notices()), 1)
        key = self.chat.state['conversation_id']
        self.chat.delivery({'id': self.sent[-1]['id'], 'ok': True})
        self.assertEqual(self.store.get_conversation(key)['events'][-1]['type'], 'qq_delivery')
        self.chat = QQChat(self.conversations, 'synthetic-app', 'synthetic-owner', self.sent.append)
        self.receive('重复事件不调用模型')
        self.assertEqual(len(model.calls), 3)
        self.receive('继续刚才的合成通知', message_id='m2')
        self.complete()
        self.assertEqual(self.chat.state['conversation_id'], key)
        self.assertIn('创建一条合成通知', json.dumps(model.calls[-1], ensure_ascii=False))
        self.assertIn('通知已创建', json.dumps(model.calls[-1], ensure_ascii=False))
        self.assertEqual(self.app.test_client().get('/api/conversations/' + key).status_code, 404)

    def test_slash_text_is_ordinary_input_and_does_not_reset_history(self):
        self.config.mode = 'api'
        model = FlowModel([finish('你好'), finish('接着聊')])
        self.app.extensions['service'].model = model
        self.receive('/new')
        self.complete()
        key = self.chat.state['conversation_id']
        self.receive('接着聊', message_id='second')
        self.complete()
        self.assertEqual(self.chat.state['conversation_id'], key)
        self.assertEqual(len(self.store.list_conversations()), 1)
        self.assertIn('/new', json.dumps(model.calls[-1]))
        self.assertEqual([t['function']['name'] for t in model.tools[0]], ['list_tools', 'finish_turn'])

    def test_busy_rejection_cancel_and_no_replay_after_recovery(self):
        entered, release = threading.Event(), threading.Event()
        def delayed(_messages):
            entered.set()
            release.wait(5)
            return finish('合成回答')
        self.config.mode = 'api'
        model = FlowModel([delayed])
        self.app.extensions['service'].model = model
        try:
            self.receive('执行一个合成请求')
            self.assertTrue(entered.wait(2))
            self.receive('执行中输入', message_id='busy')
            self.assertIn('处理中', self.sent[-1]['text'])
            self.receive('/new', message_id='new-busy')
            self.assertIn('处理中', self.sent[-1]['text'])
            key, turn_id = next((key, tid) for tid, (key, _) in self.chat.pending.items())
            self.conversations.cancel(key, turn_id)
        finally:
            release.set()
            self.complete()
        record = self.store.get_conversation(self.chat.state['conversation_id'])
        self.assertEqual(record['turns'][-1]['status'], 'cancelled')
        self.assertEqual(len(record['turns']), 1)
        recovered = QQChat(self.conversations, 'synthetic-app', 'synthetic-owner', self.sent.append)
        recovered.poll()
        self.assertEqual(recovered.pending, {})

    def test_delivery_failure_records_result_and_does_not_retry(self):
        self.receive()
        self.complete()
        command = self.sent[-1]
        self.chat.delivery({'id': command['id'], 'ok': False})
        self.chat.poll()
        self.assertEqual(len(self.sent), 1)
        record = self.store.get_conversation(self.chat.state['conversation_id'])
        self.assertEqual(record['events'][-1]['status'], 'failed')
        self.assertTrue(any(e['type'] == 'answer' for e in record['events']))
        self.chat.poll()
        self.assertEqual(len(self.sent), 1)

    def test_deleted_conversation_and_changed_owner_do_not_reuse_history(self):
        self.receive()
        self.complete()
        old = self.chat.state['conversation_id']
        self.store.delete_conversation(old)
        self.receive('删除后继续', message_id='m2')
        self.complete()
        self.assertNotEqual(old, self.chat.state['conversation_id'])
        changed = QQChat(self.conversations, 'synthetic-app', 'another-owner', self.sent.append)
        self.assertIsNone(changed.state['conversation_id'])

    def test_result_reports_latest_failure_instead_of_an_older_answer(self):
        def fail(_messages):
            raise AssistantError('合成失败', 502)
        self.config.mode = 'api'
        self.app.extensions['service'].model = FlowModel([finish('旧回答'), fail])
        self.receive('第一轮')
        self.complete()
        self.receive('第二轮', message_id='m2')
        self.complete()
        self.assertEqual(self.sent[-1]['text'], '合成失败')

    def test_disabled_and_credentials_environment_and_status_secrecy(self):
        bot = self.app.extensions['qqbot']
        with patch('assistant.qqbot.subprocess.Popen') as opened:
            bot.start()
            opened.assert_not_called()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AssistantError):
                credentials(self.config)
            auth = {'appId': 'synthetic-app', 'appSecret': 'synthetic-secret', 'userOpenid': 'synthetic-owner'}
            self.store._write_json(self.store.root / 'qqbot-credentials.json', auth)
            self.assertEqual(credentials(self.config), auth)
        with patch.dict(os.environ, {'QQBOT_APPID': 'partial', 'QQBOT_SECRET': '', 'QQBOT_OWNER_OPENID': ''}):
            with self.assertRaises(AssistantError):
                credentials(self.config)
        with patch.dict(os.environ, {'ASSISTANT_API_KEY': 'model-secret', 'QQ_IMAP_AUTH_CODE': 'mail-secret',
                                     'ASSISTANT_ACCESS_TOKEN': 'web-secret'}):
            self.assertNotIn('model-secret', json.dumps(bridge_environment()))
            self.assertNotIn('mail-secret', json.dumps(bridge_environment()))
            self.assertNotIn('web-secret', json.dumps(bridge_environment()))
        self.assertNotIn('synthetic-secret', self.app.test_client().get('/api/status').text)

    def test_stdio_process_lifecycle_with_synthetic_transport(self):
        script = Path(self.temp.name) / 'fake_bridge.py'
        script.write_text('''import sys, json
for line in sys.stdin:
    event = json.loads(line)
    if event['type'] == 'configure':
        print(json.dumps({'type': 'ready'}), flush=True)
        print(json.dumps({'type': 'message', 'kind': 'c2c', 'senderId': 'synthetic-owner',
                          'messageId': 'synthetic-inbound', 'text': '合成消息'}), flush=True)
    elif event['type'] == 'send':
        print(json.dumps({'type': 'delivery', 'id': event['id'], 'ok': True}), flush=True)
''', encoding='utf-8')
        bot = self.app.extensions['qqbot']
        self.config.qq_bot_enabled = True
        auth = {'appId': 'synthetic-app', 'appSecret': 'synthetic-secret', 'userOpenid': 'synthetic-owner'}
        with patch('assistant.qqbot.credentials', return_value=auth), \
             patch('assistant.qqbot.bridge_command', return_value=[sys.executable, '-u', str(script)]):
            bot.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            records = self.store.list_conversations()
            if records and any(e['type'] == 'qq_delivery' for e in records[0]['events']):
                break
            time.sleep(0.02)
        else:
            self.fail('No synthetic QQ round trip completed')
        self.assertEqual(bot.status()['state'], 'connected')
        bot.close()
        self.assertIsNotNone(bot.process.poll())
        self.assertFalse(bot.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
