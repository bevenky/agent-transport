/**
 * Observability setup for agent-transport (Node.js).
 *
 * Custom upload because @livekit/agents' uploadSessionReport doesn't
 * support roomTags on MetricsRecordingHeader. We pass account_id
 * (plivo_auth_id) via roomTags so the observability server can
 * identify the account for filtering.
 *
 * Set LIVEKIT_OBSERVABILITY_URL to enable.
 */

import { createSessionReport, sessionReportToJSON, type SessionReportOptions } from '@livekit/agents/dist/voice/report.js';
import { MetricsRecordingHeader } from '@livekit/protocol';
import { AccessToken } from 'livekit-server-sdk';
import FormData from 'form-data';
import fs from 'node:fs/promises';

export function getObservabilityUrl(): string | undefined {
  return process.env.LIVEKIT_OBSERVABILITY_URL;
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

  const reportOpts: SessionReportOptions = {
    jobId: callId,
    roomId: callId,
    room: callId,
    options: session.options ?? {},
    events: session._recordedEvents ?? [],
    chatHistory: session.history?.copy?.() ?? session.history ?? [],
    enableRecording: !!recordingPath,
    startedAt: session._startedAt ?? Date.now(),
    audioRecordingPath: recordingPath,
    audioRecordingStartedAt: recordingStartedAt,
    modelUsage: session.usage?.modelUsage,
  };

  const report = createSessionReport(reportOpts);

  // Build header with roomTags for account identification
  const audioStartTime = report.audioRecordingStartedAt ?? 0;
  const roomTags: Record<string, string> = {};
  if (accountId) {
    roomTags['account_id'] = accountId;
  }

  const headerMsg = new MetricsRecordingHeader({
    roomId: report.roomId,
    duration: BigInt(0),
    startTime: {
      seconds: BigInt(Math.floor(audioStartTime / 1000)),
      nanos: Math.floor((audioStartTime % 1000) * 1e6),
    },
    roomTags,
  });
  const headerBytes = Buffer.from(headerMsg.toBinary());

  // Build multipart form
  const formData = new FormData();

  formData.append('header', headerBytes, {
    filename: 'header.binpb',
    contentType: 'application/protobuf',
    knownLength: headerBytes.length,
    header: { 'Content-Type': 'application/protobuf', 'Content-Length': headerBytes.length.toString() },
  });

  const chatHistoryJson = JSON.stringify(sessionReportToJSON(report));
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

  // JWT auth
  const apiKey = process.env.LIVEKIT_API_KEY;
  const apiSecret = process.env.LIVEKIT_API_SECRET;
  if (!apiKey || !apiSecret) {
    throw new Error('LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set for session upload');
  }
  const token = new AccessToken(apiKey, apiSecret, { ttl: '6h' });
  token.addObservabilityGrant({ write: true });
  const jwt = await token.toJwt();

  // Upload
  const url = new URL('/observability/recordings/v0', obsUrl);
  return new Promise<void>((resolve, reject) => {
    formData.submit(
      { protocol: url.protocol, host: url.hostname, port: url.port || undefined, path: url.pathname, method: 'POST', headers: { Authorization: `Bearer ${jwt}` } },
      (err, res) => {
        if (err) { reject(new Error(`Failed to upload session report: ${err.message}`)); return; }
        if (res.statusCode && res.statusCode >= 400) {
          let body = '';
          res.on('data', (chunk) => { body += chunk.toString(); });
          res.on('end', () => { reject(new Error(`Upload failed: ${res.statusCode} ${res.statusMessage} - ${body}`)); });
          return;
        }
        res.resume();
        res.on('end', () => resolve());
      },
    );
  });
}
