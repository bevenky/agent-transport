import assert from 'node:assert/strict';
import test from 'node:test';

import { voice } from '@livekit/agents';
import { buildOtlpLogRecords, buildRoomTags } from '../../livekit/observability.js';

// Round-trip test: the obs server has a hand-crafted fixture
// (`accepts raw session.report logs from Agent Transport Node` in
// agent-observability/tests/routes.test.ts) that asserts what shape it
// expects from this builder. If we change the field names or the body
// string here without updating that fixture (or vice-versa), uploads
// will silently fail to populate raw_report — the obs side branches on
// `body === "session report"` and reads specific attribute keys. This
// test pins the contract on the Node side.
test('buildOtlpLogRecords emits the envelope obs server expects', () => {
  const report = voice.createSessionReport({
    jobId: 'job-room-node-raw-report',
    roomId: 'room-node-raw-report',
    room: 'room-node-raw-report',
    options: { max_tool_steps: 2 },
    events: [],
    enableRecording: false,
    chatHistory: { items: [], toJSON: () => ({ items: [] }) },
    startedAt: 1700000000,
    modelUsage: null,
  });

  const records = buildOtlpLogRecords(report, 'node-support-agent', {
    account_id: 'acct-node',
    transport: 'audio_stream',
  });

  assert.equal(records.length, 1, 'one OTLP record per session');
  const [record] = records;

  // Body string is the discriminator the obs handler branches on.
  // Changing it breaks `persistLiveKitOtlpLogs`'s "session report" branch.
  assert.equal(record.body, 'session report');

  // Every attribute key obs reads from the record. Keep in sync with the
  // fixture in agent-observability/tests/routes.test.ts:665.
  assert.equal(record.attributes.room_id, 'room-node-raw-report');
  assert.equal(record.attributes.job_id, 'job-room-node-raw-report');
  assert.equal(record.attributes['logger.name'], 'chat_history');
  assert.equal(record.attributes.agent_name, 'node-support-agent');
  assert.equal(typeof record.attributes.sdk_version, 'string');
  assert.deepEqual(record.attributes.room_tags, {
    account_id: 'acct-node',
    transport: 'audio_stream',
  });

  // session.report is the SDK's own JSON projection — assert it's an
  // object and carries the room id (sanity check that we passed the
  // right report through, not that we replicate the SDK's exact shape).
  assert.equal(typeof record.attributes['session.report'], 'object');
  // sessionReportToJSON projects to snake_case (matches the obs fixture).
  assert.equal(record.attributes['session.report']?.room_id, 'room-node-raw-report');
});

test('buildRoomTags combines session metadata with transport tags', () => {
  assert.deepEqual(
    buildRoomTags({
      agentName: 'support-agent',
      accountId: 'acct-1',
      metadata: {
        account_id: 'acct-1',
        customer_tier: 'gold',
        skipped: { nested: true },
      },
      transport: 'audio_stream',
      direction: 'inbound',
    }),
    {
      account_id: 'acct-1',
      customer_tier: 'gold',
      agent_name: 'support-agent',
      transport: 'audio_stream',
      direction: 'inbound',
    },
  );
});
