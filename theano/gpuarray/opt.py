from __future__ import absolute_import, print_function, division
import copy
import numpy
import logging
import pdb
import time
from six import iteritems
from six.moves import xrange
import sys

import theano
from theano import tensor, scalar, gof, config
from theano.compile import optdb
from theano.compile.ops import shape_i
from theano.gof import (local_optimizer, EquilibriumDB, TopoOptimizer,
                        SequenceDB, Optimizer, DB, toolbox, graph)
from theano.ifelse import IfElse
from theano.misc.ordered_set import OrderedSet

from theano.scalar.basic import Scalar, Pow, Cast
from theano.scan_module import scan_utils, scan_op, scan_opt

from theano.tensor.nnet.conv import ConvOp
from theano.tensor.nnet.blocksparse import SparseBlockGemv, SparseBlockOuter
from theano.tensor.nnet.abstract_conv import (AbstractConv2d,
                                              AbstractConv2d_gradWeights,
                                              AbstractConv2d_gradInputs)

from theano.tests.breakpoint import PdbBreakpoint

from .type import (GpuArrayType, GpuArrayConstant, get_context,
                   ContextNotDefined)
from .basic_ops import (as_gpuarray_variable, infer_context_name,
                        host_from_gpu, GpuToGpu,
                        HostFromGpu, GpuFromHost,
                        GpuSplit, GpuContiguous, gpu_contiguous,
                        GpuAlloc, GpuAllocEmpty, GpuReshape,
                        GpuEye, gpu_join, GpuJoin, gpu_alloc_empty, gpu_alloc, gpu_from_host)
from .blas import (gpu_dot22, GpuGemm, GpuGer, GpuGemmBatch,
                   gpugemm_no_inplace, gpugemm_inplace, gpugemmbatch_no_inplace,
                   gpugemv_no_inplace, gpugemv_inplace)
from .blocksparse import (GpuSparseBlockGemv, GpuSparseBlockOuter,
                          gpu_sparse_block_outer, gpu_sparse_block_outer_inplace,
                          gpu_sparse_block_gemv, gpu_sparse_block_gemv_inplace)
from .nnet import (gpu_crossentropy_softmax_1hot_with_bias_dx,
                   gpu_crossentropy_softmax_argmax_1hot_with_bias,
                   gpu_softmax_with_bias, gpu_softmax)

from .elemwise import (GpuElemwise, GpuDimShuffle, GpuCAReduceCuda,
                       GpuCAReduceCPY, gpu_ca_reduce_cuda)
from .subtensor import (GpuIncSubtensor, GpuSubtensor,
                        GpuAdvancedSubtensor1,
                        GpuAdvancedIncSubtensor1,
                        GpuAdvancedIncSubtensor1_dev20)
from .opt_util import alpha_merge, output_merge

_logger = logging.getLogger("theano.gpuarray.opt")


gpu_optimizer = EquilibriumDB()
gpu_cut_copies = EquilibriumDB()

# Not used for an EquilibriumOptimizer. It has the "tracks" that we need for GraphToGPUDB.
gpu_optimizer2 = EquilibriumDB()


class GraphToGPUDB(DB):
    """
    Retrieves the list local optimizers based on the optimizer flag's value
    from EquilibriumOptimizer by calling the method query.

    """

    def query(self, *tags, **kwtags):
        opt = gpu_optimizer2.query(*tags, **kwtags)
        return GraphToGPU(opt.local_optimizers_all, opt.local_optimizers_map)


gpu_seqopt = SequenceDB()

gpu_seqopt.register('gpuarray_graph_optimization', GraphToGPUDB(), -0.5,
                    'fast_compile', 'fast_run', 'gpuarray')

gpu_seqopt.register('gpuarray_local_optimizations', gpu_optimizer, 1,
                    'fast_compile', 'fast_run', 'gpuarray', 'gpuarray_local_optimiziations')
gpu_seqopt.register('gpuarray_cut_transfers', gpu_cut_copies, 2,
                    'fast_compile', 'fast_run', 'gpuarray')

# do not add 'fast_run' to these two as this would always enable gpuarray mode
optdb.register('gpuarray_opt', gpu_seqopt,
               optdb.__position__.get('add_destroy_handler', 49.5) - 1,
               'gpuarray')


def register_opt(*tags, **kwargs):
    def f(local_opt):
        name = (kwargs and kwargs.pop('name')) or local_opt.__name__
        gpu_optimizer.register(name, local_opt, 'fast_run', 'gpuarray', *tags)
        return local_opt
    return f


def register_opt2(tracks, *tags, **kwargs):
    '''
    Decorator for the new GraphToGPU optimizer.
    Takes an extra parameter(Op) compared to register_opt decorator.

    Parameters
    ----------
    tracks : List of Op class Or Op instance or None
        The Node's Op to which optimization is being applied.

    tags : String
        The optimization tag to which the optimizer will be registered.

    '''
    def f(local_opt):
        name = (kwargs and kwargs.pop('name')) or local_opt.__name__
        opt = theano.gof.local_optimizer(tracks)(local_opt)
        gpu_optimizer2.register(name, opt, 'fast_run', 'gpuarray', *tags)
        return local_opt
    return f


def register_inplace(*tags, **kwargs):
    def f(local_opt):
        name = (kwargs and kwargs.pop('name')) or local_opt.__name__
        optdb.register(
            name, TopoOptimizer(
                local_opt, failure_callback=TopoOptimizer.warn_inplace),
            60, 'fast_run', 'inplace', 'gpuarray', *tags)
        return local_opt
    return f

register_opt('fast_compile')(theano.tensor.opt.local_track_shape_i)
register_opt(final_opt=True, name='gpua_constant_folding')(
    tensor.opt.constant_folding)
gpu_optimizer.register('local_remove_all_assert',
                       theano.tensor.opt.local_remove_all_assert,
                       'unsafe')


def safe_to_gpu(x, ctx_name):
    if isinstance(x.type, tensor.TensorType):
        return gpu_from_host(ctx_name)(x)
    else:
        return x


def safe_to_cpu(x):
    if isinstance(x.type, GpuArrayType):
        return host_from_gpu(x)
    else:
        return x


