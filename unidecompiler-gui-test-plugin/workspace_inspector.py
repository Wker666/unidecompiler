"""A GUI SDK example with no Qt, frontend, core, or simulator imports."""
from unidecompiler_gui_sdk import Command, Panel, PanelState


def register(context):
    context.panels.register(Panel("workspace", "Workspace Inspector"))

    def refresh(active_context):
        document = active_context.active_document
        if document is None:
            active_context.set_panel_state(
                "workspace", PanelState.text_view("No active document")
            )
            return
        selected = active_context.selection.function_id
        references = active_context.get_references(document.id, selected)
        rows = tuple(
            (function.name, function.status, ", ".join(function.params))
            for function in document.functions
        )
        heading = f"{document.display_name}: {len(references)} references"
        active_context.set_panel_state(
            "workspace",
            PanelState.table((heading, "Status", "Parameters"), rows),
        )

    def discover_targets(active_context):
        document = active_context.active_document
        if document is None:
            return
        active_context.request_simulation_targets(document.id)

    def target_discovery_completed(job):
        if job.stale:
            return
        rows = tuple((target.label, ", ".join(target.params), "ready") for target in job.targets)
        context.set_panel_state(
            "workspace", PanelState.table(("Simulation target", "Parameters", "Status"), rows)
        )

    context.commands.register(Command("refresh", "Refresh workspace inspector", refresh))
    context.commands.register(Command("discover-targets", "Discover simulation targets", discover_targets))
    context.subscribe("document_selected", lambda _document: refresh(context))
    context.subscribe("document_updated", lambda _document: refresh(context))
    context.subscribe("simulation_targets_completed", target_discovery_completed)
