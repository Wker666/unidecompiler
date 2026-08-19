# Workspace Inspector

This is an example plugin for `unidecompiler-gui`.

Install the repository root from `Plugins -> Manage plugins -> Install local
folder`.

Restart the GUI after installation. The `Plugins -> Commands` menu will contain
`Refresh workspace inspector` and `Discover simulation targets`. The plugin
panel appears as `Workspace Inspector` on the right side of the workspace.

The plugin intentionally imports only `unidecompiler_gui_sdk`. It receives
read-only snapshots, uses opaque simulation target queries through host jobs,
and does not import Qt, generic IR, a frontend, or the simulator.
