#   Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

from __future__ import print_function
import struct

from ... import core
from ... import framework
from ... import layers
from ... import global_scope
from ...log_helper import get_logger
from ...wrapped_decorator import signature_safe_contextmanager
from .bf16_lists import AutoMixedPrecisionListsBF16
import collections
import logging
import numpy as np

__all__ = [
    "bf16_guard", "cast_model_to_bf16", "convert_float_to_uint16",
    "convert_uint16_to_float"
]

_logger = get_logger(
    __name__, logging.INFO, fmt='%(asctime)s-%(levelname)s: %(message)s')

_valid_types = [
    core.VarDesc.VarType.LOD_TENSOR, core.VarDesc.VarType.SELECTED_ROWS,
    core.VarDesc.VarType.LOD_TENSOR_ARRAY
]

_bf16_guard_pattern = "__use_bf16__"


def convert_float_to_uint16(in_list):
    in_list = np.asarray(in_list)
    out = np.vectorize(
        lambda x: struct.unpack('<I', struct.pack('<f', x))[0] >> 16,
        otypes=[np.uint16])(in_list.flat)
    return np.reshape(out, in_list.shape)


def convert_uint16_to_float(in_list):
    in_list = np.asarray(in_list)
    out = np.vectorize(
        lambda x: struct.unpack('<f', struct.pack('<I', x << 16))[0],
        otypes=[np.float32])(in_list.flat)
    return np.reshape(out, in_list.shape)


def _rename_arg(op, old_name, new_name):
    """
    If an op has old_name input and output, rename these input
    args new_name.

    Args:
        op (Operator): Current operator.
        old_name (str): The old name of input args.
        new_name (str): The new name of input args.
    """
    op_desc = op.desc
    if isinstance(op_desc, tuple):
        op_desc = op_desc[0]
    op_desc._rename_input(old_name, new_name)
    op_desc._rename_output(old_name, new_name)


def _rename_op_input(program, op_var_rename_map, origin_ops, keep_fp32_ops):
    for block in program.blocks:
        ops = block.ops
        block_id = block.idx
        for op in ops:
            if op not in origin_ops or op in keep_fp32_ops:
                continue
            for name in op.input_arg_names:
                if name in op_var_rename_map[block_id]:
                    op._rename_input(name, op_var_rename_map[block_id][name])


def _dtype_to_str(dtype):
    """
    Convert specific variable type to its corresponding string.

    Args:
        dtype (VarType): Variable type.
    """
    if dtype == core.VarDesc.VarType.BF16:
        return 'bf16'
    else:
        return 'fp32'


def _insert_cast_op(block, op, idx, src_dtype, dest_dtype):
    """
    Insert cast op and rename args of input and output.

    Args:
        block (Program): The block in which the operator is.
        op (Operator): The operator to insert cast op.
        idx (int): The index of current operator.
        src_dtype (VarType): The input variable dtype of cast op.
        dest_dtype (VarType): The output variable dtype of cast op.

    Returns:
        num_cast_op (int): The number of cast ops that have been inserted.
    """
    num_cast_ops = 0

    for in_name in op.input_names:
        if src_dtype == core.VarDesc.VarType.FP32 and op.type in [
                'batch_norm', 'fused_bn_add_activation', 'layer_norm'
        ]:
            if in_name not in {'X', 'Z'}:
                continue
        for in_var_name in op.input(in_name):
            in_var = block.var(in_var_name)
            if in_var.type not in _valid_types or in_var.dtype == dest_dtype:
                continue
            if in_var.dtype == src_dtype:
                cast_name = in_var.name + '.cast_' + _dtype_to_str(dest_dtype)
                out_var = block.vars.get(cast_name)
                if out_var is None or out_var.dtype != dest_dtype:
                    out_var = block.create_var(
                        name=cast_name,
                        dtype=dest_dtype,
                        persistable=False,
                        stop_gradient=in_var.stop_gradient)

                    block._insert_op(
                        idx,
                        type="cast",
                        inputs={"X": in_var},
                        outputs={"Out": out_var},
                        attrs={
                            "in_dtype": in_var.dtype,
                            "out_dtype": out_var.dtype
                        })
                    num_cast_ops += 1
                _rename_arg(op, in_var.name, out_var.name)
            else:
                if op.has_attr('in_dtype'):
                    op._set_attr('in_dtype', dest_dtype)
    if src_dtype == core.VarDesc.VarType.FP32 and dest_dtype == core.VarDesc.VarType.BF16:
        for out_name in op.output_names:
            if op.type in [
                    'batch_norm', 'fused_bn_add_activation', 'layer_norm'
            ] and out_name != 'Y':
                continue
            for out_var_name in op.output(out_name):
                out_var = block.var(out_var_name)
                if out_var.type not in _valid_types:
                    continue
                if out_var.dtype == core.VarDesc.VarType.FP32:
                    out_var.desc.set_dtype(core.VarDesc.VarType.BF16)
                    if op.has_attr('out_dtype'):
                        op._set_attr('out_dtype', core.VarDesc.VarType.BF16)
    return num_cast_ops


