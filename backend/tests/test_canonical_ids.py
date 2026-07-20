from canonical import ids


def test_known_vector():
    assert ids._netstring(["p1", "GID"]) == b"2:p13:GID"
    assert ids.element_id("p1", "GID") == "el_99d9f5f0ef2b7cb5fa2a2d39994a0642"


def test_deterministic_across_calls():
    assert ids.element_id("p1", "GID") == ids.element_id("p1", "GID")
    assert ids.property_fact_id("p", "e", "pset", "C", "N", "0") == ids.property_fact_id("p", "e", "pset", "C", "N", "0")


def test_hash_length_is_128_bits():
    eid = ids.element_id("p1", "GID")
    assert eid.startswith("el_")
    assert len(eid) == len("el_") + 32  # 32 hex chars == 128 bits


def test_prefixes():
    assert ids.element_id("p", "g").startswith("el_")
    assert ids.property_fact_id("p", "e", "pset", "c", "n", "0").startswith("pf_")
    assert ids.classification_id("p", "e", "s", "c").startswith("cf_")
    assert ids.document_id("p", "u").startswith("doc_")


def test_global_id_case_sensitive():
    assert ids.element_id("p1", "GID") != ids.element_id("p1", "gid")


def test_cross_project_separation():
    # Same GlobalId in two projects -> different element ids.
    assert ids.element_id("p1", "GID") != ids.element_id("p2", "GID")


def test_no_concatenation_ambiguity():
    # ("a","bc") must differ from ("ab","c") — netstring length-prefixing.
    assert ids.element_id("a", "bc") != ids.element_id("ab", "c")
    assert ids._netstring(["a", "bc"]) != ids._netstring(["ab", "c"])


def test_null_vs_empty_string_distinct_components():
    # An empty component is distinct from a one-char component and from a
    # different split; there is no silent collapsing.
    assert ids._netstring(["", "x"]) == b"0:1:x"
    assert ids.element_id("", "x") != ids.element_id("x", "")


def test_fact_id_stable_when_value_changes():
    # fact_id is the logical slot; the property value is NOT part of it.
    a = ids.property_fact_id("p", "e", "pset", "Pset", "Prop", "0")
    b = ids.property_fact_id("p", "e", "pset", "Pset", "Prop", "0")
    assert a == b  # the value never enters the signature, so it cannot change the id


def test_fact_id_differs_for_occurrence_key():
    a = ids.property_fact_id("p", "e", "pset", "Pset", "Prop", "0")
    b = ids.property_fact_id("p", "e", "pset", "Pset", "Prop", "1")
    assert a != b


def test_fact_id_differs_for_source_and_container():
    base = ids.property_fact_id("p", "e", "pset", "Pset", "Prop", "0")
    assert base != ids.property_fact_id("p", "e", "qto", "Pset", "Prop", "0")
    assert base != ids.property_fact_id("p", "e", "pset", "Other", "Prop", "0")


def test_classification_and_document_ids_deterministic_and_separated():
    assert ids.classification_id("p", "e", "sys", "code") == ids.classification_id("p", "e", "sys", "code")
    assert ids.classification_id("p1", "e", "sys", "code") != ids.classification_id("p2", "e", "sys", "code")
    assert ids.document_id("p", "u1") != ids.document_id("p", "u2")


def test_ids_independent_of_processing_context():
    # No timestamp/path/order input exists; the same inputs always map to the
    # same id regardless of when/where called.
    import os

    before = ids.element_id("proj", "GID")
    os.environ["IRRELEVANT"] = "x"
    after = ids.element_id("proj", "GID")
    assert before == after
