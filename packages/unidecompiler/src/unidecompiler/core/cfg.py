from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from unidecompiler.core.ir import BasicBlock, Branch, FunctionIR, Jump, MultiBranch, Raise, Reraise, Return, SourceRef


@dataclass(frozen=True)
class CFGEdge:
    source: str
    target: str
    kind: str
    provenance: SourceRef | None = None
    # ``ordinal`` distinguishes parallel edges that have the same source,
    # target, and kind.  It is assigned deterministically by ``build_cfg``;
    # callers constructing an edge directly retain the backwards-compatible
    # default of zero.
    ordinal: int = 0

    @property
    def edge_id(self) -> str:
        """Return a stable identity for this concrete CFG edge."""

        return f"{self.source}:{self.target}:{self.kind}:{self.ordinal}"

    @property
    def id(self) -> str:
        """Alias used by generic graph consumers that expect an edge ID."""

        return self.edge_id


@dataclass(frozen=True)
class CFG:
    entry: str | None
    blocks: dict[str, BasicBlock]
    edges: tuple[CFGEdge, ...]
    diagnostics: tuple[str, ...] = ()

    def successors(self, block_id: str) -> tuple[str, ...]:
        return tuple(edge.target for edge in self.edges if edge.source == block_id)

    def predecessors(self, block_id: str) -> tuple[str, ...]:
        return tuple(edge.source for edge in self.edges if edge.target == block_id)

    def incoming_edges(self, block_id: str) -> tuple[CFGEdge, ...]:
        """Return concrete incoming edges, preserving parallel-edge identity."""

        return tuple(edge for edge in self.edges if edge.target == block_id)

    def outgoing_edges(self, block_id: str) -> tuple[CFGEdge, ...]:
        """Return concrete outgoing edges, preserving their stable order."""

        return tuple(edge for edge in self.edges if edge.source == block_id)

    def analyze(self) -> "CFGAnalysis":
        """Return a lazily computed, immutable analysis snapshot.

        The snapshot is tied to this immutable CFG.  Rewriters must build a
        new CFG after accepting a candidate rather than reusing facts from an
        older graph.
        """

        return CFGAnalysis.from_cfg(self)


@dataclass(frozen=True)
class CFGAnalysis:
    """Shared lazy analysis facts for one CFG snapshot.

    Keeping this interface small gives structuring passes one seam for graph
    facts while preserving the existing standalone analysis functions for
    compatibility.
    """

    cfg: CFG

    @classmethod
    def from_cfg(cls, cfg: CFG) -> "CFGAnalysis":
        return cls(cfg)

    @cached_property
    def dominators(self) -> dict[str, frozenset[str]]:
        return compute_dominators(self.cfg)

    @cached_property
    def immediate_dominators(self) -> dict[str, str | None]:
        return _immediate_dominators_from(self.cfg, self.dominators)

    @cached_property
    def postdominators(self) -> dict[str, frozenset[str]]:
        return compute_postdominators(self.cfg)

    @cached_property
    def immediate_postdominators(self) -> dict[str, str | None]:
        return _immediate_postdominators_from(self.cfg, self.postdominators)

    @cached_property
    def dominance_frontier(self) -> dict[str, frozenset[str]]:
        return _dominance_frontier_from(self.cfg, self.immediate_dominators)

    @cached_property
    def natural_loops(self) -> tuple[NaturalLoop, ...]:
        return _natural_loops_from(self.cfg, self.dominators)

    @cached_property
    def loop_infos(self) -> tuple["LoopInfo", ...]:
        return _loop_infos_from(self.cfg, self.natural_loops)

    @cached_property
    def irreducible_edges(self) -> tuple[CFGEdge, ...]:
        return find_irreducible_edges(self.cfg)

    @cached_property
    def irreducible_blocks(self) -> frozenset[str]:
        return find_irreducible_blocks(self.cfg)


@dataclass(frozen=True)
class NaturalLoop:
    header: str
    backedge_source: str
    blocks: frozenset[str]
    exits: tuple[CFGEdge, ...]


@dataclass(frozen=True)
class LoopInfo:
    """Natural-loop facts grouped by header.

    ``NaturalLoop`` remains the compatibility view for callers that need one
    backedge at a time.  This view combines all backedges sharing a header and
    records nesting without making a structural claim about the loop body.
    """

    header: str
    backedge_sources: tuple[str, ...]
    blocks: frozenset[str]
    exits: tuple[CFGEdge, ...]
    parent_header: str | None = None
    depth: int = 0


