"""Cross-platform PyInstaller definition for the all-formats GUI bundle."""
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


ROOT = Path(SPECPATH)
sys.path.insert(0, str(ROOT / "src"))
GUI_PACKAGE = "unidecompiler_gui"
PLUGIN_PACKAGES = (
    "unidecompiler_plugin_python_pyc",
    "unidecompiler_plugin_jvm_class",
    "unidecompiler_plugin_lua",
    "unidecompiler_plugin_dotnet_cli",
    "unidecompiler_plugin_wasm",
)
PLUGIN_DISTRIBUTIONS = (
    "unidecompiler-plugin-python-pyc",
    "unidecompiler-plugin-jvm-class",
    "unidecompiler-plugin-lua",
    "unidecompiler-plugin-dotnet-cli",
    "unidecompiler-plugin-wasm",
)

hiddenimports = collect_submodules("unidecompiler")
for package in (GUI_PACKAGE, *PLUGIN_PACKAGES):
    hiddenimports.extend(collect_submodules(package))

datas = [(str(ROOT / "src" / "unidecompiler_gui" / "themes"), "unidecompiler_gui/themes")]
for distribution in ("unidecompiler", *PLUGIN_DISTRIBUTIONS):
    datas.extend(copy_metadata(distribution))

a = Analysis(
    [str(ROOT / "src" / "unidecompiler_gui" / "app.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="unidecompiler-gui", console=False)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="unidecompiler-gui")
