from app.tenant_resolver import normalize_e164_to_wa_id


def test_normalize_mx_mobile_inserts_1_to_match_whatsapp():
    """Caso clave: contacto unificado entre llamadas y WhatsApp en MX."""
    # Twilio: +523131088881 -> WhatsApp guarda como 5213131088881
    assert normalize_e164_to_wa_id("+523131088881") == "5213131088881"
    # Sin el '+' tampoco
    assert normalize_e164_to_wa_id("523131088881") == "5213131088881"


def test_normalize_mx_already_normalized_passthrough():
    """Si ya viene con el '1' móvil (13 chars), no duplicar."""
    assert normalize_e164_to_wa_id("+5213131088881") == "5213131088881"
    assert normalize_e164_to_wa_id("5213131088881") == "5213131088881"


def test_normalize_non_mx_just_strips_plus():
    """Para otros países, solo quitar el '+'."""
    # USA
    assert normalize_e164_to_wa_id("+14155551234") == "14155551234"
    # España
    assert normalize_e164_to_wa_id("+34666123456") == "34666123456"
    # UK
    assert normalize_e164_to_wa_id("+447911123456") == "447911123456"


def test_normalize_empty():
    assert normalize_e164_to_wa_id("") == ""
    assert normalize_e164_to_wa_id(None) == ""


def test_normalize_with_non_digits_does_not_force_mx_rule():
    """Si trae caracteres raros, no aplicar la regla MX (no es seguro)."""
    assert normalize_e164_to_wa_id("+52 33 2101 5972") == "52 33 2101 5972"
