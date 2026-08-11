# --- flows/pacientes.py (citas para pacientes — ARIE) ---
"""Flujo del paciente: agendar una cita, consultar las que ya tiene y
reprogramar, cada uno con su cobro cuando corresponde.

Portado del bot ARIE (whatsapp_bot_ariel/app_improved.py). Al fusionarlo cambió
de dónde salen las piezas compartidas -- mensajería, sesiones y configuración
por clínica ahora viven en `core/` y `config` -- y las llamadas a LOLCLI pasaron
de leer LOLCLI_API_URL/LOLCLI_ENTIDAD del módulo a resolverlas por clínica con
`_url()` y `g.lolcli_entidad`. Los estados, los payloads y los textos son los
mismos.

Las llamadas siguen siendo `requests.post` directos y no `core.lolcli.call()`:
cada endpoint de este flujo señala el error a su manera (unos con `status`,
otros con el código HTTP, otros devolviendo una lista vacía) y cada pantalla los
interpreta distinto. Unificarlas cambiaría ese manejo pantalla por pantalla, que
es justo lo que no conviene tocar al fusionar.
"""

import json
import os
import time
from datetime import date, datetime
from urllib.parse import urlparse, urlunparse

import requests
from flask import g

import config
from core import lolcli, sessions
from core.messaging import send_whatsapp_message
from core.utils import (
    format_date_es,
    format_menu,
    normalize_text,
    process_user_choice,
)

ROLE = "paciente"

PRESET_HORARIOS = [
    {"hora": "0800"}, {"hora": "0830"}, {"hora": "0900"}, {"hora": "0930"},
    {"hora": "1000"}, {"hora": "1030"}, {"hora": "1100"}, {"hora": "1130"},
    {"hora": "1400"}, {"hora": "1430"}, {"hora": "1500"}, {"hora": "1530"},
    {"hora": "1600"}, {"hora": "1700"},
]

# Tipos de cita que ARIE no considera "citas" para efectos de
# consulta/reprogramación.
INFORME_SERVICE_KEYWORDS = (
    "informe médico general",
    "informe médico integral",
    "informe de evaluación",
)

CONSULT_TOP_N = 10

# Reglas de reprogramación confirmadas en el SP
# SEL_API_LISTAR_CITAS_PACIENTES_WSP: mínimo 24h de anticipación, ventana de 30
# días, y una sola reprogramación por cita.
RESCHEDULE_POLICY_NOTE = (
    "_ℹ️ Recuerda: cada cita solo puede reprogramarse una vez, con un mínimo de "
    "24 horas de anticipación y dentro de los próximos 30 días._"
)

# Derecho de reprogramación de citas: oricod/tarcod son fijos para este concepto
# de cobro (confirmados por LOLIMSA junto con el payload de
# GenerarLinkPagoOrdenPrefactura), no varían por cita ni por paciente.
RESCHEDULE_FEE_ORICOD = "PM"
RESCHEDULE_FEE_TARCOD = "001001"

# Servicio forzado para el flujo "Agendar reevaluación médica".
#
# NOTA: los estados *_FOR_REEVAL y las ramas con session["flow"] == "reeval"
# vienen del bot original y hoy son inalcanzables: el menú principal ofrece 3
# opciones y ninguna pone flow="reeval". Se portan tal cual porque la función
# está pendiente de confirmación con LOLIMSA (ver los TODO de abajo); para
# habilitarla basta con agregar la opción al menú y poner session["flow"].
REEVAL_SERVICE_NAME = "medicina fisica y rehabilitacion"

# TODO: confirmar con LOLIMSA si estos son sub-servicios (ListaServiciosWsp) o
# tarifas (ListaTarifarioWsp) dentro de "Medicina Física y Rehabilitación" -- se
# asume que son entradas de tarifario, ya que así aparecían nombradas en el
# comprobante de requerimientos (p.ej. "Aplicación TB - 1").
REEVAL_EXCLUDED_TARIFA_KEYWORDS = (
    "neuropediatria",
    "psiquiatria infantil",
    "informe",
    "valoracion espastica",
    "aplicacion tb",
    "post aplicacion tb",
)

REMINDERS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reminders.json"
)


def _url(endpoint):
    return lolcli.url("pacientes", endpoint)


# ---------------------------------------------------------------------------
# Listas globales por clínica (sedes y tipos de documento)
# ---------------------------------------------------------------------------
# El bot original las cargaba una sola vez al importar el módulo, en variables
# globales, porque servía a un solo cliente. Ahora se cachean por clínica: el
# webhook puede atender varias y cada una tiene sus propias sedes.

_listas_por_clinica = {}


def _listas(clinic_id=None):
    cid = clinic_id if clinic_id is not None else g.clinic_id
    return _listas_por_clinica.setdefault(cid, {"sedes": [], "documentos": []})


