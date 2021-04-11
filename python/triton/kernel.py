import inspect
import struct
import enum
import types
import torch
import ast
import builtins
import triton._C.libtriton.triton as _triton
import triton
import sys
from abc import ABC, abstractmethod

########################
# Built-in Functions   #
########################


class float32:
    @staticmethod
    def make_ir(context):
        return _triton.ir.type.get_fp32(context)


class float16:
    @staticmethod
    def make_ir(context):
        return _triton.ir.type.get_fp16(context)


broadcast = _triton.frontend.broadcast
broadcast_to = _triton.frontend.broadcast_to
load = _triton.frontend.load
store = _triton.frontend.store
dot = _triton.frontend.dot
minimum = _triton.frontend.minimum
where = _triton.frontend.where
arange = _triton.frontend.arange
program_id = _triton.frontend.program_id
zeros = _triton.frontend.zeros


class CodeGenerator(ast.NodeVisitor):
    def get_value(self, name):
        # search node.id in local scope
        ret = None
        if name in self.lscope:
            ret = self.lscope[name]
        # search node.id in global scope
        elif name in self.gscope:
            ret = self.gscope[name]
        # search node.id in builtins
        elif name in self.builtins:
            ret = self.builtins[name]
        else:
            raise ValueError(f'{name} is not defined')
        if isinstance(ret, _triton.ir.value):
            return self.module.get_value(name)
        elif isinstance(ret, int):
            return self.builder.get_int32(ret)
        elif isinstance(ret, float):
            return self.builder.get_float32(ret)
        else:
            return ret

    def visit_compound_statement(self, stmts, add_scope=False):
        if add_scope:
            self.module.add_new_scope()
        for stmt in stmts:
            ast.NodeVisitor.visit(self, stmt)
        if add_scope:
            self.module.pop_scope()

    def __init__(self, context, prototype, gscope, attributes, kwargs):
        self.builder = _triton.ir.builder(context)
        self.module = _triton.ir.module('', self.builder)
        self.prototype = prototype
        self.gscope = gscope
        self.lscope = dict()
        self.attributes = attributes
        self.kwargs = kwargs
        self.builtins = {'range': range, 'min': minimum}

    def visit_Module(self, node):
        self.module.add_new_scope()
        ast.NodeVisitor.generic_visit(self, node)
        self.module.pop_scope()

    def visit_FunctionDef(self, node):
        arg_names, kwarg_names = ast.NodeVisitor.visit(self, node.args)
        # store keyword arguments in local scope
        self.lscope[kwarg_names] = self.kwargs
        # initialize function
        fn = self.module.get_or_insert_function(node.name, self.prototype)
        for i, arg_name in enumerate(arg_names):
            if i in self.attributes:
                is_ptr = fn.args[i].type.is_ptr()
                attr = 'aligned' if is_ptr else 'multiple_of'
                attr = getattr(_triton.ir.attribute_kind, attr)
                attr = _triton.ir.attribute(attr, self.attributes[i])
                fn.add_attr(i + 1, attr)
            fn.args[i].name = arg_name
            self.module.set_value(arg_name, fn.args[i])
            self.module.scope.set_type(arg_name, fn.args[i].type)
            self.lscope[arg_name] = fn.args[i]
        entry = _triton.ir.basic_block.create(self.builder.context, "entry", fn)
        self.module.seal_block(entry)
        self.builder.set_insert_block(entry)
        # visit function body
        self.visit_compound_statement(node.body, add_scope=True)
        # finalize function
        self.builder.ret_void()

    def visit_arguments(self, node):
        arg_names = []
        for arg in node.args:
            arg_names += [ast.NodeVisitor.visit(self, arg)]
        kwarg_names = ast.NodeVisitor.visit(self, node.kwarg)
        return arg_names, kwarg_names

    def visit_arg(self, node):
        ast.NodeVisitor.generic_visit(self, node)
        return node.arg

    def visit_Assign(self, node):
        names = []
        for target in node.targets:
            names += [ast.NodeVisitor.visit(self, target)]
        assert len(names) == 1
        name = names[0]
        value = ast.NodeVisitor.visit(self, node.value)
        if isinstance(value, _triton.ir.value):
            self.module.set_value(name, value)
            self.module.scope.set_type(name, value.type)
        self.lscope[name] = value

    def visit_AugAssign(self, node):
        name = node.target.id
        lhs = ast.Name(id=name, ctx=ast.Load())
        rhs = ast.BinOp(lhs, node.op, node.value)
        assign = ast.Assign(targets=[node.target], value=rhs)
        ast.NodeVisitor.visit(self, assign)
        return self.get_value(name)

    def visit_Name(self, node):
        if type(node.ctx) == ast.Store:
            return node.id
        return self.get_value(node.id)

    def visit_Store(self, node):
        ast.NodeVisitor.generic_visit(self, node)

    def visit_Load(self, node):
        ast.NodeVisitor.generic_visit(self, node)

    def visit_Tuple(self, node):
        args = [ast.NodeVisitor.visit(self, x) for x in node.elts]
        return tuple(args)

    def visit_BinOp(self, node):
        lhs = ast.NodeVisitor.visit(self, node.left)
        rhs = ast.NodeVisitor.visit(self, node.right)
        if isinstance(lhs, int):
            lhs = self.builder.get_int32(lhs)
        if isinstance(rhs, int):
            rhs = self.builder.get_int32(rhs)
        lhs, rhs = broadcast(lhs, rhs, builder=self.builder)
        fn = {
            ast.Add: '__add__',
            ast.Sub: '__sub__',
            ast.Mult: '__mul__',
            ast.Gt: '__gt__',
            ast.Lt: '__lt__',
            ast.Div: '__div__',
            ast.Mod: '__mod__',
            ast.BitAnd: '__and__',
            ast.BitOr: '__or__',
            ast.BitXor: '__xor__',
        }[type(node.op)]
        if isinstance(lhs, _triton.ir.value):
            return getattr(lhs, fn)(rhs, builder=self.builder)
        return getattr(lhs, fn)(rhs)

    def visit_If(self, node):
        cond = ast.NodeVisitor.visit(self, node.test)
        if cond:
            self.visit_compound_statement(node.body)
        else:
            self.visit_compound_statement(node.orelse)

    def visit_IfExp(self, node):
        cond = ast.NodeVisitor.visit(self, node.test)
        if cond:
            return ast.NodeVisitor.visit(self, node.body)
        else:
            return ast.NodeVisitor.visit(self, node.orelse)

    def visit_Compare(self, node):
        lhs = ast.NodeVisitor.visit(self, node.left)
        rhs = ast.NodeVisitor.visit(self, node.comparators[0])
        if isinstance(lhs, int):
            lhs = self.builder.get_int32(lhs)
        if isinstance(rhs, int):
            rhs = self.builder.get_int32(rhs)
        lhs, rhs = broadcast(lhs, rhs, builder=self.builder)
        fn = {
            ast.Eq: '__eq__',
            ast.NotEq: '__ne__',
            ast.Lt: '__lt__',
            ast.LtE: '__le__',
            ast.Gt: '__gt__',
            ast.GtE: '__ge__',
            ast.Is: '__eq__',
            ast.IsNot: '__ne__',
        }[type(node.ops[0])]
        if isinstance(lhs, _triton.ir.value):
            return getattr(lhs, fn)(rhs, builder=self.builder)
        return getattr(lhs, fn)(rhs)

    def visit_UnaryOp(self, node):
        operand = ast.NodeVisitor.visit(self, node.operand)
        if isinstance(operand, int):
            operand = self.builder.get_int32(operand)
        # Handle non-constant
        _0f = self.builder.get_float32(0)
        _0i = self.builder.get_int32(0)
        if operand.type.is_block():
            _0f = self.builder.splat(_0f)
            _0i = self.builder.splat(_0i)
        # HANDLE MINUS OPERATOR
        if type(node.op) == ast.USub:
            if operand.type.is_floating():
                return _0f.__sub__(operand, builder=self.builder)
            elif operand.type.is_int():
                return _0i.__sub__(operand, builder=self.builder)

        raise NotImplementedError(f"Unsupported op: {node.op}")

    def visit_Str(self, node):
        return ast.literal_eval(node)

    def visit_Subscript(self, node):
        assert node.ctx.__class__.__name__ == "Load"
        lhs = ast.NodeVisitor.visit(self, node.value)
        slices = ast.NodeVisitor.visit(self, node.slice)
        if isinstance(lhs, _triton.ir.value):
            return lhs.__getitem__(slices, builder=self.builder)
        return lhs[slices]

    def visit_ExtSlice(self, node):
        return [ast.NodeVisitor.visit(self, dim) for dim in node.dims]

    def visit_For(self, node):
        iterator = ast.NodeVisitor.visit(self, node.iter.func)
        assert iterator == self.builtins['range']
        # create nodes
        st_target = ast.Name(id=node.target.id, ctx=ast.Store())
        ld_target = ast.Name(id=node.target.id, ctx=ast.Load())
        init_node = ast.Assign(targets=[st_target], value=node.iter.args[0])
        pos_cond_node = ast.BinOp(ld_target, ast.Lt(), node.iter.args[1])
        neg_cond_node = ast.BinOp(ld_target, ast.Gt(), node.iter.args[1])
        pos_step_node = ast.BinOp(node.iter.args[2], ast.Gt(), ast.Num(0))
        build_cond = lambda: where(ast.NodeVisitor.visit(self, pos_step_node),\
                                    ast.NodeVisitor.visit(self, pos_cond_node),\
                                    ast.NodeVisitor.visit(self, neg_cond_node),\
                                    builder=self.builder)
        #cond_node = neg_cond_node
        step_node = ast.AugAssign(target=st_target, op=ast.Add(), value=node.iter.args[2])
        # code generation
        current_bb = self.builder.get_insert_block()
        loop_bb = _triton.ir.basic_block.create(self.module.builder.context, "loop", current_bb.parent)
        next_bb = _triton.ir.basic_block.create(self.module.builder.context, "postloop", current_bb.parent)

        def continue_fn():
            ast.NodeVisitor.visit(self, step_node)
            cond = build_cond()
            return self.builder.cond_br(cond, loop_bb, next_bb)

        ast.NodeVisitor.visit(self, init_node)
        cond = build_cond()
        self.builder.cond_br(cond, loop_bb, next_bb)
        self.builder.set_insert_block(loop_bb)
        self.visit_compound_statement(node.body, add_scope=True)
        # TODO: handle case where body breaks control flow
        continue_fn()
        stop_bb = self.builder.get_insert_block()
        self.module.seal_block(stop_bb)
        self.module.seal_block(loop_bb)
        self.module.seal_block(next_bb)
        self.builder.set_insert_block(next_bb)

        for stmt in node.orelse:
            ast.NodeVisitor.generic_visit(self, stmt)

    def visit_Slice(self, node):
        lower = ast.NodeVisitor.visit(self, node.lower)
        upper = ast.NodeVisitor.visit(self, node.upper)
        step = ast.NodeVisitor.visit(self, node.step)
        return (lower, upper, step)

    def visit_Index(self, node):
        return ast.NodeVisitor.visit(self, node.value)

    def visit_NameConstant(self, node):
        return ast.NodeVisitor.visit(self, node.value)

    def visit_keyword(self, node):
        return {node.arg: ast.NodeVisitor.visit(self, node.value)}

    def visit_Call(self, node):
        fn = ast.NodeVisitor.visit(self, node.func)
        kws = dict()
        for keyword in node.keywords:
            kws.update(ast.NodeVisitor.visit(self, keyword))
        args = [ast.NodeVisitor.visit(self, arg) for arg in node.args]
        if hasattr(fn, '__self__') and isinstance(fn.__self__, _triton.ir.value) or \
           sys.modules[fn.__module__] is _triton.frontend:
            return fn(*args, builder=self.builder, **kws)
        return fn(*args, **kws)

    def visit_Num(self, node):
        val = node.n
        ty = type(val)
        if ty == int:
            return self.builder.get_int32(val)
        if ty == float:
            return self.builder.get_float32(val)
        raise NotImplementedError("Unsupported constant type: {}".format(ty))

    def visit_Attribute(self, node):
        lhs = ast.NodeVisitor.visit(self, node.value)
        return getattr(lhs, node.attr)

    def visit_Expr(self, node):
        ast.NodeVisitor.generic_visit(self, node)

    def visit_NoneType(self, node):
        return None

    def generic_visit(self, node):
        typename = type(node).__name__
        raise NotImplementedError("Unsupported node: {}".format(typename))



