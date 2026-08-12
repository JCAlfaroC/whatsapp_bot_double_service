# --- app.py (Bot de doble servicio: citas de pacientes + quirófanos de médicos) ---
"""Un solo número de WhatsApp para los dos servicios.

Antes había dos bots separados, cada uno con su número: ARIE atendía a los
pacientes (agendar, consultar y reprogramar citas) y el bot de quirófanos
atendía a los médicos (reservar sala de operaciones). Los dos resuelven agenda
contra LOLCLI, pero desde extremos distintos del mostrador.

Aquí conviven detrás de un único webhook. Lo que decide a cuál de los dos va
cada mensaje es el *rol* de la conversación, que el usuario elige la primera vez
que escribe y queda guardado en su sesión:

    mensaje ─► webhook ─► dedup ─► lock de la conversación ─► comandos globales
                                                              │
                              ┌───────────────────────────────┤
                              ▼                               ▼
                    rol = "paciente"                  rol = "medico"
                    flows/pacientes.py                flows/quirofanos.py

Despachar por rol -- en vez de fusionar las dos máquinas de estados en una --
es lo que permite que cada flujo conserve sus nombres de estado. Los dos tienen
un AWAITING_MAIN_MENU, un AWAITING_POST_FLOW y un AWAITING_CONFIRMATION que
significan cosas distintas; como nunca se evalúan en el mismo despacho, no
chocan y ninguno de los dos flujos tuvo que reescribirse para fusionarlos.
"""

import locale
import os
import threading
import time
import traceback

from flask import Flask, g, jsonify, request

import config
from core import messaging, sessions
from core.messaging import send_list_message, send_whatsapp_message
from core.utils import normalize_text
from flows import pacientes, quirofanos

# Las fechas que ve el usuario no dependen de esto: format_date_es las arma con
# DAYS_ES/MONTHS_ES para que salgan igual en cualquier servidor. El locale sólo
# afecta a strftime, así que si el sistema no lo tiene instalado se avisa y se
# sigue: no es motivo para no arrancar.
try:
    locale.setlocale(locale.LC_TIME, "es_ES.UTF-8")
except (locale.Error, Exception):
    try:
        locale.setlocale(locale.LC_TIME, "Spanish_Spain.1252")
    except (locale.Error, Exception):
        print("ADVERTENCIA: Locale en español no encontrado.")

app = Flask(__name__)

# Los dos flujos que atiende el número, indexados por el rol que se guarda en la
# sesión. Agregar un tercer servicio es agregar una entrada aquí, una fila al
# menú de roles y un módulo en flows/ con start() y handle().
FLOWS = {
    pacientes.ROLE: pacientes,
    quirofanos.ROLE: quirofanos,
}

# --- Comandos globales ----------------------------------------------------
# Se atienden antes de despachar al flujo, así que funcionan igual en cualquier
# paso de cualquiera de los dos. 'retroceder' NO está aquí a propósito: cada
# flujo lleva su propio historial y los reproduce distinto, así que lo resuelve
# cada uno.
CMD_SALIR = ("salir", "cancelar")
CMD_ASESOR = ("ayuda", "asesor", "hablar con alguien", "hablar con asesor", "hablar con un asesor")
CMD_INICIO = ("inicio", "cambiar", "cambiar rol", "cambiar servicio", "menu principal", "empezar de nuevo")
CMD_VOLVER_AL_BOT = ("bot", "volver", "asistente")

# Los títulos van sin emoji y por debajo de 24 caracteres: es el máximo que
# admite el título de una fila en las listas interactivas de WhatsApp, y lo que
# sobra no se recorta, hace que la API rechace el mensaje entero.
ROLE_ROWS = [
    {
        "id": "role_paciente",
        "title": "Servicios para Pacientes",
        "description": "Agendar, consultar o reprogramar una cita",
    },
    {
        "id": "role_medico",
        "title": "Servicios para Médicos",
        "description": "Reservar un quirófano",
    },
]

