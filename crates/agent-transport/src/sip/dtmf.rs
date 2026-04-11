//! DTMF send/receive — RFC 4733 (RTP telephone-event) and SIP INFO.

/// Map a DTMF digit character to RFC 4733 event code.
pub(crate) fn digit_to_event(digit: char) -> Option<u8> {
    match digit {
        '0' => Some(0),
        '1' => Some(1),
        '2' => Some(2),
        '3' => Some(3),
        '4' => Some(4),
        '5' => Some(5),
        '6' => Some(6),
        '7' => Some(7),
        '8' => Some(8),
        '9' => Some(9),
        '*' => Some(10),
        '#' => Some(11),
        'A' | 'a' => Some(12),
        'B' | 'b' => Some(13),
        'C' | 'c' => Some(14),
        'D' | 'd' => Some(15),
        _ => None,
    }
}

/// Map an RFC 4733 event code back to a digit character.
pub(crate) fn event_to_digit(event: u8) -> Option<char> {
    match event {
        0 => Some('0'),
        1 => Some('1'),
        2 => Some('2'),
        3 => Some('3'),
        4 => Some('4'),
        5 => Some('5'),
        6 => Some('6'),
        7 => Some('7'),
        8 => Some('8'),
        9 => Some('9'),
        10 => Some('*'),
        11 => Some('#'),
        12 => Some('A'),
        13 => Some('B'),
        14 => Some('C'),
        15 => Some('D'),
        _ => None,
    }
}

/// Encode an RFC 4733 DTMF event payload (4 bytes).
///
/// Layout:
/// ```text
///  0                   1                   2                   3
///  0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
/// +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
/// |     event     |E|R| volume    |          duration             |
/// +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
/// ```
pub(crate) fn encode_rfc4733(event: u8, end: bool, volume: u8, duration: u16) -> [u8; 4] {
    let mut payload = [0u8; 4];
    payload[0] = event;
    payload[1] = if end { 0x80 } else { 0x00 } | (volume & 0x3F);
    payload[2] = (duration >> 8) as u8;
    payload[3] = duration as u8;
    payload
}

/// Decode an RFC 4733 DTMF event payload.
/// Returns (event_code, is_end, volume, duration).
pub(crate) fn decode_rfc4733(payload: &[u8]) -> Option<(u8, bool, u8, u16)> {
    if payload.len() < 4 {
        return None;
    }
    let event = payload[0];
    let end = (payload[1] & 0x80) != 0;
    let volume = payload[1] & 0x3F;
    let duration = u16::from_be_bytes([payload[2], payload[3]]);
    Some((event, end, volume, duration))
}

/// Result of processing one DTMF RTP packet against the current tracker state.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum DtmfTransition {
    /// No state change — e.g. duplicate END retransmit for an already-emitted digit.
    NoChange,
    /// A new non-END packet: tracker should move to Some(ev) and the timer
    /// should be reset to "now". This is the only signal the caller needs
    /// to know the digit changed (and thus the timer baseline changed).
    StartOrChange { event: u8 },
    /// A retransmitted non-END packet for the same event already being
    /// tracked: the tracker stays the same, the timer should NOT move.
    StartRetransmit,
    /// END packet for the currently tracked event: caller should emit
    /// the digit and clear the tracker + timer.
    EndEmit { event: u8 },
    /// END packet for a digit no longer being tracked: nothing to emit
    /// (already handled). This is the "retransmitted END" case.
    EndStale,
}

