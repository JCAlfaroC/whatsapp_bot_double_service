# Despliegue en el servidor Windows

Puesta en marcha del bot de doble servicio en el servidor de LOLIMSA, con el
proceso envuelto como servicio `LOLCLI_*` con NSSM.

Sustituye a los dos bots que corrían por separado (`LOLCLI_BOT_ARIEL` y el de
quirófanos), que se retiran recién en el paso 9, una vez validado este.

| | |
|---|---|
| Carpeta | `<CARPETA>` — ver más abajo |
| Servicio | `LOLCLI_BOT_DOBLE` |
| Puerto | `5003` |
| Instancia de Evolution | `evital` |
| LOLCLI pacientes | `15.235.105.124:3001` |
| LOLCLI quirófanos | `15.235.105.124:3011` |

> ⚠️ **Dónde NO instalarlo: dentro de la carpeta de otro bot.**
>
`<CARPETA>` puede ser cualquier ruta —la convención del resto de backends es
`D:\backend\<app>\`— siempre que cumpla dos condiciones:

1. **No estar anidada dentro de la carpeta de otro bot**, por ejemplo
   `...\whatsapp_bot_quirofano\whatsapp_bot_double_service\`. El `.env` del bot
   viejo pasaría a ser un `.env` de un directorio superior y, por lo que
   explica el paso 2, se heredaría en silencio: define `EVOLUTION_API_URL`,
   `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCE_NAME` y `PORT`, suficiente para que
   el bot arranque sin una sola advertencia, en el puerto equivocado y contra
   la instancia equivocada. Además es la carpeta que se retira en el paso 9.
2. **No tener ningún `.env` por encima**, en ninguno de sus directorios
   superiores hasta la raíz de la unidad. Lo verifica el paso 2.

Una carpeta en el escritorio cumple las dos. Lo único que hay que tener
presente es que cuelga del perfil de un usuario concreto
(`C:\Users\<usuario>\Desktop\`): el servicio corre como LocalSystem y llega
igual, pero si ese perfil se renombra o se rehace el servidor, la ruta del
servicio deja de existir.

Los comandos están en **PowerShell**. Desde `cmd.exe` varios no existen
(`Test-NetConnection`, `New-NetFirewallRule`, `nssm set` sí funciona): abrir
PowerShell con `powershell` y trabajar ahí.

---

## 0. Código al día

```powershell
cd <CARPETA>
git pull
git log --oneline -1
```

Tiene que decir `92bdbcc` o algo posterior. Si dice `eef403f`, el servidor
todavía tiene la versión en la que `retroceder`, dentro del flujo de pacientes,
retrocedía dos pasos en vez de uno. Es el paso más fácil de saltarse y el más
caro de descubrir después, porque no falla: contesta de más.

## 1. Entorno virtual

```powershell
py -3 -m venv venv
venv\Scripts\pip install -r requirements.txt
```

No hace falta gunicorn: no corre en Windows y se quitó del proyecto en
`92bdbcc`. El servidor de producción es **waitress**, y lo levanta el propio
`app.py`.

> Si en algún momento hay que mover la aplicación de carpeta, **el `venv` no se
> mueve: se rehace.** Un entorno virtual lleva su propia ruta absoluta grabada
> en `pyvenv.cfg` y en los ejecutables de `Scripts\`, así que copiado a otro
> sitio deja de funcionar (o, peor, sigue apuntando al intérprete de la
> ubicación vieja). Borrar `venv\` y repetir los dos comandos de arriba.

## 2. Los dos archivos de configuración

Ni `.env` ni `clinics.json` se versionan —llevan la clave de Evolution y el
token de LOLCLI en claro—, así que `git pull` no los trae nunca. Se crean a mano
dentro de `<CARPETA>`:

- **`.env`** — con `EVOLUTION_API_KEY` copiada del `.env` del bot de pacientes
  (`whatsapp-bot-ariel`) y `PORT=5003`.
- **`clinics.json`** — con los dos puertos de LOLCLI, `3001` para pacientes y
  `3011` para médicos.

> ⚠️ **Comprobar que no haya ningún `.env` por encima de la aplicación.**
>
> Ir carpeta por carpeta con `dir ..\.env` deja huecos: la búsqueda sube hasta
> la raíz de la unidad. Lo más corto es preguntárselo a la propia librería,
> **parado dentro de `<CARPETA>`**:
>
> ```
> venv\Scripts\python.exe -c "from dotenv import find_dotenv;print(find_dotenv() or 'NINGUNO')"
> ```
>
> Tiene que devolver el `.env` de `<CARPETA>`. Si devuelve una ruta de más
> arriba, es el que se va a cargar; si devuelve `NINGUNO`, todavía no hay
> ninguno (normal antes de crearlo).
>
> `config.py` llama a `load_dotenv()` sin ruta, y python-dotenv no se limita a
> la carpeta de la aplicación: **sube por el árbol de directorios** hasta
> encontrar el primer `.env`. Si falta el de la aplicación y hay uno más arriba,
> el bot arranca con las credenciales de ese otro backend, **sin una sola
> advertencia** —las comprobaciones del paso 5 pasan igual, porque las claves
> están, sólo que son las de otro— y atiende contra la instancia y el puerto
> equivocados. Es el único fallo de esta guía que no deja rastro en el log.
>
> Ojo con comprobar la carpeta correcta: si la aplicación no está en
> `D:\backend\`, mirar `D:\backend\.env` no sirve de nada.

## 3. Pre-flight: conectividad

```powershell
Test-NetConnection 15.235.105.124 -Port 3001    # LOLCLI pacientes
Test-NetConnection 15.235.105.124 -Port 3011    # LOLCLI médicos (nuevo)
Test-NetConnection api.elolcli.com -Port 443    # Evolution
```

Los tres tienen que dar `TcpTestSucceeded : True`. El `3011` es el que nunca
existió antes: el bot de pacientes no lo usaba, así que puede estar cerrado en
el firewall de salida aunque el `3001` funcione.

`Test-NetConnection` es un cmdlet de PowerShell: desde `cmd.exe` responde «no se
reconoce como un comando interno o externo». Se abre PowerShell, o se invoca
desde cmd así:

```
powershell -Command "Test-NetConnection 15.235.105.124 -Port 3011 -InformationLevel Quiet"
```

> ⚠️ **No probar la conectividad con un bucle de sockets en Python.** Un proceso
> que abre `connect()` contra varios puertos seguidos es la firma de un escaneo
> de puertos, y el antivirus del servidor responde poniendo en cuarentena el
> binario que lo hizo: `venv\Scripts\python.exe`. El síntoma es que a partir de
> ese momento *cualquier* comando con ese intérprete —hasta un `print`— abre el
> diálogo «No se puede ejecutar esta aplicación en el equipo», porque el archivo
> ya no es un ejecutable válido. Se recupera rehaciendo el `venv` (paso 1), pero
> conviene no provocarlo: `Test-NetConnection` hace lo mismo sin disparar nada.
>
> Por la misma razón, antes de dejar el servicio corriendo conviene pedirle al
> equipo de sistemas una **exclusión de antivirus para `<CARPETA>`**: si el
> antivirus puede neutralizar el intérprete, puede hacerlo también con el
> servicio en marcha, y el modo de fallo es un servicio que no vuelve a arrancar.

## 4. Prueba de humo

**Después del paso 2, nunca antes:**

```powershell
venv\Scripts\python.exe smoke_test.py
```

Recorre los dos flujos completos contra un doble de LOLCLI y de Evolution: no
toca la red ni manda mensajes reales.

A pesar de lo que dice su propia cabecera, **no se puede correr sin
configuración**, y por eso va después del paso 2 y no antes. Necesita que la URL
de LOLCLI del flujo de pacientes esté definida; el `clinics.json` del paso 2 ya
la trae, así que a esta altura se cumple sola. Si se corre sobre un clon recién
hecho, sin `clinics.json` ni `.env`, `preload_lists` corta en seco
(`if not url_base: return`) y fallan 11 comprobaciones con
`error_loading_lists`, que parecen un bot roto y son sólo configuración ausente.

Tiene que terminar en `RESULTADO: todas las comprobaciones pasaron ✅`.

## 5. Arranque en primer plano

Antes de envolverlo como servicio, se comprueba a mano:

```powershell
venv\Scripts\python.exe app.py
```

Tiene que imprimir estas cuatro líneas:

```
INFO: 1 clínica(s) cargada(s): ['hospital_central'] (default: 'hospital_central')
INFO [hospital_central]: Se han cargado N sedes.
INFO [hospital_central]: Se han cargado N tipos de documento.
INFO: Iniciando servidor waitress en http://0.0.0.0:5003 (50 hilos)
```

| Si ves | Es |
|---|---|
| `ADVERTENCIA: falta EVOLUTION_API_URL o EVOLUTION_API_KEY` | no encontró ningún `.env` |
| `ERROR [...]: Fallo en la conexión con la API al pre-cargar listas` | no llega a LOLCLI: URL o firewall |
| **ninguna de las dos líneas del medio, y tampoco un `ERROR`** | llegó a LOLCLI y LOLCLI rechazó: token o entidad |
| `Se han cargado 0 sedes` | LOLCLI respondió bien, pero con la lista vacía: entidad o `siscod` |
| Las cuatro líneas, pero con otra instancia o puerto | cargó un `.env` de un directorio superior (ver paso 2) |

La tercera fila es la que más despista: `preload_lists` sólo imprime cuando la
respuesta viene con `ok`, y sólo atrapa errores de conexión. Si LOLCLI contesta
un 401 o un 500, no imprime **nada** —ni la línea de sedes ni un `ERROR`— y el
arranque parece correcto salvo por dos líneas que faltan. Con el token mal, el
síntoma no es un error: es un silencio.

`curl http://localhost:5003/test` → `OK`. Recién ahí, Ctrl+C.

