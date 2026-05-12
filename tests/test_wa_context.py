"""Tests de los helpers que inyectan contexto de WhatsApp al system
prompt cuando el caller tiene historial previo."""
from datetime import datetime, timedelta, timezone

from app.routers.twilio_stream import (
    _build_returning_user_greeting_hint,
    _build_wa_context_block,
    _format_wa_recent_messages,
)


def _msg(role: str, content: str, hours_ago: float = 1.0) -> dict:
    return {
        "role": role,
        "content": content,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    }


def test_format_recent_truncates_long_content():
    long_text = "x" * 500
    out = _format_wa_recent_messages([_msg("user", long_text)], max_chars=100)
    assert "…" in out
    assert len(out) < 400  # cabe en presupuesto


def test_format_recent_skips_empty_content():
    out = _format_wa_recent_messages([
        _msg("user", "hola"),
        _msg("assistant", ""),
        _msg("assistant", "  "),
        _msg("user", "ok"),
    ])
    # Solo deben aparecer 2 líneas (hola, ok)
    lines = [l for l in out.split("\n") if l.strip()]
    assert len(lines) == 2


def test_format_recent_labels_roles_in_spanish():
    out = _format_wa_recent_messages([
        _msg("user", "hola"),
        _msg("assistant", "saludos"),
    ])
    assert "Usuario" in out
    assert "Kora" in out


def test_format_recent_relative_time():
    out = _format_wa_recent_messages([
        _msg("user", "ayer", hours_ago=30),
        _msg("user", "ahora", hours_ago=0.01),
    ])
    assert "día" in out
    assert "minutos" in out


def test_build_context_block_includes_name_and_clinic():
    ctx = {
        "name": "Juan Pérez",
        "clinic_name": "Consultorio X",
        "qualified": True,
        "total_messages": 8,
        "recent_messages": [_msg("user", "hola"), _msg("assistant", "hey")],
    }
    block = _build_wa_context_block(ctx)
    assert "Juan Pérez" in block
    assert "Consultorio X" in block
    assert "calificado" in block
    assert "8 mensajes" in block
    assert "CONTEXTO PREVIO POR WHATSAPP" in block


def test_build_context_block_handles_missing_fields():
    """Sin nombre, sin clinic, sin qualified — no debe crashear ni filtrar None."""
    ctx = {
        "name": None,
        "clinic_name": None,
        "qualified": False,
        "total_messages": 1,
        "recent_messages": [_msg("user", "hola")],
    }
    block = _build_wa_context_block(ctx)
    assert "None" not in block
    # No debe afirmar calificación si no la tiene
    assert "calificado" not in block


def test_returning_greeting_uses_first_name_only():
    """Si el nombre del WA profile es 'Juan Carlos Pérez González', el
    greeting debería decir 'Hola Juan' — no la cadena completa."""
    hint = _build_returning_user_greeting_hint({
        "name": "Juan Carlos Pérez González",
    })
    assert "Juan" in hint
    assert "Pérez González" not in hint


def test_returning_greeting_omits_name_part_when_unknown():
    hint = _build_returning_user_greeting_hint({"name": None})
    # No debe decir "¡Hola !" con espacio raro o "None"
    assert "None" not in hint
    assert "¡Hola !" not in hint
    assert "¡Hola!" in hint


def test_returning_greeting_avoids_outbound_phrasing():
    """La llamada es inbound (usuario llamó al bot). El greeting NO debe
    decir 'te marco', 'te llamo' o similares — eso implica que el bot
    inició la llamada, lo cual es falso."""
    hint = _build_returning_user_greeting_hint({"name": "Ana"})
    lower = hint.lower()
    # El hint le DICE al modelo qué decir + qué NO decir; las prohibiciones
    # aparecerán en el texto de instrucciones — checamos que la frase
    # textual a decir NO contenga "te marco" / "te llamo".
    quoted_section = hint.split('"')[1] if '"' in hint else hint
    assert "te marco" not in quoted_section.lower()
    assert "te llamo" not in quoted_section.lower()


def test_context_block_warns_inbound_direction():
    """El bloque de contexto debe instruir al modelo que es llamada inbound
    para evitar phrasing de llamada saliente."""
    block = _build_wa_context_block({
        "name": "Ana", "clinic_name": None,
        "qualified": False, "total_messages": 3,
        "recent_messages": [_msg("user", "hola")],
    })
    assert "INBOUND" in block or "inbound" in block
