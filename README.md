# recordatorios-home

Recordatorios recurrentes que llegan por Telegram. Los horarios se definen en
[`reminders.yaml`](reminders.yaml), el reloj lo pone GitHub Actions y el estado
vive en Neon. Sin servidor, sin tarjeta de crédito, sin nada que se duerma.

Resuelve el caso que cron por sí solo no expresa: **turnos rotativos entre
varias personas** — el aseo del baño los lunes y jueves, tres personas que se
van alternando, y cada aviso nombrando a quien le toca.

---

## Por qué no un servidor (y por qué Render no encaja aquí)

La idea inicial —una app Python con su propio scheduler en Render— choca con
cómo funciona el plan gratuito en 2026:

| | Render free |
|---|---|
| Web service | Se apaga a los **15 minutos sin tráfico**; despertar tarda ~30-50 s |
| Cron Jobs | **No existen en free.** Son un servicio de pago |
| Background Workers | **No existen en free.** También de pago |
| Postgres free | 1 GB y **la base expira a los 30 días** |
| Horas | 750 al mes (un mes completo son 744, así que no sobra casi nada) |

Un `APScheduler` viviendo dentro de un web service gratuito simplemente no
dispara: está dormido justo cuando toca. Se puede parchar con un pinger
externo que lo despierte cada 14 minutos, pero entonces el ping ya es el
verdadero reloj — y si el reloj va a estar afuera de todos modos, el servidor
sobra.

Así que el reloj es explícito: **GitHub Actions ejecuta un tick cada 5 minutos**
y ese tick decide qué mandar. Gratis e ilimitado en repos públicos, y sin nada
que mantener despierto.

