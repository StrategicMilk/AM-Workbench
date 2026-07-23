#include "exception_firewall.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <exception>
#include <limits>
#include <stdexcept>
#include <utility>
#include <vector>

namespace {

thread_local std::array<char, 1024> last_error{};
thread_local int32_t injected_output_exception = 0;

void set_last_error(const char * message) noexcept {
    const char * detail = message == nullptr ? "native exception without detail" : message;
    const auto length = std::min(std::strlen(detail), last_error.size() - 1);
    std::memcpy(last_error.data(), detail, length);
    last_error[length] = '\0';
}

void begin_call(int32_t * exception_status) noexcept {
    last_error[0] = '\0';
    if (exception_status != nullptr) {
        *exception_status = AMW_FFI_EXCEPTION_NONE;
    }
}

template <typename Result, typename Call>
Result guard_call(int32_t * exception_status, Result fallback, Call && call) noexcept {
    begin_call(exception_status);
    try {
        return std::forward<Call>(call)();
    } catch (const std::exception & error) {
        set_last_error(error.what());
        if (exception_status != nullptr) {
            *exception_status = AMW_FFI_EXCEPTION_STANDARD;
        }
    } catch (...) {
        set_last_error("unknown native C++ exception");
        if (exception_status != nullptr) {
            *exception_status = AMW_FFI_EXCEPTION_UNKNOWN;
        }
    }
    return fallback;
}

void maybe_inject_output_exception(int32_t operation) {
    if (injected_output_exception == operation) {
        injected_output_exception = 0;
        throw std::runtime_error("deterministic native output exception probe");
    }
}

void normalize_probabilities(llama_token_data_array * candidates) {
    if (candidates == nullptr || candidates->data == nullptr || candidates->size == 0) {
        throw std::runtime_error("sampler returned an empty candidate distribution");
    }
    float maximum = -std::numeric_limits<float>::infinity();
    for (size_t index = 0; index < candidates->size; ++index) {
        const auto logit = candidates->data[index].logit;
        if (std::isnan(logit) || logit == std::numeric_limits<float>::infinity()) {
            throw std::runtime_error("sampler returned an invalid candidate logit");
        }
        maximum = std::max(maximum, logit);
    }
    if (!std::isfinite(maximum)) {
        throw std::runtime_error("sampler returned no finite candidate logits");
    }
    double denominator = 0.0;
    for (size_t index = 0; index < candidates->size; ++index) {
        const auto logit = candidates->data[index].logit;
        const auto probability = std::isfinite(logit)
            ? std::exp(static_cast<double>(logit) - maximum)
            : 0.0;
        candidates->data[index].p = static_cast<float>(probability);
        denominator += probability;
    }
    if (!std::isfinite(denominator) || denominator <= 0.0) {
        throw std::runtime_error("sampler probability denominator is invalid");
    }
    for (size_t index = 0; index < candidates->size; ++index) {
        candidates->data[index].p = static_cast<float>(candidates->data[index].p / denominator);
    }
}

std::vector<llama_token_data> output_candidates(struct llama_context * context, int32_t output_index) {
    const llama_token sampled_token = llama_get_sampled_token_ith(context, output_index);
    if (sampled_token != LLAMA_TOKEN_NULL) {
        throw std::runtime_error("context-attached backend sampler already selected this output");
    }
    const float * sampled_probs = llama_get_sampled_probs_ith(context, output_index);
    const float * sampled_logits = llama_get_sampled_logits_ith(context, output_index);
    const llama_token * sampled_ids = llama_get_sampled_candidates_ith(context, output_index);
    std::vector<llama_token_data> candidates;
    if (sampled_probs != nullptr) {
        const uint32_t count = llama_get_sampled_probs_count_ith(context, output_index);
        if (sampled_ids == nullptr || sampled_logits == nullptr) {
            throw std::runtime_error("backend sampled probabilities lack token or logit data");
        }
        candidates.reserve(count);
        for (uint32_t index = 0; index < count; ++index) {
            candidates.push_back({sampled_ids[index], sampled_logits[index], sampled_probs[index]});
        }
        return candidates;
    }
    if (sampled_logits != nullptr) {
        const uint32_t count = llama_get_sampled_logits_count_ith(context, output_index);
        if (sampled_ids == nullptr) {
            throw std::runtime_error("backend sampled logits lack token data");
        }
        candidates.reserve(count);
        for (uint32_t index = 0; index < count; ++index) {
            candidates.push_back({sampled_ids[index], sampled_logits[index], 0.0F});
        }
        return candidates;
    }
    const llama_model * model = llama_get_model(context);
    const llama_vocab * vocab = llama_model_get_vocab(model);
    const int32_t vocabulary_size = llama_vocab_n_tokens(vocab);
    const float * logits = llama_get_logits_ith(context, output_index);
    if (logits == nullptr || vocabulary_size <= 0) {
        throw std::runtime_error("decoded output logits are unavailable");
    }
    candidates.reserve(static_cast<size_t>(vocabulary_size));
    for (llama_token token = 0; token < vocabulary_size; ++token) {
        candidates.push_back({token, logits[token], 0.0F});
    }
    return candidates;
}

int32_t transform_sampler_output(
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
    bool accept_selected) {
    if (sampler == nullptr || context == nullptr || candidate_tokens == nullptr
        || candidate_logits == nullptr || candidate_probabilities == nullptr
        || candidate_count == nullptr || selected_token == nullptr
        || selected_probability == nullptr) {
        throw std::invalid_argument("sampler output arguments must be non-null");
    }
    auto candidates = output_candidates(context, output_index);
    llama_token_data_array distribution = {
        candidates.data(), candidates.size(), -1, false,
    };
    llama_sampler_apply(sampler, &distribution);
    if (distribution.selected < 0
        || static_cast<size_t>(distribution.selected) >= distribution.size) {
        throw std::runtime_error("sampler did not select a valid token");
    }
    if (distribution.size > candidate_capacity) {
        throw std::length_error("post-transform candidates exceed caller capacity");
    }
    normalize_probabilities(&distribution);
    for (size_t index = 0; index < distribution.size; ++index) {
        candidate_tokens[index] = distribution.data[index].id;
        candidate_logits[index] = distribution.data[index].logit;
        candidate_probabilities[index] = distribution.data[index].p;
    }
    *candidate_count = distribution.size;
    *selected_token = distribution.data[distribution.selected].id;
    *selected_probability = distribution.data[distribution.selected].p;
    if (accept_selected) {
        llama_sampler_accept(sampler, *selected_token);
    }
    return 0;
}

} // namespace

