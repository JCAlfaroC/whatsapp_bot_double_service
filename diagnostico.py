"""Diagnóstico de dependencias del bot.

    python diagnostico.py

Comprueba, contra la configuración REAL del despliegue (el mismo .env y el
mismo clinics.json que lee el bot), que todo aquello de lo que depende esté en
su sitio: la configuración, cada endpoint de LOLCLI de los dos flujos, y la
instancia de Evolution.

Existe porque el bot, cuando algo falla, sólo puede decirle al usuario
"tenemos dificultades técnicas": no hay forma de saber desde el teléfono si lo
que falló fue la red, un endpoint que no existe, un token rechazado o una
entidad mal configurada. Esto lo dice en una pantalla.

SEGURIDAD: sólo hace llamadas de lectura. Los endpoints que graban
(RegistrarSeparacionQuirofanoWsp, RegistroCita, ReagendarCitaWsp) se listan
pero NO se llaman: un diagnóstico no puede dejar una reserva de quirófano ni
una cita creada. Tampoco manda mensajes de WhatsApp.
"""

import os
import sys

import requests

import config

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# La consola de Windows Server no interpreta los códigos ANSI por sí sola: sin
# esto, en vez de color salen los códigos en crudo ("[32m OK") y la tabla queda
# ilegible justo en la máquina donde más falta hace. colorama llega instalado
# como dependencia de click (que viene con flask), pero no está declarado en
# requirements.txt, así que si no está se sigue sin color en vez de fallar.
_color = True
if os.name == "nt":
    try:
        import colorama

        colorama.just_fix_windows_console()
    except Exception:
        _color = False

VERDE = "\033[32m" if _color else ""
ROJO = "\033[31m" if _color else ""
AMARILLO = "\033[33m" if _color else ""
GRIS = "\033[90m" if _color else ""
FIN = "\033[0m" if _color else ""

OK, FALLA, AVISO, OMITIDO = "OK", "FALLA", "AVISO", "OMITIDO"

resultados = []


def anotar(bloque, prueba, estado, detalle=""):
    resultados.append((bloque, prueba, estado, detalle))
    color = {OK: VERDE, FALLA: ROJO, AVISO: AMARILLO, OMITIDO: GRIS}[estado]
    print(f"  [{color}{estado:^7}{FIN}] {prueba}" + (f" — {detalle}" if detalle else ""))


def _sondear(url, payload, headers, timeout):
    """Llama a un endpoint y clasifica la respuesta.

    Distinguir "el endpoint no existe" de "el endpoint existe y rechazó lo que
    le mandé" es el punto de todo esto: los dos le llegan al usuario como el
    mismo mensaje genérico, pero el primero es un error de configuración del
    bot y el segundo es un problema de datos.
    """
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectTimeout:
        return FALLA, "timeout al conectar (¿firewall o puerto cerrado?)", None
    except requests.exceptions.ConnectionError:
        return FALLA, "conexión rechazada (¿servicio caído o puerto equivocado?)", None
    except requests.exceptions.RequestException as e:
        return FALLA, f"{type(e).__name__}: {e}", None

    if r.status_code == 404:
        return FALLA, "HTTP 404 — el endpoint NO existe con ese nombre", None
    if r.status_code in (401, 403):
        return FALLA, f"HTTP {r.status_code} — credenciales rechazadas", None

    try:
        data = r.json()
    except ValueError:
        cuerpo = r.text.strip().replace("\n", " ")[:120]
        return FALLA, f"HTTP {r.status_code} pero la respuesta no es JSON: {cuerpo}", None

    if isinstance(data, dict) and data.get("status") == "error":
        # El endpoint existe y contestó: que rechace el payload de prueba es
        # esperable, porque aquí se manda uno mínimo a propósito.
        return AVISO, f"existe; responde error: {data.get('message', '')}"[:140], data

    return OK, f"HTTP {r.status_code}", data


