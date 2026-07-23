//! Deterministic capacity coverage for the bounded telemetry data path.

use std::{collections::BTreeSet, hint::black_box, process::Command};

use amw_engine::{
    runtime::WorkloadRole,
    telemetry::{
        events::{EngineEvent, EventEnvelope},
        metrics::{MetricsHub, RequestMetrics, THROUGHPUT_WINDOW_REQUESTS},
        ring::EventCursor,
        SubscriptionItem, SubscriptionStart, TelemetryHub,
    },
};
use tokio::sync::mpsc;

const RING_CAPACITY: usize = 64;
const BURST_SIZE: usize = RING_CAPACITY;
const BURSTS: usize = 128;
const PRODUCED: usize = BURST_SIZE * BURSTS;
const MEMORY_PROBE_ENV: &str = "AMW_ENGINE_TELEMETRY_MEMORY_PROBE";
const MEMORY_RESULT_PREFIX: &str = "AMW_TELEMETRY_MEMORY_RESULT=";
const MEMORY_WARMUP_EVENTS: usize = 8 * 1024;
const MEMORY_MEASURED_BURSTS: usize = 32;
const MEMORY_MEASURED_BURST_SIZE: usize = 4 * 1024;
const MEMORY_RETENTION_CEILING_BYTES: u64 = 16 * 1024 * 1024;
const CONTROL_ALLOCATION_BYTES: usize = 32 * 1024 * 1024;
const CONTROL_MINIMUM_OBSERVED_BYTES: u64 = 24 * 1024 * 1024;

fn request_metrics() -> RequestMetrics {
    RequestMetrics {
        queue_ms: 1.0,
        prefill_ms: 2.0,
        decode_ms: 4.0,
        prompt_tokens: 8,
        completion_tokens: 4,
        tokens_per_second: 1_000.0,
        prefix_hit_tokens: 0,
        speculation_proposed_tokens: 0,
        speculation_accepted_tokens: 0,
        speculation_acceptance_rate: None,
    }
}

fn terminal_event(sequence: usize, role: WorkloadRole) -> EventEnvelope {
    EventEnvelope::new(
        sequence as f64,
        EngineEvent::RequestComplete {
            request_id: format!("soak-{sequence:05}"),
            trace_id: format!("trace-{sequence:05}"),
            model_id: "capacity-fixture".to_owned(),
            queue_ms: 1.0,
            prefill_ms: 2.0,
            decode_ms: 4.0,
            input_tokens: 8,
            output_tokens: 4,
            tok_per_s: 1_000.0,
            prefix_hit_tokens: 0,
            speculation_proposed_tokens: 0,
            speculation_accepted_tokens: 0,
            spec_accept_rate: None,
            priority_class: role.as_str().to_owned(),
            eval_slot: sequence % 4,
        },
    )
}

#[derive(Default)]
struct ConsumerAccounting {
    delivered: u64,
    missed: u64,
    delivered_cursors: BTreeSet<u64>,
}

impl ConsumerAccounting {
    fn observe(&mut self, item: SubscriptionItem) -> Result<(), &'static str> {
        match item {
            SubscriptionItem::Event(event) => {
                assert!(
                    self.delivered_cursors.insert(event.cursor.get()),
                    "a terminal event must not be delivered more than once"
                );
                self.delivered += 1;
            }
            SubscriptionItem::Lagged { missed, .. } => self.missed += missed,
            SubscriptionItem::Closed => return Err("the live producer must keep telemetry open"),
        }
        Ok(())
    }
}

#[derive(Debug)]
struct ResidentMemoryProbe {
    system: sysinfo::System,
    pid: sysinfo::Pid,
}

impl ResidentMemoryProbe {
    fn current_process() -> Self {
        let pid = sysinfo::Pid::from_u32(std::process::id());
        let mut probe = Self {
            system: sysinfo::System::new(),
            pid,
        };
        assert!(
            probe.sample() > 0,
            "the platform process sampler must report non-zero resident memory"
        );
        probe
    }

    fn sample(&mut self) -> u64 {
        self.system.refresh_processes_specifics(
            sysinfo::ProcessesToUpdate::Some(&[self.pid]),
            true,
            sysinfo::ProcessRefreshKind::nothing().with_memory(),
        );
        self.system
            .process(self.pid)
            .expect("the probe subprocess must remain visible to the process sampler")
            .memory()
    }
}

