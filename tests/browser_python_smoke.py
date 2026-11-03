"""Local code-worker smoke: synthetic Chromium pages, no Docker or external API."""
import tempfile
import threading
import time
from pathlib import Path

from assistant.browser import BrowserScope
from assistant.browser_runtime import BrowserRuntime
from assistant.config import Config
from assistant.service import Service
from assistant.storage import Store
from assistant.tasks import Tasks
from .test_tasks import ScriptModel, call


HTML = '''<html><head><title>合成配置页</title><style>
label {display:block;padding:20px;background:#eee;margin:10px}
</style></head><body><h1>配置</h1>
<input id="small" type="radio" name="size" value="small" checked><label for="small">小尺寸</label>
<input id="large" type="radio" name="size" value="large"><label for="large">大尺寸</label>
<button onclick="setTimeout(()=>document.getElementById('price').textContent='25000',600)">升级内存</button>
<p id="price">20000</p></body></html>'''


def main():
    config = Config(browser_backend='local')
    runtime = BrowserRuntime(lambda: None, config=config, scope=BrowserScope('https://catalog.example', public_web=True))
    with runtime:
        first = runtime.run(f"""secret_check = __import__('os').environ.get('ASSISTANT_API_KEY')
assert secret_check is None
count = 40
async def fixture(route):
    await route.fulfill(status=200, content_type='text/html; charset=utf-8', body={HTML!r})
await context.route('https://catalog.example/**', fixture)
await page.goto('https://catalog.example/config')
print('initial', count)
""")
        assert first['execution_status'] == 'success', first
        assert runtime.image.startswith('data:image/png;base64,')
        assert '20000' in first['text']
        changed = runtime.run("""count += 2
await page.get_by_text('大尺寸', exact=True).click()
await page.get_by_role('button', name='升级内存').click()
await page.locator('#price').filter(has_text='25000').wait_for()
print(count, await page.locator('#large').is_checked(), await page.locator('#price').inner_text())
""")
        assert changed['stdout'].strip() == '42 True 25000', changed
        assert '25000' in changed['text']
        failed = runtime.run("count += 1\nraise RuntimeError('synthetic partial failure')")
        assert failed['execution_status'] == 'failed' and failed['observation_status'] == 'success'
        assert runtime.image
        recovered = runtime.run("print(count)", observe=False)
        assert recovered['stdout'].strip() == '43', recovered
        assert runtime.image is None
        output = runtime.run("import os, subprocess\nos.write(1,b'os output\\n')\nsubprocess.run(['printf','child output'])", observe=False)
        assert 'os output' in output['stdout'] and 'child output' in output['stdout'], output
        timeout = runtime.run('await asyncio.sleep(10)', timeout=1)
        assert timeout['execution_status'] == 'timeout' and timeout['observation_status'] == 'success', timeout
        assert runtime.run('print(count)', observe=False)['stdout'].strip() == '43'
        background = runtime.run("""handled = 0
async def background_route(route):
    global handled
    handled += 1
    print('background handled')
    await route.fulfill(status=200, body='ok')
await context.route('**/background', background_route)
await page.evaluate("setTimeout(()=>fetch('/background'), 300)")
""", observe=False)
        assert background['execution_status'] == 'success', background
        time.sleep(.9)
        background = runtime.run('assert handled == 1', observe=False)
        assert background['execution_status'] == 'success', background
        assert 'background handled' in background['stdout'], background
    assert runtime.process is None
    # Exercise the production task loop with a real worker and a scripted model.
    class RoutedRuntime(BrowserRuntime):
        python_browser = True

        def __init__(self, check):
            super().__init__(check, config=config, scope=BrowserScope('https://catalog.example', public_web=True))

        def __enter__(self):
            super().__enter__()
            setup = self.run(f"async def fixture(route):\n    await route.fulfill(status=200, content_type='text/html; charset=utf-8', body={HTML!r})\nawait context.route('https://catalog.example/**', fixture)", observe=False)
            assert setup['execution_status'] == 'success', setup
            return self

    model = ScriptModel([
        call('browser_run', {'code': "await page.get_by_text('大尺寸', exact=True).click()\nawait page.get_by_role('button', name='升级内存').click()\nawait page.locator('#price').filter(has_text='25000').wait_for()\nprint(await page.locator('#price').inner_text())"}),
        call('finish_task', {'summary': '已核对配置及价格', 'questions': [], 'outcome': 'completed',
                            'findings': [{'page_id': 'p2', 'text': '选中大尺寸，当前价格25000',
                                          'evidence_type': 'screenshot', 'uncertainties': []}]})])
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / 'data')
        tasks = Tasks(Service(store, config, model), RoutedRuntime)
        try:
            task = tasks.start({'goal': '合成配置选价', 'start_url': 'https://catalog.example/config'})
            tasks.wait(timeout=30)
            task = store.get_task(task['id'])
            assert task['status'] == 'completed', task
            assert task['actions'][1]['execution']['stdout'].strip() == '25000', task
            assert task['pages'][1]['screenshot']
            assert any(isinstance(m.get('content'), list) for m in model.calls[1])
        finally:
            tasks.close()
            store.close()
    # Host cancellation must stop even a CPU-bound snippet, not just await calls.
    cancelled = threading.Event()
    def check_cancelled():
        if cancelled.is_set():
            raise RuntimeError('synthetic cancellation')
    runtime = BrowserRuntime(check_cancelled, config=config, scope=BrowserScope('https://catalog.example'))
    with runtime:
        timer = threading.Timer(.5, cancelled.set)
        timer.start()
        began = time.monotonic()
        try:
            runtime.run('while True: pass', timeout=30, observe=False)
            raise AssertionError('CPU loop did not stop')
        except RuntimeError as error:
            assert str(error) == 'synthetic cancellation'
        finally:
            timer.join()
        assert time.monotonic() - began < 5
        assert runtime.process is None
    print('Passed: persistent variables, config/price, screenshots, partial failure/recovery, output capture, async timeout, background events, full task loop, CPU-loop cancellation and cleanup. No Docker/model API.')


if __name__ == '__main__':
    main()
