# --- core/messaging.py ---
"""Salida hacia WhatsApp (Evolution API) y lectura del webhook entrante.

Los dos bots mandaban mensajes de forma distinta: el de pacientes sólo texto
plano y siempre a la instancia del .env; el de quirófanos texto, botones y
listas interactivas, con la instancia resuelta por clínica. Queda la segunda,
que es un superconjunto: el flujo de pacientes sigue mandando texto plano igual
que antes, y ahora puede además usar listas donde convenga.
"""

import time

import requests
from flask import g

import config

# Campos donde Evolution/Baileys pueden traer el número real cuando el
# remoteJid es un '@lid'. El nombre cambió entre versiones (Baileys pasó a
# 'remoteJidAlt'), así que se prueban todos en orden.
_LID_PHONE_FIELDS = ("senderPn", "remoteJidAlt", "participantAlt", "previousRemoteJid")


def _evolution_headers():
    return {"apikey": config.EVOLUTION_API_KEY, "Content-Type": "application/json"}


def _resolve_instance(instance):
    """Instancia de Evolution a la que mandar.

    Se acepta explícita porque los hilos de fondo (limpieza de sesiones,
    recordatorios de citas) mandan mensajes fuera de una petición, y ahí `g` no
    existe: esos guardan la instancia en la sesión o en el recordatorio y la
    pasan por parámetro.
    """
    if instance:
        return instance
    try:
        return g.evolution_instance
    except RuntimeError:
        return config.EVOLUTION_INSTANCE_NAME


def _log_evolution_error(label, e):
    """Registra el error real de una llamada a Evolution.

    El texto de RequestException por sí solo ('400 Client Error: Bad Request
    for url: ...') no dice qué campo rechazó la API; el cuerpo de la respuesta
    sí trae el detalle (p.ej. qué propiedad falta o sobra), así que se imprime
    también cuando hay una respuesta HTTP disponible.
    """
    resp = getattr(e, "response", None)
    detalle = f" -- body: {resp.text[:500]}" if resp is not None else ""
    print(f"ERROR {label}: {e}{detalle}")


def send_whatsapp_message(phone, text, instance=None):
    # La pausa antes de cada envío es a propósito: ver SEND_PACING_SECONDS.
    time.sleep(config.SEND_PACING_SECONDS)
    inst = _resolve_instance(instance)
    try:
        requests.post(
            f"{config.EVOLUTION_API_URL}/message/sendText/{inst}",
            json={"number": phone, "text": text},
            headers=_evolution_headers(),
            timeout=config.EVOLUTION_TIMEOUT,
        ).raise_for_status()
        print(f"[TEXT] → {phone}")
    except requests.exceptions.RequestException as e:
        _log_evolution_error("send_whatsapp_message", e)


def send_button_message(phone, body, buttons, instance=None, title="", footer="LOLIMSA"):
    """buttons = [{"id": "btn_id", "title": "Label"}, ...]  — máx. 3"""
    time.sleep(config.SEND_PACING_SECONDS)
    inst = _resolve_instance(instance)
    payload = {
        "number": phone,
        "title": title,
        "description": body,
        "footer": footer,
        "buttons": [
            {"type": "reply", "reply": {"id": b["id"], "title": b["title"]}}
            for b in buttons
        ],
    }
    try:
        requests.post(
            f"{config.EVOLUTION_API_URL}/message/sendButtons/{inst}",
            json=payload,
            headers=_evolution_headers(),
            timeout=config.EVOLUTION_TIMEOUT,
        ).raise_for_status()
        print(f"[BUTTONS] → {phone}")
    except requests.exceptions.RequestException as e:
        _log_evolution_error("send_button_message", e)
        print("  -- falling back to text")
        lines = "\n".join(f"*{i+1}.* {b['title']}" for i, b in enumerate(buttons))
        send_whatsapp_message(phone, f"{body}\n\n{lines}", inst)