#[derive(Clone, Copy, Debug)]
struct MemoryMeasurement {
    baseline_bytes: u64,
    maximum_retained_bytes: u64,
}

impl MemoryMeasurement {
    fn retained_delta(self) -> u64 {
        self.maximum_retained_bytes
            .saturating_sub(self.baseline_bytes)
    }

    fn encode(self) -> String {
        format!("{},{}", self.baseline_bytes, self.maximum_retained_bytes)
    }

    fn decode(value: &str) -> Self {
        let (baseline, maximum) = value
            .trim()
            .split_once(',')
            .expect("memory probe output must contain baseline and maximum RSS");
        Self {
            baseline_bytes: baseline.parse().expect("baseline RSS must be an integer"),
            maximum_retained_bytes: maximum.parse().expect("maximum RSS must be an integer"),
        }
    }
}

fn record_terminal(hub: &TelemetryHub, metrics: &MetricsHub, sequence: usize) {
    let roles = [
        WorkloadRole::Foreman,
        WorkloadRole::Worker,
        WorkloadRole::Inspector,
        WorkloadRole::Unknown,
    ];
    let role = roles[(sequence - 1) % roles.len()];
    metrics
        .record_admission(sequence as u64)
        .expect("every generated request has one admission");
    metrics
        .record_success(sequence as u64, role.as_str(), &request_metrics())
        .expect("every admission has one terminal outcome");
    hub.emit(terminal_event(sequence, role))
        .expect("the generated terminal event is valid");
}

fn measure_telemetry_soak() -> MemoryMeasurement {
    let hub = TelemetryHub::with_capacity(RING_CAPACITY);
    let metrics = MetricsHub::default();
    for sequence in 1..=MEMORY_WARMUP_EVENTS {
        record_terminal(&hub, &metrics, sequence);
    }

    // Construct and prime the sampler only after allocator and telemetry one-time
    // initialization. Every later sample is taken at a burst boundary, when the
    // only live workload state should be the bounded ring and metrics aggregates.
    let mut resident_memory = ResidentMemoryProbe::current_process();
    let baseline_bytes = resident_memory.sample();
    let mut maximum_retained_bytes = baseline_bytes;

    for burst in 0..MEMORY_MEASURED_BURSTS {
        let start = MEMORY_WARMUP_EVENTS + burst * MEMORY_MEASURED_BURST_SIZE + 1;
        let end = start + MEMORY_MEASURED_BURST_SIZE;
        for sequence in start..end {
            record_terminal(&hub, &metrics, sequence);
        }
        maximum_retained_bytes = maximum_retained_bytes.max(resident_memory.sample());
    }

    let produced = MEMORY_WARMUP_EVENTS + MEMORY_MEASURED_BURSTS * MEMORY_MEASURED_BURST_SIZE;
    assert_eq!(hub.len(), RING_CAPACITY);
    assert_eq!(hub.dropped(), (produced - RING_CAPACITY) as u64);
    let snapshot = metrics.snapshot();
    assert_eq!(snapshot.admitted_requests, produced as u64);
    assert_eq!(snapshot.succeeded_requests, produced as u64);
    assert_eq!(snapshot.in_flight_requests, 0);
    assert_eq!(
        snapshot.throughput_window_requests,
        THROUGHPUT_WINDOW_REQUESTS
    );
    assert_eq!(snapshot.per_role.len(), 4);

    MemoryMeasurement {
        baseline_bytes,
        maximum_retained_bytes,
    }
}

fn measure_retained_control_allocation() -> MemoryMeasurement {
    let mut resident_memory = ResidentMemoryProbe::current_process();
    let baseline_bytes = resident_memory.sample();
    let mut retained = vec![0_u8; CONTROL_ALLOCATION_BYTES];

    // Vec's zeroed allocation can be demand-paged. Touch every 4 KiB page so RSS,
    // rather than virtual address space, must reflect the deliberately retained bytes.
    for offset in (0..retained.len()).step_by(4 * 1024) {
        retained[offset] = 1;
    }
    retained[CONTROL_ALLOCATION_BYTES - 1] = 1;
    black_box(&retained);
    let maximum_retained_bytes = resident_memory.sample();
    black_box(retained);

    MemoryMeasurement {
        baseline_bytes,
        maximum_retained_bytes,
    }
}

