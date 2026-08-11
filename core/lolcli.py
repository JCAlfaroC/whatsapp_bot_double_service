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
    try:
        resp = requests.post(
            destino,
            json=payload,
            headers=headers,
            timeout=timeout or config.LOLCLI_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        # Falla de red/timeout: no hubo respuesta del servidor.
        print(f"ERROR {endpoint}: sin respuesta de {destino} -- {type(e).__name__}: {e}")
        return None, "No pudimos conectar con el servidor en este momento. Intenta de nuevo en unos minutos."

    print(f"INFO {endpoint}: POST {destino} payload={payload} -> HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        # Hubo respuesta, pero no es JSON: típicamente un 404 (nombre de
        # endpoint incorrecto) o una página de error del servidor. Se registra
        # el cuerpo para poder distinguirlo de una caída de red.
        print(f"ERROR {endpoint}: respuesta no-JSON (HTTP {resp.status_code}): {resp.text[:500]}")
        return None, "No pudimos conectar con el servidor en este momento. Intenta de nuevo en unos minutos."

    if data.get("status") == "error":
        return data, data.get("message", "Ocurrió un error inesperado.")
    return data, None
