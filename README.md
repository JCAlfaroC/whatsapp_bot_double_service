# Bot WhatsApp de doble servicio — LOLIMSA

Un solo número de WhatsApp que atiende dos públicos distintos:

| Servicio | Quién lo usa | Qué resuelve |
|---|---|---|
| **Citas (ARIE)** | Pacientes | Agendar, consultar y reprogramar citas, con su cobro |
| **Quirófanos** | Médicos | Reservar sala de operaciones por bloques de 30 minutos |

Sustituye a los dos bots que corrían por separado, cada uno con su propio
número: `whatsapp_bot_ariel` (pacientes) y `whatsapp_bot_quirofano` (médicos).

---

## Cómo se decide a quién atiende cada mensaje

No hay forma de saber por el número de teléfono si quien escribe es paciente o
médico — LOLCLI identifica a los médicos por documento, no por celular — así que
se le pregunta la primera vez y la respuesta queda guardada en su sesión:

```
mensaje ─► /webhook ─► descarta duplicados ─► lock de la conversación
                                                     │
                                              comandos globales
                                       (salir · asesor · inicio)
                                                     │
                                        ¿tiene rol la sesión?
                                       ┌─────────────┴─────────────┐
                                       no                          sí
                                       │                           │
                              menú de servicios          ┌─────────┴─────────┐
                              (1 paciente / 2 médico)    ▼                   ▼
                                                  flows/pacientes.py  flows/quirofanos.py
```

El rol dura lo que dure la sesión. Para cambiar de servicio sin esperar a que
expire, el usuario escribe **`inicio`**.

Despachar por rol —en lugar de fusionar las dos máquinas de estados en una— es
lo que permitió traer ambos flujos **sin reescribirlos**: los dos tienen un
`AWAITING_MAIN_MENU`, un `AWAITING_POST_FLOW` y un `AWAITING_CONFIRMATION` que
significan cosas distintas, pero como nunca se evalúan en el mismo despacho, no
chocan.

## Comandos globales

Funcionan en cualquier paso de cualquiera de los dos flujos:

| Comando | Efecto |
|---|---|
| `salir` / `cancelar` | Cierra la sesión y descarta el trámite en curso |
| `asesor` / `ayuda` | Deriva a una persona y avisa al teléfono de soporte |
| `bot` (durante la derivación) | Devuelve el control al asistente |
| `inicio` / `cambiar` | Vuelve al menú de servicios (cambia de rol) |
| `retroceder` | Vuelve un paso atrás — **lo resuelve cada flujo**, porque cada uno lleva su propio historial |

---

## Estructura

```
app.py                  Webhook único, comandos globales, menú de roles y despacho
config.py               clinics.json + .env; contexto por clínica en `g`
core/
  messaging.py          Evolution API: texto, listas y botones; lectura del webhook
  sessions.py           Sesiones en RAM, locks por conversación, dedup, expiración
  utils.py              Texto, fechas, menús numerados, resolución de opciones
  lolcli.py             Cliente LOLCLI (contrato status/message) y armado de URLs
flows/
  pacientes.py          Flujo ARIE completo (agendar · consultar · reprogramar)
  quirofanos.py         Flujo de quirófanos completo (auth · reserva · mis reservas)
clinics.example.json    Plantilla de configuración por clínica (copiar a clinics.json)
smoke_test.py           Prueba de humo de los dos flujos, sin red
```

---

## Configuración

### `clinics.json`

Una entrada por clínica. La clave es el `clinic_id` que aparece en la URL del
webhook. **`clinics.json` no se versiona** —lleva el token de LOLCLI en claro—;
en el repositorio está `clinics.example.json`, que se copia y se completa:

```bash
cp clinics.example.json clinics.json
```

```json
{
  "<clinic_id>": {
    "lolcli_url": "http://<host>:<puerto>/LolcliApi/api",
    "lolcli_url_pacientes": "http://<host>:<puerto>/LolcliApi/api",
    "lolcli_url_quirofanos": "http://<host>:<puerto>/LolcliApi/api",
    "lolcli_token": "<base64 de usuario:clave>",
    "lolcli_entidad": "<código de entidad>",
    "evolution_instance": "<nombre de la instancia>",
    "default_siscod": 1,
    "staff_phone": "<teléfono de soporte, con código de país>",
    "support_email": "<correo de soporte>",
    "support_hours": "Lunes a Viernes, 8am - 6pm"
  }
}
```