def op_lifter(OP, cuda_only=False):
    """
    OP(..., host_from_gpu(), ...) -> host_from_gpu(GpuOP(...))

    gpu_from_host(OP(inp0, ...)) -> GpuOP(inp0, ...)

    """
    def f(maker):
        def local_opt(node):
            if type(node.op) in OP:
                # Either one of our inputs is on the gpu or
                # all of our clients are on the gpu
                replace = False
                # TODO: Maybe set context_name with infer_context_name()?
                context_name = None
                # We replace if any input is a host_from_gpu
                for i in node.inputs:
                    if i.owner and i.owner.op == host_from_gpu:
                        context_name = i.owner.inputs[0].type.context_name
                        replace = True
                        break

                if not replace:
                    # We replace if *all* clients are on the GPU
                    clients = [c for o in node.outputs for c in o.clients]
                    replace = len(clients) != 0
                    for c, idx in clients:
                        if (c == 'output' or
                                not isinstance(c.op, GpuFromHost)):
                            replace = False
                    # TODO: check that the clients want the same context?
                    if replace:
                        # All clients are GpuFromHost and we have at least one
                        context_name = clients[0][0].op.context_name

                # Check if we should replace
                if (not replace or
                        (cuda_only and
                         get_context(context_name).kind != b'cuda') or
                        any(["complex" in i.dtype for i in node.inputs])):
                    return False

                # tag the inputs with the context in case
                # the context was derived from the outputs
                for i in node.inputs:
                    i.tag.context_name = context_name

                new_op = maker(node.op, context_name, node.inputs, node.outputs)

                # This is needed as sometimes new_op inherits from OP.
                if new_op and new_op != node.op:
                    if isinstance(new_op, theano.Op):
                        return [safe_to_cpu(o) for o in
                                new_op(*node.inputs, return_list=True)]
                    elif isinstance(new_op, (tuple, list)):
                        return [safe_to_cpu(o) for o in new_op]
                    else:  # suppose it is a variable on the GPU
                        return [host_from_gpu(new_op)]
            return False
        local_opt.__name__ = maker.__name__
        return local_optimizer(OP)(local_opt)
    return f


class InputToGpuOptimizer(Optimizer):
    """
    Transfer the input to the gpu to start the rolling wave.

    """
    def add_requirements(self, fgraph):
        fgraph.attach_feature(toolbox.ReplaceValidate())

    def apply(self, fgraph):
        for input in fgraph.inputs:
            if isinstance(input.type, GpuArrayType):
                continue

            # If all clients are outputs or transfers don't do anything.
            if (all(cl[0] == 'output' or isinstance(cl[0].op, GpuFromHost)
                    for cl in input.clients)):
                continue

            target = getattr(input.tag, 'target', None)
            if target == 'cpu':
                continue
            # Do not move *int* scalar to the GPU.
            if (isinstance(input.type, tensor.TensorType) and
                    input.ndim == 0 and 'int' in input.dtype):
                continue

            try:
                new_input = host_from_gpu(gpu_from_host(target)(input))
                fgraph.replace_validate(input, new_input,
                                        "InputToGpuOptimizer")
            except TypeError:
                # This could fail if the inputs are not TensorTypes
                pass
            except ContextNotDefined:
                if hasattr(input.tag, 'target'):
                    raise
                # If there is no context tag and no default context
                # then it stays on the CPU
                pass


gpu_seqopt.register('InputToGpuArrayOptimizer', InputToGpuOptimizer(),
                    0, 'fast_run', 'fast_compile', 'merge')


