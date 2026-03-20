//! G.711 Appendix I Packet Loss Concealment (PLC)
//!
//! Pitch-based concealment following ITU-T G.711 Appendix I:
//! - Maintains history buffer of recent decoded audio (~48.75ms)
//! - Detects pitch via autocorrelation (50-400Hz range)
//! - Repeats last pitch period with overlap-add crossfade
//! - Exponential gain decay per concealed frame
//! - Fades to silence after ~60ms of continuous concealment
//!
//! Adapted from rtpsip (audited against FreeSWITCH G.711 Appendix I).
//! Feature-gated: only compiled with `plc` feature.

const HISTORY_LEN: usize = 390; // ~48.75ms at 8kHz
const PITCH_MIN: usize = 20;    // 400Hz at 8kHz
const PITCH_MAX: usize = 160;   // 50Hz at 8kHz
const OLA_WINDOW: usize = 16;   // overlap-add crossfade samples
const DECAY_PER_FRAME: f32 = 0.96;
const MAX_CONCEAL_MS: f32 = 60.0;

/// G.711 Appendix I pitch-based Packet Loss Concealer.
pub struct PacketLossConcealer {
    history: Vec<i16>,
    history_pos: usize,
    history_valid: usize,
    pitch_period: usize,
    overlap_buf: Vec<i16>,
    conceal_count: usize,
    gain: f32,
    samples_per_packet: usize,
    max_conceal_frames: usize,
}

impl PacketLossConcealer {
    pub fn new(samples_per_packet: usize, sample_rate: u32) -> Self {
        let sr = if sample_rate > 0 { sample_rate as f32 } else { 8000.0 };
        let max_conceal_frames = if samples_per_packet > 0 {
            ((MAX_CONCEAL_MS / (samples_per_packet as f32 / sr * 1000.0)).ceil() as usize).max(1)
        } else { 3 };

        Self {
            history: vec![0i16; HISTORY_LEN], history_pos: 0, history_valid: 0,
            pitch_period: samples_per_packet.min(PITCH_MAX).max(PITCH_MIN),
            overlap_buf: Vec::new(), conceal_count: 0, gain: 1.0,
            samples_per_packet, max_conceal_frames,
        }
    }

    /// Feed good audio into history. Resets concealment state.
    pub fn update(&mut self, samples: &[i16]) {
        if samples.is_empty() { return; }

        // Crossfade recovery if we were concealing
        if self.conceal_count > 0 && !self.overlap_buf.is_empty() {
            let mut blended = samples.to_vec();
            let ola = self.overlap_buf.len().min(samples.len()).min(OLA_WINDOW);
            apply_crossfade(&self.overlap_buf, &mut blended, ola);
            self.append_history(&blended);
        } else {
            self.append_history(samples);
        }

        // Save tail for potential next loss
        let tail_len = OLA_WINDOW.min(samples.len());
        self.overlap_buf = samples[samples.len() - tail_len..].to_vec();
        self.conceal_count = 0;
        self.gain = 1.0;
        self.pitch_period = self.detect_pitch();
    }

    /// Generate concealment frame for a lost packet.
    pub fn conceal(&mut self) -> Vec<i16> {
        let n = self.samples_per_packet;
        if n == 0 { return Vec::new(); }
        self.conceal_count += 1;

        if self.conceal_count > self.max_conceal_frames {
            self.gain = 0.0;
            return vec![0i16; n];
        }

        self.gain *= DECAY_PER_FRAME;
        let mut frame = self.generate_pitch_repeat(n, self.gain);

        if self.conceal_count == 1 && !self.overlap_buf.is_empty() {
            let ola = self.overlap_buf.len().min(n).min(OLA_WINDOW);
            apply_crossfade(&self.overlap_buf, &mut frame, ola);
            self.overlap_buf.clear();
        }

        self.append_history(&frame);
        frame
    }