def preload_lists(clinic_id, clinic):
    """Carga sedes y tipos de documento de una clínica.

    Recibe la configuración explícita (y no la lee de `g`) para poder llamarse
    también desde el arranque, fuera de toda petición.
    """
    destino = _listas(clinic_id)
    url_base = config.clinic_lolcli_url(clinic, "pacientes")
    if not url_base:
        return destino

    headers = {
        "Authorization": f"Basic {clinic.get('lolcli_token', '')}",
        "Content-Type": "application/json",
    }
    try:
        response_sedes = requests.post(
            f"{url_base}/ListaEstablecimientos",
            json={"entidad": clinic.get("lolcli_entidad", "")},
            headers=headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        if response_sedes.ok:
            destino["sedes"] = response_sedes.json().get("establecimientos", [])
            print(f"INFO [{clinic_id}]: Se han cargado {len(destino['sedes'])} sedes.")

        response_docs = requests.post(
            f"{url_base}/ListaTipoDocumentoElolcli",
            json={},
            headers=headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        if response_docs.ok:
            destino["documentos"] = [
                doc
                for doc in response_docs.json().get("tipoDocumentos", [])
                if doc["tidcod"] in ["01", "02", "03", "04"]
            ]
            print(
                f"INFO [{clinic_id}]: Se han cargado {len(destino['documentos'])} "
                f"tipos de documento."
            )
    except requests.exceptions.RequestException as e:
        print(f"ERROR [{clinic_id}]: Fallo en la conexión con la API al pre-cargar listas: {e}")
    return destino


# ---------------------------------------------------------------------------
# Recordatorios de cita
# ---------------------------------------------------------------------------

def save_reminder(session):
    fecha = session.get("fecha_api", "")
    hora = session.get("hora_api", "")
    try:
        apt_datetime = datetime.strptime(fecha + hora, "%Y%m%d%H%M").strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        apt_datetime = "Fecha no disponible"

    reminder = {
        "phone": session.get("sender"),
        "email": session.get("email"),
        "patient_name": session.get("paciente_nombre", "Paciente"),
        "doctor_name": session.get("mednam", ""),
        "specialty": session.get("sernam", ""),
        "sede": session.get("establishment_name", ""),
        "appointment_datetime": apt_datetime,
        # La instancia de Evolution se guarda con el recordatorio porque quien lo
        # envía es un hilo de fondo, sin petición en curso y por lo tanto sin
        # forma de saber a qué clínica pertenecía la cita.
        "evolution_instance": session.get("evolution_instance", ""),
        "reminded": False,
    }

    reminders = []
    if os.path.exists(REMINDERS_FILE):
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                reminders = json.load(f)
        except Exception:
            reminders = []

    reminders.append(reminder)
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2)
    print(f"INFO: Recordatorio guardado para {reminder['patient_name']} -- {apt_datetime}")


def reminder_task():
    """Hilo de fondo: avisa al paciente el día antes de su cita."""
    while True:
        time.sleep(3600)  # una revisión por hora
        now = datetime.now()

        if not os.path.exists(REMINDERS_FILE):
            continue
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                reminders = json.load(f)
        except Exception:
            continue

        updated = False
        for reminder in reminders:
            if reminder.get("reminded"):
                continue
            try:
                apt_dt = datetime.strptime(reminder["appointment_datetime"], "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                continue

            hours_until = (apt_dt - now).total_seconds() / 3600

            if 23 <= hours_until <= 25:
                whatsapp_msg = (
                    f"🔔 *Recordatorio de cita -- ARIE*\n\n"
                    f"Hola {reminder['patient_name']}, le recordamos su cita de mañana:\n\n"
                    f"👨‍⚕️ *Médico:* {reminder['doctor_name']}\n"
                    f"🩺 *Especialidad:* {reminder['specialty']}\n"
                    f"🏥 *Sede:* {reminder['sede']}\n"
                    f"⏰ *Hora:* {reminder['appointment_datetime']}\n"
                    f"Por favor, preséntese 15 minutos antes. 😊"
                )
                send_whatsapp_message(
                    reminder["phone"], whatsapp_msg, reminder.get("evolution_instance", "")
                )
                reminder["reminded"] = True
                updated = True

        if updated:
            with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(reminders, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Helpers de pantalla
# ---------------------------------------------------------------------------

def _is_informe_type(cita):
    servicio = normalize_text(cita.get("servicio", ""))
    return any(normalize_text(kw) in servicio for kw in INFORME_SERVICE_KEYWORDS)


def format_appointments_list(citas, title, mode="consult"):
    # ListarCitasPacientesWsp a veces devuelve [{}] (un objeto vacío) en vez de
    # [] cuando no hay citas -- se descartan las entradas sin datos reales.
    citas = [c for c in citas if c]

    if mode == "consult":
        # TODO: confirmar con LOLIMSA si el filtro de "Informes" y el tope de
        # Top-10/mes-actual ya se aplican del lado del servidor en
        # ListarCitasPacientesWsp (tipo=C); por ahora se filtra/limita aquí.
        citas = [c for c in citas if not _is_informe_type(c)]
        citas = citas[:CONSULT_TOP_N]

    msg = f"{title}\n\n"
    formatted = []
    today = date.today()
    for i, cita in enumerate(citas, 1):
        fecha_raw = cita.get("fecha", "")
        try:
            # ListarCitasPacientesWsp ha devuelto dos formatos distintos:
            # "2026-07-07 09:00:00.000" y, más recientemente, ISO 8601
            # "2026-07-07T09:00:00.000Z". Se normaliza antes de parsear; la "Z"
            # se descarta (no se convierte de UTC) porque la hora ya viene en
            # hora local de la clínica, igual que el formato anterior.
            fecha_normalizada = fecha_raw.replace("T", " ").rstrip("Z")
            date_obj = datetime.strptime(fecha_normalizada[:19], "%Y-%m-%d %H:%M:%S")
            fecha = format_date_es(date_obj)
            hora = date_obj.strftime("%H:%M")
        except (ValueError, TypeError):
            date_obj = None
            fecha = fecha_raw or "Fecha no disponible"
            hora = "Hora no disponible"

        es_hoy = bool(date_obj) and date_obj.date() == today
        fecha_hora_line = f"🗓️ {fecha} — ⏰ {hora}"
        if es_hoy:
            fecha_hora_line = f"*{fecha_hora_line} (HOY)*"

        if mode == "consult":
            modalidad = {"P": "Presencial", "V": "Teleconsulta"}.get(cita.get("cittip", ""), "")
            estado_pago = cita.get("pagado", "")

            msg += (
                f"*{i}.* {fecha_hora_line}\n"
                f"   🏥 {cita.get('establecimiento', '')}\n"
                f"   🩺 {cita.get('servicio', '')}\n"
                f"   👨‍⚕️ {cita.get('medico', '')}\n"
                f"   🏷️ {cita.get('tardes', '')}"
                + (f" — {modalidad}" if modalidad else "")
                + "\n"
                + (f"   💳 Estado de pago: {estado_pago}\n" if estado_pago else "")
                + "\n"
            )
        else:
            msg += (
                f"*{i}.* {fecha_hora_line}\n"
                f"   🩺 {cita.get('servicio', '')}\n"
                f"   👨‍⚕️ {cita.get('medico', '')}\n\n"
            )
        formatted.append({"id": i, "data": cita})
    return msg, formatted


def show_main_menu(phone_to_reply, session):
    menu = (
        "¿Qué deseas hacer hoy?\n\n"
        "*1.* 📅 Agendar una nueva cita\n"
        "*2.* 🔍 Consultar mis citas\n"
        "*3.* 🔄 Reprogramar una cita\n\n"
        "_Escribe el número de tu elección._"
    )
    session["state"] = "AWAITING_MAIN_MENU"
    send_whatsapp_message(phone_to_reply, menu)


def start(session, phone_to_reply, lolcli_headers):
    """Arranca el flujo del paciente, ya con el rol elegido en app.py."""
    sessions.soft_reset(session)
    session["history"] = ["START"]
    send_whatsapp_message(
        phone_to_reply,
        "😊 ¡Perfecto! Soy tu asistente virtual de ARIE y estoy aquí para ayudarte con tus citas.",
    )
    show_main_menu(phone_to_reply, session)


def replay_state_prompt(state, session, phone_to_reply, headers):
    """Vuelve a hacer la pregunta de un paso anterior, al 'retroceder'."""
    print(f"Retrocediendo al estado: {state}")
    if state == "AWAITING_ESTABLISHMENT":
        response = requests.post(
            _url("ListaEstablecimientos"),
            json={"entidad": g.lolcli_entidad},
            headers=headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        options = response.json().get("establecimientos", [])
        reply, formatted_options = format_menu(
            "Claro, volvamos a elegir. ¿En cuál de nuestras sedes te gustaría atenderte?",
            options,
            "siscod",
            "sisent",
        )
        session["options"] = formatted_options
        send_whatsapp_message(phone_to_reply, reply)
    elif state == "AWAITING_SPECIALTY":
        response = requests.post(
            _url("ListaServiciosWsp"),
            json={"siscod": session["siscod"]},
            headers=headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        options = response.json().get("servicios", [])
        reply, formatted_options = format_menu(
            "No hay problema. Dime de nuevo, ¿para qué especialidad necesitas la cita?",
            options,
            "sercod",
            "serdes",
        )
        session["options"] = formatted_options
        send_whatsapp_message(phone_to_reply, reply)
    elif state == "AWAITING_DOCTOR":
        response_medicos = requests.post(
            _url("ListaMedicos"),
            json={"siscod": session["siscod"], "sercod": session["sercod"]},
            headers=headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        medicos = response_medicos.json().get("medicos", [])
        reply, formatted_options = format_menu(
            "Ok, volvamos a la selección de doctor. ¿Con quién deseas atenderte?",
            medicos,
            "medcod",
            "mednam",
        )
        session["options"] = formatted_options
        send_whatsapp_message(phone_to_reply, reply)
    elif state == "AWAITING_AVAILABLE_DATE":
        today_str = date.today().strftime("%Y%m%d")
        response = requests.post(
            _url("ListaCuposDisponibles"),
            json={
                "siscod": session["siscod"],
                "sercod": session["sercod"],
                "medcod": session["medcod"],
                "fecha": today_str,
            },
            headers=headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        fechas_disponibles = response.json().get("cupos", [])
        reply, formatted_options = format_menu(
            "Entendido. Elige nuevamente una de las fechas disponibles:",
            fechas_disponibles,
            "citdat",
            "citdat",
        )
        session["options"] = formatted_options
        send_whatsapp_message(phone_to_reply, reply)
    elif state in ("AWAITING_DOC_TYPE", "AWAITING_DOC_TYPE_FOR_REEVAL"):
        _pedir_tipo_documento(
            session,
            phone_to_reply,
            "Sin problema, empecemos de nuevo. Selecciona tu tipo de documento:",
            state,
        )
    elif state in ("AWAITING_DOC_NUMBER", "AWAITING_DOC_NUMBER_FOR_REEVAL"):
        send_whatsapp_message(
            phone_to_reply,
            f"Claro. Ingresa nuevamente tu número de {session.get('tiddes', 'documento')}.",
        )
    elif state == "AWAITING_TIME":
        _mostrar_horarios(
            session,
            phone_to_reply,
            headers,
            fecha_api=session.get("fecha_api", ""),
            invnum=0,
            titulo="⏰ Volvamos a los horarios. Elige el de tu preferencia:",
            cierre="_Elige la hora (solo el número)._",
            siguiente_estado="AWAITING_TIME",
        )
    elif state == "AWAITING_APPOINTMENT_TYPE":
        send_whatsapp_message(
            phone_to_reply,
            "Volvamos a la modalidad. ¿La cita será *Presencial* (1) o *Virtual* (2)?",
        )
    elif state == "AWAITING_TARIFF":
        # Las tarifas siguen en session["options"] porque show_final_summary no
        # las pisa, así que se re-arma el menú sin volver a pedírselas a LOLCLI
        # ni recalcular el precio de cada una. Si por lo que sea no están, se
        # retrocede un paso más (la modalidad), que es lo que las regenera.
        tarifas = [o["data"] for o in session.get("options", [])]
        if tarifas:
            reply, formatted_options = format_menu(
                "Volvamos a las tarifas. Elige una de nuevo:",
                tarifas,
                "tarcod",
                "tardes",
                key_price="precio",
            )
            session["options"] = formatted_options
            send_whatsapp_message(phone_to_reply, reply)
        else:
            session["state"] = "AWAITING_APPOINTMENT_TYPE"
            send_whatsapp_message(
                phone_to_reply,
                "Volvamos un paso más. ¿La cita será *Presencial* (1) o *Virtual* (2)?",
            )
    else:
        send_whatsapp_message(
            phone_to_reply,
            "↩️ Te hemos llevado al inicio. Cuando estés listo/a, escríbenos *hola* y empezamos de nuevo. 😊",
        )
        sessions.soft_reset(session)
        session["state"] = "START"


def fetch_tarifa_price(session, tarcod, headers):
    payload = {
        "siscod": int(session["siscod"]),
        "sercod": session["sercod"],
        "medcod": session["medcod"],
        "cittip": session["cittip"],
        "pachis": session["pachis"],
        "tarcod": tarcod,
    }
    try:
        response = requests.post(
            _url("ItemCostoServicio"),
            json=payload,
            headers=headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        costos = response.json().get("costos", [])
        if costos:
            return float(costos[0]["totnet"]), costos[0].get("plnnum")
    except (requests.exceptions.RequestException, ValueError, KeyError) as e:
        print(f"ERROR fetch_tarifa_price (tarcod={tarcod}): {e}")
    return None, None


def _aplicar_host_de_pruebas(payment_url):
    """Antepone el subdominio de pruebas al enlace de pago que da LOLCLI.

    FASE DE PRUEBAS: LOLCLI todavía devuelve el dominio de producción de la
    pasarela en "payment_link", así que para que el cobro se procese en el
    entorno de pruebas de Niubiz hay que anteponerle el prefijo de pruebas. Al
    pasar a producción real basta con poner PAGOS_QA_PREFIX="" en el .env; ya no
    hace falta tocar el código.
    """
    prefijo = config.PAGOS_QA_PREFIX
    if not prefijo:
        return payment_url
    parsed_url = urlparse(payment_url)
    if not parsed_url.netloc.startswith(prefijo):
        parsed_url = parsed_url._replace(netloc=f"{prefijo}{parsed_url.netloc}")
        payment_url = urlunparse(parsed_url)
    return payment_url


def generate_payment_link_and_send(session, phone_to_reply, headers):
    try:
        # El número de orden/cita tiene que ir como entero para la API.
        invnum_val = session.get("invnum_cita")
        invnum = int(invnum_val) if invnum_val else 0

        payload_pago = {
            "cliente": "arie_pruebas",
            "invnum": invnum,
            "paydat": datetime.now().strftime("%d-%m-%Y %H:%M:%S.000"),
        }

        print(f"INFO: Generando link de pago con payload: {payload_pago}")
        response_link = requests.post(
            _url("GenerarLinkPagoCita"),
            json=payload_pago,
            headers=headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        response_link.raise_for_status()
        data_link = response_link.json()

        if data_link.get("status") == "success" and data_link.get("payment_link"):
            payment_url = _aplicar_host_de_pruebas(data_link["payment_link"])
            try:
                session["payment_token"] = payment_url.split("/")[-1]
            except Exception:
                session["payment_token"] = None
            costo_total = session.get("costo_total", 0.0)
            send_whatsapp_message(
                phone_to_reply,
                f"Para completar tu reserva, realiza el pago de *S/ {costo_total:.2f}* en el siguiente "
                f"enlace:\n\n{payment_url}\n\nCuando hayas completado el pago en la página, regresa aquí "
                f"y escríbeme *'listo'* para confirmar tu cita. ✅",
            )
            session["state"] = "AWAITING_PAYMENT_CONFIRMATION"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "Tuvimos un problema al generar tu enlace de pago. Por favor, intenta de nuevo en un momento.",
            )
            session["state"] = "AWAITING_CONFIRMATION"

    except requests.exceptions.HTTPError as err:
        print(
            f"ERROR HTTP en generate_payment_link_and_send: "
            f"{err.response.status_code} - {err.response.text}"
        )
        send_whatsapp_message(
            phone_to_reply,
            "😔 Tuvimos un problema de comunicación al preparar tu pago. Por favor, intenta de nuevo en unos momentos. 🙏",
        )
    except Exception as e:
        print(f"ERROR en generate_payment_link_and_send: {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Ocurrió un error al preparar tu pago. Por favor, intenta de nuevo o contáctanos. 🙏",
        )


def generate_reschedule_payment_link_and_send(session, phone_to_reply, headers):
    try:
        payload_pago = {
            "oricod": RESCHEDULE_FEE_ORICOD,
            "tarcod": RESCHEDULE_FEE_TARCOD,
            "pachis": session.get("pachis"),
            "cliente": "arie_pruebas",
        }
        print(f"INFO: Generando link de pago de reprogramación con payload: {payload_pago}")

        response_link = requests.post(
            _url("GenerarLinkPagoOrdenPrefactura"),
            json=payload_pago,
            headers=headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        response_link.raise_for_status()
        data_link = response_link.json()

        if data_link.get("status") == "success" and data_link.get("payment_link"):
            payment_url = _aplicar_host_de_pruebas(data_link["payment_link"])
            session["reschedule_payment_token"] = data_link.get("token")
            monto = data_link.get("monto", 15)
            send_whatsapp_message(
                phone_to_reply,
                f"Para confirmar tu reprogramación, realiza el pago del derecho de reprogramación de citas de "
                f"*S/ {monto:.2f}* en el siguiente enlace:\n\n{payment_url}\n\nCuando hayas completado el pago "
                "en la página, regresa aquí y escríbeme *'listo'* para confirmar tu reprogramación. ✅",
            )
            session["state"] = "AWAITING_RESCHEDULE_PAYMENT_CONFIRMATION"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "Tuvimos un problema al generar tu enlace de pago. Por favor, intenta de nuevo en un momento.",
            )
            session["state"] = "AWAITING_RESCHEDULE_CONFIRMATION"

    except requests.exceptions.HTTPError as err:
        print(
            f"ERROR HTTP en generate_reschedule_payment_link_and_send: "
            f"{err.response.status_code} - {err.response.text}"
        )
        send_whatsapp_message(
            phone_to_reply,
            "😔 Tuvimos un problema de comunicación al preparar tu pago. Por favor, intenta de nuevo en unos momentos. 🙏",
        )
        session["state"] = "AWAITING_RESCHEDULE_CONFIRMATION"
    except Exception as e:
        print(f"ERROR en generate_reschedule_payment_link_and_send: {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Ocurrió un error al preparar tu pago. Por favor, intenta de nuevo o contáctanos. 🙏",
        )
        session["state"] = "AWAITING_RESCHEDULE_CONFIRMATION"


def continue_appointment_flow(session, phone_to_reply, lolcli_headers):
    send_whatsapp_message(
        phone_to_reply,
        "✅ ¡Excelente! Ya tenemos tus datos. Ahora continuemos con los detalles de tu cita. 😊",
    )
    try:
        response_est = requests.post(
            _url("ListaEstablecimientos"),
            json={"entidad": g.lolcli_entidad},
            headers=lolcli_headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        establecimientos = response_est.json().get("establecimientos", [])
    except requests.exceptions.RequestException as e:
        print(f"ERROR en continue_appointment_flow (ListaEstablecimientos): {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Tuvimos un problema técnico al buscar las sedes disponibles. Por favor, intenta de nuevo en unos minutos. 🙏",
        )
        return
    reply, opts = format_menu(
        "Para empezar, ¿en cuál de nuestras sedes te gustaría atenderte?",
        establecimientos,
        "siscod",
        "sisent",
    )
    session["options"] = opts
    session["state"] = "AWAITING_ESTABLISHMENT"
    send_whatsapp_message(phone_to_reply, reply)


def present_specialty_or_force_reeval(session, phone_to_reply, lolcli_headers, servicios, intro_text):
    if session.get("flow") == "reeval":
        match = next(
            (s for s in servicios if REEVAL_SERVICE_NAME in normalize_text(s.get("serdes", ""))),
            None,
        )
        if not match:
            send_whatsapp_message(
                phone_to_reply,
                f"😔 *{session['establishment_name']}* no ofrece reevaluación médica de Medicina Física y "
                "Rehabilitación. Escribe *retroceder* para elegir otra sede o *salir* para cancelar.",
            )
            # NO hacer pop aquí: el estado (AWAITING_ESTABLISHMENT) no avanzó,
            # así que la entrada de historial ya fue registrada por quien llamó a
            # esta función. Si también se hace pop aquí, "retroceder" (que hace
            # su propio pop) termina saltando dos pasos en vez de uno, y puede
            # vaciar el historial y reiniciar la sesión entera.
            return
        session["sercod"] = match["sercod"]
        session["sernam"] = match["serdes"]
        fetch_and_prompt_doctors(session, phone_to_reply, lolcli_headers)
        return

    reply, formatted_options = format_menu(intro_text, servicios, "sercod", "serdes")
    session["options"] = formatted_options
    session["state"] = "AWAITING_SPECIALTY"
    send_whatsapp_message(phone_to_reply, reply)


def fetch_and_prompt_doctors(session, phone_to_reply, lolcli_headers):
    try:
        response_medicos = requests.post(
            _url("ListaMedicos"),
            json={"siscod": session["siscod"], "sercod": session["sercod"]},
            headers=lolcli_headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        medicos = response_medicos.json().get("medicos", [])
    except requests.exceptions.RequestException as e:
        print(f"ERROR en fetch_and_prompt_doctors (ListaMedicos): {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Tuvimos un problema técnico al buscar los médicos disponibles. Por favor, intenta de nuevo en unos minutos. 🙏",
        )
        return
    if not medicos:
        send_whatsapp_message(
            phone_to_reply,
            f"Lo sentimos, no hay doctores para *{session['sernam']}* en esta sede.",
        )
        send_whatsapp_message(
            phone_to_reply,
            "Puedes escribir el número de otra especialidad de la lista anterior, "
            "*retroceder* para elegir otra sede, o *salir* para cancelar.",
        )
        # NO hacer pop aquí -- ver comentario equivalente en
        # present_specialty_or_force_reeval. El estado sigue siendo
        # AWAITING_SPECIALTY (no avanzó), así que session["options"] todavía
        # tiene la lista de especialidades: el usuario puede simplemente escribir
        # otro número sin necesidad de "retroceder".
    else:
        reply, formatted_options = format_menu(
            "Estos son los doctores con espacio:", medicos, "medcod", "mednam"
        )
        session["options"] = formatted_options
        session["state"] = "AWAITING_DOCTOR"
        send_whatsapp_message(phone_to_reply, reply)


def show_final_summary(session, phone_to_reply):
    tarifa_line = session["tardes"]
    if session.get("tarifa_precio") is not None:
        tarifa_line += f" – S/ {session['tarifa_precio']:.2f}"

    send_whatsapp_message(
        phone_to_reply,
        f"¡Casi listo! ✨ Por favor, revisa que todo esté correcto:\n\n"
        f"👤 *Paciente:* {session.get('paciente_nombre')}\n"
        f"🏥 *Sede:* {session['establishment_name']}\n"
        f"🩺 *Especialidad:* {session['sernam']}\n"
        f"👨‍⚕️ *Médico:* {session['mednam']}\n"
        f"🗓️ *Fecha:* {session['fecha_user']}\n"
        f"⏰ *Hora:* {session['hora_user']}\n"
        f"🏷️ *Tarifa:* {tarifa_line}\n\n"
        f"Si todo está bien, escribe *'Sí'* para confirmar tu cita.",
    )
    session["state"] = "AWAITING_CONFIRMATION"


def _pedir_tipo_documento(session, phone_to_reply, titulo, siguiente_estado):
    """Muestra el menú de tipos de documento y deja la sesión esperándolo."""
    reply, opts = format_menu(titulo, _listas()["documentos"], "tidcod", "tiddes")
    session["options"] = opts
    session["state"] = siguiente_estado
    send_whatsapp_message(phone_to_reply, reply)


def _dni_invalido(tidcod, doc_number):
    """El DNI peruano son exactamente 8 dígitos; el resto de documentos no se
    valida de forma local porque su formato varía."""
    return tidcod == "01" and (not doc_number.isdigit() or len(doc_number) != 8)


def _validar_paciente(tidcod, doc_number, lolcli_headers):
    response = requests.post(
        _url("ValidarPacienteWsp"),
        json={"tidcod": tidcod, "pacdoc": doc_number},
        headers=lolcli_headers,
        timeout=config.LOLCLI_TIMEOUT,
    )
    return response.json().get("paciente", [])


def _resolver_paciente_y_continuar(session, session_key, phone_to_reply, doc_number, lolcli_headers):
    """Valida al paciente y arranca la elección de sede. Devuelve la etiqueta de
    estado para el log.

    Es el tramo común de "agendar cita" y "agendar reevaluación": los dos piden
    tipo y número de documento y siguen igual a partir de aquí.
    """
    pacientes = _validar_paciente(session.get("tidcod"), doc_number, lolcli_headers)

    if pacientes and pacientes[0].get("valido") == "S":
        paciente = pacientes[0]
        session.update({"pachis": paciente["pachis"], "paciente_nombre": paciente["pacpmn"]})
        send_whatsapp_message(phone_to_reply, f"¡Hola de nuevo, {paciente['pacpmn']}!")
        continue_appointment_flow(session, phone_to_reply, lolcli_headers)
        return "processed"

    if pacientes:
        # Paciente registrado (ValidarPacienteWsp lo encontró), pero
        # "valido": "N" -> no tiene citas atendidas en los últimos 10 días, por
        # lo que no puede agendar por este medio. Al estar registrado, se le
        # saluda por su nombre en la respuesta.
        send_whatsapp_message(
            phone_to_reply,
            f"🔍 Hola {pacientes[0]['pacpmn']}, encontramos tu registro, pero no cuentas con citas "
            "atendidas en los últimos 10 días, por lo que no es posible agendar una nueva cita "
            "por este medio. Por favor, acércate personalmente a tu sede. 📞",
        )
    else:
        # ARIE no permite crear pacientes nuevos por WhatsApp: si no está
        # registrado en la clínica, se le pide acercarse presencialmente.
        send_whatsapp_message(
            phone_to_reply,
            "🔍 No encontramos ningún paciente registrado con ese documento. "
            "Para agendar una cita, por favor acércate personalmente a tu sede para registrarte. 📞",
        )
    sessions.drop(session_key)
    return "patient_not_eligible"


def _mostrar_horarios(session, phone_to_reply, lolcli_headers, fecha_api, invnum,
                      titulo, cierre, siguiente_estado):
    """Pantalla de horarios de un día, común a agendar y a reprogramar.

    'invnum' llega sin convertir a entero a propósito: al reprogramar sale de
    'secuencia' (una fila de ListarCitasPacientesWsp) y puede venir vacío, así
    que la conversión tiene que quedar DENTRO del try. Si se hace al llamar, un
    'secuencia' ausente revienta con TypeError, el webhook responde 500 y
    Evolution reintenta el mismo mensaje en vez de que el paciente vea los
    horarios de respaldo.
    """
    try:
        horarios_raw = requests.post(
            _url("ListaCuposDetalle"),
            json={
                "siscod": int(session["siscod"]),
                "sercod": session["sercod"],
                "medcod": session["medcod"],
                "fecha": fecha_api,
                "invnum": int(invnum),
            },
            headers=lolcli_headers,
            timeout=config.LOLCLI_TIMEOUT,
        ).json().get("horarios", [])
        horarios = [h for h in horarios_raw if h.get("estado") == "D"] or PRESET_HORARIOS
    except Exception as e:
        print(f"ERROR ListaCuposDetalle: {e}")
        horarios = PRESET_HORARIOS

    reply = f"{titulo}\n\n"
    opts = []
    for i, h in enumerate(horarios, 1):
        hora_fmt = datetime.strptime(h["hora"], "%H%M").strftime("%H:%M")
        reply += f"*{i}.* {hora_fmt}\n"
        opts.append({"id": i, "data": h})
    reply += f"\n{cierre}"
    session["options"] = opts
    session["state"] = siguiente_estado
    send_whatsapp_message(phone_to_reply, reply)


def _fechas_unicas(all_cupos):
    """Un cupo por fecha: ListaCuposDisponibles devuelve una fila por horario y
    la pantalla de fechas sólo debe mostrar el día una vez."""
    seen = set()
    unique_fechas = []
    for c in all_cupos:
        d = c.get("citdat", "")
        if d and d not in seen:
            seen.add(d)
            unique_fechas.append(c)
    return unique_fechas


def _citas_del_paciente(doc_number, tipo, lolcli_headers):
    """(citas, hubo_error_de_servidor) de ListarCitasPacientesWsp.

    tipo="C" son todas las citas del paciente y tipo="R" sólo las que el SP
    considera reprogramables.
    """
    response = requests.post(
        _url("ListarCitasPacientesWsp"),
        json={"nro_documento": doc_number, "tipo": tipo},
        headers=lolcli_headers,
        timeout=config.LOLCLI_TIMEOUT,
    )
    data = response.json()
    print(f"INFO: ListarCitasPacientesWsp (tipo={tipo}, doc={doc_number}) respuesta: {data}")
    server_error = response.status_code >= 500 or data.get("code") == 500
    citas = [c for c in data.get("citas", []) if c] if not server_error else []
    return citas, server_error


def _mensaje_post_flujo(phone_to_reply, session):
    send_whatsapp_message(
        phone_to_reply,
        "Gracias. Escribe *'continuar'* si deseas realizar otra consulta o *'salir'* para terminar la sesión. 😊",
    )
    session["state"] = "AWAITING_POST_FLOW"


# ---------------------------------------------------------------------------
# Máquina de estados
# ---------------------------------------------------------------------------

def handle(session_key, session, phone_to_reply, message_text, selected_id, lolcli_headers):
    """Atiende un mensaje del paciente. Devuelve una etiqueta de estado para el log."""
    state = session.get("state")

    if message_text.lower() == "retroceder" and state != "START":
        history = session.get("history", [])
        if len(history) > 1:
            # 'history' guarda el paso que el usuario YA contestó, así que su
            # último elemento ES el paso al que hay que volver: pop() lo
            # devuelve y lo quita de una vez, para que un segundo "retroceder"
            # siga uno más atrás. Antes se hacía pop() y después se leía
            # history[-1], lo que saltaba un paso: desde AWAITING_SPECIALTY caía
            # en AWAITING_DOC_NUMBER y, al no estar contemplado en
            # replay_state_prompt, borraba la sesión entera del paciente.
            previous_state = history.pop()
            session["state"] = previous_state
            replay_state_prompt(previous_state, session, phone_to_reply, lolcli_headers)
            return "reverted"
        send_whatsapp_message(
            phone_to_reply,
            "🔄 Ya estás en el primer paso, no hay pasos anteriores. Escribe *salir* si deseas cancelar "
            "o continúa con tu selección. 😊",
        )
        return "at_start"

    if state in (None, "START", "AWAITING_ROLE"):
        # Sesión que llegó aquí sin pasar por start() (p.ej. rol recuperado de
        # una sesión vieja): se arranca el flujo desde el principio.
        start(session, phone_to_reply, lolcli_headers)

    elif state == "AWAITING_MAIN_MENU":
        # El error técnico sólo aparece si LOLCLI sigue inalcanzable cuando el
        # usuario elige una opción; si las listas cargaron bien al arrancar, el
        # reintento no hace nada (es una comprobación instantánea).
        if not _listas()["documentos"]:
            preload_lists(g.clinic_id, g.clinic)
        if not _listas()["documentos"]:
            send_whatsapp_message(
                phone_to_reply,
                "😔 Lo sentimos, tenemos dificultades técnicas. Por favor, intenta en unos minutos o llámanos directamente. 🙏",
            )
            return "error_loading_lists"

        choice = message_text.strip().lower()
        if choice in ["1", "agendar", "nueva cita", "nueva"]:
            _pedir_tipo_documento(
                session,
                phone_to_reply,
                "Para empezar, por favor, selecciona tu tipo de documento:",
                "AWAITING_DOC_TYPE",
            )

        elif choice in ["2", "consultar", "mis citas"]:
            _pedir_tipo_documento(
                session,
                phone_to_reply,
                "Para consultar tus citas, selecciona tu tipo de documento:",
                "AWAITING_DOC_TYPE_FOR_CONSULT",
            )

        elif choice in ["3", "reprogramar", "cambiar cita"]:
            # tidcod "01" = D.N.I. (confirmado contra ListaTipoDocumentoElolcli
            # -- "03" es CEDULA DIPLOMATICA, no D.N.I.; ese valor incorrecto
            # causaba que ValidarPacienteWsp no encontrara pacientes registrados
            # con DNI real en el flujo de reprogramación).
            session["tidcod"] = "01"
            session["tiddes"] = "D.N.I."
            session["state"] = "AWAITING_RESCHEDULE_FEE_CONFIRMATION"
            send_whatsapp_message(
                phone_to_reply,
                "🔄 Para reprogramar tu cita, primero ten en cuenta que aplica un derecho de reprogramación de "
                "*S/ 15.00*. ¿Deseas continuar? Responde *Sí* o *No*.",
            )

        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ Por favor, escribe *1*, *2* o *3* para elegir una opción. 😊",
            )

    elif state in ("AWAITING_DOC_TYPE", "AWAITING_DOC_TYPE_FOR_REEVAL"):
        selected_option = process_user_choice(message_text, session.get("options", []), "tiddes")
        if selected_option:
            session["tidcod"] = selected_option["tidcod"]
            session["tiddes"] = selected_option["tiddes"]
            session.setdefault("history", []).append(state)
            send_whatsapp_message(
                phone_to_reply,
                f"Entendido. Ahora, por favor, ingresa tu número de {selected_option['tiddes']}.",
            )
            session["state"] = (
                "AWAITING_DOC_NUMBER" if state == "AWAITING_DOC_TYPE"
                else "AWAITING_DOC_NUMBER_FOR_REEVAL"
            )
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de tu elección de la lista. 🙏",
            )

    elif state in ("AWAITING_DOC_NUMBER", "AWAITING_DOC_NUMBER_FOR_REEVAL"):
        doc_number = message_text.strip()

        if _dni_invalido(session.get("tidcod"), doc_number):
            send_whatsapp_message(
                phone_to_reply,
                "⚠️ El DNI ingresado no es válido. Debe tener exactamente 8 dígitos numéricos. "
                "¿Puedes verificarlo e intentarlo de nuevo? 🙏",
            )
            return "invalid_dni"

        session["pacdoc"] = doc_number
        session.setdefault("history", []).append(state)

        try:
            return _resolver_paciente_y_continuar(
                session, session_key, phone_to_reply, doc_number, lolcli_headers
            )
        except Exception as e:
            send_whatsapp_message(
                phone_to_reply,
                "😔 Tuvimos un inconveniente al verificar tu documento. Por favor, intenta de nuevo. 🙏",
            )
            print(f"Error en {state}: {e}")

    elif state in ("AWAITING_ESTABLISHMENT", "AWAITING_ESTABLISHMENT_CLARIFICATION"):
        selected_option = process_user_choice(message_text, session.get("options", []), "sisent")
        if selected_option:
            session.setdefault("history", []).append("AWAITING_ESTABLISHMENT")
            session["siscod"] = selected_option["siscod"]
            session["establishment_name"] = selected_option["sisent"]
            try:
                response = requests.post(
                    _url("ListaServiciosWsp"),
                    json={"siscod": session["siscod"]},
                    headers=lolcli_headers,
                    timeout=config.LOLCLI_TIMEOUT,
                )
                servicios = response.json().get("servicios", [])
            except requests.exceptions.RequestException as e:
                print(f"ERROR en {state} (ListaServiciosWsp): {e}")
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 Tuvimos un problema técnico al buscar las especialidades disponibles. "
                    "Por favor, intenta de nuevo en unos minutos. 🙏",
                )
                return "server_error"
            intro = (
                f"¡Perfecto! Ahora, para la sede *{session['establishment_name']}*, ¿qué especialidad necesitas?"
                if state == "AWAITING_ESTABLISHMENT_CLARIFICATION"
                else f"Entendido. Ahora, ¿para qué especialidad en *{session['establishment_name']}* necesitas la cita?"
            )
            present_specialty_or_force_reeval(
                session, phone_to_reply, lolcli_headers, servicios, intro
            )
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa sede. Por favor, escribe el número de la sede que prefieres. 🏥",
            )

    elif state == "AWAITING_SPECIALTY":
        selected_option = process_user_choice(message_text, session.get("options", []), "serdes")
        if selected_option:
            session.setdefault("history", []).append("AWAITING_SPECIALTY")
            session["sercod"] = selected_option["sercod"]
            session["sernam"] = selected_option["serdes"]
            fetch_and_prompt_doctors(session, phone_to_reply, lolcli_headers)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa especialidad. Por favor, escribe el número de la especialidad que necesitas. 🩺",
            )

    elif state == "AWAITING_DOCTOR":
        selected_option = process_user_choice(message_text, session.get("options", []), "mednam")
        if selected_option:
            session.setdefault("history", []).append("AWAITING_DOCTOR")
            session["medcod"] = selected_option["medcod"]
            session["mednam"] = selected_option["mednam"]
            send_whatsapp_message(
                phone_to_reply,
                f"Perfecto, con el Dr(a). {session['mednam']}. Veamos sus fechas...",
            )
            response = requests.post(
                _url("ListaCuposDisponibles"),
                json={
                    "siscod": session["siscod"],
                    "sercod": session["sercod"],
                    "medcod": session["medcod"],
                    "fecha": date.today().strftime("%Y%m%d"),
                },
                headers=lolcli_headers,
                timeout=config.LOLCLI_TIMEOUT,
            )
            all_cupos = response.json().get("cupos", [])
            session["all_cupos"] = all_cupos
            reply, formatted_options = format_menu(
                "📅 Estas son sus próximas fechas disponibles:",
                _fechas_unicas(all_cupos),
                "citdat",
                "citdat",
            )
            session["options"] = formatted_options
            session["state"] = "AWAITING_AVAILABLE_DATE"
            send_whatsapp_message(phone_to_reply, reply)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No encontré ese médico. Por favor, escribe el número del médico de tu preferencia. 👨‍⚕️",
            )

    elif state == "AWAITING_AVAILABLE_DATE":
        selected_option = process_user_choice(message_text, session.get("options", []))
        if selected_option:
            session.setdefault("history", []).append("AWAITING_AVAILABLE_DATE")
            session["fecha_api"] = selected_option["citdat"]
            session["fecha_user"] = format_date_es(
                datetime.strptime(selected_option["citdat"], "%Y%m%d")
            )
            send_whatsapp_message(
                phone_to_reply,
                f"Excelente, para el *{session['fecha_user']}*. Viendo las horas libres...",
            )
            _mostrar_horarios(
                session,
                phone_to_reply,
                lolcli_headers,
                fecha_api=session["fecha_api"],
                invnum=0,
                titulo="⏰ Elige el horario de tu preferencia:",
                cierre="_Elige la hora (solo el número). ¡Ya casi terminamos!_",
                siguiente_estado="AWAITING_TIME",
            )
        else:
            send_whatsapp_message(phone_to_reply, "No reconocí esa fecha. Elige una de la lista.")

    elif state == "AWAITING_TIME":
        try:
            selected_option = session["options"][int(message_text) - 1]["data"]
            session.setdefault("history", []).append("AWAITING_TIME")
            session["hora_api"] = selected_option["hora"]
            session["hora_user"] = datetime.strptime(selected_option["hora"], "%H%M").strftime("%H:%M")
            send_whatsapp_message(
                phone_to_reply,
                "¡Anotado! Para finalizar, ¿la cita será *Presencial* (1) o *Virtual* (2)?",
            )
            session["state"] = "AWAITING_APPOINTMENT_TYPE"
        except (ValueError, IndexError):
            send_whatsapp_message(
                phone_to_reply,
                "⏰ Por favor, escribe solo el número del horario que prefieres de la lista.",
            )

    elif state == "AWAITING_APPOINTMENT_TYPE":
        choice = message_text.lower()
        if choice in ["1", "presencial"]:
            session.setdefault("history", []).append("AWAITING_APPOINTMENT_TYPE")
            session["cittip"], session["cittip_name"] = "P", "Presencial"
        elif choice in ["2", "virtual"]:
            session.setdefault("history", []).append("AWAITING_APPOINTMENT_TYPE")
            session["cittip"], session["cittip_name"] = "V", "Virtual"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ Por favor, escribe 1 para Presencial 🏥 o 2 para Virtual 💻.",
            )
            return "processed"

        send_whatsapp_message(phone_to_reply, "Buscando tarifas, un momento... 🔍")
        response = requests.post(
            _url("ListaTarifarioWsp"),
            json={
                "siscod": int(session["siscod"]),
                "sercod": session["sercod"],
                "medcod": session["medcod"],
                "cittip": session["cittip"],
            },
            headers=lolcli_headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        try:
            response.raise_for_status()
            tarifas = response.json().get("tarifas", [])
        except (requests.exceptions.HTTPError, requests.exceptions.JSONDecodeError) as e:
            print(
                f"ERROR: La API de tarifas ({response.url}) falló. "
                f"Status: {response.status_code}, Error: {e}"
            )
            tarifas = []

        if session.get("flow") == "reeval":
            # TODO: confirmar con LOLIMSA el mecanismo real de categoría social
            # -- ListaTarifarioWsp no acepta ningún parámetro de
            # paciente/categoría.
            tarifas = [
                t for t in tarifas
                if not any(kw in normalize_text(t.get("tardes", "")) for kw in REEVAL_EXCLUDED_TARIFA_KEYWORDS)
            ]

        for t in tarifas:
            precio, plnnum = fetch_tarifa_price(session, t.get("tarcod"), lolcli_headers)
            if precio is not None:
                t["precio"] = precio
                t["plnnum"] = plnnum

        if not tarifas:
            # El estado se mantiene en AWAITING_APPOINTMENT_TYPE (no avanzó), así
            # que el usuario puede simplemente escribir 1 o 2 de nuevo para
            # probar la otra modalidad, sin necesidad de "retroceder". NO hacer
            # pop del historial aquí: esta entrada ya la agregó este mismo bloque
            # arriba, y hacer pop de nuevo haría que "retroceder" (que hace su
            # propio pop) salte dos pasos en vez de uno.
            send_whatsapp_message(
                phone_to_reply,
                "😔 No encontramos tarifas para este tipo de consulta. Puedes escribir *1* (Presencial) o "
                "*2* (Virtual) para probar la otra modalidad, *retroceder* para elegir otro horario, o "
                "*salir* para cancelar. 🙏",
            )
        else:
            reply, formatted_options = format_menu(
                "Estas son las tarifas disponibles:", tarifas, "tarcod", "tardes", key_price="precio"
            )
            session["options"] = formatted_options
            session["state"] = "AWAITING_TARIFF"
            send_whatsapp_message(phone_to_reply, reply)

    elif state == "AWAITING_TARIFF":
        selected_option = process_user_choice(message_text, session.get("options", []), "tardes")
        if selected_option:
            session.setdefault("history", []).append("AWAITING_TARIFF")
            session["tarcod"] = selected_option["tarcod"]
            session["tardes"] = selected_option["tardes"]
            session["tarifa_precio"] = selected_option.get("precio")
            session["tarifa_plnnum"] = selected_option.get("plnnum")
            send_whatsapp_message(phone_to_reply, f"Ok, elegiste *'{session['tardes']}'*.")
            show_final_summary(session, phone_to_reply)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa tarifa. Por favor, escribe el número de la tarifa que deseas. 🙏",
            )

    elif state == "AWAITING_CONFIRMATION":
        if message_text.lower() in ["sí", "si"]:
            return _registrar_cita(session, session_key, phone_to_reply, lolcli_headers)
        send_whatsapp_message(
            phone_to_reply,
            "🤔 Sin problema. Escribe *retroceder* si deseas corregir algún dato, o *salir* si prefieres cancelar. 😊",
        )

    elif state == "AWAITING_PAYMENT_CONFIRMATION":
        return _confirmar_pago_cita(session, phone_to_reply, message_text, lolcli_headers)

    # ── CONSULTA DE CITAS ────────────────────────────────────────────────────

    elif state == "AWAITING_DOC_TYPE_FOR_CONSULT":
        selected_option = process_user_choice(message_text, session.get("options", []), "tiddes")
        if selected_option:
            session["tidcod"] = selected_option["tidcod"]
            session["tiddes"] = selected_option["tiddes"]
            send_whatsapp_message(
                phone_to_reply, f"Ingresa tu número de {selected_option['tiddes']}."
            )
            session["state"] = "AWAITING_DOC_NUMBER_FOR_CONSULT"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de tu elección. 🙏",
            )

    elif state == "AWAITING_DOC_NUMBER_FOR_CONSULT":
        doc_number = message_text.strip()
        if _dni_invalido(session.get("tidcod"), doc_number):
            send_whatsapp_message(
                phone_to_reply, "⚠️ El DNI debe tener exactamente 8 dígitos numéricos."
            )
            return "invalid_dni"
        try:
            pacientes = _validar_paciente(session.get("tidcod"), doc_number, lolcli_headers)
            if not pacientes:
                send_whatsapp_message(
                    phone_to_reply,
                    "🔍 No encontramos ningún paciente registrado con ese documento. 🙏",
                )
                sessions.drop(session_key)
                return "patient_not_found"
            paciente = pacientes[0]
            send_whatsapp_message(
                phone_to_reply, f"Un momento, consultando tus citas, {paciente['pacpmn']}... 🔍"
            )
            citas, server_error = _citas_del_paciente(doc_number, "C", lolcli_headers)

            if server_error:
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 Tuvimos un problema técnico al consultar tus citas. Por favor, intenta de nuevo "
                    "en unos minutos o contáctanos directamente. 🙏",
                )
            elif not citas:
                send_whatsapp_message(
                    phone_to_reply, "📋 No tienes citas agendadas en este momento. 😊"
                )
            else:
                msg, _ = format_appointments_list(
                    citas, f"📋 *Tus citas agendadas, {paciente['pacpmn']}:*", mode="consult"
                )
                msg += "_ℹ️ Para reprogramar una cita, selecciona la opción *3* en el menú principal._"
                send_whatsapp_message(phone_to_reply, msg)
            _mensaje_post_flujo(phone_to_reply, session)
            return "consult_done"
        except Exception as e:
            print(f"ERROR en AWAITING_DOC_NUMBER_FOR_CONSULT: {e}")
            send_whatsapp_message(
                phone_to_reply,
                "😔 Ocurrió un error al consultar tus citas. Por favor, intenta de nuevo. 🙏",
            )

    # ── REPROGRAMACIÓN ───────────────────────────────────────────────────────

    elif state == "AWAITING_RESCHEDULE_FEE_CONFIRMATION":
        if message_text.lower() in ["sí", "si"]:
            session["state"] = "AWAITING_DOC_NUMBER_FOR_RESCHEDULE"
            send_whatsapp_message(phone_to_reply, "Perfecto. Ingresa tu número de D.N.I.")
        elif message_text.lower() == "no":
            send_whatsapp_message(
                phone_to_reply, "Entendido, no continuaremos con la reprogramación. 😊"
            )
            show_main_menu(phone_to_reply, session)
        else:
            send_whatsapp_message(phone_to_reply, "❓ Por favor, responde *Sí* o *No*.")

    elif state == "AWAITING_DOC_TYPE_FOR_RESCHEDULE":
        selected_option = process_user_choice(message_text, session.get("options", []), "tiddes")
        if selected_option:
            session["tidcod"] = selected_option["tidcod"]
            session["tiddes"] = selected_option["tiddes"]
            send_whatsapp_message(
                phone_to_reply, f"Ingresa tu número de {selected_option['tiddes']}."
            )
            session["state"] = "AWAITING_DOC_NUMBER_FOR_RESCHEDULE"
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de tu elección. 🙏",
            )

    elif state == "AWAITING_DOC_NUMBER_FOR_RESCHEDULE":
        return _buscar_citas_reprogramables(session, session_key, phone_to_reply, message_text, lolcli_headers)

    elif state == "AWAITING_APPOINTMENT_TO_RESCHEDULE":
        # NOTA: la bifurcación por tipo de cita (reevaluación médica: mismo
        # médico/sede, ventana de 4 semanas, S/.15 -- vs. terapia: mismo
        # terapeuta/sede, ventana de 30 días, motivo "falta del niño", lista de
        # exclusión) NO está implementada todavía: requiere confirmar con LOLIMSA
        # qué campo de la fila de ListarCitasPacientesWsp distingue ambos tipos, y
        # probarla contra una cita real. Por ahora el flujo de reprogramación
        # sigue siendo único y confía en que ReagendarCitaWsp rechace del lado del
        # servidor los casos fuera de regla.
        selected_option = process_user_choice(message_text, session.get("options", []))
        if selected_option:
            session["citid_to_reschedule"] = selected_option.get("secuencia")
            # TODO: confirmar el nombre real del campo de sede/siscod en una fila
            # de ListarCitasPacientesWsp -- lolcli_entidad es un código de
            # entidad, no de sede, y usarlo como siscod es probablemente
            # incorrecto salvo que coincidan por casualidad.
            session["siscod"] = selected_option.get("siscod", g.lolcli_entidad or "000000001")
            session["sercod"] = selected_option.get("sercod")
            session["medcod"] = selected_option.get("medcod")
            session["mednam"] = selected_option.get("medico", "")
            session["sernam"] = selected_option.get("servicio", "")
            session["establishment_name"] = selected_option.get(
                "sede", selected_option.get("establecimiento", "")
            )
            session["cittip"] = selected_option.get("cittip", "P")
            session["tarcod"] = selected_option.get("tarcod", "")
            send_whatsapp_message(
                phone_to_reply,
                f"Buscando fechas disponibles para el Dr(a). {session['mednam']}... 📅",
            )
            all_cupos = requests.post(
                _url("ListaCuposDisponibles"),
                json={
                    "siscod": session["siscod"],
                    "sercod": session["sercod"],
                    "medcod": session["medcod"],
                    "fecha": date.today().strftime("%Y%m%d"),
                },
                headers=lolcli_headers,
                timeout=config.LOLCLI_TIMEOUT,
            ).json().get("cupos", [])
            if not all_cupos:
                # El estado se mantiene en AWAITING_APPOINTMENT_TO_RESCHEDULE y
                # session["options"] (la lista de citas) no se sobreescribe, así
                # que el usuario puede simplemente escribir el número de otra
                # cita de la lista anterior sin necesidad de "retroceder".
                send_whatsapp_message(
                    phone_to_reply,
                    "😔 No hay fechas disponibles para ese médico en este momento. Puedes escribir el número de "
                    "otra cita de la lista anterior, o *salir* para cancelar.",
                )
            else:
                session["all_cupos"] = all_cupos
                reply, opts = format_menu(
                    "📅 Elige la nueva fecha:", _fechas_unicas(all_cupos), "citdat", "citdat"
                )
                session["options"] = opts
                session["state"] = "AWAITING_NEW_DATE_RESCHEDULE"
                send_whatsapp_message(phone_to_reply, reply)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa opción. Por favor, escribe el número de la cita que deseas reprogramar.",
            )

    elif state == "AWAITING_NEW_DATE_RESCHEDULE":
        selected_option = process_user_choice(message_text, session.get("options", []))
        if selected_option:
            session["new_fecha_api"] = selected_option["citdat"]
            session["new_fecha_user"] = format_date_es(
                datetime.strptime(selected_option["citdat"], "%Y%m%d")
            )
            send_whatsapp_message(
                phone_to_reply,
                f"Perfecto, para el *{session['new_fecha_user']}*. Viendo horarios disponibles... ⏰",
            )
            _mostrar_horarios(
                session,
                phone_to_reply,
                lolcli_headers,
                fecha_api=session["new_fecha_api"],
                invnum=session["citid_to_reschedule"],
                titulo="⏰ Elige el nuevo horario de tu preferencia:",
                cierre="_Elige el número del horario._",
                siguiente_estado="AWAITING_NEW_TIME_RESCHEDULE",
            )
        else:
            send_whatsapp_message(
                phone_to_reply,
                "❓ No reconocí esa fecha. Por favor, elige el número de la lista.",
            )

    elif state == "AWAITING_NEW_TIME_RESCHEDULE":
        try:
            selected = session["options"][int(message_text) - 1]["data"]
            session["new_hora_api"] = selected["hora"]
            session["new_hora_user"] = datetime.strptime(selected["hora"], "%H%M").strftime("%H:%M")
            send_whatsapp_message(
                phone_to_reply,
                f"🔄 *Confirmación de reprogramación:*\n\n"
                f"🩺 *Especialidad:* {session['sernam']}\n"
                f"👨‍⚕️ *Médico:* {session['mednam']}\n"
                f"🗓️ *Nueva fecha:* {session['new_fecha_user']}\n"
                f"⏰ *Nueva hora:* {session['new_hora_user']}\n\n"
                f"¿Confirmas el cambio? Escribe *'Sí'* para confirmar o *'salir'* para cancelar.",
            )
            session["state"] = "AWAITING_RESCHEDULE_CONFIRMATION"
        except (ValueError, IndexError):
            send_whatsapp_message(
                phone_to_reply, "⏰ Por favor, escribe solo el número del horario de la lista."
            )

    elif state == "AWAITING_RESCHEDULE_CONFIRMATION":
        if message_text.lower() in ["sí", "si"]:
            return _preparar_pago_reprogramacion(session, session_key, phone_to_reply, lolcli_headers)
        send_whatsapp_message(
            phone_to_reply,
            "Entendido. Escribe *'salir'* si deseas cancelar o continúa eligiendo. 😊",
        )

    elif state == "AWAITING_RESCHEDULE_PAYMENT_CONFIRMATION":
        return _confirmar_pago_reprogramacion(session, phone_to_reply, message_text, lolcli_headers)

    elif state == "AWAITING_POST_FLOW":
        if message_text.lower() in ["continuar", "continue"]:
            sessions.soft_reset(session)
            show_main_menu(phone_to_reply, session)
        else:
            send_whatsapp_message(
                phone_to_reply,
                "Escribe *'continuar'* para volver al menú o *'salir'* para terminar la sesión. 😊",
            )

    else:
        # Estado desconocido (sesión vieja tras un despliegue, p.ej.): se vuelve
        # al menú en vez de dejar al paciente sin respuesta.
        print(f"ADVERTENCIA: estado no contemplado en el flujo de pacientes: {state}")
        show_main_menu(phone_to_reply, session)

    return "processed"


