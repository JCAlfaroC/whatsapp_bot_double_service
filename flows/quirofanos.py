# --- flows/quirofanos.py (reserva de quirófanos para médicos) ---
"""Flujo del médico: se identifica con su documento, elige quirófano, fecha y
bloques de 30 minutos seguidos, ve el precio y confirma la separación.

Portado del bot independiente de quirófanos (whatsapp_bot_quirofano/app.py). Lo
único que cambió al fusionarlo es de dónde vienen las piezas compartidas: la
mensajería, las sesiones y la configuración por clínica ahora son de `core/` y
`config`, y los comandos globales ('salir', 'asesor') los atiende app.py antes
de llegar aquí. La máquina de estados, los payloads de LOLCLI y los textos son
los mismos.
"""

from datetime import date, datetime, timedelta

from flask import g

import config
from core import lolcli, sessions
from core.messaging import send_list_message, send_whatsapp_message
from core.utils import (
    format_date_es,
    format_duration_es,
    normalize_text,
    resolve_selection,
)

ROLE = "medico"

# Endpoints según "Documentos APIS QUirofanos_V2.docx". Los marcados como
# verificados responden 200 en producción.
LOLCLI_ENDPOINTS = {
    "validar_medico": "ValidarMedicoQuirofanoWsp",           # 2.1 — verificado
    "listar_quirofanos": "ListarQuirofanosWsp",              # 2.2 — verificado
    # 2.3 — verificado. El nombre no está en el documento y no sigue el orden
    # del título ("Listar Turnos Disponibles"): el objeto va en medio, así que
    # 'ListarTurnosDisponiblesWsp' y variantes devolvían 404.
    "listar_turnos": "ListarTurnosQuirofanoDisponiblesWsp",  # 2.3 — verificado
    "registrar_separacion": "RegistrarSeparacionQuirofanoWsp",  # 2.4 — verificado
    "calcular_precio": "CalcularPrecioQuirofanoWsp",         # 2.5 — verificado
    # 2.6 — verificado, pero SIN el sufijo 'Wsp': el documento lo nombra
    # 'ListarSeparacionesPorMedicoWsp' y ese nombre devuelve 404. El que existe
    # en el servidor es 'ListarSeparacionesPorMedico'.
    "listar_separaciones": "ListarSeparacionesPorMedico",    # 2.6 — verificado
}

# Tipos de documento de identidad aceptados por ValidarMedicoQuirofanoWsp. Los
# códigos fueron verificados uno a uno contra el API (05-09 y 11+ responden
# "CODIGO TIPO DOCUMENTO DE IDENTIDAD NO EXISTE O NO HABILITADO"); las
# descripciones son las de uso habitual y el cliente debe confirmarlas, ya que
# LOLCLI no expone un endpoint que liste el catálogo.
TIPOS_DOCUMENTO = [
    ("01", "DNI"),
    ("02", "Carné de extranjería"),
    ("03", "Pasaporte"),
    ("04", "Partida de nacimiento"),
    ("10", "Carné de identidad"),
    ("00", "Otro documento"),
]

# Formato con el que se le mandan las marcas de tiempo a LOLCLI.
#
# La 'Z' del final NO significa que la hora sea UTC: es la única forma de que el
# servidor guarde la hora tal cual se le manda. El API corre sobre Node y
# convierte la cadena a Date; sin zona horaria la interpreta como hora local de
# Lima (UTC-5) y la graba desplazada +5 (se mandaba 09:00 y quedaba 14:00, que
# es lo que veía el médico en 'Mis reservas' y en el sistema). Comprobado contra
# la base de pruebas:
#     '2026-12-30T09:00:00'        -> guardado 14:00  (+5)
#     '2026-12-28T15:00:00-05:00'  -> guardado 20:00  (+5)
#     '2026-12-28T11:00:00Z'       -> guardado 11:00  (correcto)
FMT_HORA_LOLCLI = "%Y-%m-%dT%H:%M:%SZ"

# Formato de respuesta que se le promete al médico en la pantalla de horarios.
# Se muestra al pedir el horario y se repite tal cual en cada rechazo, para que
# la regla que ve sea siempre la misma. Es exactamente lo que acepta
# _parse_posiciones: si se cambia uno hay que cambiar el otro, o el bot estaría
# prometiendo un formato que después rechaza.
FORMATO_HORAS = (
    "⚠️ *Responde SÓLO con números de un mismo tramo.*\n\n"
    "• Un horario  →  *3*\n"
    "• Varios  →  *3,4,5*\n"
    "• Un rango  →  *3-5*\n\n"
    "❌ No escribas la hora (*08:00*) ni palabras.\n"
    "❌ No cruces una línea de _ocupado_."
)

FOOTER = "LOLIMSA Quirófanos"


def _call(endpoint_key, payload, headers, timeout=8):
    return lolcli.call("quirofanos", LOLCLI_ENDPOINTS[endpoint_key], payload, headers, timeout)


# ---------------------------------------------------------------------------
# Utilidades de horarios
# ---------------------------------------------------------------------------

def _hora_label(slot):
    """16 -> '08:00', 17 -> '08:30' (slot = hora*2 + 1 si es ':30').

    Acepta 48 para representar el fin de una reserva que termina a medianoche.
    """
    hora, resto = divmod(slot, 2)
    return f"{hora:02d}:{30 if resto else 0:02d}"


def _parse_hora_token(token):
    """'8', '08', '08:00', '8h', '8:30', '8h30' -> el slot de 30' (0-47) en el
    que empieza esa hora. None si no es una hora en punto o y media válida
    (00:00 a 23:30): LOLCLI separa la agenda en bloques de 30 minutos, así que
    cualquier otro minuto no corresponde a un turno real.
    """
    token = str(token).strip().lower().replace("hrs", "").replace("h", ":")
    partes = token.split(":")
    hora_txt = partes[0].strip()
    minuto_txt = partes[1].strip() if len(partes) > 1 and partes[1].strip() else "0"
    if not hora_txt.isdigit() or not minuto_txt.isdigit():
        return None
    hora, minuto = int(hora_txt), int(minuto_txt)
    if not (0 <= hora <= 23) or minuto not in (0, 30):
        return None
    return hora * 2 + (1 if minuto == 30 else 0)


