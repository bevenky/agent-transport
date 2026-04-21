/**
 * Observability setup for agent-transport (Node.js).
 *
 * Uploads session reports (transcript, events, audio) to the
 * observability server after each call ends. Uses basic auth.
 *
 * Set AGENT_OBSERVABILITY_URL to enable.
 */

import FormData from 'form-data';
import fs from 'node:fs/promises';

export function getObservabilityUrl(): string | undefined {
  return process.env.AGENT_OBSERVABILITY_URL;
}

function buildAuthHeaders(): Record<string, string> {
  const user = process.env.AGENT_OBSERVABILITY_USER;
  const pass = process.env.AGENT_OBSERVABILITY_PASS;
  if (!user || !pass) return {};
  const credentials = Buffer.from(`${user}:${pass}`).toString('base64');
  return { Authorization: `Basic ${credentials}` };
}

/**
 * Extract primitive scalar fields from AgentSession.options so the session
 * report carries the configuration that shaped the call (VAD settings, turn
 * detection flags, timeouts, etc.). Mirrors the Python SDK's filter — skips
 * private fields (leading underscore) and any non-primitive values (complex
 * plugin instances, nested objects) so the payload stays JSON-safe.
 *
 * Keys can stay camelCase — agent-observability's server normalises to
 * snake_case on ingest.
 */
function extractOptions(options: any): Record<string, unknown> {
  if (options == null || typeof options !== 'object') return {};
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(options)) {
    if (key.startsWith('_')) continue;
    const t = typeof value;
    if (t === 'string' || t === 'number' || t === 'boolean' || value === null) {
      out[key] = value;
    }
  }
  return out;
}

/**
 * Build a session report from the AgentSession.
 * Inlined to avoid deep imports from @livekit/agents internals.
 */
function buildReport(session: any, callId: string, recordingPath?: string, recordingStartedAt?: number) {
  const timestamp = Date.now();
  const chatHistory = session.history?.copy?.() ?? session.history ?? { items: [], toJSON: () => ({ items: [] }) };
  const events = (session._recordedEvents ?? [])
    .filter((e: any) => e.type !== 'metrics_collected' && e.type !== 'session_usage_updated');

  const usage = session.usage?.modelUsage?.map((u: any) => {
    const obj: Record<string, any> = {};
    for (const [k, v] of Object.entries(u)) {
      if (v !== 0 && v !== null && v !== undefined && v !== '') obj[k] = v;
    }
    return obj;
  }) ?? null;

  const options = extractOptions(session.options);

  return {
    roomId: callId,
    jobId: callId,
    audioRecordingStartedAt: recordingStartedAt,
    audioRecordingPath: recordingPath,
    toDict() {
      return {
        job_id: callId,
        room_id: callId,
        room: callId,
        events: events.map((e: any) => ({ ...e })),
        chat_history: typeof chatHistory.toJSON === 'function'
          ? chatHistory.toJSON({ excludeTimestamp: false })
          : typeof chatHistory.to_dict === 'function'
            ? chatHistory.to_dict({ exclude_timestamp: false })
            : { items: Array.isArray(chatHistory) ? chatHistory : chatHistory.items ?? [] },
        options,
        timestamp,
        usage,
      };
    },
  };
}

export async function uploadReport(options: {
  agentName: string;
  session: any;
  callId: string;
  accountId?: string;
  recordingPath?: string;
  recordingStartedAt?: number;
}): Promise<void> {
  const obsUrl = getObservabilityUrl();
  if (!obsUrl) return;

  const { agentName, session, callId, accountId, recordingPath, recordingStartedAt } = options;

  const report = buildReport(session, callId, recordingPath, recordingStartedAt);

  // Build JSON header
  const roomTags: Record<string, string> = {};
  if (accountId) {
    roomTags['account_id'] = accountId;
  }

  const headerJson = JSON.stringify({
    session_id: callId,
    room_tags: roomTags,
    start_time: report.audioRecordingStartedAt ?? 0,
  });
  const headerBuffer = Buffer.from(headerJson, 'utf-8');

  // Build multipart form
  const formData = new FormData();

  formData.append('header', headerBuffer, {
    filename: 'header.json',
    contentType: 'application/json',
    knownLength: headerBuffer.length,
    header: { 'Content-Type': 'application/json', 'Content-Length': headerBuffer.length.toString() },
  });

  const chatHistoryJson = JSON.stringify(report.toDict());
  const chatHistoryBuffer = Buffer.from(chatHistoryJson, 'utf-8');
  formData.append('chat_history', chatHistoryBuffer, {
    filename: 'chat_history.json',
    contentType: 'application/json',
    knownLength: chatHistoryBuffer.length,
    header: { 'Content-Type': 'application/json', 'Content-Length': chatHistoryBuffer.length.toString() },
  });

  if (report.audioRecordingPath && report.audioRecordingStartedAt) {
    let audioBytes: Buffer;
    try { audioBytes = await fs.readFile(report.audioRecordingPath); } catch { audioBytes = Buffer.alloc(0); }
    if (audioBytes.length > 0) {
      formData.append('audio', audioBytes, {
        filename: 'recording.ogg',
        contentType: 'audio/ogg',
        knownLength: audioBytes.length,
        header: { 'Content-Type': 'audio/ogg', 'Content-Length': audioBytes.length.toString() },
      });
    }
  }

  // Upload with basic auth
  const authHeaders = buildAuthHeaders();
  console.log(`Uploading session report for ${callId} to ${obsUrl} (account_id=${accountId})`);
  const url = new URL('/observability/recordings/v0', obsUrl);
  return new Promise<void>((resolve, reject) => {
    formData.submit(
      { protocol: url.protocol as 'https:' | 'http:', host: url.hostname, port: url.port || undefined, path: url.pathname, method: 'POST', headers: authHeaders },
      (err, res) => {
        if (err) { reject(new Error(`Failed to upload session report: ${err.message}`)); return; }
        if (res.statusCode && res.statusCode >= 400) {
          let body = '';
          res.on('data', (chunk) => { body += chunk.toString(); });
          res.on('end', () => { reject(new Error(`Upload failed: ${res.statusCode} ${res.statusMessage} - ${body}`)); });
          return;
        }
        res.resume();
        res.on('end', () => { console.log(`Session report uploaded for ${callId}`); resolve(); });
      },
    );
  });
}
