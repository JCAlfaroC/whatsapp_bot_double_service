# Plan de calidad y robustez

Estado del bot frente a lo que se le exige a software que toca historias
clínicas, agenda de quirófanos y cobros. Sale de una auditoría de los dos
flujos (`flows/pacientes.py`, `flows/quirofanos.py`) y del núcleo compartido.

El orden no es por esfuerzo: es por **qué pasa si no se arregla**. En un
sistema clínico, un fallo que borra una conversación es una molestia; uno que
duplica una cita o cobra sin reprogramar es un problema que alguien tiene que
resolver a mano, paciente por paciente.

Nada de esto cambia el flujo de conversación: son las mismas pantallas.

---

## Ya hecho · primera tanda (`dbcb9fb`)

| | Qué era | Qué es |
|---|---|---|
| Fallos no previstos | 7 llamadas sin `try/except` subían a Flask → HTTP 500, el usuario no recibía **nada** y Evolution reintentaba | red de seguridad en `app.py`: se registra el traceback, se responde al usuario y se devuelve 200 |
| Diagnóstico | un timeout y un 404 daban la **misma** frase | transitorio vs. permanente, con latencia y payload en el log |
| Envíos | `send_whatsapp_message` no devolvía nada: un fallo era indistinguible de un envío correcto | devuelve `True/False` y reintenta una vez ante fallos transitorios |
| Cita duplicada | reintentar tras un fallo de cobro grababa una **segunda cita** | guardia por `invnum_cita` |
| Verificación | no había forma de saber qué dependencia falla | `diagnostico.py` prueba cada endpoint y la instancia de Evolution |

---

## Ya hecho · segunda tanda (integridad clínica)

Los cinco puntos que estaban marcados como P0 quedan cerrados. Ninguno cambió
una sola pantalla del flujo: cambian lo que el bot hace cuando algo falla.

| | Qué era | Qué es |
|---|---|---|
| Horarios inventados | Ante cualquier fallo mostraba una lista fija y el paciente podía reservar un cupo inexistente | No se ofrecen horarios que no se puedan respaldar; se distingue "no pudimos consultar" de "ese día está lleno" |
| Cita creada informada como fallida | Un fallo al consultar el importe decía "error al registrar tu cita" y cerraba la sesión, borrando también la ALERTA | El cobro tiene su propio manejo: se le da el número de reserva, la sesión queda viva y la ALERTA se emite |
| Reserva de quirófano ambigua | Un corte de red invitaba a reintentar, con riesgo de duplicar la separación | Sólo se invita a reintentar cuando consta que NO se grabó; si no, se manda a comprobar en "Mis reservas" |
| Reprogramación cobrada sin aplicar | "Error al verificar tu pago o al reprogramar", que se lee como que quizá no se cobró | Se afirma que el cobro pasó y se deriva sólo el cambio de fecha |
| Datos personales en el log | Documentos, historia clínica y nombres en claro | Documentos truncados (`****3001`), nombres e historia ocultos; el resto del payload intacto |

Sigue pendiente, y no es técnico: fijar la **retención** de `out.log` y
`error.log` y quién tiene acceso al servidor.


## P0 — Lo único que queda abierto

### `retroceder` que falla corrompe el historial

`flows/pacientes.py`, en el bloque que atiende 'retroceder'. El estado se
retrocede **antes** de repintar la pantalla. Si la llamada a LOLCLI de ese
repintado falla, el usuario no recibe nada y el historial ya se consumió: el
siguiente `retroceder` salta dos pasos.

Con la red de seguridad de `app.py` el usuario ya recibe un mensaje en vez de
silencio, así que el síntoma es mucho menor que antes, pero el historial sigue
quedando descuadrado.

**Arreglo:** consumir el historial sólo si el repintado salió bien.

---

## P1 — Poder diagnosticar sin adivinar

### 7. Identificador de conversación en todo el log

Hoy el número de teléfono aparece en la línea de `app.py`, pero las líneas de
LOLCLI (`core/lolcli.py`) y de Evolution (`core/messaging.py`) no lo llevan.
Para reconstruir qué le pasó a un paciente concreto hay que cruzar marcas de
tiempo entre módulos.

**Arreglo:** un id corto por mensaje entrante, propagado a todas las líneas.

### 8. `preload_lists` se calla ante respuestas no-2xx

`flows/pacientes.py` 133-158: sólo registra excepciones de red. Si LOLCLI
responde 500, no se imprime nada y el arranque parece correcto salvo por dos
líneas que faltan. Es exactamente el síntoma que hizo perder tiempo en el
despliegue.

### 9. Reintentos en lecturas idempotentes

`core/lolcli.py` hace un solo intento. Listar sedes, médicos o quirófanos son
lecturas: un reintento con espera corta convierte un paquete perdido en nada,
en vez de en un error para el usuario. **No** aplicar a `RegistroCita`,
`ReagendarCitaWsp` ni `RegistrarSeparacionQuirofanoWsp`.

### 10. `/test` que compruebe algo

Hoy `/test` devuelve `OK` mientras el proceso viva, aunque LOLCLI lleve horas
caído. Debería reportar el estado de las dependencias, que es lo que
`diagnostico.py` ya sabe hacer.

---

## P2 — Escala y mantenimiento

| | Dónde | Qué |
|---|---|---|
| 11 | `core/sessions.py` | Las sesiones viven en RAM: un reinicio corta toda conversación en curso, en silencio. Es también lo que impide correr más de un proceso. Mover a Redis es el paso obligado antes de escalar. |
| 12 | `core/sessions.py` 114-128 | `_session_locks` crece con cada número atendido y nunca se poda. |
| 13 | `core/sessions.py` 96-111 | El dedup usa `list.pop(0)`, O(n) en cada mensaje una vez lleno; un `deque` es O(1). |
| 14 | `core/sessions.py` 151-201 | El hilo de limpieza lee y borra sesiones **sin** tomar el lock de conversación que sí toma el webhook. Ventana pequeña, pero es una carrera real. |

---

## Cómo verificar

1. `python diagnostico.py` — todas las dependencias en OK.
2. `python smoke_test.py` — los dos flujos de punta a punta contra un doble.
3. Desde el teléfono: paciente (agendar · consultar · reprogramar) y médico
   (documento · quirófano · reserva).
4. Prueba de caída: apagar la ruta a LOLCLI y comprobar que **ningún** paso
   deja al usuario sin respuesta.

## Qué NO hay que tocar

Los estados, los payloads de LOLCLI y los textos de cada flujo. Los `TODO`
sobre campos fijos (`prgori`, `plnnum`, `usenam`) están anotados con lo que ya
se probó y por qué se revirtió: no son descuidos.