# ---------------------------------------------------------------------------
# Pasos largos, separados de la máquina de estados para poder leerla
# ---------------------------------------------------------------------------

def _registrar_cita(session, session_key, phone_to_reply, lolcli_headers):
    """Graba la cita en LOLCLI y arranca el cobro."""
    try:
        send_whatsapp_message(phone_to_reply, "¡Excelente! Registrando tu cita, un momento por favor...")
        fecref_str = datetime.strptime(
            session["fecha_api"] + session["hora_api"], "%Y%m%d%H%M"
        ).strftime("%d-%m-%Y %H:%M")

        payload_cita = {
            "siscod": int(session["siscod"]),
            "medcod": session["medcod"],
            "sercod": session["sercod"],
            "fecref": fecref_str,
            "pachis": session["pachis"],
            "cittip": session["cittip"],
            "tarcod": session["tarcod"],
            "totnet": 0.0,
            "totimp": 0.0,
            "seccit": 0,
            # TODO: confirmar con LOLIMSA si prgori es una constante fija de
            # LOLCLI o un código específico del tenant anterior que deba cambiar
            # para ARIE.
            "prgori": "QU",
            # REVERTIDO 2026-07-14: se probó usando el plan real del paciente
            # (session["tarifa_plnnum"], resuelto por ItemCostoServicio) en vez de
            # este literal, y RegistroCita falló en producción con "El SP devolvió
            # status error sin mensaje" (paciente pachis 0005029, plan real
            # 200002). Indica que el SP de LOLCLI no acepta cualquier plnnum --
            # "161003" (posiblemente junto a prgori "QU") parece ser un valor
            # requerido/lista blanca, no sólo un placeholder mal migrado.
            # Revertido a la constante conocida-funcional hasta que LOLIMSA
            # confirme el valor correcto.
            "plnnum": "161003",
        }

        response = requests.post(
            _url("RegistroCita"),
            json=payload_cita,
            headers=lolcli_headers,
            timeout=config.LOLCLI_TIMEOUT,
        )
        response_data = response.json()

        if response_data.get("status") == "success":
            session["invnum_cita"] = response_data.get("invnum")
            session["prfnum_cita"] = response_data.get("prfnum")

            costo_final = 0.0
            if session["prfnum_cita"]:
                time.sleep(2)
                response_pagos = requests.post(
                    _url("ListaPagosPendientes"),
                    json={"pachis": session["pachis"]},
                    headers=lolcli_headers,
                    timeout=config.LOLCLI_TIMEOUT,
                )
                if response_pagos.ok:
                    for pago in response_pagos.json().get("pendientes", []):
                        if str(pago.get("prfnum")) == str(session["prfnum_cita"]):
                            costo_final = float(pago.get("prfppac", 0.0))
                            break

            session["costo_total"] = costo_final
            send_whatsapp_message(
                phone_to_reply,
                f"¡Tu cita ha sido agendada con la reserva *{session['invnum_cita']}*! 🎉\n"
                f"Ahora, estoy generando tu enlace de pago por *S/ {costo_final:.2f}*.",
            )
            generate_payment_link_and_send(session, phone_to_reply, lolcli_headers)
        else:
            error_msg = response_data.get("message", "un error del sistema.")
            send_whatsapp_message(
                phone_to_reply,
                f"No se pudo registrar la cita: {error_msg}. Escribe *'salir'* y vuelve a intentarlo.",
            )
            sessions.drop(session_key)
    except Exception as e:
        send_whatsapp_message(
            phone_to_reply,
            "😔 Lo sentimos, ocurrió un error al registrar tu cita. Por favor, intenta de nuevo o llámanos directamente. 🙏",
        )
        print(f"Error en AWAITING_CONFIRMATION (RegistroCita): {e}")
        sessions.drop(session_key)
    return "processed"


