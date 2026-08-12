# --- config.py (configuración compartida por los dos flujos) ---
"""Configuración del bot de doble servicio.

Los dos bots que se fusionan aquí venían configurados de forma distinta: el de
pacientes (ARIE) leía las credenciales de LOLCLI directamente del .env y servía
una sola entidad, mientras que el de quirófanos las leía de clinics.json y
podía servir varias clínicas por la URL del webhook (/webhook/<clinic_id>).

Se conserva el modelo de clinics.json, que es el más completo (trae además la
instancia de Evolution, el teléfono de soporte y el horario de atención), y se
le agrega un respaldo por variables de entorno: si no hay clinics.json se arma
una única clínica "default" con lo que haya en el .env, que es exactamente cómo
se desplegaba el bot de pacientes. Así ninguno de los dos despliegues actuales
necesita cambiar su forma de configurarse.

Los dos flujos pueden apuntar a URLs distintas de LOLCLI: en los archivos de
configuración que dejó cada equipo, cada flujo escuchaba en un puerto distinto
del mismo host. Por eso 'lolcli_url' es el valor común y
'lolcli_url_pacientes'/'lolcli_url_quirofanos' lo pisan por flujo cuando hacen
falta. Si sólo se define 'lolcli_url', los dos flujos usan ese.
"""

import json
import os

from dotenv import load_dotenv
from flask import g

load_dotenv()

# --- Gateway de WhatsApp (Evolution API) ---------------------------------
# Es uno solo para todo el bot: el objetivo de esta versión es justamente que
# los dos servicios compartan un único número de WhatsApp.
EVOLUTION_API_URL = (os.getenv("EVOLUTION_API_URL") or "").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")
EVOLUTION_INSTANCE_NAME = os.getenv("EVOLUTION_INSTANCE_NAME", "")

# Timeouts separados: LOLCLI y Evolution son servicios distintos y no tienen por
# qué compartir el mismo presupuesto de espera. Sin timeout, requests espera
# indefinidamente (sólo acotado por el stack TCP del SO) si el backend no
# responde, y el hilo que atiende el webhook queda retenido.
LOLCLI_TIMEOUT = int(os.getenv("LOLCLI_TIMEOUT", 15))
EVOLUTION_TIMEOUT = int(os.getenv("EVOLUTION_TIMEOUT", 15))

# Pausa antes de cada mensaje saliente. Varios mensajes seguidos y sin espera
# (el bot manda de a dos o tres por paso) llegan desordenados al teléfono y
# WhatsApp puede marcarlos como envío masivo. El bot de pacientes usaba 1.5s y
# el de quirófanos 1.2s; se unifica en 1.2s y queda configurable porque este
# valor multiplica la latencia de cada paso del flujo.
SEND_PACING_SECONDS = float(os.getenv("SEND_PACING_SECONDS", 1.2))

# Mensajes interactivos (listas y botones). Con 0 el bot no los intenta: manda
# directamente el menú numerado en texto.
#
# No todas las instalaciones de Evolution los soportan. En la de LOLIMSA,
# /message/sendList responde 400 con
# {"message":["TypeError: this.isZero is not a function"]} -- un fallo interno
# de Baileys, no del payload -- así que cada menú gastaba una petición
# condenada a fallar, esperaba dos veces SEND_PACING_SECONDS (una por el
# intento y otra por el respaldo) y dejaba una línea ERROR en el log. El
# usuario nunca llegaba a ver una lista: siempre el texto de respaldo.
#
# Se deja en 1 por defecto para no cambiar el comportamiento donde sí
# funcionan; en el .env del despliegue que no los soporta se pone 0.
EVOLUTION_INTERACTIVE = os.getenv("EVOLUTION_INTERACTIVE", "1") == "1"

# --- Pasarela de pagos de quirófanos -------------------------------------
# El flujo de pago de quirófanos queda montado pero DESACTIVADO: todavía no
# llegan las URLs ni las credenciales de la pasarela. Con PAGOS_HABILITADOS=0 el
# bot confirma la reserva y la graba directamente en la base. El flujo de
# pacientes NO usa esto: ese cobra con los enlaces de pago de LOLCLI, que sí
# están operativos.
PAGOS_HABILITADOS = os.getenv("PAGOS_HABILITADOS", "0") == "1"
PAGOS_URL_BASE = (os.getenv("PAGOS_URL_BASE") or "").rstrip("/")

