'use strict';

const http = require('node:http');
const https = require('node:https');
const { randomUUID, timingSafeEqual } = require('node:crypto');

function envInt(name, fallback, minimum, maximum) {
  const parsed = Number.parseInt(process.env[name] || '', 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(maximum, Math.max(minimum, parsed));
}

const PORT = envInt('PORT', 9000, 1, 65535);
const GATEWAY_VERSION = '2.0.1';
const MODEL = process.env.GATEWAY_MODEL || 'glm-4-flash';
const APP_SECRET = process.env.APP_SECRET || '';
const ZHIPU_API_KEY = process.env.ZHIPU_API_KEY || '';
const ZHIPU_CHAT_URL = process.env.ZHIPU_CHAT_URL || 'https://open.bigmodel.cn/api/paas/v4/chat/completions';
const MAX_BODY_BYTES = envInt('MAX_BODY_BYTES', 1024 * 1024, 1024, 8 * 1024 * 1024);
const MAX_COMPLETION_TOKENS = envInt('MAX_COMPLETION_TOKENS', 768, 1, 8192);

// The desktop/web client gives up after 8 seconds. Keep the gateway deadline below it.
const UPSTREAM_DEADLINE_MS = envInt('UPSTREAM_DEADLINE_MS', 7000, 2000, 7500);
const MAX_UPSTREAM_ATTEMPTS = envInt('MAX_UPSTREAM_ATTEMPTS', 2, 1, 2);
const RETRYABLE_STATUS = new Set([502, 503, 504]);
const RETRYABLE_ERRORS = new Set(['ECONNRESET', 'ETIMEDOUT', 'EAI_AGAIN', 'EPIPE']);

const upstreamAgent = new https.Agent({
  keepAlive: true,
  keepAliveMsecs: 1000,
  maxSockets: 64,
  maxFreeSockets: 16,
  timeout: UPSTREAM_DEADLINE_MS,
});

class RequestError extends Error {
  constructor(statusCode, code, message) {
    super(message);
    this.statusCode = statusCode;
    this.code = code;
  }
}

function setCorsHeaders(res) {
  res.setHeader('X-Easier-Gateway-Version', GATEWAY_VERSION);
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-App-Secret');
  res.setHeader('Access-Control-Max-Age', '86400');
}

function sendJson(res, statusCode, payload, requestId) {
  if (res.headersSent || res.destroyed) return;
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
    'X-Request-Id': requestId,
  });
  res.end(body);
}

function safeEqual(left, right) {
  const a = Buffer.from(String(left || ''));
  const b = Buffer.from(String(right || ''));
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

function readRequestBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0;
    let tooLarge = false;
    const chunks = [];

    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        tooLarge = true;
        chunks.length = 0;
        return;
      }
      if (!tooLarge) chunks.push(chunk);
    });
    req.on('end', () => {
      if (tooLarge) {
        reject(new RequestError(413, 'BODY_TOO_LARGE', 'Request body is too large'));
        return;
      }
      resolve(Buffer.concat(chunks).toString('utf8'));
    });
    req.on('aborted', () => reject(new RequestError(499, 'CLIENT_ABORTED', 'Client aborted the request')));
    req.on('error', reject);
  });
}

function normalizePayload(rawBody) {
  let payload;
  try {
    payload = rawBody ? JSON.parse(rawBody) : {};
  } catch {
    throw new RequestError(400, 'INVALID_JSON', 'Request body must be valid JSON');
  }

  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new RequestError(400, 'INVALID_PAYLOAD', 'Request body must be a JSON object');
  }
  if (!Array.isArray(payload.messages) || payload.messages.length === 0) {
    throw new RequestError(400, 'INVALID_MESSAGES', 'messages must be a non-empty array');
  }

  payload.model = MODEL;
  payload.stream = payload.stream === true;
  if (!Number.isInteger(payload.max_tokens) || payload.max_tokens < 1) {
    payload.max_tokens = MAX_COMPLETION_TOKENS;
  } else {
    payload.max_tokens = Math.min(payload.max_tokens, MAX_COMPLETION_TOKENS);
  }
  return payload;
}

