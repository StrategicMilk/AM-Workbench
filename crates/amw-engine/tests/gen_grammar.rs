use amw_engine::gen::{CompiledGrammar, GenError, MAX_GRAMMAR_ACCEPTED_TOKENS, MAX_GRAMMAR_BYTES};

#[test]
fn invalid_grammar_is_request_local_and_valid_grammar_advances() {
    assert!(matches!(
        CompiledGrammar::compile("not a rule"),
        Err(GenError::GrammarInvalid(_))
    ));
    assert!(matches!(
        CompiledGrammar::compile(&"x".repeat(MAX_GRAMMAR_BYTES + 1)),
        Err(GenError::GrammarInvalid(_))
    ));

    let mut concurrent_request = CompiledGrammar::compile("root ::= \"ok\"").unwrap();
    concurrent_request.accept(42).unwrap();
    assert_eq!(concurrent_request.accepted_count(), 1);
    assert_eq!(concurrent_request.source(), "root ::= \"ok\"");
}

#[test]
fn parser_rejects_malformed_and_pathologically_nested_grammar() {
    assert!(matches!(
        CompiledGrammar::compile("root ::= (\"a\""),
        Err(GenError::GrammarInvalid(_))
    ));
    assert!(matches!(
        CompiledGrammar::compile("root ::= missing-rule"),
        Err(GenError::GrammarInvalid(message)) if message.contains("undefined rule")
    ));
    let nested = format!("root ::= {}\"a\"{}", "(".repeat(65), ")".repeat(65));
    assert_eq!(
        CompiledGrammar::compile(&nested).unwrap_err(),
        GenError::GrammarResourceLimit("group nesting")
    );
    let grammar = CompiledGrammar::compile("root ::= \"a\" | \"b\"").unwrap();
    assert_eq!(grammar.rule_count(), 1);
}

#[test]
fn accepted_token_accounting_is_bounded_without_retaining_history() {
    let mut grammar = CompiledGrammar::compile("root ::= \"a\"").unwrap();
    for _ in 0..MAX_GRAMMAR_ACCEPTED_TOKENS {
        grammar.accept(1).unwrap();
    }
    assert_eq!(
        grammar.accept(1),
        Err(GenError::GrammarResourceLimit("accepted token history"))
    );
    assert_eq!(grammar.accepted_count(), MAX_GRAMMAR_ACCEPTED_TOKENS);
}

#[test]
fn nul_input_fails_without_panicking() {
    assert!(matches!(
        CompiledGrammar::compile("root ::= \"a\"\0"),
        Err(GenError::GrammarInvalid(_))
    ));
}

#[test]
fn escaped_quotes_backslashes_brackets_and_comment_markers_scan_as_literals() {
    for expression in [
        "\"\\\"\"",
        "\"\\\\\"",
        r#"[\]\[]"#,
        "\"value#part\"",
        "\"::=\"",
    ] {
        let source = format!("root ::= {expression} # real comment");
        let result = CompiledGrammar::compile(&source);
        assert!(
            result.is_ok(),
            "{:?} must scan as a literal: {:?}",
            expression.as_bytes(),
            result.as_ref().err()
        );
        let grammar = result.unwrap();
        assert_eq!(grammar.rule_count(), 1);
        assert_eq!(grammar.source(), source);
    }
}