extern "C" const char * amw_ffi_last_error(void) {
    return last_error.data();
}

extern "C" struct llama_model * amw_ffi_model_load_from_file(
    const char * path,
    struct llama_model_params params,
    int32_t * exception_status) {
    return guard_call<struct llama_model *>(exception_status, nullptr, [&] {
        return llama_model_load_from_file(path, params);
    });
}

extern "C" struct llama_context * amw_ffi_init_from_model(
    struct llama_model * model,
    struct llama_context_params params,
    int32_t * exception_status) {
    return guard_call<struct llama_context *>(exception_status, nullptr, [&] {
        return llama_init_from_model(model, params);
    });
}

extern "C" int32_t amw_ffi_decode(
    struct llama_context * context,
    struct llama_batch batch,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        return llama_decode(context, batch);
    });
}

extern "C" int32_t amw_ffi_tokenize(
    const struct llama_vocab * vocab,
    const char * text,
    int32_t text_len,
    llama_token * tokens,
    int32_t n_tokens_max,
    bool add_special,
    bool parse_special,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, 0, [&] {
        return llama_tokenize(
            vocab,
            text,
            text_len,
            tokens,
            n_tokens_max,
            add_special,
            parse_special);
    });
}

extern "C" int32_t amw_ffi_detokenize(
    const struct llama_vocab * vocab,
    const llama_token * tokens,
    int32_t n_tokens,
    char * text,
    int32_t text_len_max,
    bool remove_special,
    bool unparse_special,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, 0, [&] {
        return llama_detokenize(
            vocab,
            tokens,
            n_tokens,
            text,
            text_len_max,
            remove_special,
            unparse_special);
    });
}

