import numpy as np

import tensorflow as tf
from tensorflow.keras.layers import Layer

# Helper Functions
##################

def scaled_dot_product_attention(q, k, v, mask):
    """Calculate the attention weights.
    q, k, v must have matching leading dimensions.
    k, v must have matching penultimate dimension, i.e.: seq_len_k = seq_len_v.
    The mask has different shapes depending on its type(padding or look ahead)
    but it must be broadcastable for addition.

    Args:
    q: query shape == (..., seq_len_q, depth)
    k: key shape == (..., seq_len_k, depth)
    v: value shape == (..., seq_len_v, depth_v)
    mask: Float tensor with shape broadcastable
          to (..., seq_len_q, seq_len_k). Defaults to None.

    Returns:
    output, attention_weights

    Function taken from:
    https://www.tensorflow.org/text/tutorials/transformer
    """

    matmul_qk = tf.matmul(q, k, transpose_b=True)  # (..., seq_len_q, seq_len_k)

    # scale matmul_qk
    dk = tf.cast(tf.shape(k)[-1], tf.float32)
    scaled_attention_logits = matmul_qk / tf.math.sqrt(dk)

    # add the mask to the scaled tensor.
    if mask is not None:
        scaled_attention_logits += (mask * -1e9)

    # softmax is normalized on the last axis (seq_len_k) so that the scores
    # add up to 1.
    attention_weights = tf.nn.softmax(scaled_attention_logits, axis=-1)  # (..., seq_len_q, seq_len_k)

    output = tf.matmul(attention_weights, v)  # (..., seq_len_q, depth_v)

    return output, attention_weights


# Layers
########


class AddPositionEmbs(Layer):
    """Adds (optionally learned) positional embeddings to the inputs."""

    def __init__(self, posemb_init=None, **kwargs):
        """
        Initialized the learnable weights of the positional embedding
        :param posemb_init: Initializer of the learnable weights
        :param kwargs: Additional keyword arguments passed to the __init__ of the TF Layer class

        Function taken from:
        https://github.com/tensorflow/models/blob/master/official/projects/vit/modeling/vit.py
        """
        super().__init__(**kwargs)
        self.posemb_init = posemb_init

    def build(self, inputs_shape):
        """
        Builds the layer with a given input shape
        :param inputs_shape: Input shape for the layer
        """
        pos_emb_shape = (1, inputs_shape[1], inputs_shape[2])
        self.pos_embedding = self.add_weight('pos_embedding', pos_emb_shape, initializer=self.posemb_init)

    def call(self, inputs, *args, **kwargs):
        """
        Calls the layer and adds the positional encodings to the input tensor
        :param inputs: inputs to which the positional encoding will be added
        :param args: additional arguments not used but there to be compatible with the normal call routine
        :param kargs: additional keyword arguments not used but there to be compatible with the normal call routine
        """
        # inputs.shape is (batch_size, seq_len, emb_dim).
        pos_embedding = tf.cast(self.pos_embedding, inputs.dtype)

        return inputs + pos_embedding


class MultiHeadAttention(Layer):
    """
    A simple multi head attention layer followed by a single layer MLP according to
    https://www.tensorflow.org/text/tutorials/transformer
    """
    def __init__(self, d_model, num_heads, use_norm=True, activation="relu"):
        """
        Initializes the multiheaded attention layer.
        :param d_model: dimension of the key, query and value (total, will be split to the heads)
        :param num_heads: Number of head in the layer
        :param use_norm: If true, use layer norm
        """
        super(MultiHeadAttention, self).__init__()
        self.num_heads = num_heads
        self.d_model = d_model
        self.use_norm = use_norm

        assert d_model % self.num_heads == 0

        self.depth = d_model // self.num_heads

        self.wq = tf.keras.layers.Dense(d_model)
        self.wk = tf.keras.layers.Dense(d_model)
        self.wv = tf.keras.layers.Dense(d_model)

        if self.use_norm:
            self.layer_norm1 = tf.keras.layers.LayerNormalization()
            self.layer_norm2 = tf.keras.layers.LayerNormalization()

        self.activation_layer = tf.keras.layers.Activation(activation)

        self.dense = tf.keras.layers.Dense(d_model)

    def split_heads(self, x, batch_size):
        """Split the last dimension into (num_heads, depth).
        Transpose the result such that the shape is (batch_size, num_heads, seq_len, depth)
        :param x: input the split
        :param batch_size: batch size for the reshape
        """
        x = tf.reshape(x, (batch_size, -1, self.num_heads, self.depth))
        return tf.transpose(x, perm=[0, 2, 1, 3])

    def call(self, inputs, mask=None, *args, **kwargs):
        """
        Calls the layer
        :param inputs: The input used for the multi headed attention
        :param mask: mask to apply to the attention must be broadcastable to the q, k product
        :param args: additional arguments not used but there to be compatible with the normal call routine
        :param kwargs: additional keyword arguments not used but there to be compatible with the normal call routine
        """
        batch_size = tf.shape(inputs)[0]

        # norm before input
        inputs = self.layer_norm1(inputs)

        q = self.wq(inputs)  # (batch_size, seq_len, d_model)
        k = self.wk(inputs)  # (batch_size, seq_len, d_model)
        v = self.wv(inputs)  # (batch_size, seq_len, d_model)

        q = self.split_heads(q, batch_size)  # (batch_size, num_heads, seq_len_q, depth)
        k = self.split_heads(k, batch_size)  # (batch_size, num_heads, seq_len_k, depth)
        v = self.split_heads(v, batch_size)  # (batch_size, num_heads, seq_len_v, depth)

        # scaled_attention.shape == (batch_size, num_heads, seq_len_q, depth)
        # attention_weights.shape == (batch_size, num_heads, seq_len_q, seq_len_k)
        scaled_attention, attention_weights = scaled_dot_product_attention(q, k, v, mask)

        scaled_attention = tf.transpose(scaled_attention,
                                        perm=[0, 2, 1, 3])  # (batch_size, seq_len_q, num_heads, depth)

        concat_attention = tf.reshape(scaled_attention,
                                      (batch_size, -1, self.d_model))  # (batch_size, seq_len_q, d_model)

        # residual connection
        concat_attention = inputs + concat_attention

        # layer norm
        output = self.layer_norm2(concat_attention)

        output = self.dense(output)  # (batch_size, seq_len_q, d_model)

        # activation and res
        output = self.activation_layer(output)
        output = output + concat_attention

        return output