class GraphToGPU(Optimizer):
    """
    Transfer the graph as a whole to GPU instead of transfering node by node.

    Parameters
    ----------
    local_optimizers_all : List or SortedSet
        The local optimizations to apply to a node.
    local_optimizers_map : Dict
        Dictionary object containing the mapping of Op to list of
        LocalOptimizers.
    """

    def __init__(self, local_optimizers_all, local_optimizers_map):
        self.local_optimizers_all = local_optimizers_all
        self.local_optimizers_map = local_optimizers_map

    def add_requirements(self, fgraph):
        fgraph.attach_feature(toolbox.ReplaceValidate())

    def apply(self, fgraph):
        mapping = {}
        time_opts = {}
        node_created = {}
        process_count = {}
        t_topo = time.time()
        topo = fgraph.toposort()
        time_topo = time.time()
        toposort_timing = time_topo - t_topo

        # Building a new graph
        # Iterating through inputs of graph
        target = infer_context_name(*fgraph.inputs)
        for i in fgraph.inputs:
            # Do not move *int* scalar to the GPU.
            if (isinstance(i.type, tensor.TensorType) and
                    (i.ndim > 0 or 'int' not in i.dtype) and
                    "complex" not in i.dtype):
                mapping[i] = i.transfer(getattr(i.tag, 'target', target))
            else:
                mapping[i] = i
        for i in fgraph.variables:
            if isinstance(i, theano.Constant):
                mapping[i] = i
        for node in topo:
            for lopt in (self.local_optimizers_map.get(node.op, []) +
                         self.local_optimizers_map.get(type(node.op), []) +
                         self.local_optimizers_all):
                process_count.setdefault(lopt, 0)
                time_opts.setdefault(lopt, 0)
                node_created.setdefault(lopt, 0)

        for node in topo:

            if isinstance(node.op, HostFromGpu):
                mapping[node.outputs[0]] = mapping[node.inputs[0]]
                continue

            # Move only if any of the inputs are on the GPU.
            move_to_GPU = False

            context_name = None
            for i in [mapping[i] for i in node.inputs]:
                if isinstance(i.type, GpuArrayType):
                    context_name = i.type.context_name
                    move_to_GPU = True
                    break
            if (not move_to_GPU and
                    isinstance(node.op, (theano.tensor.Alloc,
                                         theano.tensor.AllocEmpty,
                                         theano.tensor.basic.Eye))):
                # If the Alloc[Empty] have a client that will be moved
                # to the GPU, we should move the Alloc* on the GPU.

                # We approximate this by supposing that if we have an
                # optimization for one of the clients op, then we will
                # move the client to the GPU.
                for c, _ in node.outputs[0].clients:
                    if (c != 'output' and
                        (self.local_optimizers_map.get(c.op, []) +
                         self.local_optimizers_map.get(type(c.op), []))):
                        move_to_GPU = True
            new_ops = None
            if move_to_GPU and any(["complex" in i.dtype for i in node.inputs]):
                move_to_GPU = False

            # Apply the lifter
            if move_to_GPU:
                for lopt in (self.local_optimizers_map.get(node.op, []) +
                             self.local_optimizers_map.get(type(node.op), []) +
                             self.local_optimizers_all):
                        t_opt = time.time()
                        new_ops = lopt.transform(node.op, context_name,
                                                 [mapping[i] for i in node.inputs],
                                                 node.outputs)
                        t_opt2 = time.time()
                        time_opts[lopt] += t_opt2 - t_opt

                        if new_ops:
                            process_count[lopt] += 1
                            break
            outputs = []

            if isinstance(new_ops, theano.Op):
                outputs = new_ops(*[mapping[i] for i in node.inputs], return_list=True)
            elif not new_ops:
                newnode = node.clone_with_new_inputs([mapping.get(i) for i in node.inputs])
                outputs = newnode.outputs
            elif isinstance(new_ops, (tuple, list)):
                outputs = new_ops
            elif isinstance(new_ops, theano.Variable):
                outputs = [new_ops]

            if new_ops:
                node_created[lopt] += len(graph.ops([mapping[i] for i in node.inputs], outputs))
                if any([getattr(old_o, 'dtype', None) != getattr(new_o, 'dtype', None)
                        for old_o, new_o in zip(outputs, node.outputs)]):
                    _logger.warning(
                        "The optimization %s returned bad dtype. Skipping it."
                        " Write to theano-dev mailing list about this." %
                        str(lopt))
                    newnode = node.clone_with_new_inputs([mapping.get(i) for i in node.inputs])
                    outputs = newnode.outputs

            for new_o, old_o in zip(outputs, node.outputs):
                assert len(outputs) == len(node.outputs)
                mapping[old_o] = new_o

        new_nodes = []
        for o in fgraph.outputs:
            new_o = mapping[o]
            if new_o.type != o.type:
                assert isinstance(o.type, tensor.TensorType)
                assert isinstance(new_o.type, GpuArrayType)

                # This condition is needed in the case one input is an
                # output of the graph. Without this, it would
                # introduce cycle as we don't replace correctly that
                # case. It would also add extra transfer to/from the
                # gpu.
                if (new_o.owner and
                        isinstance(new_o.owner.op, GpuFromHost) and
                        new_o.owner.inputs[0].type == o.type):
                    new_o = new_o.owner.inputs[0]
                else:
                    new_o = safe_to_cpu(new_o)
            new_nodes.append(new_o)
        fgraph.replace_all_validate(zip(fgraph.outputs, new_nodes),
                                    reason=self.__class__.__name__)

        return (self, toposort_timing, time_opts, node_created, process_count)

    @staticmethod
    def print_profile(stream, prof, level=0):
        (opt, toposort_timing, time_opts, node_created, process_count) = prof
        blanc = ('    ' * level)
        print(blanc, "GraphToGPUOptimizer", end=' ', file=stream)

        print(blanc, getattr(opt, "name",
                             getattr(opt, "__name__", "")), file=stream)

        print(blanc, "  time io_toposort %.3fs" % toposort_timing, file=stream)

        s = sum([v for k, v in time_opts.iteritems()])
        print(blanc, "Total time taken by local optimizers %.3fs " % s, file=stream)

        count_opt = []
        not_used = []
        not_used_time = 0

        for o, count in iteritems(process_count):
            if count > 0:
                count_opt.append((time_opts[o], count,
                                  node_created[o], o))
            else:
                not_used.append((time_opts[o], o))
                not_used_time += time_opts[o]

        if count_opt:
            print(blanc,
                  '  times - times applied - Node created - name:',
                  file=stream)
            count_opt.sort()
            for (t, count, n_created, o) in count_opt[::-1]:
                print(blanc, '  %.3fs - %d - %d - %s' % (
                    t, count, n_created, o), file=stream)
            print(blanc, '  %.3fs - in %d optimization that were not used (display only those with a runtime > 0)' % (
                not_used_time, len(not_used)), file=stream)
            not_used.sort(key=lambda nu: (nu[0], str(nu[1])))
            for (t, o) in not_used[::-1]:
                if t > 0:
                    # Skip opt that have 0 times, they probably wasn't even tried.
                    print(blanc + "  ", '  %.3fs - %s' % (t, o), file=stream)
            print(file=stream)

    @staticmethod
    def merge_profile(prof1, prof2):
        # (opt, toposort_timing, time_opts, node_created, process_count) = prof1
        local_optimizers = OrderedSet(prof1[0].local_optimizers_all).union(
            prof2[0].local_optimizers_all)

        def merge_dict(d1, d2):
            """
            merge 2 dicts by adding the values.
            """
            d = d1.copy()
            for k, v in iteritems(d2):
                if k in d:
                    d[k] += v
                else:
                    d[k] = v
            return d

        local_optimizers_map = merge_dict(prof1[0].local_optimizers_map,
                                          prof2[0].local_optimizers_map)
        new_opt = GraphToGPU(local_optimizers, local_optimizers_map)

        toposort_timing = prof1[1] + prof2[1]
        time_opts = merge_dict(prof1[2], prof2[2])
        node_created = merge_dict(prof1[3], prof2[3])
        process_count = merge_dict(prof1[4], prof2[4])
        return (new_opt,
                toposort_timing,
                time_opts,
                node_created,
                process_count)

    def print_summary(self, stream=sys.stdout, level=0, depth=-1):
        print("%s%s (%i)" % (
            (' ' * level), self.__class__.__name__, id(self)), file=stream)
        if depth != 0:
            map_values = []
            for opts in self.local_optimizers_map.values():
                map_values += opts
            for opt in self.local_optimizers_all + map_values:
                opt.print_summary(stream, level=(level + 2), depth=(depth - 1))


@local_optimizer([GpuFromHost, GpuToGpu, HostFromGpu])
def local_cut_gpu_transfers(node):
    # gpu[ab] -> host -> gpub
    if (isinstance(node.op, GpuFromHost) and
            node.inputs[0].owner and
            isinstance(node.inputs[0].owner.op, HostFromGpu)):
        other = node.inputs[0].owner.inputs[0]
        if node.op.context_name == other.type.context_name:
            return [other]
        else:
            return [GpuToGpu(node.op.context_name)(other)]

    # ? -> gpua -> host
    elif (isinstance(node.op, HostFromGpu) and
          node.inputs[0].owner):
        n2 = node.inputs[0].owner

        # host ->
        if isinstance(n2.op, GpuFromHost):
            return [n2.inputs[0]]

        # gpub ->
        if isinstance(n2.op, GpuToGpu):
            return [host_from_gpu(n2.inputs[0])]

    # ? -> gpua -> gpub
    elif isinstance(node.op, GpuToGpu):
        # Transfer within same context
        if node.inputs[0].type.context_name == node.op.context_name:
            return [node.inputs[0]]

        if node.inputs[0].owner:
            n2 = node.inputs[0].owner

            # host ->
            if isinstance(n2.op, GpuFromHost):
                return [as_gpuarray_variable(n2.inputs[0],
                                             node.op.context_name)]

            # gpuc ->
            if isinstance(n2.op, GpuToGpu):
                if node.op.context_name == n2.inputs[0].type.context_name:
                    return [n2.inputs[0]]
                else:
                    return [node.op(n2.inputs[0])]

gpu_cut_copies.register('cut_gpua_host_transfers', local_cut_gpu_transfers,
                        'fast_compile', 'fast_run', 'gpuarray')
gpu_cut_copies.register('cut_gpua_constant_transfers',
                        tensor.opt.constant_folding,
                        'fast_compile', 'fast_run', 'gpuarray')
optdb['canonicalize'].register('local_cut_gpua_host_gpua',
                               local_cut_gpu_transfers,
                               'fast_compile', 'fast_run', 'gpuarray')


@register_opt('fast_compile')
@local_optimizer([tensor.Alloc])
def local_gpua_alloc2(node):
    """
    Join(axis, {Alloc or HostFromGPU}, ...) -> Join(axis, GpuAlloc, Alloc, ...)

    Moves an alloc that is an input to join to the gpu.

    """
    try:
        get_context(None)
    except ContextNotDefined:
        # If there is no default context then we do not perform the move here.
        return
    if (isinstance(node.op, tensor.Alloc) and
        all(c != 'output' and
            c.op == tensor.join and
            all(i.owner and
                i.owner.op in [host_from_gpu, tensor.alloc]
                for i in c.inputs[1:])
            for c, idx in node.outputs[0].clients)):
        return [host_from_gpu(gpu_alloc(None)(*node.inputs))]


