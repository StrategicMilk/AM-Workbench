//! Backend-independent batched embedding extraction, pooling, and normalization.

use super::GenError;

/// Pooling applied to one sequence of token embeddings.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PoolingMode {
    /// Arithmetic mean across token rows.
    Mean,
    /// First token row, for models trained with CLS pooling.
    Cls,
    /// Last token row, for causal embedding models.
    Last,
}

/// One tokenized embedding request. Batch order is response order.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct EmbeddingInput {
    /// Native token identifiers for one input.
    pub tokens: Vec<i32>,
}

/// Request-local embedding post-processing configuration.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EmbeddingOptions {
    /// Pooling contract advertised by the loaded model.
    pub pooling: PoolingMode,
    /// Whether to return unit-length vectors.
    pub normalize: bool,
}

impl Default for EmbeddingOptions {
    fn default() -> Self {
        Self {
            pooling: PoolingMode::Mean,
            normalize: true,
        }
    }
}

/// Native extraction seam implemented by the loaded-model runtime.
pub trait EmbeddingBackend {
    /// Decodes token batches and returns `[sequence][token][dimension]` rows in input order.
    fn extract_token_embeddings(
        &mut self,
        inputs: &[EmbeddingInput],
    ) -> Result<Vec<Vec<Vec<f32>>>, GenError>;
}

/// Native unpooled embedding extractor for one dedicated embedding context.
#[cfg(any(feature = "cpu", feature = "cuda"))]
pub struct NativeEmbeddingBackend<'context> {
    context: &'context mut crate::ffi::Context,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl<'context> NativeEmbeddingBackend<'context> {
    /// Binds a context configured with `EmbeddingPooling::None`.
    pub fn new(context: &'context mut crate::ffi::Context) -> Self {
        Self { context }
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl EmbeddingBackend for NativeEmbeddingBackend<'_> {
    fn extract_token_embeddings(
        &mut self,
        inputs: &[EmbeddingInput],
    ) -> Result<Vec<Vec<Vec<f32>>>, GenError> {
        let total_tokens = inputs.iter().try_fold(0_usize, |total, input| {
            total.checked_add(input.tokens.len())
        });
        let total_tokens = total_tokens.ok_or(GenError::InvalidEmbedding)?;
        let capacity = i32::try_from(total_tokens).map_err(|_| GenError::InvalidEmbedding)?;
        let max_sequences = i32::try_from(inputs.len()).map_err(|_| GenError::InvalidEmbedding)?;
        let mut batch = crate::ffi::Batch::tokens(capacity, max_sequences)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        let mut output_rows = Vec::with_capacity(inputs.len());
        let mut batch_row = 0_i32;
        for (sequence, input) in inputs.iter().enumerate() {
            let sequence_id = i32::try_from(sequence).map_err(|_| GenError::InvalidEmbedding)?;
            let mut rows = Vec::with_capacity(input.tokens.len());
            for (position, token) in input.tokens.iter().enumerate() {
                let position = i32::try_from(position).map_err(|_| GenError::InvalidEmbedding)?;
                batch
                    .add_token(*token, position, &[sequence_id], true)
                    .map_err(|error| GenError::Backend(error.to_string()))?;
                rows.push(batch_row);
                batch_row = batch_row.checked_add(1).ok_or(GenError::InvalidEmbedding)?;
            }
            output_rows.push(rows);
        }
        self.context
            .decode(&mut batch)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        output_rows
            .into_iter()
            .map(|rows| {
                rows.into_iter()
                    .map(|row| {
                        self.context
                            .embeddings(row)
                            .map(|values| values.to_vec())
                            .map_err(|error| GenError::Backend(error.to_string()))
                    })
                    .collect()
            })
            .collect()
    }
}

/// Executes one in-order batch, then pools and optionally normalizes each sequence.
pub fn execute_embedding_batch<B: EmbeddingBackend>(
    backend: &mut B,
    inputs: &[EmbeddingInput],
    options: EmbeddingOptions,
) -> Result<Vec<Vec<f32>>, GenError> {
    if inputs.is_empty()
        || inputs
            .iter()
            .any(|input| input.tokens.is_empty() || input.tokens.iter().any(|token| *token < 0))
    {
        return Err(GenError::InvalidEmbedding);
    }
    let extracted = backend.extract_token_embeddings(inputs)?;
    if extracted.len() != inputs.len() {
        return Err(GenError::InvalidEmbedding);
    }
    extracted
        .iter()
        .map(|tokens| {
            let pooled = pool_embedding(tokens, options.pooling)?;
            if options.normalize {
                normalize_embedding(pooled)
            } else {
                Ok(pooled)
            }
        })
        .collect()
}

/// Pools one non-empty rectangular token-embedding matrix.
pub fn pool_embedding(
    token_embeddings: &[Vec<f32>],
    pooling: PoolingMode,
) -> Result<Vec<f32>, GenError> {
    let dimension = token_embeddings.first().map_or(0, Vec::len);
    if dimension == 0
        || token_embeddings
            .iter()
            .any(|row| row.len() != dimension || row.iter().any(|component| !component.is_finite()))
    {
        return Err(GenError::InvalidEmbedding);
    }
    match pooling {
        PoolingMode::Cls => Ok(token_embeddings[0].clone()),
        PoolingMode::Last => token_embeddings
            .last()
            .cloned()
            .ok_or(GenError::InvalidEmbedding),
        PoolingMode::Mean => {
            let mut pooled = vec![0.0_f64; dimension];
            for row in token_embeddings {
                for (total, value) in pooled.iter_mut().zip(row) {
                    *total += f64::from(*value);
                }
            }
            let divisor = token_embeddings.len() as f64;
            pooled
                .into_iter()
                .map(|value| {
                    let value = value / divisor;
                    if value.is_finite() {
                        Ok(value as f32)
                    } else {
                        Err(GenError::InvalidEmbedding)
                    }
                })
                .collect()
        }
    }
}

/// Returns one L2-normalized vector while preserving component order.
pub fn normalize_embedding(mut vector: Vec<f32>) -> Result<Vec<f32>, GenError> {
    if vector.is_empty() || vector.iter().any(|value| !value.is_finite()) {
        return Err(GenError::InvalidEmbedding);
    }
    let norm = vector
        .iter()
        .map(|value| f64::from(*value) * f64::from(*value))
        .sum::<f64>()
        .sqrt();
    if norm == 0.0 || !norm.is_finite() {
        return Err(GenError::InvalidEmbedding);
    }
    for value in &mut vector {
        *value = (f64::from(*value) / norm) as f32;
    }
    Ok(vector)
}

/// Normalizes a batch without reordering its inputs.
pub fn normalize_embedding_batch(batch: Vec<Vec<f32>>) -> Result<Vec<Vec<f32>>, GenError> {
    batch.into_iter().map(normalize_embedding).collect()
}
