"""Immutable, VM-neutral views used by CFG structuring passes.

The region graph is deliberately separate from :class:`FunctionIR`.  A
structuring pass may collapse nodes in a derived view while the original
FunctionIR remains available as the exact CFG fallback.  This module contains
only graph facts; it does not construct AST nodes or make language decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from unidecompiler.core.cfg import CFG, build_cfg, find_natural_loops
from unidecompiler.core.ir import FunctionIR, SourceRef


RegionEdgeRole = Literal[
    "ordinary",
    "back",
    "loop-exit",
    "exceptional",
    "irreducible",
]


@dataclass(frozen=True)
class RegionEdge:
    """An edge in a region graph, retaining its original CFG kind."""

    source: str
    target: str
    kind: str
    provenance: SourceRef | None = None
    roles: frozenset[RegionEdgeRole] = frozenset({"ordinary"})


@dataclass(frozen=True)
class RegionNode:
    """A node and the original blocks represented by it."""

    id: str
    members: frozenset[str]
    kind: str = "basic"
    internal_edges: tuple[RegionEdge, ...] = ()


@dataclass(frozen=True)
class RegionGraph:
    """Immutable structured view over a function's control-flow graph.

    ``nodes`` and ``edges`` are tuples to make snapshots deterministic and
    safe to retain across a candidate rewrite.  A region node always keeps
    the set of original block ids it represents, so later validation can
    compare a structured view with the preservation CFG.
    """

    entry: str | None
    nodes: tuple[RegionNode, ...]
    edges: tuple[RegionEdge, ...]
    diagnostics: tuple[str, ...] = ()

    @classmethod
    def from_cfg(cls, cfg: CFG) -> "RegionGraph":
        natural_loops = find_natural_loops(cfg)
        back_edges = {
            (loop.backedge_source, loop.header)
            for loop in natural_loops
        }
        loop_exit_edges = {
            (edge.source, edge.target)
            for loop in natural_loops
            for edge in loop.exits
        }
        irreducible_blocks = _irreducible_blocks(cfg)

        def roles(source: str, target: str, kind: str) -> frozenset[RegionEdgeRole]:
            result: set[RegionEdgeRole] = set()
            if kind == "exception":
                result.add("exceptional")
            if (source, target) in back_edges:
                result.add("back")
            if (source, target) in loop_exit_edges:
                result.add("loop-exit")
            if source in irreducible_blocks or target in irreducible_blocks:
                result.add("irreducible")
            if not result:
                result.add("ordinary")
            return frozenset(result)

        nodes = tuple(
            RegionNode(id=block_id, members=frozenset({block_id}))
            for block_id in cfg.blocks
        )
        edges = tuple(
            RegionEdge(
                source=edge.source,
                target=edge.target,
                kind=edge.kind,
                provenance=edge.provenance,
                roles=roles(edge.source, edge.target, edge.kind),
            )
            for edge in cfg.edges
        )
        return cls(
            entry=cfg.entry,
            nodes=nodes,
            edges=edges,
            diagnostics=cfg.diagnostics,
        )

    @classmethod
    def from_function(cls, function: FunctionIR) -> "RegionGraph":
        return cls.from_cfg(build_cfg(function))

    def node(self, node_id: str) -> RegionNode | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    def successors(self, node_id: str) -> tuple[str, ...]:
        return tuple(edge.target for edge in self.edges if edge.source == node_id)

    def predecessors(self, node_id: str) -> tuple[str, ...]:
        return tuple(edge.source for edge in self.edges if edge.target == node_id)

    def is_sese_body(
        self,
        members: frozenset[str],
        *,
        entry: str,
        exit: str,
    ) -> bool:
        """Check a conservative single-entry/single-exit body boundary.

        ``members`` excludes the exit node.  Every member must be reachable
        from ``entry`` inside the body, and all outgoing edges must remain in
        the body or target ``exit``.  Incoming edges from outside are allowed
        only at ``entry``.  This is a boundary proof, not a complete semantic
        equivalence proof; callers still own Phi and side-effect checks.
        """

        if self.diagnostics or entry not in members or exit in members:
            return False
        if any(self.node(member) is None for member in members):
            return False

        for edge in self.edges:
            if (
                edge.source in members or edge.target in members
            ) and edge.roles & {"back", "loop-exit", "exceptional", "irreducible"}:
                return False
            if edge.target in members and edge.source not in members and edge.target != entry:
                return False
            if edge.source in members and edge.target not in members and edge.target != exit:
                return False

        reachable = {entry}
        pending = [entry]
        while pending:
            current = pending.pop()
            for target in self.successors(current):
                if target == exit or target not in members or target in reachable:
                    continue
                reachable.add(target)
                pending.append(target)
        if reachable != set(members):
            return False

        # Every member must be able to reach the designated exit without
        # leaving the region.  This rejects dangling side branches that would
        # otherwise look like a valid local tree.
        can_reach_exit = {exit}
        changed = True
        while changed:
            changed = False
            for member in members - can_reach_exit:
                if any(
                    target in can_reach_exit
                    for target in self.successors(member)
                    if target in members or target == exit
                ):
                    can_reach_exit.add(member)
                    changed = True
        return members <= can_reach_exit

    def is_single_entry_loop(
        self,
        members: frozenset[str],
        *,
        header: str,
        preheader: str,
        exit: str,
    ) -> bool:
        """Check a conservative natural-loop boundary.

        A loop differs from an acyclic SESE body because it contains one or
        more back edges to its header.  The check therefore permits ordinary
        internal back edges, while still rejecting exceptional and
        irreducible edges.  It proves only the graph boundary; callers must
        still validate loop phi values and statement ordering before folding.
        """

        if (
            self.diagnostics
            or not members
            or header not in members
            or exit in members
            or preheader in members
        ):
            return False
        if any(self.node(member) is None for member in members):
            return False

        incoming = [
            edge
            for edge in self.edges
            if edge.target in members and edge.source not in members
        ]
        if any(edge.target != header for edge in incoming):
            return False
        if {edge.source for edge in incoming} != {preheader}:
            return False

        outgoing = [
            edge
            for edge in self.edges
            if edge.source in members and edge.target not in members
        ]
        if not outgoing or any(edge.target != exit for edge in outgoing):
            return False
        if any(
            edge.roles & {"exceptional", "irreducible"}
            for edge in self.edges
            if edge.source in members or edge.target in members
        ):
            return False

        # All members must be reachable from the header without leaving the
        # loop.  This prevents accidentally collapsing a disconnected block
        # that happens to share the same external boundary.
        reachable = {header}
        pending = [header]
        while pending:
            current = pending.pop()
            for target in self.successors(current):
                if target not in members or target in reachable:
                    continue
                reachable.add(target)
                pending.append(target)
        if reachable != set(members):
            return False

        # Every member must be able to make progress to a backedge/header or
        # to the designated exit.  This rejects an internal SCC that is
        # reachable from the header but can never complete an iteration.
        can_progress = {header}
        changed = True
        while changed:
            changed = False
            for member in members - can_progress:
                if any(
                    target == exit or target in can_progress
                    for target in self.successors(member)
                ):
                    can_progress.add(member)
                    changed = True
        return can_progress == set(members)

    def collapse(
        self,
        *,
        region_id: str,
        members: frozenset[str],
        kind: str,
    ) -> "RegionGraph" | None:
        """Return a derived graph with ``members`` replaced by one node.

        The method is intentionally conservative: the caller must provide a
        previously validated member set, and the set must contain complete
        region nodes.  Original block membership is retained on the collapsed
        node; no edge is silently discarded.
        """

        if not members or any(self.node(member) is None for member in members):
            return None
        if self.node(region_id) is not None:
            return None

        member_nodes = tuple(
            node for node in self.nodes if node.id in members
        )
        collapsed_members = frozenset(
            original
            for node in member_nodes
            for original in node.members
        )
        collapsed_internal_edges = tuple(
            edge
            for node in member_nodes
            for edge in node.internal_edges
        ) + tuple(
            edge
            for edge in self.edges
            if edge.source in members and edge.target in members
        )
        nodes = tuple(node for node in self.nodes if node.id not in members) + (
            RegionNode(
                id=region_id,
                members=collapsed_members,
                kind=kind,
                internal_edges=collapsed_internal_edges,
            ),
        )

        def endpoint(node_id: str) -> str:
            return region_id if node_id in members else node_id

        rewritten_edges: list[RegionEdge] = []
        for edge in self.edges:
            source = endpoint(edge.source)
            target = endpoint(edge.target)
            if source == target:
                continue
            rewritten_edges.append(
                RegionEdge(
                    source=source,
                    target=target,
                    kind=edge.kind,
                    provenance=edge.provenance,
                    roles=edge.roles,
                )
            )
        return RegionGraph(
            entry=endpoint(self.entry) if self.entry is not None else None,
            nodes=nodes,
            edges=tuple(rewritten_edges),
            diagnostics=self.diagnostics,
        )


def _irreducible_blocks(cfg: CFG) -> frozenset[str]:
    """Return blocks in strongly connected components with multiple entries."""

    irreducible: set[str] = set()
    for component in _strongly_connected_components(cfg):
        if len(component) == 1:
            only = next(iter(component))
            if only not in cfg.successors(only):
                continue
        entry_targets = {
            edge.target
            for edge in cfg.edges
            if edge.target in component and edge.source not in component
        }
        if cfg.entry in component:
            entry_targets.add(cfg.entry)
        if len(entry_targets) > 1:
            irreducible.update(component)
    return frozenset(irreducible)


def _strongly_connected_components(cfg: CFG) -> tuple[frozenset[str], ...]:
    """Compute deterministic SCCs with Tarjan's algorithm."""

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

        for successor in sorted(set(cfg.successors(node))):
            if successor not in indexes:
                visit(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[successor])

        if lowlinks[node] != indexes[node]:
            return
        component: set[str] = set()
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node:
                break
        components.append(frozenset(component))

    for block_id in sorted(cfg.blocks):
        if block_id not in indexes:
            visit(block_id)
    return tuple(components)
