from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ValueId = str
BlockId = str
NumericDomain = Literal["default", "signed", "unsigned", "float"]


@dataclass(frozen=True)
class SourceRef:
    """Best-effort location in a frontend-specific input artifact."""

    frontend: str
    offset: int | None = None
    line: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class TypeRef:
    """A deliberately loose type reference.

    Frontends may provide precise types, but dynamic VMs can leave this as
    ``unknown`` without lying to later passes.
    """

    name: str = "unknown"


@dataclass(frozen=True)
class Expr:
    source: SourceRef | None = None
    type: TypeRef = field(default_factory=TypeRef)


@dataclass(frozen=True)
class Var(Expr):
    name: ValueId = ""


@dataclass(frozen=True)
class Const(Expr):
    value: Any = None


@dataclass(frozen=True)
class UndefinedLiteral(Expr):
    """An undefined/uninitialized value distinct from null.

    Its truthiness and language-specific operations require frontend-owned,
    data-only simulator facts; generic recovery never guesses them.
    """


@dataclass(frozen=True)
class CurrentException(Expr):
    """The value of the exception active in the current generic-IR handler."""


@dataclass(frozen=True)
class ExceptionResumePosition(Expr):
    """Opaque VM resume position supplied to an exception handler."""


@dataclass(frozen=True)
class ExceptionRewrite(Expr):
    """A conditional rewrite of an exception value.

    ``predicate`` and ``replacement`` are generic data operands.  The source
    VM may provide their concrete values, but generic recovery never infers a
    language-specific exception family or message.
    """

    value: Expr = field(default_factory=Expr)
    predicate: Expr = field(default_factory=Expr)
    replacement: Expr = field(default_factory=Expr)
    retain_input_as_cause: bool = False


@dataclass(frozen=True)
class ExceptionCleanupValue(Expr):
    """A successful value produced by a conditional exception cleanup.

    The cleanup predicate and exceptional input remain attached to the value
    so later generic passes and the simulator cannot silently erase the
    exceptional alternative while carrying the normal stack result.
    """

    value: Expr = field(default_factory=Expr)
    input_exception: Expr | None = None
    propagate_input: bool = True
    predicate: Expr | None = None
    predicate_operand: Expr | None = None


@dataclass(frozen=True)
class UnaryOp(Expr):
    op: str = ""
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class BinaryOp(Expr):
    op: str = ""
    left: Expr = field(default_factory=Expr)
    right: Expr = field(default_factory=Expr)
    semantics: Literal["static", "dynamic"] = "dynamic"
    numeric_domain: NumericDomain = "default"
    bit_width: int | None = None
    overflow_policy: Literal["wrap", "trap"] = "wrap"


@dataclass(frozen=True)
class CallEffectSummary:
    """VM-neutral, descriptive effects of a call.

    This is analysis metadata, not executable behavior.  ``unknown`` keeps
    core passes fail-closed when a callee cannot be resolved safely.
    """

    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    may_raise: bool = True
    may_suspend: bool = False
    returns: int | Literal["unknown"] = "unknown"
    unknown: bool = True

    @property
    def may_mutate(self) -> bool:
        return self.unknown or bool(self.writes)


@dataclass(frozen=True)
class Call(Expr):
    callee: Expr = field(default_factory=Expr)
    args: tuple[Expr, ...] = ()
    keywords: tuple[TableField, ...] = ()
    returns: int | Literal["unknown"] = 1
    effect_summary: CallEffectSummary | None = None


@dataclass(frozen=True)
class MultiReturn(Expr):
    """Represents a dynamic-language expression that may yield many values."""

    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class Phi(Expr):
    """SSA phi node with auditable predecessor identity.

    ``incoming`` keeps the long-standing logical labels used by structured
    regions.  When a low-level CFG has parallel edges from the same block,
    ``edge_ids`` carries one concrete CFG edge ID per incoming value; this
    avoids collapsing distinct edges while preserving source compatibility for
    existing block-keyed Phi users.
    """

    incoming: tuple[tuple[str, Expr], ...] = ()
    edge_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Global(Expr):
    name: str = ""


@dataclass(frozen=True)
class CapturedVar(Expr):
    """A value supplied by an enclosing function or VM activation environment."""

    name: str = ""


@dataclass(frozen=True)
class Placeholder(Expr):
    """Temporary value identity used while generic effects recover VM state."""

    token: str = ""
    label: str = "placeholder"


@dataclass(frozen=True)
class ResumeInput(Expr):
    """Value supplied when a suspended generator or coroutine is resumed."""

    pass


