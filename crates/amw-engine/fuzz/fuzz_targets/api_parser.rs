#![no_main]

use amw_engine::api::dto::parse_completion;
use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    let _ = parse_completion(data);
});
