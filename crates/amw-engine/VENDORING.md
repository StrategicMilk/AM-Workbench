# Updating the vendored llama.cpp snapshot

The authoritative upstream is `https://github.com/ggml-org/llama.cpp.git`. The current
pin is the full commit SHA in `Cargo.toml` at
`package.metadata.am_engine.libllama_rev`; it must equal the imported tree revision.

From a clean pack worktree, update with:

```text
git subtree pull --prefix crates/amw-engine/vendor/llama.cpp https://github.com/ggml-org/llama.cpp.git <full-sha> --squash
```

For the first import, use the corresponding `git subtree add` command. Then update the
metadata SHA, inspect upstream LICENSE/NOTICE changes, refresh `NOTICE` if obligations
changed, build and test both the featureless workspace path and `--features cpu`, run
`cargo deny check`, and commit the subtree and metadata change together. Never use a
branch or moving tag as the metadata value, and never silently fall back to another
backend when configure, bindgen, or link fails.
