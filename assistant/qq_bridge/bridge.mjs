import { QQBot } from '@tencent-connect/qqbot-nodejs';
import { startQrConnect } from '@tencent-connect/qqbot-connector';
import QRCode from 'qrcode';
import { writeFileSync, renameSync, chmodSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { createChannel } from './channel.mjs';

const emit = (event) => process.stdout.write(JSON.stringify(event) + '\n');

async function bind(path) {
  const abort = new AbortController();
  process.once('SIGINT', () => abort.abort());
  await new Promise((resolve, reject) => {
    startQrConnect({
      onQrDisplayed(url) {
        console.log('请用本人QQ扫描绑定机器人，或在浏览器打开以下链接：\n' + url);
        QRCode.toString(url, { type: 'terminal', small: true }).then(console.log)
          .catch(() => console.log('二维码渲染失败，请使用上方链接。'));
      },
      onQrExpired() { console.log('二维码已过期，等待刷新。'); },
      onFailure: reject,
      onSuccess(items) {
        try {
          const auth = items[0];
          if (!auth?.appId || !auth?.appSecret || !auth?.userOpenid) {
            throw new Error('missing credentials or owner');
          }
          const temporary = path + '.tmp';
          writeFileSync(temporary, JSON.stringify({ schema_version: 1,
            appId: auth.appId, appSecret: auth.appSecret, userOpenid: auth.userOpenid }), { mode: 0o600 });
          chmodSync(temporary, 0o600);
          renameSync(temporary, path);
          console.log('QQ绑定已保存，已限定为扫码本人的私聊。凭据未显示。');
          resolve();
        } catch (error) { reject(error); }
      },
    }, { displayQrCodeToConsole: false, signal: abort.signal, source: 'personal-assistant' });
  });
}

async function run() {
  const input = createInterface({ input: process.stdin });
  let bot, channel;
  const stop = () => { bot?.stop(); input.close(); };
  process.once('SIGTERM', stop);
  process.once('SIGINT', stop);
  try {
    for await (const line of input) {
      const command = JSON.parse(line);
      if (command.type === 'configure' && !bot) {
        const { appId, appSecret, userOpenid } = command.credentials;
        if (![appId, appSecret, userOpenid].every((value) => typeof value === 'string' && value.trim())) {
          throw new Error('invalid credentials');
        }
        bot = new QQBot({ appId, appSecret, transport: 'websocket', markdownSupport: false,
          // Only C2C/group intent is needed; group events are discarded by the channel.
          intents: 1 << 25,
          baseUrl: 'https://api.bot.qq.com', tokenBaseUrl: 'https://api.bot.qq.com' });
        channel = createChannel(bot, userOpenid, emit);
        bot.on('message', (_ctx, message) => channel.inbound(message));
        bot.on('ready', () => emit({ type: 'ready' }));
        bot.on('resumed', () => emit({ type: 'ready' }));
        bot.on('error', () => emit({ type: 'error' }));
        bot.on('disconnected', ({ willReconnect }) => emit({ type: 'disconnected', willReconnect }));
        // SDK owns heartbeat/reconnect; raw exceptions can contain credentials or message bodies.
        bot.start().catch(() => { emit({ type: 'error' }); stop(); });
      } else if (command.type === 'send' && channel) {
        await channel.reply(command);
      }
    }
  } finally { bot?.stop(); }
}

try {
  if (process.argv[2] === 'bind') await bind(process.argv[3]);
  else if (process.argv[2] === 'check') console.log('QQ SDK与扫码依赖已加载，未连接账号。');
  else await run();
} catch {
  if (process.argv[2] === 'bind') console.error('QQ绑定失败或已取消；请检查网络，扫码需返回本人userOpenid。');
  else emit({ type: 'error' });
  process.exitCode = 1;
}