@dataclass(frozen=True)
class TableField:
    key: Expr
    value: Expr


@dataclass(frozen=True)
class TableLiteral(Expr):
    array_items: tuple[Expr, ...] = ()
    fields: tuple[TableField, ...] = ()


@dataclass(frozen=True)
class ArrayLiteral(Expr):
    kind: Literal["list", "tuple"] = "list"
    items: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class SetLiteral(Expr):
    items: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class CollectionProjection(Expr):
    kind: Literal["list", "set"] = "list"
    target: Var = field(default_factory=Var)
    iterable: Expr = field(default_factory=Expr)
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class ObjectLiteral(Expr):
    fields: tuple[TableField, ...] = ()


@dataclass(frozen=True)
class MapLiteral(Expr):
    fields: tuple[TableField, ...] = ()


@dataclass(frozen=True)
class NewObject(Expr):
    type_name: str = "unknown"
    constructor: Expr | None = None
    args: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class GetAttr(Expr):
    obj: Expr = field(default_factory=Expr)
    attr: str = ""


@dataclass(frozen=True)
class GetItem(Expr):
    obj: Expr = field(default_factory=Expr)
    key: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class IndirectCall(Expr):
    """A VM-neutral dynamic dispatch with an explicit selector operand."""

    selector: Expr = field(default_factory=Expr)
    signature: str = "unknown"


@dataclass(frozen=True)
class IndirectRef(Expr):
    """A VM-neutral writable reference to a value location."""

    target: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class Stmt:
    source: SourceRef | None = None


@dataclass(frozen=True)
class Assign(Stmt):
    target: Expr = field(default_factory=Var)
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class AssignMany(Stmt):
    targets: tuple[Var, ...] = ()
    values: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class StoreAttr(Stmt):
    obj: Expr = field(default_factory=Expr)
    attr: str = ""
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class StoreItem(Stmt):
    obj: Expr = field(default_factory=Expr)
    key: Expr = field(default_factory=Expr)
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class ExprStmt(Stmt):
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class Delete(Stmt):
    """Delete a generic writable location."""

    target: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class Unsupported(Stmt):
    message: str = "unsupported"
    detail: str | None = None
    raw: tuple[str, ...] = ()


@dataclass(frozen=True)
class Raise(Stmt):
    value: Expr = field(default_factory=Expr)
    cause: Expr | None = None


@dataclass(frozen=True)
class Reraise(Stmt):
    """Propagate either the active exception or one explicit handler value.

    ``resume_slots`` are opaque VM handler-state values consumed with an
    explicit rethrow.  They preserve provenance and stack order without
    pretending to model a frontend's instruction pointer.
    """

    value: Expr | None = None
    resume_slots: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class Yield(Stmt):
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class ExceptHandler:
    """A recovered handler attached to a generic protected region."""

    # ``None`` is a VM-neutral catch-all handler.  It is distinct from an
    # unresolved expression: core uses it only when the CFG proves that every
    # exceptional transfer for a protected region reaches this handler.
    exception_type: Expr | None = None
    binding: Var | None = None
    body: tuple[Stmt | Terminator, ...] = ()


@dataclass(frozen=True)
class Try(Stmt):
    """A structured protected region recovered by the VM-neutral core."""

    body: tuple[Stmt | Terminator, ...] = ()
    handlers: tuple[ExceptHandler, ...] = ()


@dataclass(frozen=True)
class If(Stmt):
    condition: Expr = field(default_factory=Expr)
    then_body: tuple[Stmt, ...] = ()
    else_body: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class Switch(Stmt):
    """A structured VM-neutral multi-way dispatch."""

    selector: Expr = field(default_factory=Expr)
    cases: tuple[tuple[Expr, tuple[Stmt, ...]], ...] = ()
    default_body: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class While(Stmt):
    condition: Expr = field(default_factory=Expr)
    body: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class DoWhile(Stmt):
    """A body-first loop recovered by the VM-neutral core.

    The body is executed once before ``condition`` is evaluated.  It exists
    separately from ``While`` so core recovery never has to manufacture an
    initial condition evaluation while preserving a stack VM's ordering.
    """

    body: tuple[Stmt, ...] = ()
    condition: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class ForEach(Stmt):
    target: Var = field(default_factory=Var)
    iterable: Expr = field(default_factory=Expr)
    body: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class Break(Stmt):
    pass


@dataclass(frozen=True)
class Continue(Stmt):
    pass


@dataclass(frozen=True)
class Fallthrough(Stmt):
    """Continue execution with the next selected generic ``Switch`` arm.

    This is only valid as the final direct statement of a switch arm.  It is
    intentionally explicit so a renderer and simulator do not infer a
    language-specific default case policy.
    """

    pass


