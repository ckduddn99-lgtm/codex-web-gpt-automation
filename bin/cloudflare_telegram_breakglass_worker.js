const ALLOWED_COMMANDS = new Set(['/heal', '/status', '/restart_pc', '/stop_browser']);

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8' },
  });
}

async function dispatch(env, command, updateId) {
  const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPOSITORY}/dispatches`, {
    method: 'POST',
    headers: {
      accept: 'application/vnd.github+json',
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      'content-type': 'application/json',
      'user-agent': 'project-control-breakglass-worker',
      'x-github-api-version': '2022-11-28',
    },
    body: JSON.stringify({
      event_type: 'telegram-breakglass',
      client_payload: { command, update_id: String(updateId) },
    }),
  });
  return response.status === 204;
}

export default {
  async fetch(request, env) {
    if (request.method === 'GET') {
      return json({ ok: true, service: 'telegram-breakglass-relay' });
    }
    if (request.method !== 'POST') return json({ ok: false }, 405);

    const telegramSecret = request.headers.get('x-telegram-bot-api-secret-token');
    if (!telegramSecret || telegramSecret !== env.TELEGRAM_WEBHOOK_SECRET) {
      return json({ ok: false }, 403);
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return json({ ok: false }, 400);
    }

    const message = update?.message || {};
    const chatId = String(message?.chat?.id ?? '');
    const userId = String(message?.from?.id ?? '');
    if (chatId !== String(env.TELEGRAM_ALLOWED_CHAT_ID) || userId !== String(env.TELEGRAM_ALLOWED_USER_ID)) {
      return json({ ok: true, ignored: true });
    }

    const command = String(message?.text || '').trim().split(/\s+/, 1)[0].toLowerCase();
    if (!ALLOWED_COMMANDS.has(command)) return json({ ok: true, ignored: true });

    const dispatched = await dispatch(env, command, update?.update_id ?? 'unknown');
    return json({ ok: dispatched, dispatched }, dispatched ? 200 : 502);
  },
};