    pub fn reset(&mut self) {
        self.history.fill(0);
        self.history_pos = 0;
        self.history_valid = 0;
        self.pitch_period = self.samples_per_packet.min(PITCH_MAX).max(PITCH_MIN);
        self.overlap_buf.clear();
        self.conceal_count = 0;
        self.gain = 1.0;
    }

    fn append_history(&mut self, samples: &[i16]) {
        for &s in samples {
            self.history[self.history_pos] = s;
            self.history_pos = (self.history_pos + 1) % HISTORY_LEN;
            if self.history_valid < HISTORY_LEN { self.history_valid += 1; }
        }
    }

    fn read_history_tail(&self, count: usize) -> Vec<i16> {
        let count = count.min(self.history_valid);
        let start = if self.history_pos >= count { self.history_pos - count } else { HISTORY_LEN - (count - self.history_pos) };
        (0..count).map(|i| self.history[(start + i) % HISTORY_LEN]).collect()
    }

    fn detect_pitch(&self) -> usize {
        let analysis_len = PITCH_MAX * 2;
        if self.history_valid < analysis_len {
            return self.samples_per_packet.min(PITCH_MAX).max(PITCH_MIN);
        }

        let buf = self.read_history_tail(analysis_len);
        let window_start = buf.len() - PITCH_MAX;
        let energy_window: f64 = buf[window_start..].iter().map(|&s| (s as f64) * (s as f64)).sum();
        if energy_window < 1.0 { return self.samples_per_packet.min(PITCH_MAX).max(PITCH_MIN); }

        let (mut best_period, mut best_corr, mut found_strong) = (PITCH_MIN, -1.0f64, false);

        for lag in PITCH_MIN..=PITCH_MAX {
            let ref_start = window_start.saturating_sub(lag);
            if ref_start + lag > buf.len() { continue; }

            let (mut cross, mut e_ref, mut e_win) = (0.0f64, 0.0f64, 0.0f64);
            for i in 0..lag {
                let (a, b) = (buf[window_start + i] as f64, buf[ref_start + i] as f64);
                cross += a * b; e_ref += b * b; e_win += a * a;
            }
            if e_ref < 1.0 || e_win < 1.0 { continue; }

            let norm = cross / (e_win.sqrt() * e_ref.sqrt());
            if !found_strong {
                if norm > best_corr { best_corr = norm; best_period = lag; }
                if best_corr >= 0.5 { found_strong = true; }
            } else if norm > best_corr * 1.05 {
                best_corr = norm; best_period = lag;
            }
        }

        if best_corr < 0.5 { self.samples_per_packet.min(PITCH_MAX).max(PITCH_MIN) } else { best_period }
    }

    fn generate_pitch_repeat(&self, n: usize, gain: f32) -> Vec<i16> {
        let period = self.pitch_period;
        if period == 0 || self.history_valid == 0 { return vec![0i16; n]; }

        let tail = self.read_history_tail((period * 2).min(self.history_valid));
        let last_period: Vec<i16> = if tail.len() >= period { tail[tail.len() - period..].to_vec() } else { tail.clone() };
        let prev_period: Vec<i16> = if tail.len() >= period * 2 { tail[tail.len() - period * 2..tail.len() - period].to_vec() } else { last_period.clone() };

        let template = if prev_period == last_period { last_period } else { build_ola_period(&prev_period, &last_period) };
        let tlen = template.len();
        if tlen == 0 { return vec![0i16; n]; }

        let ola = OLA_WINDOW.min(tlen);
        (0..n).map(|i| {
            let pos = i % tlen;
            let mut s = template[pos] as f32 * gain;
            if pos < ola && i >= tlen {
                let w = hann_weight(pos, ola);
                s = template[tlen - ola + pos] as f32 * gain * (1.0 - w) + s * w;
            }
            clamp_i16(s)
        }).collect()
    }
}