@register_opt('fast_compile')
@op_lifter([tensor.Alloc])
@register_opt2([tensor.Alloc], 'fast_compile')
def local_gpua_alloc(op, context_name, inputs, outputs):
    return gpu_alloc(context_name)


@register_opt('fast_compile')
@op_lifter([tensor.AllocEmpty])
@register_opt2([tensor.AllocEmpty], 'fast_compile')
def local_gpua_alloc_empty(op, context_name, inputs, outputs):
    # We use _props_dict() to make sure that the GPU op know all the
    # CPU op props.
    return gpu_alloc_empty(context_name, **op._props_dict())


@register_opt()
@local_optimizer([GpuAlloc])
def local_gpualloc_memset_0(node):
    if isinstance(node.op, GpuAlloc) and not node.op.memset_0:
        inp = node.inputs[0]
        if (isinstance(inp, GpuArrayConstant) and
                inp.data.size == 1 and
                (numpy.asarray(inp.data) == 0).all()):
            new_op = gpu_alloc(node.op.context_name, memset_0=True)
            return [new_op(*node.inputs)]


# Don't register by default.
@gof.local_optimizer([GpuAllocEmpty])
def local_gpua_alloc_empty_to_zeros(node):
    if isinstance(node.op, GpuAllocEmpty):
        context_name = infer_context_name(*node.inputs)
        z = numpy.asarray(0, dtype=node.outputs[0].dtype)
        return [gpu_alloc(context_name)(as_gpuarray_variable(z, context_name),
                                        *node.inputs)]
optdb.register('local_gpua_alloc_empty_to_zeros',
               theano.tensor.opt.in2out(local_gpua_alloc_empty_to_zeros),
               # After move to gpu and merge2, before inplace.
               49.3,
               'alloc_empty_to_zeros',)


@register_opt()
@local_optimizer([GpuContiguous])
def local_gpu_contiguous_gpu_contiguous(node):
    """
    gpu_contiguous(gpu_contiguous(x)) -> gpu_contiguous(x)

    """
    if isinstance(node.op, GpuContiguous):
        inp = node.inputs[0]
        if inp.owner and isinstance(inp.owner.op, GpuContiguous):
            return [inp]


@register_opt('fast_compile')
@op_lifter([tensor.extra_ops.CpuContiguous])
@register_opt2([tensor.extra_ops.CpuContiguous], 'fast_compile')
def local_gpua_contiguous(op, context_name, inputs, outputs):
    return gpu_contiguous


@register_opt('fast_compile')
@op_lifter([tensor.Reshape])
@register_opt2([tensor.Reshape], 'fast_compile')
def local_gpua_reshape(op, context_name, inputs, outputs):
    res = GpuReshape(op.ndim)
    return res


@register_opt('fast_compile')
@op_lifter([tensor.Rebroadcast])
@register_opt2([tensor.Rebroadcast], 'fast_compile')
def local_gpua_rebroadcast(op, context_name, inputs, outputs):
    return op(as_gpuarray_variable(inputs[0], context_name))


@register_opt('fast_compile')
@op_lifter([tensor.Flatten])
@register_opt2([tensor.Flatten], 'fast_compile')
def local_gpua_flatten(op, context_name, inputs, outputs):
    shp = []
    if op.outdim != 1:
        shp = [inputs[0].shape[i] for i in range(op.outdim - 1)]
    shp += [-1]
    res = GpuReshape(op.outdim)
    o = res(inputs[0], theano.tensor.as_tensor_variable(shp))
    return o


@register_opt('fast_compile')
@op_lifter([tensor.Elemwise])
@register_opt2([tensor.Elemwise], 'fast_compile')
def local_gpua_elemwise(op, context_name, inputs, outputs):
    scal_op = op.scalar_op
    name = op.name
    if name:
        name = 'Gpu' + name
    if len(outputs) > 1:
        return
    res = GpuElemwise(scal_op, name=name,
                      inplace_pattern=copy.copy(op.inplace_pattern),
                      nfunc_spec=op.nfunc_spec)

    # If the elemwise operation is a pow, casts might be required on the
    # inputs and or outputs because only the (float, float)->float and
    # (double, double)->double cases are implemented at the moment.
    if isinstance(op.scalar_op, Pow):

        # Only transfer the computation on the gpu if the output dtype is
        # floating point. Else, give up on the transfer to the gpu.
        out_dtype = outputs[0].dtype
        if out_dtype not in ['float16', 'float32', 'float64']:
            return

        # Transfer the inputs on the GPU and cast them to the right dtype.
        new_inputs = []
        for inp in inputs:
            if inp.dtype != out_dtype:
                gpu_cast_op = GpuElemwise(Cast(Scalar(out_dtype)))
                new_inputs.append(gpu_cast_op(as_gpuarray_variable(inp, context_name)))
            else:
                new_inputs.append(as_gpuarray_variable(inp, context_name))

        # Perform the exponent on the gpu and transfer the output back to the
        # cpu.
        gpu_output = res(*new_inputs)
        return [gpu_output]
    else:
        return res


def max_inputs_to_GpuElemwise(node):
    ptr_size = 8
    int_size = 4

    # we take the limit from CUDA for now
    argument_limit = 232
    ndim = node.inputs[0].type.ndim
    # number of elements and shape
    size_param_mandatory = (int_size * (ndim + 1)) + \
        (ptr_size + int_size * ndim) * len(node.outputs)

    nb_bytes_avail = argument_limit - size_param_mandatory
    nb_bytes_per_input = ptr_size + ndim * int_size
    max_nb_inputs = nb_bytes_avail // nb_bytes_per_input

    return max_nb_inputs

gpu_local_elemwise_fusion = tensor.opt.local_elemwise_fusion_op(
    GpuElemwise,
    max_inputs_to_GpuElemwise)
optdb.register('gpua_elemwise_fusion',
               tensor.opt.FusionOptimizer(gpu_local_elemwise_fusion), 71.00,
               'fast_run', 'fusion', 'local_elemwise_fusion', 'gpuarray')

inplace_gpu_elemwise_opt = tensor.opt.inplace_elemwise_optimizer_op(
    GpuElemwise)
optdb.register('gpua_inplace_opt', inplace_gpu_elemwise_opt, 75,
               'inplace_elemwise_optimizer', 'fast_run', 'inplace', 'gpuarray')


@register_opt('fast_compile')
@op_lifter([tensor.DimShuffle])
@register_opt2([tensor.DimShuffle], 'fast_compile')
def local_gpua_dimshuffle(op, context_name, inputs, outputs):
    return GpuDimShuffle(op.input_broadcastable,
                         op.new_order)


