from triton._C.libtriton.triton import ir
from triton._C.libtriton.triton import frontend
import triton
from functools import wraps


def _patch(fn):

    # convert block/dtype to ir values
    def _to_ir(x):
        forward_handle = isinstance(x, (dtype, block))
        return x.handle if forward_handle else x

    def _from_ir(x):
        return block(x) if isinstance(x, ir.value) else x

    def wrapper(*args, **kwargs):
        args = [_to_ir(x) for x in args]
        kwargs = {k: _convert(v) for k, v in kwargs.items()}
        ret = fn(*args, **kwargs)
        if isinstance(ret, tuple):
            return map(_from_ir, ret)
        return ret

    return wrapper


for name in dir(frontend):
    fn = getattr(frontend, name)
    if callable(fn):
        setattr(frontend, name, _patch(fn))


def builtin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if 'builder' not in kwargs or \
           kwargs['builder'] is None:
            raise ValueError("Builder argument must be provided outside of JIT functions")
        return fn(*args, **kwargs)

    if wrapper.__doc__:
        wrapper.__doc__ += """\
:param builder: IR builder to generate code into, optional from within @triton.jit functions
    :type builder: triton.ir.builder
"""
    return wrapper


class dtype:
    def __init__(self, handle):
        self.handle = handle


class block:
    def __init__(self, handle):
        self.handle = handle
        self.type = handle.type

    @builtin
    def __add__(self, other, builder=None):
        return frontend.add(self, other, builder)

    @builtin
    def __sub__(self, other, builder=None):
        return frontend.sub(self, other, builder)

    @builtin
    def __mul__(self, other, builder=None):
        return frontend.mul(self, other, builder)

    @builtin
    def __truediv__(self, other, builder=None):
        return frontend.truediv(self, other, builder)

    @builtin
    def __mod__(self, other, builder=None):
        return frontend.mod(self, other, builder)

    # unary operators
    @builtin
    def __neg__(self, builder=None):
        return frontend.minus(self, builder)

    @builtin
    def __invert__(self, builder=None):
        return frontend.invert(self, builder)

    # bitwise operators

    @builtin
    def __and__(self, other, builder=None):
        return frontend.and_(self, other, builder)

    @builtin
    def __or__(self, other, builder=None):
        return frontend.or_(self, other, builder)

    @builtin
    def __xor__(self, other, builder=None):
        return frontend.xor_(self, other, builder)

    @builtin
    def __lshift__(self, other, builder=None):
        return frontend.shl(self, other, builder)

    @builtin
    def __rshift__(self, other, builder=None):
        return frontend.lshr(self, other, builder)

    # comparison operators

    @builtin
    def __gt__(self, other, builder=None):
        return frontend.greater_than(self, other, builder)

    @builtin
    def __ge__(self, other, builder=None):
        return frontend.greater_equal(self, other, builder)

    @builtin
    def __lt__(self, other, builder=None):
        return frontend.less_than(self, other, builder)

    @builtin
    def __le__(self, other, builder=None):
        return frontend.less_equal(self, other, builder)

    @builtin
    def __eq__(self, other, builder=None):
        return frontend.equal(self, other, builder)

    @builtin
    def __ne__(self, other, builder=None):
        return frontend.not_equal(self, other, builder)

    @builtin
    def __getitem__(self, slices, builder=None):
        print(slices)
        assert False

    @builtin
    def to(self, dtype, builder=None):
        assert False


# -----------------------
# SPMD Programming Model
# -----------------------


@builtin
def program_id(axis, builder=None):
    """
    Returns the id of the current program instance along the given `axis`.
    Triton uses an SPMD model in which different @triton.jit functions run in parallel with different `program_id`s.

    :param axis: The axis of the 3D launch grid. Has to be either 0, 1 or 2.
    :type axis: int
    """
    return frontend.program_id(axis, builder)


@builtin
def num_programs(axis, builder=None):
    """
    Returns the number of program instances launched along the given `axis`.

    :param axis: The axis of the 3D launch grid. Has to be either 0, 1 or 2.
    :type axis: int
    """
    return frontend.num_programs(axis, builder)


# -----------------------
# Block Initialization
# -----------------------


@builtin
def arange(start, end, builder=None):
    """
    Returns contiguous values within the open interval [start, end).

    :param start: Start of the interval.
    :type start: int
    :param stop: End of the interval.
    :type stop: int
    """
    return frontend.arange(start, end, builder)