class Graph_ViT(Layer):
    """
    A visual transformer layer for healpy graphs

    This visual transformer is a very simple implementation of a transformer. It is based on the tutorial
    https://www.tensorflow.org/text/tutorials/transformer
    and the simple parts of
    https://github.com/tensorflow/models/tree/master/official/projects/vit
    note that this could be far more advanced and should be used as a starting point.

    The individual patches of the graph are healpy superpixels with a size of 4**p and the whole layer can be
    repeated multiple times.
    """

    def __init__(self, p, key_dim, num_heads, positional_encoding=True, n_layers=1, activation="relu",
                 layer_norm=True):
        """
        Creates a visual transformer according to:
        https://arxiv.org/pdf/2010.11929.pdf
        by dividing the healpy graph into super pixels
        :param p: reduction factor >1 of the nside -> number of nodes reduces by 4^p, note that the layer only checks
                  if the dimensionality of the input is evenly divisible by 4^p and not if the ordering is correct
                  (should be nested ordering)
        :param key_dim: Dimension of the key, query and value for the embedding in the multi head attention for each
                        head. Note that this means that the initial embedding will be key_dim*num_heads
        :param num_heads: Number of heads to learn in the multi head attention
        :param positional_encoding: If True, add positional encoding to the superpixel embedding in the beginning.
        :param n_layers: Number of TransformerEncoding layers after the initial embedding
        :param activation: The activation function to use for the multiheaded attention
        :param layer_norm: If layernorm should be used for the multiheaded attention
        """

        # This is necessary for every Layer
        super(Graph_ViT, self).__init__()

        # check p
        if not p > 1:
            raise IOError("The super pixel size factor p has to be at least 1!")

        # save variables
        self.p = p
        self.embed_filter_size = int(4 ** p)
        self.key_dim = key_dim
        self.num_heads = num_heads
        self.embedding_size = self.key_dim*self.num_heads
        self.positional_encoding = positional_encoding
        self.n_layers = n_layers
        self.activation = activation
        self.layer_norm = layer_norm

        # create the embedding with a conv1D with correct strides and filters
        self.embed = tf.keras.layers.Conv1D(self.embedding_size, self.embed_filter_size, strides=self.embed_filter_size,
                                            padding='valid', data_format='channels_last')
        if self.positional_encoding:
            self.pos_encoder = AddPositionEmbs()

        # the multiheaded attention layers
        assert n_layers >= 1, "Number of attention layers should be at least 1"
        self.mha_layers = []
        for i in range(n_layers):
            self.mha_layers.append(MultiHeadAttention(d_model=self.embedding_size, num_heads=self.num_heads,
                                                      use_norm=self.layer_norm, activation=self.activation))


    def build(self, input_shape):
        """
        Builds the layer given an input shape
        :param input_shape: shape of the input
        """

        # deal with the initial embedding
        n_nodes = int(input_shape[1])
        if n_nodes % self.embed_filter_size != 0:
            raise IOError(f"Input shape {input_shape} not compatible with the embedding filter "
                          f"size {self.embed_filter_size}")
        self.embed.build(input_shape)

        # add the positional encoding
        if self.positional_encoding:
            self.pos_encoder.build(inputs_shape=input_shape)


    def call(self, inputs, *args, **kwargs):
        """
        Calls the layer and performs all the operations
        :param inputs: inputs to which the positional encoding will be added
        :param args: additional arguments not used but there to be compatible with the normal call routine
        :param kwargs: additional keyword arguments not used but there to be compatible with the normal call routine
        """

        # perform the initial embedding
        x = self.embed(inputs)

        # apply the attention layers
        for mha in self.mha_layers:
            x = mha(x)

        return x