@register_opt('fast_compile')
@op_lifter([tensor.SpecifyShape])
@register_opt2([tensor.SpecifyShape], 'fast_compile')
def local_gpua_specifyShape(op, context_name, inputs, outputs):
    if isinstance(inputs[0].type, GpuArrayType):
        return
    return local_gpua_specifyShape_graph(op, context_name, inputs, outputs)


@register_opt2([tensor.SpecifyShape], 'fast_compile')
def local_gpua_specifyShape_graph(op, context_name, inputs, outputs):
    inp = [as_gpuarray_variable(inputs[0], context_name)]
    inp += inputs[1:]
    return tensor.specify_shape(*inp)


@register_opt('fast_compile')
@op_lifter([theano.compile.ops.Shape])
def local_gpua_shape(op, context_name, inputs, outputs):
    # op_lifter will call this opt too frequently as the output is
    # always on the CPU.
    if isinstance(inputs[0].type, GpuArrayType):
        return
    return local_gpua_shape_graph(op, context_name, inputs, outputs)


@register_opt2([tensor.compile.ops.Shape], 'fast_compile')
def local_gpua_shape_graph(op, context_name, inputs, outputs):
    return [as_gpuarray_variable(inputs[0], context_name).shape]


def gpu_print_wrapper(op, cnda):
    op.old_op.global_fn(op.old_op, numpy.asarray(cnda))


@register_opt('fast_compile')
@op_lifter([tensor.printing.Print])
@register_opt2([tensor.printing.Print], 'fast_compile')
def local_gpua_print_op(op, context_name, inputs, outputs):
    x, = inputs
    gpu_x = as_gpuarray_variable(x, context_name=context_name)
    new_op = op.__class__(global_fn=gpu_print_wrapper)
    new_op.old_op = op
    return new_op(gpu_x)


@register_opt('fast_compile')
@local_optimizer([PdbBreakpoint])
def local_gpu_pdbbreakpoint_op(node):
    if isinstance(node.op, PdbBreakpoint):

        old_inputs = node.inputs
        old_outputs = node.outputs

        new_inputs = node.inputs[:1]
        input_transfered = []

        # Go through the monitored variables, only transfering on GPU those
        # for which the input comes from the GPU or the output will be
        # transfered on the GPU.
        nb_monitored_vars = len(node.outputs)
        for i in range(nb_monitored_vars):

            inp = old_inputs[i + 1]
            out = old_outputs[i]

            input_is_from_gpu = (inp.owner and
                                 isinstance(inp.owner.op, HostFromGpu))
            output_goes_to_gpu = False
            for c in out.clients:
                if c == 'output':
                    continue
                if isinstance(c[0].op, GpuFromHost):
                    output_goes_to_gpu = True
                    context_name = c[0].op.context_name
                    break

            if input_is_from_gpu:
                # The op should be applied on the GPU version of the input
                new_inputs.append(inp.owner.inputs[0])
                input_transfered.append(True)

            elif output_goes_to_gpu:
                # The input should be transfered to the gpu
                new_inputs.append(as_gpuarray_variable(inp, context_name))
                input_transfered.append(True)

            else:
                # No transfer is required.
                new_inputs.append(inp)
                input_transfered.append(False)

        # Only continue the optimization if at least one input has been
        # transfered to the gpu
        if not any(input_transfered):
            return False

        # Apply the op on the new inputs
        new_op_outputs = node.op(*new_inputs, return_list=True)

        # Propagate the transfer to the gpu through the outputs that require
        # it
        new_outputs = []
        for i in range(len(new_op_outputs)):
            if input_transfered[i]:
                new_outputs.append(host_from_gpu(new_op_outputs[i]))
            else:
                new_outputs.append(new_op_outputs[i])

        return new_outputs

    return False


@register_opt('fast_compile')
@op_lifter([IfElse])
@register_opt2([IfElse], 'fast_compile')
def local_gpua_lazy_ifelse(op, context_name, inputs, outputs):
    if op.gpu:
        return
    c = inputs[0]
    inps = []
    for v in inputs[1:]:
        if isinstance(v.type, tensor.TensorType):
            inps.append(as_gpuarray_variable(v, context_name))
        else:
            inps.append(v)
    return IfElse(op.n_outs, gpu=True)(c, *inps, return_list=True)


@register_opt('fast_compile')
@op_lifter([tensor.Join])
@register_opt2([tensor.Join], 'fast_compile')
def local_gpua_join(op, context_name, inputs, outputs):
    return gpu_join


@register_opt('fast_compile')
@local_optimizer([GpuJoin])
def local_gpua_join_1(node):
    # join of a single element
    if (isinstance(node.op, GpuJoin) and
            len(node.inputs) == 2):
        return [node.inputs[1]]


@register_opt('fast_compile')
@op_lifter([tensor.Split])
@register_opt2([tensor.Split], 'fast_compile')
def local_gpua_split(op, context_name, inputs, outputs):
    # TODO use props
    return GpuSplit(op.len_splits)


@register_opt('fast_compile')
@op_lifter([tensor.Subtensor])
def local_gpua_subtensor(op, context_name, inputs, outputs):
    x = inputs[0]
    if (x.owner and isinstance(x.owner.op, HostFromGpu)):
        gpu_x = x.owner.inputs[0]
        if (gpu_x.owner and
                isinstance(gpu_x.owner.op, GpuFromHost) and
                # And it is a shared var or an input of the graph.
                not gpu_x.owner.inputs[0].owner):
            if len(x.clients) == 1:
                if any([n == 'output' or any([isinstance(v.type, GpuArrayType)
                                              for v in n.inputs + n.outputs])
                        for n, _ in outputs[0].clients]):
                    return
                else:
                    return [host_from_gpu(gpu_x.owner.op(outputs[0]))]

    return GpuSubtensor(op.idx_list)


@register_opt2([tensor.Subtensor], 'fast_compile')
def local_gpua_subtensor_graph(op, context_name, inputs, outputs):
    # We need different code as the condition is different as inputs
    # aren't the same.
    x = inputs[0]
    # We don't want to move the subtensor to the GPU if the inputs is
    # on the CPU and the only client of the CPU node is this
    # subtensor. This allow to have a smaller transfer.

    if (x.owner and isinstance(x.owner.op, GpuFromHost)):
        cpu_x = x.owner.inputs[0]
        # And it is a shared var or an input of the graph.
        # and is used by only 1 node.
        # x is in the new graph, so we can't tests its number of clients.
        if not cpu_x.owner and len(cpu_x.clients) == 1:
            c = outputs[0].clients
            # If the subtensor have only 1 client, do it on the CPU.
            # We let the other optimization to take care to move the
            # next node or not.
            if len(c) == 1:
                return
    return GpuSubtensor(op.idx_list)


@register_opt('fast_compile')
@op_lifter([tensor.IncSubtensor])
@register_opt2([tensor.IncSubtensor], 'fast_compile')
def local_gpua_inc_subtensor(op, context_name, inputs, outputs):
    op = GpuIncSubtensor(op.idx_list, op.inplace,
                         op.set_instead_of_inc,
                         op.destroyhandler_tolerate_aliased)
    ret = op(*inputs)
    val = getattr(outputs[0].tag, 'nan_guard_mode_check', True)
    ret.tag.nan_guard_mode_check = val
    return ret