Ese intervalo de 5 minutos **no** sale de pedirle a Actions 288 arranques al día.
Se probó, y no funciona: ver [Por qué el reloj es un bucle](#por-qué-el-reloj-es-un-bucle-y-no-un-cron-de-5-minutos).
Lo pone un bucle dentro de un job largo,
[`tick-loop.yml`](.github/workflows/tick-loop.yml), que arranca 8 veces al día.

Lo que se cede a cambio:

- **Puntualidad.** Actions no garantiza la hora exacta: un job puede tardar en
  arrancar en horas pico. Para recordatorios domésticos alcanza; para algo al
  minuto exacto, no sirve.
- **Los cambios van por git.** Crear un recordatorio es editar el YAML y hacer
  push. No hay interfaz web (ver [Si algún día querés UI](#si-algún-día-querés-ui)).
- **GitHub apaga los workflows programados** de un repo con 60 días sin
  actividad. Por eso está [`keepalive.yml`](.github/workflows/keepalive.yml),
  que hace un commit trivial una vez al mes.

El tick nunca pierde un recordatorio por un retraso: no pregunta "¿toca justo
ahora?" sino "¿qué venció desde la corrida anterior?". Si Actions se saltó tres
turnos, el siguiente tick recupera lo pendiente. Y lo que ni así se recupera
—un hueco más largo que la ventana— se avisa por Telegram en vez de perderse
en silencio.

---

## Por qué el reloj es un bucle y no un cron de 5 minutos

El diseño original era un solo workflow con `cron: "*/5 * * * *"`. En agosto de
2026 dejó de llegar el recordatorio del almuerzo, y las corridas de Actions
estaban **todas en verde**. El problema no era el código: era que GitHub había
dejado de arrancarlas.

Corridas reales del workflow `tick`, contra las 288 diarias que pide el cron:

| día | corridas | del cron |
|---|---|---|
| 16–23 ago | 37–58 | ~15 % |
| 26 ago | 24 | 8 % |
| 27 ago | 3 | 1 % |
| 28 ago | 1 | 0,3 % |

GitHub retrasa y **descarta** corridas `schedule`, y castiga especialmente los
crons de alta frecuencia en repos sin actividad reciente. La degradación empezó
justo después del último push y se aceleró hasta dejar huecos de 10 horas —
más anchos que la ventana de recuperación, así que las ocurrencias que caían
adentro no entraban en ninguna corrida y desaparecían sin dejar rastro.

La salida es no depender de que Actions arranque seguido:
[`tick-loop.yml`](.github/workflows/tick-loop.yml) pide **8 arranques al día**
en vez de 288, y el intervalo de 5 minutos lo pone un bucle `tick; sleep 300`
dentro del job. Un arranque que llega media hora tarde ya no pierde nada: solo
desplaza el comienzo de su bloque.

Los bloques son de 3 h, no de 6 (que es el tope duro de un job), porque un
arranque que Actions no honre se lleva el bloque entero: el largo del bloque
**es** el tamaño del peor hueco posible. Con 3 h se pierde la mitad que con 6 y
cuesta las mismas horas de runner — solo son más `pip install`, que con la
caché de pip son segundos.

[`tick.yml`](.github/workflows/tick.yml) se queda con su `*/5` como red de
respaldo —las corridas sueltas que sí arrancan pueden tapar un bloque que no
haya arrancado— y como consola de mando a mano. Que los dos coincidan no
duplica mensajes: `store.claim()` es un upsert atómico y solo una corrida se
queda con cada ocurrencia.

---

## Cómo se expresan los lunes alternos

Cron sabe decir "los lunes y jueves", pero no sabe decir "y le toca a la
siguiente persona de la lista". Eso lo resuelve `rotation`.

```yaml
- id: bano-manana
  cron: "0 6 * * 1,4"        # lunes y jueves a las 6:00
  rotation:                  # se turnan en este orden
    - "${PERSONA_1}"
    - "${PERSONA_2}"
    - "${PERSONA_3}"
  anchor: 2026-08-03         # el primer turno es de PERSONA_1
  message: "🧽 Buenos días {turno}, el baño te espera."
```

Los nombres entran por `${PERSONA_n}` porque este repo es público — ver
[Puesta en marcha](#3-el-repositorio). Si los tuyos no son sensibles, podés
escribirlos literales: `rotation: [Ana, Beto, Carla]`.

**El turno avanza una vez por día de disparo, no por semana ni por aviso.** Con
dos días de aseo y tres personas, el ciclo completo dura tres semanas:

|  | lunes | jueves |
|---|---|---|
| semana 1 | Ana | Beto |
| semana 2 | Carla | Ana |
| semana 3 | Beto | Carla |

Que el turno se cuente por día es lo que permite tener dos avisos el mismo día
—uno a las 6:00 y otro a las 18:00— nombrando a la misma persona. Para eso los
dos recordatorios tienen que compartir `rotation`, `anchor` y los mismos días
en el cron; si divergen, cada uno contaría su propia secuencia.

En los mensajes podés usar `{turno}` (a quién le toca) y `{siguiente}` (quién
viene después).

### Mensajes rotativos

Un chiste repetido cada semana deja de ser chiste. `message` acepta una lista y
los textos se van turnando con el mismo índice que las personas:

```yaml
  message:
    - "🧽 {turno}, hoy sos vos."
    - "🚿 El baño llama a {turno}. Mañana le toca a {siguiente}."
    - "✨ {turno}, tu momento de brillar (y de hacer brillar el baño)."
```

Como las dos listas suelen tener largos distintos, las combinaciones tardan en
repetirse: 3 personas × 5 mensajes son 15 turnos antes de volver al mismo par.

### La alternativa de bajo nivel

`every_weeks` + `week_offset` filtran *qué semanas* cuentan, sin nombrar a
nadie. Sirve cuando querés algo cada dos semanas sin turnos de personas:

```yaml
  cron: "0 7 * * 1"
  every_weeks: 2       # 1 de cada 2 semanas...
  week_offset: 0       # ...la "par", contando desde el anchor
  anchor: 2026-08-03
```

Los dos mecanismos se pueden combinar: el filtro de semanas decide si el
recordatorio dispara, y la rotación decide a quién nombra.

### Comprobalo antes de subir nada

```bash
python -m recordatorios agenda --days 21
```

```
Próximos 21 días — 12 ejecución(es)

  2026-08-03 06:00 -05 (Mon)       bano-manana  →  Ana
  2026-08-03 18:00 -05 (Mon)       bano-tarde   →  Ana
  2026-08-06 06:00 -05 (Thu)       bano-manana  →  Beto
  2026-08-06 18:00 -05 (Thu)       bano-tarde   →  Beto
  2026-08-10 06:00 -05 (Mon)       bano-manana  →  Carla
  ...
```

---

## Puesta en marcha

### 1. El bot de Telegram

1. Escribile a [@BotFather](https://t.me/BotFather) → `/newbot` → seguí los pasos.
2. Guardá el token que te da (`123456789:AA...`).
3. Mandale un mensaje cualquiera a tu bot desde el chat donde querés recibir los
   recordatorios (si es un grupo, agregá el bot al grupo primero).
4. Averiguá el `chat_id` abriendo en el navegador:
   `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y buscá `"chat":{"id":...}`.
   Los chats privados dan un número positivo; los grupos, uno negativo.

### 2. La base de datos (Neon)

Los runners de Actions son efímeros: no recuerdan qué se envió. Ese estado va a
Neon, que tiene un plan gratuito sin caducidad ni tarjeta.

1. Creá una cuenta en [neon.tech](https://neon.tech) y un proyecto
   (`recordatorios-home`).
2. Copiá la cadena de conexión (`postgresql://...?sslmode=require`).
3. Las tablas se crean solas en el primer envío real (no en cada tick); si
   querés adelantarlo: `python -m recordatorios init-db`.

> El Postgres gratuito de Render **no** sirve para esto: expira a los 30 días.

### 3. El repositorio

```bash
git init
git add .
git commit -m "recordatorios-home"
gh repo create recordatorios-home --public --source=. --push
```

Repo **público**: los minutos de Actions son ilimitados ahí, y en privado se
consumirían los 2 000 mensuales. No hay secretos en el código — token, chat y
base de datos van en GitHub Secrets.

En *Settings → Secrets and variables → Actions*, creá tres secrets:

| Secret | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | El token de BotFather |
| `TELEGRAM_CHAT_ID` | El id del chat destino |
| `DATABASE_URL` | La cadena de conexión de Neon |
| `PERSONA_1` … `PERSONA_4` | Los nombres de las personas |

Los nombres van como secrets a propósito: **el repo es público**, así que
`reminders.yaml` los referencia como `${PERSONA_1}` en vez de escribirlos. Y al
ser secrets, GitHub los enmascara también en los logs de Actions, donde si no
aparecerían en cada línea de envío (`turno de …`).

Los nombres de los secrets son genéricos por lo mismo: `PERSONA_1` no dice
nada, mientras que un secret con el nombre de pila de alguien delataría
justamente lo que intenta ocultar.

### 4. Comprobar que quedó bien

En la pestaña *Actions* → workflow **tick** → *Run workflow*. El desplegable
*Qué ejecutar* trae cuatro opciones, y las dos primeras no envían nada:

| Opción | Qué hace |
|---|---|
| `check` | Revisa las cuatro cosas que tienen que estar bien: el YAML, que el token sirva, que el bot alcance cada chat y que la base conecte. Es el diagnóstico completo |
| `dry-run` | Muestra qué se enviaría en este instante |
| `history` | Los últimos 50 envíos registrados, con su estado (`sent`, `failed`, `stale`). Es lo que hay que mirar cuando un recordatorio no llegó |
| `tick` | La corrida normal, igual a la del cron |
| `descartar` | Da por perdida una ocurrencia (pegá la referencia del dashboard) |
| `send-test` | Manda un recordatorio ya mismo. Poné su `id` en el campo de abajo. Usa el turno de la ocurrencia más cercana —la de hoy si ya disparó— y te dice cuál imitó |

Empezá por `check`. Si sale todo en `[OK ]`, terminás con `send-test` para ver
un mensaje real en el grupo. A partir de ahí el tick corre solo cada 5 minutos.

```
[OK ] reminders.yaml — 6 recordatorios, 6 activos
[OK ] Token de Telegram — el bot es @tu_bot
[OK ] Chat -100xxxxxxxxxx — alcanzable (supergroup: La casa)
[OK ] Base de datos (postgres) — conecta y el esquema está listo

Todo en orden. Lo próximo que va a pasar:
  2026-08-03 06:00 -05 (Mon)       bano-manana  →  Ana
  2026-08-03 07:00 -05 (Mon)       basura-lun-mie-manana  →  Carla
```

---

## Uso diario

Crear o cambiar un recordatorio es editar `reminders.yaml` y hacer push. El CI
valida el archivo en cada push, así que un typo no llega a producción.

### Campos disponibles

| Campo | Obligatorio | Descripción |
|---|---|---|
| `id` | sí | Identificador estable, minúsculas. Cambiarlo hace que el historial empiece de cero |
| `cron` | sí | 5 campos, evaluado en `timezone` |
| `message` | sí | Texto a enviar, o una lista de textos que se van turnando. Admite HTML de Telegram y los marcadores `{turno}` / `{siguiente}` |
| `rotation` | no | Lista de personas que se turnan, un turno por día de disparo. Exige `anchor` y que algún mensaje use `{turno}` |
| `name` | no | Nombre legible (por defecto, el `id`) |
| `timezone` | no | Zona IANA. Por defecto `America/Bogota` |
| `chat_id` | no | Chat destino. Normalmente se hereda de `defaults` |
| `enabled` | no | `false` para pausarlo sin borrarlo |
| `every_weeks` | no | Dispara 1 de cada N semanas. Por defecto 1 |
| `week_offset` | no | Cuál de esas N semanas (0 … N-1) |
| `anchor` | no | Fecha del primer turno, y semana 0 para `every_weeks` (para eso se normaliza a su lunes) |
| `starts_on` / `ends_on` | no | Límites de vigencia (`AAAA-MM-DD`) |
| `max_delay_minutes` | no | Si queda más atrasado que esto, se descarta en vez de llegar a destiempo (y queda anotado como `stale`, con aviso al chat). Por defecto 120, pero conviene fijarlo por recordatorio — ver [Cuánto puede llegar tarde](#cuánto-puede-llegar-tarde-cada-recordatorio) |
| `parse_mode` | no | `HTML`, `Markdown`, `MarkdownV2` o `none` |
| `silent` | no | `true` envía sin notificación sonora |
| `poll` | no | Convierte el aviso en encuesta — ver [Encuestas](#encuestas-para-que-alguien-confirme) |

`defaults` acepta los mismos campos salvo `id`, `name`, `cron`, `message`,
`rotation`, `every_weeks` y `week_offset`.

### Encuestas, para que alguien confirme

Un recordatorio puede llegar como encuesta en vez de como mensaje: el texto
pasa a ser la pregunta y `poll.options` son las respuestas.

```yaml
  - id: basura-lun-mie-manana
    cron: "0 6 * * 1,3"
    poll:
      options:
        - "✅ Sí, yo la saco"
        - "🕒 Sí, antes de que pase el camión"
        - "🙅 Hoy no puedo"
    message:
      - "🗑️ Hoy le toca a {turno} sacar la basura."
```

La encuesta **no es anónima**, que es todo el punto: lo que se quiere saber no
es cuántos contestaron sino si contestó la persona a la que le toca.

**El bot manda las encuestas pero no lee las respuestas.** Sirven para que el
grupo vea quién confirmó, no para que el sistema reaccione — insistirle a quien
no contestó necesitaría `getUpdates` o un webhook, y estado en la base.

Dos límites de Telegram que conviene tener presentes, porque el síntoma de
pasarse es un recordatorio que no llega:

- **La pregunta admite 300 caracteres** y no interpreta HTML, así que las
  negritas se pierden. Los mensajes del almuerzo ya rozan los 280.
- **Entre 2 y 12 respuestas**, de hasta 100 caracteres cada una.

`validate` comprueba las dos cosas en cada push, y mide la pregunta con el
**nombre más largo de la rotación ya sustituido** — medirla con el `{turno}` sin
sustituir dejaría pasar un YAML que después falla al enviar.

En este repo la llevan los cinco avisos de la mañana (los que preguntan si vas
a hacer la tarea). Los controles de la tarde siguen siendo mensajes, que ya
preguntan si *ya* la hiciste.

### Cuánto puede llegar tarde cada recordatorio

`max_delay_minutes` es el campo que más se subestima. La pregunta para elegirlo
es siempre la misma: **¿hasta qué hora este mensaje todavía le sirve a quien lo
recibe?**

Ponerlo corto no protege de nada. Un recordatorio descartado no es uno
entregado a tiempo: es uno que no llegó. Con 120 min para todo, el aviso de
"saca la carne del congelador" de las 9:00 se tiraba a las 11:00 — aunque a las
15:00 seguía siendo útil, porque se cocina a las 18:00. Así se perdió un
almuerzo en agosto de 2026.

Los valores de este repo, y por qué:

| Recordatorio | Hora | `max_delay` | Sirve hasta |
|---|---|---|---|
| `bano-manana` | 5:00 | 480 | 13:00 — el baño se limpia a cualquier hora |
| `bano-tarde` | 17:00 | 240 | 21:00 — es un control, después no cambia nada |
| `basura-*-manana` | 6:00 | 180 | 9:00 — después ya pasó el camión |
| `basura-*-tarde` | 19:00 | 180 | 22:00 — control nocturno, sin despertar a nadie |
| `almuerzo-*-manana` | 9:00 | 420 | 16:00 — deja 2 h de descongelado |
| `almuerzo-*-tarde` | 18:00 | 240 | 22:00 — el almuerzo es de mañana, da tiempo |

El tope es la ventana de recuperación (`TICK_LOOKBACK_MINUTES`, 720 min): un
`max_delay` que la alcance devuelve el sistema a su peor modo de fallo, porque
lo vencido ya salió de la ventana cuando el tick lo miraría y se pierde sin
fila en la base, sin log y sin aviso. `validate` lo comprueba y avisa.

### Comandos

```bash
python -m recordatorios check             # revisa TODO: YAML, token, chats y base de datos
python -m recordatorios validate          # revisa el YAML y reporta todos los errores juntos
python -m recordatorios list              # cada recordatorio con sus próximas 3 ejecuciones
python -m recordatorios agenda --days 28  # cronología combinada
python -m recordatorios tick --dry-run    # qué se enviaría ahora mismo
python -m recordatorios tick              # el envío real (lo que corre en Actions)
python -m recordatorios send-test --id X  # manda uno a mano, con el turno de la ocurrencia más cercana
python -m recordatorios history           # últimos envíos registrados
python -m recordatorios dashboard         # arma la página de estado en site/
python -m recordatorios descartar --ref X # da por perdida una ocurrencia
python -m recordatorios init-db           # crea las tablas
```

### Desarrollo local

```bash
python -m venv .venv && .venv\Scripts\activate     # PowerShell
pip install -e ".[dev,postgres]"
copy .env.example .env                              # y completá los valores
pytest -q
```

Sin `DATABASE_URL`, el estado va a un SQLite local (`recordatorios.db`), que es
suficiente para probar. Los tests corren siempre sobre SQLite y no tocan la red.

---

## Cómo funciona por dentro

```
tick-loop (8 arranques/día, bucle interno cada 5 min)
        │
        ▼
  python -m recordatorios tick
        │
        ├─ lee reminders.yaml               → qué recordatorios existen
        ├─ calcula la ventana (ahora-12h, ahora]
        ├─ separa lo vencido (más de max_delay_minutes) de lo pendiente
        │
        ├─ ¿quedó algo? NO  → termina sin conectar a la base   ← ~99% de los ticks
        │
        └─ ¿quedó algo? SÍ  → recién acá conecta a Neon
              ├─ lo vencido → se anota una vez como 'stale', no se envía,
              │                y se avisa al chat de que se perdió
              └─ lo pendiente
                    ├─ claim (reminder_id, occurrence_at)
                    │     └─ ¿ya lo tomó otra corrida? → se abstiene
                    └─ POST a la Bot API de Telegram
```

Tres decisiones que sostienen todo lo demás:

**Ventanas, no instantes.** El tick procesa el intervalo `(ahora - 12h, ahora]`.
Un retraso de Actions se recupera en la corrida siguiente en vez de perderse, y
la ventana es fija: no hay ningún cursor que mantener.

La ventana (12 h) es deliberadamente más ancha que el plazo de entrega
(`max_delay_minutes`, entre 3 y 8 h según el recordatorio), y esa diferencia
no es decorativa. Si fueran
iguales, todo lo que entra en la ventana estaría por definición dentro del
plazo, y lo que se pasó del plazo ya habría salido de la ventana: el tick no lo
vería nunca. Un recordatorio perdido por una caída larga de Actions
desaparecería sin error, sin fila en la base y sin línea en el log. Con el
margen, la ocurrencia vencida todavía se ve, se descarta a conciencia y queda
anotada como `stale` — que es como uno se entera de que faltó.

El margen era de 4 h hasta que la caída de agosto de 2026 dejó huecos de 10 h y
demostró que era corto: lo que caía adentro se perdía en silencio, que es
exactamente lo que el margen existe para impedir. Ahora la ventana cubre el
peor hueco realista —un bloque entero de `tick-loop` perdido (3 h) más su
retraso de arranque—. El costo es que, después de una caída, lo vencido sigue
apareciendo 12 h y cada tick abre la base para confirmar que ya lo anotó; se
paga solo después de una caída, no en régimen normal.

**Una pérdida no puede ser silenciosa.** Anotar el `stale` en la base solo sirve
si alguien mira la base. Cuando el tick descarta una ocurrencia por vencida,
manda además un mensaje al chat diciendo qué no salió y para cuándo era. Se
avisa una sola vez por ocurrencia (mientras siga en la ventana, los ticks
siguientes la ven como `already_handled`), y solo de lo definitivamente perdido:
un `failed` se reintenta solo, así que avisar de eso sería ruido. Si el aviso
falla, queda en el informe del tick pero no lo tumba.

**La clave primaria es la idempotencia.** La tabla `deliveries` tiene
`PRIMARY KEY (reminder_id, occurrence_at)`. Antes de enviar, el tick inserta esa
fila; si ya existe y está en `sent`, otra corrida ya se encargó y esta se
abstiene. Como cada ocurrencia se identifica por sí misma, procesar la misma
ventana veinte veces seguidas da el mismo resultado que procesarla una.

Eso también hace trivial el reintento: un envío fallido queda en `failed` y
sigue dentro de la ventana, así que los ticks siguientes lo reintentan solos
hasta que salga o se pase de `max_delay_minutes`. Un claim que quedó colgado en
`sending` (runner muerto a mitad de envío) se puede retomar a los 10 minutos.

**La base se toca solo cuando hay algo que enviar.** El orden importa: primero
se calcula todo con el YAML en la mano, y la conexión se abre recién si quedó
algo pendiente. No es una optimización cosmética — es lo que hace viable el plan
gratuito de Neon, como se explica abajo.

### El presupuesto de Neon

Esto merece su propio apartado porque es la restricción menos evidente del
diseño. El plan Free de Neon da **100 CU-hours al mes** y suspende el compute
tras 5 minutos de inactividad (no se puede desactivar).

Un tick cada 5 minutos cae justo en ese borde: si cada corrida consultara la
base, el compute no alcanzaría a dormirse nunca. A 0.25 CU encendida 24/7 son
~182 CU-hours al mes — la cuota se agotaría **cerca del día 17** y Neon
suspendería el proyecto hasta el mes siguiente. Los recordatorios morirían a
mitad de mes, sin aviso.

Con la conexión perezosa, la base solo despierta cuando un recordatorio vence
de verdad. Pero eso solo no alcanza con la ventana en 12 h: una ocurrencia ya
enviada **sigue apareciendo en la ventana** durante esas 12 h, y los ticks de
ese rato volvían a abrir la conexión nada más que para reconfirmar lo que ya
sabían. Con recordatorios repartidos entre las 5:00 y las 19:00, eso encadenaba
las ventanas y dejaba el compute despierto casi 24/7 — la ventana ancha, sola,
habría reventado la cuota.

Por eso el tick guarda un **caché local** de lo que ya resolvió
(`.tick-state.json`, en el workspace del job). Lo que está ahí no se le vuelve
a preguntar a la base. Con eso, la conexión se abre solo cuando hay algo
genuinamente nuevo, y la ventana de 12 h pasa a costar prácticamente lo mismo
que la de 4 h.

Tres propiedades lo hacen seguro: la base sigue siendo la única fuente de
verdad, un caché vacío o ilegible solo hace la corrida más cara (nunca
incorrecta), y solo se cachean estados terminales — un `failed` no entra nunca,
así que se sigue reintentando.

El caché vive en el workspace, así que dura lo que dura un bloque de
`tick-loop` y arranca vacío en cada corrida nueva. Por eso las corridas de
respaldo de `tick.yml`, que estrenan runner cada vez, van con
`TICK_LOOKBACK_MINUTES=240`: sin caché, una ventana ancha ahí sí saldría cara.

De ahí salen los parámetros que conviene no tocar a la ligera:

| Variable | Por defecto | Efecto de subirlo |
|---|---|---|
| `TICK_LOOKBACK_MINUTES` | 720 | Más tolerancia a caídas de Actions, pero más horas de compute cuando el caché no ayuda |
| `TICK_MAX_WINDOW_HOURS` | 12 | Tope duro de la anterior, para que un error de dedo no vacíe la cuota |
| `TICK_STATE_FILE` | `.tick-state.json` | Dónde vive el caché. En vacío (`""`) lo desactiva: todo sigue funcionando, solo que más caro |

Si algún día tenés muchos recordatorios diarios y te acercás al límite, la
palanca es bajar `TICK_LOOKBACK_MINUTES`. Lo que no conviene es bajarlo hasta
igualar el `max_delay_minutes` de los recordatorios: ahí volvés a la
configuración en la que las pérdidas son invisibles.

### Estructura

```
reminders.yaml              definición de los recordatorios
src/recordatorios/
  models.py                 el dataclass Reminder
  schedule.py               cron + filtro de semanas → ocurrencias; y turnos
  loader.py                 parseo y validación del YAML
  store.py                  Neon/SQLite: claims e historial (conexión perezosa)
  telegram.py               Bot API con reintentos
  tick.py                   la corrida: ventana → envíos
  dashboard.py              el cruce calendario × base → los datos de la página
  render.py                 la página en sí: HTML, estilos, la tira de latidos
  cli.py                    los comandos
.github/workflows/
  tick-loop.yml             el reloj: 8 bloques de 3 h, bucle interno de 5 min
  tick.yml                  respaldo del reloj + acciones manuales
  dashboard.yml             genera y publica la página en Pages
  ci.yml                    tests + validación del YAML
  keepalive.yml             evita que GitHub apague el cron
```

---

## El dashboard

Una página en GitHub Pages que contesta la pregunta que costó tanto responder en
agosto de 2026: **de todo lo que tenía que salir, ¿qué salió?**

Cruza el calendario del YAML contra la tabla `deliveries`, y cada ocurrencia
esperada de los últimos 7 días queda en uno de estos estados:

| Estado | Qué significa |
|---|---|
| `enviado` | Salió. Con la hora real de salida |
| `en curso` | Venció hace poco y todavía está dentro de `max_delay_minutes` |
| `vencido` | Un tick la vio, pero ya era tarde. Quedó anotada como `stale` |
| `fallido` | Telegram rechazó el envío; se reintenta solo |
| **`perdido`** | **Ningún tick llegó a verla: no hay ni fila en la base** |
| `descartado` | Alguien la vio y la dio por perdida. Sigue en el historial, no cuenta como problema |

`perdido` es el que importa. Es el fallo que no deja rastro en ningún lado y que
solo se ve cruzando las dos fuentes — el que estuvo dos días pasando sin que
nadie se enterara.

### Por qué no tiene nombres

El repo es público y un sitio de Pages en un repo público también lo es, servido
desde una URL indexable. Publicar «hoy le toca a *[nombre]*» desharía justamente
lo que el diseño de `${PERSONA_n}` protege, y un secret que llega a una URL
pública no se puede despublicar.

Así que la página habla de ids de recordatorio y de horas. A quién le toca ya lo
dice el mensaje de Telegram, que es donde corresponde. Además del cuidado en la
plantilla, el workflow hace `grep` de cada secret sobre el HTML antes de
publicar y aborta el deploy si encuentra alguno.

### Reenviar o descartar, según el día

Cada problema trae un botón, y cuál depende de cuándo estaba programado:

- **Lo de hoy** → **Mandarlo ahora**, con **Copiar id** para pegar en
  `send_test_id`. Todavía le puede servir a alguien.
- **Lo de días anteriores** → **Descartar**, con **Copiar referencia**
  (`id@fecha-hora`) para pegar en `descartar_ref`.

La distinción no es cosmética. Mandar el aviso del baño del jueves un sábado no
le sirve a nadie, y un botón que invita a hacerlo es peor que no tener botón.
Pero dejar la pérdida para siempre en la lista tampoco sirve: una alarma que no
se puede apagar se termina ignorando, y entonces tampoco se ve la que sí
importa. Descartar es la tercera salida — la ocurrencia **no se borra**, queda
en el historial marcada como `descartado`, y deja de pedir acción.

Descartar nunca pisa un `sent`: falsear un envío sería peor que el problema que
resuelve. Desde la línea de comandos también acepta `--before AAAA-MM-DD`, para
limpiar de una vez todo lo que quedó atrás:

```bash
python -m recordatorios descartar --ref "basura-viernes-manana@2026-08-28T11:00:00+00:00"
python -m recordatorios descartar --before 2026-08-28
```

Es un enlace y no un botón de verdad, y no por pereza: Pages sirve estáticos, así
que la página no tiene con quién hablar. Reenviar exige disparar un workflow, y
eso exige un token de GitHub. Un token en un HTML público lo puede usar
cualquiera que abra la URL — no es una opción. GitHub tampoco admite prellenar
los inputs de un `workflow_dispatch` por URL, de ahí el botón de copiar.

Para un botón real haría falta algo que guarde el token del lado del servidor:
un Worker de Cloudflare en su capa gratuita alcanzaría. Ahí sí, un clic.

### Cómo se genera

Pages sirve archivos estáticos: no hay backend, y meter la credencial de Neon en
un HTML público está fuera de discusión. El cruce lo hace el runner, que sí
tiene los secrets, y lo único que se publica es el HTML resultante:

```bash
python -m recordatorios dashboard --out site
```

La página no carga nada externo —ni fuentes, ni scripts, ni analítica—: se
sirve entera desde un solo archivo. El único enlace saliente es el que va a
Actions. Los datos van en monoespaciada con cifras tabulares porque esto es un
registro y las horas se comparan en columna; abajo de 640px cada fila de la
tabla se convierte en ficha para no obligar a hacer scroll lateral.

Se despliega por artifact (`actions/deploy-pages`), así que no ensucia el repo
con commits. Corre al terminar cada bloque de `tick-loop` —unas 8 veces al día—
y no cada hora, porque cada corrida despierta a Neon.

También corre al terminar una corrida **manual** de `tick`, que es donde viven
`descartar` y `send-test`: un botón cuya función es hacer desaparecer algo de
esta página no sirve de nada si la página no se vuelve a generar. Las corridas
de `tick` por cron quedan excluidas — son cientos al día, y cada publicación
despertaría a Neon para nada.

Que la página esté vieja no es el indicador: el número de problemas se calcula
contra la base y es correcto sin importar cuándo se generó. Pero si «Página
generada» quedó muchas horas atrás, el reloj está caído.

---

## Problemas frecuentes

**No llega nada y el workflow figura en verde.** Mirá el log del paso *Ejecutar
tick*: imprime la ventana y qué hizo con cada ocurrencia. Un `skipped_stale`
significa que el tick llegó más tarde que `max_delay_minutes`.

**`chat not found` o `unauthorized`.** El bot no puede escribirle primero a
alguien: mandale vos un mensaje al bot (o agregalo al grupo) antes del primer
envío. Verificá también que el `chat_id` sea el correcto — los de grupo son
negativos.

**El mensaje se envía pero se ve raro.** Con `parse_mode: HTML`, Telegram solo
acepta unas pocas etiquetas (`<b>`, `<i>`, `<code>`, `<a>`). Un `<` suelto rompe
el envío: escapalo como `&lt;` o poné `parse_mode: none`.

**Los workflows programados dejaron de correr.** Puede ser la regla de los 60
días de inactividad: reactivalos desde la pestaña *Actions* y verificá que
`keepalive` esté habilitado. Pero antes descartá lo otro, que es más común y no
se ve igual: Actions **descarta corridas `schedule`** sin apagar nada. El
workflow figura como *active*, las corridas que sí arrancan salen en verde, y
lo único raro es que son muchas menos de las que pide el cron. Para medirlo, la
API pública dice cuántas corridas hubo de verdad:

```bash
curl -s "https://api.github.com/repos/<usuario>/<repo>/actions/workflows/<id>/runs?per_page=100" \
  | grep -o '"created_at": "[0-9T:Z-]*"' | cut -c17-26 | sort | uniq -c
```

Si el conteo diario está muy por debajo de lo que pide el cron, el problema es
ese, y la respuesta es `tick-loop.yml`
(ver [Por qué el reloj es un bucle](#por-qué-el-reloj-es-un-bucle-y-no-un-cron-de-5-minutos)).

**Llegó un aviso de "esto se perdió".** El tick encontró una ocurrencia vencida
hace más de `max_delay_minutes` y la descartó en vez de mandarla tarde. El
recordatorio en sí no va a llegar; si todavía sirve, mandalo a mano con
`send-test`. Que haya avisado es la parte que funciona — antes esas pérdidas
eran invisibles.

**Cambié la hora y llegó el recordatorio viejo.** El tick ya había procesado esa
ocurrencia. Los cambios aplican a las ocurrencias futuras.

---

## Si algún día querés UI

Este diseño no cierra la puerta: `schedule.py`, `store.py` y `telegram.py` no
saben nada de GitHub Actions. Para una interfaz web bastaría con montar FastAPI
encima y mover las definiciones del YAML a la base — el tick seguiría siendo el
mismo, invocado por `POST /tick` en vez de por el workflow. En ese escenario
Render vuelve a ser razonable (con el pinger externo despertándolo), o mejor una
VM Always Free de Oracle Cloud, que no duerme.

Mientras el YAML alcance, esto es menos infraestructura para mantener.
