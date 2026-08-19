def test_plugin_module_imports() -> None:
    from __PACKAGE__.plugin import register

    assert callable(register)
