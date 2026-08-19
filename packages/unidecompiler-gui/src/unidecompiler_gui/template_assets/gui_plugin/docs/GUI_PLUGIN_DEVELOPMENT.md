# GUI Plugin Development Guide

This document is the complete guide for writing an `unidecompiler-gui` Python
plugin. Follow it without reading the GUI, core, frontend, or simulator source
code.

GUI plugins extend the desktop application. They are **not** VM frontends,
decompiler backends, simulator adapters, or MCP servers.

## 1. Design Boundary

The plugin boundary is intentionally strict:

```txt
your plugin -> unidecompiler-gui-sdk -> GUI plugin host -> public decompiler/simulator APIs
```

The GUI gives a plugin a versioned, read-only `PluginContext`. The plugin sees
immutable snapshots and sends requests to the host. This decoupling is required
for all plugins and prevents a plugin from depending on a particular bytecode
language or on GUI implementation details.

### Allowed

- Read document, function, AST, reference, selection, and job snapshots.
- Register menu commands and declarative dock panels.
- Ask the host to navigate to a function or source location.
- Subscribe to document and simulation events.
- Ask a frontend to enumerate simulation targets, then simulate a selected
  target through the generic simulator.
- Store JSON-compatible plugin settings in your own settings namespace.

### Forbidden

Do not import or access:

- `PySide6`, a `Workbench`, widgets, menus, or Qt threads.
- `unidecompiler.core`, `ModuleIR`, `FunctionIR`, decoded bytecode, thin IR,
  opcode tables, or frontend-private models.
- a frontend object or its `simulation_adapter`.
- simulator runners, frames, stacks, control-flow internals, or bytecode
  instructions.

Do not mutate input artifacts, generic IR, AST, pseudocode, registered
frontends, or simulator state. A plugin may be trusted Python code, but it must
still respect this API boundary. Code that needs to decode a VM or recover
language structures belongs in a frontend/core change, not in a GUI plugin.

## 2. Prerequisites

Install the GUI and its SDK in the Python environment that starts the GUI:

```sh
python -m pip install unidecompiler-gui
```

For a source checkout without installing packages, make the SDK importable
before starting the GUI:

```sh
ROOT=/path/to/decompile_c
PYTHONPATH="$ROOT/packages/unidecompiler-gui-sdk/src:$ROOT/packages/unidecompiler-simulator/src:$ROOT/packages/unidecompiler-simulation-host-python/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$ROOT/.venv/bin/python" -m unidecompiler_gui.app
```

The SDK package is named `unidecompiler_gui_sdk`:

```python
from unidecompiler_gui_sdk import Command, Panel, PanelState
```

Only import SDK types in plugin code. Do not make your plugin depend on the
source checkout layout.

## 3. Plugin Directory

Each plugin is a directory containing a root-level `plugin.toml` and the Python
entry module declared by that manifest.

```txt
my-plugin/
├── plugin.toml
├── my_plugin.py
├── helpers.py                 # optional
└── assets/                    # optional data files, loaded by your code
```

The GUI copies the entire directory to its user plugin store during
installation. The source folder is not run in place. Install, update, enable,
disable, and uninstall changes take effect after a GUI restart; plugins are not
hot-reloaded.

## 4. `plugin.toml`

Create this exact minimal manifest:

```toml
[plugin]
id = "com.example.function-browser"
name = "Function Browser"
version = "1.0.0"
api = "1"
entry = "function_browser:register"
description = "Lists functions from the active decompiled document."

[python]
requires = []
```

### `[plugin]` fields

| Field | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Stable plugin identifier. It must start with a letter or digit and then use only letters, digits, `.`, `_`, or `-`. Examples: `acme.tools`, `my_plugin-2`. Do not use `/`, `\\`, spaces, or `..`. |
| `name` | yes | Human-visible plugin name. |
| `version` | yes | Human-visible plugin release version. Use a normal version such as `1.0.0`. |
| `api` | yes | SDK API version. The current value is exactly `"1"`. |
| `entry` | yes | Python entry point in `module:callable` form. Both the module components and callable must be Python identifiers. |
| `description` | no | Human-visible summary. |

`entry = "function_browser:register"` means the GUI imports
`function_browser.py` from the plugin root and calls `register(context)` once
at GUI startup.

### `[python]` fields

