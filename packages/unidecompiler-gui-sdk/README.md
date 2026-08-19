# unidecompiler-gui-sdk

Stable, read-only API for trusted `unidecompiler-gui` Python plugins. The SDK
does not depend on Qt, core internals, frontends, or the simulator implementation.

Install it directly when developing a plugin:

```sh
python -m pip install unidecompiler-gui-sdk
```

End users do not need to install it separately: `unidecompiler-gui` depends on
the matching SDK API.

## Minimal Plugin

Place `plugin.toml` and the declared entry module in one directory:

```toml
[plugin]
id = "example.workspace-inspector"
name = "Workspace Inspector"
version = "1.0.0"
api = "1"
entry = "workspace_inspector:register"

[python]
requires = []
```

```python
from unidecompiler_gui_sdk import Command, Panel, PanelState


def register(context):
    context.panels.register(Panel("summary", "Summary"))

    def refresh(plugin_context):
        document = plugin_context.active_document
        text = "No active document" if document is None else document.display_name
        plugin_context.set_panel_state("summary", PanelState.text_view(text))

    context.commands.register(Command("refresh", "Refresh summary", refresh))
    context.subscribe("document_selected", lambda _document: refresh(context))
```

The plugin entry function receives a plugin-scoped context. Plugins are trusted
in-process code, are not sandboxed, and must not import Qt or private
decompiler/simulator modules. See the repository's
`docs/GUI_PLUGIN_DEVELOPMENT.md` for the complete contract.

The GUI calls the manifest entry function with a plugin-scoped `PluginContext`.
Context snapshots are frozen data and navigation/simulation are host requests.
Register extensions through `context.commands.register(Command(...))` and
`context.panels.register(Panel(...))`; panels use `PanelState` data rather than
Qt widgets. The SDK exposes no generic IR, decoded artifacts, frontends, Qt
objects, or simulator execution internals.

For simulation, request frontend-owned targets with
`request_simulation_targets(document_id)`. The asynchronous target job returns
`SimulationTargetSnapshot` values containing a display label, parameter names,
and an opaque query. Pass that query unchanged to `submit_simulation`. Finished
jobs contain SDK-owned `SimulationResultSnapshot` and
`SimulationEventSnapshot` data, never simulator implementation objects.

`context.settings` stores JSON-compatible values under the current plugin ID;
one plugin cannot address another plugin's settings namespace.
