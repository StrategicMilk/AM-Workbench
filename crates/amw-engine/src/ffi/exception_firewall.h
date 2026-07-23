#pragma once

#include "llama.h"

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Status is written independently from the wrapped API result because negative
// tokenization results are part of llama.cpp's normal sizing contract.
enum amw_ffi_exception_status {
    AMW_FFI_EXCEPTION_NONE = 0,
    AMW_FFI_EXCEPTION_STANDARD = 1,
    AMW_FFI_EXCEPTION_UNKNOWN = 2,
};

const char * amw_ffi_last_error(void);

struct llama_model * amw_ffi_model_load_from_file(
    const char * path,
    struct llama_model_params params,
    int32_t * exception_status);

struct llama_context * amw_ffi_init_from_model(
    struct llama_model * model,
    struct llama_context_params params,
    int32_t * exception_status);

int32_t amw_ffi_decode(
    struct llama_context * context,
    struct llama_batch batch,
    int32_t * exception_status);

int32_t amw_ffi_tokenize(
    const struct llama_vocab * vocab,
    const char * text,
    int32_t text_len,
    llama_token * tokens,
    int32_t n_tokens_max,
    bool add_special,
    bool parse_special,
    int32_t * exception_status);

int32_t amw_ffi_detokenize(
    const struct llama_vocab * vocab,
    const llama_token * tokens,
    int32_t n_tokens,
    char * text,
    int32_t text_len_max,
    bool remove_special,
    bool unparse_special,
    int32_t * exception_status);

int32_t amw_ffi_token_to_piece(
    const struct llama_vocab * vocab,
    llama_token token,
    char * buffer,
    int32_t length,
    int32_t lstrip,
    bool special,
    int32_t * exception_status);

struct amw_ffi_vocab_identity {
    int32_t vocabulary_type;
    llama_token bos;
    llama_token eos;
    llama_token eot;
    llama_token separator;
    llama_token newline;
    llama_token padding;
    llama_token mask;
    llama_token fim_prefix;
    llama_token fim_suffix;
    llama_token fim_middle;
    llama_token fim_padding;
    llama_token fim_repository;
    llama_token fim_separator;
};

int32_t amw_ffi_vocab_identity_get(
    const struct llama_vocab * vocab,
    struct amw_ffi_vocab_identity * identity,
    int32_t * exception_status);
int32_t amw_ffi_vocab_token_metadata(
    const struct llama_vocab * vocab,
    llama_token token,
    const char ** text,
    size_t * text_length,
    float * score,
    int32_t * attributes,
    bool * is_eog,
    bool * is_control,
    int32_t * exception_status);

struct llama_sampler * amw_ffi_sampler_init_grammar(
    const struct llama_vocab * vocab,
    const char * grammar,
    const char * root,
    int32_t * exception_status);

struct llama_sampler * amw_ffi_sampler_chain_init(
    struct llama_sampler_chain_params params,
    int32_t * exception_status);
