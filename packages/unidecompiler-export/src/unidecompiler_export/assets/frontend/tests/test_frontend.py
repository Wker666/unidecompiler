from __PACKAGE__.plugin import Frontend


def test_frontend_declares_stable_metadata() -> None:
    frontend = Frontend()
    assert frontend.id == "__PROJECT_ID__"
    assert frontend.supported_inputs
