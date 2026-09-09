"""Fail-closed validation for generic value and Phi invariants."""

from __future__ import annotations

from dataclasses import fields, is_dataclass

from unidecompiler.core.cfg import build_cfg
from unidecompiler.core.ir import Assign, CapturedVar, Expr, FunctionIR, GetAttr, GetItem, Global, IndirectRef, Phi, Var


def validate_value_invariants(function: FunctionIR) -> tuple[str, ...]:
    """Return deterministic diagnostics for malformed generic value flow.

    The validator is intentionally conservative.  It enforces invariants that
    are representation-independent (duplicate Phi labels and empty value
    identities) and only checks complete predecessor coverage when concrete
    edge identity is unambiguous.
    """

    diagnostics: list[str] = []
    cfg = build_cfg(function)
    for block in function.blocks:
        predecessors = set(cfg.predecessors(block.id))
        edge_ambiguous = len(cfg.incoming_edges(block.id)) != len(predecessors)
        for value in _walk_values((*block.statements, block.terminator)):
            if isinstance(value, Var) and not value.name:
                diagnostics.append(f"block {block.id}: variable has empty identity")
            if isinstance(value, IndirectRef) and not _valid_reference_target(value):
                diagnostics.append(f"block {block.id}: indirect reference has invalid target")
            if not isinstance(value, Phi):
                continue
            labels = tuple(label for label, _ in value.incoming)
            if len(labels) != len(set(labels)):
                if not value.edge_ids:
                    diagnostics.append(f"block {block.id}: phi has duplicate predecessor labels")
                    continue
            if value.edge_ids:
                if len(value.edge_ids) != len(value.incoming):
                    diagnostics.append(f"block {block.id}: phi edge identity count does not match incoming values")
                    continue
                if len(set(value.edge_ids)) != len(value.edge_ids):
                    diagnostics.append(f"block {block.id}: phi has duplicate concrete edge identities")
                    continue
                # Only a Phi assigned at the physical block entry is keyed by
                # this block's outer CFG predecessors.  Nested Phis can carry
                # concrete identities from an already-collapsed child region;
                # comparing those identities with the outer block's incoming
                # edges would falsely reject a valid structured value.
                if _is_block_entry_phi(block, value):
                    incoming_edges = {edge.edge_id: edge for edge in cfg.incoming_edges(block.id)}
                    if any(edge_id not in incoming_edges for edge_id in value.edge_ids):
                        diagnostics.append(f"block {block.id}: phi references an incoming edge outside the CFG")
                        continue
                    if set(value.edge_ids) != set(incoming_edges):
                        diagnostics.append(
                            f"block {block.id}: phi concrete edges do not completely cover CFG predecessors"
                        )
                        continue
                    # The logical label remains useful for structured
                    # diagnostics, but a supplied concrete edge must agree
                    # with its source.
                    if any(
                        label not in {edge_id, incoming_edges[edge_id].source}
                        for label, edge_id in zip(labels, value.edge_ids, strict=True)
                    ):
                        diagnostics.append(f"block {block.id}: phi label does not match concrete edge source")
                        continue
            # Only a Phi assignment at block entry is directly keyed by this
            # block's CFG predecessors.  Nested structured regions have their
            # own logical predecessor labels and are checked for duplicates,
            # but must not be compared with the outer low-level block.
            if not _is_block_entry_phi(block, value):
                continue
            if edge_ambiguous and not value.edge_ids:
                # A block-keyed Phi cannot prove which parallel edge supplied
                # an input.  This is a preservation-floor condition, not a
                # malformed value; callers should retain low-level CFG form.
                diagnostics.append(f"block {block.id}: phi lacks concrete edge identity")
                continue
            # A structured reducer may retain a logical carrier label (for
            # example ``existing``) alongside concrete edges while it moves a
            # Phi into a nested region.  Only enforce complete predecessor
            # coverage when every label is demonstrably an outer CFG block;
            # mixed labels are validated for duplicates but left to the
            # structurer's local proof.
            concrete_labels = set(labels).issubset(set(cfg.blocks))
            # A single-predecessor block may carry a stack/materialization
            # Phi whose labels describe the earlier merge that produced the
            # value (for example an iterator header flowing through a linear
            # block).  It is not a block-entry merge for this block, so there
            # is no outer predecessor set to compare against.  Require exact
            # coverage only at genuine joins with multiple incoming edges.
            if len(predecessors) > 1 and concrete_labels and set(labels) != predecessors:
                diagnostics.append(
                    f"block {block.id}: phi predecessors do not match concrete CFG predecessors"
                )
    return tuple(dict.fromkeys(diagnostics))


def _is_block_entry_phi(block, phi: Phi) -> bool:
    for statement in block.statements:
        if isinstance(statement, Assign):
            if statement.value is phi:
                return True
        # The first non-Phi statement marks the end of block-entry Phi area.
        if not _contains_phi(statement):
            return False
    return False


def _contains_phi(value: object) -> bool:
    if isinstance(value, Phi):
        return True
    if isinstance(value, (tuple, list)):
        return any(_contains_phi(item) for item in value)
    if is_dataclass(value):
        return any(_contains_phi(getattr(value, field.name)) for field in fields(value))
    return False


def _valid_reference_target(reference: IndirectRef) -> bool:
    target = reference.target
    return isinstance(target, (Var, Global, CapturedVar, GetAttr, GetItem))


def _walk_values(value: object):
    if value is None:
        return
    if isinstance(value, (str, bytes, int, float, bool)):
        return
    if isinstance(value, Expr):
        yield value
    if isinstance(value, (tuple, list)):
        for item in value:
            yield from _walk_values(item)
        return
    if is_dataclass(value):
        for field in fields(value):
            yield from _walk_values(getattr(value, field.name))
