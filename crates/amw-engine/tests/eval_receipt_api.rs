use std::{fs, sync::Arc};

use amw_engine::{
    api::{self, ApiState},
    config::EngineConfig,
    receipt::{Digest32, ReceiptLedger, SoftwareTestSigner},
    runtime::{EngineRuntime, EvalReceiptAuthority, ReceiptRuntimeIdentity},
    telemetry::{metrics::MetricsHub, TelemetryHub},
};
use axum::{
    body::{to_bytes, Body},
    http::{Request, StatusCode},
};
use rusqlite::Connection;
use serde_json::{json, Value};
use tower::ServiceExt;

const TOKEN: &str = "eval-receipt-api-test-token";

fn runtime(temp: &tempfile::TempDir, with_test_authority: bool) -> EngineRuntime {
    let models = temp.path().join("models");
    fs::create_dir(&models).expect("create model root");
    let mut config = EngineConfig::default();
    config.models.dirs = vec![models];
    config.kv.session_dir = temp.path().join("sessions");
    let telemetry = TelemetryHub::default();
    let metrics = MetricsHub::default();
    if !with_test_authority {
        return EngineRuntime::new(config, telemetry, metrics).expect("create runtime");
    }
    let ledger = Arc::new(
        ReceiptLedger::open_for_test(temp.path().join("eval-receipts.sqlite3"))
            .expect("open receipt ledger"),
    );
    let signer =
        Arc::new(SoftwareTestSigner::from_secret_bytes([7; 32], 1).expect("create test signer"));
    let identity = ReceiptRuntimeIdentity {
        installation_id: "installation-test".to_owned(),
        anchor_sha256: Digest32::sha256(b"test-anchor"),
        authority_pin_sha256: Digest32::sha256(b"test-authority"),
        engine_release: "0.1.0-test".to_owned(),
        source_commit: "test-source-commit".to_owned(),
        libllama_revision: "test-libllama-revision".to_owned(),
        release_manifest_sha256: Digest32::sha256(b"test-release-manifest"),
        engine_binary_sha256: Digest32::sha256(b"test-engine-binary"),
    };
    let authority = EvalReceiptAuthority::new(
        ledger,
        signer,
        identity,
        "untrusted-contract-test".to_owned(),
    )
    .expect("create test receipt authority");
    EngineRuntime::new_with_receipt_authority(config, telemetry, metrics, authority)
        .expect("create runtime with receipt authority")
}

fn eval_body(stream: bool) -> Value {
    json!({
        "schema_version": 1,
        "model": "missing-model",
        "prompt": "hello",
        "max_tokens": 1,
        "seed": 42,
        "priority_class": "eval",
        "eval_slot": 0,
        "eval_context": {
            "schema_version": 1,
            "run_id": "run-a",
            "suite_id": "suite-a",
            "suite_revision_sha256": "a".repeat(64),
            "case_id": "case-a",
            "ordinal": 0,
            "case_spec_sha256": "b".repeat(64)
        },
        "stream": stream
    })
}

async fn post(
    runtime: EngineRuntime,
    path: &str,
    body: Value,
    request_id: &str,
) -> (StatusCode, Value) {
    let app = api::router(ApiState::new(TOKEN, runtime).expect("create API state"));
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(path)
                .header("authorization", format!("Bearer {TOKEN}"))
                .header("content-type", "application/json")
                .header("x-request-id", request_id)
                .body(Body::from(body.to_string()))
                .expect("build request"),
        )
        .await
        .expect("route response");
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 1024 * 1024)
        .await
        .expect("read response");
    let body = serde_json::from_slice(&bytes).expect("response is JSON");
    (status, body)
}

#[tokio::test]
async fn unprovisioned_eval_fails_with_explicit_receipt_error_before_model_lookup() {
    let temp = tempfile::tempdir().expect("temp directory");

    let (status, body) = post(
        runtime(&temp, false),
        "/v1/completions",
        eval_body(false),
        "req-a",
    )
    .await;

    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "eval_receipt_unavailable");
}

