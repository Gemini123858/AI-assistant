import test from 'node:test';
import assert from 'node:assert/strict';
import { createChannel, splitReply } from './channel.mjs';

const message = (overrides = {}) => ({ kind: 'c2c', senderId: 'synthetic-owner',
  messageId: 'incoming-1', content: '合成请求', ...overrides });

test('only owner C2C messages enter Python, with duplicate suppression', () => {
  const events = [];
  const channel = createChannel({}, 'synthetic-owner', (event) => events.push(event));
  channel.inbound(message({ senderId: 'someone-else' }));
  channel.inbound(message({ kind: 'group' }));
  channel.inbound(message({ attachments: [{ url: 'https://example.invalid/image' }] }));
  channel.inbound(message());
  assert.equal(events.length, 1);
  assert.equal(events[0].hasAttachments, true);
  assert.equal(events[0].senderId, 'synthetic-owner');
  assert.equal(JSON.stringify(events).includes('example.invalid'), false);
});

test('reply is tied to an authorized inbound message; target cannot be supplied by caller', async () => {
  const events = [], sent = [];
  const channel = createChannel({ sendText: async (...args) => sent.push(args) },
    'synthetic-owner', (event) => events.push(event));
  await channel.reply({ id: 'no-inbound', messageId: 'missing', text: 'x' });
  assert.equal(sent.length, 0);
  channel.inbound(message());
  await channel.reply({ id: 'delivery', messageId: 'incoming-1', text: '回答', targetId: 'someone-else' });
  assert.deepEqual(sent, [[{ scope: 'c2c', targetId: 'synthetic-owner', msgId: 'incoming-1' }, '回答']]);
  assert.deepEqual(events.at(-1), { type: 'delivery', id: 'delivery', ok: true, truncated: false });
  await channel.reply({ id: 'delivery', messageId: 'incoming-1', text: '回答' });
  assert.equal(sent.length, 1);
});

test('long text respects reply count, preserves Unicode and explicitly announces truncation', () => {
  const short = '合成😀'.repeat(2000);
  const whole = splitReply(short);
  assert.equal(whole.chunks.join(''), short);
  assert.equal(whole.truncated, false);
  assert.equal(whole.chunks.some((s) => /[\uD800-\uDBFF]$/.test(s)), false);
  const large = splitReply('测'.repeat(16000));
  assert.equal(large.chunks.length, 4);
  assert.equal(large.truncated, true);
  assert.match(large.chunks.at(-1), /分段继续/);
  assert.ok(large.chunks.every((s) => Array.from(s).length <= 3500));
});

test('ambiguous send failure is reported without leaking errors or retrying', async () => {
  const events = [];
  let calls = 0;
  const channel = createChannel({ sendText: async () => { calls++; throw new Error('synthetic-secret'); } },
    'synthetic-owner', (event) => events.push(event));
  channel.inbound(message());
  const command = { id: 'failure', messageId: 'incoming-1', text: '长'.repeat(8000) };
  await channel.reply(command);
  await channel.reply(command);
  assert.equal(calls, 1);
  assert.equal(events.at(-1).ok, false);
  assert.equal(JSON.stringify(events).includes('synthetic-secret'), false);
});

test('one inbound accepts distinct commentary and final deliveries in order', async () => {
  const sent = [];
  const channel = createChannel({ sendText: async (_target, text) => sent.push(text) }, 'synthetic-owner', () => {});
  channel.inbound(message());
  await channel.reply({ id: 'step-1', messageId: 'incoming-1', text: '我去看看邮件。' });
  await channel.reply({ id: 'step-2', messageId: 'incoming-1', text: '找到了，看看正文。' });
  await channel.reply({ id: 'final', messageId: 'incoming-1', text: '这是结论。' });
  assert.deepEqual(sent, ['我去看看邮件。', '找到了，看看正文。', '这是结论。']);
});
