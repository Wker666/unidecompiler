# __DISPLAY_NAME__

__DESCRIPTION__

## Requested Feature

__USER_REQUIREMENTS__

Project generation, file export, and progress display remain host concerns. A
GUI plugin must use only the GUI SDK and must not import or depend on the
`unidecompiler-export` implementation at runtime.

## Development

Read `AGENTS.md`, then follow `docs/GUI_PLUGIN_DEVELOPMENT.md`. Use only
`unidecompiler_gui_sdk`; the plugin is a read-only application extension.