# Mensaje con el que la pasarela le devuelve el ID de pago al paciente; el
# paciente lo reenvía tal cual por WhatsApp.
PAYMENT_ID_PREFIX = "¡ya he completado mi pago!, el id de pago es:"
PAYMENT_DONE_WORDS = ["listo", "pagado", "ya pagué", "ya pague"]


def _token_de_pago(message_text, session, token_key):
    """Token a consultar según lo que escribió el paciente, o None si el mensaje
    no es una confirmación de pago."""
    message_lower = message_text.lower()
    if message_lower.startswith(PAYMENT_ID_PREFIX):
        return message_text[len(PAYMENT_ID_PREFIX):].strip()
    if message_lower in PAYMENT_DONE_WORDS:
        return session.get(token_key)
    return None


def _confirmar_pago_cita(session, phone_to_reply, message_text, lolcli_headers):
    if not (
        message_text.lower().startswith(PAYMENT_ID_PREFIX)
        or message_text.lower() in PAYMENT_DONE_WORDS
    ):
        send_whatsapp_message(
            phone_to_reply,
            "Para confirmar tu cita, por favor envíanos el mensaje completo de confirmación que recibiste "
            "al pagar (debe incluir el ID de pago). 📋",
        )
        return "awaiting_proper_confirmation"

    token_to_check = _token_de_pago(message_text, session, "payment_token")
    if not token_to_check:
        send_whatsapp_message(
            phone_to_reply,
            "❓ No encontramos un pago pendiente. Por favor, envíanos el mensaje completo de confirmación "
            "que recibiste al pagar. 📋",
        )
        return "processed"

    try:
        send_whatsapp_message(
            phone_to_reply,
            "✅ Recibido. Estamos verificando el estado de tu pago, un momento por favor... 🔍",
        )
        url_consulta = _url("ConsultarLinkPago")
        response_consulta = requests.post(
            url_consulta,
            json={"token": token_to_check},
            headers=lolcli_headers,
            timeout=config.LOLCLI_TIMEOUT,
        )

        if response_consulta.status_code == 404:
            print(f"ERROR 404: El endpoint '{url_consulta}' no fue encontrado.")
            send_whatsapp_message(
                phone_to_reply,
                "😔 No pudimos verificar tu pago. Por favor, contacta a nuestro equipo de soporte técnico. 🙏",
            )
            return "error_404_consulting_payment"

        response_consulta.raise_for_status()
        data_consulta = response_consulta.json()
        payment_data = data_consulta.get("data", {})

        if (
            data_consulta.get("status") == "success"
            and payment_data.get("estado_pago") == "COMPLETADO"
        ):
            send_whatsapp_message(
                phone_to_reply,
                "¡Pago confirmado! ✅\n\nTu cita está 100% confirmada.\n\n¡Gracias por preferir ARIE! Te esperamos.",
            )
            save_reminder(session)
            _mensaje_post_flujo(phone_to_reply, session)
            return "completed"

        current_status = payment_data.get("estado_pago", "desconocido")
        print(f"El estado del pago aún no es 'COMPLETADO'. Estado actual: {current_status}")
        send_whatsapp_message(
            phone_to_reply,
            "⏳ Aún no podemos confirmar tu pago. Asegúrate de haber completado la transacción y envíanos "
            "el mensaje de confirmación en unos minutos. 🙏",
        )
    except Exception as e:
        print(f"ERROR Inesperado al consultar pago: {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Ocurrió un error al verificar tu pago. Por favor, intenta nuevamente o contáctanos. 🙏",
        )
    return "processed"


