/**
 * Type declarations for @livekit/agents internal voice/io module.
 *
 * AudioInput and AudioOutput are not publicly exported from @livekit/agents,
 * but we need to extend them (same as LiveKit's own _ParticipantAudioInputStream
 * and _ParticipantAudioOutput). This declaration file provides TypeScript types
 * for the deep import.
 */
declare module '@livekit/agents/dist/voice/io.js' {
  import type { AudioFrame } from '@livekit/rtc-node';
  import type { ReadableStream } from 'node:stream/web';
  import { EventEmitter } from 'node:events';

  // ── Pipeline node types (STT/LLM/TTS) ────────────────────────────
  // Minimal shapes — downstream code usually treats these as opaque
  // callables so we just declare the general call signature upstream
  // uses. Upstream has richer generic parameters (ModelSettings,
  // ChatContext, etc.) but those aren't needed here.
  export type STTNode = (
    audio: ReadableStream<AudioFrame>,
    modelSettings: unknown,
  ) => Promise<ReadableStream<unknown> | null>;

  export type LLMNode = (
    chatCtx: unknown,
    toolCtx: unknown,
    modelSettings: unknown,
  ) => Promise<ReadableStream<unknown> | null>;

  export type TTSNode = (
    text: ReadableStream<string>,
    modelSettings: unknown,
  ) => Promise<ReadableStream<AudioFrame> | null>;

  // ── TimedString — word-level transcript alignment ────────────────
  export const TIMED_STRING_SYMBOL: unique symbol;
  export interface TimedString {
    readonly [TIMED_STRING_SYMBOL]: true;
    text: string;
    startTime?: number;
    endTime?: number;
    confidence?: number;
    startTimeOffset?: number;
  }
  export function createTimedString(opts: {
    text: string;
    startTime?: number;
    endTime?: number;
    confidence?: number;
    startTimeOffset?: number;
  }): TimedString;
  export function isTimedString(value: unknown): value is TimedString;

  export interface MultiInputStream<T> {
    get stream(): ReadableStream<T>;
    get inputCount(): number;
    get isClosed(): boolean;
    addInputStream(source: ReadableStream<T>): string;
    removeInputStream(id: string): Promise<void>;
    close(): Promise<void>;
  }

  export abstract class AudioInput {
    protected multiStream: MultiInputStream<AudioFrame>;
    get stream(): ReadableStream<AudioFrame>;
    close(): Promise<void>;
    onAttached(): void;
    onDetached(): void;
  }

  export abstract class TextOutput {
    protected readonly nextInChain?: TextOutput;
    constructor(nextInChain?: TextOutput);
    abstract captureText(text: string | TimedString): Promise<void>;
    abstract flush(): void;
    onAttached(): void;
    onDetached(): void;
  }

  export interface AudioOutputCapabilities {
    pause: boolean;
  }

  export interface PlaybackFinishedEvent {
    playbackPosition: number;
    interrupted: boolean;
    synchronizedTranscript?: string;
  }

  export interface PlaybackStartedEvent {
    createdAt: number;
  }

  export abstract class AudioOutput extends EventEmitter {
    sampleRate?: number | undefined;
    protected readonly nextInChain?: AudioOutput | undefined;
    static readonly EVENT_PLAYBACK_STARTED: string;
    static readonly EVENT_PLAYBACK_FINISHED: string;
    protected readonly capabilities: AudioOutputCapabilities;

    constructor(
      sampleRate?: number | undefined,
      nextInChain?: AudioOutput | undefined,
      capabilities?: AudioOutputCapabilities,
    );

    get canPause(): boolean;
    captureFrame(_frame: AudioFrame): Promise<void>;
    waitForPlayout(): Promise<PlaybackFinishedEvent>;
    onPlaybackStarted(createdAt: number): void;
    onPlaybackFinished(options: PlaybackFinishedEvent): void;
    flush(): void;
    abstract clearBuffer(): void;
    onAttached(): void;
    onDetached(): void;
    pause(): void;
    resume(): void;
  }
}
