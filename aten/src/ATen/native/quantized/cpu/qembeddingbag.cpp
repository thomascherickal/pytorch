#include <ATen/ATen.h>
#include <ATen/native/quantized/cpu/embedding_packed_params.h>
#include <ATen/native/quantized/cpu/fbgemm_utils.h>
#include <torch/library.h>
#ifdef USE_FBGEMM
#include <fbgemm/Fbgemm.h>
#include <fbgemm/FbgemmEmbedding.h>
#endif

#include <ATen/Parallel.h>

torch::class_<EmbeddingPackedParamsBase> register_embedding_params();

namespace {
at::Tensor embedding_bag_4bit_helper(
    const at::Tensor& weight,
    const at::Tensor& indices,
    const c10::optional<at::Tensor>& offsets_in,
    bool pruned_weights,
    const c10::optional<at::Tensor>& per_sample_weights_,
    const c10::optional<at::Tensor>& compressed_indices_mapping,
    bool include_last_offset) {
  TORCH_CHECK(
      offsets_in.has_value(),
      "embedding_bag_4bit_rowwise_offsets expects offsets to be set");

  TORCH_CHECK(weight.dim() == 2);
  TORCH_CHECK(indices.dim() == 1);

  auto offsets = offsets_in.value();
  TORCH_CHECK(offsets.dim() == 1);

  // FBGEMM expects the offsets to be of int type.
  at::Tensor offsets_new = offsets.toType(at::ScalarType::Int);

  auto offsets_data = offsets_new.data_ptr<int>();
  const auto weight_data = weight.data_ptr<uint8_t>();
  auto weight_contig = weight.contiguous();
  uint8_t* input_data = weight_contig.data_ptr<uint8_t>();

  // Get compressed indices for pruned_weights op.
  int32_t* compressed_indices_mapping_data = nullptr;
  int compressed_index_size = 0;
  if (pruned_weights) {
    compressed_index_size = compressed_indices_mapping.value().numel();
    compressed_indices_mapping_data =
        compressed_indices_mapping.value().data_ptr<int32_t>();
  }

  const auto indices_data = indices.data_ptr<int64_t>();
  const int64_t N = weight.size(0);
  const int64_t weight_size = weight.size(1);
  const int64_t D =
      (weight_size - 4) * 2; // NB: 2-byte fp16 scale and 2-byte zero_offset
  const int64_t M = offsets.size(0);

  int64_t output_size = M - 1;
  std::vector<int> offsets_include_last_val;
  if (!include_last_offset) {
    output_size = M;
    offsets_include_last_val.resize(M + 1);
    // Avoid `null pointer passed as argument 2` ASAN violation when ofests
    // tensor is empty.
    if (M > 0) {
      std::memcpy(
          offsets_include_last_val.data(), offsets_data, sizeof(int) * M);
    }
    offsets_include_last_val[M] = indices.numel();
    offsets_data = offsets_include_last_val.data();
  }

  const std::vector<int64_t> shape = {output_size, D};
  auto output = at::empty(shape, weight.options().dtype(at::kFloat));
  auto* output_data = output.data_ptr<float>();
  const int64_t block_size = output.size(1);
  TORCH_CHECK(block_size % 2 == 0, "block size must be divisible by 2");
  const int index_size = indices.numel();
  constexpr int prefetch_distance = 16;
#ifdef USE_FBGEMM
  if (!pruned_weights) {
    // Generate the fbgemm kernel
    auto kernel_64_ = fbgemm::GenerateEmbeddingSpMDMNBit<std::int64_t>(
        /*bit rate=*/4,
        /*block size=*/block_size,
        /*has weights=*/per_sample_weights_.has_value(),
        /*normalize_by_lengths=*/false,
        /*prefetch distance=*/prefetch_distance,
        /*is_weight_positional=*/false,
        /*use_offsets=*/true);

    bool success = kernel_64_(
        /*output_size=*/output_size,
        /*index_size=*/index_size,
        /*data_size=*/N,
        /*input=*/input_data,
        /*indices=*/indices_data,
        /*offsets=*/offsets_data,
        /*weights=*/
        per_sample_weights_.has_value()
            ? per_sample_weights_.value().data_ptr<float>()
            : nullptr,
        /*output=*/output_data);

    TORCH_CHECK(
        success,
        "FBGEMM GenerateEmbeddingSpMDMNBit kernel failed for 4-bit input");
  } else {
    auto kernel_64_ =
        fbgemm::GenerateEmbeddingSpMDMNBitRowWiseSparse<std::int64_t>(
            /*bit rate=*/4,
            /*block_size=*/block_size,
            /*has weights=*/per_sample_weights_.has_value(),
            /*normalize_by_lengths=*/false,
            /*prefetch distance*/ prefetch_distance,
            /*is_weight_positional*/ false,
            /*use_offsets*/ true);
    bool success = kernel_64_(
        /*output_size=*/output_size,
        /*index_size=*/index_size,
        /*data_size=*/compressed_index_size,
        /*input=*/input_data,
        /*indices=*/indices_data,
        /*offsets=*/offsets_data,
        /*weights=*/
        per_sample_weights_.has_value()
            ? per_sample_weights_.value().data_ptr<float>()
            : nullptr,
        /*output=*/output_data,
        /*compressed_indices_table=*/compressed_indices_mapping_data);
    TORCH_CHECK(
        success,
        "FBGEMM GenerateEmbeddingSpMDMNBitRowWiseSparse kernel failed for 4-bit input");
  }
#else

  auto accessor = offsets.accessor<int64_t, 1>();
  std::vector<int> lengths_data;

  int64_t lower = accessor[0];
  for (int64_t i = 1; i < offsets.numel(); ++i) {
    lengths_data.push_back(accessor[i] - lower);
    lower = accessor[i];
  }
  if (!include_last_offset) {
    lengths_data.push_back(indices.numel() - lower);
  }

  int64_t current = 0;
  float* per_sample_weights_data;
  if (per_sample_weights_.has_value()) {
    per_sample_weights_data = per_sample_weights_.value().data_ptr<float>();
  }
  for (int m = 0; m < output_size; ++m) {
    memset(output_data, 0, block_size * sizeof(float));
    TORCH_CHECK(
        current + lengths_data[m] <= index_size,
        "Expect the lengths data to be less than indices size");

    for (int i = 0; i < lengths_data[m]; ++i, ++current) {
      int64_t idx;
      if (!pruned_weights) {
        idx = indices_data[current];
        TORCH_CHECK((idx >= 0 && idx < N), "Invalid indices data");
      } else {
        int64_t uncompressed_idx = indices_data[current];
        TORCH_CHECK(
            uncompressed_idx >= 0 && uncompressed_idx < compressed_index_size,
            "Invalid indices data for Sparse Op.")
        idx = compressed_indices_mapping_data[uncompressed_idx];
        if (idx == -1) {
          continue;
        }
      }
      const at::Half* scale_bias = reinterpret_cast<const at::Half*>(
          input_data + (idx + 1) * weight_size - 2 * sizeof(at::Half));

      float weight_val = 1.0f;
      if (per_sample_weights_.has_value()) {
        weight_val = per_sample_weights_data[current];
      }
      const float scale = weight_val * scale_bias[0];
      const float bias = weight_val * scale_bias[1];

      for (int j = 0; j < block_size; ++j) {
        uint8_t quantized =
            input_data[idx * weight_size + j / /*NUM_ELEM_PER_BYTE*/ 2];
        quantized >>= (j % 2) * 4;
        quantized &= (1 << 4) - 1;

        output_data[j] = fma(scale, quantized, output_data[j] + bias);
      }
    } // for each i
    output_data += block_size;
  } // for each m

#endif
  return output;
}

at::Tensor embedding_bag_byte_helper(
    const at::Tensor& packed_w,
    const at::Tensor& indices,
    const c10::optional<at::Tensor>& offsets_in,
    bool pruned_weights,
    const c10::optional<at::Tensor>& per_sample_weights_,
    bool include_last_offset) {
  TORCH_CHECK(
      offsets_in.has_value(),
      "embedding_bag_byte_rowwise_offsets expects offsets to be set");
  auto offsets = offsets_in.value();
  auto offsets_data = offsets.data_ptr<int64_t>();
  const auto indices_data = indices.data_ptr<int64_t>();

  const auto weight_data = packed_w.data_ptr<uint8_t>();

  const int64_t N = packed_w.size(0);
  const int64_t D =
      packed_w.size(1) - 8; // NB: -8 to account for scale and bias
  const int64_t M = offsets.size(0);

  int64_t output_size = M - 1;
  std::vector<int64_t> offsets_include_last;

  if (!include_last_offset) {
    output_size = M;
    offsets_include_last.resize(M + 1);
    std::memcpy(
        offsets_include_last.data(),
        offsets.data_ptr<int64_t>(),
        sizeof(int64_t) * M);
    offsets_include_last[M] = indices.numel();
    offsets_data = offsets_include_last.data();
  }

  std::vector<int64_t> shape = {output_size, D};
  auto output = at::empty(shape, packed_w.options().dtype(at::kFloat));
  auto* output_data = output.data_ptr<float>();

#ifdef USE_FBGEMM

  auto kernel_i8_i64 =
      fbgemm::GenerateEmbeddingSpMDM<uint8_t, int64_t, int64_t>(
          /*block_size=*/D,
          /*has_weight=*/per_sample_weights_.has_value(),
          /*normalize_by_lengths=*/false,
          /*prefetch=*/16, // NOLINT(cppcoreguidelines-avoid-magic-numbers)
          /*is_weight_positional=*/false,
          /*use_offsets=*/true);

  if (packed_w.is_contiguous()) {
    at::parallel_for(
        0, output_size, 1, [&](int64_t start_idx, int64_t end_idx) {
          bool success = kernel_i8_i64(
              /*output_size=*/end_idx - start_idx,
              /*index_size=*/offsets_data[end_idx] - offsets_data[start_idx],
              /*data_size=*/N,
              /*input=*/weight_data,
              /*indices=*/indices_data + offsets_data[start_idx],
              /*offsets_or_lengths=*/offsets_data + start_idx,
              /*weights=*/
              per_sample_weights_
                  ? per_sample_weights_.value().data_ptr<float>() +
                      offsets_data[start_idx]
                  : nullptr,
              /*out=*/output_data + start_idx * D);

          TORCH_CHECK(
              success,
              "FBGEMM GenerateEmbeddingSpMDM kernel failed for 8-bit input");
        });
  } else {
    auto weight_contig = packed_w.contiguous();
    const auto weight_data_contig = weight_contig.data_ptr<uint8_t>();
    at::parallel_for(
        0, output_size, 1, [&](int64_t start_idx, int64_t end_idx) {
          bool success = kernel_i8_i64(
              /*output_size=*/end_idx - start_idx,
              /*index_size=*/offsets_data[end_idx] - offsets_data[start_idx],
              /*data_size=*/N,
              /*input=*/weight_data_contig,
              /*indices=*/indices_data + offsets_data[start_idx],
              /*offsets_or_lengths=*/offsets_data + start_idx,
              /*weights=*/
              per_sample_weights_
                  ? per_sample_weights_.value().data_ptr<float>() +
                      offsets_data[start_idx]
                  : nullptr,
              /*out=*/output_data + start_idx * D);
          TORCH_CHECK(
              success,
              "FBGEMM GenerateEmbeddingSpMDM kernel failed for 8-bit input");
        });
  }
#endif
  // TODO add default (non-FBGEMM) implementation.
  return output;
}

} // namespace

