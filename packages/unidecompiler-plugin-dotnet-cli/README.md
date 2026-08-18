# unidecompiler-plugin-dotnet-cli

Frontend plugin for .NET CLI `.dll` and `.exe` assemblies. It uses `dnfile` to
read assembly metadata and IL, then submits neutral thin IR to `unidecompiler`.

Install with:

```sh
python -m pip install unidecompiler-plugin-dotnet-cli
```

The plugin is discovered automatically by compatible CLI and GUI hosts.
