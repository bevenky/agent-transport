/**
 * Observability setup for agent-transport (Node.js).
 *
 * Keep this layer intentionally thin: build a LiveKit SessionReport, attach
 * Agent Transport metadata to the recording header, and ship native-shaped
 * payloads to observability. Parsing/normalization belongs in observability.
 *
 * Set AGENT_OBSERVABILITY_URL plus LIVEKIT_API_KEY / LIVEKIT_API_SECRET to enable.
 */

import { MetricsRecordingHeader } from '@livekit/protocol';
import { version as sdkVersion, voice } from '@livekit/agents';
import { AccessToken } from 'livekit-server-sdk';
import fs from 'node:fs/promises';

type Transport = 'sip' | 'audio_stream';

interface OtlpLogRecord {
  body: string;
  timestampMs: number;
  attributes: Record<string, unknown>;
}

export function getObservabilityUrl(): string | undefined {
  return process.env.AGENT_OBSERVABILITY_URL;
}

async function buildBearerAuthHeaders(): Promise<Record<string, string>> {
  const apiKey = process.env.LIVEKIT_API_KEY;
  const apiSecret = process.env.LIVEKIT_API_SECRET;
  if (!apiKey || !apiSecret) {
    throw new Error('LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required for LiveKit observability uploads');
  }

  const token = new AccessToken(apiKey, apiSecret, { ttl: '6h' });
  token.addObservabilityGrant({ write: true });
  return { Authorization: `Bearer ${await token.toJwt()}` };
}

function scalarTagValue(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return undefined;
}

function scalarMetadata(metadata?: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(metadata ?? {})) {
    const tagValue = scalarTagValue(value);
    if (tagValue !== undefined) out[key] = tagValue;
  }
  return out;
}

export function buildRoomTags(options: {
  agentName: string;
  accountId?: string;
  metadata?: Record<string, unknown>;
  transport?: Transport;
  direction?: string;
}): Record<string, string> {
  return {
    ...scalarMetadata(options.metadata),
    agent_name: options.agentName,
    ...(options.accountId ? { account_id: options.accountId } : {}),
    ...(options.transport ? { transport: options.transport } : {}),
    ...(options.direction ? { direction: options.direction } : {}),
  };
}

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

function buildReport(
  session: any,
  callId: string,
  recordingPath?: string,
  recordingStartedAt?: number,
): voice.SessionReport {
  const chatHistory = session.history?.copy?.() ?? session.history ?? { items: [], toJSON: () => ({ items: [] }) };
  return voice.createSessionReport({
    jobId: `job-${callId}`,
    roomId: callId,
    room: callId,
    options: session.sessionOptions ?? extractOptions(session.options),
    events: session._recordedEvents ?? [],
    enableRecording: Boolean(recordingPath),
    chatHistory,
    startedAt: session._startedAt,
    audioRecordingPath: recordingPath,
    audioRecordingStartedAt: recordingStartedAt,
    modelUsage:
      session._usageCollector?.flatten?.() ??
      session.usage?.modelUsage ??
      session.usage?.model_usage ??
      null,
  });
}

function otlpValue(value: unknown, path = ''): Record<string, unknown> {
  if (value === null || value === undefined) return { stringValue: '' };
  if (typeof value === 'string') return { stringValue: value };
  if (typeof value === 'boolean') return { boolValue: value };
  if (typeof value === 'number') {
    const leafKey = path.split('.').pop()?.replace(/\[\d+\]$/, '') ?? path;
    const forceDouble = new Set([
      'transcriptConfidence',
      'transcriptionDelay',
      'endOfTurnDelay',
      'onUserTurnCompletedDelay',
      'llmNodeTtft',
      'ttsNodeTtfb',
      'e2eLatency',
    ]);
    return Number.isInteger(value) && !forceDouble.has(leafKey)
      ? { intValue: String(value) }
      : { doubleValue: value };
  }
  if (Array.isArray(value)) {
    return { arrayValue: { values: value.map((item, i) => otlpValue(item, `${path}[${i}]`)) } };
  }
  if (typeof value === 'object') {
    return {
      kvlistValue: {
        values: Object.entries(value as Record<string, unknown>).map(([key, item]) => ({
          key,
          value: otlpValue(item, path ? `${path}.${key}` : key),
        })),
      },
    };
  }
  return { stringValue: String(value) };
}

function otlpAttributes(attributes: Record<string, unknown>): Array<{ key: string; value: Record<string, unknown> }> {
  return Object.entries(attributes).map(([key, value]) => ({ key, value: otlpValue(value, key) }));
}