`requires` is an optional list of Python distribution names already installed
in the GUI environment:

```toml
[python]
requires = ["requests>=2.31", "tomli"]
```

The GUI checks whether every named distribution exists. It never runs `pip` or
installs dependencies. If a dependency is missing, the plugin does not load;
install it yourself into the GUI's Python environment, then restart the GUI.

Avoid unnecessary dependencies. A plugin that only uses
`unidecompiler_gui_sdk` should use `requires = []`.

## 5. Install and Test Locally

1. Start `unidecompiler-gui` using the environment that can import the SDK.
2. Open `Plugins -> Manage plugins`.
3. Select `Install local folder`.
4. Choose the folder containing `plugin.toml`, not the Python file.
5. Restart the GUI.
6. Open `Plugins -> Commands` and check that your commands are present.

The user plugin store is platform dependent:

| Platform | Store root |
| --- | --- |
| Linux | `$XDG_DATA_HOME/unidecompiler/plugins`, or `~/.local/share/unidecompiler/plugins` |
| macOS | `~/Library/Application Support/unidecompiler/plugins` |
| Windows | `%APPDATA%/unidecompiler/plugins` |

The plugin manager can install from a local folder or GitHub. GitHub input can
be `owner/repository`, a repository URL, or a URL ending in `/tree/<ref>` or
`/commit/<ref>`. The manager downloads a GitHub archive, rejects unsafe archive
paths, and copies the plugin into the same user plugin store.

Treat local and GitHub plugins as trusted code. They run in the GUI Python
process and are not sandboxed.

## 6. Your Entry Function

The entry callable gets one plugin-scoped context:

```python
from unidecompiler_gui_sdk import Command, Panel, PanelState


def register(context):
    context.panels.register(Panel("overview", "Overview"))

    def refresh(active_context):
        document = active_context.active_document
        if document is None:
            active_context.set_panel_state(
                "overview", PanelState.text_view("No active document")
            )
            return
        rows = tuple(
            (function.name, function.status, ", ".join(function.params))
            for function in document.functions
        )
        active_context.set_panel_state(
            "overview",
            PanelState.table(("Function", "Status", "Parameters"), rows),
        )

    context.commands.register(Command("refresh", "Refresh overview", refresh))
    context.subscribe("document_selected", lambda _document: refresh(context))
```

`register` runs on the GUI thread. Keep it short: register panels, commands,
and event handlers. Do not block startup with network or long-running work.

### Local IDs and namespacing

`Panel.id` and `Command.id` are local plugin IDs. The host automatically scopes
them as `<plugin-id>.<local-id>`. In the preceding example the panel key is
internally `com.example.function-browser.overview`, but all calls from that
plugin must use only `"overview"`:

```python
context.set_panel_state("overview", PanelState.text_view("Updated"))
```

Use stable, unique local IDs. Registering the same local command/panel more
than once raises an error during plugin loading.

## 7. Commands and Panels

### Commands

Register a `Command` through `context.commands.register`:

```python
def show_active(context):
    document = context.active_document
    if document is not None:
        context.focus_function(document.id, document.functions[0].id)

context.commands.register(
    Command(
        id="show-first-function",
        title="Show first function",
        callback=show_active,
        shortcut="Ctrl+Alt+F",  # optional
    )
)
```

Commands appear under `Plugins -> Commands`. The host catches command callback
exceptions and reports a diagnostic in the GUI status bar. Handle expected
conditions yourself, such as an empty workspace or a resource document with no
functions.

### Panels

Panels are declarative. You provide a title and `PanelState`; the GUI creates
the dock widget and renders it. You cannot and must not create a Qt widget.

```python
context.panels.register(Panel("notes", "Notes", PanelState.text_view("Ready")))
context.set_panel_state("notes", PanelState.text_view("Document changed"))
```

Two panel states are available:

```python
# Read-only text panel.
PanelState.text_view("A short report")

# Read-only table panel. Values are converted to strings by the SDK.
PanelState.table(
    ("Name", "Status"),
    (("main", "ok"), ("helper", "partial")),
)
```

The table has one column for every value in `columns`. Supply rows of matching
length to avoid an incomplete presentation. Panels are output views; they do
not offer plugin-defined buttons, editable fields, or custom widgets. Use
commands for user actions.

## 8. Read-Only Workspace API

