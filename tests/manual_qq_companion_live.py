"""Opt-in paid-model smoke test through QQChat, with synthetic data and fake QQ transport.

Run manually with --root pointing to a dedicated test directory. The persistent ledger
counts attempted generation calls before contacting the provider, across reruns.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

from assistant.config import Config
from assistant.errors import AssistantError
from assistant.llm import APIModel
from assistant.qqbot import QQChat
from assistant.web import create_app


class BudgetModel:
    def __init__(self, config, ledger):
        self.api = APIModel(config)
        self.ledger = ledger
        self.attempts = json.loads(ledger.read_text()) if ledger.exists() else []

    def count_tokens(self, *args, **kwargs):
        return None

    def complete_tools(self, messages, tools, timeout=None):
        allowed = {'list_tools', 'finish_turn', 'remember', 'recall', 'read_chat', 'write_note', 'close_note', 'save_summary'}
        if any(t['function']['name'] not in allowed for t in tools):
            raise AssistantError('Live check only loads memory-related tools.', 400)
        if len(self.attempts) >= 20:
            raise AssistantError('Live check reached its 20-call budget.', 429)
        entry = {'attempt': len(self.attempts) + 1, 'status': 'started'}
        self.attempts.append(entry)
        self.ledger.write_text(json.dumps(self.attempts, ensure_ascii=False, indent=2))
        print(f"API attempt {entry['attempt']}/20", flush=True)
        started = time.monotonic()
        try:
            result = self.api.complete_tools(messages, tools, timeout)
            entry.update(status='received', elapsed_seconds=round(time.monotonic() - started, 2),
                         tools=[c['function']['name'] for c in result['tool_calls']], usage=result.get('_usage'))
            return result
        except Exception as error:
            entry.update(status='failed', error_type=type(error).__name__)
            raise
        finally:
            self.ledger.write_text(json.dumps(self.attempts, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--scenario', choices=['core', 'correction', 'verify'], default='core')
    args = parser.parse_args()
    root = args.root.absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = replace(Config.from_env(), data_dir=root / 'store', mode='api', token_count_mode='off',
                     qq_bot_enabled=False, qq_imap_user='', qq_imap_auth_code='', access_token='',
                     daily_calls=20, turn_calls=6)
    budget = BudgetModel(config, root / 'api-attempts.json')
    app = create_app(config, budget)
    controller, store = app.extensions['conversations'], app.extensions['store']
    sent = []
    def transport(command):
        sent.append(command)
        print('QQ:', command['text'], flush=True)
    chat = QQChat(controller, 'synthetic-live-app', 'synthetic-live-owner', transport)
    record = chat._current()
    memory = controller.companion(record['id'])
    if not memory.load()['items']:
        store.update_identity({'assistant_name': '小舟', 'user_nickname': '测试同学'})
        memory.write('memory', '虚构演练回忆：纸船项目那次排查，最后发现两份文件用了同一个缓存键，造成结果相互覆盖。换成独立缓存键后通过演练。', importance='important')
        memory.write('memory', '虚构小故事：上次一起为一只纸做的小狐狸取名叫阿橘。与纸船项目无关。')
        memory.write('note', '虚构纸船项目还有一件事：准备一个最小复现样例，等用户决定何时继续。')
    prompts = ([
        '这是虚构测试。给松果项目留个未完便签：比较小批次和大批次的耗时，现在先不做。',
        '去翻翻你保存的旧回忆：纸船项目那次排查，最后发现哪里出错了？',
        '虚构的松果耗时对比已经做完，把对应便签收掉；结果数值先不聊。',
        '实验先放一边，要是给你一个超能力，你会挑什么？',
    ] if args.scenario == 'core' else [
        '请查到纸船那条回忆并补充：缓存冲突只是演练假设，还没有实际验证，先前“通过演练”的表述不准确。'
    ] if args.scenario == 'correction' else [
        '再从你现在保存的回忆核对一下：纸船的缓存问题到底已经验证了吗？我们目前还有哪件事没收尾？简短说就好。'
    ])
    report = []
    try:
        for index, prompt in enumerate(prompts):
            remaining = 20 - len(budget.attempts)
            if not remaining:
                break
            config.turn_calls = min(6, remaining)
            before_calls, before_sent = len(budget.attempts), len(sent)
            identity = f"synthetic-{len(budget.attempts)}-{index}-{time.time_ns()}"
            chat.receive({'kind': 'c2c', 'senderId': 'synthetic-live-owner', 'messageId': identity, 'text': prompt})
            while chat.pending:
                chat.poll()
                time.sleep(.05)
            current = store.get_conversation(record['id'])
            turn = current['turns'][-1]
            events = [e for e in current['events'] if e.get('turn_id') == turn['id']]
            row = {'prompt': prompt, 'status': turn['status'], 'api_attempts': len(budget.attempts) - before_calls,
                   'tools': [c['function']['name'] for e in events if e['type'] == 'model' for c in e['message']['tool_calls']],
                   'tool_errors': [e['output'].get('error') for e in events if e['type'] == 'tool_result' and e['output'].get('failed')],
                   'responses': [c['text'] for c in sent[before_sent:]]}
            report.append(row)
            (root / f'{args.scenario}-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print('TURN:', json.dumps({k: row[k] for k in ('status', 'api_attempts', 'tools', 'tool_errors')}, ensure_ascii=False), flush=True)
            if turn['status'] != 'completed':
                break
        (root / f'{args.scenario}-memory.json').write_text(json.dumps(memory.load(), ensure_ascii=False, indent=2))
        print('TOTAL ATTEMPTS:', len(budget.attempts), flush=True)
    finally:
        for name in ('qqbot', 'conversations', 'mail', 'tasks', 'store'):
            app.extensions[name].close()


if __name__ == '__main__':
    main()