int32_t amw_ffi_sampler_chain_add(
    struct llama_sampler * chain,
    struct llama_sampler * sampler,
    int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_greedy(int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_dist(uint32_t seed, int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_top_k(int32_t k, int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_top_p(float p, size_t min_keep, int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_min_p(float p, size_t min_keep, int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_typical(float p, size_t min_keep, int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_temp(float temperature, int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_xtc(
    float probability,
    float threshold,
    size_t min_keep,
    uint32_t seed,
    int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_top_n_sigma(float sigma, int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_mirostat(
    int32_t vocab_size,
    uint32_t seed,
    float tau,
    float eta,
    int32_t candidates,
    int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_mirostat_v2(
    uint32_t seed,
    float tau,
    float eta,
    int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_penalties(
    int32_t last_n,
    float repetition,
    float frequency,
    float presence,
    int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_dry(
    const struct llama_vocab * vocab,
    int32_t context_train,
    float multiplier,
    float base,
    int32_t allowed_length,
    int32_t penalty_last_n,
    const char ** sequence_breakers,
    size_t breaker_count,
    int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_logit_bias(
    int32_t vocab_size,
    int32_t bias_count,
    const llama_logit_bias * biases,
    int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_init_infill(
    const struct llama_vocab * vocab,
    int32_t * exception_status);
llama_token amw_ffi_sampler_sample(
    struct llama_sampler * sampler,
    struct llama_context * context,
    int32_t output_index,
    int32_t * exception_status);
int32_t amw_ffi_sampler_accept(
    struct llama_sampler * sampler,
    llama_token token,
    int32_t * exception_status);
struct llama_sampler * amw_ffi_sampler_clone(
    const struct llama_sampler * sampler,
    int32_t * exception_status);

// Applies every sampler stage to one decoded output, exposes the resulting
// probability distribution, selects once, and accepts the selected token once.
// The caller must provide capacity for the model vocabulary. Candidate arrays
// are parallel and contain the post-transform retained distribution.
int32_t amw_ffi_sampler_transform_sample_accept(
    struct llama_sampler * sampler,
    struct llama_context * context,
    int32_t output_index,
    llama_token * candidate_tokens,
    float * candidate_logits,
    float * candidate_probabilities,
    size_t candidate_capacity,
    size_t * candidate_count,
    llama_token * selected_token,
    float * selected_probability,
    int32_t * exception_status);
int32_t amw_ffi_sampler_transform_probe(
    struct llama_sampler * sampler,
    struct llama_context * context,
    int32_t output_index,
    llama_token * candidate_tokens,
    float * candidate_logits,
    float * candidate_probabilities,
    size_t candidate_capacity,
    size_t * candidate_count,
    llama_token * selected_token,
    float * selected_probability,
    int32_t * exception_status);

const float * amw_ffi_get_logits_ith(
    struct llama_context * context,
    int32_t output_index,
    int32_t * exception_status);
const float * amw_ffi_get_embeddings_ith(
    struct llama_context * context,
    int32_t output_index,
    int32_t * exception_status);
const float * amw_ffi_get_embeddings_seq(
    struct llama_context * context,
    llama_seq_id sequence,
    int32_t * exception_status);

// Resolves and renders only the model-embedded template. Rust compares the
// resolved template to its trusted catalog value before calling the renderer.
const char * amw_ffi_model_chat_template(
    const struct llama_model * model,
    int32_t * exception_status);
int32_t amw_ffi_chat_apply_template(
    const char * embedded_template,
    const char * const * roles,
    const char * const * contents,
    size_t message_count,
    bool add_assistant,
    char * output,
    int32_t output_capacity,
    int32_t * exception_status);

struct llama_adapter_lora * amw_ffi_adapter_lora_init(
    struct llama_model * model,
    const char * path,
    int32_t * exception_status);
int32_t amw_ffi_set_adapters_lora(
    struct llama_context * context,
    struct llama_adapter_lora ** adapters,
    size_t adapter_count,
    float * scales,
    int32_t * exception_status);

bool amw_ffi_memory_seq_rm(
    llama_memory_t memory,
    llama_seq_id sequence,
    llama_pos start,
    llama_pos end,
    int32_t * exception_status);
int32_t amw_ffi_memory_seq_cp(
    llama_memory_t memory,
    llama_seq_id source,
    llama_seq_id destination,
    llama_pos start,
    llama_pos end,
    int32_t * exception_status);
int32_t amw_ffi_memory_seq_keep(
    llama_memory_t memory,
    llama_seq_id sequence,
    int32_t * exception_status);
int32_t amw_ffi_memory_seq_add(
    llama_memory_t memory,
    llama_seq_id sequence,
    llama_pos start,
    llama_pos end,
    llama_pos delta,
    int32_t * exception_status);
int32_t amw_ffi_memory_seq_div(
    llama_memory_t memory,
    llama_seq_id sequence,
    llama_pos start,
    llama_pos end,
    int divisor,
    int32_t * exception_status);
llama_pos amw_ffi_memory_seq_pos_min(
    llama_memory_t memory,
    llama_seq_id sequence,
    int32_t * exception_status);
llama_pos amw_ffi_memory_seq_pos_max(
    llama_memory_t memory,
    llama_seq_id sequence,
    int32_t * exception_status);
bool amw_ffi_memory_can_shift(llama_memory_t memory, int32_t * exception_status);

size_t amw_ffi_state_seq_get_size(
    struct llama_context * context,
    llama_seq_id sequence,
    int32_t * exception_status);
size_t amw_ffi_state_seq_get_data(
    struct llama_context * context,
    uint8_t * destination,
    size_t size,
    llama_seq_id sequence,
    int32_t * exception_status);
size_t amw_ffi_state_seq_set_data(
    struct llama_context * context,
    const uint8_t * source,
    size_t size,
    llama_seq_id destination_sequence,
    int32_t * exception_status);

// A deterministic test seam proving that a C++ exception is contained before
// it can unwind into Rust. It has no runtime side effects.
int32_t amw_ffi_test_exception_firewall(int32_t * exception_status);

// Thread-local deterministic injection used to prove the concrete output
// wrappers contain exceptions. 1=logits, 2=embedding row, 3=pooled embedding.
void amw_ffi_test_inject_output_exception(int32_t operation);

#ifdef __cplusplus
}
#endif
