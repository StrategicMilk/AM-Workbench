use std::{
    net::SocketAddr,
    path::{Path, PathBuf},
    process,
    time::{SystemTime, UNIX_EPOCH},
};

use amw_engine::{
    api::{self, auth, ApiState},
    config::{CliOverrides, EngineConfig, LogLevel},
    runtime::EngineRuntime,
    telemetry::{
        logging::{write_crash_report, CrashReport, RotatingJsonLog},
        metrics::MetricsHub,
        TelemetryHub, TraceContext,
    },
    EXIT_CONFIG_BAD, EXIT_PORT_CONFLICT, EXIT_SUCCESS,
};
use clap::Parser;
use tokio::net::TcpListener;
use tracing::{error, info};
use tracing_subscriber::EnvFilter;

#[derive(Debug, Parser)]
#[command(
    name = "amw-engine-server",
    version,
    about = "Local AM Engine lifecycle host"
)]
struct Cli {
    #[arg(long)]
    config: PathBuf,
    #[arg(long)]
    host: Option<String>,
    #[arg(long)]
    port: Option<u16>,
    #[arg(long = "model-dir")]
    model_dirs: Vec<PathBuf>,
    #[arg(long)]
    device: Option<String>,
    #[arg(long)]
    log_level: Option<String>,
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();
    let log_level = match cli
        .log_level
        .as_deref()
        .map(str::parse::<LogLevel>)
        .transpose()
    {
        Ok(value) => value,
        Err(()) => {
            eprintln!("invalid --log-level; expected trace, debug, info, warn, or error");
            process::exit(EXIT_CONFIG_BAD.get());
        }
    };
    let overrides = CliOverrides {
        host: cli.host,
        port: cli.port,
        model_dirs: (!cli.model_dirs.is_empty()).then_some(cli.model_dirs),
        log_level,
    };
    let config = match EngineConfig::load(&cli.config, &overrides) {
        Ok(config) => config,
        Err(error) => {
            eprintln!("{error}");
            process::exit(EXIT_CONFIG_BAD.get());
        }
    };
    let structured_log = RotatingJsonLog::new(config.log.dir.join("engine.jsonl"));
    if tracing_subscriber::fmt()
        .json()
        .with_writer(structured_log.make_writer())
        .with_env_filter(EnvFilter::new(config.log.level.as_str()))
        .try_init()
        .is_err()
    {
        eprintln!("failed to initialize tracing subscriber");
        process::exit(EXIT_CONFIG_BAD.get());
    }

    let addresses = match auth::resolve_loopback(&config.server.host, config.server.port) {
        Ok(addresses) => addresses,
        Err(error) => {
            eprintln!("{}", error.body.message);
            process::exit(EXIT_CONFIG_BAD.get());
        }
    };
    let address = addresses[0];
    let listener = match TcpListener::bind(address).await {
        Ok(listener) => listener,
        Err(error) => {
            error!(%address, %error, "engine listener bind failed");
            process::exit(EXIT_PORT_CONFLICT.get());
        }
    };
    let auth_credentials = match auth::load_policy(&config.server.auth_policy_path) {
        Ok(credentials) => credentials,
        Err(error) => {
            eprintln!("failed to load engine authentication policy: {error}");
            process::exit(EXIT_CONFIG_BAD.get());
        }
    };
    let telemetry = TelemetryHub::default();
    install_crash_report_hook(config.log.dir.join("engine-crash.json"), telemetry.clone());
    let metrics = MetricsHub::default();
    let runtime = match EngineRuntime::new_for_server(config.clone(), telemetry, metrics) {
        Ok(runtime) => runtime,
        Err(error) => {
            error!(%error, "engine runtime initialization failed");
            process::exit(EXIT_CONFIG_BAD.get());
        }
    };
    info!(%address, device = ?cli.device, "engine API host ready");
    if let Err(error) = axum::serve(
        listener,
        api::router(ApiState::with_credentials(auth_credentials, runtime))
            .into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown_signal())
    .await
    {
        error!(%error, "engine API host failed");
        process::exit(EXIT_CONFIG_BAD.get());
    }
    info!("engine lifecycle host drained");
    process::exit(EXIT_SUCCESS.get());
}

fn install_crash_report_hook(path: PathBuf, telemetry: TelemetryHub) {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |panic_info| {
        let report = CrashReport::new(
            unix_timestamp(),
            "panic",
            "AM Engine terminated after an unrecoverable panic",
            TraceContext::default(),
            telemetry.try_recent_events(),
        );
        if let Err(error) = write_crash_report(Path::new(&path), &report) {
            tracing::error!(%error, "failed to write bounded engine crash report");
        }
        previous(panic_info);
    }));
}

fn unix_timestamp() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64())
}

async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate());
        match terminate {
            Ok(mut terminate) => {
                tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = terminate.recv() => {} }
            }
            Err(_) => {
                let _ = tokio::signal::ctrl_c().await;
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
