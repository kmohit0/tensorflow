# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
"""Stateless random ops which take seed as a tensor input."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import enum
import numpy as np
import six
import sys

from tensorflow.python.compat import compat
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_util
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import bitwise_ops
from tensorflow.python.ops import gen_stateless_random_ops
from tensorflow.python.ops import gen_stateless_random_ops_v2
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import logging_ops
from tensorflow.python.util import deprecation
from tensorflow.python.util import dispatch
from tensorflow.python.util.tf_export import tf_export


ops.NotDifferentiable("StatelessMultinomial")
ops.NotDifferentiable("StatelessRandomBinomial")
ops.NotDifferentiable("StatelessRandomNormal")
ops.NotDifferentiable("StatelessRandomPoisson")
ops.NotDifferentiable("StatelessRandomUniform")
ops.NotDifferentiable("StatelessRandomUniformInt")
ops.NotDifferentiable("StatelessRandomUniformFullInt")
ops.NotDifferentiable("StatelessTruncatedNormal")


ops.NotDifferentiable("StatelessRandomNormalV2")
ops.NotDifferentiable("StatelessRandomUniformV2")
ops.NotDifferentiable("StatelessRandomUniformIntV2")
ops.NotDifferentiable("StatelessRandomUniformFullIntV2")
ops.NotDifferentiable("StatelessTruncatedNormalV2")


@tf_export("random.Algorithm", "random.experimental.Algorithm")
class Algorithm(enum.Enum):
  # The numbers here must match framework/rng_alg.h
  PHILOX = 1
  THREEFRY = 2
  AUTO_SELECT = 3


def convert_alg_to_int(alg):
  """Converts algorithm to an integer.

  Args:
    alg: can be one of these types: integer, Algorithm, Tensor, string. Allowed
      strings are "philox" and "threefry".

  Returns:
    An integer, unless the input is a Tensor in which case a Tensor is returned.
  """
  if isinstance(alg, six.integer_types):
    return alg
  if isinstance(alg, Algorithm):
    return alg.value
  if isinstance(alg, ops.Tensor):
    return alg
  if isinstance(alg, str):
    if alg == "philox":
      return Algorithm.PHILOX.value
    elif alg in ("threefry", "three-fry", "three_fry"):
      return Algorithm.THREEFRY.value
    elif alg in ("autoselect", "auto-select", "auto_select"):
      return Algorithm.AUTO_SELECT.value
    else:
      raise ValueError(
          f"Argument `alg` got unsupported string value {alg}. Supported "
          f"string values are 'philox' for the Philox algorithm, 'threefry' "
          f"for the ThreeFry algorithm, and 'auto_select' for auto-selection.")
  else:
    raise TypeError(
        f"Can't convert argument `alg` (of value {alg} and type {type(alg)}) "
        f"to int.")


def _resolve_alg(alg):
  if alg == Algorithm.AUTO_SELECT.value:
    return gen_stateless_random_ops_v2.stateless_random_get_alg()
  return alg


def _get_key_counter(seed, alg):
  """Calculates the key and counter to pass to raw RNG ops.

  This function calculates the key and counter that will be passed to
  the raw RNG ops like `StatelessRandomUniformV2`. Depending on the
  input `alg`, the key and counter may be scrambled or copied from
  `seed`. If `alg` is `"auto_select"`, the key and counter will be
  determined at runtime based on device type.

  Args:
    seed: An integer tensor of shape [2]. The seed to calculate the
      key and counter from.
    alg: The RNG algorithm. See `tf.random.stateless_uniform` for an
      explanation.

  Returns:
    A pair (key, counter) suitable for V2 stateless RNG ops like
    `StatelessRandomUniformV2`.
  """
  if alg == Algorithm.AUTO_SELECT.value:
    key, counter = gen_stateless_random_ops_v2.stateless_random_get_key_counter(
        seed)
  elif alg == Algorithm.PHILOX.value:
    key, counter = _philox_scramble_seed(seed)
  elif alg == Algorithm.THREEFRY.value:
    key = array_ops.reshape(
        uint32s_to_uint64(math_ops.cast(seed, dtypes.uint32)), [1])
    counter = array_ops.zeros([1], dtypes.uint64)
  else:
    raise ValueError(
        f"Argument `alg` got unsupported value {alg}. Supported values are "
        f"{Algorithm.PHILOX.value} for the Philox algorithm, "
        f"{Algorithm.THREEFRY.value} for the ThreeFry algorithm, and "
        f"{Algorithm.AUTO_SELECT.value} for auto-selection.")
  return key, counter


def _get_key_counter_alg(seed, alg):
  if alg is None:
    alg = Algorithm.AUTO_SELECT.value
  alg = convert_alg_to_int(alg)
  key, counter = _get_key_counter(seed, alg)
  if compat.forward_compatible(2021, 8, 11):
    return key, counter, alg
  else:
    return key, counter, _resolve_alg(alg)


def _philox_scramble_seed(seed):
  # the same scrambling procedure as core/kernels/stateless_random_ops.cc
  key = constant_op.constant([0x02461e293ec8f720], dtypes.uint64)
  counter = math_ops.cast(seed, dtypes.uint64)
  mix = gen_stateless_random_ops_v2.stateless_random_uniform_full_int_v2(
      [4], key=key, counter=counter, dtype=dtypes.uint32,
      alg=Algorithm.PHILOX.value)
  key = array_ops.reshape(uint32s_to_uint64(mix[:2]), [1])
  counter = array_ops.stack([0, uint32s_to_uint64(mix[2:])], axis=0)
  return key, counter


def uint32s_to_uint64(x):
  return bitwise_ops.bitwise_or(
      math_ops.cast(x[0], dtypes.uint64),
      bitwise_ops.left_shift(math_ops.cast(x[1], dtypes.uint64),
                             constant_op.constant(32, dtypes.uint64)))


@tf_export("random.experimental.stateless_split")
@dispatch.add_dispatch_support
def split(seed, num=2, alg="auto_select"):
  """Splits an RNG seed into `num` new seeds by adding a leading axis.

  Example:

  >>> seed = [1, 2]
  >>> new_seeds = tf.random.experimental.stateless_split(seed, num=3)
  >>> print(new_seeds)
  tf.Tensor(
  [[1105988140 1738052849]
   [-335576002  370444179]
   [  10670227 -246211131]], shape=(3, 2), dtype=int32)
  >>> tf.random.stateless_normal(shape=[3], seed=new_seeds[0, :])
  <tf.Tensor: shape=(3,), dtype=float32, numpy=array([-0.59835213, -0.9578608 ,
  0.9002807 ], dtype=float32)>

  Args:
    seed: an RNG seed (a tensor with shape [2] and dtype `int32` or
      `int64`). (When using XLA, only `int32` is allowed.)
    num: optional, a positive integer or scalar tensor indicating the number of
      seeds to produce (default 2).
    alg: The RNG algorithm used to generate the random numbers. See
      `tf.random.stateless_uniform` for a detailed explanation.

  Returns:
    A tensor with shape [num, 2] representing `num` new seeds. It will have the
    same dtype as `seed` (if `seed` doesn't have an explict dtype, the dtype
    will be determined by `tf.convert_to_tensor`).
  """
  seed = ops.convert_to_tensor(seed)
  return stateless_random_uniform(shape=[num, 2], seed=seed, dtype=seed.dtype,
                                  minval=None, maxval=None, alg=alg)


@tf_export("random.experimental.stateless_fold_in")
@dispatch.add_dispatch_support
def fold_in(seed, data, alg="auto_select"):
  """Folds in data to an RNG seed to form a new RNG seed.

  For example, in a distributed-training setting, suppose we have a master seed
  and a replica ID. We want to fold the replica ID into the master seed to
  form a "replica seed" to be used by that replica later on, so that different
  replicas will generate different random numbers but the reproducibility of the
  whole system can still be controlled by the master seed:

  >>> master_seed = [1, 2]
  >>> replica_id = 3
  >>> replica_seed = tf.random.experimental.stateless_fold_in(
  ...   master_seed, replica_id)
  >>> print(replica_seed)
  tf.Tensor([1105988140          3], shape=(2,), dtype=int32)
  >>> tf.random.stateless_normal(shape=[3], seed=replica_seed)
  <tf.Tensor: shape=(3,), dtype=float32, numpy=array([0.03197195, 0.8979765 ,
  0.13253039], dtype=float32)>

  Args:
    seed: an RNG seed (a tensor with shape [2] and dtype `int32` or
      `int64`). (When using XLA, only `int32` is allowed.)
    data: an `int32` or `int64` scalar representing data to be folded in to the
      seed.
    alg: The RNG algorithm used to generate the random numbers. See
      `tf.random.stateless_uniform` for a detailed explanation.

  Returns:
    A new RNG seed that is a deterministic function of the inputs and is
    statistically safe for producing a stream of new pseudo-random values. It
    will have the same dtype as `data` (if `data` doesn't have an explict dtype,
    the dtype will be determined by `tf.convert_to_tensor`).
  """
  data = ops.convert_to_tensor(data)
  seed1 = stateless_random_uniform(shape=[], seed=seed, dtype=data.dtype,
                                   minval=None, maxval=None, alg=alg)
  return array_ops.stack([seed1, data])


@tf_export("random.stateless_uniform")
@dispatch.add_dispatch_support
def stateless_random_uniform(shape,
                             seed,
                             minval=0,
                             maxval=None,
                             dtype=dtypes.float32,
                             name=None,
                             alg="auto_select"):
  """Outputs deterministic pseudorandom values from a uniform distribution.

  This is a stateless version of `tf.random.uniform`: if run twice with the
  same seeds and shapes, it will produce the same pseudorandom numbers.  The
  output is consistent across multiple runs on the same hardware (and between
  CPU and GPU), but may change between versions of TensorFlow or on non-CPU/GPU
  hardware.

  The generated values follow a uniform distribution in the range
  `[minval, maxval)`. The lower bound `minval` is included in the range, while
  the upper bound `maxval` is excluded.

  For floats, the default range is `[0, 1)`.  For ints, at least `maxval` must
  be specified explicitly.

  In the integer case, the random integers are slightly biased unless
  `maxval - minval` is an exact power of two.  The bias is small for values of
  `maxval - minval` significantly smaller than the range of the output (either
  `2**32` or `2**64`).

  For full-range (i.e. inclusive of both max and min) random integers, pass
  `minval=None` and `maxval=None` with an integer `dtype`. For an integer dtype
  either both `minval` and `maxval` must be `None` or neither may be `None`. For
  example:
  ```python
  ints = tf.random.stateless_uniform(
      [10], seed=(2, 3), minval=None, maxval=None, dtype=tf.int32)
  ```

  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    minval: A Tensor or Python value of type `dtype`, broadcastable with
      `shape` (for integer types, broadcasting is not supported, so it needs to
      be a scalar). The lower bound on the range of random values to
      generate. Pass `None` for full-range integers.  Defaults to 0.
    maxval: A Tensor or Python value of type `dtype`, broadcastable with
      `shape` (for integer types, broadcasting is not supported, so it needs to
      be a scalar). The upper bound on the range of random values to generate.
      Defaults to 1 if `dtype` is floating point. Pass `None` for full-range
      integers.
    dtype: The type of the output: `float16`, `float32`, `float64`, `int32`, or
      `int64`. For unbounded uniform ints (`minval`, `maxval` both `None`),
      `uint32` and `uint64` may be used.
    name: A name for the operation (optional).
    alg: The RNG algorithm used to generate the random numbers. Valid
      choices are `"philox"` for [the Philox
      algorithm](https://www.thesalmons.org/john/random123/papers/random123sc11.pdf),
      `"threefry"` for [the ThreeFry
      algorithm](https://www.thesalmons.org/john/random123/papers/random123sc11.pdf),
      and `"auto_select"` (default) for the system to automatically
      select an algorithm based the device type. Values of
      `tf.random.Algorithm` can also be used. Note that with
      `"auto_select"`, the outputs of this function may change when
      it is running on a different device.

  Returns:
    A tensor of the specified shape filled with random uniform values.

  Raises:
    ValueError: If `dtype` is integral and only one of `minval` or `maxval` is
      specified.
  """
  dtype = dtypes.as_dtype(dtype)
  accepted_dtypes = (dtypes.float16, dtypes.bfloat16, dtypes.float32,
                     dtypes.float64, dtypes.int32, dtypes.int64, dtypes.uint32,
                     dtypes.uint64)
  if dtype not in accepted_dtypes:
    raise ValueError(
        f"Argument `dtype` got invalid value {dtype}. Accepted dtypes are "
        f"{accepted_dtypes}.")
  if dtype.is_integer:
    if (minval is None) != (maxval is None):
      raise ValueError(
          f"For integer `dtype` argument {dtype}, argument `minval` and "
          f"`maxval` must be both None or not None. Got `minval`={minval} and "
          f"`maxval`={maxval}.")
    if minval is not None and dtype in (dtypes.uint32, dtypes.uint64):
      raise ValueError(
          f"Argument `dtype` got invalid value {dtype} when argument `minval` "
          f"is not None. Please don't use unsigned integers in this case.")
  elif maxval is None:
    maxval = 1
  with ops.name_scope(name, "stateless_random_uniform",
                      [shape, seed, minval, maxval]) as name:
    shape = tensor_util.shape_tensor(shape)
    if dtype.is_integer and minval is None:
      key, counter, alg = _get_key_counter_alg(seed, alg)
      result = (
          gen_stateless_random_ops_v2.stateless_random_uniform_full_int_v2(
              shape, key=key, counter=counter, dtype=dtype, alg=alg, name=name))
    else:
      minval = ops.convert_to_tensor(minval, dtype=dtype, name="min")
      maxval = ops.convert_to_tensor(maxval, dtype=dtype, name="max")
      if dtype.is_integer:
        key, counter, alg = _get_key_counter_alg(seed, alg)
        result = gen_stateless_random_ops_v2.stateless_random_uniform_int_v2(
            shape,
            key=key,
            counter=counter,
            minval=minval,
            maxval=maxval,
            alg=alg,
            name=name)
      else:
        key, counter, alg = _get_key_counter_alg(seed, alg)
        rnd = gen_stateless_random_ops_v2.stateless_random_uniform_v2(
            shape, key=key, counter=counter, dtype=dtype, alg=alg)
        result = math_ops.add(rnd * (maxval - minval), minval, name=name)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export("random.deterministic_uniform")
@dispatch.add_dispatch_support
def deterministic_random_uniform(shape,
                             minval=0,
                             maxval=None,
                             dtype=dtypes.float32,
                             name=None,
                             alg="auto_select"):

  dtype = dtypes.as_dtype(dtype)
  if dtype not in (dtypes.float16, dtypes.bfloat16, dtypes.float32,
                   dtypes.float64, dtypes.int32, dtypes.int64, dtypes.uint32,
                   dtypes.uint64):
    raise ValueError("Invalid dtype %r" % dtype)
  if dtype.is_integer:
    if (minval is None) != (maxval is None):
      raise ValueError("For integer dtype {}, minval and maxval must be both "
                       "`None` or both non-`None`.".format(dtype))
    if minval is not None and dtype in (dtypes.uint32, dtypes.uint64):
      raise ValueError("Invalid dtype for bounded uniform integers: %r" % dtype)
  elif maxval is None:
    maxval = 1

  seed=ops.get_default_graph().seed
  #logging_ops.print_v2('the original seed is ',seed,output_stream=sys.stdout)
  ops.get_default_graph().seed = split(seed,num=1)[0,:]
  #logging_ops.print_v2('the original seed is ',ops.get_default_graph().seed,output_stream=sys.stdout)
  with ops.name_scope(name, "stateless_random_uniform",
                      [shape, seed, minval, maxval]) as name:
    shape = tensor_util.shape_tensor(shape)
    if dtype.is_integer and minval is None:
      key, counter, alg = _get_key_counter_alg(seed, alg)
      result = (
          gen_stateless_random_ops_v2.stateless_random_uniform_full_int_v2(
              shape, key=key, counter=counter, dtype=dtype, alg=alg, name=name))
    else:
      minval = ops.convert_to_tensor(minval, dtype=dtype, name="min")
      maxval = ops.convert_to_tensor(maxval, dtype=dtype, name="max")
      if dtype.is_integer:
        key, counter, alg = _get_key_counter_alg(seed, alg)
        result = gen_stateless_random_ops_v2.stateless_random_uniform_int_v2(
            shape,
            key=key,
            counter=counter,
            minval=minval,
            maxval=maxval,
            alg=alg,
            name=name)
      else:
        key, counter, alg = _get_key_counter_alg(seed, alg)
        rnd = gen_stateless_random_ops_v2.stateless_random_uniform_v2(
            shape, key=key, counter=counter, dtype=dtype, alg=alg)
        result = math_ops.add(rnd * (maxval - minval), minval, name=name)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export("random.stateless_binomial")
@dispatch.add_dispatch_support
def stateless_random_binomial(shape,
                              seed,
                              counts,
                              probs,
                              output_dtype=dtypes.int32,
                              name=None):
  """Outputs deterministic pseudorandom values from a binomial distribution.

  The generated values follow a binomial distribution with specified count and
  probability of success parameters.

  This is a stateless version of `tf.random.Generator.binomial`: if run twice
  with the same seeds and shapes, it will produce the same pseudorandom numbers.
  The output is consistent across multiple runs on the same hardware (and
  between CPU and GPU), but may change between versions of TensorFlow or on
  non-CPU/GPU hardware.

  Example:

  ```python
  counts = [10., 20.]
  # Probability of success.
  probs = [0.8]

  binomial_samples = tf.random.stateless_binomial(
      shape=[2], seed=[123, 456], counts=counts, probs=probs)

  counts = ... # Shape [3, 1, 2]
  probs = ...  # Shape [1, 4, 2]
  shape = [3, 4, 3, 4, 2]
  # Sample shape will be [3, 4, 3, 4, 2]
  binomial_samples = tf.random.stateless_binomial(
      shape=shape, seed=[123, 456], counts=counts, probs=probs)
  ```

  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    counts: Tensor. The counts of the binomial distribution. Must be
      broadcastable with `probs`, and broadcastable with the rightmost
      dimensions of `shape`.
    probs: Tensor. The probability of success for the binomial distribution.
      Must be broadcastable with `counts` and broadcastable with the rightmost
      dimensions of `shape`.
    output_dtype: The type of the output. Default: tf.int32
    name: A name for the operation (optional).

  Returns:
    samples: A Tensor of the specified shape filled with random binomial
      values.  For each i, each samples[..., i] is an independent draw from
      the binomial distribution on counts[i] trials with probability of
      success probs[i].

  """
  with ops.name_scope(name, "stateless_random_binomial",
                      [shape, seed, counts, probs]) as name:
    shape = tensor_util.shape_tensor(shape)
    probs = ops.convert_to_tensor(
        probs, dtype_hint=dtypes.float32, name="probs")
    counts = ops.convert_to_tensor(
        counts, dtype_hint=probs.dtype, name="counts")
    result = gen_stateless_random_ops.stateless_random_binomial(
        shape=shape, seed=seed, counts=counts, probs=probs, dtype=output_dtype)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export("random.deterministic_binomial")
@dispatch.add_dispatch_support
def deterministic_random_binomial(shape,
                              counts,
                              probs,
                              output_dtype=dtypes.int32,
                              name=None):
  """Outputs deterministic pseudorandom values from a binomial distribution.
  The generated values follow a binomial distribution with specified count and
  probability of success parameters.
  This is a stateless version of `tf.random.Generator.binomial`: if run twice
  with the same seeds and shapes, it will produce the same pseudorandom numbers.
  The output is consistent across multiple runs on the same hardware (and
  between CPU and GPU), but may change between versions of TensorFlow or on
  non-CPU/GPU hardware.
  Example:
  ```python
  counts = [10., 20.]
  # Probability of success.
  probs = [0.8]
  binomial_samples = tf.random.stateless_binomial(
      shape=[2], seed=[123, 456], counts=counts, probs=probs)
  counts = ... # Shape [3, 1, 2]
  probs = ...  # Shape [1, 4, 2]
  shape = [3, 4, 3, 4, 2]
  # Sample shape will be [3, 4, 3, 4, 2]
  binomial_samples = tf.random.stateless_binomial(
      shape=shape, seed=[123, 456], counts=counts, probs=probs)
  ```
  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    counts: Tensor. The counts of the binomial distribution. Must be
      broadcastable with `probs`, and broadcastable with the rightmost
      dimensions of `shape`.
    probs: Tensor. The probability of success for the binomial distribution.
      Must be broadcastable with `counts` and broadcastable with the rightmost
      dimensions of `shape`.
    output_dtype: The type of the output. Default: tf.int32
    name: A name for the operation (optional).
  Returns:
    samples: A Tensor of the specified shape filled with random binomial
      values.  For each i, each samples[..., i] is an independent draw from
      the binomial distribution on counts[i] trials with probability of
      success probs[i].
  """
  seed=ops.get_default_graph().seed

  ops.get_default_graph().seed = split(seed,num=1)[0,:]


  with ops.name_scope(name, "stateless_random_binomial",
                      [shape, seed, counts, probs]) as name:
    shape = tensor_util.shape_tensor(shape)
    probs = ops.convert_to_tensor(
        probs, dtype_hint=dtypes.float32, name="probs")
    counts = ops.convert_to_tensor(
        counts, dtype_hint=probs.dtype, name="counts")
    result = gen_stateless_random_ops.stateless_random_binomial(
        shape=shape, seed=seed, counts=counts, probs=probs, dtype=output_dtype)
    tensor_util.maybe_set_static_shape(result, shape)
    return result




@tf_export("random.stateless_gamma")
@dispatch.add_dispatch_support
def stateless_random_gamma(shape,
                           seed,
                           alpha,
                           beta=None,
                           dtype=dtypes.float32,
                           name=None):
  """Outputs deterministic pseudorandom values from a gamma distribution.

  The generated values follow a gamma distribution with specified concentration
  (`alpha`) and inverse scale (`beta`) parameters.

  This is a stateless version of `tf.random.gamma`: if run twice with the same
  seeds and shapes, it will produce the same pseudorandom numbers. The output is
  consistent across multiple runs on the same hardware (and between CPU and
  GPU),
  but may change between versions of TensorFlow or on non-CPU/GPU hardware.

  A slight difference exists in the interpretation of the `shape` parameter
  between `stateless_gamma` and `gamma`: in `gamma`, the `shape` is always
  prepended to the shape of the broadcast of `alpha` with `beta`; whereas in
  `stateless_gamma` the `shape` parameter must always encompass the shapes of
  each of `alpha` and `beta` (which must broadcast together to match the
  trailing dimensions of `shape`).

  Note: Because internal calculations are done using `float64` and casting has
  `floor` semantics, we must manually map zero outcomes to the smallest
  possible positive floating-point value, i.e., `np.finfo(dtype).tiny`.  This
  means that `np.finfo(dtype).tiny` occurs more frequently than it otherwise
  should.  This bias can only happen for small values of `alpha`, i.e.,
  `alpha << 1` or large values of `beta`, i.e., `beta >> 1`.

  The samples are differentiable w.r.t. alpha and beta.
  The derivatives are computed using the approach described in
  (Figurnov et al., 2018).

  Example:

  ```python
  samples = tf.random.stateless_gamma([10, 2], seed=[12, 34], alpha=[0.5, 1.5])
  # samples has shape [10, 2], where each slice [:, 0] and [:, 1] represents
  # the samples drawn from each distribution

  samples = tf.random.stateless_gamma([7, 5, 2], seed=[12, 34], alpha=[.5, 1.5])
  # samples has shape [7, 5, 2], where each slice [:, :, 0] and [:, :, 1]
  # represents the 7x5 samples drawn from each of the two distributions

  alpha = tf.constant([[1.], [3.], [5.]])
  beta = tf.constant([[3., 4.]])
  samples = tf.random.stateless_gamma(
      [30, 3, 2], seed=[12, 34], alpha=alpha, beta=beta)
  # samples has shape [30, 3, 2], with 30 samples each of 3x2 distributions.

  with tf.GradientTape() as tape:
    tape.watch([alpha, beta])
    loss = tf.reduce_mean(tf.square(tf.random.stateless_gamma(
        [30, 3, 2], seed=[12, 34], alpha=alpha, beta=beta)))
  dloss_dalpha, dloss_dbeta = tape.gradient(loss, [alpha, beta])
  # unbiased stochastic derivatives of the loss function
  alpha.shape == dloss_dalpha.shape  # True
  beta.shape == dloss_dbeta.shape  # True
  ```

  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    alpha: Tensor. The concentration parameter of the gamma distribution. Must
      be broadcastable with `beta`, and broadcastable with the rightmost
      dimensions of `shape`.
    beta: Tensor. The inverse scale parameter of the gamma distribution. Must be
      broadcastable with `alpha` and broadcastable with the rightmost dimensions
      of `shape`.
    dtype: Floating point dtype of `alpha`, `beta`, and the output.
    name: A name for the operation (optional).

  Returns:
    samples: A Tensor of the specified shape filled with random gamma values.
      For each i, each `samples[..., i] is an independent draw from the gamma
      distribution with concentration alpha[i] and scale beta[i].

  """
  with ops.name_scope(name, "stateless_random_gamma",
                      [shape, seed, alpha, beta]) as name:
    shape = tensor_util.shape_tensor(shape)
    alpha = ops.convert_to_tensor(alpha, dtype=dtype, name="alpha")
    beta = ops.convert_to_tensor(
        beta if beta is not None else 1, name="beta", dtype=dtype)
    broadcast_shape = array_ops.broadcast_dynamic_shape(
        array_ops.shape(alpha), array_ops.shape(beta))
    alpha_broadcast = array_ops.broadcast_to(alpha, broadcast_shape)
    result = math_ops.maximum(
        np.finfo(alpha.dtype.as_numpy_dtype).tiny,
        gen_stateless_random_ops.stateless_random_gamma_v2(
            shape, seed=seed, alpha=alpha_broadcast) / beta)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export("random.deterministic_gamma")
@dispatch.add_dispatch_support
def deterministic_random_gamma(shape,
                           alpha,
                           beta=None,
                           dtype=dtypes.float32,
                           name=None):
  """Outputs deterministic pseudorandom values from a gamma distribution.
  The generated values follow a gamma distribution with specified concentration
  (`alpha`) and inverse scale (`beta`) parameters.
  This is a stateless version of `tf.random.gamma`: if run twice with the same
  seeds and shapes, it will produce the same pseudorandom numbers. The output is
  consistent across multiple runs on the same hardware (and between CPU and
  GPU),
  but may change between versions of TensorFlow or on non-CPU/GPU hardware.
  A slight difference exists in the interpretation of the `shape` parameter
  between `stateless_gamma` and `gamma`: in `gamma`, the `shape` is always
  prepended to the shape of the broadcast of `alpha` with `beta`; whereas in
  `stateless_gamma` the `shape` parameter must always encompass the shapes of
  each of `alpha` and `beta` (which must broadcast together to match the
  trailing dimensions of `shape`).
  Note: Because internal calculations are done using `float64` and casting has
  `floor` semantics, we must manually map zero outcomes to the smallest
  possible positive floating-point value, i.e., `np.finfo(dtype).tiny`.  This
  means that `np.finfo(dtype).tiny` occurs more frequently than it otherwise
  should.  This bias can only happen for small values of `alpha`, i.e.,
  `alpha << 1` or large values of `beta`, i.e., `beta >> 1`.
  The samples are differentiable w.r.t. alpha and beta.
  The derivatives are computed using the approach described in
  (Figurnov et al., 2018).
  Example:
  ```python
  samples = tf.random.stateless_gamma([10, 2], seed=[12, 34], alpha=[0.5, 1.5])
  # samples has shape [10, 2], where each slice [:, 0] and [:, 1] represents
  # the samples drawn from each distribution
  samples = tf.random.stateless_gamma([7, 5, 2], seed=[12, 34], alpha=[.5, 1.5])
  # samples has shape [7, 5, 2], where each slice [:, :, 0] and [:, :, 1]
  # represents the 7x5 samples drawn from each of the two distributions
  alpha = tf.constant([[1.], [3.], [5.]])
  beta = tf.constant([[3., 4.]])
  samples = tf.random.stateless_gamma(
      [30, 3, 2], seed=[12, 34], alpha=alpha, beta=beta)
  # samples has shape [30, 3, 2], with 30 samples each of 3x2 distributions.
  with tf.GradientTape() as tape:
    tape.watch([alpha, beta])
    loss = tf.reduce_mean(tf.square(tf.random.stateless_gamma(
        [30, 3, 2], seed=[12, 34], alpha=alpha, beta=beta)))
  dloss_dalpha, dloss_dbeta = tape.gradient(loss, [alpha, beta])
  # unbiased stochastic derivatives of the loss function
  alpha.shape == dloss_dalpha.shape  # True
  beta.shape == dloss_dbeta.shape  # True
  ```
  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    alpha: Tensor. The concentration parameter of the gamma distribution. Must
      be broadcastable with `beta`, and broadcastable with the rightmost
      dimensions of `shape`.
    beta: Tensor. The inverse scale parameter of the gamma distribution. Must be
      broadcastable with `alpha` and broadcastable with the rightmost dimensions
      of `shape`.
    dtype: Floating point dtype of `alpha`, `beta`, and the output.
    name: A name for the operation (optional).
  Returns:
    samples: A Tensor of the specified shape filled with random gamma values.
      For each i, each `samples[..., i] is an independent draw from the gamma
      distribution with concentration alpha[i] and scale beta[i].
  """
  seed = ops.get_default_graph().seed

  ops.get_default_graph().seed = split(seed,num=1)[0,:]
  with ops.name_scope(name, "stateless_random_gamma",
                      [shape, seed, alpha, beta]) as name:
    shape = tensor_util.shape_tensor(shape)
    alpha = ops.convert_to_tensor(alpha, dtype=dtype, name="alpha")
    beta = ops.convert_to_tensor(
        beta if beta is not None else 1, name="beta", dtype=dtype)
    broadcast_shape = array_ops.broadcast_dynamic_shape(
        array_ops.shape(alpha), array_ops.shape(beta))
    alpha_broadcast = array_ops.broadcast_to(alpha, broadcast_shape)
    result = math_ops.maximum(
        np.finfo(alpha.dtype.as_numpy_dtype).tiny,
        gen_stateless_random_ops.stateless_random_gamma_v2(
            shape, seed=seed, alpha=alpha_broadcast) / beta)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export("random.stateless_poisson")
@dispatch.add_dispatch_support
def stateless_random_poisson(shape,
                             seed,
                             lam,
                             dtype=dtypes.int32,
                             name=None):
  """Outputs deterministic pseudorandom values from a Poisson distribution.

  The generated values follow a Poisson distribution with specified rate
  parameter.

  This is a stateless version of `tf.random.poisson`: if run twice with the same
  seeds and shapes, it will produce the same pseudorandom numbers. The output is
  consistent across multiple runs on the same hardware, but may change between
  versions of TensorFlow or on non-CPU/GPU hardware.

  A slight difference exists in the interpretation of the `shape` parameter
  between `stateless_poisson` and `poisson`: in `poisson`, the `shape` is always
  prepended to the shape of `lam`; whereas in `stateless_poisson` the shape of
  `lam` must match the trailing dimensions of `shape`.

  Example:

  ```python
  samples = tf.random.stateless_poisson([10, 2], seed=[12, 34], lam=[5, 15])
  # samples has shape [10, 2], where each slice [:, 0] and [:, 1] represents
  # the samples drawn from each distribution

  samples = tf.random.stateless_poisson([7, 5, 2], seed=[12, 34], lam=[5, 15])
  # samples has shape [7, 5, 2], where each slice [:, :, 0] and [:, :, 1]
  # represents the 7x5 samples drawn from each of the two distributions

  rate = tf.constant([[1.], [3.], [5.]])
  samples = tf.random.stateless_poisson([30, 3, 1], seed=[12, 34], lam=rate)
  # samples has shape [30, 3, 1], with 30 samples each of 3x1 distributions.
  ```

  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    lam: Tensor. The rate parameter "lambda" of the Poisson distribution. Shape
      must match the rightmost dimensions of `shape`.
    dtype: Dtype of the samples (int or float dtypes are permissible, as samples
      are discrete). Default: int32.
    name: A name for the operation (optional).

  Returns:
    samples: A Tensor of the specified shape filled with random Poisson values.
      For each i, each `samples[..., i]` is an independent draw from the Poisson
      distribution with rate `lam[i]`.

  """
  with ops.name_scope(name, "stateless_random_poisson",
                      [shape, seed, lam]) as name:
    shape = tensor_util.shape_tensor(shape)
    result = gen_stateless_random_ops.stateless_random_poisson(
        shape, seed=seed, lam=lam, dtype=dtype)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export("random.deterministic_poisson")
@dispatch.add_dispatch_support
def deterministic_random_poisson(shape,
                             seed,
                             lam,
                             dtype=dtypes.int32,
                             name=None):
  """Outputs deterministic pseudorandom values from a Poisson distribution.
  The generated values follow a Poisson distribution with specified rate
  parameter.
  This is a stateless version of `tf.random.poisson`: if run twice with the same
  seeds and shapes, it will produce the same pseudorandom numbers. The output is
  consistent across multiple runs on the same hardware, but may change between
  versions of TensorFlow or on non-CPU/GPU hardware.
  A slight difference exists in the interpretation of the `shape` parameter
  between `stateless_poisson` and `poisson`: in `poisson`, the `shape` is always
  prepended to the shape of `lam`; whereas in `stateless_poisson` the shape of
  `lam` must match the trailing dimensions of `shape`.
  Example:
  ```python
  samples = tf.random.stateless_poisson([10, 2], seed=[12, 34], lam=[5, 15])
  # samples has shape [10, 2], where each slice [:, 0] and [:, 1] represents
  # the samples drawn from each distribution
  samples = tf.random.stateless_poisson([7, 5, 2], seed=[12, 34], lam=[5, 15])
  # samples has shape [7, 5, 2], where each slice [:, :, 0] and [:, :, 1]
  # represents the 7x5 samples drawn from each of the two distributions
  rate = tf.constant([[1.], [3.], [5.]])
  samples = tf.random.stateless_poisson([30, 3, 1], seed=[12, 34], lam=rate)
  # samples has shape [30, 3, 1], with 30 samples each of 3x1 distributions.
  ```
  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    lam: Tensor. The rate parameter "lambda" of the Poisson distribution. Shape
      must match the rightmost dimensions of `shape`.
    dtype: Dtype of the samples (int or float dtypes are permissible, as samples
      are discrete). Default: int32.
    name: A name for the operation (optional).
  Returns:
    samples: A Tensor of the specified shape filled with random Poisson values.
      For each i, each `samples[..., i]` is an independent draw from the Poisson
      distribution with rate `lam[i]`.
  """
  seed=ops.get_default_graph().seed

  ops.get_default_graph().seed = split(seed,num=1)[0,:]
  with ops.name_scope(name, "stateless_random_poisson",
                      [shape, seed, lam]) as name:
    shape = tensor_util.shape_tensor(shape)
    result = gen_stateless_random_ops.stateless_random_poisson(
        shape, seed=seed, lam=lam, dtype=dtype)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export("random.stateless_normal")
@dispatch.add_dispatch_support
def stateless_random_normal(shape,
                            seed,
                            mean=0.0,
                            stddev=1.0,
                            dtype=dtypes.float32,
                            name=None,
                            alg="auto_select"):
  """Outputs deterministic pseudorandom values from a normal distribution.

  This is a stateless version of `tf.random.normal`: if run twice with the
  same seeds and shapes, it will produce the same pseudorandom numbers.  The
  output is consistent across multiple runs on the same hardware (and between
  CPU and GPU), but may change between versions of TensorFlow or on non-CPU/GPU
  hardware.

  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    mean: A 0-D Tensor or Python value of type `dtype`. The mean of the normal
      distribution.
    stddev: A 0-D Tensor or Python value of type `dtype`. The standard deviation
      of the normal distribution.
    dtype: The type of the output.
    name: A name for the operation (optional).
    alg: The RNG algorithm used to generate the random numbers. See
      `tf.random.stateless_uniform` for a detailed explanation.

  Returns:
    A tensor of the specified shape filled with random normal values.
  """
  with ops.name_scope(name, "stateless_random_normal",
                      [shape, seed, mean, stddev]) as name:
    shape = tensor_util.shape_tensor(shape)
    mean = ops.convert_to_tensor(mean, dtype=dtype, name="mean")
    stddev = ops.convert_to_tensor(stddev, dtype=dtype, name="stddev")
    key, counter, alg = _get_key_counter_alg(seed, alg)
    rnd = gen_stateless_random_ops_v2.stateless_random_normal_v2(
        shape, key=key, counter=counter, dtype=dtype, alg=alg)
    result = math_ops.add(rnd * stddev, mean, name=name)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export("random.deterministic_normal")
@dispatch.add_dispatch_support
def deterministic_random_normal(shape,
                            mean=0.0,
                            stddev=1.0,
                            dtype=dtypes.float32,
                            name=None):
  """Outputs deterministic pseudorandom values from a normal distribution.
  This is a stateless version of `tf.random.normal`: if run twice with the
  same seeds and shapes, it will produce the same pseudorandom numbers.  The
  output is consistent across multiple runs on the same hardware (and between
  CPU and GPU), but may change between versions of TensorFlow or on non-CPU/GPU
  hardware.
  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    mean: A 0-D Tensor or Python value of type `dtype`. The mean of the normal
      distribution.
    stddev: A 0-D Tensor or Python value of type `dtype`. The standard deviation
      of the normal distribution.
    dtype: The type of the output.
    name: A name for the operation (optional).
  Returns:
    A tensor of the specified shape filled with random normal values.
  """
  seed = ops.get_default_graph().seed

  ops.get_default_graph().seed = split(seed,num=1)[0,:]
  with ops.name_scope(name, "stateless_random_normal",
                      [shape, seed, mean, stddev]) as name:
    shape = tensor_util.shape_tensor(shape)
    mean = ops.convert_to_tensor(mean, dtype=dtype, name="mean")
    stddev = ops.convert_to_tensor(stddev, dtype=dtype, name="stddev")
    key, counter, alg = _get_key_counter_alg(seed, alg)
    rnd = gen_stateless_random_ops_v2.stateless_random_normal_v2(
        shape, key=key, counter=counter, dtype=dtype, alg=alg)
    result = math_ops.add(rnd * stddev, mean, name=name)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export("random.stateless_truncated_normal")
@dispatch.add_dispatch_support
def stateless_truncated_normal(shape,
                               seed,
                               mean=0.0,
                               stddev=1.0,
                               dtype=dtypes.float32,
                               name=None,
                               alg="auto_select"):
  """Outputs deterministic pseudorandom values, truncated normally distributed.

  This is a stateless version of `tf.random.truncated_normal`: if run twice with
  the same seeds and shapes, it will produce the same pseudorandom numbers.  The
  output is consistent across multiple runs on the same hardware (and between
  CPU and GPU), but may change between versions of TensorFlow or on non-CPU/GPU
  hardware.

  The generated values follow a normal distribution with specified mean and
  standard deviation, except that values whose magnitude is more than 2 standard
  deviations from the mean are dropped and re-picked.

  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    mean: A 0-D Tensor or Python value of type `dtype`. The mean of the
      truncated normal distribution.
    stddev: A 0-D Tensor or Python value of type `dtype`. The standard deviation
      of the normal distribution, before truncation.
    dtype: The type of the output.
    name: A name for the operation (optional).
    alg: The RNG algorithm used to generate the random numbers. See
      `tf.random.stateless_uniform` for a detailed explanation.

  Returns:
    A tensor of the specified shape filled with random truncated normal values.
  """
  with ops.name_scope(name, "stateless_truncated_normal",
                      [shape, seed, mean, stddev]) as name:
    shape = tensor_util.shape_tensor(shape)
    mean = ops.convert_to_tensor(mean, dtype=dtype, name="mean")
    stddev = ops.convert_to_tensor(stddev, dtype=dtype, name="stddev")
    key, counter, alg = _get_key_counter_alg(seed, alg)
    rnd = gen_stateless_random_ops_v2.stateless_truncated_normal_v2(
        shape, key=key, counter=counter, dtype=dtype, alg=alg)
    result = math_ops.add(rnd * stddev, mean, name=name)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export("random.deterministic_truncated_normal")
@dispatch.add_dispatch_support
def deterministic_truncated_normal(shape,
                               mean=0.0,
                               stddev=1.0,
                               dtype=dtypes.float32,
                               name=None):
  """Outputs deterministic pseudorandom values, truncated normally distributed.
  This is a stateless version of `tf.random.truncated_normal`: if run twice with
  the same seeds and shapes, it will produce the same pseudorandom numbers.  The
  output is consistent across multiple runs on the same hardware (and between
  CPU and GPU), but may change between versions of TensorFlow or on non-CPU/GPU
  hardware.
  The generated values follow a normal distribution with specified mean and
  standard deviation, except that values whose magnitude is more than 2 standard
  deviations from the mean are dropped and re-picked.
  Args:
    shape: A 1-D integer Tensor or Python array. The shape of the output tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    mean: A 0-D Tensor or Python value of type `dtype`. The mean of the
      truncated normal distribution.
    stddev: A 0-D Tensor or Python value of type `dtype`. The standard deviation
      of the normal distribution, before truncation.
    dtype: The type of the output.
    name: A name for the operation (optional).
  Returns:
    A tensor of the specified shape filled with random truncated normal values.
  """
  seed = ops.get_default_graph().seed

  ops.get_default_graph().seed = split(seed,num=1)[0,:]

  with ops.name_scope(name, "stateless_truncated_normal",
                      [shape, seed, mean, stddev]) as name:
    shape = tensor_util.shape_tensor(shape)
    mean = ops.convert_to_tensor(mean, dtype=dtype, name="mean")
    stddev = ops.convert_to_tensor(stddev, dtype=dtype, name="stddev")
    key, counter, alg = _get_key_counter_alg(seed, alg)
    rnd = gen_stateless_random_ops_v2.stateless_truncated_normal_v2(
        shape, key=key, counter=counter, dtype=dtype, alg=alg)
    result = math_ops.add(rnd * stddev, mean, name=name)
    tensor_util.maybe_set_static_shape(result, shape)
    return result

@tf_export(v1=["random.stateless_multinomial"])
@dispatch.add_dispatch_support
@deprecation.deprecated(
    date=None, instructions="Use `tf.random.stateless_categorical` instead.")
def stateless_multinomial(logits,
                          num_samples,
                          seed,
                          output_dtype=dtypes.int64,
                          name=None):
  """Draws deterministic pseudorandom samples from a multinomial distribution.

  This is a stateless version of `tf.random.categorical`: if run twice with the
  same seeds and shapes, it will produce the same pseudorandom numbers.  The
  output is consistent across multiple runs on the same hardware (and between
  CPU and GPU), but may change between versions of TensorFlow or on non-CPU/GPU
  hardware.

  Example:

  ```python
  # samples has shape [1, 5], where each value is either 0 or 1 with equal
  # probability.
  samples = tf.random.stateless_categorical(
      tf.math.log([[0.5, 0.5]]), 5, seed=[7, 17])
  ```

  Args:
    logits: 2-D Tensor with shape `[batch_size, num_classes]`.  Each slice
      `[i, :]` represents the unnormalized log-probabilities for all classes.
    num_samples: 0-D.  Number of independent samples to draw for each row slice.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    output_dtype: integer type to use for the output. Defaults to int64.
    name: Optional name for the operation.

  Returns:
    The drawn samples of shape `[batch_size, num_samples]`.
  """
  with ops.name_scope(name, "stateless_multinomial", [logits, seed]):
    return stateless_multinomial_categorical_impl(logits, num_samples,
                                                  output_dtype, seed)


@tf_export("random.stateless_categorical")
@dispatch.add_dispatch_support
def stateless_categorical(logits,
                          num_samples,
                          seed,
                          dtype=dtypes.int64,
                          name=None):
  """Draws deterministic pseudorandom samples from a categorical distribution.

  This is a stateless version of `tf.categorical`: if run twice with the
  same seeds and shapes, it will produce the same pseudorandom numbers.  The
  output is consistent across multiple runs on the same hardware (and between
  CPU and GPU), but may change between versions of TensorFlow or on non-CPU/GPU
  hardware.


  Example:

  ```python
  # samples has shape [1, 5], where each value is either 0 or 1 with equal
  # probability.
  samples = tf.random.stateless_categorical(
      tf.math.log([[0.5, 0.5]]), 5, seed=[7, 17])
  ```

  Args:
    logits: 2-D Tensor with shape `[batch_size, num_classes]`.  Each slice
      `[i, :]` represents the unnormalized log-probabilities for all classes.
    num_samples: 0-D.  Number of independent samples to draw for each row slice.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    dtype: integer type to use for the output. Defaults to int64.
    name: Optional name for the operation.

  Returns:
    The drawn samples of shape `[batch_size, num_samples]`.
  """
  with ops.name_scope(name, "stateless_categorical", [logits, seed]):
    return stateless_multinomial_categorical_impl(logits, num_samples, dtype,
                                                  seed)


def stateless_multinomial_categorical_impl(logits, num_samples, dtype, seed):
  """Implementation for stateless multinomial/categorical ops (v1/v2)."""
  logits = ops.convert_to_tensor(logits, name="logits")
  return gen_stateless_random_ops.stateless_multinomial(
      logits, num_samples, seed, output_dtype=dtype)

@tf_export("random.deterministic_categorical")
@dispatch.add_dispatch_support
def deterministic_categorical(logits,
                          num_samples,
                          dtype=dtypes.int64,
                          name=None):
  """Draws deterministic pseudorandom samples from a categorical distribution.
  This is a stateless version of `tf.categorical`: if run twice with the
  same seeds and shapes, it will produce the same pseudorandom numbers.  The
  output is consistent across multiple runs on the same hardware (and between
  CPU and GPU), but may change between versions of TensorFlow or on non-CPU/GPU
  hardware.
  Example:
  ```python
  # samples has shape [1, 5], where each value is either 0 or 1 with equal
  # probability.
  samples = tf.random.stateless_categorical(
      tf.math.log([[0.5, 0.5]]), 5, seed=[7, 17])
  ```
  Args:
    logits: 2-D Tensor with shape `[batch_size, num_classes]`.  Each slice
      `[i, :]` represents the unnormalized log-probabilities for all classes.
    num_samples: 0-D.  Number of independent samples to draw for each row slice.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    dtype: integer type to use for the output. Defaults to int64.
    name: Optional name for the operation.
  Returns:
    The drawn samples of shape `[batch_size, num_samples]`.
  """
  seed=ops.get_default_graph().seed

  ops.get_default_graph().seed = split(seed,num=1)[0,:]
  with ops.name_scope(name, "stateless_categorical", [logits, seed]):
    return stateless_multinomial_categorical_impl(logits, num_samples, dtype,
                                                  seed)

@dispatch.add_dispatch_support
@tf_export("random.stateless_parameterized_truncated_normal")
def stateless_parameterized_truncated_normal(shape,
                                             seed,
                                             means=0.0,
                                             stddevs=1.0,
                                             minvals=-2.0,
                                             maxvals=2.0,
                                             name=None):
  """Outputs random values from a truncated normal distribution.

  The generated values follow a normal distribution with specified mean and
  standard deviation, except that values whose magnitude is more than 2 standard
  deviations from the mean are dropped and re-picked.


  Examples:

  Sample from a Truncated normal, with deferring shape parameters that
  broadcast.

  >>> means = 0.
  >>> stddevs = tf.math.exp(tf.random.uniform(shape=[2, 3]))
  >>> minvals = [-1., -2., -1000.]
  >>> maxvals = [[10000.], [1.]]
  >>> y = tf.random.stateless_parameterized_truncated_normal(
  ...   shape=[10, 2, 3], seed=[7, 17],
  ...   means=means, stddevs=stddevs, minvals=minvals, maxvals=maxvals)
  >>> y.shape
  TensorShape([10, 2, 3])

  Args:
    shape: A 1-D integer `Tensor` or Python array. The shape of the output
      tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    means: A `Tensor` or Python value of type `dtype`. The mean of the truncated
      normal distribution. This must broadcast with `stddevs`, `minvals` and
      `maxvals`, and the broadcasted shape must be dominated by `shape`.
    stddevs: A `Tensor` or Python value of type `dtype`. The standard deviation
      of the truncated normal distribution. This must broadcast with `means`,
      `minvals` and `maxvals`, and the broadcasted shape must be dominated by
      `shape`.
    minvals: A `Tensor` or Python value of type `dtype`. The minimum value of
      the truncated normal distribution. This must broadcast with `means`,
      `stddevs` and `maxvals`, and the broadcasted shape must be dominated by
      `shape`.
    maxvals: A `Tensor` or Python value of type `dtype`. The maximum value of
      the truncated normal distribution. This must broadcast with `means`,
      `stddevs` and `minvals`, and the broadcasted shape must be dominated by
      `shape`.
    name: A name for the operation (optional).

  Returns:
    A tensor of the specified shape filled with random truncated normal values.
  """
  with ops.name_scope(name, "stateless_parameterized_truncated_normal",
                      [shape, means, stddevs, minvals, maxvals]) as name:
    shape_tensor = tensor_util.shape_tensor(shape)
    means_tensor = ops.convert_to_tensor(means, name="means")
    stddevs_tensor = ops.convert_to_tensor(stddevs, name="stddevs")
    minvals_tensor = ops.convert_to_tensor(minvals, name="minvals")
    maxvals_tensor = ops.convert_to_tensor(maxvals, name="maxvals")
    rnd = gen_stateless_random_ops.stateless_parameterized_truncated_normal(
        shape_tensor, seed, means_tensor, stddevs_tensor, minvals_tensor,
        maxvals_tensor)
    tensor_util.maybe_set_static_shape(rnd, shape)
    return rnd


@dispatch.add_dispatch_support
@tf_export("random.deterministic_parameterized_truncated_normal")
def deterministic_parameterized_truncated_normal(shape,
                                             means=0.0,
                                             stddevs=1.0,
                                             minvals=-2.0,
                                             maxvals=2.0,
                                             name=None):
  """Outputs random values from a truncated normal distribution.
  The generated values follow a normal distribution with specified mean and
  standard deviation, except that values whose magnitude is more than 2 standard
  deviations from the mean are dropped and re-picked.
  Examples:
  Sample from a Truncated normal, with deferring shape parameters that
  broadcast.
  >>> means = 0.
  >>> stddevs = tf.math.exp(tf.random.uniform(shape=[2, 3]))
  >>> minvals = [-1., -2., -1000.]
  >>> maxvals = [[10000.], [1.]]
  >>> y = tf.random.stateless_parameterized_truncated_normal(
  ...   shape=[10, 2, 3], seed=[7, 17],
  ...   means=means, stddevs=stddevs, minvals=minvals, maxvals=maxvals)
  >>> y.shape
  TensorShape([10, 2, 3])
  Args:
    shape: A 1-D integer `Tensor` or Python array. The shape of the output
      tensor.
    seed: A shape [2] Tensor, the seed to the random number generator. Must have
      dtype `int32` or `int64`. (When using XLA, only `int32` is allowed.)
    means: A `Tensor` or Python value of type `dtype`. The mean of the truncated
      normal distribution. This must broadcast with `stddevs`, `minvals` and
      `maxvals`, and the broadcasted shape must be dominated by `shape`.
    stddevs: A `Tensor` or Python value of type `dtype`. The standard deviation
      of the truncated normal distribution. This must broadcast with `means`,
      `minvals` and `maxvals`, and the broadcasted shape must be dominated by
      `shape`.
    minvals: A `Tensor` or Python value of type `dtype`. The minimum value of
      the truncated normal distribution. This must broadcast with `means`,
      `stddevs` and `maxvals`, and the broadcasted shape must be dominated by
      `shape`.
    maxvals: A `Tensor` or Python value of type `dtype`. The maximum value of
      the truncated normal distribution. This must broadcast with `means`,
      `stddevs` and `minvals`, and the broadcasted shape must be dominated by
      `shape`.
    name: A name for the operation (optional).
  Returns:
    A tensor of the specified shape filled with random truncated normal values.
  """
  seed=ops.get_default_graph().seed

  ops.get_default_graph().seed = split(seed,num=1)[0,:]
  with ops.name_scope(name, "stateless_parameterized_truncated_normal",
                      [shape, means, stddevs, minvals, maxvals]) as name:
    shape_tensor = tensor_util.shape_tensor(shape)
    means_tensor = ops.convert_to_tensor(means, name="means")
    stddevs_tensor = ops.convert_to_tensor(stddevs, name="stddevs")
    minvals_tensor = ops.convert_to_tensor(minvals, name="minvals")
    maxvals_tensor = ops.convert_to_tensor(maxvals, name="maxvals")
    rnd = gen_stateless_random_ops.stateless_parameterized_truncated_normal(
        shape_tensor, seed, means_tensor, stddevs_tensor, minvals_tensor,
        maxvals_tensor)
    tensor_util.maybe_set_static_shape(rnd, shape)
    return rnd

