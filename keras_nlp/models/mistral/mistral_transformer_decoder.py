# Copyright 2023 The KerasNLP Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from keras_nlp.backend import keras
from keras_nlp.backend import ops
from keras_nlp.layers.modeling.transformer_layer_utils import (
    compute_causal_mask,
)
from keras_nlp.layers.modeling.transformer_layer_utils import (
    merge_padding_and_attention_mask,
)
from keras_nlp.models.mistral.mistral_attention import CachedMistralAttention
from keras_nlp.models.mistral.mistral_layer_norm import (
    MistralLayerNormalization,
)
from keras_nlp.utils.keras_utils import clone_initializer


class MistralTransformerDecoder(keras.layers.Layer):
    """A Transformer decoder layer for the Mistral backbone."""

    def __init__(
        self,
        intermediate_dim,
        num_query_heads,
        num_key_value_heads,
        rope_max_wavelength=10000,
        rope_scaling_factor=1.0,
        activation="relu",
        layer_norm_epsilon=1e-5,
        kernel_initializer="glorot_uniform",
        sliding_window=512,
        dropout=0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.intermediate_dim = intermediate_dim
        self.num_query_heads = num_query_heads
        self.num_key_value_heads = num_key_value_heads

        self.rope_max_wavelength = rope_max_wavelength
        self.rope_scaling_factor = rope_scaling_factor

        self.dropout = dropout

        self.sliding_window = sliding_window
        self.activation = keras.activations.get(activation)
        self.layer_norm_epsilon = layer_norm_epsilon
        self.kernel_initializer = keras.initializers.get(kernel_initializer)

        self.supports_masking = True

    def build(self, decoder_sequence_shape):
        self._decoder_sequence_shape = decoder_sequence_shape
        self.hidden_dim = decoder_sequence_shape[-1]

        # Self attention layer.
        self._self_attention_layer = CachedMistralAttention(
            num_query_heads=self.num_query_heads,
            num_key_value_heads=self.num_key_value_heads,
            rope_max_wavelength=self.rope_max_wavelength,
            rope_scaling_factor=self.rope_scaling_factor,
            sliding_window=self.sliding_window,
            kernel_initializer=clone_initializer(self.kernel_initializer),
            dropout=self.dropout,
            dtype=self.compute_dtype,
            name="self_attention",
        )
        self._self_attention_layer.build(decoder_sequence_shape)

        self._self_attention_layernorm = MistralLayerNormalization(
            epsilon=self.layer_norm_epsilon,
            name="self_attention_layernorm",
            dtype=self.compute_dtype,
        )
        self._self_attention_layernorm.build(decoder_sequence_shape)
        self._self_attention_dropout = keras.layers.Dropout(
            rate=self.dropout,
            dtype=self.compute_dtype,
            name="self_attention_dropout",
        )

        # Feedforward layers.
        self._feedforward_intermediate_dense = keras.layers.Dense(
            self.intermediate_dim,
            kernel_initializer=clone_initializer(self.kernel_initializer),
            use_bias=False,
            dtype=self.compute_dtype,
            name="feedforward_intermediate_dense",
        )
        self._feedforward_intermediate_dense.build(decoder_sequence_shape)

        self._feedforward_gate_dense = keras.layers.Dense(
            self.intermediate_dim,
            activation=self.activation,
            kernel_initializer=clone_initializer(self.kernel_initializer),
            use_bias=False,
            name="feedforward_gate_dense",
        )
        self._feedforward_gate_dense.build(decoder_sequence_shape)

        self._feedforward_output_dense = keras.layers.Dense(
            self.hidden_dim,
            kernel_initializer=clone_initializer(self.kernel_initializer),
            use_bias=False,
            dtype=self.compute_dtype,
            name="feedforward_output_dense",
        )

        self._feedforward_output_dense.build(
            self._feedforward_gate_dense.compute_output_shape(
                decoder_sequence_shape
            )
        )

        self._feedforward_layernorm = MistralLayerNormalization(
            epsilon=self.layer_norm_epsilon,
            name="feedforward_layernorm",
            dtype=self.compute_dtype,
        )
        self._feedforward_layernorm.build(decoder_sequence_shape)

        self.built = True

    def call(
        self,
        decoder_sequence,
        decoder_padding_mask=None,
        decoder_attention_mask=None,
        self_attention_cache=None,
        self_attention_cache_update_index=None,
        training=None,
    ):
        self_attention_mask = self._compute_self_attention_mask(
            decoder_sequence=decoder_sequence,
            decoder_padding_mask=decoder_padding_mask,
            decoder_attention_mask=decoder_attention_mask,
        )
        residual = decoder_sequence

        x = self._self_attention_layernorm(decoder_sequence)

        # Self attention block.
        x = self._self_attention_layer(
            hidden_states=x,
            attention_mask=self_attention_mask,
            cache=self_attention_cache,
            cache_update_index=self_attention_cache_update_index,
        )

        if self_attention_cache is not None:
            x, self_attention_cache = x

        x = self._self_attention_dropout(x, training=training)

        x = x + residual
        residual = x

        x = self._feedforward_layernorm(x)
        gate_output = self._feedforward_gate_dense(x)

        x = self._feedforward_intermediate_dense(x)

        x = self._feedforward_output_dense(ops.multiply(x, gate_output))

        decoder_output = x + residual

        if self_attention_cache is not None:
            return decoder_output, self_attention_cache
        return decoder_output

    def _compute_self_attention_mask(
        self,
        decoder_sequence,
        decoder_padding_mask,
        decoder_attention_mask,
    ):
        decoder_mask = merge_padding_and_attention_mask(
            decoder_sequence, decoder_padding_mask, decoder_attention_mask
        )
        batch_size = ops.shape(decoder_sequence)[0]
        input_length = output_length = ops.shape(decoder_sequence)[1]

        # Mistral uses a banded attention mask
        causal_mask_lower = compute_causal_mask(
            batch_size, input_length, output_length, 0
        )
        # Below is a workaround for `ops.triu` for Keras 2.
        # TODO(tirthasheshpatel): Use `ops.triu` once Keras 2 support is removed.
        # causal_mask = ops.triu(causal_mask_lower, k=-self.sliding_window)
        i = ops.arange(output_length)[:, None]
        j = ops.arange(input_length)[None, :]
        causal_mask_upper = ops.cast(i <= j + self.sliding_window, "int32")
        causal_mask = ops.minimum(causal_mask_lower, causal_mask_upper)

        return (
            ops.minimum(decoder_mask, causal_mask)
            if decoder_mask is not None
            else causal_mask
        )

    def compute_output_shape(self, decoder_sequence_shape):
        return decoder_sequence_shape

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "intermediate_dim": self.intermediate_dim,
                "num_query_heads": self.num_query_heads,
                "rope_max_wavelength": self.rope_max_wavelength,
                "rope_scaling_factor": self.rope_scaling_factor,
                "num_key_value_heads": self.num_key_value_heads,
                "sliding_window": self.sliding_window,
                "activation": keras.activations.serialize(self.activation),
                "layer_norm_epsilon": self.layer_norm_epsilon,
                "kernel_initializer": keras.initializers.serialize(
                    self.kernel_initializer
                ),
                "dropout": self.dropout,
            }
        )
        return config