extern "C" int32_t amw_ffi_token_to_piece(
    const struct llama_vocab * vocab,
    llama_token token,
    char * buffer,
    int32_t length,
    int32_t lstrip,
    bool special,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, 0, [&] {
        return llama_token_to_piece(vocab, token, buffer, length, lstrip, special);
    });
}

extern "C" int32_t amw_ffi_vocab_identity_get(
    const struct llama_vocab * vocab,
    struct amw_ffi_vocab_identity * identity,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        if (vocab == nullptr || identity == nullptr) {
            throw std::invalid_argument("vocabulary identity arguments must be non-null");
        }
        *identity = {
            static_cast<int32_t>(llama_vocab_type(vocab)),
            llama_vocab_bos(vocab),
            llama_vocab_eos(vocab),
            llama_vocab_eot(vocab),
            llama_vocab_sep(vocab),
            llama_vocab_nl(vocab),
            llama_vocab_pad(vocab),
            llama_vocab_mask(vocab),
            llama_vocab_fim_pre(vocab),
            llama_vocab_fim_suf(vocab),
            llama_vocab_fim_mid(vocab),
            llama_vocab_fim_pad(vocab),
            llama_vocab_fim_rep(vocab),
            llama_vocab_fim_sep(vocab),
        };
        return 0;
    });
}

extern "C" int32_t amw_ffi_vocab_token_metadata(
    const struct llama_vocab * vocab,
    llama_token token,
    const char ** text,
    size_t * text_length,
    float * score,
    int32_t * attributes,
    bool * is_eog,
    bool * is_control,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        if (vocab == nullptr || text == nullptr || text_length == nullptr || score == nullptr
            || attributes == nullptr || is_eog == nullptr || is_control == nullptr) {
            throw std::invalid_argument("vocabulary token metadata arguments must be non-null");
        }
        const int32_t count = llama_vocab_n_tokens(vocab);
        if (token < 0 || token >= count) {
            throw std::out_of_range("vocabulary token metadata id is out of range");
        }
        const char * token_text = llama_vocab_get_text(vocab, token);
        if (token_text == nullptr) {
            throw std::runtime_error("vocabulary token text is unavailable");
        }
        *text = token_text;
        *text_length = std::strlen(token_text);
        *score = llama_vocab_get_score(vocab, token);
        *attributes = static_cast<int32_t>(llama_vocab_get_attr(vocab, token));
        *is_eog = llama_vocab_is_eog(vocab, token);
        *is_control = llama_vocab_is_control(vocab, token);
        return 0;
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_grammar(
    const struct llama_vocab * vocab,
    const char * grammar,
    const char * root,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_grammar(vocab, grammar, root);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_chain_init(
    struct llama_sampler_chain_params params,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_chain_init(params);
    });
}

