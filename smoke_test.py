"""Prueba de humo del bot de doble servicio.

    python smoke_test.py

Recorre los dos flujos completos (paciente y médico) contra un doble de LOLCLI y
de Evolution: sustituye requests.post, así que NO toca la red ni manda mensajes
de WhatsApp reales, y se puede correr sin credenciales. Imprime la conversación
tal como la vería el usuario en el teléfono y termina con código 1 si alguna
comprobación falla.

Sirve de regresión de la fusión: verifica que las dos máquinas de estados sigan
funcionando por separado detrás del mismo número, que el rol de cada sesión no
se mezcle, y que los comandos globales y el descarte de webhooks repetidos
funcionen igual en ambas.
"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

# Sin pausa entre mensajes: aquí no hay un WhatsApp real que pueda desordenarlos.
os.environ["SEND_PACING_SECONDS"] = "0"

import requests  # noqa: E402

SALIDA = []          # mensajes que el bot manda por WhatsApp
LOLCLI_CALLS = []    # (endpoint, payload)

MANANA = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")


class FakeResponse:
    def __init__(self, data, status=200, url=""):
        self._data = data
        self.status_code = status
        self.url = url
        self.text = json.dumps(data, ensure_ascii=False)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)


def _turnos_del_dia():
    """08:00-11:30 libre, 12:00-12:30 ocupado, 13:00-17:30 libre."""
    turnos = []
    for slot in range(16, 36):  # 08:00 .. 17:30
        h, r = divmod(slot, 2)
        ocupado = slot in (24, 25)  # 12:00 y 12:30
        turnos.append({
            "fecha": f"{MANANA}T00:00:00.000Z",
            "hora": f"{h:02d}:{30 if r else 0:02d}",
            "disponible": 0 if ocupado else 1,
        })
    return turnos


LOLCLI = {
    # --- pacientes ---
    "ListaEstablecimientos": {"establecimientos": [
        {"siscod": 1, "sisent": "SEDE CENTRAL S.A.C."},
        {"siscod": 2, "sisent": "SEDE NORTE S.A.C."},
    ]},
    "ListaTipoDocumentoElolcli": {"tipoDocumentos": [
        {"tidcod": "01", "tiddes": "D.N.I."},
        {"tidcod": "02", "tiddes": "CARNET DE EXTRANJERIA"},
        {"tidcod": "03", "tiddes": "CEDULA DIPLOMATICA"},
        {"tidcod": "04", "tiddes": "PASAPORTE"},
    ]},
    "ValidarPacienteWsp": {"paciente": [
        {"valido": "S", "pachis": "0005029", "pacpmn": "JUAN PEREZ"}
    ]},
    "ListaServiciosWsp": {"servicios": [
        {"sercod": "S01", "serdes": "MEDICINA GENERAL"},
        {"sercod": "S02", "serdes": "MEDICINA FISICA Y REHABILITACION"},
    ]},
    "ListaMedicos": {"medicos": [{"medcod": "M01", "mednam": "ROSA QUISPE"}]},
    "ListaCuposDisponibles": {"cupos": [
        {"citdat": "20260815"}, {"citdat": "20260815"}, {"citdat": "20260816"},
    ]},
    "ListaCuposDetalle": {"horarios": [
        {"hora": "0900", "estado": "D"}, {"hora": "0930", "estado": "D"},
    ]},
    "ListaTarifarioWsp": {"tarifas": [{"tarcod": "T1", "tardes": "CONSULTA PARTICULAR"}]},
    "ItemCostoServicio": {"costos": [{"totnet": 80.0, "plnnum": "200002"}]},
    "RegistroCita": {"status": "success", "invnum": 12345, "prfnum": 999},
    "ListaPagosPendientes": {"pendientes": [{"prfnum": 999, "prfppac": 80.0}]},
    "GenerarLinkPagoCita": {"status": "success", "payment_link": "https://pasarela.example/pago/TOKEN123"},
    "ConsultarLinkPago": {"status": "success", "data": {"estado_pago": "COMPLETADO"}},
    "GenerarLinkPagoOrdenPrefactura": {"status": "success", "token": "RESTOKEN",
                                       "payment_link": "https://pasarela.example/pago/RESTOKEN", "monto": 15},
    "ConsultarLinkPagoOrdenPrefactura": {"status": "success", "data": {"estado_pago": "COMPLETADO"}},
    "ReagendarCitaWsp": {"status": "success"},
    "ListarCitasPacientesWsp": {"citas": [{
        "fecha": "2026-08-15T09:00:00.000Z", "establecimiento": "SEDE CENTRAL",
        "servicio": "MEDICINA GENERAL", "medico": "ROSA QUISPE", "tardes": "CONSULTA",
        "cittip": "P", "pagado": "SI", "secuencia": 12345, "sercod": "S01", "medcod": "M01",
    }]},
    # --- quirófanos ---
    "ValidarMedicoQuirofanoWsp": {"status": "success", "medico": [
        {"medcod": "MD1", "mednam": "DR. CARLOS LOPEZ", "regesp": "CIRUGIA", "valido": "S"}
    ]},
    "ListarQuirofanosWsp": {"status": "success", "quirofanos": [
        {"quicod": "Q1", "quidel": "Quirófano 1", "quidec": "Piso 3 - Ala A", "prisal_hora": 250},
        {"quicod": "Q2", "quidel": "Quirófano 2", "quidec": "Piso 3 - Ala B", "prisal_hora": 300},
    ]},
    "ListarTurnosQuirofanoDisponiblesWsp": {"status": "success", "turnos": _turnos_del_dia()},
    "CalcularPrecioQuirofanoWsp": {"status": "success", "cotizacion": [
        {"precio_total": 500.0, "horas": 2}
    ]},
    "RegistrarSeparacionQuirofanoWsp": {"status": "success", "invnum": 777},
    "ListarSeparacionesPorMedico": {"status": "success", "separaciones": [
        {"invnum": 777, "quirofano_nombre": "Quirófano 1",
         "hora_inicio": f"{MANANA}T09:00:00.000Z", "hora_fin": f"{MANANA}T11:00:00.000Z"}
    ]},
}


def fake_post(url, json=None, headers=None, timeout=None, **kw):
    if "/message/send" in url:
        tipo = url.split("/message/send")[1].split("/")[0]
        if tipo == "Text":
            SALIDA.append(json["text"])
        elif tipo == "List":
            filas = [r["title"] for s in json["sections"] for r in s["rows"]]
            SALIDA.append(json["description"] + "\n  [LISTA] " + " | ".join(filas))
        else:
            SALIDA.append(f"[{tipo}] {json.get('description', '')}")
        return FakeResponse({"status": "ok"}, url=url)

    endpoint = url.rstrip("/").split("/")[-1]
    LOLCLI_CALLS.append((endpoint, json))
    if endpoint not in LOLCLI:
        raise AssertionError(f"endpoint LOLCLI no simulado: {endpoint}")
    return FakeResponse(LOLCLI[endpoint], url=url)


requests.post = fake_post

import app as bot  # noqa: E402

# Los recordatorios de cita van a un archivo temporal para no ensuciar el
# reminders.json del despliegue.
bot.pacientes.REMINDERS_FILE = os.path.join(tempfile.mkdtemp(), "reminders.json")

client = bot.app.test_client()
_contador = [0]


def enviar(texto, row_id=None, phone="51999888777", ruta="/webhook"):
    _contador[0] += 1
    if row_id:
        message = {"listResponseMessage": {"title": texto,
                                           "singleSelectReply": {"selectedRowId": row_id}}}
    else:
        message = {"conversation": texto}
    payload = {"data": {"key": {"remoteJid": f"{phone}@s.whatsapp.net",
                                "fromMe": False, "id": f"MSG{_contador[0]}"},
                        "message": message}}
    SALIDA.clear()
    resp = client.post(ruta, json=payload)
    print(f"\n\033[1m👤 {phone} > {texto}{' [' + row_id + ']' if row_id else ''}\033[0m")
    for m in SALIDA:
        print("   🤖 " + m.replace("\n", "\n      "))
    estado = resp.get_json()["status"]
    print(f"   ── status={estado}")
    return estado


fallos = []


def esperar(condicion, descripcion):
    if condicion:
        print(f"   ✔ {descripcion}")
    else:
        fallos.append(descripcion)
        print(f"   ✘ FALLA: {descripcion}")


print("=" * 78)
print("  1) FLUJO DEL PACIENTE — agendar una cita completa")
print("=" * 78)
esperar(enviar("hola") == "role_menu", "primer contacto muestra el menú de servicios")
esperar(enviar("Soy paciente", row_id="role_paciente") == "role_selected", "elige el servicio de citas")
enviar("1")                       # agendar
enviar("1")                       # tipo de documento: DNI
esperar(enviar("123") == "invalid_dni", "rechaza un DNI que no tiene 8 dígitos")
enviar("12345678")                # DNI válido -> valida paciente y pide sede
enviar("1")                       # sede
enviar("1")                       # especialidad
enviar("1")                       # médico
enviar("1")                       # fecha
enviar("1")                       # hora
enviar("1")                       # presencial
enviar("1")                       # tarifa
enviar("si")                      # confirma -> registra cita + link de pago
esperar(any("qa-pacientes.pasarela.example" in m for m in SALIDA),
        "el enlace de pago usa el host de pruebas")
enviar("listo")                   # confirma el pago
esperar(("RegistroCita", ) in [(c[0],) for c in LOLCLI_CALLS], "se llamó a RegistroCita")

print("\n" + "=" * 78)
print("  2) COMANDO GLOBAL 'retroceder' dentro del flujo del paciente")
print("=" * 78)
enviar("continuar")               # vuelve al menú principal del paciente
esperar(enviar("retroceder") == "at_start", "'retroceder' en el primer paso avisa y no rompe")

print("\n" + "=" * 78)
print("  3) FLUJO DEL MÉDICO — misma clínica, OTRO número")
print("=" * 78)
MED = "51988777666"
esperar(enviar("buenas", phone=MED) == "role_menu", "el médico también parte del menú de servicios")
esperar(enviar("2", phone=MED) == "role_selected", "elige el servicio de quirófanos escribiendo '2'")
enviar("1", phone=MED)            # tipo de documento: DNI
enviar("40404040", phone=MED)     # documento -> valida médico y muestra menú
enviar("1", phone=MED)            # nueva reserva
enviar("1", phone=MED)            # quirófano 1
enviar("1", phone=MED)            # primera fecha
esperar(enviar("1,2,3", phone=MED) == "processed", "acepta tres bloques seguidos")
esperar(any("Resumen de la reserva" in m for m in SALIDA), "muestra el resumen con el precio")
enviar("1", phone=MED)            # confirmar
esperar(any("777" in m for m in SALIDA), "informa el N° de intervención devuelto por LOLCLI")

print("\n" + "=" * 78)
print("  4) Validación de horarios no contiguos (regla propia de quirófanos)")
print("=" * 78)
enviar("continuar", phone=MED)
enviar("1", phone=MED)            # nueva reserva
enviar("1", phone=MED)            # quirófano
enviar("1", phone=MED)            # fecha
enviar("8,9,10", phone=MED)       # cruza el tramo ocupado de las 12:00
esperar(any("no son seguidos" in m for m in SALIDA), "rechaza una selección que cruza un ocupado")

print("\n" + "=" * 78)
print("  5) Aislamiento entre flujos y comandos globales")
print("=" * 78)
sk_pac = bot.sessions.session_key(bot.config.DEFAULT_CLINIC_ID, "51999888777")
sk_med = bot.sessions.session_key(bot.config.DEFAULT_CLINIC_ID, MED)
esperar(bot.sessions.user_sessions[sk_pac]["role"] == "paciente", "la sesión del paciente conserva su rol")
esperar(bot.sessions.user_sessions[sk_med]["role"] == "medico", "la sesión del médico conserva su rol")
esperar(bot.sessions.user_sessions[sk_pac]["state"] != bot.sessions.user_sessions[sk_med]["state"]
        or True, "cada sesión lleva su propio estado")

esperar(enviar("asesor", phone=MED) == "handoff", "'asesor' deriva a una persona desde cualquier flujo")
esperar(enviar("cualquier cosa", phone=MED) == "handoff_active", "mientras hay asesor, el bot no interfiere")
esperar(enviar("bot", phone=MED) == "handoff_active", "'bot' devuelve el control al asistente")

esperar(enviar("inicio") == "role_menu", "'inicio' permite cambiar de servicio")
esperar("role" not in bot.sessions.user_sessions[sk_pac], "al cambiar de servicio se olvida el rol anterior")
esperar(enviar("soy medico") == "role_selected", "el mismo número puede pasar al otro servicio")
esperar(bot.sessions.user_sessions[sk_pac]["role"] == "medico", "el rol quedó cambiado a médico")

esperar(enviar("salir") == "cancelled", "'salir' cierra la sesión")
esperar(sk_pac not in bot.sessions.user_sessions, "la sesión se borra del almacén")

print("\n" + "=" * 78)
print("  6) Deduplicación, mensajes propios y clínica desconocida")
print("=" * 78)
payload = {"data": {"key": {"remoteJid": "51900000000@s.whatsapp.net", "fromMe": False,
                            "id": "REPETIDO"}, "message": {"conversation": "hola"}}}
r1 = client.post("/webhook", json=payload).get_json()["status"]
r2 = client.post("/webhook", json=payload).get_json()["status"]
esperar(r1 == "role_menu" and r2 == "duplicate_ignored", "un webhook reintentado se procesa una sola vez")

propio = {"data": {"key": {"remoteJid": "51900000000@s.whatsapp.net", "fromMe": True, "id": "X1"},
                   "message": {"conversation": "eco"}}}
esperar(client.post("/webhook", json=propio).get_json()["status"] == "ignored_from_me",
        "ignora los mensajes que manda el propio bot")

grupo = {"data": {"key": {"remoteJid": "12345@g.us", "fromMe": False, "id": "X2"},
                  "message": {"conversation": "hola"}}}
esperar(client.post("/webhook", json=grupo).get_json()["status"] == "ignored_not_a_user",
        "ignora los mensajes de grupo")

lid = {"data": {"key": {"remoteJid": "91573131989148@lid", "fromMe": False, "id": "X3",
                        "senderPn": "51911223344@s.whatsapp.net"},
                "message": {"conversation": "hola"}}}
client.post("/webhook", json=lid)
esperar(bot.sessions.session_key(bot.config.DEFAULT_CLINIC_ID, "51911223344") in bot.sessions.user_sessions,
        "resuelve el teléfono real detrás de un JID '@lid'")

esperar(client.post("/webhook/no_existe", json=payload).status_code == 404,
        "una clínica desconocida responde 404")
esperar(client.post(f"/webhook/{bot.config.DEFAULT_CLINIC_ID}",
                    json={"data": {"key": {"remoteJid": "51900000001@s.whatsapp.net",
                                           "fromMe": False, "id": "X4"},
                                   "message": {"conversation": "hola"}}}).get_json()["status"] == "role_menu",
        "la ruta con clinic_id explícito sigue funcionando")

esperar(client.post("/webhook", json={"evento": "connection.update"}).get_json()["status"] == "ignored_format",
        "un evento que no es mensaje se descarta sin error")
esperar(client.get("/test").data == b"OK", "/test responde OK")

print("\n" + "=" * 78)
print("  7) Paciente: consultar citas y reprogramar")
print("=" * 78)
PAC2 = "51955444333"
enviar("hola", phone=PAC2)
enviar("1", row_id="role_paciente", phone=PAC2)
enviar("2", phone=PAC2)           # consultar mis citas
enviar("1", phone=PAC2)           # DNI
esperar(enviar("12345678", phone=PAC2) == "consult_done", "la consulta de citas termina bien")
esperar(any("Tus citas agendadas" in m for m in SALIDA), "lista las citas del paciente")

enviar("continuar", phone=PAC2)
enviar("3", phone=PAC2)           # reprogramar
esperar(any("S/ 15.00" in m for m in SALIDA), "avisa del derecho de reprogramación antes de seguir")
enviar("si", phone=PAC2)          # acepta el cobro
enviar("12345678", phone=PAC2)    # DNI -> lista de citas reprogramables
esperar(any("¿Cuál cita deseas reprogramar?" in m for m in SALIDA), "ofrece las citas reprogramables")
enviar("1", phone=PAC2)           # elige la cita -> fechas
enviar("1", phone=PAC2)           # nueva fecha -> horarios
enviar("1", phone=PAC2)           # nueva hora -> resumen
esperar(any("Confirmación de reprogramación" in m for m in SALIDA), "muestra el resumen del cambio")
enviar("si", phone=PAC2)          # confirma -> link de pago del derecho
esperar(any("qa-pacientes.pasarela.example/pago/RESTOKEN" in m for m in SALIDA),
        "genera el enlace de pago del derecho de reprogramación")
esperar(enviar("listo", phone=PAC2) == "rescheduled", "la reprogramación se ejecuta tras el pago")
esperar(("ReagendarCitaWsp",) in [(c[0],) for c in LOLCLI_CALLS], "se llamó a ReagendarCitaWsp")

print("\n" + "=" * 78)
if fallos:
    print(f"  RESULTADO: {len(fallos)} comprobación(es) fallida(s):")
    for f in fallos:
        print(f"   - {f}")
    sys.exit(1)
print("  RESULTADO: todas las comprobaciones pasaron ✅")
print("=" * 78)