suffixes = {
    int: 'I', float: 'f', bool: 'B',\
    torch.float16: 'f16', torch.float32: 'f32', torch.float64: 'f64',
    torch.bool: 'i1', \
    torch.int8: 'i8', torch.int16: 'i16', torch.int32: 'i32', torch.int64: 'i64',
}

type_map = {
    'I': _triton.ir.type.get_int32,
    'f': _triton.ir.type.get_fp32,
    'B': _triton.ir.type.get_int1,
    'f16': _triton.ir.type.get_fp16,
    'f32': _triton.ir.type.get_fp32,
    'f64': _triton.ir.type.get_fp64,
    'i1': _triton.ir.type.get_int1,
    'i8': _triton.ir.type.get_int8,
    'i16': _triton.ir.type.get_int16,
    'i32': _triton.ir.type.get_int32,
    'i64': _triton.ir.type.get_int64,
}


def as_ir_type(context, obj):
    if isinstance(obj, torch.Tensor):
        ty = type_map[suffixes[obj.dtype]](context)
        return _triton.ir.type.make_ptr(ty, 1)
    return type_map[suffixes[obj.__class__]](context)


def cdiv(a, b):
    return (a + b - 1) // b


def pow2_divisor(N):
    if N % 16 == 0: return 16
    if N % 8 == 0: return 8
    if N % 4 == 0: return 4
    if N % 2 == 0: return 2
    return 1