@register_opt('fast_compile')
@op_lifter([tensor.AdvancedSubtensor1])
@register_opt2([tensor.AdvancedSubtensor1], 'fast_compile')
def local_gpua_advanced_subtensor(op, context_name, inputs, outputs):
    return GpuAdvancedSubtensor1()


@register_opt('fast_compile')
@op_lifter([tensor.AdvancedIncSubtensor1])
@register_opt2([tensor.AdvancedIncSubtensor1], 'fast_compile')
def local_gpua_advanced_incsubtensor(op, context_name, inputs, outputs):
    context = get_context(context_name)
    # This is disabled on non-cuda contexts
    if context.kind != b'cuda':
        return None

    x, y, ilist = inputs

    # Gpu Ops needs both inputs to have the same dtype
    if (x.type.dtype != y.type.dtype):
        dtype = scalar.upcast(x.type.dtype, y.type.dtype)
        if x.type.dtype != dtype:
            x = tensor.cast(x, dtype)
        if y.type.dtype != dtype:
            y = tensor.cast(y, dtype)

    set_instead_of_inc = op.set_instead_of_inc

    compute_capability = int(context.bin_id[-2])

    if (compute_capability < 2 or x.ndim != 2 or y.ndim != 2):
        return GpuAdvancedIncSubtensor1(
            set_instead_of_inc=set_instead_of_inc)
    else:
        return GpuAdvancedIncSubtensor1_dev20(
            set_instead_of_inc=set_instead_of_inc)


@register_inplace()
@local_optimizer([GpuAdvancedIncSubtensor1, GpuAdvancedIncSubtensor1_dev20])
def local_advincsub1_gpua_inplace(node):
    if isinstance(node.op, (GpuAdvancedIncSubtensor1,
                            GpuAdvancedIncSubtensor1_dev20)):
        if not node.op.inplace:
            return [node.op.clone_inplace()(*node.inputs)]


@register_opt('fast_compile')
@op_lifter([tensor.CAReduce, tensor.Sum, tensor.elemwise.Prod])
@register_opt2([tensor.CAReduce, tensor.Sum, tensor.elemwise.Prod], 'fast_compile')
def local_gpua_careduce(op, context_name, inputs, outputs):
    if isinstance(op.scalar_op, (scalar.Add, scalar.Mul,
                                 scalar.Maximum, scalar.Minimum)):

        ctx = get_context(context_name)
        if ctx.kind == b'opencl':
            op2 = GpuCAReduceCPY
            if op.scalar_op not in [scalar.add, scalar.mul]:
                # We don't support yet all reduction with cpy code.
                return
        elif ctx.kind == b'cuda':
            op2 = GpuCAReduceCuda
        else:
            return False
        x, = inputs
        greduce = op2(
            op.scalar_op, axis=op.axis,
            dtype=getattr(op, 'dtype', outputs[0].dtype),
            acc_dtype=getattr(op, 'acc_dtype', None))
        gvar = greduce(x)
        # We need to have the make node called, otherwise the mask can
        # be None
        if (op2 is GpuCAReduceCPY or
                gvar.owner.op.supports_c_code([
                    as_gpuarray_variable(x, context_name)])):
            return greduce
        else:
            # Try to make a simpler pattern based on reshaping
            # The principle is that if two adjacent dimensions have
            # the same value in the reduce_mask, then we can reshape
            # to make them a single dimension, do the reduction, and
            # then reshape to get them back.

            if op.axis is None:
                reduce_mask = [1] * x.type.ndim
            else:
                reduce_mask = [0] * x.type.ndim
                for a in op.axis:
                    assert reduce_mask[a] == 0
                    reduce_mask[a] = 1

            new_in_shp = [shape_i(x, 0)]
            new_mask = [reduce_mask[0]]
            for i in xrange(1, x.type.ndim):
                if reduce_mask[i] == reduce_mask[i - 1]:
                    new_in_shp[-1] *= shape_i(x, i)
                else:
                    new_mask.append(reduce_mask[i])
                    new_in_shp.append(shape_i(x, i))
            new_axis = []
            for idx, m in enumerate(new_mask):
                if m == 1:
                    new_axis.append(idx)
            greduce = op2(
                op.scalar_op,
                axis=new_axis, reduce_mask=new_mask,
                dtype=getattr(op, 'dtype', outputs[0].dtype),
                acc_dtype=getattr(op, 'acc_dtype', None))

            reshaped_x = x.reshape(tensor.stack(new_in_shp))
            gpu_reshaped_x = as_gpuarray_variable(reshaped_x, context_name)
            gvar = greduce(gpu_reshaped_x)
            # We need to have the make node called, otherwise the mask can
            # be None
            reshaped_gpu_inputs = [gpu_reshaped_x]
            if greduce.supports_c_code(reshaped_gpu_inputs):
                reduce_reshaped_x = greduce(gpu_reshaped_x)

                if reduce_reshaped_x.ndim != outputs[0].ndim:
                    out_shp = []
                    for i in range(x.ndim):
                        if i not in op.axis:
                            out_shp.append(shape_i(x, i))
                    unreshaped_reduce = GpuReshape(len(out_shp))(reduce_reshaped_x,
                                                                 tensor.stack(out_shp))
                else:
                    unreshaped_reduce = reduce_reshaped_x
                return [unreshaped_reduce]


@register_opt('fast_compile')
@op_lifter([tensor.blas.Gemv, tensor.blas_c.CGemv])
@register_opt2([tensor.blas.Gemv], 'fast_compile')
def local_gpua_gemv(op, context_name, inputs, outputs):
    if op.inplace:
        return gpugemv_inplace
    else:
        return gpugemv_no_inplace


@register_opt('fast_compile')
@op_lifter([tensor.blas.Gemm])
@register_opt2([tensor.blas.Gemm], 'fast_compile')
def local_gpua_gemm(op, context_name, inputs, outputs):
    if op.inplace:
        return gpugemm_inplace
    else:
        return gpugemm_no_inplace


@register_opt('fast_compile')
@op_lifter([tensor.blas.BatchedDot])
@register_opt2([tensor.blas.BatchedDot], 'fast_compile')
def local_gpua_gemmbatch(op, context_name, inputs, outputs):
    a, b = inputs
    c = tensor.AllocEmpty(a.dtype)(a.shape[0], a.shape[1], b.shape[2])
    return gpugemmbatch_no_inplace(c, 1.0, a, b, 0.0)


