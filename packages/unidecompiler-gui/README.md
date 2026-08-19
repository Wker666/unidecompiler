# unidecompiler-gui

`unidecompiler-gui` is a read-only PySide6 workbench for the public
`unidecompiler.DecompilerEngine` API. It accepts one artifact, a directory, or
a ZIP/JAR archive and shows pseudocode, AST, bytecode, and diagnostics together.

Install from PyPI; cloning this repository is not required for normal use.

Install the base GUI with its Qt dependency:

```sh
python -m pip install unidecompiler-gui
```

Install all separately published frontend plugins when needed:

```sh
python -m pip install 'unidecompiler-gui[all-formats]'
```

The GUI never imports frontend plugin packages directly. It discovers installed
plugins through `DecompilerEngine` and does not modify input artifacts or save
workspace state. Its optional Simulation tab uses the separate generic IR
simulator and can load a trusted Python runtime file for unresolved functions.

## GUI plugins

The `Plugins` menu manages optional Python GUI plugins. A plugin is an
application-layer extension, separate from bytecode frontend plugins. It can
inspect immutable snapshots of open documents, functions, AST/reference
summaries and selections; add commands and declarative panels; request
navigation through stable IDs/source locations; and start asynchronous
simulation jobs.

Plugins cannot modify artifacts, generic IR, AST, pseudocode, frontend
registration, or simulator execution. They never receive a Qt Workbench,
frontend decoder payload, `ModuleIR`/`FunctionIR`, simulator frame, or stack.
Function lookup remains frontend-owned: a plugin submits an opaque query while
the GUI host delegates execution to the generic simulator.

Plugins are trusted in-process Python code, with the same permissions as the
GUI process. Review every local folder or GitHub repository before installing.
Dependencies in the manifest are checked at load time but never installed
automatically. Install, update, enable, disable, and removal take effect after
restart.

### Plugin layout

Install a folder with `plugin.toml` through `Plugins -> Manage plugins`, or
install a GitHub `owner/repository` or `/tree/ref` URL.

```toml
[plugin]
id = "example.function-browser"
name = "Function browser"
version = "1.0.0"
api = "1"
entry = "function_browser:register"

[python]
requires = []
```

```python
from unidecompiler_gui_sdk import Command, Panel, PanelState

def register(context):
    context.panels.register(Panel("functions", "Functions"))

    def refresh(ctx):
        document = ctx.active_document
        rows = () if document is None else tuple((item.name, item.status) for item in document.functions)
        ctx.set_panel_state("functions", PanelState.table(("Function", "Status"), rows))

    context.commands.register(Command("refresh", "Refresh function browser", refresh))
    context.subscribe("document_selected", lambda _document: refresh(context))
```

Use only `unidecompiler_gui_sdk` types. Its API is Qt-neutral and versioned;
`plugin.toml` must declare the matching API version. The repository includes
`unidecompiler-gui-test-plugin/` as a complete working example.

To simulate, first call `context.request_simulation_targets(document_id)`. It
returns immediately with a target-discovery job; observe
`simulation_targets_completed`, obtain its `SimulationTargetSnapshot` values
through `context.get_target_job(job_id)`, then pass a selected snapshot's
opaque `query` to `context.submit_simulation(...)`. Observe
`simulation_completed` and use the SDK's `SimulationResultSnapshot`; plugins
never receive simulator runner objects or frontend adapters.