## 6. Servicio

```powershell
nssm install LOLCLI_BOT_DOBLE "<CARPETA>\venv\Scripts\python.exe" "app.py"
nssm set LOLCLI_BOT_DOBLE AppDirectory "<CARPETA>"
nssm set LOLCLI_BOT_DOBLE AppEnvironmentExtra PYTHONUNBUFFERED=1
nssm set LOLCLI_BOT_DOBLE AppStdout "<CARPETA>\out.log"
nssm set LOLCLI_BOT_DOBLE AppStderr "<CARPETA>\error.log"
nssm set LOLCLI_BOT_DOBLE AppRotateFiles 1
nssm set LOLCLI_BOT_DOBLE AppRotateOnline 1
nssm set LOLCLI_BOT_DOBLE AppRotateBytes 10485760
nssm set LOLCLI_BOT_DOBLE AppExit Default Restart
nssm set LOLCLI_BOT_DOBLE AppRestartDelay 5000
nssm set LOLCLI_BOT_DOBLE Start SERVICE_AUTO_START
nssm start LOLCLI_BOT_DOBLE
```

`PYTHONUNBUFFERED=1` no es cosmético: sin él Python retiene la salida en el
búfer y `out.log` se queda **vacío**, incluso con el bot funcionando
perfectamente. Las cuatro líneas del paso 5 no aparecen, y la única forma de
saber si arrancó pasa a ser el `curl`.

