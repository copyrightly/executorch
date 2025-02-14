# Copyright (c) Qualcomm Innovation Center, Inc.
# All rights reserved
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
from collections import Counter
from typing import Callable, List

import torch
from executorch.backends.transforms.addmm_mm_to_linear import (
    apply_addmm_mm_to_linear_transform,
)
from executorch.exir.dialects._ops import ops as exir_ops
from executorch.exir.dialects.edge._ops import EdgeOpOverload as edge_op
from executorch.exir.pass_base import ExportPass, PassResult
from executorch.exir.passes import dead_code_elimination_pass
from torch.fx.passes.utils.source_matcher_utils import (
    get_source_partitions,
    SourcePartition,
)

from .utils import dq_ops, q_ops


class ConvertToLinear(ExportPass):
    """
    Handle missing quantization tag for addmm op after decomposing
    """

    view_copy = exir_ops.edge.aten.view_copy.default
    permute_copy = exir_ops.edge.aten.permute_copy.default
    expand_copy = exir_ops.edge.aten.expand_copy.default
    linear = exir_ops.edge.aten.linear.default
    add = exir_ops.edge.aten.add.Tensor
    addmm = exir_ops.edge.aten.addmm.default
    bmm = exir_ops.edge.aten.bmm.default
    mm = exir_ops.edge.aten.mm.default

    addmm_patterns = [
        {view_copy: 2, permute_copy: 1, addmm: 1},
        {permute_copy: 1, addmm: 1},
    ]

    bmm_patterns = [
        {view_copy: 3, permute_copy: 1, expand_copy: 2, add: 1, bmm: 1},
    ]

    mm_patterns = [
        {view_copy: 2, permute_copy: 1, mm: 1},
        {permute_copy: 1, mm: 1},
    ]

    def __init__(self):
        super(ConvertToLinear, self).__init__()

    def _get_original_input(
        self, inputs: List[torch.fx.Node], cur_node: torch.fx.Node
    ) -> torch.fx.Node:
        while cur_node not in inputs and cur_node.args:
            cur_node = cur_node.args[0]
        return cur_node

    def _annotate_quant_attrs(
        self, gm: torch.fx.GraphModule, node: torch.fx.Node, q_node: torch.fx.Node
    ) -> torch.fx.Node:
        quant_attr_keys = [arg.name for arg in q_node.target._schema.arguments][1:]
        quant_attrs = dict.fromkeys(quant_attr_keys)

        for i in range(1, len(q_node.args)):
            attr_n = q_node.args[i]
            value = attr_n
            if type(attr_n) == torch.fx.node.Node:
                value = getattr(gm, attr_n.target)
            quant_attrs[quant_attr_keys[i - 1]] = value
        quant_attrs["encoding"] = q_node.target
        node.meta["quant_attrs"] = quant_attrs
        return node

    def _convert_to_linear(
        self,
        gm: torch.fx.GraphModule,
        src_partition: SourcePartition,
        extract_ops_fn: Callable,
    ):
        inputs = src_partition.input_nodes
        # output_nodes contains output node and input buffer such as argX_X
        outputs = [
            node
            for node in src_partition.output_nodes
            if node.target != torch.ops.aten.sym_size.int and node.op != "placeholder"
        ]
        assert (
            len(outputs) == 1
        ), f"Unexpected number of outputs for a torch.nn.Linear module, expecting 1 but got {outputs}"
        output = outputs[0]

        ops = extract_ops_fn(src_partition.nodes)
        input_node, weight_node, fn_node = ops[:3]
        bias_node = None if len(ops) == 3 else ops[3]

        # qnn htp does not support keepdim, the view_copy(reshape) should exist for now
        if self._get_original_input(inputs, input_node).target in dq_ops:
            input_node = self._annotate_quant_attrs(
                gm, input_node, self._get_original_input(inputs, input_node).args[0]
            )
        args = [input_node, weight_node]
        if bias_node:
            args.append(bias_node)

        with gm.graph.inserting_before(output):
            linear_node = gm.graph.create_node(
                "call_function", self.linear, tuple(args)
            )
            linear_node.meta = fn_node.meta
            if list(output.users)[0].target in q_ops:
                linear_node = self._annotate_quant_attrs(
                    gm, linear_node, list(output.users)[0]
                )
            for user in fn_node.users.copy():
                user.replace_input_with(fn_node, linear_node)

    def _extract_mm_ops(self, partitioned_nodes: List[edge_op]) -> List[torch.fx.Node]:
        mm_node = [n for n in partitioned_nodes if n.target == self.mm][0]
        # weight -> permute -> input of mm
        weight_node = mm_node.args[1].args[0]
        input_node = mm_node.args[0]
        return [input_node, weight_node, mm_node]

    def _extract_addmm_ops(
        self, partitioned_nodes: List[edge_op]
    ) -> List[torch.fx.Node]:
        addmm_node = [n for n in partitioned_nodes if n.target == self.addmm][0]
        # weight -> permute -> input of addmm
        weight_node = addmm_node.args[2].args[0]
        input_node = addmm_node.args[1]
        bias_node = addmm_node.args[0]
        return [input_node, weight_node, addmm_node, bias_node]

    def _extract_bmm_ops(self, partitioned_nodes: List[edge_op]) -> List[torch.fx.Node]:
        bmm_node = [n for n in partitioned_nodes if n.target == self.bmm][0]
        add_node = [n for n in partitioned_nodes if n.target == self.add][0]

        # weight -> expand_copy -> view_copy -> input of bmm
        weight_node = bmm_node.args[1].args[0].args[0].args[0]
        # input -> expand_copy -> view_copy -> input of bmm
        input_node = bmm_node.args[0].args[0].args[0]
        bias_node = add_node.args[1]
        return [input_node, weight_node, bias_node]

    def _convert(self, graph_module: torch.fx.GraphModule):
        partitions = get_source_partitions(graph_module.graph, [torch.nn.Linear])
        for _, src_partitions in partitions.items():
            for src_partition in src_partitions:
                op_cnt = Counter(
                    [n.target for n in src_partition.nodes if type(n.target) == edge_op]
                )
                if self.linear in op_cnt:
                    continue
                elif op_cnt in self.addmm_patterns:
                    self._convert_to_linear(
                        graph_module, src_partition, self._extract_addmm_ops
                    )
                elif op_cnt in self.mm_patterns:
                    self._convert_to_linear(
                        graph_module, src_partition, self._extract_mm_ops
                    )
                elif op_cnt in self.bmm_patterns:
                    self._convert_to_linear(
                        graph_module, src_partition, self._extract_bmm_ops
                    )
                else:
                    raise AssertionError(
                        "Found a new pattern needed be converted to linear op"
                    )

    def call(self, graph_module: torch.fx.GraphModule):
        self._convert(graph_module)
        # We could not use get_source_partitions because it is the same source for MultiheadAttention
        apply_addmm_mm_to_linear_transform(graph_module.graph)
        dead_code_elimination_pass(graph_module)
        graph_module.recompile()
        return PassResult(graph_module, True)