extern "C" int32_t amw_ffi_sampler_chain_add(
    struct llama_sampler * chain,
    struct llama_sampler * sampler,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        llama_sampler_chain_add(chain, sampler);
        return 0;
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_greedy(int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [] {
        return llama_sampler_init_greedy();
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_dist(
    uint32_t seed,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_dist(seed);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_top_k(
    int32_t k,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_top_k(k);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_top_p(
    float p,
    size_t min_keep,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_top_p(p, min_keep);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_min_p(
    float p,
    size_t min_keep,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_min_p(p, min_keep);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_typical(
    float p,
    size_t min_keep,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_typical(p, min_keep);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_temp(
    float temperature,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_temp(temperature);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_xtc(
    float probability,
    float threshold,
    size_t min_keep,
    uint32_t seed,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_xtc(probability, threshold, min_keep, seed);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_top_n_sigma(
    float sigma,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_top_n_sigma(sigma);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_mirostat(
    int32_t vocab_size,
    uint32_t seed,
    float tau,
    float eta,
    int32_t candidates,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_mirostat(vocab_size, seed, tau, eta, candidates);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_mirostat_v2(
    uint32_t seed,
    float tau,
    float eta,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_mirostat_v2(seed, tau, eta);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_penalties(
    int32_t last_n,
    float repetition,
    float frequency,
    float presence,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_penalties(last_n, repetition, frequency, presence);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_dry(
    const struct llama_vocab * vocab,
    int32_t context_train,
    float multiplier,
    float base,
    int32_t allowed_length,
    int32_t penalty_last_n,
    const char ** sequence_breakers,
    size_t breaker_count,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_dry(
            vocab,
            context_train,
            multiplier,
            base,
            allowed_length,
            penalty_last_n,
            sequence_breakers,
            breaker_count);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_logit_bias(
    int32_t vocab_size,
    int32_t bias_count,
    const llama_logit_bias * biases,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_logit_bias(vocab_size, bias_count, biases);
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_init_infill(
    const struct llama_vocab * vocab,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_init_infill(vocab);
    });
}

extern "C" llama_token amw_ffi_sampler_sample(
    struct llama_sampler * sampler,
    struct llama_context * context,
    int32_t output_index,
    int32_t * exception_status) {
    return guard_call<llama_token>(exception_status, -1, [&] {
        return llama_sampler_sample(sampler, context, output_index);
    });
}

extern "C" int32_t amw_ffi_sampler_accept(
    struct llama_sampler * sampler,
    llama_token token,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        llama_sampler_accept(sampler, token);
        return 0;
    });
}

extern "C" struct llama_sampler * amw_ffi_sampler_clone(
    const struct llama_sampler * sampler,
    int32_t * exception_status) {
    return guard_call<struct llama_sampler *>(exception_status, nullptr, [&] {
        return llama_sampler_clone(sampler);
    });
}

extern "C" int32_t amw_ffi_sampler_transform_sample_accept(
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
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        return transform_sampler_output(
            sampler,
            context,
            output_index,
            candidate_tokens,
            candidate_logits,
            candidate_probabilities,
            candidate_capacity,
            candidate_count,
            selected_token,
            selected_probability,
            true);
    });
}

extern "C" int32_t amw_ffi_sampler_transform_probe(
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
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        return transform_sampler_output(
            sampler,
            context,
            output_index,
            candidate_tokens,
            candidate_logits,
            candidate_probabilities,
            candidate_capacity,
            candidate_count,
            selected_token,
            selected_probability,
            false);
    });
}

extern "C" const float * amw_ffi_get_logits_ith(
    struct llama_context * context,
    int32_t output_index,
    int32_t * exception_status) {
    return guard_call<const float *>(exception_status, nullptr, [&] {
        maybe_inject_output_exception(1);
        return llama_get_logits_ith(context, output_index);
    });
}

extern "C" const float * amw_ffi_get_embeddings_ith(
    struct llama_context * context,
    int32_t output_index,
    int32_t * exception_status) {
    return guard_call<const float *>(exception_status, nullptr, [&] {
        maybe_inject_output_exception(2);
        return llama_get_embeddings_ith(context, output_index);
    });
}

extern "C" const float * amw_ffi_get_embeddings_seq(
    struct llama_context * context,
    llama_seq_id sequence,
    int32_t * exception_status) {
    return guard_call<const float *>(exception_status, nullptr, [&] {
        maybe_inject_output_exception(3);
        return llama_get_embeddings_seq(context, sequence);
    });
}

extern "C" const char * amw_ffi_model_chat_template(
    const struct llama_model * model,
    int32_t * exception_status) {
    return guard_call<const char *>(exception_status, nullptr, [&] {
        return llama_model_chat_template(model, nullptr);
    });
}

extern "C" int32_t amw_ffi_chat_apply_template(
    const char * embedded_template,
    const char * const * roles,
    const char * const * contents,
    size_t message_count,
    bool add_assistant,
    char * output,
    int32_t output_capacity,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        if (embedded_template == nullptr || (message_count > 0 && (roles == nullptr || contents == nullptr))) {
            throw std::invalid_argument("chat template arguments must be non-null");
        }
        std::vector<llama_chat_message> messages;
        messages.reserve(message_count);
        for (size_t index = 0; index < message_count; ++index) {
            if (roles[index] == nullptr || contents[index] == nullptr) {
                throw std::invalid_argument("chat message fields must be non-null");
            }
            messages.push_back({roles[index], contents[index]});
        }
        return llama_chat_apply_template(
            embedded_template,
            messages.data(),
            messages.size(),
            add_assistant,
            output,
            output_capacity);
    });
}

extern "C" struct llama_adapter_lora * amw_ffi_adapter_lora_init(
    struct llama_model * model,
    const char * path,
    int32_t * exception_status) {
    return guard_call<struct llama_adapter_lora *>(exception_status, nullptr, [&] {
        return llama_adapter_lora_init(model, path);
    });
}

extern "C" int32_t amw_ffi_set_adapters_lora(
    struct llama_context * context,
    struct llama_adapter_lora ** adapters,
    size_t adapter_count,
    float * scales,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        return llama_set_adapters_lora(context, adapters, adapter_count, scales);
    });
}

extern "C" bool amw_ffi_memory_seq_rm(
    llama_memory_t memory,
    llama_seq_id sequence,
    llama_pos start,
    llama_pos end,
    int32_t * exception_status) {
    return guard_call<bool>(exception_status, false, [&] {
        return llama_memory_seq_rm(memory, sequence, start, end);
    });
}

extern "C" int32_t amw_ffi_memory_seq_cp(
    llama_memory_t memory,
    llama_seq_id source,
    llama_seq_id destination,
    llama_pos start,
    llama_pos end,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        llama_memory_seq_cp(memory, source, destination, start, end);
        return 0;
    });
}

extern "C" int32_t amw_ffi_memory_seq_keep(
    llama_memory_t memory,
    llama_seq_id sequence,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        llama_memory_seq_keep(memory, sequence);
        return 0;
    });
}