fn build_ola_period(prev: &[i16], cur: &[i16]) -> Vec<i16> {
    let mut out = cur.to_vec();
    let ola = OLA_WINDOW.min(cur.len()).min(prev.len());
    let prev_off = prev.len().saturating_sub(ola);
    for i in 0..ola {
        let w = hann_weight(i, ola);
        out[i] = clamp_i16(prev[prev_off + i] as f32 * (1.0 - w) + cur[i] as f32 * w);
    }
    out
}

fn apply_crossfade(old: &[i16], frame: &mut [i16], len: usize) {
    let len = len.min(old.len()).min(frame.len());
    for i in 0..len {
        let w = hann_weight(i, len);
        frame[i] = clamp_i16(old[i] as f32 * (1.0 - w) + frame[i] as f32 * w);
    }
}

#[inline]
fn hann_weight(i: usize, len: usize) -> f32 {
    if len <= 1 { return 1.0; }
    0.5 * (1.0 - (std::f32::consts::PI * i as f32 / (len - 1).max(1) as f32).cos())
}

#[inline]
fn clamp_i16(v: f32) -> i16 { v.round().clamp(i16::MIN as f32, i16::MAX as f32) as i16 }

#[cfg(test)]
mod tests {
    use super::*;

    fn sine_frame(freq: f64, rate: f64, n: usize, amp: f64) -> Vec<i16> {
        (0..n).map(|i| (amp * (2.0 * std::f64::consts::PI * freq * i as f64 / rate).sin()) as i16).collect()
    }

    #[test]
    fn test_conceal_produces_correct_length() {
        let mut plc = PacketLossConcealer::new(160, 8000);
        let frame = sine_frame(200.0, 8000.0, 160, 10000.0);
        plc.update(&frame);
        let concealed = plc.conceal();
        assert_eq!(concealed.len(), 160);
    }

    #[test]
    fn test_conceal_not_silence_after_voice() {
        let mut plc = PacketLossConcealer::new(160, 8000);
        // Feed several frames to build up history
        for _ in 0..5 {
            plc.update(&sine_frame(200.0, 8000.0, 160, 10000.0));
        }
        let concealed = plc.conceal();
        let energy: f64 = concealed.iter().map(|&s| (s as f64).abs()).sum();
        assert!(energy > 100.0, "Concealed frame should not be silence after voice: energy={}", energy);
    }

    #[test]
    fn test_conceal_decays_to_silence() {
        let mut plc = PacketLossConcealer::new(160, 8000);
        for _ in 0..5 { plc.update(&sine_frame(200.0, 8000.0, 160, 10000.0)); }
        // Conceal many frames — should eventually fade to silence
        let mut last_energy = f64::MAX;
        for _ in 0..10 {
            let c = plc.conceal();
            let energy: f64 = c.iter().map(|&s| (s as f64).abs()).sum();
            assert!(energy <= last_energy + 1.0, "Energy should decay");
            last_energy = energy;
        }
    }

    #[test]
    fn test_update_resets_concealment() {
        let mut plc = PacketLossConcealer::new(160, 8000);
        plc.update(&sine_frame(200.0, 8000.0, 160, 10000.0));
        plc.conceal(); plc.conceal();
        plc.update(&sine_frame(200.0, 8000.0, 160, 10000.0));
        // After update, conceal_count should reset — gain should be near 1.0
        let c = plc.conceal();
        let energy: f64 = c.iter().map(|&s| (s as f64).abs()).sum();
        assert!(energy > 100.0, "First conceal after update should have energy");
    }

    #[test]
    fn test_reset_clears_state() {
        let mut plc = PacketLossConcealer::new(160, 8000);
        plc.update(&sine_frame(200.0, 8000.0, 160, 10000.0));
        plc.reset();
        assert_eq!(plc.history_valid, 0);
        assert_eq!(plc.conceal_count, 0);
    }

    #[test]
    fn test_empty_input() {
        let mut plc = PacketLossConcealer::new(160, 8000);
        plc.update(&[]);
        let c = plc.conceal();
        assert_eq!(c.len(), 160);
    }
}
