use amw_engine::gen::{
    execute_embedding_batch, normalize_embedding_batch, EmbeddingBackend, EmbeddingInput,
    EmbeddingOptions, GenError, PoolingMode,
};

struct Backend {
    rows: Vec<Vec<Vec<f32>>>,
}

impl EmbeddingBackend for Backend {
    fn extract_token_embeddings(
        &mut self,
        _inputs: &[EmbeddingInput],
    ) -> Result<Vec<Vec<Vec<f32>>>, GenError> {
        Ok(self.rows.clone())
    }
}

#[test]
fn batch_is_extracted_pooled_normalized_and_kept_in_order() {
    let inputs = vec![
        EmbeddingInput { tokens: vec![1, 2] },
        EmbeddingInput { tokens: vec![3] },
    ];
    let mut backend = Backend {
        rows: vec![vec![vec![2.0, 0.0], vec![0.0, 2.0]], vec![vec![0.0, 3.0]]],
    };
    let values = execute_embedding_batch(
        &mut backend,
        &inputs,
        EmbeddingOptions {
            pooling: PoolingMode::Mean,
            normalize: true,
        },
    )
    .unwrap();
    let root_half = 0.5_f32.sqrt();
    assert_eq!(values, vec![vec![root_half, root_half], vec![0.0, 1.0]]);
}

#[test]
fn malformed_backend_shapes_fail_closed() {
    assert_eq!(
        normalize_embedding_batch(vec![vec![0.0, 0.0]]),
        Err(GenError::InvalidEmbedding)
    );
    let mut backend = Backend { rows: vec![] };
    assert_eq!(
        execute_embedding_batch(
            &mut backend,
            &[EmbeddingInput { tokens: vec![1] }],
            EmbeddingOptions::default()
        ),
        Err(GenError::InvalidEmbedding)
    );
}
