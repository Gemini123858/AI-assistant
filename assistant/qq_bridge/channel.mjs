// This adapter only accepts the bound owner's C2C events and replies to those events.
export function splitReply(text, limit = 3500) {
  const characters = Array.from(text);
  const notice = '\n\n回答较长，QQ仅展示前面部分；可以接着问我，分段继续。';
  const truncated = characters.length > limit * 4;
  const visible = truncated
    ? characters.slice(0, limit * 4 - Array.from(notice).length).join('') + notice
    : text;
  const chars = Array.from(visible);
  const chunks = [];
  for (let i = 0; i < chars.length; i += limit) chunks.push(chars.slice(i, i + limit).join(''));
  return { chunks, truncated };
}

export function createChannel(bot, owner, emit) {
  const received = new Map();
  return {
    inbound(message) {
      if (message.kind !== 'c2c' || message.senderId !== owner ||
          typeof message.messageId !== 'string' || !message.messageId || received.has(message.messageId)) return;
      received.set(message.messageId, { target: { scope: 'c2c', targetId: owner, msgId: message.messageId }, sent: new Set() });
      if (received.size > 200) received.delete(received.keys().next().value);
      emit({ type: 'message', kind: 'c2c', senderId: owner, messageId: message.messageId,
        text: typeof message.content === 'string' ? message.content : '',
        hasAttachments: Boolean(message.attachments?.length) });
    },
    async reply(command) {
      const entry = received.get(command.messageId);
      if (!entry || entry.sent.has(command.id) || typeof command.text !== 'string' || !command.text.trim()) {
        emit({ type: 'delivery', id: command.id, ok: false });
        return;
      }
      // A failed/ambiguous send must not cause the full answer to be replayed.
      entry.sent.add(command.id);
      const { chunks, truncated } = splitReply(command.text);
      try {
        for (const chunk of chunks) await bot.sendText(entry.target, chunk);
        emit({ type: 'delivery', id: command.id, ok: true, truncated });
      } catch {
        emit({ type: 'delivery', id: command.id, ok: false, truncated });
      }
    },
  };
}