All snapshot dataclasses are frozen. Treat identifiers as opaque stable strings
within the currently open document. Do not construct IDs by parsing names.

### Documents and selection

```python
documents = context.documents                 # tuple[DocumentSnapshot, ...]
document = context.active_document            # DocumentSnapshot | None
selection = context.selection                 # SelectionSnapshot
same = context.get_document(document.id)      # DocumentSnapshot | None
```

`DocumentSnapshot` fields:

| Field | Meaning |
| --- | --- |
| `id` | Document ID. Pass it back to every document-scoped API. It is normally the display path, including archive-member provenance when relevant. |
| `display_name` | Short user-facing filename. |
| `status` | Decompilation status such as `ok`, `partial`, `unsupported`, `error`, or `resource`. |
| `frontend_id` | Selected dynamic frontend ID, or `None` for a resource/error before selection. Do not branch on it for language semantics. |
| `revision` | Increments when that document result is replaced. Compare it with asynchronous job revisions. |
| `functions` | `FunctionSnapshot` records for the document. |
| `pseudocode` | Read-only rendered pseudocode text, or an empty string when unavailable. |

`SelectionSnapshot` has `document_id`, `function_id`, and an optional source
location. `function_id` can be `None` when the document itself is selected.

### Functions

```python
function = context.get_function(document_id, function_id)
```

`FunctionSnapshot` has `id`, `name`, `status`, `params`, and optional `source`.
Function names are not necessarily unique: always navigate or look up using
`id`, never by name alone.

### AST snapshots

```python
module_ast = context.get_ast(document_id)
function_ast = context.get_ast(document_id, function_id)
```

The return is `AstNodeSnapshot | None`. A module request returns the recovered
module AST; a function request returns the matching recovered function AST, or
`None` if it cannot be matched safely. Each node has:

- `id`: a presentation ID useful for display only.
- `kind`: generic recovered node class name, such as `FunctionDecl` or
  `IfStmt`.
- `source`: optional `SourceLocation`.
- `children`: nested `AstNodeSnapshot` values.

These are generic recovered AST facts. Do not infer VM instructions, source
language features, or frontend implementation behavior from node names.

### References

```python
all_references = context.get_references(document_id)
function_references = context.get_references(document_id, function_id)
```

Each `ReferenceSnapshot` contains `kind`, `name`, the containing `function_id`,
optional source, and zero or more `target_ids`. A missing target does not mean
the reference is invalid; it may be external, dynamic, or unresolved.

### Source locations and navigation

```python
from unidecompiler_gui_sdk import SourceLocation

context.focus_function(document_id, function_id)
context.focus_source(
    document_id,
    SourceLocation(frontend="python-pyc", offset=42),
)
```

Both calls return `True` only when the host can navigate. `focus_source` finds
the nearest matching rendered source mapping for the requested frontend/offset.
Use a `SourceLocation` obtained from a snapshot whenever possible. Do not guess
another frontend ID or manufacture bytecode offsets.

## 9. Events

Subscribe once during `register`:

```python
unsubscribe = context.subscribe("document_selected", on_document_selected)
```

The callback receives the payload listed below. Keep callbacks short; they run
on the GUI thread. You can retain `unsubscribe` and call it later if your own
plugin logic no longer needs the event.

| Event | Payload | When it occurs |
| --- | --- | --- |
| `document_updated` | `DocumentSnapshot | None` | A decompilation result was loaded or replaced. |
| `document_selected` | `DocumentSnapshot | None` | The tree selection changes to an open document/function. |
| `simulation_targets_started` | `SimulationTargetJobSnapshot` | Frontend-owned target discovery was scheduled. |
| `simulation_targets_completed` | `SimulationTargetJobSnapshot` | Target discovery completed, possibly with a diagnostic. |
| `simulation_targets_failed` | `SimulationTargetJobSnapshot` | Target discovery worker failed. |
| `simulation_started` | `SimulationJobSnapshot` | Generic simulation was scheduled. |
| `simulation_completed` | `SimulationJobSnapshot` | Simulation produced a structured result. |
| `simulation_failed` | `SimulationJobSnapshot` | Simulation worker failed before it could produce a result. |
| `plugin_failed` | `str` | Another installed plugin failed while loading. Useful for diagnostics only. |

An event may carry `None` for a document when no active document exists. Never
assume a selected document or selected function exists.

