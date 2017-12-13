# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
# Modifications Copyright 2017 Abigail See
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""This file contains code to build and run the tensorflow graph for the sequence-to-sequence model"""

import os
import sys
import time
import numpy as np
import tensorflow as tf
import data
from tensorflow.contrib.tensorboard.plugins import projector
import pdb

FLAGS = tf.app.flags.FLAGS

class SentenceSelector(object):
  """A class to represent a sequence-to-sequence model for text summarization. Supports both baseline mode, pointer-generator mode, and coverage"""

  def __init__(self, hps, vocab):
    self._hps = hps
    self._vocab = vocab

  def _add_placeholders(self):
    """Add placeholders to the graph. These are entry points for any input data."""
    hps = self._hps
    self._art_batch = tf.placeholder(tf.int32, [hps.batch_size, hps.max_art_len, hps.max_sent_len], name='art_batch')
    self._art_lens = tf.placeholder(tf.int32, [hps.batch_size], name='art_lens')
    self._sent_lens = tf.placeholder(tf.int32, [hps.batch_size*hps.max_art_len], name='sent_lens')
    #self._art_padding_mask = tf.placeholder(tf.float32, [hps.batch_size, None, None], name='enc_padding_mask')
    self._target_batch = tf.placeholder(tf.int32, [hps.batch_size, hps.max_art_len], name='target_batch')


  def _make_feed_dict(self, batch, just_enc=False):
    """Make a feed dictionary mapping parts of the batch to the appropriate placeholders.
       max_art_len means maximum number of sentences of one article.
       max_sent_len means maximum number of words of one article sentence.

    Args:
      batch: Batch object
    """
    hps = self._hps
    feed_dict = {}
    feed_dict[self._art_batch] = batch.art_batch # (batch_size, max_art_len, max_sent_len)
    feed_dict[self._art_lens] = batch.art_lens   # (batch_size, )
    feed_dict[self._sent_lens] = batch.sent_lens # (batch_size*max_art_len, ), article1_sents + article2_sents + ...
    #feed_dict[self._art_padding_mask] = batch.art_padding_mask # (batch_size, max_art_len, max_sent_len)
    feed_dict[self._target_batch] = batch.target_batch # (batch_size, max_art_len)
    return feed_dict


  def _add_encoder(self, encoder_inputs, seq_len, name):
    """Add a single-layer bidirectional LSTM encoder to the graph.

    Args:
      encoder_inputs: A tensor of shape [batch_size, <=max_enc_steps, emb_size].
      seq_len: Lengths of encoder_inputs (before padding). A tensor of shape [batch_size].

    Returns:
      encoder_outputs:
        A tensor of shape [batch_size, <=max_enc_steps, 2*hidden_dim]. It's 2*hidden_dim because it's the concatenation of the forwards and backwards states.
    """
    with tf.variable_scope(name):
      cell_fw = tf.contrib.rnn.LSTMCell(self._hps.hidden_dim, initializer=self.rand_unif_init, state_is_tuple=True)
      cell_bw = tf.contrib.rnn.LSTMCell(self._hps.hidden_dim, initializer=self.rand_unif_init, state_is_tuple=True)
      (encoder_outputs, (_, _)) = tf.nn.bidirectional_dynamic_rnn(cell_fw, cell_bw, encoder_inputs, dtype=tf.float32, sequence_length=seq_len, swap_memory=True)
      encoder_outputs = tf.concat(axis=2, values=encoder_outputs) # concatenate the forwards and backwards states
    return encoder_outputs


  def _add_classifier(self, sent_feats, art_feats):
    """a logistic layer makes a binary decision as to whether the sentence belongs to the summary"""
    hidden_dim = self._hps.hidden_dim
    batch_size = self._hps.batch_size

    with tf.variable_scope('classifier'):
      w_content = tf.get_variable('w_content', [hidden_dim * 2, 1], dtype=tf.float32, initializer=self.trunc_norm_init)
      w_salience = tf.get_variable('w_salience', [hidden_dim * 2, hidden_dim * 2], dtype=tf.float32, initializer=self.trunc_norm_init)
      w_novelty = tf.get_variable('w_novelty', [hidden_dim * 2, hidden_dim * 2], dtype=tf.float32, initializer=self.trunc_norm_init)
      bias = tf.get_variable('bias', [1], dtype=tf.float32, initializer=tf.zeros_initializer())

      # s is the dynamic representation of the summary at the j-th sentence
      s = tf.zeros([batch_size, hidden_dim * 2])

      logits = [] # logits before the sigmoid layer

      for i in range(self._hps.max_art_len):
        content_feats = tf.matmul(sent_feats[:, i, :], w_content) # (batch_size, )
        salience_feats = tf.reduce_sum(tf.matmul(sent_feats[:, i, :], w_salience) * art_feats, 1) # (batch_size, )
        novelty_feats = tf.reduce_sum(tf.matmul(sent_feats[:, i, :], w_novelty) * tf.tanh(s), 1) # (batch_size, )
        logit = content_feats + salience_feats - novelty_feats + bias
        logits.append(logit)

        p = tf.sigmoid(logit) # (batch_size, )
        s += tf.multiply(sent_feats[:, i, :], p)

      return tf.stack(logits, 1) # (batch_size, max_art_len)
    

  def _add_emb_vis(self, embedding_var):
    """Do setup so that we can view word embedding visualization in Tensorboard, as described here:
    https://www.tensorflow.org/get_started/embedding_viz
    Make the vocab metadata file, then make the projector config file pointing to it."""
    train_dir = os.path.join(FLAGS.log_root, "train")
    vocab_metadata_path = os.path.join(train_dir, "vocab_metadata.tsv")
    self._vocab.write_metadata(vocab_metadata_path) # write metadata file
    summary_writer = tf.summary.FileWriter(train_dir)
    config = projector.ProjectorConfig()
    embedding = config.embeddings.add()
    embedding.tensor_name = embedding_var.name
    embedding.metadata_path = vocab_metadata_path
    projector.visualize_embeddings(summary_writer, config)

  def _add_sent_selector(self):
    """Add the whole sequence-to-sequence model to the graph."""
    hps = self._hps
    vsize = self._vocab.size() # size of the vocabulary

    with tf.variable_scope('SentSelector'):
      # Some initializers
      self.rand_unif_init = tf.random_uniform_initializer(-hps.rand_unif_init_mag, hps.rand_unif_init_mag, seed=123)
      self.trunc_norm_init = tf.truncated_normal_initializer(stddev=hps.trunc_norm_init_std)

      ####################################################################
      # Add embedding matrix (shared by the encoder and decoder inputs)  #
      ####################################################################
      with tf.variable_scope('embedding'):
        self.embedding = tf.get_variable('embedding', [vsize, hps.emb_dim], dtype=tf.float32, \
                                          initializer=self.trunc_norm_init)
        if hps.mode=="train": self._add_emb_vis(self.embedding) # add to tensorboard
        emb_batch = tf.nn.embedding_lookup(self.embedding, self._art_batch) # tensor with shape (batch_size, max_art_len, max_sent_len, emb_size), article1_sents + article2_sents + ...

      ########################################
      # Add the two encoders.                #
      ########################################
      # Add word-level encoder to encode each sentence.
      sent_enc_inputs = tf.reshape(emb_batch, [-1, hps.max_sent_len, hps.emb_dim]) # (batch_size*max_art_len, max_sent_len, emb_dim)
      sent_enc_outputs = self._add_encoder(sent_enc_inputs, self._sent_lens, name='sent_encoder') # (batch_size*max_art_len, max_sent_len, hidden_dim*2)

      # Add sentence-level encoder to produce sentence representations.
      # sentence-level encoder input: average-pooled, concatenated hidden states of the word-level bi-LSTM.
      art_enc_inputs = tf.reduce_mean(sent_enc_outputs, axis=1) # (batch_size*max_art_len, hidden_dim*2)
      art_enc_inputs = tf.reshape(art_enc_inputs, [hps.batch_size, -1, hps.hidden_dim*2]) # (batch_size, max_art_len, hidden_dim*2)
      art_enc_outputs = self._add_encoder(art_enc_inputs, self._art_lens, name='art_encoder') # (batch_size, max_art_len, hidden_dim*2)

      # Get each sentence representation and the document representation.
      sent_feats = tf.contrib.layers.fully_connected(art_enc_outputs, hps.hidden_dim*2, activation_fn=tf.tanh) # (batch_size, max_art_len, hidden_dim*2)
      art_feats = tf.reduce_mean(art_enc_outputs, axis=1) # (batch_size, hidden_dim*2)
      art_feats = tf.contrib.layers.fully_connected(art_feats, hps.hidden_dim*2, activation_fn=tf.tanh) # (batch_size, hidden_dim*2)

      ########################################
      # Add the classifier.                  #
      ########################################
      logits = self._add_classifier(sent_feats, art_feats) # (batch_size, max_art_len)

      ################################################
      # Calculate the loss                           #
      ################################################
      with tf.variable_scope('loss'):
        self._loss = tf.sigmoid_cross_entropy_with_logits(logits=logits, labels=self._target_batch)
        tf.summary.scalar('loss', self._loss)


  def _add_train_op(self):
    """Sets self._train_op, the op to run for training."""
    # Take gradients of the trainable variables w.r.t. the loss function to minimize
    tvars = tf.trainable_variables()
    gradients = tf.gradients(self._loss, tvars, aggregation_method=tf.AggregationMethod.EXPERIMENTAL_TREE)

    # Clip the gradients
    with tf.device("/gpu:0"):
      grads, global_norm = tf.clip_by_global_norm(gradients, self._hps.max_grad_norm)

    # Add a summary
    tf.summary.scalar('global_norm', global_norm)

    # Apply adagrad optimizer
    optimizer = tf.train.AdagradOptimizer(self._hps.lr, initial_accumulator_value=self._hps.adagrad_init_acc)
    with tf.device("/gpu:0"):
      self._train_op = optimizer.apply_gradients(zip(grads, tvars), global_step=self.global_step, name='train_step')


  def build_graph(self):
    """Add the placeholders, model, global step, train_op and summaries to the graph"""
    tf.logging.info('Building graph...')
    t0 = time.time()
    self._add_placeholders()
    with tf.device("/gpu:0"):
      self._add_sent_selector()
    self.global_step = tf.Variable(0, name='global_step', trainable=False)
    if self._hps.mode == 'train':
      self._add_train_op()
    self._summaries = tf.summary.merge_all()
    t1 = time.time()
    tf.logging.info('Time to build graph: %i seconds', t1 - t0)


  def run_train_step(self, sess, batch):
    """Runs one training iteration. Returns a dictionary containing train op, 
       summaries, loss, global_step and (optionally) coverage loss."""
    hps = self._hps
    feed_dict = self._make_feed_dict(batch)
    pdb.set_trace()

    to_return = {
        'train_op': self._train_op,
        'summaries': self._summaries,
        'loss': self._loss,
        'global_step': self.global_step,
    }
    return sess.run(to_return, feed_dict)

  def run_eval_step(self, sess, batch):
    """Runs one evaluation iteration. Returns a dictionary containing summaries, 
       loss, global_step and (optionally) coverage loss."""
    hps = self._hps
    feed_dict = self._make_feed_dict(batch)

    to_return = {
        'summaries': self._summaries,
        'loss': self._loss,
        'global_step': self.global_step,
    }
    return sess.run(to_return, feed_dict)

