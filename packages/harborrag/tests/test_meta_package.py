from harborrag import CompositionRoot, Document


def test_meta_exports_implemented_public_facade():
    assert CompositionRoot.__name__ == "CompositionRoot"
    assert Document.__name__ == "Document"