def _buscar_citas_reprogramables(session, session_key, phone_to_reply, message_text, lolcli_headers):
    doc_number = message_text.strip()
    tidcod = session.get("tidcod")
    if _dni_invalido(tidcod, doc_number):
        send_whatsapp_message(phone_to_reply, "⚠️ El DNI debe tener exactamente 8 dígitos numéricos.")
        return "invalid_dni"
    session["pacdoc"] = doc_number
    try:
        send_whatsapp_message(phone_to_reply, "Un momento, buscando tus citas... 🔍")

        # ListarCitasPacientesWsp no devuelve "pachis" (código interno del
        # paciente), pero se necesita para el pago del derecho de reprogramación
        # (GenerarLinkPagoOrdenPrefactura). Se consulta ValidarPacienteWsp sólo
        # para obtenerlo -- su campo "valido" (regla de 10 días para citas
        # nuevas) no aplica aquí, así que no se usa para bloquear el acceso a
        # reprogramar. Si esto falla o vuelve vacío, no se bloquea el flujo:
        # hay un segundo intento justo antes del pago.
        try:
            pacientes = _validar_paciente(tidcod, doc_number, lolcli_headers)
            if pacientes:
                session["pachis"] = pacientes[0].get("pachis")
        except Exception as e:
            print(f"ERROR ValidarPacienteWsp (reschedule, pachis lookup): {e}")

        citas, server_error = _citas_del_paciente(doc_number, "R", lolcli_headers)

        if server_error:
            send_whatsapp_message(
                phone_to_reply,
                "😔 Tuvimos un problema técnico al buscar tus citas. Por favor, intenta de nuevo en unos "
                "minutos o contáctanos directamente. 🙏",
            )
            sessions.drop(session_key)
            return "server_error"

        if not citas:
            # La lista de reprogramables vino vacía. Antes de asumir que el
            # paciente no tiene ninguna cita, se consulta tipo=C para distinguir
            # "no tiene ninguna cita" de "tiene citas, pero ya ninguna es
            # reprogramable" (ya reprogramada una vez o fuera de la ventana de
            # 24h/30 días).
            citas_todas, _ = _citas_del_paciente(doc_number, "C", lolcli_headers)
            if citas_todas:
                send_whatsapp_message(
                    phone_to_reply,
                    "📋 Tienes citas agendadas, pero ninguna puede reprogramarse en este momento. 😊\n\n"
                    + RESCHEDULE_POLICY_NOTE
                    + "\n\nSi ya reprogramaste una cita antes, o está fuera de ese rango, ya no se puede "
                    "volver a reprogramar.",
                )
            else:
                send_whatsapp_message(
                    phone_to_reply, "📋 No tienes ninguna cita agendada en este momento. 😊"
                )
            sessions.drop(session_key)
            return "no_appointments"

        msg, formatted = format_appointments_list(
            citas, "¿Cuál cita deseas reprogramar?", mode="reschedule"
        )
        msg += RESCHEDULE_POLICY_NOTE
        session["options"] = formatted
        session["state"] = "AWAITING_APPOINTMENT_TO_RESCHEDULE"
        send_whatsapp_message(phone_to_reply, msg)
    except Exception as e:
        print(f"ERROR en AWAITING_DOC_NUMBER_FOR_RESCHEDULE: {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Ocurrió un error al buscar tus citas. Por favor, intenta de nuevo. 🙏",
        )
    return "processed"


