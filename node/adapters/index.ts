/**
 * Agent Transport Adapters for Node.js
 *
 * The Rust Node.js binding (agent-transport) exposes SipEndpoint and
 * AudioStreamEndpoint directly. This package provides TypeScript types
 * and adapter utilities for integration with Node.js frameworks.
 *
 * For LiveKit/Pipecat integration, use the Python adapters
 * (agent-transport-adapters) as those frameworks are Python-based.
 */

export interface TransportSession {
  sessionId: number;
  sendAudio(data: Int16Array, sampleRate: number): void;
  recvAudio(): Int16Array | null;
  clearBuffer(): void;
  hangup(): void;
}

export interface TransportEvent {
  eventType: string;
  callId?: number;
  digit?: string;
  reason?: string;
}
