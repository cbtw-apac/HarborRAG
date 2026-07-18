from harborrag import CompositionRoot, HarborDocument


def test_meta_exports_implemented_public_facade():
    diagnostics = CompositionRoot.local().diagnostics()

    assert diagnostics["engine"]["tenant"] == "default"
    assert HarborDocument.__name__ == "HarborDocument"