# Lo que se acepta escrito para cada rol, además del número de la fila y del id
# que manda la lista interactiva.
ROLE_KEYWORDS = {
    pacientes.ROLE: ("1", "paciente", "soy paciente", "cita", "citas", "consulta", "atenderme"),
    quirofanos.ROLE: ("2", "medico", "soy medico", "doctor", "doctora", "quirofano",
                      "quirofanos", "sala de operaciones", "reservar quirofano"),
}


def _ask_role(session, phone):
    # Un solo mensaje, no dos: la presentación va dentro del cuerpo de la lista.
    # Antes se mandaba primero un saludo suelto y después el menú, y como cada
    # envío espera SEND_PACING_SECONDS, el usuario veía dos globos separados
    # para lo que es una sola pregunta.
    send_list_message(
        phone,
        "👋 ¡Hola! Soy *ARIE*, el asistente virtual de *LOLIMSA*.\n\n"
        "Por favor indícanos qué deseas hacer:",
        sections=[{"title": "¿Cómo podemos ayudarte?", "rows": ROLE_ROWS}],
        title="Selecciona un servicio",
        button_text="Ver opciones",
        footer="LOLIMSA",
    )
    session["state"] = "AWAITING_ROLE"


def _resolve_role(message_text, selected_id):
    """Rol elegido, o None si la respuesta no corresponde a ninguno."""
    if selected_id and selected_id.startswith("role_"):
        candidato = selected_id[5:]
        if candidato in FLOWS:
            return candidato

    normalizado = normalize_text(message_text)
    if not normalizado:
        return None
    for role, palabras in ROLE_KEYWORDS.items():
        if normalizado in palabras:
            return role
    # Se acepta también la palabra suelta dentro de una frase ("hola, soy un
    # medico"), pero sólo después de haber probado las coincidencias exactas:
    # así "1"/"2" y las respuestas limpias nunca dependen de esta búsqueda.
    for role, palabras in ROLE_KEYWORDS.items():
        if any(len(p) > 3 and p in normalizado for p in palabras):
            return role
    return None


def _trigger_human_handoff(session, phone):
    """Deriva la conversación a una persona y avisa al equipo de soporte.

    Venía del bot de quirófanos y ahora es global: en un número compartido, un
    paciente que escribe 'asesor' tiene que recibir lo mismo que un médico, y no
    caer en la máquina de estados de las citas.
    """
    send_whatsapp_message(
        phone,
        f"👤 Conectando con un asesor...\n\n"
        f"Nuestro equipo ha sido notificado y se pondrá en contacto pronto.\n"
        f"⏰ Horario de atención: {g.support_hours}\n\n"
        f"Escribe *'bot'* en cualquier momento para volver al asistente automático.",
    )
    staff_phone = g.staff_phone
    if staff_phone:
        if session.get("role") == quirofanos.ROLE:
            quien = (
                f"Médico: {session.get('mednam', 'desconocido')} ({session.get('medcod', '')})"
            )
        elif session.get("role") == pacientes.ROLE:
            quien = (
                f"Paciente: {session.get('paciente_nombre', 'desconocido')} "
                f"(doc. {session.get('pacdoc', '—')})"
            )
        else:
            quien = "Usuario sin servicio elegido todavía"
        send_whatsapp_message(
            staff_phone,
            f"🔔 *Solicitud de asesor*\n{quien}\nTeléfono: {phone}",
        )
    session["state"] = "HUMAN_HANDOFF"


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@app.route("/test", methods=["GET"])
def test():
    return "OK", 200