extern "C" int32_t amw_ffi_memory_seq_add(
    llama_memory_t memory,
    llama_seq_id sequence,
    llama_pos start,
    llama_pos end,
    llama_pos delta,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        llama_memory_seq_add(memory, sequence, start, end, delta);
        return 0;
    });
}

extern "C" int32_t amw_ffi_memory_seq_div(
    llama_memory_t memory,
    llama_seq_id sequence,
    llama_pos start,
    llama_pos end,
    int divisor,
    int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, -1, [&] {
        llama_memory_seq_div(memory, sequence, start, end, divisor);
        return 0;
    });
}

extern "C" llama_pos amw_ffi_memory_seq_pos_min(
    llama_memory_t memory,
    llama_seq_id sequence,
    int32_t * exception_status) {
    return guard_call<llama_pos>(exception_status, -1, [&] {
        return llama_memory_seq_pos_min(memory, sequence);
    });
}

extern "C" llama_pos amw_ffi_memory_seq_pos_max(
    llama_memory_t memory,
    llama_seq_id sequence,
    int32_t * exception_status) {
    return guard_call<llama_pos>(exception_status, -1, [&] {
        return llama_memory_seq_pos_max(memory, sequence);
    });
}

extern "C" bool amw_ffi_memory_can_shift(
    llama_memory_t memory,
    int32_t * exception_status) {
    return guard_call<bool>(exception_status, false, [&] {
        return llama_memory_can_shift(memory);
    });
}

extern "C" size_t amw_ffi_state_seq_get_size(
    struct llama_context * context,
    llama_seq_id sequence,
    int32_t * exception_status) {
    return guard_call<size_t>(exception_status, 0, [&] {
        return llama_state_seq_get_size(context, sequence);
    });
}

extern "C" size_t amw_ffi_state_seq_get_data(
    struct llama_context * context,
    uint8_t * destination,
    size_t size,
    llama_seq_id sequence,
    int32_t * exception_status) {
    return guard_call<size_t>(exception_status, 0, [&] {
        return llama_state_seq_get_data(context, destination, size, sequence);
    });
}

extern "C" size_t amw_ffi_state_seq_set_data(
    struct llama_context * context,
    const uint8_t * source,
    size_t size,
    llama_seq_id destination_sequence,
    int32_t * exception_status) {
    return guard_call<size_t>(exception_status, 0, [&] {
        return llama_state_seq_set_data(context, source, size, destination_sequence);
    });
}

extern "C" int32_t amw_ffi_test_exception_firewall(int32_t * exception_status) {
    return guard_call<int32_t>(exception_status, 0, []() -> int32_t {
        throw std::runtime_error("deterministic exception-firewall probe");
    });
}

extern "C" void amw_ffi_test_inject_output_exception(int32_t operation) {
    injected_output_exception = operation;
}
