# Nodes represent a definition of a value in our graph of operators.
from typing import TYPE_CHECKING, Union, Callable, Any, Set, Tuple, List, Optional, Dict
import torch

if TYPE_CHECKING:
    from .graph import Graph

BaseArgumentTypes = Union[str, int, float, bool, torch.dtype, torch.Tensor]
base_types = BaseArgumentTypes.__args__  # type: ignore

Target = Union[Callable[..., Any], str]

Argument = Optional[Union[
    Tuple[Any, ...],  # actually Argument, but mypy can't represent recursive types
    List[Any],  # actually Argument
    Dict[str, Any],  # actually Argument
    slice,  # Slice[Argument, Argument, Argument], but slice is not a templated type in typing
    'Node',
    BaseArgumentTypes
]]

class Node:
    def __init__(self, graph: 'Graph', name: str, op: str, target: Target,
                 args: Tuple[Argument, ...], kwargs: Dict[str, Argument],
                 type : Optional[Any] = None) -> None:
        self.graph = graph
        self.name = name  # unique name of value being created
        assert op in ['placeholder', 'call_method', 'call_module', 'call_function', 'get_attr', 'output', 'root']
        self.op = op  # the kind of operation = placeholder|call_method|call_module|call_function|get_attr
        if op in ['call_method', 'call_module']:
            assert isinstance(target, str)
        self.target = target  # for method/module/function, the name of the method/module/function/attr
        # being invoked, e.g add, layer1, or torch.add
        self._args : Tuple[Argument, ...] = ()
        self._kwargs : Dict[str, Argument] = {}
        self.args, self.kwargs = args, kwargs
        # All of the nodes that use the value produced by this Node
        # Note one user may correspond to several uses, e.g. the node fo `x + x`
        # would appear once here, but represents two uses.
        #
        # Is a dict to act as an "ordered set". Keys are significant, value dont-care
        self.users : Dict['Node', None] = {}
        # Type expression representing the output value of this node.
        # This should contain the same class of Type objects that would appear
        # as type annotations for function inputs/outputs.
        #
        # For placeholder nodes, this value will be used to type-annotate the
        # generated function parameters.
        # For the return ndoe, this value will be used to type-annotate the
        # generated function return type. (Note this is a special case. `return`
        # does not produce a value, it's more of a notation. Thus, this value
        # describes the type of args[0] in the `return` node.
        self.type : Optional[Any] = type
        self._prev = self
        self._next = self
        self._erased = False

    @property
    def next(self) -> 'Node':
        return self._next

    @property
    def prev(self) -> 'Node':
        return self._prev

    def prepend(self, x: 'Node'):
        """Insert x before this node in the list of nodes in the graph.
           Before: p -> self
                   bx -> x -> ax
           After:  p -> x -> self
                   bx -> ax

        Args:
            x (Node): The node to put before this node. Must be a member of the same graph.
        """
        assert self.graph == x.graph, "Attempting to move a Node into a different Graph"
        x._remove_from_list()
        p = self._prev
        p._next, x._prev = x, p
        x._next, self._prev = self, x

    def append(self, x: 'Node'):
        """Insert x after this node in the list of nodes in the graph.
        Equvalent to `self.next.prepend(x)`

        Args:
            x (Node): The node to put after this node. Must be a member of the same graph.
        """
        self._next.prepend(x)

    def _remove_from_list(self):
        p, n = self._prev, self._next
        p._next, n._prev = n, p

    @property
    def args(self) -> Tuple[Argument, ...]:
        return self._args

    @args.setter
    def args(self, a : Tuple[Argument, ...]):
        self._update_args_kwargs(new_args=a, new_kwargs=self._kwargs)

    @property
    def kwargs(self) -> Dict[str, Argument]:
        return self._kwargs

    @kwargs.setter
    def kwargs(self, k : Dict[str, Argument]):
        self._update_args_kwargs(new_args=self._args, new_kwargs=k)

    def _update_args_kwargs(self, new_args : Tuple[Argument, ...], new_kwargs : Dict[str, Argument]):
        old_defs = self._collect_all_defs()
        self._args = new_args
        self._kwargs = new_kwargs
        new_defs = self._collect_all_defs()
        for to_remove in old_defs - new_defs:
            to_remove.users.pop(self)
        for to_add in new_defs - old_defs:
            to_add.users.setdefault(self)

    def _collect_all_defs(self) -> Set['Node']:
        defs = set()
        map_arg(self._args, lambda n: defs.add(n))
        map_arg(self._kwargs, lambda n: defs.add(n))
        return defs

    def __repr__(self) -> str:
        return self.name

    def replace_all_uses_with(self, replace_with : 'Node') -> List['Node']:
        """
        Replace all uses of `self` in the Graph with the Node `replace_with`.
        Returns the list of nodes on which this change was made.
        """
        to_process = list(self.users)
        for use_node in to_process:
            def maybe_replace_node(n : Node) -> Node:
                if n == self:
                    return replace_with
                else:
                    return n

            new_args = map_arg(use_node.args, maybe_replace_node)
            new_kwargs = map_arg(use_node.kwargs, maybe_replace_node)
            assert isinstance(new_args, tuple)
            assert isinstance(new_kwargs, dict)
            use_node._update_args_kwargs(new_args, new_kwargs)

        assert len(self.users) == 0
        return to_process


def map_arg(a: Argument, fn: Callable[[Node], Argument]) -> Argument:
    """ apply fn to each Node appearing arg. arg may be a list, tuple, slice, or dict with string keys. """
    if isinstance(a, (tuple, list)):
        return type(a)(map_arg(elem, fn) for elem in a)
    elif isinstance(a, dict):
        return {k: map_arg(v, fn) for k, v in a.items()}
    elif isinstance(a, slice):
        return slice(map_arg(a.start, fn), map_arg(a.stop, fn), map_arg(a.step, fn))
    elif isinstance(a, Node):
        return fn(a)
    else:
        return a
