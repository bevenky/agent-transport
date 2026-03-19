use std::collections::HashMap;
use std::ffi::CString;

use tracing::error;

use crate::error::{EndpointError, Result};

use pjsua_sys::*;

/// Extract all custom SIP headers (X-* headers) from a SIP message by walking
/// the header linked list. Returns a map of header name -> value.
///
/// This handles headers from any SIP message: INVITE, 180, 200 OK, BYE, etc.
/// Standard headers (To, From, Via, etc.) are skipped — only X- prefixed
/// custom headers are collected.
pub(crate) unsafe fn extract_custom_headers(msg: *const pjsip_msg) -> HashMap<String, String> {
    let mut headers = HashMap::new();
    if msg.is_null() {
        return headers;
    }

    // The header list is a circular doubly-linked list with msg.hdr as the sentinel.
    let head = &(*msg).hdr as *const pjsip_hdr;
    let mut hdr = (*msg).hdr.next;

    while hdr != head as *mut pjsip_hdr {
        if hdr.is_null() {
            break;
        }

        let name = pj_str_to_string(&(*hdr).name);
        // Collect X- prefixed headers (case-insensitive per RFC 3261)
        if name.len() >= 2 && (name.starts_with("X-") || name.starts_with("x-")) {
            // Custom X- headers are parsed as pjsip_generic_string_hdr
            let generic = hdr as *const pjsip_generic_string_hdr;
            let value = pj_str_to_string(&(*generic).hvalue);
            headers.insert(name, value);
        }

        hdr = (*hdr).next;
    }

    headers
}

/// Extract custom headers from a pjsip_event if it contains a received SIP message.
/// Works for TSX_STATE events (state changes triggered by SIP responses/requests).
pub(crate) unsafe fn extract_headers_from_event(
    e: *const pjsip_event,
) -> HashMap<String, String> {
    if e.is_null() {
        return HashMap::new();
    }

    // TSX_STATE events contain the SIP message that triggered the state change
    if (*e).type_ == pjsip_event_id_e_PJSIP_EVENT_TSX_STATE {
        let tsx = &(*e).body.tsx_state;
        // Check if the source is a received message
        if tsx.type_ == pjsip_event_id_e_PJSIP_EVENT_RX_MSG {
            let rdata = tsx.src.rdata;
            if !rdata.is_null() {
                let msg = (*rdata).msg_info.msg;
                return extract_custom_headers(msg);
            }
        }
    }

    HashMap::new()
}

pub(crate) unsafe fn get_call_info(call_id: pjsua_call_id) -> Result<pjsua_call_info> {
    let mut ci: pjsua_call_info = std::mem::zeroed();
    let status = pjsua_call_get_info(call_id, &mut ci);
    if status != pj_constants__PJ_SUCCESS as i32 {
        return Err(EndpointError::InvalidCallId(call_id));
    }
    Ok(ci)
}

pub(crate) unsafe fn pj_str_from_cstr(s: &CString) -> pj_str_t {
    pj_str_t {
        ptr: s.as_ptr() as *mut _,
        slen: s.as_bytes().len() as _,
    }
}

pub(crate) unsafe fn pj_str_to_string(s: &pj_str_t) -> String {
    if s.ptr.is_null() || s.slen <= 0 {
        return String::new();
    }
    let slice = std::slice::from_raw_parts(s.ptr as *const u8, s.slen as usize);
    String::from_utf8_lossy(slice).into_owned()
}

