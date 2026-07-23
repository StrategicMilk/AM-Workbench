# Native kernel container. The retired Python web host is intentionally not the
# production entrypoint for this image.
FROM rust:1.88.0-slim-bookworm@sha256:38bc5a86d998772d4aec2348656ed21438d20fcdce2795b56ca434cf21430d89 AS builder

WORKDIR /src
COPY Cargo.toml Cargo.toml
COPY crates/ crates/
RUN cargo build --release -p amw-kernel --bin amw-kernel-server

FROM debian:bookworm-slim@sha256:60eac759739651111db372c07be67863818726f754804b8707c90979bda511df

LABEL org.vetinari.wave21.finding.FSA-0681="native-kernel-container-state-dir"

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/target/release/amw-kernel-server /usr/local/bin/amw-kernel-server
COPY config/ config/

EXPOSE 8080
ENV AMW_KERNEL_HOST=0.0.0.0
ENV AMW_KERNEL_PORT=8080
ENV AMW_KERNEL_STATE_DIR=/app/data
# Container port publishing requires a non-loopback bind; the kernel refuses
# that bind unless this explicit override is present.
ENV AMW_KERNEL_ALLOW_INSECURE_REMOTE_BIND=true
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${AMW_KERNEL_PORT}/ready" >/dev/null

CMD ["sh", "-c", "exec amw-kernel-server --host \"$AMW_KERNEL_HOST\" --port \"$AMW_KERNEL_PORT\""]
