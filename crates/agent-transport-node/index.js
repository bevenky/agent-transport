// ESM wrapper for native napi-rs binding
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const binding = require('./agent-transport.node');
export const { SipEndpoint, AudioStreamEndpoint } = binding;
export default binding;
