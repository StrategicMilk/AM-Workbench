#![no_main]

use amw_engine::gen::CompiledGrammar;
use libfuzzer_sys::fuzz_target;

#[cfg(feature = "native-grammar")]
thread_local! {
    static MODEL: std::cell::RefCell<Option<amw_engine::ffi::Model>> = const {
        std::cell::RefCell::new(None)
    };
}

fuzz_target!(|data: &[u8]| {
    if let Ok(source) = std::str::from_utf8(data) {
        if let Ok(grammar) = CompiledGrammar::compile(source) {
            let _ = &grammar;
            #[cfg(feature = "native-grammar")]
            if let Some(model_path) = std::env::var_os("AMW_GBNF_FUZZ_MODEL") {
                MODEL.with(|slot| {
                    let mut slot = slot.borrow_mut();
                    if slot.is_none() {
                        *slot =
                            amw_engine::ffi::Model::load(std::path::Path::new(&model_path)).ok();
                    }
                    if let Some(model) = slot.as_ref() {
                        let _ = grammar.activate(model);
                    }
                });
            }
        }
    }
});