def _insert_cast_post_op(block, op, idx, src_dtype, dest_dtype, target_name,
                         op_var_rename_map):
    num_cast_ops = 0

    target_var = block.var(target_name)
    if target_var.type not in _valid_types or target_var.dtype == dest_dtype:
        return num_cast_ops

    assert target_var.dtype == src_dtype, \
        "The real dtype({}) is not equal to the src dtype({})".format(_dtype_to_str(target_var.dtype), _dtype_to_str(src_dtype))

    cast_name = target_var.name + '.cast_' + _dtype_to_str(dest_dtype)
    cast_var = block.vars.get(cast_name)
    if cast_var is None or cast_var.dtype != dest_dtype:
        cast_var = block.create_var(
            name=cast_name,
            dtype=dest_dtype,
            persistable=False,
            stop_gradient=target_var.stop_gradient)
        block._insert_op(
            idx,
            type="cast",
            inputs={"X": target_var},
            outputs={"Out": cast_var},
            attrs={"in_dtype": target_var.dtype,
                   "out_dtype": cast_var.dtype})
        num_cast_ops += 1
        op_var_rename_map[block.idx][target_var.name] = cast_var.name

    return num_cast_ops


def find_true_post_op(ops, cur_op, var_name):
    """
    if there are post ops, return them, if there is no post op,
    return None instead.
    Args:
        ops (list): A list of ops.
        cur_op (Operator): Current operator which has var_name variable.
        var_name (string): Variable name.
    """
    post_op = []
    for idx, op in enumerate(ops):
        if op == cur_op:
            break

    for i in range(idx + 1, len(ops)):
        op = ops[i]
        for in_name in op.input_names:
            for in_var_name in op.input(in_name):
                if in_var_name == var_name:
                    post_op.append(op)

    return post_op


def find_op_index(block_desc, cur_op_desc):
    """
    """
    for idx in range(block_desc.op_size()):
        if cur_op_desc == block_desc.op(idx):
            return idx
    return -1


def _need_keep_fp32(op, unsupported_op_list, use_bf16_guard):
    if op.type in unsupported_op_list:
        # the highest priority condition: If ops don't have bf16 computing kernels,
        # they must be executed in fp32 calculation pattern.
        return True

    # process ops about learning rate
    in_out_arg_names = []
    in_out_arg_names.extend(list(op.input_arg_names))
    in_out_arg_names.extend(list(op.output_arg_names))
    for name in in_out_arg_names:
        if "learning_rate" in name:
            return True

    if use_bf16_guard:
        if op.has_attr("op_namescope") and \
                (_bf16_guard_pattern in op.attr("op_namescope")):
            # op in bf16 guard
            return False
        else:
            # op not in bf16 guard
            return True
    else:
        return False


@signature_safe_contextmanager
def bf16_guard():
    """
    As for the pure bf16 training, if users set `use_bf16_guard` to True,
    only those ops created in the context manager `bf16_guard` will be
    transformed as float16 type.

    Examples:
        .. code-block:: python

            import numpy as np
            import paddle
            import paddle.nn.functional as F
            paddle.enable_static()
            data = paddle.static.data(name='X', shape=[None, 1, 28, 28], dtype='float32')
            conv2d = paddle.static.nn.conv2d(input=data, num_filters=6, filter_size=3)

            with paddle.static.amp.bf16_guard():
                bn = paddle.static.nn.batch_norm(input=conv2d, act="relu")
                pool = F.max_pool2d(bn, kernel_size=2, stride=2)
                hidden = paddle.static.nn.fc(pool, size=10)
                loss = paddle.mean(hidden)
    """
    with framework.name_scope(prefix=_bf16_guard_pattern):
        yield


