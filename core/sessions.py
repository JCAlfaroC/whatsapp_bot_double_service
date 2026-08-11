# --- core/sessions.py ---
"""Sesiones en memoria, locks por conversación, deduplicación y expiración.

Las conversaciones viven en RAM, indexadas por "<clinic_id>:<telefono>". El
teléfono solo no alcanza como clave porque el bot puede atender más de una
clínica; el rol (paciente / médico) NO forma parte de la clave, porque es un
mismo número de WhatsApp y una misma conversación: el rol se guarda dentro de la
sesión y decide a qué flujo se despacha cada mensaje.
"""

import threading
import time

from core.messaging import send_whatsapp_message

user_sessions = {}

# --- Deduplicación de mensajes entrantes ---------------------------------
# Evolution/WhatsApp puede reintentar la entrega del mismo webhook ante un
# timeout de red. Sin esto, un reintento en el paso de confirmación podría
# registrar la misma reserva de quirófano (o la misma cita) dos veces. El bot de
# pacientes no tenía esta protección y la hereda al fusionar.
_DEDUP_MAXLEN = 5000
_dedup_lock = threading.Lock()
_processed_msg_ids = set()
_processed_msg_ids_order = []

# --- Lock por conversación ------------------------------------------------
_session_locks_meta_lock = threading.Lock()
_session_locks = {}

# --- Tiempos de sesión, en segundos --------------------------------------
# Los dos flujos tenían presupuestos distintos y se conservan tal cual, porque
# responden a usos distintos: el paciente contesta desde el teléfono en el
# momento (3 minutos, con un aviso por minuto), mientras que el médico consulta
# su agenda entre pacientes y necesita más margen (15 minutos, con un aviso a
# los 5). Se eligen según session["role"].
TIEMPOS_POR_ROL = {
    "paciente": {"expiracion": 3 * 60, "aviso_cada": 60},
    "medico": {"expiracion": 15 * 60, "aviso_cada": 5 * 60},
}
# Quien todavía no eligió rol se rige por el presupuesto corto: es una sesión
# que aún no invirtió nada.
TIEMPOS_POR_DEFECTO = TIEMPOS_POR_ROL["paciente"]

# El sondeo es menor que cualquier aviso a propósito: con un presupuesto de sólo
# 3 minutos para el paciente, un sondeo de 60s podría retrasar un aviso o el
# cierre casi un minuto extra (el temporizador es compartido por todas las
# sesiones, no uno por sesión). 20s da buena precisión con costo despreciable,
# porque el cuerpo del bucle descarta al instante las sesiones inactivas.
CLEANUP_POLL_INTERVAL = 20


def session_key(clinic_id, phone):
    return f"{clinic_id}:{phone}"


def get(key):
    return user_sessions.get(key, {"state": "START"})


def save(key, session):
    user_sessions[key] = session


def drop(key):
    user_sessions.pop(key, None)


# Datos que identifican la conversación, no el trámite en curso: sobreviven a
# cualquier reinicio de flujo. Si 'role' se perdiera aquí, el siguiente mensaje
# del usuario se despacharía al flujo equivocado.
_CLAVES_PERSISTENTES = (
    "role",
    "sender",
    "clinic_id",
    "evolution_instance",
    "last_interaction_time",
)


def soft_reset(session, keep=()):
    """Vacía la sesión conservando la identidad de la conversación.

    Reemplaza a los session.clear() que hacían los dos bots al volver al menú
    tras terminar un trámite. Es la misma idea que ya aplicaba el bot de
    quirófanos al preservar a mano medcod/mednam: leer ANTES de limpiar, porque
    'session' es el mismo objeto que está en user_sessions y clear() lo vacía.
    """
    preservado = {k: session[k] for k in (*_CLAVES_PERSISTENTES, *keep) if k in session}
    session.clear()
    session.update(preservado)
    return session


