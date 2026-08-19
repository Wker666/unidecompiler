# EmojiVM unidecompiler frontend

This standalone frontend lives outside the `unidecompiler/` checkout. It
decodes the `.evm` EmojiVM text format documented in `../EMOJIVM.md` and submits thin
VM steps to the generic unidecompiler pipeline.

Register this directory from the GUI with `Frontend manager -> Register folder`.
Choose the directory that contains `unidecompiler-plugin.toml`:

```text
unidecompiler-plugin-emojivm/
```

## Simulation

The frontend exposes the generic-IR `main` function as a simulation target.
The shared unidecompiler simulator then executes that generic IR; this plugin
does not execute EmojiVM bytecode.

Programs that need I/O or buffer behavior require a host-provided external
environment. If recovery is partial or an operation is not supported by the
generic simulator, the result is reported explicitly instead of being treated
as a successful execution.
