from tools.codegen.model import *
from tools.codegen.api.types import TensorOptionsArguments, CppArgument, ThisArgument
import tools.codegen.local as local
from typing import Optional, Sequence, Union, Callable, List, Tuple
import copy
from dataclasses import dataclass

# This file describes the translation of JIT schema to the public C++
# API, which is what people use when they call functions like at::add.
#
# Prominent characteristics of the C++ API:
#
#   - dtype, layout, device and pin_memory are collected into
#     a single C++ type TensorOptions  (the legacy dispatcher API
#     also has this, but tensor options is really most relevant
#     for the C++ API; it makes calling kwarg factory functions
#     pleasant)
#
#   - for 'use_c10_dispatcher: full' functions, optional tensors are
#     represented explicitly using c10::optional
#
#   - defaulting lives here (in fact, the dispatcher is completely
#     oblivious of defaults!)
#
# BTW: policy on name collisions: we try not to have types with
# collisions, but functions are fair game to collide

def name(func: FunctionSchema) -> str:
    name = str(func.name.name)
    if func.is_out_fn():
        name += '_out'
    return name

# Translation of "value types" in JIT schema to C++ API type.  Value
# types look the same no matter if they are argument types are return
# types.  Returns None if the type in question is not a value type.
def valuetype_type(t: Type) -> Optional[str]:
    if isinstance(t, BaseType):
        if t.name == BaseTy.Tensor:
            return None
        elif t.name == BaseTy.int:
            return 'int64_t'
        elif t.name == BaseTy.float:
            return 'double'
        elif t.name == BaseTy.str:
            return 'std::string'
        elif t.name in [BaseTy.bool, BaseTy.QScheme, BaseTy.Scalar,
                        BaseTy.ScalarType, BaseTy.Generator, BaseTy.Storage,
                        BaseTy.Layout, BaseTy.Device, BaseTy.MemoryFormat,
                        BaseTy.Dimname, BaseTy.ConstQuantizerPtr]:
            # These C++ names line up with their schema names
            return t.name.name
        else:
            raise AssertionError(f"unsupported type: {t}")
    elif isinstance(t, OptionalType):
        elem = valuetype_type(t.elem)
        if elem is None:
            return None
        return f"c10::optional<{elem}>"
    elif isinstance(t, ListType):
        if str(t.elem) == 'bool':
            assert t.size is not None
            return f"std::array<bool,{t.size}>"
        else:
            return None
    else:
        raise AssertionError(f"unrecognized type {repr(t)}")

# Translation of types occuring in JIT arguments to a C++ argument type.
def argumenttype_type(t: Type, *, mutable: bool) -> str:
    # If it's a value type, do the value type translation
    r = valuetype_type(t)
    if r is not None:
        return r

    if isinstance(t, BaseType):
        if t.name == BaseTy.Tensor:
            if mutable:
                return 'Tensor &'
            else:
                return 'const Tensor &'
        else:
            raise AssertionError(f"base type should have been value type {t}")
    elif isinstance(t, OptionalType):
        if str(t.elem) == 'Tensor':
            if mutable:
                return 'Tensor &'  # TODO: fix this discrepancy
            else:
                if local.use_c10_dispatcher().dispatcher_uses_new_style():
                    return 'const c10::optional<Tensor>&'
                else:
                    return 'const Tensor &'
        elem = argumenttype_type(t.elem, mutable=mutable)
        return f"c10::optional<{elem}>"
    elif isinstance(t, ListType):
        # TODO: remove these special cases, ArrayRef fallthrough works fine
        if str(t.elem) == 'int':
            return "IntArrayRef"
        elif str(t.elem) == 'Tensor':
            return "TensorList"
        elif str(t.elem) == 'Dimname':
            return "DimnameList"
        # TODO: do something reasonable about lists of optional tensors
        elif (not local.use_c10_dispatcher().dispatcher_uses_new_style()) and str(t.elem) == 'Tensor?':
            return "TensorList"
        elem = argumenttype_type(t.elem, mutable=mutable)
        # TODO: explicitly qualify namespace here
        return f"ArrayRef<{elem}>"
    else:
        raise AssertionError(f"unrecognized type {repr(t)}")