def cast_model_to_bf16(program, amp_lists=None, use_bf16_guard=True):
    """
    Traverse all ops in the whole model and set their inputs and outputs
    to the bf16 data type. This function will do some special process for
    the batch normalization, which keeps the computational process of
    batchnorms in FP32.
    Args:
        program (Program): The used program.
        amp_lists (AutoMixedPrecisionListsBF16): An AutoMixedPrecisionListsBF16 object.
        use_bf16_guard(bool): Determine whether to use `bf16_guard` when
                              constructing the program. Default True.
    """

    if amp_lists is None:
        amp_lists = AutoMixedPrecisionListsBF16()
    global_block = program.global_block()
    keep_fp32_ops = set()
    to_bf16_var_names = set()
    to_bf16_pre_cast_ops = set()
    origin_ops = []
    for block in program.blocks:
        origin_ops.extend(block.ops)

    for block in program.blocks:
        ops = block.ops
        for op in ops:
            if op.type == 'create_py_reader' or op.type == 'read':
                continue
            if _need_keep_fp32(op, amp_lists.unsupported_list, use_bf16_guard):
                keep_fp32_ops.add(op)
                continue  # processed below
            for in_name in op.input_names:
                if op.type in {
                        'batch_norm', 'fused_bn_add_activation', 'layer_norm'
                } and in_name not in {'X', 'Z'}:
                    continue
                for in_var_name in op.input(in_name):
                    in_var = None
                    try:
                        in_var = block.var(in_var_name)
                    except ValueError as e:
                        _logger.debug(
                            "-- {}, try to get it in the global block --".
                            format(e))
                        in_var = global_block.var(in_var_name)
                        if in_var is not None:
                            _logger.debug(
                                "-- var {} is got in the global block --".
                                format(in_var_name))

                    if in_var is None or in_var.type not in _valid_types:
                        continue

                    if in_var.dtype == core.VarDesc.VarType.FP32:
                        if in_var.is_data:
                            to_bf16_pre_cast_ops.add(op)
                        else:
                            in_var.desc.set_dtype(core.VarDesc.VarType.BF16)
                            to_bf16_var_names.add(in_var_name)

                    _logger.debug(
                        "-- op type: {}, in var name: {}, in var dtype: {} --".
                        format(op.type, in_var_name, in_var.dtype))

            for out_name in op.output_names:
                if op.type in {
                        'batch_norm', 'fused_bn_add_activation', 'layer_norm'
                } and out_name != 'Y':
                    continue
                for out_var_name in op.output(out_name):
                    out_var = None
                    try:
                        out_var = block.var(out_var_name)
                    except ValueError as e:
                        _logger.debug(
                            "-- {}, try to get it in the global block --".
                            format(e))
                        out_var = global_block.var(out_var_name)
                        if out_var is not None:
                            _logger.debug(
                                "-- var {} is got in the global block --".
                                format(out_var_name))

                    if out_var is None or out_var.type not in _valid_types:
                        continue

                    if out_var.dtype == core.VarDesc.VarType.FP32:
                        out_var.desc.set_dtype(core.VarDesc.VarType.BF16)

                    _logger.debug(
                        "-- op type: {}, out var name: {}, out var dtype: {} --".
                        format(op.type, out_var_name, out_var.dtype))
            if op.has_attr('in_dtype') and op.attr(
                    'in_dtype') == core.VarDesc.VarType.FP32:
                op._set_attr('in_dtype', core.VarDesc.VarType.BF16)
            if op.has_attr('out_dtype') and op.attr(
                    'out_dtype') == core.VarDesc.VarType.FP32:
                op._set_attr('out_dtype', core.VarDesc.VarType.BF16)
            if op.has_attr('dtype') and op.attr(
                    'dtype') == core.VarDesc.VarType.FP32:
                op._set_attr('dtype', core.VarDesc.VarType.BF16)
            if op.has_attr('use_mkldnn'):
                op._set_attr('use_mkldnn', True)
                op._set_attr('mkldnn_data_type', 'bfloat16')

    # process ops in keep_fp32_ops
    op_var_rename_map = [
        collections.OrderedDict() for _ in range(len(program.blocks))
    ]
    for block in program.blocks:
        ops = block.ops
        idx = 0
        while idx < len(ops):
            op = ops[idx]
            num_cast_ops = 0
            if op not in keep_fp32_ops:
                if op in to_bf16_pre_cast_ops:
                    in_var_cast_num = _insert_cast_op(block, op, idx,
                                                      core.VarDesc.VarType.FP32,
                                                      core.VarDesc.VarType.BF16)
                    num_cast_ops += in_var_cast_num
            else:
                pre_cast_num = _insert_cast_op(block, op, idx,
                                               core.VarDesc.VarType.BF16,
                                               core.VarDesc.VarType.FP32)
                num_cast_ops += pre_cast_num
                for out_var_name in op.output_arg_names:
                    out_var = block.vars.get(out_var_name)
                    if out_var is None or out_var.type not in _valid_types:
                        continue
                    if out_var.dtype == core.VarDesc.VarType.BF16:
                        out_var.desc.set_dtype(core.VarDesc.VarType.FP32)
                        post_ops = find_true_post_op(ops, op, out_var_name)
                        for post_op in post_ops:
                            if post_op in keep_fp32_ops:
                                continue
                            post_cast_num = _insert_cast_post_op(
                                block, op, idx + pre_cast_num + 1,
                                core.VarDesc.VarType.FP32,
                                core.VarDesc.VarType.BF16, out_var_name,
                                op_var_rename_map)
                            num_cast_ops += post_cast_num
            idx += num_cast_ops + 1

    _rename_op_input(program, op_var_rename_map, origin_ops, keep_fp32_ops)
    return to_bf16_var_names
