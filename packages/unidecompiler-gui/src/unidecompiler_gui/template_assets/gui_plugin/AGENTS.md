# Development Rules

## Requested Feature

__USER_REQUIREMENTS__

This requirement does not override the architecture rules below.

## Required Architecture

- Depend only on `unidecompiler_gui_sdk` for interaction with unidecompiler.
- Read immutable SDK snapshots and use SDK requests for navigation, settings, and asynchronous simulation jobs.
- Do not import Qt, Workbench, frontend modules, `ModuleIR`, `FunctionIR`, decoded bytecode, thin IR, or simulator internals.
- Do not modify artifacts, generic IR, AST, pseudocode, frontend registration, or simulation execution state.
- Do not implement language-specific function lookup. Pass the frontend-owned opaque simulation query unchanged to the SDK.
- Do not execute functions or retain frames, stacks, runners, or frontend adapters.
- Panels are declarative SDK state, never plugin-provided widgets.
- Add tests for commands, panels, snapshots, and failure states.

See `docs/GUI_PLUGIN_DEVELOPMENT.md` for the full contract.