/// Pure state-transition function for the DTMF tracker used by the RTP
/// recv loop. Given the current tracker and a decoded RTP DTMF event,
/// return the transition the caller should apply.
///
/// This is extracted so the state machine can be unit-tested without
/// running the whole RTP transport. See tests below for the packet-loss
/// regression (previously the timer was only reset when `dtmf_timer.is_none()`,
/// which leaked the first digit's start time into the second digit when the
/// END packet for the first digit was lost).
pub(crate) fn dtmf_transition(current: Option<u8>, incoming_ev: u8, incoming_end: bool) -> DtmfTransition {
    if incoming_end {
        if current.is_some() {
            DtmfTransition::EndEmit { event: incoming_ev }
        } else {
            DtmfTransition::EndStale
        }
    } else if current != Some(incoming_ev) {
        DtmfTransition::StartOrChange { event: incoming_ev }
    } else {
        DtmfTransition::StartRetransmit
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_digit_event_roundtrip() {
        for digit in "0123456789*#ABCD".chars() {
            let event = digit_to_event(digit).unwrap();
            let back = event_to_digit(event).unwrap();
            assert_eq!(back, digit.to_ascii_uppercase());
        }
    }

    #[test]
    fn test_rfc4733_encode_decode() {
        let payload = encode_rfc4733(5, false, 10, 1600);
        let (event, end, volume, duration) = decode_rfc4733(&payload).unwrap();
        assert_eq!(event, 5);
        assert!(!end);
        assert_eq!(volume, 10);
        assert_eq!(duration, 1600);
    }

    #[test]
    fn test_rfc4733_end_flag() {
        let payload = encode_rfc4733(11, true, 10, 3200);
        let (event, end, _, duration) = decode_rfc4733(&payload).unwrap();
        assert_eq!(event, 11); // '#'
        assert!(end);
        assert_eq!(duration, 3200);
    }

    // ─── dtmf_transition state machine ───────────────────────────────────

    #[test]
    fn test_transition_first_start_is_change() {
        // First packet of a new digit → StartOrChange (caller resets timer)
        assert_eq!(
            dtmf_transition(None, 5, false),
            DtmfTransition::StartOrChange { event: 5 },
        );
    }

    #[test]
    fn test_transition_start_retransmit_keeps_timer() {
        // Retransmit of the same START → tracker unchanged, timer NOT reset
        assert_eq!(
            dtmf_transition(Some(5), 5, false),
            DtmfTransition::StartRetransmit,
        );
    }

    #[test]
    fn test_transition_end_for_tracked_emits() {
        assert_eq!(
            dtmf_transition(Some(7), 7, true),
            DtmfTransition::EndEmit { event: 7 },
        );
    }

    #[test]
    fn test_transition_stale_end_ignored() {
        // END packet with no tracker (first one already consumed) → ignore
        assert_eq!(
            dtmf_transition(None, 7, true),
            DtmfTransition::EndStale,
        );
    }

    #[test]
    fn test_transition_digit_change_without_end_resets() {
        // Regression: the old code used `if dtmf_timer.is_none()` to gate
        // timer reset, so a new digit whose START arrived while the tracker
        // was still holding the previous digit (END lost) kept the OLD
        // baseline → timeout fired with wrong timing. The new transition
        // reports StartOrChange so the caller resets the timer correctly.
        assert_eq!(
            dtmf_transition(Some(1), 2, false),
            DtmfTransition::StartOrChange { event: 2 },
        );
    }

    #[test]
    fn test_transition_lost_end_scenario() {
        // End-to-end walkthrough of the packet-loss regression:
        //   packets: START(1), START(1)*retransmit, [END(1) LOST],
        //            START(2), START(2)*retransmit, END(2)
        let mut tracker: Option<u8> = None;
        let mut timer_resets = 0usize;
        let mut emitted = Vec::new();

        let mut apply = |tr: DtmfTransition, tracker: &mut Option<u8>| {
            match tr {
                DtmfTransition::StartOrChange { event } => {
                    *tracker = Some(event);
                    timer_resets += 1;
                }
                DtmfTransition::StartRetransmit => {}
                DtmfTransition::EndEmit { event } => {
                    emitted.push(event);
                    *tracker = None;
                }
                DtmfTransition::EndStale | DtmfTransition::NoChange => {}
            }
        };

        // START(1)
        apply(dtmf_transition(tracker, 1, false), &mut tracker);
        // START(1) retransmit
        apply(dtmf_transition(tracker, 1, false), &mut tracker);
        // END(1) LOST — skip this packet entirely
        // START(2) — must reset the timer even though tracker is still Some(1)
        apply(dtmf_transition(tracker, 2, false), &mut tracker);
        // START(2) retransmit
        apply(dtmf_transition(tracker, 2, false), &mut tracker);
        // END(2)
        apply(dtmf_transition(tracker, 2, true), &mut tracker);

        // Timer must have reset exactly twice: once for digit 1, once for digit 2.
        // The OLD buggy code reset only once (for digit 1) because
        // `if timer.is_none()` was false when digit 2's START arrived.
        assert_eq!(timer_resets, 2, "timer must reset on digit change");
        assert_eq!(emitted, vec![2], "only digit 2 emits (digit 1 was lost)");
    }
}