def build_cfg(function: FunctionIR) -> CFG:
    blocks = {block.id: block for block in function.blocks}
    entry = function.blocks[0].id if function.blocks else None
    edges: list[CFGEdge] = []
    diagnostics: list[str] = []

    for index, block in enumerate(function.blocks):
        if block.exception_edge is not None:
            _add_edge(
                edges,
                diagnostics,
                blocks,
                block.id,
                block.exception_edge.target,
                "exception",
                block.exception_edge.source,
            )
        terminator = block.terminator
        if isinstance(terminator, Branch):
            _add_edge(edges, diagnostics, blocks, block.id, terminator.true_target, "true")
            _add_edge(edges, diagnostics, blocks, block.id, terminator.false_target, "false")
        elif isinstance(terminator, MultiBranch):
            for value, target in terminator.cases:
                _add_edge(edges, diagnostics, blocks, block.id, target, f"case:{_edge_value(value)}")
            _add_edge(edges, diagnostics, blocks, block.id, terminator.default_target, "default")
        elif isinstance(terminator, Jump):
            _add_edge(edges, diagnostics, blocks, block.id, terminator.target, "jump")
        elif isinstance(terminator, Return):
            continue
        elif (
            terminator is None
            and not (block.statements and isinstance(block.statements[-1], (Raise, Reraise)))
            and index + 1 < len(function.blocks)
        ):
            _add_edge(
                edges,
                diagnostics,
                blocks,
                block.id,
                function.blocks[index + 1].id,
                "fallthrough",
            )

    return CFG(
        entry=entry,
        blocks=blocks,
        edges=tuple(edges),
        diagnostics=tuple(diagnostics),
    )


def compute_dominators(cfg: CFG) -> dict[str, frozenset[str]]:
    if cfg.entry is None:
        return {}

    blocks = set(cfg.blocks)
    preds = _predecessors(cfg)
    dominators: dict[str, set[str]] = {
        block: ({block} if block == cfg.entry else set(blocks)) for block in blocks
    }

    changed = True
    while changed:
        changed = False
        for block in blocks:
            if block == cfg.entry:
                continue
            predecessors = preds.get(block, set())
            if not predecessors:
                new_doms = {block}
            else:
                intersection = set(blocks)
                for pred in predecessors:
                    intersection &= dominators[pred]
                new_doms = {block} | intersection
            if new_doms != dominators[block]:
                dominators[block] = new_doms
                changed = True

    return {block: frozenset(dom_set) for block, dom_set in dominators.items()}


def compute_immediate_dominators(cfg: CFG) -> dict[str, str | None]:
    return _immediate_dominators_from(cfg, compute_dominators(cfg))


def _immediate_dominators_from(
    cfg: CFG,
    dominators: dict[str, frozenset[str]],
) -> dict[str, str | None]:
    idoms: dict[str, str | None] = {}

    for block, doms in dominators.items():
        if block == cfg.entry:
            idoms[block] = None
            continue
        strict_doms = doms - {block}
        candidate = None
        for dom in strict_doms:
            if all(dom == other or dom not in dominators[other] for other in strict_doms):
                candidate = dom
                break
        idoms[block] = candidate

    return idoms


def compute_postdominators(cfg: CFG) -> dict[str, frozenset[str]]:
    """Return postdominators for blocks that can reach a terminal exit.

    Multiple terminal blocks are treated as independent exits.  A block that
    cannot reach any exit (for example, an infinite loop) has no proven
    postdominator set and is omitted rather than being assigned a guessed
    relation.
    """

    successors = {block: set(cfg.successors(block)) for block in cfg.blocks}
    exits = {block for block, targets in successors.items() if not targets}
    if not exits:
        return {}

    # Use a least fixed point: a block is eligible only when every successor
    # is already proven to reach a terminal exit.  This deliberately excludes
    # loops and branches into non-terminating components; treating only the
    # terminating successor as relevant would manufacture a postdominator that
    # is not taken on every execution path.
    can_reach_exit = set(exits)
    changed = True
    while changed:
        changed = False
        for block, targets in sorted(successors.items()):
            if block in can_reach_exit or not targets or not targets <= can_reach_exit:
                continue
            can_reach_exit.add(block)
            changed = True

    postdominators: dict[str, set[str]] = {
        block: ({block} if block in exits else set(can_reach_exit))
        for block in can_reach_exit
    }
    changed = True
    while changed:
        changed = False
        for block in sorted(can_reach_exit - exits):
            reachable_successors = successors[block] & can_reach_exit
            if not reachable_successors:
                continue
            intersection = set(can_reach_exit)
            for successor in sorted(reachable_successors):
                intersection &= postdominators[successor]
            new_postdominators = {block} | intersection
            if new_postdominators != postdominators[block]:
                postdominators[block] = new_postdominators
                changed = True

    return {
        block: frozenset(postdoms)
        for block, postdoms in postdominators.items()
    }


