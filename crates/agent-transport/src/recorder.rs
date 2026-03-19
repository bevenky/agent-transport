//! WAV file recorder for call audio.

use std::fs::File;
use std::io::{Seek, SeekFrom, Write};

pub(crate) struct WavRecorder {
    file: File,
    sample_count: u32,
}

impl WavRecorder {
    pub fn new(path: &str) -> std::io::Result<Self> {
        let mut f = File::create(path)?;
        f.write_all(&[0u8; 44])?; // placeholder header
        Ok(Self { file: f, sample_count: 0 })
    }

    pub fn write_samples(&mut self, samples: &[i16]) {
        for &s in samples { let _ = self.file.write_all(&s.to_le_bytes()); }
        self.sample_count += samples.len() as u32;
    }

    pub fn finalize(&mut self) {
        let ds = self.sample_count * 2;
        let _ = self.file.seek(SeekFrom::Start(0));
        let _ = self.file.write_all(b"RIFF");
        let _ = self.file.write_all(&(36 + ds).to_le_bytes());
        let _ = self.file.write_all(b"WAVEfmt ");
        let _ = self.file.write_all(&16u32.to_le_bytes());
        let _ = self.file.write_all(&1u16.to_le_bytes()); // PCM
        let _ = self.file.write_all(&1u16.to_le_bytes()); // mono
        let _ = self.file.write_all(&16000u32.to_le_bytes()); // sample rate
        let _ = self.file.write_all(&32000u32.to_le_bytes()); // byte rate
        let _ = self.file.write_all(&2u16.to_le_bytes()); // block align
        let _ = self.file.write_all(&16u16.to_le_bytes()); // bits per sample
        let _ = self.file.write_all(b"data");
        let _ = self.file.write_all(&ds.to_le_bytes());
    }
}
