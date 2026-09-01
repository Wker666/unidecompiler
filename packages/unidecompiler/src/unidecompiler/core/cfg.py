from __future__ import annotations

from dataclasses import dataclass

from unidecompiler.core.ir import BasicBlock, Branch, FunctionIR, Jump, MultiBranch, Raise, Reraise, Return, SourceRef


@dataclass(frozen=True)
class CFGEdge:
    source: str
    target: str
    kind: str
    provenance: SourceRef | None = None


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


@dataclass(frozen=True)
class NaturalLoop:
    header: str
    backedge_source: str
    blocks: frozenset[str]
    exits: tuple[CFGEdge, ...]


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
    dominators = compute_dominators(cfg)
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

    postdominators = compute_postdominators(cfg)
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
    idoms = compute_immediate_dominators(cfg)
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
    dominators = compute_dominators(cfg)
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
    edges.append(CFGEdge(source=source, target=target, kind=kind, provenance=provenance))


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
