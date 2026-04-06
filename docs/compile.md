# Building from Source

Requires Rust, a C compiler, and CMake.

```bash
# Rust core
cargo build                                     # Core library (SIP transport)
cargo build --features audio-stream             # + Plivo audio streaming
cargo build --features audio-processing         # + jitter buffer, PLC, comfort noise

# Python binding
cd crates/agent-transport-python && pip install -e .
```

> **CMake 4.x:** If you see `Compatibility with CMake < 3.5 has been removed`, set `CMAKE_POLICY_VERSION_MINIMUM=3.5` in your environment.