# --- Pagos de citas (pacientes) ------------------------------------------
# FASE DE PRUEBAS: LOLCLI todavía devuelve el dominio de producción de la
# pasarela en el "payment_link" de las citas, así que para que el cobro se
# procese en el entorno de pruebas de Niubiz hay que anteponerle un prefijo
# al host. En el bot original esto estaba escrito en el código
# con la nota "quitar al pasar a producción"; ahora es configuración: al pasar a
# producción real basta con dejar PAGOS_QA_PREFIX vacío en el .env.
PAGOS_QA_PREFIX = os.getenv("PAGOS_QA_PREFIX", "qa-pacientes.")

# --- Clínicas -------------------------------------------------------------
CLINICS = {}
DEFAULT_CLINIC_ID = ""

# La ruta se resuelve contra este archivo y no contra el directorio actual: el
# bot de quirófanos abría "clinics.json" en relativo y sólo arrancaba bien si el
# proceso se lanzaba parado en la carpeta del proyecto, que es un detalle fácil
# de perder al envolver el proceso como servicio (NSSM, systemd).
CLINICS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clinics.json")

# Claves que puede traer una clínica, con el valor por defecto que se toma del
# .env cuando el archivo no las trae. Sirve para dos cosas: armar la clínica
# "default" cuando no hay clinics.json, y completar las que sí están pero vienen
# incompletas (p.ej. el clinics.json de quirófanos no trae LOLCLI_API_URL de
# pacientes).
_CLINIC_DEFAULTS = {
    "lolcli_url": lambda: (os.getenv("LOLCLI_API_URL") or "").rstrip("/"),
    "lolcli_url_pacientes": lambda: (os.getenv("LOLCLI_API_URL_PACIENTES") or "").rstrip("/"),
    "lolcli_url_quirofanos": lambda: (os.getenv("LOLCLI_API_URL_QUIROFANOS") or "").rstrip("/"),
    "lolcli_token": lambda: os.getenv("LOLCLI_API_TOKEN", ""),
    # Los dos flujos pueden vivir en servidores LOLCLI distintos, y cada
    # servidor tiene sus propias credenciales: el bot de pacientes apuntaba a
    # una máquina y el de quirófanos a otra, con tokens distintos. Igual que
    # con las URL, si sólo se define 'lolcli_token' los dos flujos usan ese.
    "lolcli_token_pacientes": lambda: os.getenv("LOLCLI_API_TOKEN_PACIENTES", ""),
    "lolcli_token_quirofanos": lambda: os.getenv("LOLCLI_API_TOKEN_QUIROFANOS", ""),
    "lolcli_entidad": lambda: os.getenv("LOLCLI_ENTIDAD", ""),
    "evolution_instance": lambda: EVOLUTION_INSTANCE_NAME,
    "default_siscod": lambda: int(os.getenv("DEFAULT_SISCOD", 1)),
    "staff_phone": lambda: os.getenv("STAFF_PHONE", ""),
    "support_email": lambda: os.getenv("SUPPORT_EMAIL", ""),
    "support_hours": lambda: os.getenv("SUPPORT_HOURS", "Lunes a Viernes, 8am - 6pm"),
}


def _completar(clinic):
    """Rellena con el .env las claves que la clínica no define."""
    for clave, default in _CLINIC_DEFAULTS.items():
        if not clinic.get(clave):
            clinic[clave] = default()
    return clinic


