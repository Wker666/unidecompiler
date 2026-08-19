from __PACKAGE__.decoder import looks_like_input


def test_filename_suffix_is_recognized() -> None:
    assert looks_like_input(b"", "sample__FIRST_SUFFIX__")