def _preparar_pago_reprogramacion(session, session_key, phone_to_reply, lolcli_headers):
    try:
        fecref_str = datetime.strptime(
            session["new_fecha_api"] + session["new_hora_api"], "%Y%m%d%H%M"
        ).strftime("%d-%m-%Y %H:%M")

        payload_actualizar = {
            "xxinvnum": int(session["citid_to_reschedule"]),
            "xxmedcod": session["medcod"],
            "xxsercod": session["sercod"],
            "xxfecref": fecref_str,
            "xxcittip": session.get("cittip", "P"),
            "usecod": 1,
            # TODO: confirmar con LOLIMSA si usenam/usecod identifican al
            # usuario/sistema que ejecuta la acción (no cambiar sin confirmar) o
            # si es texto de marca visible al paciente (en cuyo caso debería
            # decir "ARIE").
            "usenam": "LOLIMSA",
        }
        # TODO: si la cita reprogramada es de terapia (recuperación), ARIE
        # requiere marcarla con un valor de "tipo de citado" -- el propio
        # requerimiento del cliente lo deja como "por consultar", así que no se
        # puede completar este campo todavía.
        if session.get("cittip") == "V" and session.get("zoom_link"):
            payload_actualizar["xxcitzoomlink"] = session["zoom_link"]

        # El cambio de cita sólo se ejecuta (ReagendarCitaWsp) una vez confirmado
        # el pago del derecho de reprogramación.
        session["reschedule_payload"] = payload_actualizar

        # Salvaguarda: el "pachis" normalmente ya se obtuvo al buscar las citas,
        # pero esa consulta a ValidarPacienteWsp pudo fallar o volver vacía en
        # silencio. GenerarLinkPagoOrdenPrefactura exige "pachis", así que aquí
        # se reintenta una vez antes de generar el link de pago -- mejor que
        # enviar una solicitud que sabemos que va a fallar.
        if not session.get("pachis"):
            try:
                retry_pacientes = _validar_paciente(
                    session.get("tidcod"), session.get("pacdoc"), lolcli_headers
                )
                if retry_pacientes:
                    session["pachis"] = retry_pacientes[0].get("pachis")
            except Exception as e:
                print(f"ERROR ValidarPacienteWsp (reschedule, pachis retry): {e}")

        if not session.get("pachis"):
            send_whatsapp_message(
                phone_to_reply,
                "😔 No pudimos verificar tu identificación de paciente para generar el pago. "
                "Por favor, intenta de nuevo o contáctanos directamente. 🙏",
            )
            sessions.drop(session_key)
            return "pachis_not_found"

        send_whatsapp_message(
            phone_to_reply,
            "Antes de confirmar el cambio, es necesario abonar el derecho de reprogramación de citas. "
            "Generando tu enlace de pago... 💳",
        )
        generate_reschedule_payment_link_and_send(session, phone_to_reply, lolcli_headers)
    except Exception as e:
        print(f"ERROR en AWAITING_RESCHEDULE_CONFIRMATION: {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Ocurrió un error al preparar tu reprogramación. Por favor, intenta de nuevo o contáctanos. 🙏",
        )
    return "processed"