def _parse_posiciones(text, total):
    """Posiciones (1-based) que pide el médico sobre la lista numerada.

    Acepta los tres formatos que anuncia FORMATO_HORAS y nada más: un número
    suelto ('3'), una lista por comas ('3,4,5') o un rango con guion ('3-5'). Se
    tolera el espacio alrededor de las comas ('3, 4, 5') porque el teclado del
    teléfono lo agrega solo, pero no como separador ('3 4 5').

    Devuelve [] si el texto no cumple, si algún número se sale de 1..total o si
    el rango va al revés. El rechazo es en bloque, nunca parcial: la lista de
    horarios cambia en cada consulta, así que aceptar '3,99' a medias reservaría
    un horario que el médico no pidió.
    """
    limpio = str(text).strip()
    if not limpio or total <= 0:
        return []

    def entero(token):
        # isascii() además de isdigit() porque isdigit() acepta cosas como '²' o
        # los dígitos índico-arábigos, y con esos int() revienta: una excepción
        # aquí sería un 500 en el webhook, o sea el bot mudo.
        token = token.strip()
        return int(token) if token.isascii() and token.isdigit() else None

    if "-" in limpio:
        partes = limpio.split("-")
        if len(partes) != 2:
            return []
        ini, fin = entero(partes[0]), entero(partes[1])
        if ini is None or fin is None or ini > fin:
            return []
        # Los extremos se validan ANTES de armar la lista: con '1-99999999999'
        # el range() se comería la memoria del proceso antes de que nadie
        # pudiera rechazarlo.
        if not (1 <= ini <= total and 1 <= fin <= total):
            return []
        posiciones = list(range(ini, fin + 1))
    else:
        posiciones = []
        for token in limpio.split(","):
            numero = entero(token)
            if numero is None:
                return []
            posiciones.append(numero)
        posiciones = sorted(set(posiciones))

    if any(not 1 <= p <= total for p in posiciones):
        return []
    return posiciones


def _turno_disponible(turno):
    """True si el bloque de 30' está realmente libre.

    'disponible' no llega como el "S"/"N" del documento: el API real devuelve un
    entero (1 = libre). Se aceptan las dos formas porque el documento y el
    servidor no coinciden y no se sabe cuál cambiará.

    Además se descarta todo bloque que traiga una intervención asociada (invnum,
    sepcon o intcod1 con contenido) aunque la bandera diga que está libre. Es a
    propósito más estricto que la bandera sola: ante la duda es preferible
    ocultar un horario libre que ofrecer uno que ya tiene cirugía y provocar un
    cruce al grabar.
    """
    marca = turno.get("disponible")
    if isinstance(marca, str):
        libre = marca.strip().upper() in ("S", "SI", "SÍ", "1", "TRUE")
    else:
        libre = bool(marca)
    if not libre:
        return False
    if turno.get("invnum"):
        return False
    return not (str(turno.get("sepcon") or "").strip()
                or str(turno.get("intcod1") or "").strip())


def _tramos_continuos(slots):
    """Parte los bloques libres en tramos seguidos, sin huecos.

    Recibe los slots ordenados y devuelve [(pos_ini, pos_fin, slot_ini,
    slot_fin), ...] con posiciones 1-based sobre la lista numerada que ve el
    médico. Cada corte entre tramos es un turno ya ocupado, que no se lista: por
    eso dos números seguidos en pantalla pueden no ser dos horarios seguidos, y
    hay que poder señalarlo.
    """
    if not slots:
        return []
    tramos = []
    ini = 0
    for i in range(1, len(slots) + 1):
        if i == len(slots) or slots[i] - slots[i - 1] != 1:
            tramos.append((ini + 1, i, slots[ini], slots[i - 1]))
            ini = i
    return tramos


