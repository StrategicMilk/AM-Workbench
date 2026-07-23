use amw_engine::{
    gen::{
        resolve_draft_mode, DraftJob, DraftMode, DraftModelBackend, DraftModelCompatibility,
        DraftModelProposer, DraftProposal, DraftResult, GenError, ProbabilityRow,
        PromptLookupProposer, ProposalSource, ProposedToken, SpeculationEligibility,
        SpeculationIneligibleReason, SpeculationPlan, TargetProbe, TargetVerification,
        TokenProbability,
    },
    store::registry::{DraftPair, ModelRecord},
};

fn record() -> ModelRecord {
    ModelRecord {
        id: "target".into(),
        path: "target.gguf".into(),
        aliases: Vec::new(),
        draft_pair: None,
    }
}

fn row(probabilities: &[(i32, f32)]) -> ProbabilityRow {
    ProbabilityRow::new(
        4,
        probabilities
            .iter()
            .map(|(token_id, probability)| TokenProbability {
                token_id: *token_id,
                probability: *probability,
            })
            .collect(),
    )
    .unwrap()
}

fn proposed(token_id: i32, probabilities: &[(i32, f32)]) -> ProposedToken {
    ProposedToken {
        token_id,
        draft: row(probabilities),
    }
}

fn probe(selected_token: i32, probabilities: &[(i32, f32)]) -> TargetProbe {
    TargetProbe {
        selected_token,
        distribution: row(probabilities),
    }
}

#[test]
fn probability_rows_are_canonical_normalized_and_unique() {
    let canonical = row(&[(2, 0.25), (0, 0.75)]);
    assert_eq!(canonical.candidates()[0].token_id, 0);
    assert_eq!(canonical.probability(2), 0.25);
    assert_eq!(canonical.probability(3), 0.0);

    assert!(ProbabilityRow::new(
        4,
        vec![
            TokenProbability {
                token_id: 1,
                probability: 0.5,
            },
            TokenProbability {
                token_id: 1,
                probability: 0.5,
            },
        ],
    )
    .is_err());
    assert!(ProbabilityRow::new(
        4,
        vec![TokenProbability {
            token_id: 0,
            probability: 0.8,
        }],
    )
    .is_err());
}

#[test]
fn eligibility_leaves_one_output_and_context_token_for_the_final_sample() {
    let bounded = SpeculationEligibility::for_request_with_limits(
        &record(),
        60,
        5,
        64,
        9,
        std::mem::size_of::<TokenProbability>() * 4,
    );
    assert!(bounded.eligible);
    assert_eq!(bounded.maximum_budget, 3);

    let one_output = SpeculationEligibility::for_request_with_limits(
        &record(),
        1,
        1,
        64,
        9,
        std::mem::size_of::<TokenProbability>() * 4,
    );
    assert_eq!(
        one_output.reason,
        Some(SpeculationIneligibleReason::InsufficientRemainingTokens)
    );
}

#[test]
fn prompt_lookup_jobs_are_versioned_and_stale_results_fail_closed() {
    let job = DraftJob::new(3, 9, &[1, 2, 3, 1, 2], 3, 4).unwrap();
    let mut proposer = PromptLookupProposer::default();
    let result = proposer.propose(&job).unwrap();
    assert_eq!(result.proposal.mode, DraftMode::PromptLookup);
    assert_eq!(result.proposal.token_ids(), vec![3, 1, 2]);

    let stale = DraftResult {
        version: 8,
        ..result
    };
    assert!(job.validate_result(&stale).is_err());
}

#[test]
fn optimistic_draft_result_reuses_only_the_bonus_conditioned_tail() {
    let job = DraftJob::new(3, 10, &[1, 2], 3, 4).unwrap();
    let result = DraftResult::new(
        &job,
        DraftProposal::new(
            DraftMode::DraftModel("draft".into()),
            vec![
                proposed(2, &[(2, 1.0)]),
                proposed(3, &[(3, 1.0)]),
                proposed(1, &[(1, 1.0)]),
            ],
        )
        .unwrap(),
    )
    .unwrap();

    let reconciled = result.reconcile_optimistic_bonus(2).unwrap().unwrap();
    assert_eq!(reconciled.sequence_id, 3);
    assert_eq!(reconciled.version, 10);
    assert_eq!(reconciled.proposal.token_ids(), vec![3, 1]);
    assert!(result.reconcile_optimistic_bonus(0).unwrap().is_none());

    let single = DraftResult::new(
        &job,
        DraftProposal::new(
            DraftMode::DraftModel("draft".into()),
            vec![proposed(2, &[(2, 1.0)])],
        )
        .unwrap(),
    )
    .unwrap();
    assert!(single.reconcile_optimistic_bonus(2).unwrap().is_none());
}

struct CountingBackend(usize);

impl DraftModelBackend for CountingBackend {
    fn propose_tokens(&mut self, _job: &DraftJob) -> Result<Vec<ProposedToken>, GenError> {
        self.0 += 1;
        Ok(Vec::new())
    }
}

#[test]
fn draft_actor_owner_can_configure_or_remove_through_mutable_backend_access() {
    let mut proposer = DraftModelProposer::new("draft".into(), CountingBackend(0)).unwrap();
    proposer.backend_mut().0 = 4;
    assert_eq!(proposer.backend().0, 4);
}

