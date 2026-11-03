"""Standalone persistent Python/Playwright worker, also copied into the browser image.

This is a code executor, not a Python sandbox. Docker is the isolation boundary.
No application imports, credentials or host data are needed here.
"""

import ast
import asyncio
import base64
import inspect
import json
import os
from pathlib import Path
import select
import socket
import socketserver
import sys
import tempfile
import threading
import traceback
from datetime import datetime, timezone

from playwright.async_api import async_playwright


def relay(client, upstream):
    with upstream:
        while True:
            readable, _, _ = select.select([client, upstream], [], [], 1)
            for source in readable:
                chunk = source.recv(65536)
                if not chunk:
                    return
                (upstream if source is client else client).sendall(chunk)


def proxy_bridge(path):
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                upstream = socket.socket(socket.AF_UNIX)
                upstream.connect(path)
                relay(self.request, upstream)
            except OSError:
                pass

    server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def observe(scope):
    result = {'observation_status': 'success', 'tabs': [], 'observation_errors': []}
    context, page = scope['context'], scope['page']
    try:
        pages = context.pages
        result['tabs'] = [{'tab_id': f't{n + 1}', 'url': p.url} for n, p in enumerate(pages)]
        if page.is_closed() and pages:
            page = scope['page'] = pages[-1]
        result.update(url=page.url, tab_id=f't{pages.index(page) + 1}' if page in pages else None,
                      viewport=page.viewport_size, fetched_at=datetime.now(timezone.utc).isoformat())
    except Exception as error:
        result['observation_errors'].append('tabs: ' + str(error))
    # Capture channels independently: losing text must not lose an available image.
    try:
        raw = await page.screenshot(type='png', timeout=8000)
        result['image'] = 'data:image/png;base64,' + base64.b64encode(raw).decode('ascii')
    except Exception as error:
        result['observation_errors'].append('screenshot: ' + str(error))
    try:
        text = await page.locator('body').inner_text(timeout=8000)
        result.update(text=text[:65536], text_truncated=len(text) > 65536)
    except Exception as error:
        result['observation_errors'].append('body: ' + str(error))
    try:
        result['title'] = (await asyncio.wait_for(page.title(), 3))[:300]
    except Exception as error:
        result['observation_errors'].append('title: ' + str(error))
    if result['observation_errors']:
        result['observation_status'] = 'incomplete'
    return result


async def main():
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', buffering=1)
    work = Path(os.environ['BROWSER_WORKDIR']) if os.environ.get('BROWSER_WORKDIR') else Path(tempfile.mkdtemp(prefix='browser-python-'))
    os.chdir(work)
    bridge = proxy_bridge(os.environ['BROWSER_GATEWAY_SOCKET']) if os.environ.get('BROWSER_GATEWAY_SOCKET') else None
    async with async_playwright() as pw:
        launch = {'headless': True, 'channel': 'chromium',
                  'chromium_sandbox': os.environ.get('BROWSER_SANDBOX') == 'true'}
        if bridge:
            launch['proxy'] = {'server': f'http://127.0.0.1:{bridge.server_address[1]}', 'bypass': '<-loopback>'}
            launch['args'] = ['--disable-quic', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']
        browser = await pw.chromium.launch(**launch)
        context = await browser.new_context(locale='zh-CN', viewport={'width': 1440, 'height': 1000},
                                            service_workers='block', accept_downloads=False)
        context.set_default_timeout(10000)
        context.set_default_navigation_timeout(30000)
        scope = {'__builtins__': __builtins__, 'asyncio': asyncio, 'browser': browser,
                 'context': context, 'page': await context.new_page()}
        stdout_path, stderr_path = work / 'stdout.txt', work / 'stderr.txt'
        logs = [stdout_path.open('w'), stderr_path.open('w')]
        for target, log in zip((1, 2), logs):
            os.dup2(log.fileno(), target)
        offsets = {'stdout': 0, 'stderr': 0}
        protocol.write(json.dumps({'ready': True}) + '\n')
        while True:
            # Keep servicing Playwright events while the model is thinking.
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            request = json.loads(line)
            if request.get('close'):
                break
            result = {'execution_status': 'success', 'execution_error': None}
            try:
                code = compile(request['code'], '<browser_run>', 'exec', flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                value = eval(code, scope)
                if inspect.isawaitable(value):
                    await asyncio.wait_for(value, timeout=request['timeout'])
            except BaseException as error:
                result.update(execution_status='timeout' if isinstance(error, asyncio.TimeoutError) else 'failed',
                              execution_error=''.join(traceback.format_exception(type(error), error, error.__traceback__)))
            if request.get('observe', True):
                result.update(await observe(scope))
            else:
                result['observation_status'] = 'skipped'
            sys.stdout.flush()
            sys.stderr.flush()
            for name, path in [('stdout', stdout_path), ('stderr', stderr_path)]:
                with path.open(errors='replace') as source:
                    source.seek(offsets[name])
                    result[name] = source.read(1_000_000)
                    result[name + '_truncated'] = bool(source.read(1))
                    source.seek(0, os.SEEK_END)
                    offsets[name] = source.tell()
                result[name + '_file'] = str(path)
            protocol.write(json.dumps(result, ensure_ascii=False) + '\n')
        await browser.close()
    if bridge:
        bridge.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