# Translate a JIT argument into its C++ type
def argument_type(a: Argument) -> str:
    return argumenttype_type(a.type, mutable=a.is_write)

# Translation of a (non-multi) return type from JIT to C++
def returntype_type(t: Type, *, mutable: bool) -> str:
    r = valuetype_type(t)
    if r is not None:
        return r

    if isinstance(t, BaseType):
        if t.name == BaseTy.Tensor:
            if mutable:
                return 'Tensor &'
            else:
                return 'Tensor'
    elif isinstance(t, ListType):
        elem = returntype_type(t.elem, mutable=mutable)
        assert t.size is None, f"fixed size list returns not supported: {t}"
        return f"std::vector<{elem}>"

    raise AssertionError(f"unrecognized return type {t}")

# Translation of a single return to its C++ type
def return_type(r: Return) -> str:
    return returntype_type(r.type, mutable=r.is_write)

# Translation of a full (possibly multi) return from JIT to its C++ type
def returns_type(rs: Sequence[Return]) -> str:
    if len(rs) == 0:
        return 'void'
    elif len(rs) == 1:
        return return_type(rs[0])
    else:
        args = ','.join(map(return_type, rs))
        return f'std::tuple<{args}>'

JIT_TO_CPP_DEFAULT = {
    'False': 'false',
    'True': 'true',
    'None': 'c10::nullopt',  # UGH this one is type directed
    'Mean': 'at::Reduction::Mean',
    '[]': '{}',
    'contiguous_format': 'MemoryFormat::Contiguous',
    'long': 'at::kLong',
}

# Convert a JIT default into C++ expression representing the default
def default_expr(d: str, t: Type) -> str:
    if d == 'None' and str(t) == 'Tensor?':
        return '{}'
    if isinstance(t, BaseType) and t.name is BaseTy.str:
        # Schema allows single quotes but C++ needs double
        if len(d) >= 2 and d[0] == "'" and d[-1] == "'":
            s = ''
            i = 1
            while i + 1 < len(d):
                if d[i] != '\\':
                    if d[i] == '"':
                        s += '\\"'
                    else:
                        s += d[i]
                    i += 1
                else:
                    if d[i + 1] == "'":
                        s += "'"
                    else:
                        s += d[i:i + 2]
                    i += 2

            return f'"{s}"'

    if isinstance(t, OptionalType):
        if d == 'None':
            return 'c10::nullopt'

        return default_expr(d, t.elem)

    if isinstance(t, ListType):
        if (d.startswith('[') and d.endswith(']')):
            return '{' + d[1:-1] + '}'
        elif t.size is None:
            # NOTE: Sized lists can have scalar defaults
            raise ValueError(f"Expected a list default '[...]' but found: '{d}'")

    return JIT_TO_CPP_DEFAULT.get(d, d)

# Convert an argument into its C++ API form
def argument(a: Union[Argument, TensorOptionsArguments, ThisArgument]) -> CppArgument:
    if isinstance(a, Argument):
        return CppArgument(
            type=argument_type(a),
            name=a.name,
            default=default_expr(a.default, a.type) if a.default is not None else None,
            argument=a,
        )
    elif isinstance(a, ThisArgument):
        return CppArgument(
            type=argument_type(a.argument),
            name="const_cast<Tensor&>(*this)",  # this is an abuse but it's convenient
            default=None,
            argument=a,
        )
    elif isinstance(a, TensorOptionsArguments):
        default = None
        if all(x.default == "None" for x in a.all()):
            default = '{}'
        elif a.dtype.default == "long":
            default = 'at::kLong'  # TODO: this is wrong
        return CppArgument(
            type='const TensorOptions &',
            name='options',
            default=default,
            argument=a,
        )
    else:
        assert_never(a)