at::Tensor PackedEmbeddingBagWeight::embeddingbag_byte(
    const at::Tensor& indices,
    const c10::optional<at::Tensor>& offsets_in,
    bool pruned_weights,
    const c10::optional<at::Tensor>& per_sample_weights_,
    bool include_last_offset) {
  return embedding_bag_byte_helper(
      packed_w,
      indices,
      offsets_in,
      pruned_weights,
      per_sample_weights_,
      include_last_offset);
}

at::Tensor PackedEmbeddingBagWeight::embeddingbag_4bit(
    const at::Tensor& indices,
    const c10::optional<at::Tensor>& offsets_in,
    bool pruned_weights,
    const c10::optional<at::Tensor>& per_sample_weights_,
    const c10::optional<at::Tensor>& compressed_indices_mapping,
    bool include_last_offset) {
  return embedding_bag_4bit_helper(
      packed_w,
      indices,
      offsets_in,
      pruned_weights,
      per_sample_weights_,
      compressed_indices_mapping,
      include_last_offset);
}

namespace at {
namespace native {
namespace {

Tensor embedding_bag_byte_rowwise_offsets(
    const Tensor& weight,
    const Tensor& indices,
    const c10::optional<Tensor>& offsets_in,
    const bool /* scale_grad_by_freq */,
    const int64_t /* mode */,
    bool pruned_weights,
    const c10::optional<Tensor>& per_sample_weights_,
    bool include_last_offset) {
  TORCH_CHECK(weight.scalar_type() == at::kByte);
  TORCH_CHECK(weight.dim() == 2);
  return embedding_bag_byte_helper(
      weight,
      indices,
      offsets_in,
      pruned_weights,
      per_sample_weights_,
      include_last_offset);
}

Tensor embedding_bag_4bit_rowwise_offsets(
    const Tensor& weight,
    const Tensor& indices,
    const c10::optional<Tensor>& offsets_in,
    const bool /* scale_grad_by_freq */,
    const int64_t /* mode */,
    bool pruned_weights,
    const c10::optional<Tensor>& per_sample_weights_,
    const c10::optional<Tensor>& compressed_indices_mapping,
    bool include_last_offset) {
  return embedding_bag_4bit_helper(
      weight,
      indices,
      offsets_in,
      pruned_weights,
      per_sample_weights_,
      compressed_indices_mapping,
      include_last_offset);
}

template <int bit_rate>
class QEmbeddingBag final {
 public:
  static at::Tensor run(
      const c10::intrusive_ptr<EmbeddingPackedParamsBase>& packed_weight,
      const Tensor& indices,
      const c10::optional<Tensor>& offsets,
      const bool /* scale_grad_by_freq */,
      const int64_t /* mode */,
      bool pruned_weights,
      const c10::optional<Tensor>& per_sample_weights_,
      const c10::optional<Tensor>& compressed_indices_mapping,
      bool include_last_offset) {
    if (bit_rate == 8) {
      return packed_weight->embeddingbag_byte(
          indices,
          offsets,
          pruned_weights,
          per_sample_weights_,
          include_last_offset);
    } else if (bit_rate == 4) {
      return packed_weight->embeddingbag_4bit(
          indices,
          offsets,
          pruned_weights,
          per_sample_weights_,
          compressed_indices_mapping,
          include_last_offset);
    } else {
      TORCH_INTERNAL_ASSERT(
          "Currently only support 8-bit embedding_bag quantization");
    }
  }
};

template <int bit_rate>
class QEmbedding final {
 public:
  static at::Tensor run(
      const c10::intrusive_ptr<EmbeddingPackedParamsBase>& packed_weight,
      const Tensor& indices,
      bool pruned_weights) {
    const auto offsets_size = indices.numel();
    at::Tensor offsets = at::arange(0, offsets_size, at::kLong);
    at::Tensor output;
    if (bit_rate == 8) {
      return packed_weight->embeddingbag_byte(
          indices, offsets, pruned_weights, c10::nullopt, false);
    } else {
      TORCH_INTERNAL_ASSERT(
          "Currently only support 8-bit embedding quantization");
    }
    return output;
  }
};

TORCH_LIBRARY_IMPL(quantized, CPU, m) {
  // Function that works on TorchBind packed weights.
  m.impl(
      TORCH_SELECTIVE_NAME("quantized::embedding_bag_byte"),
      TORCH_FN(QEmbeddingBag<8>::run));
  m.impl(
      TORCH_SELECTIVE_NAME("quantized::embedding_bag_4bit"),
      TORCH_FN(QEmbeddingBag<4>::run));
  m.impl(
      TORCH_SELECTIVE_NAME("quantized::embedding_byte"),
      TORCH_FN(QEmbedding<8>::run));

  // Functions that work on at::Tensor packed weight.
  m.impl(
      TORCH_SELECTIVE_NAME("quantized::embedding_bag_byte_rowwise_offsets"),
      embedding_bag_byte_rowwise_offsets);
  m.impl(
      TORCH_SELECTIVE_NAME("quantized::embedding_bag_4bit_rowwise_offsets"),
      embedding_bag_4bit_rowwise_offsets);
}
} // namespace
} // namespace native
} // namespace at