class Binary:
    def __init__(self, module, kernel, num_warps, shared_mem):
        self.module = module
        self.kernel = kernel
        self.shared_mem = shared_mem
        self.num_warps = num_warps

    def __call__(self, stream, args, grid_0, grid_1=1, grid_2=1):
        stream.enqueue(self.kernel, grid_0, grid_1, grid_2, self.num_warps * 32, 1, 1, args, self.shared_mem)


class Kernel:
    def __init__(self, fn, grid):
        self.fn = fn
        self.grid = grid

    def __call__(self, *wargs, num_warps, **meta):
        # device inference
        tensor_idxs = [i for i, arg in enumerate(wargs) if isinstance(arg, torch.Tensor)]
        if len(tensor_idxs) == 0:
            raise ValueError("No Tensor argument found.")
        device = wargs[tensor_idxs[0]].device
        # type inference
        types_key = [None] * len(wargs)
        for i, arg in enumerate(wargs):
            prefix = 'P' if i in tensor_idxs else ''
            suffix = suffixes[arg.dtype] if i in tensor_idxs else suffixes[arg.__class__]
            types_key[i] = prefix + suffix
        types_key = '_'.join(types_key)
        # attribute key
        args = [arg.data_ptr() if i in tensor_idxs else arg for i, arg in enumerate(wargs)]
        attributes = {i: pow2_divisor(a) for i, a in enumerate(args) if isinstance(a, int)}
        attr_key = '_'.join(map(str, attributes.values()))
        # meta key
        meta_key = '_'.join([f'{k}_{v}' for k, v in meta.items()])
        # cache
        key = f'{device.type}_{device.index}_{types_key}_{attr_key}_{meta_key}'
        cache = self.fn.cache
        if key not in cache:
            # create IR module
            context = _triton.ir.context()
            # get just-in-time proto-type of kernel
            arg_types = [as_ir_type(context, arg) for arg in wargs]
            ret_type = _triton.ir.type.get_void(context)
            prototype = _triton.ir.type.make_function(ret_type, arg_types)
            # generate Triton-IR
            # export symbols visible from self.fn into code-generator object
            gscope = sys.modules[self.fn.src.__module__].__dict__
            generator = CodeGenerator(context, prototype, gscope=gscope, attributes=attributes, kwargs=meta)
            tree = ast.parse(inspect.getsource(self.fn.src))
            generator.visit(tree)
            tt_device = _triton.driver.cu_device(device.index, False)
            # Compile to machine code
            mod, ker, shared_mem = _triton.code_gen.add_passes_to_emit_bin(generator.module, tt_device, num_warps)
            cache[key] = Binary(mod, ker, num_warps, shared_mem)
            pass
        # # pack arguments
        fmt = ''.join(['P' if i in tensor_idxs else suffixes[arg.__class__] for i, arg in enumerate(wargs)])
        params = struct.pack(fmt, *args)
        # enqueue cached function into stream
        binary = cache[key]
        cu_stream = torch.cuda.current_stream(device.index).cuda_stream
        stream = _triton.driver.cu_stream(cu_stream, False)
        binary(stream, params, *self.grid(meta))


