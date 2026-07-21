from harborrag import CompositionRoot, Document


def test_meta_exports_implemented_public_facade():
    diagnostics = CompositionRoot.local().diagnostics()

    assert diagnostics["engine"]["tenant"] == "default"
    assert Document.__name__ == "Document"