def _confirmar_pago_reprogramacion(session, phone_to_reply, message_text, lolcli_headers):
    if not (
        message_text.lower().startswith(PAYMENT_ID_PREFIX)
        or message_text.lower() in PAYMENT_DONE_WORDS
    ):
        send_whatsapp_message(
            phone_to_reply,
            "Para confirmar tu reprogramación, por favor envíanos el mensaje completo de confirmación que "
            "recibiste al pagar (debe incluir el ID de pago), o escribe *'listo'* si ya completaste el pago. 📋",
        )
        return "awaiting_proper_confirmation"

    token_to_check = _token_de_pago(message_text, session, "reschedule_payment_token")
    if not token_to_check:
        send_whatsapp_message(
            phone_to_reply,
            "❓ No encontramos un pago pendiente. Por favor, envíanos el mensaje completo de confirmación "
            "que recibiste al pagar. 📋",
        )
        return "processed"

    try:
        send_whatsapp_message(
            phone_to_reply,
            "✅ Recibido. Estamos verificando el estado de tu pago, un momento por favor... 🔍",
        )
        response_consulta = requests.post(
            _url("ConsultarLinkPagoOrdenPrefactura"),
            json={"token": token_to_check},
            headers=lolcli_headers,
            timeout=config.LOLCLI_TIMEOUT,
        )

        if response_consulta.status_code == 404:
            print("ERROR 404: El endpoint 'ConsultarLinkPagoOrdenPrefactura' no fue encontrado.")
            send_whatsapp_message(
                phone_to_reply,
                "😔 No pudimos verificar tu pago. Por favor, contacta a nuestro equipo de soporte técnico. 🙏",
            )
            return "error_404_consulting_payment"

        response_consulta.raise_for_status()
        data_consulta = response_consulta.json()
        payment_data = data_consulta.get("data", {})

        if (
            data_consulta.get("status") == "success"
            and payment_data.get("estado_pago") == "COMPLETADO"
        ):
            send_whatsapp_message(
                phone_to_reply, "¡Pago confirmado! ✅ Procesando el cambio de tu cita, un momento..."
            )
            payload_actualizar = session.get("reschedule_payload", {})
            print(f"INFO ReagendarCitaWsp payload: {payload_actualizar}")
            resp = requests.post(
                _url("ReagendarCitaWsp"),
                json=payload_actualizar,
                headers=lolcli_headers,
                timeout=config.LOLCLI_TIMEOUT,
            )
            result = resp.json()
            print(f"INFO ReagendarCitaWsp response {resp.status_code}: {result}")
            if resp.ok and result.get("status") == "success":
                send_whatsapp_message(
                    phone_to_reply,
                    f"✅ ¡Tu cita ha sido reprogramada exitosamente!\n\n"
                    f"🗓️ *Nueva fecha:* {session['new_fecha_user']}\n"
                    f"⏰ *Nueva hora:* {session['new_hora_user']}\n\n"
                    f"¡Te esperamos! 😊",
                )
                _mensaje_post_flujo(phone_to_reply, session)
                return "rescheduled"
            raise Exception(result.get("xxmessage", result.get("message", "error desconocido")))

        current_status = payment_data.get("estado_pago", "desconocido")
        print(f"El estado del pago de reprogramación aún no es 'COMPLETADO'. Estado actual: {current_status}")
        send_whatsapp_message(
            phone_to_reply,
            "⏳ Aún no podemos confirmar tu pago. Asegúrate de haber completado la transacción y "
            "envíanos el mensaje de confirmación en unos minutos. 🙏",
        )
    except Exception as e:
        print(f"ERROR Inesperado al consultar pago de reprogramación / reprogramar: {e}")
        send_whatsapp_message(
            phone_to_reply,
            "😔 Ocurrió un error al verificar tu pago o al reprogramar tu cita. Por favor, intenta "
            "nuevamente o contáctanos. 🙏",
        )
    return "processed"
