import assert from 'node:assert/strict';
import test from 'node:test';

import { buildRoomTags } from '../../livekit/observability.js';

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