pub(crate) fn check_status(status: i32, context: &str) -> Result<()> {
    if status == pj_constants__PJ_SUCCESS as i32 {
        Ok(())
    } else {
        error!("{} failed with status {}", context, status);
        Err(EndpointError::Pjsua {
            code: status,
            message: format!("{} failed", context),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pj_str_roundtrip() {
        let cs = CString::new("hello world").unwrap();
        unsafe {
            let pj = pj_str_from_cstr(&cs);
            let back = pj_str_to_string(&pj);
            assert_eq!(back, "hello world");
        }
    }

    #[test]
    fn test_pj_str_to_string_null() {
        unsafe {
            let s = pj_str_t {
                ptr: std::ptr::null_mut(),
                slen: 0,
            };
            assert_eq!(pj_str_to_string(&s), "");
        }
    }

    #[test]
    fn test_pj_str_to_string_negative_len() {
        unsafe {
            let s = pj_str_t {
                ptr: std::ptr::null_mut(),
                slen: -1,
            };
            assert_eq!(pj_str_to_string(&s), "");
        }
    }

    #[test]
    fn test_check_status_success() {
        assert!(check_status(pj_constants__PJ_SUCCESS as i32, "test").is_ok());
    }

    #[test]
    fn test_check_status_failure() {
        let result = check_status(70001, "test_op");
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert!(err.to_string().contains("test_op"));
    }

    // --- Header extraction tests using mock pjsip_msg ---

    /// Build a fake pjsip_msg on the heap with a circular header list.
    /// The Box ensures a stable address for the sentinel's self-pointers.
    /// Returns (msg_box, cstrings, header_boxes) — keep all alive for the test duration.
    unsafe fn build_fake_msg(
        headers: &[(&str, &str)],
    ) -> (
        Box<pjsip_msg>,
        Vec<CString>,
        Vec<Box<pjsip_generic_string_hdr>>,
    ) {
        let mut msg = Box::new(std::mem::zeroed::<pjsip_msg>());
        // Initialize sentinel: prev and next point to itself (empty circular list)
        let sentinel = &mut msg.hdr as *mut pjsip_hdr;
        (*sentinel).next = sentinel;
        (*sentinel).prev = sentinel;

        let mut cstrings = Vec::new();
        let mut hdr_boxes = Vec::new();

        for &(name, value) in headers {
            let name_cs = CString::new(name).unwrap();
            let value_cs = CString::new(value).unwrap();

            let mut hdr: Box<pjsip_generic_string_hdr> = Box::new(std::mem::zeroed());
            hdr.name = pj_str_t {
                ptr: name_cs.as_ptr() as *mut _,
                slen: name_cs.as_bytes().len() as _,
            };
            hdr.hvalue = pj_str_t {
                ptr: value_cs.as_ptr() as *mut _,
                slen: value_cs.as_bytes().len() as _,
            };

            // Insert before sentinel (append to end of list)
            let hdr_ptr = &mut *hdr as *mut pjsip_generic_string_hdr as *mut pjsip_hdr;
            (*hdr_ptr).next = sentinel;
            (*hdr_ptr).prev = (*sentinel).prev;
            (*(*sentinel).prev).next = hdr_ptr;
            (*sentinel).prev = hdr_ptr;

            cstrings.push(name_cs);
            cstrings.push(value_cs);
            hdr_boxes.push(hdr);
        }

        (msg, cstrings, hdr_boxes)
    }

    #[test]
    fn test_extract_custom_headers_basic() {
        unsafe {
            let (msg, _cs, _hdrs) = build_fake_msg(&[
                ("X-CallUUID", "abc-123"),
                ("X-Custom-Header", "value1"),
                ("From", "sip:foo@bar.com"), // standard header, should be skipped
            ]);
            let result = extract_custom_headers(&*msg as *const _);
            assert_eq!(result.len(), 2);
            assert_eq!(result.get("X-CallUUID").unwrap(), "abc-123");
            assert_eq!(result.get("X-Custom-Header").unwrap(), "value1");
            assert!(!result.contains_key("From"));
        }
    }

    #[test]
    fn test_extract_custom_headers_empty() {
        unsafe {
            let (msg, _cs, _hdrs) = build_fake_msg(&[]);
            let result = extract_custom_headers(&*msg as *const _);
            assert!(result.is_empty());
        }
    }

    #[test]
    fn test_extract_custom_headers_null_msg() {
        unsafe {
            let result = extract_custom_headers(std::ptr::null());
            assert!(result.is_empty());
        }
    }

    #[test]
    fn test_extract_custom_headers_lowercase_x() {
        unsafe {
            let (msg, _cs, _hdrs) = build_fake_msg(&[
                ("x-lowercase", "val"),
                ("X-Uppercase", "val2"),
            ]);
            let result = extract_custom_headers(&*msg as *const _);
            assert_eq!(result.len(), 2);
            assert_eq!(result.get("x-lowercase").unwrap(), "val");
            assert_eq!(result.get("X-Uppercase").unwrap(), "val2");
        }
    }

    #[test]
    fn test_extract_headers_from_event_null() {
        unsafe {
            let result = extract_headers_from_event(std::ptr::null());
            assert!(result.is_empty());
        }
    }
}
