import copy
import onnx
from collections import namedtuple


class ONNXModelUtils:
    @staticmethod
    def merge_name(prefix, name):
        return "{}_{}".format(prefix, name)

    @staticmethod
    def _rename_iter(iterables, prefix_name, inplace=False):
        new_iz = iterables if inplace else [copy.deepcopy(iz_) for iz_ in iterables]
        for iz_ in new_iz:
            iz_.name = ONNXModelUtils.merge_name(prefix_name, iz_.name)
        return new_iz

    @classmethod
    def _rename_graph(cls, graph, prefix, graph_or_container):
        def io_rename(node, prefix_name, idx):
            new_node = copy.deepcopy(node)
            if not node.name:
                new_node.name = cls.merge_name(prefix_name, "op{}".format(idx))
            else:
                new_node.name = cls.merge_name(prefix_name, node.name)

            del new_node.input[:]
            new_node.input.extend(ONNXModelUtils.merge_name(prefix_name, nm_) if nm_ else '' for nm_ in node.input)
            del new_node.output[:]
            new_node.output.extend(ONNXModelUtils.merge_name(prefix_name, nm_) if nm_ else '' for nm_ in node.output)
            return new_node

        assert prefix is not None, 'The graph prefix could not be None'
        graph_or_container.initializer.extend(cls._rename_iter(graph.initializer, prefix))
        graph_or_container.value_info.extend(cls._rename_iter(graph.value_info, prefix))
        return list(io_rename(nd_, prefix, idx_) for idx_, nd_ in enumerate(graph.node))

    @classmethod
    def _process_node_body(cls, node, prefix):
        if all(attr.name != 'body' for attr in node.attribute):
            return node

        def _process_attr(attr, prefix_name):
            if attr.name == 'body':
                new_attr = copy.deepcopy(attr)
                del new_attr.g.value_info[:]
                del new_attr.g.node[:]
                new_attr.g.node.extend(cls._rename_graph(attr.g, prefix_name, new_attr.g))
                cls._rename_iter(new_attr.g.input, prefix_name, inplace=True)
                cls._rename_iter(new_attr.g.output, prefix_name, inplace=True)
                return new_attr
            else:
                return attr

        attr_list = list(_process_attr(attr_, prefix) for attr_ in node.attribute)
        del node.attribute[:]
        node.attribute.extend(attr_list)
        return node

    @classmethod
    def unfold_model_node(cls, container):
        top_containter = container
        while top_containter.parent is not None:  # only one opset_import in the model.
            top_containter = top_containter.parent

        nodes = container.nodes
        model_nodes = {node.name: node for node in nodes if hasattr(node, 'model')}
        onnx_nodes = [nd_ for nd_ in nodes if nd_.name not in model_nodes]

        for node in model_nodes.values():
            renamed_nodes = cls._rename_graph(node.model.graph, node.name, container)
            onnx_nodes.extend(cls._process_node_body(nd_, node.name) for nd_ in renamed_nodes)

            top_containter.node_domain_version_pair_sets.update(
                [(opset_.domain, opset_.version) for opset_ in node.model.opset_import])
        return onnx_nodes

    @classmethod
    def topological_sort(cls, container, nodes, inputs, outputs):
        op_output_map = {}
        DynNode = namedtuple('DynNode', ['name', 'output'])
        input_nodes = [DynNode(name='placeholder',
                               output=[nm_.name for nm_ in inputs] +
                                      [it_.name for it_ in container.initializers])] + \
                      [nd_ for nd_ in nodes if nd_.op_type == 'Constant']

        for nd_ in nodes + input_nodes:
            for ky_ in nd_.output:
                op_output_map[ky_] = nd_

        edges = {}
        for op in nodes:
            for x in op.input:
                if x == '':
                    continue
                try:
                    predecessor = op_output_map[x]
                except KeyError:
                    raise RuntimeError(
                        "{}: cannot find an operator to produce the tensor: {}".format(op.name, x)) from None

                val = edges.get(predecessor.name, [])
                val.append(op)
                edges[predecessor.name] = val

        for y_ in outputs:
            op = op_output_map[y_.name].name
            if op not in edges:
                edges[op] = []

        visited = set()
        sorted_nodes = []
        unfinished_nodes = set()

        def recursive_helper(node):
            if node.name in visited:
                return

            if node.name in unfinished_nodes:
                raise RuntimeError("ONNX Graph is not a DAG, the cycle is found at {}".format(node.name))

            unfinished_nodes.add(node.name)
            if node.name in edges:  # if the node's output is not in the Graph output.
                assert node.name != '', 'this topological-sort depends on the unique node name.'
                for successor in edges[node.name]:
                    recursive_helper(successor)

            unfinished_nodes.remove(node.name)
            visited.add(node.name)
            if node is not input_nodes[0]:
                sorted_nodes.insert(0, node)

        for nd_ in input_nodes:
            recursive_helper(nd_)

        return sorted_nodes

    @staticmethod
    def _remove_unused_initializers(nodes, initializers, reversed_names):
        nodes_input_set = set()
        for nd_ in nodes:
            nodes_input_set.update(n_ for n_ in nd_.input)

        return [intlz_ for intlz_ in initializers if intlz_.name in nodes_input_set or intlz_.name in reversed_names]

    @classmethod
    def join_models(cls, *models, io_mapping=None):
        # generate the prefix id for the embedding graph to avoid the name conflict
        mdl_prefix = []
        for _i in range(len(models)):
            mdl_prefix.append("g{}".format(_i + 1))

        inputs = cls._rename_iter(models[0].graph.input, mdl_prefix[0])
        outputs = cls._rename_iter(models[-1].graph.output, mdl_prefix[-1])

        port_mapping = {}
        if io_mapping is not None:
            assert callable(io_mapping), "io_mapping is a custom function to build the linkage of the models"
            ModelPort = namedtuple('ModelPort', "input output")
            ports = []
            for _idx in range(len(models)):
                mio = ModelPort([cls.merge_name(mdl_prefix[_idx], _x.name) for _x in models[_idx].graph.input],
                                [cls.merge_name(mdl_prefix[_idx], _y.name) for _y in models[_idx].graph.output])
                ports.append(mio)
            port_mapping = io_mapping(ports)
        for _idx in range(len(models) - 1):
            for _i, _x in enumerate(models[_idx + 1].graph.input):
                iname = cls.merge_name(mdl_prefix[_idx + 1], _x.name)
                if iname not in port_mapping:
                    oname = cls.merge_name(mdl_prefix[_idx], models[_idx].graph.output[_i].name)
                    port_mapping[iname] = oname

        nodes = []
        Container = namedtuple('Container', ['initializer', 'value_info'])
        container = Container(initializer=[], value_info=[])
        for _idx, _m in enumerate(models):
            container.initializer.extend(_m.graph.initializer)
            container.value_info.extend(_m.graph.value_info)
            nodes += cls._rename_graph(_m.graph, mdl_prefix[_idx], container)

        for _n in nodes:
            replaceable = False
            for _i in _n.input:
                if _i in port_mapping:
                    replaceable = True
                    break
            if replaceable:
                new_input = copy.deepcopy(_n.input)
                del _n.input[:]
                _n.input.extend([port_mapping[_i] if _i in port_mapping else _i for _i in new_input])

        name = ''
        domains = set()
        _opset = []
        for _mdl in models:
            for _ops in _mdl.opset_import:
                if _ops.domain not in domains:
                    domains.update([_ops.domain])
                    _opset.append(_ops)
            name = name + '_' + _mdl.graph.name if name else _mdl.graph.name

        inits = cls._remove_unused_initializers(nodes, container.initializer, set())
        helper = onnx.helper
        g = helper.make_graph(nodes, name, inputs, outputs,
                              initializer=inits,
                              value_info=container.value_info)
        m = helper.make_model(g, opset_imports=_opset)
        return m