`AppDirectory` conviene ponerlo por higiene, pero **no es lo que hace que se
carguen las credenciales**: `.env`, `clinics.json` y `reminders.json` se
resuelven todos contra la carpeta del propio código (`__file__`), no contra el
directorio de trabajo. Si algo arranca sin credenciales, la causa está en el
paso 2, no aquí.

Verificar: `sc query LOLCLI_BOT_DOBLE` → `RUNNING`,
`curl http://localhost:5003/test` → `OK`, y las mismas cuatro líneas en
`out.log`.

## 7. Firewall

```powershell
New-NetFirewallRule -DisplayName "Bot WhatsApp doble servicio - Webhook" `
  -Direction Inbound -LocalPort 5003 -Protocol TCP -Action Allow `
  -RemoteAddress <IP_DEL_SERVIDOR_EVOLUTION>
```

## 8. Webhook y prueba real

En Evolution, el webhook de la instancia `evital` →
`http://<host>:5003/webhook`.

Desde un teléfono, probar **las dos ramas**: es lo único que la prueba de humo
no cubre, porque ahí LOLCLI está simulado.

- `hola` → menú de servicios (paciente / médico)
- **paciente** → "Agendar" → la lista de sedes tiene que venir poblada (eso es
  LOLCLI `3001`)
- **médico** → documento real → tiene que aparecer la lista de quirófanos (eso
  es `3011`, el camino que nunca existió antes)
- en cualquier menú, `retroceder` → tiene que volver **un** paso, no al inicio

## 9. Retirar los viejos

Recién cuando lo anterior pasó:

```powershell
nssm stop LOLCLI_BOT_ARIEL
nssm set LOLCLI_BOT_ARIEL Start SERVICE_DEMAND_START
```

Y limpiar el webhook de la instancia vieja de quirófanos.

Dejarlos instalados-pero-detenidos unos días: el rollback es un `nssm start`.

## 10. El primer día

Buscar en `out.log`:

```
ALERTA: Cita registrada sin pago confirmado
```

Es el presupuesto de 3 minutos de la sesión del paciente venciendo mientras
todavía está en la página de pago de Niubiz. Deja la cita grabada y sin cobrar,
para cancelar a mano en LOLCLI con el `invnum` que aparece en la misma línea. Si
sale seguido, subir `SESSION_EXPIRATION_PERIOD`.

---

## Apéndice: qué archivo vive dónde

| Archivo | Se resuelve contra | ¿Lo trae `git pull`? |
|---|---|---|
| `.env` | sube por el árbol de directorios desde `config.py` | no (gitignored) |
| `clinics.json` | la carpeta del código (`__file__`) | no (gitignored) |
| `reminders.json` | la carpeta del código (`__file__`) | no, lo crea el bot |
| `out.log` / `error.log` | ruta absoluta en la config de NSSM | no |

Ninguno depende del directorio de trabajo del proceso, salvo el `.env` en el
sentido peligroso del paso 2: no lo busca en el directorio de trabajo, pero sí
en los directorios **superiores** al código.