def _lista_es(items):
    """['a'] -> 'a'; ['a','b'] -> 'a y b'; ['a','b','c'] -> 'a, b y c'."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " y " + items[-1]


def _fmt_hora_iso(valor):
    """'2026-08-04T13:00:00.000Z' -> '13:00'.

    La cadena se corta a propósito en vez de parsearla como UTC: LOLCLI devuelve
    la hora local con una 'Z' de más, así que convertir la zona horaria restaría
    5 horas y mostraría un horario equivocado.
    """
    texto = str(valor or "")
    return texto[11:16] if len(texto) >= 16 else ""


def _fmt_fecha_iso(valor):
    texto = str(valor or "")
    try:
        return format_date_es(datetime.strptime(texto[:10], "%Y-%m-%d").date())
    except ValueError:
        return texto[:10]


def next_business_days(n=14):
    """Las próximas n fechas que se le ofrecen al médico.

    Empiezan mañana: el día de hoy no se ofrece, porque a esta altura del día la
    agenda ya está en curso. Se excluye sólo el domingo, así que el sábado sí
    aparece en la lista.
    """
    days = []
    current = date.today() + timedelta(days=1)
    while len(days) < n:
        if current.weekday() < 6:  # lunes a sábado
            days.append(current)
        current += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# Entrada del flujo
# ---------------------------------------------------------------------------

def start(session, phone, lolcli_headers):
    """Arranca el flujo del médico, ya con el rol elegido en app.py."""
    sessions.soft_reset(session)
    session["history"] = ["START"]
    send_whatsapp_message(
        phone,
        "👨‍⚕️ ¡Bienvenido/a al sistema de reservas de quirófanos LOLIMSA!",
    )
    _ask_tipo_documento(session, phone)


def show_main_menu(phone, session, instance=None):
    send_list_message(
        phone,
        "¿Qué deseas hacer?",
        sections=[{
            "title": "Opciones",
            "rows": [
                {"id": "menu_nueva",     "title": "🗓️ Nueva reserva",       "description": "Reservar un quirófano"},
                {"id": "menu_consultar", "title": "📋 Mis reservas",         "description": "Ver tus reservas programadas"},
                {"id": "menu_cancelar",  "title": "❌ Cancelar reserva",      "description": "Próximamente disponible"},
                {"id": "menu_asesor",    "title": "👤 Hablar con un asesor",  "description": "Conectar con personal de soporte"},
            ],
        }],
        instance=instance,
        title="Menú principal",
        button_text="Ver opciones",
        footer=FOOTER,
    )
    session["state"] = "AWAITING_MAIN_MENU"


# ---------------------------------------------------------------------------
# Máquina de estados
# ---------------------------------------------------------------------------

def handle(session_key, session, phone, message_text, selected_id, lolcli_headers):
    """Atiende un mensaje del médico. Devuelve una etiqueta de estado para el log.

    Cada estado que pueda quedar guardado en la sesión tiene que tener su rama
    aquí y su equivalente en _replay_state (que es el que lo vuelve a preguntar
    al 'retroceder'). Un estado sin rama no responde nada y deja al médico
    esperando, así que al agregar un paso hay que tocar los dos.
    """
    normalized = normalize_text(message_text)
    state = session.get("state", "START")

    if normalized == "retroceder" and state not in ["START", "AWAITING_TIDCOD"]:
        history = session.get("history", [])
        if len(history) > 1:
            # 'history' guarda los pasos ya respondidos, no el actual.
            # Retroceder es volver a preguntar el último respondido, así que se
            # saca de la lista y se reproduce ese mismo.
            prev = history.pop()
            session["state"] = prev
            _replay_state(prev, session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "🔄 Ya estás en el primer paso. Escribe *'salir'* para cancelar.")
        return "reverted"

    if state in ("START", "AWAITING_ROLE"):
        # Sesión que llegó aquí sin pasar por start() (p.ej. rol recuperado de
        # una sesión vieja): se arranca el flujo desde el principio.
        start(session, phone, lolcli_headers)

    elif state == "AWAITING_TIDCOD":
        tidcod = _resolve_tidcod(message_text, selected_id)
        if not tidcod:
            send_whatsapp_message(phone, "⚠️ Elige tu tipo de documento de la lista.")
        else:
            existe, err = _tidcod_existe(tidcod, lolcli_headers)
            if err:
                send_whatsapp_message(phone, f"❌ {err}")
            elif not existe:
                send_whatsapp_message(
                    phone,
                    f"❌ El tipo de documento *{tidcod}* no existe o no está habilitado.",
                )
                _ask_tipo_documento(session, phone)
            else:
                session["tidcod"] = tidcod
                session["tidnam"] = dict(TIPOS_DOCUMENTO).get(tidcod, tidcod)
                session.setdefault("history", []).append("AWAITING_TIDCOD")
                _ask_meddoc(session, phone)

    elif state == "AWAITING_MEDDOC":
        meddoc = message_text.strip()
        if not meddoc:
            send_whatsapp_message(phone, "⚠️ Por favor, ingresa tu número de documento.")
        else:
            resp_data, err = _call(
                "validar_medico",
                {"tidcod": session.get("tidcod", ""), "meddoc": meddoc},
                lolcli_headers,
            )
            if err:
                # El documento manda imprimir el 'message' del API tal cual.
                send_whatsapp_message(phone, f"❌ {err}\n\nVerifica tu número e inténtalo de nuevo.")
            else:
                medicos = resp_data.get("medico", [])
                if isinstance(medicos, dict):
                    medicos = [medicos]
                medicos = [m for m in medicos if m.get("valido", "S") == "S"]
                if medicos:
                    medico = medicos[0]
                    session["meddoc"] = meddoc
                    session["medcod"] = medico.get("medcod", "")
                    session["mednam"] = medico.get("mednam", "")
                    session["regesp"] = medico.get("regesp", "")
                    session.setdefault("history", []).append("AWAITING_MEDDOC")
                    send_whatsapp_message(phone, f"✅ ¡Hola, {session['mednam']}!")
                    show_main_menu(phone, session)
                else:
                    send_whatsapp_message(
                        phone,
                        "❌ No encontramos un médico registrado con ese documento. "
                        "Verifica el número e inténtalo de nuevo.",
                    )

    elif state == "AWAITING_MAIN_MENU":
        choice = selected_id or normalized
        if choice in ["menu_nueva", "1", "nueva reserva", "nueva", "reservar"]:
            _start_booking_flow(session, phone, lolcli_headers)
        elif choice in ["menu_consultar", "2", "mis reservas", "consultar"]:
            _show_mis_reservas(session, phone, lolcli_headers)
        elif choice in ["menu_cancelar", "3", "cancelar reserva", "anular"]:
            # LOLCLI todavía no expone un endpoint de anulación (no aparece en
            # ninguna de las dos versiones del documento), así que se deriva a
            # soporte en vez de prometer algo que el API no puede hacer.
            send_whatsapp_message(
                phone,
                "🚧 La cancelación desde el chat estará disponible próximamente.\n"
                "Por ahora, para anular una reserva escribe *'asesor'* y te ayudamos.",
            )
            send_whatsapp_message(phone, "Escribe *'continuar'* para volver al menú.")
            session["state"] = "AWAITING_POST_FLOW"
        else:
            send_whatsapp_message(phone, "❓ Elige una opción del menú. 😊")

    elif state == "AWAITING_QUIROFANO":
        selected = resolve_selection(message_text, selected_id, session)
        if selected:
            session.setdefault("history", []).append("AWAITING_QUIROFANO")
            session["quicod"] = selected["quicod"]
            session["quidel"] = selected["quidel"]
            session["quidec"] = selected["quidec"]
            session["prisal_hora"] = selected["prisal_hora"]
            _ask_date(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "❓ No reconocí ese quirófano. Elige uno de la lista.")

    elif state == "AWAITING_DATE":
        selected = resolve_selection(message_text, selected_id, session)
        if selected:
            session.setdefault("history", []).append("AWAITING_DATE")
            session["fecha_api"] = selected["fecha_api"]
            session["fecha_user"] = selected["fecha_user"]
            _ask_horas(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(phone, "❓ No reconocí esa fecha. Elige una de la lista.")

    elif state == "AWAITING_HORAS":
        _handle_horas(session, phone, message_text, selected_id, lolcli_headers)

    elif state == "AWAITING_CONFIRMATION":
        # El resumen ofrece '1' y '2'. Se siguen aceptando las palabras y los
        # ids de botón por si el médico contesta con el texto de siempre o si el
        # servidor llega a soportar botones interactivos.
        choice = selected_id or normalized
        if choice in ["conf_si", "si", "sí", "confirmar", "confirm", "1"] or "confirmar" in choice:
            _confirm_booking(session, phone, lolcli_headers)
        elif choice in ["conf_no", "no", "retroceder", "2"] or "retroceder" in choice:
            send_whatsapp_message(
                phone,
                "↩️ Escribe *'retroceder'* para corregir un paso o *'salir'* para cancelar.",
            )
        else:
            send_whatsapp_message(
                phone,
                "❓ Responde *1* para confirmar la reserva o *2* para retroceder.",
            )

    elif state == "AWAITING_POST_FLOW":
        if normalized in ["continuar", "continue", "hola", "menu", "menú"]:
            # Se conservan los datos del médico ya validado para no hacerle
            # repetir su documento en cada reserva. soft_reset lee antes de
            # limpiar: 'session' es el mismo objeto que está en user_sessions,
            # así que un clear() a secas vaciaría también lo que se quiere
            # preservar (medcod volvía vacío y la reserva se grababa sin médico).
            sessions.soft_reset(session, keep=("medcod", "mednam"))
            show_main_menu(phone, session)
        else:
            send_whatsapp_message(
                phone,
                "Escribe *'continuar'* para volver al menú o *'salir'* para cerrar la sesión. 😊",
            )

    else:
        # Estado desconocido (sesión vieja tras un despliegue, p.ej.): se vuelve
        # al menú en vez de dejar al médico sin respuesta.
        print(f"ADVERTENCIA: estado no contemplado en el flujo de quirófanos: {state}")
        show_main_menu(phone, session)

    return "processed"


# ---------------------------------------------------------------------------
# Autenticación: tipo de documento (tidcod) + número de documento (meddoc)
# ---------------------------------------------------------------------------

def _ask_tipo_documento(session, phone):
    rows = [
        {"id": f"tid_{code}", "title": label, "description": f"Código {code}"}
        for code, label in TIPOS_DOCUMENTO
    ]
    send_list_message(
        phone,
        "Para identificarte, elige tu *tipo de documento*:",
        sections=[{"title": "Tipo de documento", "rows": rows}],
        title="Identificación del médico",
        button_text="Ver tipos",
        footer=FOOTER,
    )
    session["state"] = "AWAITING_TIDCOD"


def _ask_meddoc(session, phone):
    send_whatsapp_message(
        phone,
        f"Ahora ingresa tu *número de documento* ({session.get('tidnam', '')}):",
    )
    session["state"] = "AWAITING_MEDDOC"


def _resolve_tidcod(message_text, selected_id):
    """Obtiene el tidcod de la selección de lista o de lo que el médico escribió.

    Acepta el código con o sin cero a la izquierda ("1" -> "01") y el nombre del
    documento ("dni" -> "01"), porque no todos los teléfonos renderizan la lista
    interactiva y el médico puede terminar escribiendo la respuesta.
    """
    if selected_id and selected_id.startswith("tid_"):
        return selected_id[4:]

    texto = message_text.strip()
    if not texto:
        return ""
    if texto.isdigit():
        return texto.zfill(2)

    objetivo = normalize_text(texto)
    for code, label in TIPOS_DOCUMENTO:
        if normalize_text(label) == objetivo:
            return code
    return texto


def _tidcod_existe(tidcod, lolcli_headers):
    """(existe, error) para un tipo de documento.

    LOLCLI no publica un endpoint que liste el catálogo, pero
    ValidarMedicoQuirofanoWsp sí distingue los dos casos: ante un tidcod
    inhabilitado responde "... NO EXISTE O NO HABILITADO", y ante uno válido con
    un documento inexistente responde "MEDICO NO SE ENCUENTRA REGISTRADO". Para
    los códigos ya verificados se evita la llamada; cualquier otro se consulta
    con un documento centinela.
    """
    if tidcod in dict(TIPOS_DOCUMENTO):
        return True, None

    resp_data, _ = _call("validar_medico", {"tidcod": tidcod, "meddoc": "0"}, lolcli_headers)
    if resp_data is None:
        return False, "No pudimos validar tu tipo de documento en este momento. Intenta de nuevo en unos minutos."
    return "NO EXISTE O NO HABILITADO" not in (resp_data.get("message") or "").upper(), None


# ---------------------------------------------------------------------------
# Reserva
# ---------------------------------------------------------------------------

def _start_booking_flow(session, phone, lolcli_headers):
    resp_data, err = _call("listar_quirofanos", {"xxsiscod": g.default_siscod}, lolcli_headers)
    if err:
        send_whatsapp_message(phone, f"❌ {err}")
        return

    quirofanos = resp_data.get("quirofanos", [])
    if not quirofanos:
        send_whatsapp_message(phone, "😔 No hay quirófanos disponibles en este momento.")
        send_whatsapp_message(phone, "Escribe *'continuar'* para volver al menú.")
        session["state"] = "AWAITING_POST_FLOW"
        return

    rows = []
    formatted = []
    for i, q in enumerate(quirofanos):
        quicod = q.get("quicod", "")
        quidel = q.get("quidel") or f"Quirófano {i+1}"
        quidec = q.get("quidec", "")
        prisal_hora = float(q.get("prisal_hora") or 0)
        row_id = f"qui_{quicod}"
        rows.append({"id": row_id, "title": quidel, "description": f"{quidec} — S/ {prisal_hora:.2f}/hora"})
        formatted.append({
            "id": i + 1,
            "data": {"_id": row_id, "quicod": quicod, "quidel": quidel, "quidec": quidec, "prisal_hora": prisal_hora},
        })

    session["options"] = formatted
    session["state"] = "AWAITING_QUIROFANO"
    send_list_message(
        phone,
        "Selecciona el *quirófano* que deseas reservar:",
        sections=[{"title": "Quirófanos disponibles", "rows": rows}],
        title="Nueva reserva de quirófano",
        button_text="Ver quirófanos",
        footer=FOOTER,
    )


def _ask_date(session, phone, lolcli_headers):
    days = next_business_days(14)
    rows = []
    formatted = []
    for i, d in enumerate(days):
        fecha_api = d.strftime("%Y-%m-%d")
        fecha_user = format_date_es(d)
        row_id = f"date_{fecha_api}"
        rows.append({"id": row_id, "title": fecha_user, "description": ""})
        formatted.append({"id": i + 1, "data": {"_id": row_id, "fecha_api": fecha_api, "fecha_user": fecha_user}})
    session["options"] = formatted
    session["state"] = "AWAITING_DATE"
    send_list_message(
        phone,
        f"Selecciona la *fecha* para *{session['quidel']}*:",
        sections=[{"title": "Fechas disponibles", "rows": rows}],
        title="Nueva reserva de quirófano",
        button_text="Ver fechas",
        footer=FOOTER,
    )


def _ask_horas(session, phone, lolcli_headers):
    """Pide los turnos libres del día y arranca la selección de horas.

    No se pregunta por la duración: el médico va sumando bloques de 30 minutos
    seguidos y la duración sale de cuántos eligió.
    """
    fecha_ini = session["fecha_api"]
    # El rango es inclusivo en los dos extremos: pedir [día, día] devuelve las
    # 48 medias horas de ese día.
    payload = {
        "xxsiscod": g.default_siscod,
        "xxfechaini": fecha_ini,
        "xxfechafin": fecha_ini,
        "xxquicod": session["quicod"],
    }
    resp_data, err = _call("listar_turnos", payload, lolcli_headers)
    if err:
        send_whatsapp_message(phone, f"❌ {err}")
        return

    # Se filtra también por fecha exacta, no sólo por disponibilidad: el rango es
    # inclusivo, así que un xxfechafin distinto devolvería turnos de otro día y
    # duplicaría bloques (p.ej. dos "08:00"), arriesgando que el médico reserve
    # sin querer el día equivocado.
    #
    # LOLCLI trae un registro por cada bloque de 30 minutos (":00" y ":30"), no
    # uno por hora, así que _parse_hora_token conserva el minuto en vez de
    # truncarlo: si sólo se guardara la hora, un bloque libre de media hora
    # bastaría para ofrecer la hora entera como disponible aunque el otro bloque
    # ya tuviera una cirugía asignada, y la reserva chocaría con ella.
    turnos = [
        t for t in resp_data.get("turnos", [])
        if _turno_disponible(t) and str(t.get("fecha", "")).startswith(fecha_ini)
    ]
    horas = sorted({
        h for h in (_parse_hora_token(t.get("hora", "")) for t in turnos) if h is not None
    })
    if not horas:
        send_whatsapp_message(
            phone,
            "😔 No hay horarios disponibles para ese quirófano en esta fecha. "
            "Escribe *'retroceder'* para elegir otra fecha.",
        )
        return

    session["horas_sel"] = []
    # Misma forma de 'options' que las pantallas de quirófano y fecha, para que
    # el número que escribe el médico signifique lo mismo en todo el bot: la
    # posición en la lista que acaba de ver. Como 'options' se arma sólo con los
    # bloques libres, todo lo que el médico pueda elegir está disponible por
    # construcción y no hace falta revalidarlo después.
    session["options"] = [
        {"id": i + 1, "data": {"_id": f"hora_{slot}", "slot": slot, "label": _hora_label(slot)}}
        for i, slot in enumerate(horas)
    ]
    session["state"] = "AWAITING_HORAS"
    _send_horas_numeradas(session, phone)


def _send_horas_numeradas(session, phone):
    """Muestra los bloques libres como una lista numerada de texto plano.

    No se usa send_list_message a propósito: la lista interactiva de WhatsApp
    admite pocas filas y aquí puede haber más de 40 bloques de 30 minutos, así
    que el texto es el único render que siempre entra completo. Con un solo
    camino de render, el número que ve el médico es siempre el que entiende
    _handle_horas.
    """
    opciones = session.get("options", [])
    lineas = []
    anterior = None
    for o in opciones:
        slot = o["data"]["slot"]
        if anterior is not None and slot - anterior != 1:
            # Entre este bloque y el anterior hay turnos ya tomados, que no se
            # listan. Sin marcar el corte, dos números seguidos en pantalla
            # parecen encadenables y no lo son.
            faltantes = list(range(anterior + 1, slot))
            if len(faltantes) <= 2:
                ocupado = _lista_es([_hora_label(s) for s in faltantes])
            else:
                ocupado = f"{_hora_label(faltantes[0])} a {_hora_label(faltantes[-1] + 1)}"
            lineas.append(f"───── ocupado: {ocupado} ─────")
        lineas.append(f"*{o['id']}.* {o['data']['label']}")
        anterior = slot

    send_whatsapp_message(
        phone,
        f"🕐 *Horarios disponibles*\n"
        f"🏥 {session.get('quidel', '')}\n"
        f"🗓️ {session.get('fecha_user', '')}\n\n"
        + "\n".join(lineas)
        + f"\n\n━━━━━━━━━━━━━━━━━━━━\n{FORMATO_HORAS}",
    )


def _mensaje_no_seguidos(opciones, posiciones, slots):
    """Explica el rechazo y ofrece tramos que sí se pueden reservar.

    Rechazar y nada más deja al médico buscando a mano una combinación válida
    sobre una lista de casi 40 líneas, así que además se le dice hasta dónde
    llega el tramo donde empezó y cuáles son los tramos largos del día.
    """
    ocupados = [
        _hora_label(s) for a, b in zip(slots, slots[1:]) for s in range(a + 1, b)
    ]
    partes = [
        "⚠️ Elegiste " + _lista_es([_hora_label(s) for s in slots])
        + " y no son seguidos: " + _lista_es(ocupados)
        + (" ya está ocupado." if len(ocupados) == 1 else " ya están ocupados.")
    ]

    tramos = _tramos_continuos([o["data"]["slot"] for o in opciones])
    primera = posiciones[0]
    for pos_ini, pos_fin, _slot_ini, slot_fin in tramos:
        if pos_ini <= primera <= pos_fin:
            if pos_fin > primera:
                partes.append(
                    f"\nDesde el *{primera}* puedes seguir hasta el *{pos_fin}* "
                    f"({_hora_label(slots[0])} a {_hora_label(slot_fin + 1)})."
                )
            else:
                partes.append(
                    f"\nDesde el *{primera}* el tramo termina ahí mismo "
                    f"({_hora_label(slots[0])} a {_hora_label(slots[0] + 1)})."
                )
            break

    # Los más largos primero, pero se muestran en el orden del día para que sea
    # fácil ubicarlos en la lista.
    largos = sorted(tramos, key=lambda t: t[1] - t[0], reverse=True)[:3]
    if largos:
        partes.append("\n*Tramos seguidos más largos de hoy:*")
        for pos_ini, pos_fin, slot_ini, slot_fin in sorted(largos):
            numeros = f"*{pos_ini}*" if pos_ini == pos_fin else f"*{pos_ini}-{pos_fin}*"
            partes.append(
                f"  {numeros}  →  {_hora_label(slot_ini)} a {_hora_label(slot_fin + 1)}"
                f"  ({format_duration_es((pos_fin - pos_ini + 1) / 2)})"
            )
    return "\n".join(partes)


def _handle_horas(session, phone, message_text, selected_id, lolcli_headers):
    """Procesa una respuesta en la pantalla de selección de horas.

    El médico responde con la POSICIÓN en la lista numerada, igual que en las
    pantallas de quirófano y fecha. Como los horarios ocupados no se muestran, la
    posición y la hora casi nunca coinciden: leer el número como la hora en sí
    ('3' = 03:00) hacía que el médico terminara reservando otro horario.
    """
    opciones = session.get("options", [])

    if selected_id and selected_id.startswith("hora_"):
        # Un tap en la lista interactiva, si el servidor llegara a soportarla,
        # ya trae el bloque elegido; equivale a una sola posición.
        posiciones = [i + 1 for i, o in enumerate(opciones) if o["data"]["_id"] == selected_id]
    else:
        posiciones = _parse_posiciones(message_text, len(opciones))

    if not posiciones:
        send_whatsapp_message(phone, f"❓ No entendí esa respuesta.\n\n{FORMATO_HORAS}")
        _send_horas_numeradas(session, phone)
        return

    slots = [opciones[p - 1]["data"]["slot"] for p in posiciones]
    if any(b - a != 1 for a, b in zip(slots, slots[1:])):
        send_whatsapp_message(phone, _mensaje_no_seguidos(opciones, posiciones, slots))
        _send_horas_numeradas(session, phone)
        return

    session["horas_sel"] = slots
    _cerrar_seleccion_horas(session, phone, lolcli_headers)


def _cerrar_seleccion_horas(session, phone, lolcli_headers):
    sel = session.get("horas_sel", [])
    if not sel:
        send_whatsapp_message(phone, "⚠️ Primero elige al menos un horario.")
        _send_horas_numeradas(session, phone)
        return

    horini_dt = datetime.strptime(f"{session['fecha_api']}T{_hora_label(sel[0])}", "%Y-%m-%dT%H:%M")
    # Cada elemento de 'sel' es un bloque de 30 minutos, no una hora.
    horfin_dt = horini_dt + timedelta(minutes=30 * len(sel))
    session["horini"] = horini_dt.strftime(FMT_HORA_LOLCLI)
    session["horfin"] = horfin_dt.strftime(FMT_HORA_LOLCLI)
    session["hora_user"] = horini_dt.strftime("%H:%M")
    session["hora_fin_user"] = _hora_label(sel[-1] + 1)
    session["duracion_horas"] = len(sel) / 2
    # El paso sólo se da por respondido si se llegó al resumen. Si el cálculo de
    # precio falla, el médico se queda en esta pantalla y cada reintento
    # agregaría una entrada repetida al historial, haciendo que un 'retroceder'
    # posterior se quede dando vueltas en el mismo paso.
    if _calcular_precio_y_continuar(session, phone, lolcli_headers):
        session.setdefault("history", []).append("AWAITING_HORAS")


def _calcular_precio_y_continuar(session, phone, lolcli_headers):
    """Cotiza el horario elegido y muestra el resumen. True si se llegó a él."""
    payload = {
        "xxquicod": session["quicod"],
        "xxfechaini": session["horini"],
        "xxfechafin": session["horfin"],
        "xxprisal_hora": session["prisal_hora"],
    }
    resp_data, err = _call("calcular_precio", payload, lolcli_headers, timeout=10)
    if err:
        send_whatsapp_message(phone, f"❌ {err}")
        return False

    cotizaciones = resp_data.get("cotizacion", [])
    if not cotizaciones:
        send_whatsapp_message(phone, "😔 No pudimos calcular el precio. Intenta de nuevo.")
        return False

    cot = cotizaciones[0]
    session["precio_total"] = float(cot.get("precio_total") or 0)
    session["horas_cobradas"] = float(cot.get("horas") or session["duracion_horas"])

    # No se pregunta por el procedimiento: el nombre del quirófano indica su
    # especialidad y su uso, así que preguntarlo era un paso de más.
    _show_booking_summary(session, phone)
    return True


def _show_booking_summary(session, phone):
    """Resumen de lo elegido y las dos opciones, también numeradas.

    Se responde con un número, igual que en el resto del flujo, en vez de con
    botones: los botones de Evolution fallan en este servidor y al caer a texto
    quedaban dos formas distintas de contestar la misma pregunta.
    """
    precio = session.get("precio_total", 0)
    send_whatsapp_message(
        phone,
        f"📋 *Resumen de la reserva:*\n\n"
        f"👨‍⚕️ *Médico:* {session.get('mednam', '')}\n"
        f"🏥 *Quirófano:* {session.get('quidel', '')}\n"
        f"📍 *Ubicación:* {session.get('quidec', '')}\n"
        f"🗓️ *Fecha:* {session.get('fecha_user', '')}\n"
        f"⏰ *Horario:* {session.get('hora_user', '')} – {session.get('hora_fin_user', '')}\n"
        f"⏱️ *Duración:* {format_duration_es(session.get('duracion_horas', 0))}\n"
        f"💰 *Total:* S/ {precio:.2f}\n\n"
        f"Al confirmar, la reserva queda registrada en el sistema.\n"
        f"_El pago se coordina por separado._\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*1.* ✅ Confirmar reserva\n"
        f"*2.* ↩️ Retroceder\n\n"
        f"Responde *1* o *2*.",
    )
    session["state"] = "AWAITING_CONFIRMATION"


def _build_pago_payload(session):
    """Datos de la reserva que necesitará la pasarela de pagos.

    Se arma desde ya para que el día que lleguen las credenciales sólo haya que
    hacer el POST. Hoy nadie lo consume: sólo se registra en el log.
    """
    return {
        "medcod": session.get("medcod", ""),
        "mednam": session.get("mednam", ""),
        "quicod": session.get("quicod", ""),
        "quidel": session.get("quidel", ""),
        "fecha": session.get("fecha_api", ""),
        "horini": session.get("horini", ""),
        "horfin": session.get("horfin", ""),
        "horas": session.get("duracion_horas", 0),
        "moneda": "PEN",
        "importe": round(float(session.get("precio_total") or 0), 2),
    }


def _iniciar_pago(session, phone):
    """Punto de enganche de la pasarela de pagos de quirófanos.

    Devuelve True si el cobro quedó pendiente (el flujo debe esperar al pago) y
    False si hay que seguir y grabar la reserva sin cobrar.

    Mientras PAGOS_HABILITADOS sea 0 siempre devuelve False: la reserva se graba
    directo en la base y el resumen del chat hace de comprobante. Ante cualquier
    problema de configuración también devuelve False, para que una pasarela mal
    configurada nunca deje al médico sin poder reservar.
    """
    payload = _build_pago_payload(session)

    if not config.PAGOS_HABILITADOS:
        print(f"INFO: pago omitido (PAGOS_HABILITADOS=0) -- payload listo: {payload}")
        return False

    if not config.PAGOS_URL_BASE:
        print("ADVERTENCIA: PAGOS_HABILITADOS=1 pero falta PAGOS_URL_BASE; se continúa sin cobrar.")
        return False

    # TODO: POST a la pasarela con `payload`, enviar el link de pago al médico y
    # pasar la sesión a AWAITING_PAGO. Falta la URL y las credenciales.
    print(f"ADVERTENCIA: integración de pagos pendiente; se continúa sin cobrar -- {payload}")
    return False


def _confirm_booking(session, phone, lolcli_headers):
    # Una separación ya grabada no se vuelve a grabar. El dedup del webhook no
    # cubre este caso, porque dos "confirmar" escritos por el médico son
    # mensajes distintos con id distinto, y LOLCLI aceptaría el segundo como una
    # reserva nueva sobre el mismo horario.
    if session.get("invnum"):
        send_whatsapp_message(
            phone,
            f"ℹ️ Esta reserva ya estaba registrada con el "
            f"*N° de intervención {session['invnum']}*.\n\n"
            f"Escribe *'continuar'* para hacer otra reserva o *'salir'* para cerrar la sesión.",
        )
        session["state"] = "AWAITING_POST_FLOW"
        return

    # El cobro va antes de grabar. Hoy _iniciar_pago siempre devuelve False y se
    # pasa de largo, que es lo acordado para esta etapa.
    if _iniciar_pago(session, phone):
        return

    # 'xxsepdat' es la fecha en que se registra la reserva (ahora), distinta de
    # 'xxhorini'/'xxhorfin', que son las de la intervención y ya vienen armadas
    # desde la selección de horarios.
    payload = {
        "xxsiscod": g.default_siscod,
        "xxquicod": session["quicod"],
        "xxsepdat": datetime.now().strftime(FMT_HORA_LOLCLI),
        "xxhorini": session["horini"],
        "xxhorfin": session["horfin"],
        "xxmedcod": session["medcod"],
    }
    # Timeout más holgado que el resto de llamadas: ésta es la que escribe en la
    # base y valida cruces, así que tarda más, y cortarla antes de tiempo dejaría
    # la duda de si la reserva quedó grabada o no.
    resp_data, err = _call("registrar_separacion", payload, lolcli_headers, timeout=10)
    if err:
        # Un cruce de horarios no es un fallo del que el médico pueda salir
        # reintentando: ese horario ya está tomado y confirmar de nuevo va a
        # fallar igual. Se le devuelve a elegir horario en vez de dejarlo
        # atascado en la pantalla de confirmación.
        #
        # Pasa más de lo que debería porque ListarTurnosQuirofanoDisponiblesWsp
        # devuelve el día entero como libre aunque haya separaciones grabadas,
        # así que el cruce recién se descubre aquí, al grabar.
        cruce = any(p in err.upper() for p in ("CRUCE", "YA PRESENTA UNA RESERVA", "TRASLAP"))
        if cruce:
            send_whatsapp_message(
                phone,
                f"⚠️ El horario *{session.get('hora_user', '')} – "
                f"{session.get('hora_fin_user', '')}* ya está reservado por otra "
                f"intervención, así que no se pudo registrar.\n\n"
                f"Elige otro horario 👇",
            )
            session["horas_sel"] = []
            _ask_horas(session, phone, lolcli_headers)
        else:
            send_whatsapp_message(
                phone, f"❌ No se pudo registrar la reserva: {err}. Intenta de nuevo."
            )
        return

    invnum = resp_data.get("invnum", "—")
    session["invnum"] = invnum
    send_whatsapp_message(
        phone,
        f"✅ *¡Reserva registrada!*\n\n"
        f"🆔 *N° de intervención:* {invnum}\n"
        f"👨‍⚕️ *Médico:* {session.get('mednam', '')}\n"
        f"🏥 *Quirófano:* {session.get('quidel', '')}\n"
        f"🗓️ *Fecha:* {session.get('fecha_user', '')}\n"
        f"⏰ *Horario:* {session.get('hora_user', '')} – {session.get('hora_fin_user', '')}\n"
        f"⏱️ *Duración:* {format_duration_es(session.get('duracion_horas', 0))}\n"
        f"💰 *Total:* S/ {session.get('precio_total', 0):.2f}\n\n"
        f"Puedes verla cuando quieras en *Mis reservas*.\n"
        f"¡Hasta pronto! 🙏",
    )
    send_whatsapp_message(
        phone,
        "Escribe *'continuar'* para hacer otra reserva o *'salir'* para cerrar la sesión.",
    )
    session["state"] = "AWAITING_POST_FLOW"


def _show_mis_reservas(session, phone, lolcli_headers):
    """Sección 2.6: reservas del médico desde hoy en adelante."""
    resp_data, err = _call(
        "listar_separaciones", {"xxmedcod": session.get("medcod", "")}, lolcli_headers
    )

    if err:
        # "No registra reservas" llega como un 400 de negocio, no como un fallo:
        # se imprime el propio mensaje del API, según la sección 3 del documento.
        send_whatsapp_message(phone, f"📭 {err}")
    else:
        separaciones = resp_data.get("separaciones", [])
        # LOLCLI no las devuelve en orden cronológico (una reserva de agosto
        # aparecía después de las de diciembre), y así el médico no puede ver de
        # un vistazo cuál tiene más próxima. 'hora_inicio' trae fecha y hora en
        # formato ISO, que ordena bien como texto.
        separaciones = sorted(separaciones, key=lambda s: str(s.get("hora_inicio") or ""))
        if not separaciones:
            send_whatsapp_message(phone, "📭 No tienes reservas programadas de hoy en adelante.")
        else:
            bloques = [f"📋 *Tus reservas programadas* ({len(separaciones)}):"]
            for s in separaciones:
                # La fecha sale de 'hora_inicio', NO de 'fecha_separacion': ese
                # campo es la fecha en que se registró la reserva, no la de la
                # intervención. Se veía igual en todas (el día en que se
                # grabaron) mientras las cirugías eran de días distintos, así que
                # el médico veía la fecha equivocada en cada reserva.
                bloques.append(
                    f"\n🆔 *N° {s.get('invnum', '—')}*\n"
                    f"🏥 {s.get('quirofano_nombre') or s.get('quicod', '')}\n"
                    f"🗓️ {_fmt_fecha_iso(s.get('hora_inicio') or s.get('fecha_separacion'))}\n"
                    f"⏰ {_fmt_hora_iso(s.get('hora_inicio'))} – {_fmt_hora_iso(s.get('hora_fin'))}"
                )
            send_whatsapp_message(phone, "\n".join(bloques))

    send_whatsapp_message(phone, "Escribe *'continuar'* para volver al menú.")
    session["state"] = "AWAITING_POST_FLOW"


def _replay_state(state, session, phone, lolcli_headers):
    """Vuelve a hacer la pregunta de un paso anterior, al 'retroceder'.

    Se rearman las opciones consultando de nuevo al API en vez de reusar las que
    quedaron en la sesión: entre medio pudo tomarse un horario, y volver atrás
    tiene que mostrar la agenda como está ahora. Si el paso no está contemplado
    se devuelve al menú, para no dejar la conversación sin salida.
    """
    if state == "AWAITING_TIDCOD":
        _ask_tipo_documento(session, phone)
    elif state == "AWAITING_MEDDOC":
        _ask_meddoc(session, phone)
    elif state == "AWAITING_QUIROFANO":
        _start_booking_flow(session, phone, lolcli_headers)
    elif state == "AWAITING_DATE":
        _ask_date(session, phone, lolcli_headers)
    elif state == "AWAITING_HORAS":
        _ask_horas(session, phone, lolcli_headers)
    elif state == "AWAITING_MAIN_MENU":
        show_main_menu(phone, session)
    else:
        send_whatsapp_message(phone, "↩️ Volviendo al menú principal.")
        show_main_menu(phone, session)