def mark_processed_if_new(msg_id):
    """True si es la primera vez que se ve este id de mensaje de WhatsApp.

    Sin id (payload inesperado) se deja pasar — no hay forma de deduplicar.
    """
    if not msg_id:
        return True
    with _dedup_lock:
        if msg_id in _processed_msg_ids:
            return False
        _processed_msg_ids.add(msg_id)
        _processed_msg_ids_order.append(msg_id)
        if len(_processed_msg_ids_order) > _DEDUP_MAXLEN:
            oldest = _processed_msg_ids_order.pop(0)
            _processed_msg_ids.discard(oldest)
        return True


def get_lock(key):
    """Lock de esta conversación, creándolo la primera vez.

    Serializa los mensajes de UN mismo usuario (evita condiciones de carrera
    sobre su sesión) sin bloquear a los demás. No se borra al cerrarse la
    sesión: un mensaje que llega justo mientras la sesión expira tiene que
    encontrar el mismo lock que el hilo de limpieza, y son unos pocos bytes por
    número atendido.
    """
    with _session_locks_meta_lock:
        lock = _session_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _session_locks[key] = lock
        return lock


def _avisar_tramite_a_medias(session, phone):
    """Deja rastro en el log de lo que quedó a medio grabar al expirar.

    Son los dos casos en que la sesión se cae con algo ya escrito en LOLCLI que
    nadie va a completar: una cita registrada cuyo pago nunca se confirmó, y una
    separación de quirófano ya grabada. Los dos requieren revisión manual.
    """
    if session.get("invnum_cita"):
        print(
            f"ALERTA: Cita registrada sin pago confirmado. "
            f"invnum={session['invnum_cita']}, paciente={session.get('paciente_nombre', '?')}, "
            f"teléfono={phone}. Requiere cancelación manual en LOLCLI."
        )
    if session.get("invnum"):
        print(
            f"INFO: Sesión con reserva de quirófano ya registrada (invnum={session['invnum']}, "
            f"médico={session.get('mednam', '?')}, teléfono={phone}) cerrada por inactividad."
        )


def session_cleanup_task():
    """Hilo de fondo que avisa y cierra las sesiones abandonadas.

    Se recorre una copia de las claves porque los hilos que atienden el webhook
    agregan y quitan sesiones al mismo tiempo, y recorrer el diccionario en vivo
    reventaría el hilo (y con él la limpieza) en cuanto llegara un mensaje.
    """
    while True:
        time.sleep(CLEANUP_POLL_INTERVAL)
        ahora = time.time()
        for key in list(user_sessions.keys()):
            session = user_sessions.get(key)
            if not session or session.get("state") == "START":
                continue
            if "last_interaction_time" not in session:
                continue

            inactivo = ahora - session["last_interaction_time"]
            phone = key.split(":", 1)[1]
            inst = session.get("evolution_instance", "")
            tiempos = TIEMPOS_POR_ROL.get(session.get("role"), TIEMPOS_POR_DEFECTO)

            if inactivo > tiempos["expiracion"]:
                print(f"INFO: Sesión expirada por inactividad ({key}, rol={session.get('role')}).")
                _avisar_tramite_a_medias(session, phone)
                # La sesión se borra ANTES de avisar: send_whatsapp_message
                # duerme y hace red, y hasta que vuelva este mismo hilo no puede
                # atender el resto de sesiones vencidas.
                user_sessions.pop(key, None)
                send_whatsapp_message(
                    phone,
                    "⏰ Tu sesión ha cerrado por inactividad. Cuando quieras continuar, "
                    "sólo escríbenos y estaremos listos para ayudarte. 😊",
                    inst,
                )
                continue

            # Un aviso por cada tramo cumplido: con el presupuesto del paciente
            # (3 min / avisos cada 1 min) son dos avisos antes del cierre; con el
            # del médico (15 min / aviso cada 5 min) son dos también.
            avisos_debidos = int(inactivo // tiempos["aviso_cada"])
            if avisos_debidos > session.get("reminders_sent", 0):
                session["reminders_sent"] = avisos_debidos
                print(f"INFO: Recordatorio de inactividad a {key} (aviso #{avisos_debidos}).")
                send_whatsapp_message(
                    phone,
                    "👋 ¿Sigues ahí? Dejaste tu trámite a medias. Responde para continuar "
                    "o tu sesión se cerrará pronto por inactividad. 🕐",
                    inst,
                )
