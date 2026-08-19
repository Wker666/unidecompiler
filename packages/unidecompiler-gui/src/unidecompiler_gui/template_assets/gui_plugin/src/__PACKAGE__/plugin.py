from unidecompiler_gui_sdk import Command, Panel, PanelState


def register(context) -> None:
    context.panels.register(Panel("summary", "Summary"))

    def refresh(plugin_context) -> None:
        document = plugin_context.active_document
        text = "No active document" if document is None else document.display_name
        plugin_context.set_panel_state("summary", PanelState.text_view(text))

    context.commands.register(Command("refresh", "Refresh", refresh))
    context.subscribe("document_selected", lambda _document: refresh(context))
