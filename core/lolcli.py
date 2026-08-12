# --- core/lolcli.py ---
"""Acceso al sistema clínico LOLCLI.

Los dos flujos hablan con LOLCLI, pero con contratos distintos:

* El flujo de quirófanos usa el contrato documentado en "Documentos APIS
  QUirofanos.docx" (sección 3): toda respuesta trae `status`, y si es "error"
  hay que mostrarle al usuario el `message` que devuelve el servidor tal cual.
  Para eso está `call()`.

* El flujo de pacientes habla con endpoints más viejos, cada uno con su propia
  forma de indicar el error (a veces `status`, a veces el código HTTP, a veces
  una lista vacía). Ese flujo sigue haciendo sus llamadas directas con
  `requests` y usa `url()` sólo para resolver la dirección: unificarlas bajo
  `call()` cambiaría el manejo de errores de cada pantalla, que es justamente lo
  que no conviene tocar en una fusión.
"""

import time

import requests

import config


def url(flow, endpoint):
    """URL absoluta de un endpoint para el flujo indicado."""
    return f"{config.lolcli_url(flow)}/{endpoint}"


def call(flow, endpoint, payload, headers, timeout=None):
    """POST a LOLCLI siguiendo el contrato `status`/`message`.

    Retorna (data, error_message); error_message es None si status == "success".
    """
    destino = url(flow, endpoint)

    # Dos mensajes distintos a propósito, porque son dos problemas distintos y
    # el usuario no puede hacer lo mismo ante los dos:
    #
    # - TRANSITORIO (timeout, conexión rechazada): el servidor puede volver
    #   solo, así que tiene sentido pedirle que reintente.
    # - PERMANENTE (404, HTML de error): el endpoint no existe con ese nombre o
    #   está mal configurado. Reintentar no lo va a arreglar nunca, y decirle
    #   "intenta en unos minutos" lo deja en un bucle. Se le deriva a una
    #   persona.
    #
    # Antes los dos devolvían la MISMA frase, y eso desviaba el diagnóstico:
    # "no pudimos conectar" se lee como caída de red, cuando en este proyecto
    # ya hubo dos endpoints cuyo nombre real no coincidía con el documentado
    # (ver LOLCLI_ENDPOINTS en flows/quirofanos.py). Con el mismo texto para
    # ambos, la única forma de distinguirlos era leer el log del servidor.
    MSG_TRANSITORIO = ("No pudimos conectar con el servidor en este momento. "
                       "Intenta de nuevo en unos minutos.")
    MSG_PERMANENTE = ("Este trámite no está disponible en este momento. "
                      "Escribe *'asesor'* y te atenderá una persona.")

    inicio = time.monotonic()
    try:
        resp = requests.post(
            destino,
            json=payload,
            headers=headers,
            timeout=timeout or config.LOLCLI_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        # Falla de red/timeout: no hubo respuesta del servidor. El payload se
        # registra aquí también: antes sólo se imprimía después de recibir
        # respuesta, así que justo en el caso de fallo de red no quedaba
        # constancia de qué se había intentado enviar.
        ms = (time.monotonic() - inicio) * 1000
        print(f"ERROR {endpoint}: TRANSITORIO sin respuesta de {destino} tras {ms:.0f}ms "
              f"-- {type(e).__name__}: {e} -- payload={payload}")
        return None, MSG_TRANSITORIO

    ms = (time.monotonic() - inicio) * 1000
    print(f"INFO {endpoint}: POST {destino} payload={payload} -> HTTP {resp.status_code} ({ms:.0f}ms)")

    if resp.status_code in (401, 403):
        print(f"ERROR {endpoint}: PERMANENTE credenciales rechazadas (HTTP {resp.status_code}). "
              f"Revisar lolcli_token de la clínica.")
        return None, MSG_PERMANENTE

    try:
        data = resp.json()
    except ValueError:
        # Hubo respuesta, pero no es JSON: típicamente un 404 (nombre de
        # endpoint incorrecto) o una página de error del servidor.
        pista = ("el endpoint NO existe con ese nombre" if resp.status_code == 404
                 else "el servidor devolvió algo que no es JSON")
        print(f"ERROR {endpoint}: PERMANENTE {pista} (HTTP {resp.status_code}) "
              f"-- {resp.text[:300]}")
        return None, MSG_PERMANENTE

    if data.get("status") == "error":
        return data, data.get("message", "Ocurrió un error inesperado.")
    return data, None
