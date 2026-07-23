use std::{
    env, io,
    path::{Path, PathBuf},
};

fn first_existing_directory(candidates: impl IntoIterator<Item = PathBuf>) -> Option<PathBuf> {
    candidates.into_iter().find(|path| path.is_dir())
}

fn cuda_toolkit_root() -> Result<PathBuf, io::Error> {
    let configured = ["CUDAToolkit_ROOT", "CUDA_PATH", "CUDA_HOME"]
        .into_iter()
        .filter_map(env::var_os)
        .map(PathBuf::from);
    let defaults = [PathBuf::from("/usr/local/cuda")];
    first_existing_directory(configured.chain(defaults)).ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotFound,
            "the cuda feature requires CUDAToolkit_ROOT, CUDA_PATH, or CUDA_HOME to identify an installed CUDA toolkit",
        )
    })
}

fn emit_cuda_link_directives(toolkit: &Path, target_os: &str) -> Result<(), io::Error> {
    let library_candidates = if target_os == "windows" {
        vec![toolkit.join("lib/x64")]
    } else {
        vec![
            toolkit.join("lib64"),
            toolkit.join("targets/x86_64-linux/lib"),
            toolkit.join("lib64/stubs"),
            toolkit.join("targets/x86_64-linux/lib/stubs"),
        ]
    };
    let existing: Vec<_> = library_candidates
        .into_iter()
        .filter(|path| path.is_dir())
        .collect();
    if existing.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!(
                "CUDA toolkit at {} has no supported library directory",
                toolkit.display()
            ),
        ));
    }
    for path in existing {
        println!("cargo:rustc-link-search=native={}", path.display());
    }
    println!("cargo:rustc-link-lib=static=ggml-cuda");
    for library in ["cublas", "cublasLt", "cudart", "cuda"] {
        println!("cargo:rustc-link-lib=dylib={library}");
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("cargo:rerun-if-env-changed=CARGO_FEATURE_CPU");
    println!("cargo:rerun-if-env-changed=CARGO_FEATURE_CUDA");
    println!("cargo:rerun-if-env-changed=CUDAToolkit_ROOT");
    println!("cargo:rerun-if-env-changed=CUDA_PATH");
    println!("cargo:rerun-if-env-changed=CUDA_HOME");

    let cpu = env::var_os("CARGO_FEATURE_CPU").is_some();
    let cuda = env::var_os("CARGO_FEATURE_CUDA").is_some();
    if !cpu && !cuda {
        return Ok(());
    }

    let vendor = PathBuf::from("vendor/llama.cpp");
    let header = vendor.join("include/llama.h");
    let ggml_include = vendor.join("ggml/include");
    let firewall_header = PathBuf::from("src/ffi/exception_firewall.h");
    let firewall_source = PathBuf::from("src/ffi/exception_firewall.cpp");
    if !header.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            format!(
                "amw-engine FFI build requires vendored llama.cpp header at {}",
                header.display()
            ),
        )
        .into());
    }

    println!("cargo:rerun-if-changed={}", header.display());
    println!("cargo:rerun-if-changed={}", firewall_header.display());
    println!("cargo:rerun-if-changed={}", firewall_source.display());
    println!(
        "cargo:rerun-if-changed={}",
        vendor.join("CMakeLists.txt").display()
    );

    let mut config = cmake::Config::new(&vendor);
    config
        .profile("Release")
        .define("BUILD_SHARED_LIBS", "OFF")
        .define("GGML_NATIVE", "OFF")
        .define("GGML_OPENMP", "OFF")
        .define("LLAMA_BUILD_COMMON", "OFF")
        .define("LLAMA_BUILD_TESTS", "OFF")
        .define("LLAMA_BUILD_TOOLS", "OFF")
        .define("LLAMA_BUILD_EXAMPLES", "OFF")
        .define("LLAMA_BUILD_SERVER", "OFF")
        .define("LLAMA_BUILD_APP", "OFF")
        .define("LLAMA_BUILD_UI", "OFF")
        .define("LLAMA_CURL", "OFF")
        .define("GGML_CUDA", if cuda { "ON" } else { "OFF" });
    let dst = config.build();

    cc::Build::new()
        .cpp(true)
        .file(&firewall_source)
        .include(vendor.join("include"))
        .include(&ggml_include)
        .flag_if_supported("/EHsc")
        .flag_if_supported("-std=c++17")
        .warnings(true)
        .compile("amw_ffi_exception_firewall");

    println!(
        "cargo:rustc-link-search=native={}",
        dst.join("lib").display()
    );
    for library in ["llama", "ggml", "ggml-base", "ggml-cpu"] {
        println!("cargo:rustc-link-lib=static={library}");
    }
    let target_os = env::var("CARGO_CFG_TARGET_OS").map_err(|error| {
        io::Error::new(
            io::ErrorKind::NotFound,
            format!("Cargo did not set CARGO_CFG_TARGET_OS: {error}"),
        )
    })?;
    if cuda {
        emit_cuda_link_directives(&cuda_toolkit_root()?, &target_os)?;
    }
    if target_os == "windows" {
        println!("cargo:rustc-link-lib=dylib=advapi32");
    }

    let bindings = bindgen::Builder::default()
        .header(header.to_string_lossy())
        .header(firewall_header.to_string_lossy())
        .clang_arg(format!("-I{}", vendor.join("include").display()))
        .clang_arg(format!("-I{}", ggml_include.display()))
        .allowlist_function("llama_backend_init")
        .allowlist_function("llama_model_default_params")
        .allowlist_function("llama_model_free")
        .allowlist_function("llama_adapter_lora_free")
        .allowlist_function("llama_context_default_params")
        .allowlist_function("llama_free")
        .allowlist_function("llama_model_get_vocab")
        .allowlist_function("llama_model_n_ctx_train")
        .allowlist_function("llama_model_n_embd")
        .allowlist_function("llama_model_n_embd_out")
        .allowlist_function("llama_vocab_n_tokens")
        .allowlist_function("llama_vocab_is_eog")
        .allowlist_function("llama_vocab_is_control")
        .allowlist_function("llama_vocab_bos")
        .allowlist_function("llama_vocab_eos")
        .allowlist_function("llama_vocab_fim_pre")
        .allowlist_function("llama_vocab_fim_suf")
        .allowlist_function("llama_vocab_fim_mid")
        .allowlist_function("llama_vocab_fim_pad")
        .allowlist_function("llama_vocab_fim_rep")
        .allowlist_function("llama_vocab_fim_sep")
        .allowlist_function("amw_ffi_.*")
        .allowlist_function("llama_batch_init")
        .allowlist_function("llama_batch_free")
        .allowlist_function("llama_n_ctx")
        .allowlist_function("llama_n_batch")
        .allowlist_function("llama_n_ubatch")
        .allowlist_function("llama_n_seq_max")
        .allowlist_function("llama_get_logits_ith")
        .allowlist_function("llama_get_embeddings_ith")
        .allowlist_function("llama_get_embeddings_seq")
        .allowlist_function("llama_pooling_type")
        .allowlist_function("llama_get_memory")
        .allowlist_function("llama_sampler_chain_default_params")
        .allowlist_function("llama_sampler_free")
        .allowlist_type("llama_model")
        .allowlist_type("llama_adapter_lora")
        .allowlist_type("llama_context")
        .allowlist_type("llama_memory_i")
        .allowlist_type("llama_memory_t")
        .allowlist_type("llama_pos")
        .allowlist_type("llama_seq_id")
        .allowlist_type("llama_token")
        .allowlist_type("llama_sampler")
        .allowlist_type("llama_logit_bias")
        .allowlist_type("llama_sampler_chain_params")
        .allowlist_type("llama_vocab")
        .allowlist_type("llama_model_params")
        .allowlist_type("llama_model_kv_override")
        .allowlist_type("llama_model_kv_override_type")
        .allowlist_type("llama_model_tensor_buft_override")
        .allowlist_type("llama_split_mode")
        .allowlist_type("llama_progress_callback")
        .allowlist_type("ggml_backend_dev_t")
        .allowlist_type("ggml_backend_device")
        .allowlist_type("ggml_backend_buffer_type_t")
        .allowlist_type("ggml_backend_buffer_type")
        .allowlist_type("llama_context_params")
        .allowlist_type("llama_context_type")
        .allowlist_type("llama_rope_scaling_type")
        .allowlist_type("llama_pooling_type")
        .allowlist_type("llama_attention_type")
        .allowlist_type("llama_flash_attn_type")
        .allowlist_type("llama_sampler_seq_config")
        .allowlist_type("ggml_backend_sched_eval_callback")
        .allowlist_type("ggml_abort_callback")
        .allowlist_type("ggml_type")
        .allowlist_type("ggml_tensor")
        .allowlist_type("llama_batch")
        .allowlist_type("amw_ffi_exception_status")
        .allowlist_type("amw_ffi_vocab_identity")
        .opaque_type("llama_model")
        .opaque_type("llama_adapter_lora")
        .opaque_type("llama_context")
        .opaque_type("llama_memory_i")
        .opaque_type("llama_sampler")
        .opaque_type("llama_vocab")
        .opaque_type("ggml_tensor")
        .allowlist_recursively(false)
        .derive_default(true)
        .generate_comments(false)
        .generate()
        .map_err(|error| {
            io::Error::other(format!("failed to generate llama.cpp bindings: {error}"))
        })?;
    let output = PathBuf::from(
        env::var_os("OUT_DIR")
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "Cargo did not set OUT_DIR"))?,
    );
    bindings.write_to_file(output.join("llama_bindings.rs"))?;
    Ok(())
}