def bloque_configuracion():
    print(f"\n{'─' * 74}\n CONFIGURACIÓN\n{'─' * 74}")
    config.load_clinics()

    if not config.CLINICS:
        anotar("config", "clínicas cargadas", FALLA, "ninguna")
        return

    anotar("config", "clínicas cargadas", OK,
           f"{list(config.CLINICS)} (por defecto: '{config.DEFAULT_CLINIC_ID}')")

    if config.EVOLUTION_API_URL and config.EVOLUTION_API_KEY:
        anotar("config", "credenciales de Evolution presentes", OK, config.EVOLUTION_API_URL)
    else:
        anotar("config", "credenciales de Evolution presentes", FALLA,
               "falta EVOLUTION_API_URL o EVOLUTION_API_KEY")

    for cid, clinic in config.CLINICS.items():
        for flujo in ("pacientes", "quirofanos"):
            url = config.clinic_lolcli_url(clinic, flujo)
            if url:
                anotar("config", f"[{cid}] URL de LOLCLI ({flujo})", OK, url)
            else:
                anotar("config", f"[{cid}] URL de LOLCLI ({flujo})", FALLA, "sin definir")
        for flujo in ("pacientes", "quirofanos"):
            if config.clinic_lolcli_token(clinic, flujo):
                anotar("config", f"[{cid}] token de LOLCLI ({flujo})", OK, "presente")
            else:
                anotar("config", f"[{cid}] token de LOLCLI ({flujo})", FALLA, "vacío")
        if not clinic.get("lolcli_entidad"):
            anotar("config", f"[{cid}] entidad", AVISO, "vacía")


def bloque_lolcli():
    print(f"\n{'─' * 74}\n LOLCLI\n{'─' * 74}")

    for cid, clinic in config.CLINICS.items():
        siscod = clinic.get("default_siscod", 1)
        entidad = clinic.get("lolcli_entidad", "")

        def _headers(flujo):
            # Por flujo: cada servidor de LOLCLI tiene sus propias credenciales.
            return {
                "Authorization": f"Basic {config.clinic_lolcli_token(clinic, flujo)}",
                "Content-Type": "application/json",
            }

        # --- Flujo de pacientes (sólo lectura) ---
        headers = _headers("pacientes")
        base = config.clinic_lolcli_url(clinic, "pacientes")
        if base:
            estado, detalle, data = _sondear(
                f"{base}/ListaEstablecimientos", {"entidad": entidad}, headers, config.LOLCLI_TIMEOUT)
            if estado == OK:
                n = len(data.get("establecimientos", []) if isinstance(data, dict) else [])
                detalle = f"{n} sedes"
                if n == 0:
                    estado = AVISO
                    detalle = "0 sedes — revisar entidad"
            anotar("lolcli", f"[{cid}] pacientes · ListaEstablecimientos", estado, detalle)

            estado, detalle, data = _sondear(
                f"{base}/ListaTipoDocumentoElolcli", {}, headers, config.LOLCLI_TIMEOUT)
            if estado == OK:
                n = len(data.get("tipoDocumentos", []) if isinstance(data, dict) else [])
                detalle = f"{n} tipos de documento"
                if n == 0:
                    estado = AVISO
                    detalle = "0 tipos de documento"
            anotar("lolcli", f"[{cid}] pacientes · ListaTipoDocumentoElolcli", estado, detalle)

            anotar("lolcli", f"[{cid}] pacientes · ValidarPacienteWsp", OMITIDO,
                   "necesita un documento real; probar a mano")
            for grabador in ("RegistroCita", "ReagendarCitaWsp"):
                anotar("lolcli", f"[{cid}] pacientes · {grabador}", OMITIDO, "graba: no se prueba")

        # --- Flujo de quirófanos ---
        headers = _headers("quirofanos")
        base = config.clinic_lolcli_url(clinic, "quirofanos")
        if not base:
            continue

        estado, detalle, data = _sondear(
            f"{base}/ListarQuirofanosWsp", {"xxsiscod": siscod}, headers, config.LOLCLI_TIMEOUT)
        quirofanos = []
        if estado == OK and isinstance(data, dict):
            quirofanos = data.get("quirofanos", [])
            detalle = f"{len(quirofanos)} quirófanos"
            if not quirofanos:
                estado = AVISO
                detalle = f"0 quirófanos — revisar siscod={siscod}"
        anotar("lolcli", f"[{cid}] quirófanos · ListarQuirofanosWsp", estado, detalle)

        # Los tres que el flujo médico nunca llegó a ejercitar contra este
        # servidor. Se mandan payloads mínimos: da igual que los rechace, lo
        # que se quiere saber es si el nombre del endpoint existe (404 o no).
        for etiqueta, endpoint, payload in (
            ("ListarTurnosQuirofanoDisponiblesWsp", "ListarTurnosQuirofanoDisponiblesWsp", {"xxsiscod": siscod}),
            ("CalcularPrecioQuirofanoWsp", "CalcularPrecioQuirofanoWsp", {"xxsiscod": siscod}),
            ("ListarSeparacionesPorMedico", "ListarSeparacionesPorMedico", {"xxsiscod": siscod}),
        ):
            estado, detalle, _ = _sondear(f"{base}/{endpoint}", payload, headers, config.LOLCLI_TIMEOUT)
            anotar("lolcli", f"[{cid}] quirófanos · {etiqueta}", estado, detalle)

        anotar("lolcli", f"[{cid}] quirófanos · RegistrarSeparacionQuirofanoWsp", OMITIDO,
               "graba: no se prueba")