@app.route("/webhook", methods=["POST"])
@app.route("/webhook/<clinic_id>", methods=["POST"])
def webhook_handler(clinic_id=None):
    """Punto de entrada único para los dos servicios.

    Se aceptan las dos formas de URL que usaban los bots originales: /webhook a
    secas (el bot de pacientes, que servía a un solo cliente) y
    /webhook/<clinic_id> (el de quirófanos, multi-clínica). Sin clinic_id se usa
    la clínica por defecto, así que los webhooks ya dados de alta en Evolution
    siguen funcionando sin tocarlos.
    """
    if clinic_id is None:
        clinic_id = config.DEFAULT_CLINIC_ID
    if clinic_id not in config.CLINICS:
        return jsonify({"status": "unknown_clinic"}), 404

    config.bind_request_context(clinic_id)

    data = request.json
    try:
        key = data["data"]["key"]
        # El descarte de los mensajes propios va antes de resolver el número:
        # así no se ensucia el log con advertencias de '@lid' por los envíos que
        # hace el propio bot.
        if key["fromMe"]:
            return jsonify({"status": "ignored_from_me"}), 200
        sender = messaging.extract_phone(key)
        msg = data["data"]["message"]
        msg_id = key.get("id")
    except (KeyError, TypeError):
        # Evolution manda por el mismo webhook eventos que no son mensajes de
        # chat (acuses de entrega, cambios de conexión, ediciones). Se responde
        # 200 y no un error: un código de error haría que Evolution reintentara
        # el mismo evento una y otra vez.
        return jsonify({"status": "ignored_format"}), 200

    if not sender:
        print(f"INFO: mensaje ignorado, remoteJid no es un chat individual ({key.get('remoteJid')})")
        return jsonify({"status": "ignored_not_a_user"}), 200

    if not sessions.mark_processed_if_new(msg_id):
        print(f"INFO: mensaje duplicado ignorado (id={msg_id}, sender={sender})")
        return jsonify({"status": "duplicate_ignored"}), 200

    message_text, selected_id = messaging.read_incoming(msg)
    key_sesion = sessions.session_key(clinic_id, sender)

    # Serializa los mensajes de UN mismo usuario (evita condiciones de carrera
    # sobre su sesión) sin bloquear a los demás, que pueden estar escribiendo al
    # mismo tiempo -- el servidor atiende varias peticiones en paralelo.
    with sessions.get_lock(key_sesion):
        try:
            status = _handle_message(key_sesion, sender, message_text, selected_id)
        except Exception:
            status = _fallo_no_controlado(key_sesion, sender)
    return jsonify({"status": status})


def _fallo_no_controlado(key_sesion, phone):
    """Última red: un error no previsto dentro de un flujo.

    Varias llamadas a LOLCLI de flows/pacientes.py no están envueltas en
    try/except. Sin esta red, una caída de LOLCLI en cualquiera de ellas sube
    como excepción hasta Flask, que responde HTTP 500, y entonces pasan dos
    cosas malas a la vez: el usuario no recibe NADA -- la conversación se corta
    a media frase, después de un "Buscando fechas disponibles..." -- y
    Evolution, al ver un 5xx, reintenta el mismo webhook.

    Se responde 200 a propósito: el mensaje ya se procesó (bien o mal) y
    reintentarlo sólo duplicaría el trabajo. El dedup de sessions atrapa el
    reintento, pero es mejor no provocarlo.

    El traceback completo va al log porque es la única pista que queda de un
    fallo que, por definición, nadie previó.
    """
    traceback.print_exc()
    print(f"ERROR no controlado ({key_sesion}): ver traceback arriba")
    send_whatsapp_message(
        phone,
        "😔 Tuvimos un problema técnico procesando tu mensaje.\n\n"
        "Escribe *'inicio'* para empezar de nuevo o *'asesor'* si prefieres "
        "que te atienda una persona.",
    )
    return "unhandled_error"