def load_clinics():
    """Carga clinics.json y deja CLINICS/DEFAULT_CLINIC_ID listos.

    Un fallo aquí no detiene el arranque a propósito: el servidor igual levanta
    y responde /test, y el webhook contesta 404 por clínica desconocida, que es
    un síntoma mucho más fácil de diagnosticar que un proceso que no arranca.
    """
    global CLINICS, DEFAULT_CLINIC_ID

    datos = {}
    if os.path.exists(CLINICS_FILE):
        try:
            with open(CLINICS_FILE, "r", encoding="utf-8") as f:
                datos = json.load(f)
        except Exception as e:
            print(f"ERROR: No se pudo leer {CLINICS_FILE}: {e}")
            datos = {}

    if not datos:
        # Despliegue de un solo cliente, configurado sólo por .env: es como
        # corría el bot de pacientes.
        print("INFO: sin clinics.json utilizable; se arma la clínica 'default' desde el .env.")
        datos = {"default": {}}

    CLINICS = {cid: _completar(dict(cfg or {})) for cid, cfg in datos.items()}

    # La clínica por defecto es la que atiende /webhook (sin clinic_id en la
    # URL), que es la forma en que estaba dado de alta el webhook del bot de
    # pacientes en Evolution.
    preferida = os.getenv("DEFAULT_CLINIC_ID", "")
    if preferida and preferida in CLINICS:
        DEFAULT_CLINIC_ID = preferida
    else:
        if preferida:
            print(f"ADVERTENCIA: DEFAULT_CLINIC_ID='{preferida}' no existe en clinics.json; se usa la primera.")
        DEFAULT_CLINIC_ID = next(iter(CLINICS), "")

    print(f"INFO: {len(CLINICS)} clínica(s) cargada(s): {list(CLINICS.keys())} (default: '{DEFAULT_CLINIC_ID}')")
    _avisar_configuracion_incompleta()


def _avisar_configuracion_incompleta():
    """Avisa al arrancar de lo que falta, en vez de fallar recién en el primer
    mensaje de un paciente."""
    if not EVOLUTION_API_URL or not EVOLUTION_API_KEY:
        print("ADVERTENCIA: falta EVOLUTION_API_URL o EVOLUTION_API_KEY; el bot no podrá responder.")
    for cid, cfg in CLINICS.items():
        if not clinic_lolcli_url(cfg, "pacientes"):
            print(f"ADVERTENCIA: clínica '{cid}' sin URL de LOLCLI para el flujo de pacientes.")
        if not clinic_lolcli_url(cfg, "quirofanos"):
            print(f"ADVERTENCIA: clínica '{cid}' sin URL de LOLCLI para el flujo de quirófanos.")
        for flujo in ("pacientes", "quirofanos"):
            if not clinic_lolcli_token(cfg, flujo):
                print(f"ADVERTENCIA: clínica '{cid}' sin token de LOLCLI para el flujo de {flujo}.")


def clinic_lolcli_url(clinic, flow):
    """URL de LOLCLI de esa clínica para ese flujo ('pacientes'|'quirofanos')."""
    return (clinic.get(f"lolcli_url_{flow}") or clinic.get("lolcli_url") or "").rstrip("/")


def clinic_lolcli_token(clinic, flow):
    """Token de LOLCLI de esa clínica para ese flujo.

    Existe por lo mismo que clinic_lolcli_url: los dos flujos pueden apuntar a
    servidores LOLCLI distintos, y un token que vale en uno no tiene por qué
    valer en el otro. Con un solo 'lolcli_token' para ambos, apuntar el flujo
    de pacientes a su servidor real dejaba de funcionar por credenciales.
    """
    return clinic.get(f"lolcli_token_{flow}") or clinic.get("lolcli_token") or ""


def bind_request_context(clinic_id):
    """Deja la configuración de la clínica en `g` para lo que dure la petición.

    Los dos flujos leen de aquí en vez de leer variables de módulo, que es lo
    que hacía el bot de pacientes y lo que impedía servir más de una clínica.
    """
    clinic = CLINICS[clinic_id]
    g.clinic_id = clinic_id
    g.clinic = clinic
    g.lolcli_entidad = clinic["lolcli_entidad"]
    g.evolution_instance = clinic["evolution_instance"]
    g.default_siscod = clinic["default_siscod"]
    g.staff_phone = clinic["staff_phone"]
    g.support_hours = clinic["support_hours"]


def lolcli_url(flow):
    """URL de LOLCLI del flujo indicado, para la clínica de esta petición."""
    return clinic_lolcli_url(g.clinic, flow)


def lolcli_headers(flow):
    """Cabeceras para el flujo indicado ('pacientes'|'quirofanos').

    Pide el flujo explícitamente porque el token puede ser distinto en cada
    uno; app.py lo sabe al despachar (cada módulo de flows declara su
    LOLCLI_FLOW).
    """
    return {
        "Authorization": f"Basic {clinic_lolcli_token(g.clinic, flow)}",
        "Content-Type": "application/json",
    }