class Autotuner:
    def __init__(self, src, configs, key):
        if not configs:
            self.configs = [Config(dict(), num_warps=4)]
        else:
            self.configs = configs
        arg_names = inspect.getfullargspec(src).args
        self.key_idx = [arg_names.index(k) for k in key]
        self.cache = dict()
        self.kernel = None

    def __call__(self, *args, **meta):
        key = tuple([args[i] for i in self.key_idx])
        if key not in self.cache:
            timings = dict()
            for config in self.configs:
                # check for conflicts, i.e. meta-parameters both provided
                # as kwargs and by the autotuner
                conflicts = meta.keys() & config.meta.keys()
                if conflicts:
                    raise ValueError(
                        f"Conflicting meta-parameters: {', '.join(conflicts)}."
                        " Make sure that you don't re-define auto-tuned symbols."
                    )
                # augment meta-parameters with tunable ones
                current = dict(meta, **config.meta)
                kernel_call = lambda: self.kernel(*args, num_warps=config.num_warps, **current)
                timings[config] = triton.testing.do_bench(kernel_call)
            self.cache[key] = builtins.min(timings, key=timings.get)
        config = self.cache[key]
        self.kernel(*args, num_warps=config.num_warps, **meta, **config.meta)


class JITFunction:
    def __init__(self, src, configs, key):
        self.src = src
        self.configs = configs
        self.cache = dict()
        self.key = key
        self.tuner = Autotuner(src, configs, key)

    def __getitem__(self, grid_fn):
        self.tuner.kernel = Kernel(self, grid_fn)
        return self.tuner


class Config:
    def __init__(self, meta, num_warps=4):
        self.meta = meta
        self.num_warps = num_warps


def jit(configs=None, key=None):
    if configs is None:
        configs = []
    if key is None:
        key = []

    def decorator(fn):
        return JITFunction(fn, configs, key)

    return decorator