def _handle_message(key_sesion, phone, message_text, selected_id):
    session = sessions.get(key_sesion)
    # Se guarda ya: a partir de aquí `session` es EL objeto que vive en el
    # almacén, así que los flujos lo mutan en sitio y un sessions.drop() dentro
    # de un flujo cierra la conversación de verdad, sin que nadie la reviva
    # volviéndola a guardar al final.
    sessions.save(key_sesion, session)

    session["sender"] = phone
    session["clinic_id"] = g.clinic_id
    session["evolution_instance"] = g.evolution_instance
    session["last_interaction_time"] = time.time()
    # Cada mensaje del usuario reinicia la cuenta de avisos de inactividad.
    session["reminders_sent"] = 0

    normalized = normalize_text(message_text)
    role = session.get("role")
    state = session.get("state", "START")
    print(f"[{session['clinic_id']}] {phone} (rol={role or '-'}, estado={state}): "
          f"'{message_text}' | id={selected_id}")

    # --- Comandos globales ---
    if normalized in CMD_SALIR:
        sessions.drop(key_sesion)
        send_whatsapp_message(
            phone,
            "✅ Entendido, hemos cancelado el proceso. Cuando nos necesites, aquí estaremos. "
            "¡Que tengas un excelente día! 🌟",
        )
        return "cancelled"

    if normalized in CMD_ASESOR or selected_id == "menu_asesor":
        _trigger_human_handoff(session, phone)
        return "handoff"

    if state == "HUMAN_HANDOFF":
        if normalized in CMD_VOLVER_AL_BOT:
            send_whatsapp_message(phone, "🤖 De vuelta con el asistente. Escribe *'hola'* para continuar.")
            session["state"] = "START"
        else:
            send_whatsapp_message(
                phone,
                f"Un asesor ha sido notificado y se pondrá en contacto pronto.\n"
                f"📞 También puedes llamarnos durante: {g.support_hours}\n\n"
                f"Escribe *'bot'* para volver al asistente automático.",
            )
        return "handoff_active"

    if normalized in CMD_INICIO:
        # Única forma de cambiar de servicio sin cerrar la sesión: útil cuando un
        # médico que reservó quirófano quiere además agendarse una cita.
        sessions.soft_reset(session)
        session.pop("role", None)
        _ask_role(session, phone)
        return "role_menu"

    # --- Elección de servicio ---
    if state == "AWAITING_ROLE":
        elegido = _resolve_role(message_text, selected_id)
        if not elegido:
            send_whatsapp_message(
                phone,
                "❓ No entendí tu respuesta. Responde *1* si eres *paciente* (citas) "
                "o *2* si eres *médico* (quirófanos). 😊",
            )
            return "role_not_recognized"
        session["role"] = elegido
        print(f"INFO: {phone} eligió el servicio '{elegido}'.")
        FLOWS[elegido].start(session, phone, config.lolcli_headers())
        return "role_selected"

    if not role:
        _ask_role(session, phone)
        return "role_menu"

    # --- Despacho al flujo correspondiente ---
    flow = FLOWS.get(role)
    if flow is None:
        # Rol desconocido: sólo puede venir de una sesión creada por una versión
        # anterior. Se vuelve a preguntar en vez de dejar el mensaje sin
        # respuesta.
        print(f"ADVERTENCIA: rol desconocido en la sesión ({role}); se vuelve a preguntar.")
        sessions.soft_reset(session)
        session.pop("role", None)
        _ask_role(session, phone)
        return "role_menu"

    return flow.handle(
        key_sesion, session, phone, message_text, selected_id, config.lolcli_headers()
    )


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------

def _startup():
    config.load_clinics()

    # Las sedes y los tipos de documento del flujo de pacientes se precargan por
    # clínica para no pedirlas en medio de una conversación. Si LOLCLI no
    # responde ahora, el flujo vuelve a intentarlo cuando el paciente elige una
    # opción del menú.
    for cid, cfg in config.CLINICS.items():
        pacientes.preload_lists(cid, cfg)

    threading.Thread(target=sessions.session_cleanup_task, daemon=True).start()
    threading.Thread(target=pacientes.reminder_task, daemon=True).start()


_startup()


if __name__ == "__main__":
    # waitress es un servidor WSGI de producción (a diferencia de app.run(),
    # pensado sólo para desarrollo). WAITRESS_THREADS controla cuántas
    # peticiones se atienden en simultáneo; cada mensaje saliente duerme
    # SEND_PACING_SECONDS antes de enviarse y varios pasos mandan 2+ mensajes,
    # así que un solo mensaje entrante puede retener un hilo varios segundos.
    # Subir los hilos es seguro porque el tiempo se gasta esperando (sleep/red),
    # no en CPU.
    #
    # Con un solo proceso, las sesiones, los locks y el dedup viven en memoria
    # compartida entre esos hilos; si algún día se corre más de un worker habrá
    # que moverlos a un almacén externo (Redis).
    from waitress import serve

    _port = int(os.getenv("PORT", 5000))
    _threads = int(os.getenv("WAITRESS_THREADS", 50))
    print(f"INFO: Iniciando servidor waitress en http://0.0.0.0:{_port} ({_threads} hilos)")
    serve(app, host="0.0.0.0", port=_port, threads=_threads)