def compute_immediate_postdominators(cfg: CFG) -> dict[str, str | None]:
    """Return the nearest strict postdominator for each proven block."""

    return _immediate_postdominators_from(cfg, compute_postdominators(cfg))


def _immediate_postdominators_from(
    cfg: CFG,
    postdominators: dict[str, frozenset[str]],
) -> dict[str, str | None]:
    immediate: dict[str, str | None] = {}
    for block, postdoms in postdominators.items():
        strict = postdoms - {block}
        immediate[block] = next(
            (
                candidate
                for candidate in sorted(strict)
                if all(
                    candidate == other
                    or candidate not in postdominators.get(other, frozenset())
                    for other in sorted(strict)
                )
            ),
            None,
        )
    return immediate


def compute_dominance_frontier(cfg: CFG) -> dict[str, frozenset[str]]:
    return _dominance_frontier_from(cfg, compute_immediate_dominators(cfg))


def _dominance_frontier_from(
    cfg: CFG,
    idoms: dict[str, str | None],
) -> dict[str, frozenset[str]]:
    preds = _predecessors(cfg)
    frontier: dict[str, set[str]] = {block: set() for block in cfg.blocks}

    for block, predecessors in preds.items():
        if len(predecessors) < 2:
            continue
        for pred in predecessors:
            runner = pred
            stop = idoms.get(block)
            while runner is not None and runner != stop:
                frontier.setdefault(runner, set()).add(block)
                runner = idoms.get(runner)

    return {block: frozenset(targets) for block, targets in frontier.items()}


def find_natural_loops(cfg: CFG) -> tuple[NaturalLoop, ...]:
    return _natural_loops_from(cfg, compute_dominators(cfg))


def _natural_loops_from(
    cfg: CFG,
    dominators: dict[str, frozenset[str]],
) -> tuple[NaturalLoop, ...]:
    loops: list[NaturalLoop] = []

    for edge in cfg.edges:
        if edge.target not in dominators.get(edge.source, frozenset()):
            continue
        blocks = _natural_loop_blocks(cfg, header=edge.target, tail=edge.source)
        exits = tuple(
            candidate
            for candidate in cfg.edges
            if candidate.source in blocks and candidate.target not in blocks
        )
        loops.append(
            NaturalLoop(
                header=edge.target,
                backedge_source=edge.source,
                blocks=frozenset(blocks),
                exits=exits,
            )
        )

    return tuple(sorted(loops, key=lambda loop: (len(loop.blocks), loop.header, loop.backedge_source)))


def find_loop_infos(cfg: CFG) -> tuple[LoopInfo, ...]:
    """Group natural loops by header while retaining all concrete backedges."""

    return _loop_infos_from(cfg, find_natural_loops(cfg))


def _loop_infos_from(
    cfg: CFG,
    natural: tuple[NaturalLoop, ...],
) -> tuple[LoopInfo, ...]:
    grouped: dict[str, list[NaturalLoop]] = {}
    for loop in natural:
        grouped.setdefault(loop.header, []).append(loop)

    merged: list[LoopInfo] = []
    for header, loops in grouped.items():
        blocks = frozenset().union(*(loop.blocks for loop in loops))
        exits = tuple(
            dict.fromkeys(
                edge
                for edge in cfg.edges
                if edge.source in blocks and edge.target not in blocks
            )
        )
        merged.append(
            LoopInfo(
                header=header,
                backedge_sources=tuple(sorted({loop.backedge_source for loop in loops})),
                blocks=blocks,
                exits=exits,
            )
        )

    # The smallest strict enclosing loop is the parent.  Process outer loops
    # first so the derived depth is deterministic.
    by_size = sorted(merged, key=lambda loop: (len(loop.blocks), loop.header))
    parent_by_header: dict[str, LoopInfo | None] = {}
    for loop in by_size:
        parents = [candidate for candidate in by_size if loop.blocks < candidate.blocks]
        parent_by_header[loop.header] = min(
            parents,
            key=lambda candidate: (len(candidate.blocks), candidate.header),
            default=None,
        )

    depth_cache: dict[str, int] = {}

    def depth(loop: LoopInfo) -> int:
        cached = depth_cache.get(loop.header)
        if cached is not None:
            return cached
        parent = parent_by_header[loop.header]
        value = 0 if parent is None else depth(parent) + 1
        depth_cache[loop.header] = value
        return value

    enriched: list[LoopInfo] = []
    for loop in by_size:
        parent = parent_by_header[loop.header]
        enriched.append(
            LoopInfo(
                header=loop.header,
                backedge_sources=loop.backedge_sources,
                blocks=loop.blocks,
                exits=loop.exits,
                parent_header=parent.header if parent is not None else None,
                depth=depth(loop),
            )
        )
    return tuple(sorted(enriched, key=lambda loop: (loop.depth, len(loop.blocks), loop.header)))


