import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeRecordingStartedAt } from '../../livekit/observability.js';

test('normalizeRecordingStartedAt keeps epoch seconds unchanged', () => {
  assert.equal(normalizeRecordingStartedAt(1_700_000_000.123), 1_700_000_000.123);
});

test('normalizeRecordingStartedAt converts epoch milliseconds to seconds', () => {
  assert.equal(normalizeRecordingStartedAt(1_700_000_000_123), 1_700_000_000.123);
});

test('normalizeRecordingStartedAt treats missing and invalid timestamps as unset', () => {
  assert.equal(normalizeRecordingStartedAt(), 0);
  assert.equal(normalizeRecordingStartedAt(0), 0);
  assert.equal(normalizeRecordingStartedAt(Number.NaN), 0);
});