Los dos flujos pueden apuntar a **servidores o puertos distintos de LOLCLI**.
Si sólo se define `lolcli_url`, ambos usan ese. Los valores reales de cada
despliegue viven en su propio `clinics.json`, que no se versiona.

Cualquier clave que falte se completa desde el `.env` (ver `.env.example`). Si
no existe `clinics.json`, se arma una única clínica `default` enteramente desde
el `.env`, que es como se desplegaba el bot de pacientes.

### Webhook en Evolution

Las dos formas de URL funcionan:

- `POST /webhook` → usa la clínica por defecto (`DEFAULT_CLINIC_ID`, o la
  primera de `clinics.json`)
- `POST /webhook/<clinic_id>` → esa clínica en concreto

Los webhooks ya dados de alta con cualquiera de las dos formas siguen sirviendo
sin cambios.

---

## Ejecución

```bash
pip install -r requirements.txt
cp .env.example .env               # completar credenciales
cp clinics.example.json clinics.json
python app.py                      # waitress en el puerto $PORT (5000 por defecto)
```

`python app.py` **es también el arranque de producción**: levanta waitress, que
es un servidor WSGI de producción. En el servidor Windows de LOLIMSA el proceso
se envuelve como servicio con NSSM, siguiendo la convención del resto de
backends (`D:\backend\<app>\`, servicios `LOLCLI_*`):

```
nssm install LOLCLI_BOT_DOBLE "D:\backend\...\venv\Scripts\python.exe" "app.py"
nssm set LOLCLI_BOT_DOBLE AppDirectory "D:\backend\..."
```

> Los tres archivos que el bot lee de disco —`.env`, `clinics.json` y
> `reminders.json`— se resuelven contra la carpeta del código (`__file__`), no
> contra el directorio de trabajo, así que ninguno depende de `AppDirectory`.
> Conviene ponerlo igual por higiene, pero no es lo que carga las credenciales.
>
> Lo que sí muerde es que `load_dotenv()` **sube por el árbol de directorios**:
> si falta el `.env` de la aplicación y hay uno en la carpeta que la contiene
> (`D:\backend\.env`), el bot arranca con las credenciales de otro backend y sin
> ninguna advertencia. Ver [DEPLOY.md](DEPLOY.md), paso 2.

El procedimiento completo de puesta en marcha en el servidor está en
[DEPLOY.md](DEPLOY.md).

> **Un solo proceso, no negociable.** Las sesiones, los locks de conversación y
> el registro de mensajes ya procesados viven en la memoria del proceso. Con más
> de uno, dos mensajes del mismo usuario pueden caer en procesos distintos y ver
> sesiones distintas. Para escalar a varios hay que mover ese estado a un
> almacén externo (Redis).

Muchos hilos sí es seguro y necesario: cada mensaje saliente espera
`SEND_PACING_SECONDS` antes de enviarse y varios pasos mandan dos o tres
mensajes, así que un solo mensaje entrante puede retener un hilo varios
segundos.

### Prueba de humo

```bash
python smoke_test.py
```

Recorre los dos flujos de punta a punta contra un doble de LOLCLI y de
Evolution. No toca la red ni manda mensajes reales, e imprime la conversación
tal como se vería en el teléfono.

Credenciales válidas no necesita, pero **sí necesita configuración**: la URL de
LOLCLI del flujo de pacientes tiene que estar definida y no vacía, venga de
`clinics.json` (`lolcli_url_pacientes` o `lolcli_url`) o del `.env`
(`LOLCLI_API_URL`). El valor da igual —la red está simulada—, pero si queda
vacío `preload_lists` corta en seco (`if not url_base: return`) sin llegar a
llamar al doble, y fallan 11 comprobaciones con `error_loading_lists`: parecen
un bot roto y son sólo configuración ausente.

En una máquina recién clonada, sin `clinics.json` todavía, basta con copiar
`.env.example` a `.env`.

---

## Tiempos de sesión

Cada rol conserva el presupuesto que tenía su bot original, porque responden a
usos distintos:

| Rol | Aviso de inactividad | Cierre |
|---|---|---|
| Paciente | cada 1 min | 3 min |
| Médico | cada 5 min | 15 min |

El paciente contesta desde el teléfono en el momento; el médico consulta su
agenda entre pacientes y necesita más margen.

> ⚠️ El presupuesto de 3 minutos del paciente corre también mientras está en la
> página de pago. Si la sesión expira con la cita ya registrada pero el pago sin
> confirmar, queda en el log una línea `ALERTA: Cita registrada sin pago
> confirmado…` con el `invnum` para cancelarla a mano en LOLCLI. Es el
> comportamiento que ya tenía el bot original; si en producción aparece seguido,
> conviene subir `SESSION_EXPIRATION_PERIOD` para los estados de pago.

---

## Qué cambió respecto de los dos bots originales

**Mejoras que un flujo tenía y ahora comparten los dos:**

| | Antes | Ahora |
|---|---|---|
| Números `@lid` | Sólo quirófanos lo resolvía; en pacientes era un TODO y el paciente no recibía respuesta | Los dos resuelven el teléfono real |
| Webhooks repetidos | Sólo quirófanos deduplicaba | Los dos, sobre el mismo registro |
| Race de mensajes seguidos | Sólo quirófanos usaba lock por conversación | Los dos |
| Respuestas por lista/botón | Pacientes sólo leía texto escrito | Los dos leen texto, botón y lista |
| Derivación a un asesor | Sólo quirófanos | Global, con el aviso a soporte adaptado al rol |
| Configuración | Pacientes leía el `.env` y servía a un solo cliente | Los dos por clínica, con respaldo en `.env` |

**Otros cambios:**

- `clinics.json` se resuelve contra la carpeta del proyecto y no contra el
  directorio de trabajo, así que ya no importa desde dónde se lance el proceso.
- El prefijo `qa-pacientes.` de los enlaces de pago pasó de estar escrito en el
  código a ser configuración (`PAGOS_QA_PREFIX`). **Para pasar a producción
  real, dejarlo vacío en el `.env`.**
- La pausa entre mensajes salientes se unificó en 1,2 s (pacientes usaba 1,5 s) y
  es configurable con `SEND_PACING_SECONDS`.
- Los recordatorios de cita guardan la instancia de Evolution, porque el hilo que
  los envía no tiene forma de deducir a qué clínica pertenecía la cita.
- Al volver al menú tras terminar un trámite, la sesión ya no se vacía del todo:
  conserva la identidad de la conversación (rol, teléfono, clínica). El bot de
  quirófanos ya hacía esto a mano con `medcod`/`mednam`; ahora está centralizado
  en `sessions.soft_reset()`.

**Lo que NO cambió:** los estados, los payloads de LOLCLI, las reglas de negocio
y los textos de cada flujo son los mismos. Toda la lógica de cita y de reserva se
portó tal cual, con sus comentarios sobre los comportamientos raros de LOLCLI
(la `Z` de las horas, el `plnnum` fijo, los nombres de endpoint que no coinciden
con el documento).

---

## Pendientes heredados

Se portaron tal cual, sin resolver, porque dependen de confirmaciones de LOLIMSA:

- **Pagos de quirófanos**: el enganche está montado pero desactivado
  (`PAGOS_HABILITADOS=0`); faltan URL y credenciales de la pasarela. Con 0, la
  reserva se graba sin cobrar.
- **Cancelar reserva de quirófano**: LOLCLI no expone endpoint de anulación; el
  bot deriva a soporte.
- **Cruces de horario en quirófanos**: `ListarTurnosQuirofanoDisponiblesWsp`
  devuelve días enteros como libres aunque haya separaciones grabadas, así que el
  cruce recién se detecta al confirmar. El bot lo detecta y devuelve al médico a
  elegir horario.
- **Flujo de reevaluación médica (pacientes)**: los estados `*_FOR_REEVAL` están
  en el código pero no son alcanzables — el menú ofrece 3 opciones y ninguna pone
  `flow="reeval"`. Para habilitarlo hay que agregar la opción al menú y confirmar
  con LOLIMSA si las exclusiones son sub-servicios o tarifas.
- **Reprogramación por tipo de cita**: la bifurcación reevaluación vs. terapia no
  está implementada; falta saber qué campo de `ListarCitasPacientesWsp` las
  distingue. Hoy se confía en que `ReagendarCitaWsp` rechace del lado del
  servidor lo que esté fuera de regla.
- Varios `TODO` de campos con valor fijo (`prgori`, `plnnum`, `usenam`, el
  `siscod` de una fila de citas) siguen anotados en el código con el detalle de
  qué se probó y por qué se revirtió.

---

## Carpetas de referencia

`whatsapp_bot_ariel/` y `whatsapp_bot_quirofano/` son las copias de los dos bots
originales de los que salió esta versión. No las usa nadie en tiempo de
ejecución y se pueden borrar una vez validado el despliegue.