function buildOtlpJsonPayload(
  records: OtlpLogRecord[],
  report: voice.SessionReport,
): Record<string, unknown> {
  return {
    resourceLogs: [
      {
        resource: {
          attributes: otlpAttributes({
            room_id: report.roomId,
            job_id: report.jobId,
            'service.name': 'livekit-agents',
          }),
        },
        scopeLogs: [
          {
            scope: {
              name: 'chat_history',
              attributes: otlpAttributes({
                room_id: report.roomId,
                job_id: report.jobId,
                room: report.room,
              }),
            },
            logRecords: records.map((record) => {
              const timestampMs = Number.isFinite(record.timestampMs) ? record.timestampMs : Date.now();
              return {
                timeUnixNano: String(BigInt(Math.floor(timestampMs * 1_000_000))),
                observedTimeUnixNano: String(BigInt(Date.now()) * 1_000_000n),
                severityNumber: 0,
                severityText: 'unspecified',
                body: { stringValue: record.body },
                attributes: otlpAttributes(record.attributes),
                traceId: '',
                spanId: '',
              };
            }),
          },
        ],
      },
    ],
  };
}

function buildOtlpLogRecords(
  report: voice.SessionReport,
  agentName: string,
  roomTags: Record<string, string>,
): OtlpLogRecord[] {
  return [
    {
      body: 'session report',
      timestampMs: report.startedAt || report.timestamp || Date.now(),
      attributes: {
        room_id: report.roomId,
        job_id: report.jobId,
        'logger.name': 'chat_history',
        'session.report': voice.sessionReportToJSON(report),
        agent_name: agentName,
        sdk_version: sdkVersion,
        room_tags: roomTags,
      },
    },
  ];
}

async function uploadOtlpLogs(
  obsUrl: string,
  authHeaders: Record<string, string>,
  report: voice.SessionReport,
  records: OtlpLogRecord[],
): Promise<void> {
  if (records.length === 0) return;
  const url = new URL('/observability/logs/otlp/v0', obsUrl);
  const response = await fetch(url, {
    method: 'POST',
    headers: { ...authHeaders, 'Content-Type': 'application/json' },
    body: JSON.stringify(buildOtlpJsonPayload(records, report)),
  });
  if (!response.ok) {
    throw new Error(`OTLP log upload failed: ${response.status} ${response.statusText} - ${await response.text()}`);
  }
}

function recordingHeader(report: voice.SessionReport, roomTags: Record<string, string>): Uint8Array {
  const startedAt = report.audioRecordingStartedAt ?? 0;
  const header = new MetricsRecordingHeader({
    roomId: report.roomId,
    duration: BigInt(Math.max(0, Math.round(report.duration ?? 0))),
    startTime: startedAt
      ? {
          seconds: BigInt(Math.floor(startedAt / 1000)),
          nanos: Math.floor((startedAt % 1000) * 1e6),
        }
      : undefined,
    roomTags,
  });
  return header.toBinary();
}

function blobBytes(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
}

async function uploadRecordingCallback(
  obsUrl: string,
  authHeaders: Record<string, string>,
  report: voice.SessionReport,
  roomTags: Record<string, string>,
): Promise<void> {
  const formData = new FormData();
  formData.append('header', new Blob([blobBytes(recordingHeader(report, roomTags))], { type: 'application/protobuf' }), 'header.binpb');
  formData.append(
    'chat_history',
    new Blob([JSON.stringify(report.chatHistory.toJSON({ excludeTimestamp: false }))], { type: 'application/json' }),
    'chat_history.json',
  );

  if (report.audioRecordingPath && report.audioRecordingStartedAt) {
    let audioBytes: Buffer;
    try {
      audioBytes = await fs.readFile(report.audioRecordingPath);
    } catch {
      audioBytes = Buffer.alloc(0);
    }
    if (audioBytes.length > 0) {
      formData.append('audio', new Blob([blobBytes(audioBytes)], { type: 'audio/ogg' }), 'recording.ogg');
    }
  }

  const url = new URL('/observability/recordings/v0', obsUrl);
  const response = await fetch(url, {
    method: 'POST',
    headers: authHeaders,
    body: formData,
  });
  if (!response.ok) {
    throw new Error(`Recording upload failed: ${response.status} ${response.statusText} - ${await response.text()}`);
  }
}

export async function uploadReport(options: {
  agentName: string;
  session: any;
  callId: string;
  accountId?: string;
  metadata?: Record<string, unknown>;
  direction?: string;
  recordingPath?: string;
  recordingStartedAt?: number;
  transport?: Transport;
}): Promise<void> {
  const obsUrl = getObservabilityUrl();
  if (!obsUrl) return;

  const {
    agentName,
    session,
    callId,
    accountId,
    metadata,
    direction,
    recordingPath,
    recordingStartedAt,
    transport,
  } = options;

  const report = buildReport(session, callId, recordingPath, recordingStartedAt);
  const roomTags = buildRoomTags({ agentName, accountId, metadata, transport, direction });
  const authHeaders = await buildBearerAuthHeaders();

  console.log(`Uploading native LiveKit observability for ${callId} to ${obsUrl} (account_id=${accountId})`);
  await uploadOtlpLogs(obsUrl, authHeaders, report, buildOtlpLogRecords(report, agentName, roomTags));
  await uploadRecordingCallback(obsUrl, authHeaders, report, roomTags);
  console.log(`Native LiveKit observability uploaded for ${callId}`);
}