## 10. Simulation

Simulation is optional. A frontend may not support it, and a plugin must handle
that outcome normally. The only correct cross-language workflow is:

1. Pick a `document_id` from a snapshot.
2. Call `request_simulation_targets(document_id)`.
3. Wait for the corresponding `simulation_targets_completed` event.
4. Read the completed job with `get_target_job(job.id)`.
5. Let the user choose a returned `SimulationTargetSnapshot`.
6. Call `submit_simulation(document_id, target.query, args, runtime_path)`.
7. Wait for `simulation_completed` or `simulation_failed`.
8. Read the job using `get_job(job.id)`.

Never build a target query from a function name. Lua functions, Java methods,
Python nested functions, overloads, and other frontends can require different
lookup logic. The selected frontend owns that logic. `target.query` is an opaque
data value: store it only for the relevant session and pass it back unchanged.

### Target discovery example

```python
from unidecompiler_gui_sdk import Command

def discover(context):
    document = context.active_document
    if document is not None:
        context.request_simulation_targets(document.id)

def targets_ready(job):
    if job.status != "completed" or job.stale:
        return
    if job.diagnostic:
        # The selected frontend may not support target discovery.
        return
    for target in job.targets:
        print(target.label, target.params)
        # Keep target.query; do not inspect or recreate it.

context.commands.register(Command("discover", "Discover targets", discover))
context.subscribe("simulation_targets_completed", targets_ready)
```

`SimulationTargetJobSnapshot` fields are `id`, `document_id`, `revision`,
`status`, `targets`, `diagnostic`, and `stale`. A valid completed job can have
an empty target list. Check `diagnostic` to explain unsupported simulation or
target discovery errors.

### Start and inspect simulation

```python
selected_target = None  # Save this from a completed target job.

def run(context):
    document = context.active_document
    if document is None or selected_target is None:
        return
    context.submit_simulation(
        document.id,
        selected_target.query,
        args=(5,),
        runtime_path=None,
    )

def simulation_done(job):
    if job.status != "completed" or job.result is None or job.stale:
        return
    result = job.result
    print(result.status, result.values, result.steps)
    for event in result.events:
        print(event.kind, event.function, event.detail)

context.commands.register(Command("run", "Run selected target", run))
context.subscribe("simulation_completed", simulation_done)
```

Arguments must be generic runtime values accepted by the simulator. Use simple
JSON-like values such as `None`, booleans, numbers, strings, lists, and maps.
Simulation validates arguments and returns an explicit invalid-request result
when they cannot be represented safely.

`runtime_path` is optional. When specified, it is a path to a Python file that
implements unresolved external functions, for example `print`. That file is
trusted host code executed with GUI process permissions; it is not sandboxed.
Do not select a runtime file automatically without an explicit user decision.

`SimulationJobSnapshot` fields are `id`, `document_id`, `revision`, `status`,
`result`, `diagnostic`, and `stale`. `cancel_job(job_id)` returns `True` only
while a simulation job is still cancellable.

`SimulationResultSnapshot` contains `status`, returned `values`, textual
`exception`/`cause`, final local bindings, `steps`, `diagnostic`, trace events,
and `trace_truncated`. All outcomes are structured: completed execution,
raised values, unsupported IR, unhandled external calls, invalid requests,
limits, and cancellation must be presented as their actual status, not as a
successful result.

## 11. Document Revisions and Stale Jobs

Every job records the document revision from when it started. If the document
is reloaded/replaced before the job ends, the host sets `job.stale` to `True`.
Do not apply stale output to a panel as if it represented the current document.

Safe event handler pattern:

```python
def targets_ready(job):
    current = context.get_document(job.document_id)
    if job.stale or current is None or current.revision != job.revision:
        return
    # This output belongs to the currently displayed document revision.
```

This is essential with multiple open files and background analysis/simulation.

## 12. Plugin Settings

`context.settings` is persistent, JSON-compatible storage isolated to the
current plugin ID.

```python
previous = context.settings.get("ui/last-document", None)
context.settings.set("ui/last-document", "example.pyc")
context.settings.set("options", {"show_partial": True, "max_rows": 200})
context.settings.delete("ui/last-document")
```

Keys must be non-empty relative paths. Do not begin them with `/` or use `..`.
Values must be JSON serializable. A plugin cannot read or write another
plugin's namespace.