#[tokio::test]
async fn streaming_eval_is_rejected_before_reservation_or_generation() {
    let temp = tempfile::tempdir().expect("temp directory");

    let (status, body) = post(
        runtime(&temp, false),
        "/v1/completions",
        eval_body(true),
        "req-stream",
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(body["error"]["code"], "unsupported_param");
    assert!(body["error"]["message"]
        .as_str()
        .expect("error message")
        .contains("streaming EVAL"));
}

#[tokio::test]
async fn eval_session_restoration_is_rejected_before_reservation() {
    let temp = tempfile::tempdir().expect("temp directory");
    let mut body = eval_body(false);
    body["session_id"] = json!("prior-session");

    let (status, response) = post(
        runtime(&temp, false),
        "/v1/completions",
        body,
        "req-session",
    )
    .await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(response["error"]["code"], "unsupported_param");
    assert!(response["error"]["message"]
        .as_str()
        .expect("error message")
        .contains("session restoration"));
}

#[tokio::test]
async fn eval_prefix_references_are_rejected_before_reservation() {
    let temp = tempfile::tempdir().expect("temp directory");
    let mut body = eval_body(false);
    body["prefix_refs"] = json!([{
        "name": "governed-prefix",
        "content_hash": "a".repeat(64)
    }]);

    let (status, response) =
        post(runtime(&temp, false), "/v1/completions", body, "req-prefix").await;

    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(response["error"]["code"], "unsupported_param");
    assert!(response["error"]["message"]
        .as_str()
        .expect("error message")
        .contains("prefix references"));
}

#[tokio::test]
async fn failed_generation_consumes_attempt_before_model_resolution() {
    let temp = tempfile::tempdir().expect("temp directory");
    let runtime = runtime(&temp, true);

    let (first_status, first) = post(
        runtime.clone(),
        "/v1/completions",
        eval_body(false),
        "req-first",
    )
    .await;
    assert_eq!(first_status, StatusCode::NOT_FOUND);
    assert_eq!(first["error"]["code"], "model_not_loaded");

    let (second_status, second) =
        post(runtime, "/v1/completions", eval_body(false), "req-second").await;
    assert_eq!(second_status, StatusCode::CONFLICT);
    assert_eq!(second["error"]["code"], "eval_attempt_conflict");
}

#[tokio::test]
async fn software_test_authority_is_never_advertised_as_protected_trust() {
    let temp = tempfile::tempdir().expect("temp directory");
    let app = api::router(ApiState::new(TOKEN, runtime(&temp, true)).expect("create API state"));

    let response = app
        .oneshot(
            Request::builder()
                .uri("/version")
                .header("authorization", format!("Bearer {TOKEN}"))
                .body(Body::empty())
                .expect("build request"),
        )
        .await
        .expect("route response");
    let bytes = to_bytes(response.into_body(), 1024 * 1024)
        .await
        .expect("read response");
    let body: Value = serde_json::from_slice(&bytes).expect("response is JSON");

    assert!(body.get("receipt_trust").is_none());
}

#[tokio::test]
async fn readyz_fails_closed_after_post_start_receipt_ledger_tamper() {
    let temp = tempfile::tempdir().expect("temp directory");
    let runtime = runtime(&temp, true);
    let ledger_path = temp.path().join("eval-receipts.sqlite3");
    let tamper = Connection::open(&ledger_path).expect("open ledger for tamper simulation");
    tamper
        .execute_batch("DROP TRIGGER eval_receipt_no_delete;")
        .expect("simulate post-start trigger removal");
    drop(tamper);
    let app = api::router(ApiState::new(TOKEN, runtime).expect("create API state"));

    let response = app
        .oneshot(
            Request::builder()
                .uri("/readyz")
                .header("authorization", format!("Bearer {TOKEN}"))
                .body(Body::empty())
                .expect("build request"),
        )
        .await
        .expect("route response");
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    let bytes = to_bytes(response.into_body(), 1024 * 1024)
        .await
        .expect("read response");
    let body: Value = serde_json::from_slice(&bytes).expect("response is JSON");

    assert_eq!(body["control_ready"], false);
    assert!(body["reason_codes"]
        .as_array()
        .expect("reason codes")
        .contains(&json!("receipt_ledger_unready")));
}
