"""Task-local Python worker and a host-owned, public-HTTPS-only Docker exit."""

import json
import os
from pathlib import Path
import queue
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .browser import BrowserError, normalize_domain, public_addresses
from .browser_network import PublicHTTPSProxy
from .browser_worker import relay


class BrowserRuntime:
    def __init__(self, check, *, config, scope):
        self.check, self.config, self.scope = check, config, scope
        self.process = self.gateway = self.proxy = self.directory = None
        self.image = None
        self._responses = queue.Queue()
        self._stderr = ''
        self.container_name = 'assistant-browser-' + uuid.uuid4().hex

    def docker_command(self, directory):
        config = self.config
        command = ['docker', 'run', '--rm', '-i', '--init', '--pull=never', '--name', self.container_name,
                   '--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                   '--security-opt=seccomp=' + str(Path(config.browser_seccomp).resolve()),
                   '--user', f'{os.getuid()}:{os.getgid()}', '--cpus=2', '--memory=2g', '--pids-limit=512',
                   '--shm-size=512m', '--tmpfs', '/tmp:rw,nosuid,nodev,size=512m,mode=1777',
                   '--mount', f'type=bind,source={directory},target=/gateway,readonly',
                   '-e', 'BROWSER_GATEWAY_SOCKET=/gateway/https.sock', '-e', 'BROWSER_SANDBOX=true',
                   config.browser_image, 'python', '-I', '-u', '/opt/browser_worker.py']
        return command

    def __enter__(self):
        try:
            if self.config.browser_backend == 'docker':
                if not Path(self.config.browser_seccomp).is_file():
                    raise BrowserError('缺少浏览器 seccomp 配置；按README构建运行环境。', 'internal', False)
                self.directory = tempfile.TemporaryDirectory(prefix='assistant-browser-')
                domains = None if self.scope.public_web else self.scope.domains | self.scope.resource_domains
                self.proxy = PublicHTTPSProxy(public_addresses, normalize_domain, self.config.browser_proxy,
                                              allow_unregistered=True, domains=domains)
                address = self.proxy._server.server_address

                class Handler(socketserver.BaseRequestHandler):
                    def handle(self):
                        try:
                            relay(self.request, socket.create_connection(address, timeout=5))
                        except OSError:
                            pass

                self.gateway = socketserver.ThreadingUnixStreamServer(self.directory.name + '/https.sock', Handler)
                # Rootless Docker maps the non-root container UID to a subordinate UID.
                # The only exposed object is this public-only relay, never application data.
                os.chmod(self.directory.name, 0o755)
                os.chmod(self.directory.name + '/https.sock', 0o666)
                self.gateway.daemon_threads = True
                threading.Thread(target=self.gateway.serve_forever, daemon=True).start()
                command = self.docker_command(self.directory.name)
            else:
                self.directory = tempfile.TemporaryDirectory(prefix='assistant-browser-local-')
                command = [sys.executable, '-I', '-u', str(Path(__file__).with_name('browser_worker.py'))]
            # Never forward .env / API / mailbox variables into the code interpreter.
            environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'PLAYWRIGHT_BROWSERS_PATH',
                           'DOCKER_HOST', 'DOCKER_CONTEXT', 'XDG_RUNTIME_DIR') if key in os.environ}
            if self.config.browser_backend == 'local':
                environment['BROWSER_WORKDIR'] = self.directory.name
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=environment, start_new_session=True)
            threading.Thread(target=self._read_responses, args=(self.process.stdout,), daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(self.process.stderr,), daemon=True).start()
            result = self._receive(60)
            if not result.get('ready'):
                raise BrowserError('浏览器代码会话未就绪。', 'internal', False)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _read_responses(self, stream):
        try:
            for line in stream:
                self._responses.put(json.loads(line))
        except (ValueError, OSError) as error:
            self._responses.put({'worker_error': str(error)})
        finally:
            self._responses.put({'worker_error': '代码执行进程已退出。'})

    def _read_stderr(self, stream):
        try:
            for line in stream:
                self._stderr = (self._stderr + line)[-8000:]
        except (ValueError, OSError):
            pass

    def _receive(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self.check()
                result = self._responses.get(timeout=min(.2, max(.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            except BaseException:
                self.__exit__(None, None, None)
                raise
            if 'worker_error' in result:
                raise BrowserError(result['worker_error'] + self._stderr, 'internal', False)
            return result
        self.__exit__(None, None, None)
        raise BrowserError('代码或观察超过执行上限；会话已销毁，部分操作可能已执行，未自动重跑。', 'execution', False)

    def run(self, code, timeout=30, observe=True):
        self.check()
        if self.process is None or self.process.poll() is not None:
            raise BrowserError('代码会话已关闭；可显式 restart_browser，旧变量和页面不会恢复。', 'internal', False)
        try:
            self.process.stdin.write(json.dumps({'code': code, 'timeout': timeout, 'observe': observe}) + '\n')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise BrowserError('代码会话已退出；未重放代码。', 'internal', False) from None
        result = self._receive(timeout + 25)
        self.image = result.pop('image', None)
        if observe:
            result.update(kind='page', links=[], elements=[], ready=bool(result.get('text', '').strip()),
                          url=result.get('url', self.scope.start_url), title=result.get('title', ''),
                          text=result.get('text', ''), fetched_at=result.get('fetched_at', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
        return result

    def read(self, url):
        return self.run(f'await page.goto({url!r}, wait_until="domcontentloaded")')

    def diagnostics(self):
        return {'backend': self.config.browser_backend,
                'isolated': self.config.browser_backend == 'docker',
                'connection_errors': self.proxy.failures() if self.proxy else []}

    def __exit__(self, *_):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        if self.config.browser_backend == 'docker' and self.directory and process is not None:
            subprocess.run(['docker', 'rm', '-f', self.container_name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        if self.gateway:
            self.gateway.shutdown()
            self.gateway.server_close()
            self.gateway = None
        if self.proxy:
            self.proxy.close()
            self.proxy = None
        if self.directory:
            self.directory.cleanup()
            self.directory = None