function requestUpstream(payload, requestId) {
  return new Promise((resolve, reject) => {
    const url = new URL(ZHIPU_CHAT_URL);
    const body = Buffer.from(JSON.stringify(payload));
    const deadline = Date.now() + UPSTREAM_DEADLINE_MS;
    let attempt = 0;

    const runAttempt = () => {
      attempt += 1;
      const remaining = deadline - Date.now();
      if (remaining < 500) {
        const error = new RequestError(504, 'UPSTREAM_TIMEOUT', 'Upstream model timed out');
        reject(error);
        return;
      }

      const upstreamReq = https.request({
        protocol: url.protocol,
        hostname: url.hostname,
        port: url.port || 443,
        path: url.pathname + url.search,
        method: 'POST',
        agent: upstreamAgent,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': body.length,
          'Authorization': `Bearer ${ZHIPU_API_KEY}`,
          'Accept': payload.stream ? 'text/event-stream' : 'application/json',
          'User-Agent': 'Easier-banking-chat-proxy/2.0',
          'X-Request-Id': requestId,
        },
      }, (upstreamRes) => {
        const status = Number(upstreamRes.statusCode || 502);
        if (RETRYABLE_STATUS.has(status) && attempt < MAX_UPSTREAM_ATTEMPTS && deadline - Date.now() > 900) {
          upstreamRes.resume();
          upstreamRes.once('end', () => setTimeout(runAttempt, 120));
          return;
        }
        resolve({ upstreamReq, upstreamRes });
      });

      upstreamReq.setTimeout(Math.max(500, remaining), () => {
        const error = new Error('Upstream request timed out');
        error.code = 'ETIMEDOUT';
        upstreamReq.destroy(error);
      });
      upstreamReq.on('error', (error) => {
        const canRetry = RETRYABLE_ERRORS.has(error.code) &&
          attempt < MAX_UPSTREAM_ATTEMPTS && deadline - Date.now() > 900;
        if (canRetry) {
          setTimeout(runAttempt, 120);
          return;
        }
        reject(error.code === 'ETIMEDOUT'
          ? new RequestError(504, 'UPSTREAM_TIMEOUT', 'Upstream model timed out')
          : new RequestError(502, 'UPSTREAM_UNAVAILABLE', 'Upstream model is unavailable'));
      });
      upstreamReq.end(body);
    };

    runAttempt();
  });
}

function forwardUpstream(upstreamRes, res, req, requestId) {
  const contentType = upstreamRes.headers['content-type'] || 'application/json; charset=utf-8';
  const responseHeaders = {
    'Content-Type': contentType,
    'Cache-Control': 'no-store',
    'X-Request-Id': requestId,
  };
  if (String(contentType).includes('text/event-stream')) {
    responseHeaders['X-Accel-Buffering'] = 'no';
  }
  if (upstreamRes.headers['retry-after']) {
    responseHeaders['Retry-After'] = upstreamRes.headers['retry-after'];
  }

  res.writeHead(Number(upstreamRes.statusCode || 502), responseHeaders);
  upstreamRes.once('error', () => res.destroy());
  res.once('close', () => {
    if (!res.writableFinished) upstreamRes.destroy();
  });
  req.once('aborted', () => upstreamRes.destroy());
  upstreamRes.pipe(res);
}

const server = http.createServer(async (req, res) => {
  const requestId = randomUUID();
  const startedAt = Date.now();
  setCorsHeaders(res);

  if (req.method === 'OPTIONS') {
    res.writeHead(204, { 'X-Request-Id': requestId });
    res.end();
    return;
  }

  const pathname = new URL(req.url || '/', 'http://localhost').pathname.replace(/\/+$/, '') || '/';
  const hasClientSecret = Boolean(req.headers['x-app-secret']);
  if (req.method === 'GET' && !hasClientSecret && (pathname === '/' || pathname === '/healthz')) {
    sendJson(res, 200, {
      status: 'ok',
      version: GATEWAY_VERSION,
      model: MODEL,
      upstream_configured: Boolean(ZHIPU_API_KEY),
      secret_configured: Boolean(APP_SECRET),
      deadline_ms: UPSTREAM_DEADLINE_MS,
    }, requestId);
    return;
  }

  if (!APP_SECRET || !ZHIPU_API_KEY) {
    sendJson(res, 503, {
      error: { code: 'GATEWAY_NOT_CONFIGURED', message: 'Gateway environment variables are incomplete' },
      request_id: requestId,
    }, requestId);
    return;
  }

  if (!safeEqual(req.headers['x-app-secret'], APP_SECRET)) {
    sendJson(res, 401, {
      error: { code: 'UNAUTHORIZED', message: 'Invalid X-App-Secret' },
      request_id: requestId,
    }, requestId);
    return;
  }

  try {
    const payload = normalizePayload(await readRequestBody(req));
    const { upstreamRes } = await requestUpstream(payload, requestId);
    forwardUpstream(upstreamRes, res, req, requestId);
    console.log(JSON.stringify({
      request_id: requestId,
      status: upstreamRes.statusCode,
      stream: payload.stream,
      duration_ms: Date.now() - startedAt,
    }));
  } catch (error) {
    const status = error instanceof RequestError ? error.statusCode : 500;
    const code = error instanceof RequestError ? error.code : 'INTERNAL_ERROR';
    console.error(JSON.stringify({
      request_id: requestId,
      status,
      code,
      duration_ms: Date.now() - startedAt,
      message: String(error.message || error).slice(0, 180),
    }));
    sendJson(res, status, {
      error: { code, message: status >= 500 ? 'Gateway request failed' : error.message },
      request_id: requestId,
    }, requestId);
  }
});

server.requestTimeout = 15000;
server.headersTimeout = 10000;
server.keepAliveTimeout = 5000;
server.on('clientError', (_error, socket) => {
  if (socket.writable) socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n');
});

function shutdown(signal) {
  console.log(JSON.stringify({ event: 'shutdown', signal }));
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 5000).unref();
}

process.once('SIGTERM', () => shutdown('SIGTERM'));
process.once('SIGINT', () => shutdown('SIGINT'));

server.listen(PORT, '0.0.0.0', () => {
  console.log(JSON.stringify({ event: 'listening', port: PORT, model: MODEL }));
});