def bloque_evolution():
    print(f"\n{'─' * 74}\n EVOLUTION (WhatsApp)\n{'─' * 74}")
    if not (config.EVOLUTION_API_URL and config.EVOLUTION_API_KEY):
        anotar("evolution", "consulta de instancias", OMITIDO, "sin credenciales")
        return

    try:
        r = requests.get(
            f"{config.EVOLUTION_API_URL}/instance/fetchInstances",
            headers={"apikey": config.EVOLUTION_API_KEY},
            timeout=config.EVOLUTION_TIMEOUT,
        )
    except requests.exceptions.ConnectTimeout:
        anotar("evolution", "consulta de instancias", FALLA, "timeout al conectar")
        return
    except requests.exceptions.ConnectionError:
        anotar("evolution", "consulta de instancias", FALLA,
               f"no se llega a {config.EVOLUTION_API_URL}")
        return
    except requests.exceptions.RequestException as e:
        anotar("evolution", "consulta de instancias", FALLA, f"{type(e).__name__}")
        return

    if r.status_code in (401, 403):
        anotar("evolution", "clave de API", FALLA, f"HTTP {r.status_code} — clave rechazada")
        return
    try:
        instancias = r.json()
    except ValueError:
        anotar("evolution", "consulta de instancias", FALLA, f"respuesta no-JSON (HTTP {r.status_code})")
        return

    anotar("evolution", "clave de API", OK, f"{len(instancias)} instancia(s)")

    por_nombre = {i.get("name"): i for i in instancias if isinstance(i, dict)}
    esperadas = {c.get("evolution_instance") for c in config.CLINICS.values()}
    esperadas.discard("")

    for nombre in sorted(esperadas):
        inst = por_nombre.get(nombre)
        if not inst:
            anotar("evolution", f"instancia '{nombre}'", FALLA,
                   f"no existe. Disponibles: {sorted(por_nombre)}")
            continue
        conexion = inst.get("connectionStatus")
        numero = inst.get("number", "?")
        if conexion == "open":
            anotar("evolution", f"instancia '{nombre}'", OK, f"conectada · {numero}")
        else:
            anotar("evolution", f"instancia '{nombre}'", FALLA,
                   f"connectionStatus={conexion} · {numero}")

    anotar("evolution", "listas interactivas (sendList)", OMITIDO,
           f"mandaría un mensaje real; EVOLUTION_INTERACTIVE={'1' if config.EVOLUTION_INTERACTIVE else '0'}")


def resumen():
    print(f"\n{'═' * 74}")
    fallas = [r for r in resultados if r[2] == FALLA]
    avisos = [r for r in resultados if r[2] == AVISO]

    if not fallas and not avisos:
        print(f" {VERDE}Todo en orden.{FIN}")
    if avisos:
        print(f" {AMARILLO}{len(avisos)} aviso(s):{FIN}")
        for _, prueba, _, detalle in avisos:
            print(f"   · {prueba} — {detalle}")
    if fallas:
        print(f" {ROJO}{len(fallas)} fallo(s) — el bot NO va a funcionar bien:{FIN}")
        for _, prueba, _, detalle in fallas:
            print(f"   · {prueba} — {detalle}")
    print(f"{'═' * 74}")
    return 1 if fallas else 0


if __name__ == "__main__":
    print("Diagnóstico del bot de doble servicio — sólo lecturas, no manda WhatsApp")
    bloque_configuracion()
    if config.CLINICS:
        bloque_lolcli()
        bloque_evolution()
    sys.exit(resumen())