@dataclass(frozen=True)
class ForRange(Stmt):
    target: Var = field(default_factory=Var)
    start: Expr = field(default_factory=Expr)
    stop: Expr = field(default_factory=Expr)
    step: Expr = field(default_factory=Expr)
    body: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class Terminator:
    source: SourceRef | None = None


@dataclass(frozen=True)
class Return(Terminator):
    values: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class Jump(Terminator):
    target: BlockId = ""


@dataclass(frozen=True)
class Branch(Terminator):
    condition: Expr = field(default_factory=Expr)
    true_target: BlockId = ""
    false_target: BlockId = ""


@dataclass(frozen=True)
class MultiBranch(Terminator):
    selector: Expr = field(default_factory=Expr)
    cases: tuple[tuple[Expr, BlockId], ...] = ()
    default_target: BlockId = ""


@dataclass(frozen=True)
class ExceptionalEdge:
    """A VM-neutral exceptional CFG edge with instruction provenance."""

    target: BlockId
    source: SourceRef | None = None


@dataclass(frozen=True)
class ExceptionalTransfer:
    """One concrete exceptional transfer from a basic block.

    A block can contain more than one potentially throwing operation.  The
    transfer therefore belongs to the operation provenance, rather than to a
    synthetic block-wide catch edge.  ``ordinal`` is the deterministic order
    among a source block's exceptional transfers; CFG edge ordinals are still
    assigned from the complete outgoing-edge sequence by ``build_cfg``.

    ``stack_snapshot`` and ``handler_chain`` are generic handler-entry facts.
    They are data only: recovery and execution own their interpretation.
    """

    target: BlockId
    source: SourceRef | None = None
    ordinal: int = 0
    stack_snapshot: tuple[Expr, ...] = ()
    handler_chain: tuple[str, ...] = ()


@dataclass(frozen=True)
class BytecodeControlFlow:
    """Typed bytecode control-flow facts retained for read-only analysis."""

    source: SourceRef
    flow: Literal["conditional", "unconditional", "multiway"] | None = None
    targets: tuple[int, ...] = ()


@dataclass(frozen=True)
class BasicBlock:
    id: BlockId
    statements: tuple[Stmt, ...] = ()
    terminator: Terminator | None = None
    # Compatibility input for pre-transfer callers.  New core construction
    # must populate ``exception_transfers`` instead.
    exception_edge: ExceptionalEdge | None = None
    # New core representation.  It preserves every concrete exceptional
    # transfer, including parallel transfers to the same handler.
    exception_transfers: tuple[ExceptionalTransfer, ...] = ()
    # VM-neutral identities for nested handlers in which bare re-raise is valid.
    active_exception_handlers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.exception_transfers and self.exception_edge is not None:
            first = self.exception_transfers[0]
            if self.exception_edge != ExceptionalEdge(target=first.target, source=first.source):
                raise ValueError("a basic block cannot declare conflicting exception representations")
        object.__setattr__(self, "exception_transfers", tuple(self.exception_transfers))
        object.__setattr__(self, "active_exception_handlers", tuple(self.active_exception_handlers))
        # ``exception_edge`` remains a compatibility read view.  It is never
        # used to build the CFG when concrete transfers are present.
        if self.exception_transfers:
            first = self.exception_transfers[0]
            object.__setattr__(
                self,
                "exception_edge",
                ExceptionalEdge(target=first.target, source=first.source),
            )


def exceptional_transfers(block: BasicBlock) -> tuple[ExceptionalTransfer, ...]:
    """Return concrete transfers, adapting the legacy single-edge field.

    This is the sole compatibility boundary for old IR producers.  Consumers
    must use it instead of silently assuming a block has at most one handler.
    """

    if block.exception_transfers:
        return block.exception_transfers
    if block.exception_edge is None:
        return ()
    return (
        ExceptionalTransfer(
            target=block.exception_edge.target,
            source=block.exception_edge.source,
            handler_chain=block.active_exception_handlers,
        ),
    )


@dataclass(frozen=True)
class FunctionIR:
    name: str
    params: tuple[str, ...] = ()
    blocks: tuple[BasicBlock, ...] = ()
    nested_functions: tuple["FunctionIR", ...] = ()
    source: SourceRef | None = None
    recovery_kind: str | None = None
    control_provenance: tuple[SourceRef, ...] = ()
    bytecode_control_flow: tuple[BytecodeControlFlow, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModuleIR:
    name: str
    functions: tuple[FunctionIR, ...] = ()
    source_language: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)