@register_opt('fast_compile')
@op_lifter([tensor.basic.Dot])
@register_opt2([tensor.basic.Dot], 'fast_compile')
def local_gpua_hgemm(op, context_name, inputs, outputs):
    from theano.sandbox.cuda import nvcc_compiler
    if nvcc_compiler.nvcc_version < '7.5':
        _logger.warning("Not performing dot of float16 on the GPU since "
                        "cuda 7.5 is not available. Updating could speed up "
                        "your code.")
        return
    A = inputs[0]
    B = inputs[1]
    if (A.ndim == 2 and B.ndim == 2 and
            A.dtype == 'float16' and B.dtype == 'float16'):
        fgraph = outputs[0].fgraph
        C = gpu_alloc_empty(context_name, dtype='float16')(
            shape_i(A, 0, fgraph),
            shape_i(B, 1, fgraph))
        return gpugemm_no_inplace(C, 1.0, A, B, 0.0)


@register_opt()
@alpha_merge(GpuGemm, alpha_in=1, beta_in=4)
def local_gpua_gemm_alpha_merge(node, *inputs):
    return [gpugemm_no_inplace(*inputs)]


@register_opt()
@output_merge(GpuGemm, alpha_in=1, beta_in=4, out_in=0)
def local_gpua_gemm_output_merge(node, *inputs):
    return [gpugemm_no_inplace(*inputs)]


@register_opt()
@alpha_merge(GpuGemmBatch, alpha_in=1, beta_in=4)
def local_gpua_gemmbatch_alpha_merge(node, *inputs):
    return [gpugemmbatch_no_inplace(*inputs)]


@register_opt()
@output_merge(GpuGemmBatch, alpha_in=1, beta_in=4, out_in=0)
def local_gpua_gemmbatch_output_merge(node, *inputs):
    return [gpugemmbatch_no_inplace(*inputs)]


@register_opt('fast_compile')
@op_lifter([tensor.blas.Ger, tensor.blas_c.CGer, tensor.blas_scipy.ScipyGer])
@register_opt2([tensor.blas.Ger, tensor.blas_c.CGer, tensor.blas_scipy.ScipyGer], 'fast_compile')
def local_gpua_ger(op, context_name, inputs, outputs):
    return GpuGer(inplace=op.destructive)


@register_opt('fast_compile')
@op_lifter([tensor.blas.Dot22])
@register_opt2([tensor.blas.Dot22], 'fast_compile')
def local_gpua_dot22(op, context_name, inputs, outputs):
    return gpu_dot22


@register_opt('fast_compile')
@op_lifter([tensor.blas.Dot22Scalar])
@register_opt2([tensor.blas.Dot22Scalar], 'fast_compile')
def local_gpua_dot22scalar(op, context_name, inputs, outputs):
    x, y, a = inputs
    x = as_gpuarray_variable(x, context_name)
    y = as_gpuarray_variable(y, context_name)
    z = gpu_alloc_empty(context_name, dtype=x.dtype)(x.shape[0], y.shape[1])
    return [gpugemm_no_inplace(z, a, x, y, 0)]


@register_opt('fast_compile')
@op_lifter([tensor.basic.Eye])
@register_opt2([tensor.basic.Eye], 'fast_compile')
def local_gpua_eye(op, context_name, inputs, outputs):
    return GpuEye(dtype=op.dtype, context_name=context_name)


@register_opt('fast_compile')
@op_lifter([tensor.nnet.CrossentropySoftmaxArgmax1HotWithBias], cuda_only=True)
@register_opt2([tensor.nnet.CrossentropySoftmaxArgmax1HotWithBias], 'fast_compile')
def local_gpua_crossentropysoftmaxargmax1hotwithbias(op, context_name, inputs, outputs):
    return gpu_crossentropy_softmax_argmax_1hot_with_bias


@register_opt('fast_compile')
@op_lifter([tensor.nnet.CrossentropySoftmax1HotWithBiasDx], cuda_only=True)
@register_opt2([tensor.nnet.CrossentropySoftmax1HotWithBiasDx], 'fast_compile')
def local_gpua_crossentropysoftmax1hotwithbiasdx(op, context_name, inputs, outputs):
    return gpu_crossentropy_softmax_1hot_with_bias_dx


@register_opt('fast_compile')
@op_lifter([tensor.nnet.Softmax], cuda_only=True)
@register_opt2([tensor.nnet.Softmax], 'fast_compile')
def local_gpua_softmax(op, context_name, inputs, outputs):
    return gpu_softmax


@register_opt('fast_compile')
@op_lifter([tensor.nnet.SoftmaxWithBias], cuda_only=True)
@register_opt2([tensor.nnet.SoftmaxWithBias], 'fast_compile')
def local_gpua_softmaxwithbias(op, context_name, inputs, outputs):
    return gpu_softmax_with_bias


@register_opt('fast_compile')
@op_lifter([theano.tensor.opt.Assert])
def local_gpua_assert(op, context_name, inputs, outputs):
    if isinstance(inputs[0].type, GpuArrayType):
        return
    return local_gpua_assert_graph(op, context_name, inputs, outputs)


@register_opt2([theano.tensor.opt.Assert], 'fast_compile')
def local_gpua_assert_graph(op, context_name, inputs, outputs):
    return [op(as_gpuarray_variable(inputs[0], context_name),
               *inputs[1:])]


@register_opt('fast_compile')
@op_lifter([ConvOp])
@register_opt2([ConvOp], 'fast_compile')
def local_gpua_error_convop(op, context_name, inputs, outputs):
    assert False, """
ConvOp does not work with the gpuarray backend.

Use the new convolution interface to have GPU convolution working:
theano.tensor.nnet.conv2d()
"""


@register_opt('fast_compile')
@op_lifter([SparseBlockGemv])
@register_opt2([SparseBlockGemv], 'fast_compile')
def local_gpua_sparseblockgemv(op, context_name, inputs, outputs):
    if op.inplace:
        return gpu_sparse_block_gemv_inplace
    else:
        return gpu_sparse_block_gemv


@register_opt('fast_compile')
@op_lifter([SparseBlockOuter])
@register_opt2([SparseBlockOuter], 'fast_compile')
def local_gpua_sparseblockouter(op, context_name, inputs, outputs):
    if op.inplace:
        return gpu_sparse_block_outer_inplace
    else:
        return gpu_sparse_block_outer


@register_inplace()
@local_optimizer([GpuSparseBlockGemv], inplace=True)
def local_inplace_sparseblockgemv(node):
    if isinstance(node.op, GpuSparseBlockGemv) and not node.op.inplace:
        return [gpu_sparse_block_gemv_inplace(*node.inputs)]


@register_inplace()
@local_optimizer([GpuSparseBlockOuter], inplace=True)
def local_inplace_sparseblockouter(node):
    if isinstance(node.op, GpuSparseBlockOuter) and not node.op.inplace:
        return [GpuSparseBlockOuter(inplace=True)(*node.inputs)]


# This deals with any abstract convs that have a transfer somewhere
@register_opt('fast_compile', 'conv_dnn', 'cudnn')
@op_lifter([AbstractConv2d,
            AbstractConv2d_gradWeights,
            AbstractConv2d_gradInputs])