## 13. Complete Example

This plugin has one panel, shows functions for the active document, and lists
simulation targets after a command. It is language-neutral and uses no private
implementation API.

```python
from unidecompiler_gui_sdk import Command, Panel, PanelState


PANEL_ID = "inspector"


def register(context):
    context.panels.register(Panel(PANEL_ID, "Function Inspector"))

    def show_document(active_context):
        document = active_context.active_document
        if document is None:
            active_context.set_panel_state(
                PANEL_ID, PanelState.text_view("No active document")
            )
            return
        rows = tuple(
            (function.name, function.status, ", ".join(function.params))
            for function in document.functions
        )
        active_context.set_panel_state(
            PANEL_ID,
            PanelState.table(("Function", "Status", "Parameters"), rows),
        )

    def find_targets(active_context):
        document = active_context.active_document
        if document is not None:
            active_context.request_simulation_targets(document.id)

    def targets_ready(job):
        current = context.get_document(job.document_id)
        if job.stale or current is None or current.revision != job.revision:
            return
        if job.status != "completed":
            context.set_panel_state(PANEL_ID, PanelState.text_view(job.diagnostic or "Target lookup failed"))
            return
        if job.diagnostic:
            context.set_panel_state(PANEL_ID, PanelState.text_view(job.diagnostic))
            return
        rows = tuple(
            (target.label, ", ".join(target.params)) for target in job.targets
        )
        context.set_panel_state(
            PANEL_ID, PanelState.table(("Simulation target", "Parameters"), rows)
        )

    context.commands.register(Command("refresh", "Refresh function inspector", show_document))
    context.commands.register(Command("targets", "Find simulation targets", find_targets))
    context.subscribe("document_selected", lambda _document: show_document(context))
    context.subscribe("document_updated", lambda _document: show_document(context))
    context.subscribe("simulation_targets_completed", targets_ready)
    context.subscribe("simulation_targets_failed", targets_ready)
```

Pair it with this manifest:

```toml
[plugin]
id = "com.example.function-inspector"
name = "Function Inspector"
version = "1.0.0"
api = "1"
entry = "function_inspector:register"

[python]
requires = []
```

## 14. Troubleshooting

| Symptom | Cause and solution |
| --- | --- |
| `GUI plugin requires plugin.toml` | Select the directory containing `plugin.toml`, not a parent or Python file. |
| API incompatible error | Set `[plugin].api` to exactly `"1"`, or upgrade the GUI/SDK together when a newer API is documented. |
| Entry error | Check `entry` has `module:callable` form, the module exists in the plugin root, and the callable accepts one context argument. |
| Missing Python dependencies | Install the declared distribution in the GUI interpreter. The GUI does not install it. |
| Plugin does not appear after install | Restart the GUI. Installation does not hot-load plugins. |
| Command appears but fails | Check the GUI status bar. Handle `active_document is None`, empty function lists, and resource documents. |
| Panel state raises `KeyError` | Register the panel first and use its local ID, not the host-qualified ID. |
| No simulation targets | The selected frontend may not support simulation, no targets may be unambiguous, or `job.diagnostic` explains the issue. |
| A simulation result is ignored | Check `job.stale`; reload/reanalysis changed the document revision while the job ran. |
| `focus_function`/`focus_source` returns `False` | The document/function/source has no safe matching view. Use IDs and `SourceLocation` values obtained from snapshots. |

## 15. Release Checklist

Before publishing or sharing a plugin:

1. Verify `plugin.toml` is at the plugin root and uses a safe, stable ID.
2. Start a fresh GUI process, install the local folder, restart, and confirm all
   commands and panels load.
3. Test no-file, resource-file, partial/unsupported result, and multiple-open-
   document states.
4. Treat all snapshot values as read-only; do not import private GUI/core/
   frontend/simulator modules.
5. Test failed and unsupported target discovery, a completed simulation, and a
   stale job after reloading a document.
6. Keep all `target.query` values opaque and return them only to
   `submit_simulation` for the same document/session.
7. Document any Python dependencies and never expect the GUI to install them.
8. Explain clearly if your plugin asks users to choose a trusted `runtime.py`.

For a complete working example, see the repository plugin under
`unidecompiler-gui-test-plugin/`. Install that directory itself through the
GUI plugin manager.