@dataclass(frozen=True)
class CppSignature:
    returns: Tuple[Return, ...]
    arguments: Tuple[Union[Argument, TensorOptionsArguments, ThisArgument], ...]

    def cpp_arguments(self) -> Sequence[CppArgument]:
        return list(map(argument, self.arguments))

    # Return arguments as a comma separated list, i.e. like they would be in a C++
    # function signature. Include default values for arguments.
    def cpp_arguments_str(self, with_defaults: bool) -> str:
        args_without_this = [argument(a) for a in self.arguments if not isinstance(a, ThisArgument)]
        if with_defaults:
            return ', '.join(map(str, args_without_this))
        else:
            return ', '.join(map(lambda s: s.str_no_default(), args_without_this))


@dataclass(frozen=True)
class CppSignatureGroup:
    # arguments contains the arguments for the C++ signature as it is represented
    # in the JIT schema.
    signature: CppSignature

    # gathered_signature is an alternative C++ signature in which TensorOptions are
    # gathered into one TensorOptions object instead of being scattered into
    # ScalarType, Layout, Device. This is only present for factory operators,
    # other operators have this set to None. This can be used to generate a
    # convenience API in the C++ frontend so users can call using TensorOptions objects.
    gathered_signature: Optional[CppSignature]

    # If it is a factory op, this returns the arguments for the convenience API
    # that takes TensorOptions. If it is not a factory op and doesn't have
    # a gathered signature, then this returns the regular signature instead.
    def signature_prefer_gathered(self) -> CppSignature:
        if self.gathered_signature is not None:
            return self.gathered_signature
        else:
            return self.signature


def signature_group(
    func: FunctionSchema, *, method: bool = False,
) -> CppSignatureGroup:
    args: List[Union[Argument, ThisArgument, TensorOptionsArguments]] = []
    args.extend(func.out_arguments)

    if method:
        args.extend(ThisArgument(a) if a.name == "self" else a for a in func.arguments)
    else:
        args.extend(func.arguments)

    gathered_args = copy.deepcopy(args)

    # group up arguments for tensor options
    def pred(name: str, ty: Type) -> Callable[[Argument], bool]:
        return lambda a: a.name == name and a.type in [ty, OptionalType(ty)]
    predicates = [  # order matters
        pred('dtype', Type.parse('ScalarType')),
        pred('layout', Type.parse('Layout')),
        pred('device', Type.parse('Device')),
        pred('pin_memory', Type.parse('bool')),
    ]

    has_tensoroptions_argument = False
    i = 0
    while i < len(func.kwarg_only_arguments):
        # If there is enough space...
        if i <= len(func.kwarg_only_arguments) - len(predicates):
            # And the next len(predicates) arguments look like TensorOptions arguments
            if all(p(a) for p, a in zip(predicates, func.kwarg_only_arguments[i : i + len(predicates)])):
                has_tensoroptions_argument = True
                # Group them together as one argument
                gathered_args.append(TensorOptionsArguments(
                    dtype=func.kwarg_only_arguments[i],
                    layout=func.kwarg_only_arguments[i + 1],
                    device=func.kwarg_only_arguments[i + 2],
                    pin_memory=func.kwarg_only_arguments[i + 3],
                ))
                i += len(predicates)
                continue
        gathered_args.append(func.kwarg_only_arguments[i])
        i += 1

    args.extend(func.kwarg_only_arguments)

    if has_tensoroptions_argument:
        return CppSignatureGroup(
            signature=CppSignature(arguments=tuple(args), returns=tuple(func.returns)),
            gathered_signature=CppSignature(arguments=tuple(gathered_args), returns=tuple(func.returns)),
        )
    else:
        assert gathered_args == args
        return CppSignatureGroup(
            signature=CppSignature(arguments=tuple(args), returns=tuple(func.returns)),
            gathered_signature=None,
        )