def local_gpua_abstractconv2d(op, context_name, inputs, outputs):
    if isinstance(outputs[0].type, GpuArrayType):
        # Don't handle this node here, it's already on the GPU.
        return
    return local_gpua_lift_abstractconv2d_graph(op, context_name, inputs, outputs)


@register_opt2([AbstractConv2d,
                AbstractConv2d_gradWeights,
                AbstractConv2d_gradInputs], 'fast_compile', 'conv_dnn', 'cudnn')
def local_gpua_lift_abstractconv2d_graph(op, context_name, inputs, outputs):
    inps = list(inputs)
    inps[0] = as_gpuarray_variable(inputs[0],
                                   context_name=context_name)
    inps[1] = as_gpuarray_variable(inputs[1],
                                   context_name=context_name)
    return [op(*inps)]


@register_opt("low_memory")
@local_optimizer([GpuCAReduceCuda])
def local_gpu_elemwise_careduce(node):
    """
    Merge some GpuCAReduceCuda and GPUElemwise.

    """
    if (isinstance(node.op, GpuCAReduceCuda) and
            node.op.pre_scalar_op is None and
            node.inputs[0].owner and
            isinstance(node.inputs[0].owner.op, GpuElemwise) and
            # The Op support all scalar with 1 inputs.  We don't
            # automatically add more case, as some like trigonometic
            # operation with some reduction pattern will probably results
            # in slow down.
            isinstance(node.inputs[0].owner.op.scalar_op, scalar.basic.Sqr)):
        op = node.op
        inp = node.inputs[0].owner.inputs[0]
        return [gpu_ca_reduce_cuda(scalar_op=op.scalar_op,
                                   axis=op.axis,
                                   reduce_mask=op.reduce_mask,
                                   pre_scalar_op=scalar.basic.sqr)(inp)]


@local_optimizer(None)
def local_assert_no_cpu_op(node):
    if (all([var.owner and isinstance(var.owner.op, HostFromGpu)
             for var in node.inputs]) and
        any([[c for c in var.clients if isinstance(c[0].op, GpuFromHost)]
             for var in node.outputs])):

            if config.assert_no_cpu_op == "warn":
                _logger.warning(("CPU Op %s is detected in the computation "
                                 "graph") % node)
            elif config.assert_no_cpu_op == "raise":
                raise AssertionError("The Op %s is on CPU." % node)
            elif config.assert_no_cpu_op == "pdb":
                pdb.set_trace()

# Register the local_assert_no_cpu_op:
assert_no_cpu_op = theano.tensor.opt.in2out(local_assert_no_cpu_op,
                                            name='assert_no_cpu_op')
# 49.2 is after device specialization & fusion optimizations for last transfers
optdb.register('gpua_assert_no_cpu_op', assert_no_cpu_op, 49.2,
               'assert_no_cpu_op')


def tensor_to_gpu(x, context_name):
    if isinstance(x.type, tensor.TensorType):
        y = GpuArrayType(broadcastable=x.type.broadcastable,
                         context_name=context_name,
                         dtype=x.type.dtype)()
        if x.name:
            y.name = x.name + '[Gpua]'
        return y
    else:
        return x


def gpu_safe_new(x, tag=''):
    """
    Internal function that constructs a new variable from x with the same
    type, but with a different name (old name + tag). This function is used
    by gradient, or the R-op to construct new variables for the inputs of
    the inner graph such that there is no interference between the original
    graph and the newly constructed graph.

    """
    if hasattr(x, 'name') and x.name is not None:
        nw_name = x.name + tag
    else:
        nw_name = None

    if isinstance(x, theano.Constant):
        return x.clone()

    nw_x = x.type()
    nw_x.name = nw_name
    return nw_x


def gpu_reconstruct_graph(inputs, outputs, tag=None):
    """
    Different interface to clone, that allows you to pass inputs.
    Compared to clone, this method always replaces the inputs with
    new variables of the same type, and returns those (in the same
    order as the original inputs).

    """
    if tag is None:
        tag = ''
    nw_inputs = [gpu_safe_new(x, tag) for x in inputs]
    givens = {}
    for nw_x, x in zip(nw_inputs, inputs):
        givens[x] = nw_x
    nw_outputs = scan_utils.clone(outputs, replace=givens)
    return (nw_inputs, nw_outputs)


@register_opt('scan', 'fast_compile')
@op_lifter([scan_op.Scan])
@register_opt2([scan_op.Scan], 'fast_compile')
def local_gpua_scan_to_gpua(op, context_name, inputs, outputs):
    info = copy.deepcopy(op.info)
    if info.get('gpua', False):
        return
    info['gpua'] = True
    nw_ins = [inputs[0]]
    e = (1 +
         op.n_seqs +
         op.n_mit_mot +
         op.n_mit_sot +
         op.n_sit_sot +
         op.n_shared_outs)
    nw_ins += [safe_to_gpu(x, context_name) for x in inputs[1:e]]
    b = e
    e = e + op.n_nit_sot
    nw_ins += inputs[b:e]
    nw_ins += [safe_to_gpu(x, context_name) for x in inputs[e:]]
    scan_ins = [tensor_to_gpu(x, context_name) for x in op.inputs]

    # The inner output corresponding to the looping condition should not be
    # moved to the gpu
    if op.info['as_while']:
        scan_outs = [safe_to_gpu(x, context_name) for x in op.outputs[:-1]]
        scan_outs += [op.outputs[-1]]
    else:
        scan_outs = [safe_to_gpu(x, context_name) for x in op.outputs]
    scan_outs = scan_utils.clone(
        scan_outs,
        replace=list(zip(op.inputs,
                         (safe_to_cpu(x) for x in scan_ins))))

    # We need to construct the hash here, because scan
    # __init__ does not know about the gpu and can not
    # handle graphs with inputs being on the gpu
    tmp_in, tmp_out = gpu_reconstruct_graph(scan_ins, scan_outs)
    local_fgraph = gof.FunctionGraph(tmp_in, tmp_out, clone=True)
    _cmodule_key = gof.CLinker().cmodule_key_(local_fgraph, [])
    info['gpu_hash'] = hash(_cmodule_key)

    def typebuild(dtype, broadcastable, context_name=context_name):
        return GpuArrayType(dtype=dtype, broadcastable=broadcastable,
                            context_name=context_name)

    nw_op = scan_op.Scan(scan_ins, scan_outs, info,
                         typeConstructor=typebuild).make_node(*nw_ins)
    return nw_op.outputs


def _scan_type_infer(node):
    context_name = infer_context_name(*node.inputs)

    def typebuild(dtype, broadcastable, context_name=context_name):
        return GpuArrayType(dtype=dtype, broadcastable=broadcastable,
                            context_name=context_name)
    return typebuild

# Do not register in fast_run or fast_compile.
# It will be added to fast_run if the GPU is enabled.
optdb.register('gpua_scanOp_make_inplace',
               scan_opt.ScanInplaceOptimizer(typeInfer=_scan_type_infer,
                                             gpua_flag=True),
               75,
               'gpuarray',
               'inplace',
               'scan')