def find_irreducible_edges(cfg: CFG) -> tuple[CFGEdge, ...]:
    """Return edges entering multi-entry cyclic components.

    This is an edge-level companion to the legacy block-level irreducibility
    predicate.  Callers can reject or preserve only the ambiguous entry edges
    instead of treating every edge in the component as equally problematic.
    """

    result: list[CFGEdge] = []
    for component in _irreducible_components(cfg):
        incoming = tuple(
            edge
            for edge in cfg.edges
            if edge.target in component and edge.source not in component
        )
        result.extend(incoming)
    return tuple(sorted(result, key=lambda edge: edge.edge_id))


def find_irreducible_blocks(cfg: CFG) -> frozenset[str]:
    """Return blocks belonging to multi-entry cyclic components."""

    return frozenset().union(*_irreducible_components(cfg))


def _irreducible_components(cfg: CFG) -> tuple[frozenset[str], ...]:
    components: list[frozenset[str]] = []
    for component in _strongly_connected_components(cfg):
        if len(component) == 1:
            only = next(iter(component))
            if only not in cfg.successors(only):
                continue
        incoming = tuple(
            edge
            for edge in cfg.edges
            if edge.target in component and edge.source not in component
        )
        entry_targets = {edge.target for edge in incoming}
        if cfg.entry in component:
            entry_targets.add(cfg.entry)
        if len(entry_targets) > 1:
            components.append(component)
    return tuple(sorted(components, key=lambda component: tuple(sorted(component))))


def _add_edge(
    edges: list[CFGEdge],
    diagnostics: list[str],
    blocks: dict[str, BasicBlock],
    source: str,
    target: str,
    kind: str,
    provenance: SourceRef | None = None,
) -> None:
    if target not in blocks:
        diagnostics.append(f"edge from {source} points to missing block {target}")
        return
    ordinal = sum(1 for edge in edges if edge.source == source)
    edges.append(
        CFGEdge(
            source=source,
            target=target,
            kind=kind,
            provenance=provenance,
            ordinal=ordinal,
        )
    )


def _edge_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _predecessors(cfg: CFG) -> dict[str, set[str]]:
    preds: dict[str, set[str]] = {block: set() for block in cfg.blocks}
    for edge in cfg.edges:
        preds.setdefault(edge.target, set()).add(edge.source)
    return preds


def _natural_loop_blocks(cfg: CFG, header: str, tail: str) -> set[str]:
    preds = _predecessors(cfg)
    loop_blocks = {header, tail}
    stack = [tail]

    while stack:
        block = stack.pop()
        for pred in preds.get(block, set()):
            if pred in loop_blocks:
                continue
            loop_blocks.add(pred)
            stack.append(pred)

    return loop_blocks


def _strongly_connected_components(cfg: CFG) -> tuple[frozenset[str], ...]:
    """Compute deterministic SCCs for edge-level irreducibility facts."""

    next_index = 0
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[frozenset[str]] = []

    def visit(node: str) -> None:
        nonlocal next_index
        indexes[node] = next_index
        lowlinks[node] = next_index
        next_index += 1
        stack.append(node)
        on_stack.add(node)

        for target in sorted(set(cfg.successors(node))):
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])

        if lowlinks[node] != indexes[node]:
            return
        component: set[str] = set()
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node:
                break
        components.append(frozenset(component))

    for node in sorted(cfg.blocks):
        if node not in indexes:
            visit(node)
    return tuple(sorted(components, key=lambda component: tuple(sorted(component))))