#[test]
fn configured_draft_pair_requires_native_semantic_fingerprint_and_context_match() {
    let fingerprint = "a".repeat(64);
    let target = ModelRecord {
        draft_pair: Some(DraftPair {
            draft_model_id: "draft".into(),
            minimum_context: Some(128),
            vocabulary_fingerprint: Some(fingerprint.clone()),
        }),
        ..record()
    };
    let compatible = DraftModelCompatibility {
        model_id: "draft".into(),
        vocabulary_fingerprint: fingerprint.clone(),
        context_capacity: 256,
    };
    assert_eq!(
        resolve_draft_mode(&target, &fingerprint, 256, Some(&compatible)).unwrap(),
        DraftMode::DraftModel("draft".into())
    );
    assert!(resolve_draft_mode(&target, &"b".repeat(64), 256, Some(&compatible)).is_err());
    assert!(resolve_draft_mode(&target, &fingerprint, 256, None).is_err());
    assert_eq!(
        resolve_draft_mode(&record(), &fingerprint, 256, None).unwrap(),
        DraftMode::PromptLookup
    );
}

#[test]
fn biased_rejection_samples_exact_positive_target_minus_draft_residual() {
    let proposal = DraftProposal::new(
        DraftMode::DraftModel("draft".into()),
        vec![proposed(0, &[(0, 0.9), (1, 0.1)])],
    )
    .unwrap();
    let verification = TargetVerification::new(
        &proposal,
        vec![
            probe(1, &[(0, 0.1), (1, 0.9)]),
            probe(1, &[(0, 0.1), (1, 0.9)]),
        ],
    )
    .unwrap();
    let plan = SpeculationPlan::new(DraftMode::DraftModel("draft".into()), 7).unwrap();
    let decision = plan.decide(&proposal, &verification).unwrap();
    assert_eq!(decision.accepted, 0);
    assert!(decision.rejected);
    assert!(decision.kv_tokens.is_empty());
    assert_eq!(decision.pending_token, 1);
}

#[test]
fn all_accepted_draft_tokens_leave_the_bonus_pending_outside_kv() {
    let proposal = DraftProposal::new(
        DraftMode::DraftModel("draft".into()),
        vec![proposed(0, &[(0, 1.0)]), proposed(1, &[(1, 1.0)])],
    )
    .unwrap();
    let verification = TargetVerification::new(
        &proposal,
        vec![
            probe(0, &[(0, 1.0)]),
            probe(1, &[(1, 1.0)]),
            probe(2, &[(2, 1.0)]),
        ],
    )
    .unwrap();
    let mut plan = SpeculationPlan::new(DraftMode::DraftModel("draft".into()), 1).unwrap();
    let decision = plan.decide(&proposal, &verification).unwrap();
    assert_eq!(decision.accepted, 2);
    assert_eq!(decision.kv_tokens, vec![0, 1]);
    assert_eq!(decision.pending_token, 2);
    assert!(!decision.rejected);

    plan.record_commit(&decision).unwrap();
    assert_eq!(plan.counters.proposed, 2);
    assert_eq!(plan.counters.accepted, 2);
    assert_eq!(plan.counters.steps, 1);
    assert_eq!(plan.counters.acceptance_rate(), 1.0);
}

#[test]
fn stop_truncated_commit_counts_only_accepted_tokens_that_reached_output() {
    let decision = SpeculationPlan::new(DraftMode::DraftModel("draft".into()), 1)
        .unwrap()
        .decide(
            &DraftProposal::new(
                DraftMode::DraftModel("draft".into()),
                vec![
                    proposed(0, &[(0, 1.0)]),
                    proposed(1, &[(1, 1.0)]),
                    proposed(2, &[(2, 1.0)]),
                ],
            )
            .unwrap(),
            &TargetVerification {
                probes: vec![
                    probe(0, &[(0, 1.0)]),
                    probe(1, &[(1, 1.0)]),
                    probe(2, &[(2, 1.0)]),
                    probe(3, &[(3, 1.0)]),
                ]
                .into_boxed_slice(),
            },
        )
        .unwrap();
    let mut plan = SpeculationPlan::new(DraftMode::DraftModel("draft".into()), 1).unwrap();
    plan.record_commit_prefix(&decision, 2).unwrap();
    assert_eq!(plan.counters.proposed, 3);
    assert_eq!(plan.counters.accepted, 2);
    assert_eq!(plan.counters.steps, 1);
    assert!(plan.record_commit_prefix(&decision, 0).is_err());
    assert!(plan.record_commit_prefix(&decision, 5).is_err());
}

#[test]
fn prompt_lookup_exact_verification_stops_at_first_target_mismatch() {
    let proposal = DraftProposal::new(
        DraftMode::PromptLookup,
        vec![
            proposed(0, &[(0, 1.0)]),
            proposed(1, &[(1, 1.0)]),
            proposed(2, &[(2, 1.0)]),
        ],
    )
    .unwrap();
    let verification = TargetVerification::new(
        &proposal,
        vec![
            probe(0, &[(0, 1.0)]),
            probe(3, &[(3, 1.0)]),
            probe(2, &[(2, 1.0)]),
            probe(1, &[(1, 1.0)]),
        ],
    )
    .unwrap();
    let plan = SpeculationPlan::new(DraftMode::PromptLookup, 5).unwrap();
    let decision = plan.decide(&proposal, &verification).unwrap();
    assert_eq!(decision.accepted, 1);
    assert_eq!(decision.kv_tokens, vec![0]);
    assert_eq!(decision.pending_token, 3);
    assert!(decision.rejected);
}