fn run_memory_probe(mode: &str) -> MemoryMeasurement {
    let output = Command::new(std::env::current_exe().expect("test executable path is available"))
        .args([
            "--ignored",
            "--exact",
            "telemetry_memory_probe_subprocess",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(MEMORY_PROBE_ENV, mode)
        .output()
        .expect("memory probe subprocess must launch");
    assert!(
        output.status.success(),
        "memory probe subprocess failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    String::from_utf8(output.stdout)
        .expect("memory probe output must be UTF-8")
        .lines()
        .find_map(|line| {
            line.find(MEMORY_RESULT_PREFIX)
                .map(|offset| &line[offset + MEMORY_RESULT_PREFIX.len()..])
        })
        .map(MemoryMeasurement::decode)
        .expect("memory probe subprocess must emit a measurement")
}

#[test]
fn sustained_terminal_load_has_a_measured_resident_memory_ceiling() {
    let soak = run_memory_probe("telemetry-soak");
    let control = run_memory_probe("retained-control");

    assert!(
        soak.retained_delta() <= MEMORY_RETENTION_CEILING_BYTES,
        "bounded telemetry retained {} bytes above its post-warmup RSS baseline; ceiling is {} bytes",
        soak.retained_delta(),
        MEMORY_RETENTION_CEILING_BYTES
    );
    assert!(
        control.retained_delta() >= CONTROL_MINIMUM_OBSERVED_BYTES,
        "RSS sampler observed only {} of the {}-byte retained control allocation",
        control.retained_delta(),
        CONTROL_ALLOCATION_BYTES
    );
    assert!(
        control.retained_delta() > MEMORY_RETENTION_CEILING_BYTES,
        "the retained-allocation control must violate the telemetry ceiling"
    );
}

#[test]
#[ignore = "launched by sustained_terminal_load_has_a_measured_resident_memory_ceiling"]
fn telemetry_memory_probe_subprocess() {
    let mode = std::env::var(MEMORY_PROBE_ENV).expect("probe mode is supplied by the parent test");
    let measurement = if mode == "telemetry-soak" {
        measure_telemetry_soak()
    } else {
        assert_eq!(
            mode, "retained-control",
            "the parent supplied an unknown memory probe mode"
        );
        measure_retained_control_allocation()
    };
    println!("{MEMORY_RESULT_PREFIX}{}", measurement.encode());
}

#[tokio::test]
async fn sustained_terminal_load_stays_bounded_accounts_lag_and_recovers() {
    let hub = TelemetryHub::with_capacity(RING_CAPACITY);
    let metrics = MetricsHub::default();
    let mut subscription = hub
        .subscribe(SubscriptionStart::After(EventCursor::new(0)))
        .expect("the initial cursor is valid");
    let (burst_ready_tx, mut burst_ready_rx) = mpsc::channel::<()>(1);
    let (consumer_ack_tx, mut consumer_ack_rx) = mpsc::channel::<()>(1);

    let producer_hub = hub.clone();
    let producer_metrics = metrics.clone();
    let producer = tokio::spawn(async move {
        let roles = [
            WorkloadRole::Foreman,
            WorkloadRole::Worker,
            WorkloadRole::Inspector,
            WorkloadRole::Unknown,
        ];
        let request_metrics = request_metrics();
        let mut peak_retained_wire_bytes = 0;
        let mut largest_event_wire_bytes = 0;

        for burst in 0..BURSTS {
            for offset in 0..BURST_SIZE {
                let sequence = burst * BURST_SIZE + offset + 1;
                let role = roles[(sequence - 1) % roles.len()];
                producer_metrics
                    .record_admission(sequence as u64)
                    .expect("every generated request has one admission");
                producer_metrics
                    .record_success(sequence as u64, role.as_str(), &request_metrics)
                    .expect("every admission has one terminal outcome");

                let event = terminal_event(sequence, role);
                largest_event_wire_bytes =
                    largest_event_wire_bytes.max(event.to_ndjson().unwrap().len());
                assert_eq!(
                    producer_hub.emit(event).unwrap(),
                    EventCursor::new(sequence as u64)
                );
            }

            let retained_wire_bytes = producer_hub.ndjson_snapshot().len();
            peak_retained_wire_bytes = peak_retained_wire_bytes.max(retained_wire_bytes);
            assert_eq!(producer_hub.len(), RING_CAPACITY);
            assert!(
                retained_wire_bytes <= RING_CAPACITY * largest_event_wire_bytes,
                "retained payload bytes must remain bounded by the fixed ring"
            );

            burst_ready_tx.send(()).await.unwrap();
            consumer_ack_rx.recv().await.unwrap();
        }

        (peak_retained_wire_bytes, largest_event_wire_bytes)
    });

    // Deliberately consume only one item per full-capacity producer burst. The
    // handshake makes the lag deterministic without wall-clock sleeps.
    let consumer = tokio::spawn(async move {
        let mut accounting = ConsumerAccounting::default();
        for _ in 0..BURSTS {
            burst_ready_rx.recv().await.unwrap();
            accounting
                .observe(subscription.recv().await)
                .expect("the consumer must remain live during the producer soak");
            consumer_ack_tx.send(()).await.unwrap();
        }
        (subscription, accounting)
    });

    let (peak_retained_wire_bytes, largest_event_wire_bytes) = producer.await.unwrap();
    let (mut subscription, mut accounting) = consumer.await.unwrap();

    while subscription.cursor() < EventCursor::new(PRODUCED as u64) {
        accounting
            .observe(subscription.recv().await)
            .expect("the retained terminal backlog must remain replayable");
    }

    assert_eq!(accounting.missed + accounting.delivered, PRODUCED as u64);
    assert_eq!(
        accounting.delivered as usize,
        accounting.delivered_cursors.len(),
        "every retained terminal event is delivered exactly once"
    );
    assert!(
        accounting.delivered_cursors.contains(&(PRODUCED as u64)),
        "the final terminal outcome must survive overload and be delivered"
    );
    assert_eq!(hub.len(), RING_CAPACITY);
    assert_eq!(hub.dropped(), (PRODUCED - RING_CAPACITY) as u64);
    assert!(peak_retained_wire_bytes <= RING_CAPACITY * largest_event_wire_bytes);

    let snapshot = metrics.snapshot();
    assert_eq!(snapshot.admitted_requests, PRODUCED as u64);
    assert_eq!(snapshot.succeeded_requests, PRODUCED as u64);
    assert_eq!(snapshot.in_flight_requests, 0);
    assert_eq!(
        snapshot.throughput_window_requests,
        THROUGHPUT_WINDOW_REQUESTS
    );
    assert_eq!(
        snapshot
            .per_role
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["foreman", "inspector", "unknown", "worker"],
        "sustained traffic must not create attacker-controlled metric labels"
    );

    // Prove the live path recovers after overload rather than remaining stuck in
    // a lag state. No sleep or timeout is necessary because the event is emitted
    // only after the consumer has reached the producer's terminal cursor.
    let recovery_sequence = PRODUCED + 1;
    metrics.record_admission(recovery_sequence as u64).unwrap();
    metrics
        .record_success(
            recovery_sequence as u64,
            WorkloadRole::Worker.as_str(),
            &request_metrics(),
        )
        .unwrap();
    hub.emit(terminal_event(recovery_sequence, WorkloadRole::Worker))
        .unwrap();

    let recovered = subscription.recv().await;
    assert!(matches!(
        recovered,
        SubscriptionItem::Event(ref item)
            if item.cursor == EventCursor::new(recovery_sequence as u64)
    ));
    assert_eq!(hub.len(), RING_CAPACITY);
    assert_eq!(hub.dropped(), (PRODUCED - RING_CAPACITY + 1) as u64);
    let recovered_metrics = metrics.snapshot();
    assert_eq!(
        recovered_metrics.succeeded_requests,
        recovery_sequence as u64
    );
    assert_eq!(recovered_metrics.in_flight_requests, 0);
    assert_eq!(recovered_metrics.per_role.len(), 4);
}
