"""Immutable, VM-neutral views used by CFG structuring passes.

The region graph is deliberately separate from :class:`FunctionIR`.  A
structuring pass may collapse nodes in a derived view while the original
FunctionIR remains available as the exact CFG fallback.  This module contains
only graph facts; it does not construct AST nodes or make language decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from unidecompiler.core.cfg import CFG, build_cfg, find_irreducible_blocks, validate_cfg_consistency
from unidecompiler.core.ir import ExceptionalTransfer, FunctionIR, SourceRef, exceptional_transfers


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
    ordinal: int = 0
    # A derived region edge may have different visible endpoints after one or
    # more collapses.  Keep the concrete preservation-CFG identities that it
    # represents so later proofs never have to infer them from rewritten node
    # names or ordinals.
    origin_edge_ids: tuple[str, ...] = ()

    @property
    def edge_id(self) -> str:
        return f"{self.source}:{self.target}:{self.kind}:{self.ordinal}"

    @property
    def id(self) -> str:
        return self.edge_id

    @property
    def concrete_edge_ids(self) -> tuple[str, ...]:
        """Return the preservation-CFG edges represented by this edge."""

        return self.origin_edge_ids or (self.edge_id,)


@dataclass(frozen=True)
class RegionNode:
    """A node and the original blocks represented by it."""

    id: str
    members: frozenset[str]
    kind: str = "basic"
    internal_edges: tuple[RegionEdge, ...] = ()


@dataclass(frozen=True)
class ProtectedRegionView:
    """Immutable proof facts for one exception-isolated block region.

    The view does not authorize a CFG rewrite by itself.  It proves only that
    every member observes the same active handler scope and the same handler
    entry contract, while retaining every concrete exceptional edge.  A
    collapse matcher must still prove normal-edge topology, Phi remapping,
    statement ordering, and simulator dispatch before consuming the region.
    """

    members: tuple[str, ...]
    active_handler_chain: tuple[str, ...] = ()
    handler_target: str | None = None
    handler_stack_snapshot: tuple[object, ...] = ()
    handler_chain: tuple[str, ...] = ()
    transfers: tuple[tuple[str, ExceptionalTransfer], ...] = ()
    edge_ids: tuple[str, ...] = ()
    snapshot_key: tuple[tuple[object, ...], ...] = ()
    rejections: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        return not self.rejections

    @classmethod
    def from_cfg(
        cls,
        cfg: CFG,
        members: frozenset[str],
    ) -> "ProtectedRegionView":
        ordered_members = tuple(block_id for block_id in cfg.block_ids if block_id in members)
        reasons: list[str] = []
        if not members:
            reasons.append("protected region is empty")
        if len(ordered_members) != len(members):
            reasons.append("protected region references a missing block")
        consistency = validate_cfg_consistency(cfg)
        if consistency:
            reasons.append("CFG consistency check failed: " + "; ".join(consistency))

        blocks = tuple(cfg.blocks[block_id] for block_id in ordered_members if block_id in cfg.blocks)
        active_chains = {tuple(block.active_exception_handlers) for block in blocks}
        if len(active_chains) > 1:
            reasons.append("protected blocks have different active handler chains")
        active_chain = next(iter(active_chains), ())

        transfers = tuple(
            (block.id, transfer)
            for block in blocks
            for transfer in exceptional_transfers(block)
        )
        if not transfers:
            reasons.append("protected region has no exceptional transfers")
        if any(transfer.source is None for _block_id, transfer in transfers):
            reasons.append("exceptional transfer lacks source provenance")
        contracts = {
            (
                transfer.target,
                tuple(transfer.stack_snapshot),
                tuple(transfer.handler_chain),
            )
            for _block_id, transfer in transfers
        }
        if len(contracts) > 1:
            reasons.append("protected blocks have different handler-entry contracts")
        contract = next(iter(contracts), (None, (), ()))

        exceptional_edges = tuple(
            edge
            for edge in cfg.edges
            if edge.kind == "exception" and edge.source in members
        )
        if len(exceptional_edges) != len(transfers):
            reasons.append("protected region does not retain every exceptional edge")
        if any(edge.target in members for edge in exceptional_edges):
            reasons.append("protected region contains a nested exceptional transfer")

        analysis = cfg.analyze()
        if any(
            edge.source in members or edge.target in members
            for edge in analysis.irreducible_edges
        ):
            reasons.append("protected region crosses an irreducible edge")

        return cls(
            members=ordered_members,
            active_handler_chain=tuple(active_chain),
            handler_target=contract[0],
            handler_stack_snapshot=tuple(contract[1]),
            handler_chain=tuple(contract[2]),
            transfers=transfers,
            edge_ids=tuple(edge.edge_id for edge in exceptional_edges),
            snapshot_key=cfg.snapshot_key,
            rejections=tuple(dict.fromkeys(reasons)),
        )


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
    irreducible_edge_ids: frozenset[str] = frozenset()

    @classmethod
    def from_cfg(cls, cfg: CFG) -> "RegionGraph":
        # Share one immutable analysis snapshot for all region facts.  The
        # snapshot is discarded whenever a rewrite creates a new CFG, so no
        # pass can accidentally observe stale loop information.
        analysis = cfg.analyze()
        loop_infos = analysis.loop_infos
        # Keep role assignment edge-keyed.  Parallel edges may share source
        # and target but represent different control alternatives; assigning
        # roles by a node pair would silently mark or consume all of them.
        back_edges = {
            edge.edge_id
            for edge in analysis.backedges
        }
        loop_exit_edges = {
            edge.edge_id
            for loop in loop_infos
            for edge in loop.exits
        }
        irreducible_blocks = find_irreducible_blocks(cfg)

        def roles(edge_id: str, source: str, target: str, kind: str) -> frozenset[RegionEdgeRole]:
            result: set[RegionEdgeRole] = set()
            if kind == "exception":
                result.add("exceptional")
            if edge_id in back_edges:
                result.add("back")
            if edge_id in loop_exit_edges:
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
                roles=roles(edge.edge_id, edge.source, edge.target, edge.kind),
                ordinal=edge.ordinal,
                origin_edge_ids=(edge.edge_id,),
            )
            for edge in cfg.edges
        )
        return cls(
            entry=cfg.entry,
            nodes=nodes,
            edges=edges,
            diagnostics=cfg.diagnostics,
            irreducible_edge_ids=frozenset(edge.edge_id for edge in analysis.irreducible_edges),
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

    def is_irreducible_entry_edge(self, edge: RegionEdge) -> bool:
        """Return whether ``edge`` enters a multi-entry cyclic component."""

        return edge.edge_id in self.irreducible_edge_ids

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
        preheader: str | None,
        exit: str,
    ) -> bool:
        """Check a conservative natural-loop boundary.

        A loop differs from an acyclic SESE body because it contains one or
        more back edges to its header.  The check therefore permits ordinary
        internal back edges, while still rejecting exceptional and
        irreducible edges.  ``preheader=None`` is the entry-loop form and
        requires that the loop has no incoming edge from outside its members.
        It proves only the graph boundary; callers must still validate loop
        phi values and statement ordering before folding.
        """

        if (
            self.diagnostics
            or not members
            or header not in members
            or exit in members
            or (preheader is not None and preheader in members)
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
        incoming_sources = {edge.source for edge in incoming}
        if preheader is None:
            if incoming_sources:
                return False
        elif incoming_sources != {preheader}:
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

        pending_edges: list[RegionEdge] = []
        for edge in self.edges:
            source = endpoint(edge.source)
            target = endpoint(edge.target)
            if source == target:
                continue
            pending_edges.append(
                RegionEdge(
                    source=source,
                    target=target,
                    kind=edge.kind,
                    provenance=edge.provenance,
                    roles=edge.roles,
                    # Reassigned below.  A collapse can make edges from
                    # several member nodes outgoing from one region node, so
                    # retaining their old source-local ordinals is ambiguous.
                    ordinal=0,
                    origin_edge_ids=edge.concrete_edge_ids,
                )
            )

        outgoing_ordinals: dict[str, int] = {}
        rewritten_edges: list[RegionEdge] = []
        for edge in pending_edges:
            ordinal = outgoing_ordinals.get(edge.source, 0)
            outgoing_ordinals[edge.source] = ordinal + 1
            rewritten_edges.append(
                RegionEdge(
                    source=edge.source,
                    target=edge.target,
                    kind=edge.kind,
                    provenance=edge.provenance,
                    roles=edge.roles,
                    ordinal=ordinal,
                    origin_edge_ids=edge.concrete_edge_ids,
                )
            )
        return RegionGraph(
            entry=endpoint(self.entry) if self.entry is not None else None,
            nodes=nodes,
            edges=tuple(rewritten_edges),
            diagnostics=self.diagnostics,
            irreducible_edge_ids=frozenset(
                edge.edge_id
                for edge in rewritten_edges
                if "irreducible" in edge.roles
            ),
        )
