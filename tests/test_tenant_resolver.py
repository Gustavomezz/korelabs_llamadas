from app.tenant_resolver import normalize_e164_to_wa_id


def test_normalize_strips_plus():
    assert normalize_e164_to_wa_id("+523321015972") == "523321015972"


def test_normalize_no_plus_passthrough():
    assert normalize_e164_to_wa_id("523321015972") == "523321015972"


def test_normalize_empty():
    assert normalize_e164_to_wa_id("") == ""