@builtin
def zeros(shape, dtype, builder=None):
    """
    Returns a block filled with the scalar value 0 and the given shape.

    :param shape: Shape of the new array, e.g., (8, 16) or (8, )
    :type shape: tuple of ints
    :param dtype: Data-type of the new array, e.g., triton.float16
    :type dtype: triton.ir.dtype
    """
    return frontend.zeros(shape, dtype, builder)


# -----------------------
# Shape Manipulation
# -----------------------


@builtin
def broadcast(input, other, builder=None):
    """
    Tries to broadcast two blocks to a common compatible shape.

    :param input: The first input block.
    :type input: triton.ir.value
    :param other: The second input block.
    :type other: triton.ir.value
    """
    return frontend.broadcast(input, other, builder)


@builtin
def broadcast_to(input, shape, builder=None):
    """
    Tries to broadcast a block to a new shape.

    :param input: The input block.
    :type input: triton.value
    :param shape: The new shape.
    :type shape: tuple of int
    """
    return frontend.broadcast_to(input, shape, builder)


# -----------------------
# Linear Algebra
# -----------------------


@builtin
def dot(input, other, builder=None):
    """
    Returns the matrix product of two blocks.
    The two blocks must be two dimensionals and have compatible inner dimensions.

    :param input: The first block to be multiplied.
    :type input: 2D block of scalar-type in {`float16`, `float32`}
    :param other: The second block to be multiplied.
    :type other: 2D block of scalar-type in {`float16`, `float32`}
    """
    return frontend.dot(input, other, builder)


# -----------------------
# Memory Operations
# -----------------------


@builtin
def load(pointer, mask=None, other=None, builder=None):
    """
    Return a block of data whose values are, elementwise, loaded from memory at location defined by `pointer`.

    :param pointer: Pointer to the data to be loaded.
    :type pointer: Block of triton.pointer
    :param mask: if mask[idx] is false, do not load the data at `pointer[idx]`.
    :type mask: Block of triton.bool, optional
    :param other: if mask[idx] is false, return other[idx] instead of 'pointer[idx]`
    :type other: Block of triton.value, optional
    """
    return frontend.load(pointer, mask, other, builder)


@builtin
def store(pointer, value, mask=None, builder=None):
    """
    Stores `value` block of elements in memory, element-wise, at the memory locations specified by `pointer`. 

    :param pointer: The memory locations where the elements of `value` are stored.
    :type pointer: Block of triton.pointer
    :param value: The block of elements to be stored.
    :type value: Block of triton.value
    :param mask: If mask[idx] is false, do not store `value[idx]` at `pointer[idx]`.
    :type mask: Block of triton.bool, optional
    """
    return frontend.store(pointer, value, mask, builder)


@builtin
def atomic_cas(ptr, cmp, val, builder=None):
    return frontend.atomic_cas(ptr, cmp, val, builder)


@builtin
def atomic_xchg(ptr, val, builder=None):
    return frontend.atomic_xchg(ptr, val, builder)


# -----------------------
# Conditioning
# -----------------------


@builtin
def where(condition, x, y, builder=None):
    """
    Returns a block of elements from either `x` or `y`, depending on `condition`.
    Note that `x` and `y` are always evaluated regardless of the value of `condition`.
    If you want to avoid unintented memory operations, use the `mask` arguments in `triton.load` and `triton.store` instead.

    :param condition: When True (nonzero), yield x, otherwise yield y.
    :type condition: Block of triton.bool
    :param x: values selected at indices where condition is True.
    :param y: values selected at indices where condition is False.
    """
    return frontend.where(condition, x, y, builder)


# -----------------------
# Math
# -----------------------


@builtin
def exp(x, builder=None):
    return frontend.exp(x, builder)


@builtin
def log(x, builder=None):
    return frontend.log(x, builder)


# -----------------------
# Reductions
# -----------------------


@builtin
def max(input, axis, builder=None):
    return frontend.max(input, axis, builder)


@builtin
def min(input, axis, builder=None):
    return frontend.min(input, axis, builder)


@builtin
def sum(input, axis, builder=None):
    return frontend.sum(input, axis, builder)


# -----------------------
# Internal for debugging
# -----------------------


@builtin
def debug_barrier(builder=None):
    return frontend.debug_barrier(builder)


# -----------------------
# Standard library
# -----------------------


@triton.jit
def minimum(x, y):
    return triton.where(x < y, x, y)


@triton.jit
def maximum(x, y):
    return triton.where(x > y, x, y)


@triton.jit
def softmax(x):
    z = x - triton.max(x, 0)
    num = triton.exp(z)
    den = triton.sum(num, 0)
    return num / den


def cdiv(x, y):
    return (x + y - 1) // y