def send_list_message(phone, body, sections, instance=None, title="", button_text="Ver opciones", footer=""):
    """sections = [{"title": "Sec", "rows": [{"id": "r1", "title": "T", "description": "D"}]}]

    A Evolution se le manda 'footerText' y 'rowId' (no 'footer'/'id'): son los
    nombres que espera el endpoint /message/sendList; con los nombres viejos la
    API devuelve 400 Bad Request y el mensaje nunca llega como lista
    interactiva, siempre por el texto de respaldo.

    Sólo la usan pantallas cuyas opciones se responden por posición, así que el
    respaldo de texto puede numerarlas 1, 2, 3... sin ambigüedad: el número que
    ve el usuario es el mismo que entiende process_user_choice.
    """
    time.sleep(config.SEND_PACING_SECONDS)
    inst = _resolve_instance(instance)
    api_sections = [
        {
            "title": sec.get("title", ""),
            "rows": [
                {"title": row["title"], "description": row.get("description", ""), "rowId": row["id"]}
                for row in sec.get("rows", [])
            ],
        }
        for sec in sections
    ]
    payload = {
        "number": phone,
        "title": title,
        "description": body,
        "buttonText": button_text,
        "footerText": footer,
        "sections": api_sections,
    }
    try:
        requests.post(
            f"{config.EVOLUTION_API_URL}/message/sendList/{inst}",
            json=payload,
            headers=_evolution_headers(),
            timeout=config.EVOLUTION_TIMEOUT,
        ).raise_for_status()
        print(f"[LIST] → {phone}")
    except requests.exceptions.RequestException as e:
        _log_evolution_error("send_list_message", e)
        print("  -- falling back to text")
        lines = [
            f"*{i}.* {row['title']}"
            for i, row in enumerate([r for sec in sections for r in sec.get("rows", [])], 1)
        ]
        send_whatsapp_message(phone, f"{body}\n\n" + "\n".join(lines), inst)


# ---------------------------------------------------------------------------
# Lectura del webhook
# ---------------------------------------------------------------------------

def extract_phone(key):
    """Devuelve el destinatario al que hay que responder, o "" si el JID no es
    de un chat individual.

    WhatsApp ya no siempre manda el número en 'remoteJid': desde que existen los
    nombres de usuario (@miusuario), los contactos que ocultan su teléfono
    llegan con un identificador interno terminado en '@lid' (p.ej.
    '91573131989148@lid'). Si se usa ese valor como número, Evolution responde
    HTTP 400 y el usuario nunca recibe la respuesta. El bot de pacientes todavía
    no tenía esta corrección (estaba anotada como TODO) y la hereda al fusionar.

    En la mayoría de los casos el teléfono verdadero sí viaja en el mensaje, en
    alguno de los campos de _LID_PHONE_FIELDS. Cuando no viaja en ninguno se
    responde al propio '@lid': puede fallar, pero callarse falla siempre.

    También se descartan grupos ('@g.us') y estados ('status@broadcast'), que no
    son números y producirían el mismo 400.
    """
    jid = key.get("remoteJid") or ""

    if jid.endswith("@lid"):
        for field in _LID_PHONE_FIELDS:
            pn = (key.get(field) or "").split("@")[0]
            if pn.isdigit():
                return pn
        print(f"ADVERTENCIA: JID @lid sin teléfono asociado, se responderá al propio lid -- key={key}")
        return jid

    if jid.endswith("@g.us") or jid == "status@broadcast":
        return ""

    return jid.split("@")[0]


def read_incoming(msg):
    """(texto, id_seleccionado) de un mensaje entrante.

    Se aceptan las cuatro formas que puede tomar la respuesta del usuario: texto
    escrito, texto citado, tap en un botón y tap en una fila de lista. El bot de
    pacientes sólo leía las dos primeras, así que un paciente que respondiera
    tocando una fila quedaba sin respuesta.
    """
    message_text = (
        msg.get("conversation")
        or msg.get("extendedTextMessage", {}).get("text", "")
        or msg.get("buttonsResponseMessage", {}).get("selectedDisplayText", "")
        or msg.get("listResponseMessage", {}).get("title", "")
    ).strip()

    selected_id = (
        msg.get("buttonsResponseMessage", {}).get("selectedButtonId")
        or msg.get("listResponseMessage", {}).get("singleSelectReply", {}).get("selectedRowId")
    )
    return message_text, selected_id
