# Duet Orchestrator

Orquestador acotado que coordina OpenAI y Claude sobre un repositorio Git. Cada
agente puede implementar tareas, pedir trabajo al otro y revisar sus cambios. El
resultado queda en una rama de integración revisable; el motor no publica ni
fusiona en la rama principal.

Python 3.12 o posterior, Git y biblioteca estándar. Para ejecutar pruebas de
código generado en un uso real se necesita Docker. No hay dependencias de
ejecución de Python ni un servicio alojado obligatorio.

## Inicio rápido sin claves ni gasto

Desde la raíz de este repositorio:

```console
python -m duet doctor
python -m unittest discover -s tests -v
python -m duet demo --directory ../duet-demo
```

La demostración crea un repositorio pequeño separado y utiliza dos proveedores
**simulados**. Sirve para comprobar el recorrido completo del orquestador:
implementación, delegación, revisión cruzada, pruebas e integración Git. No valida
acceso a las APIs ni calidad de los modelos reales. La carpeta de demostración
debe ser nueva y estar fuera del repositorio del orquestador.

Para comprobar además el aislamiento de las pruebas:

```console
docker pull python:3.12-slim
python -m duet demo --directory ../duet-demo-docker --sandbox docker
```

La demostración local sólo ejecuta el pequeño caso de prueba suministrado. No
debe usarse como justificación para ejecutar código generado desconocido en el
equipo anfitrión.

## Ejecución con OpenAI y Claude

1. Configura `OPENAI_API_KEY` y `ANTHROPIC_API_KEY` en el entorno del proceso
   mediante tu gestor de secretos. No pegues claves en chats, archivos de tarea,
   argumentos de comandos ni archivos versionados. Una sesión de ChatGPT o
   Claude en el navegador no proporciona por sí sola acceso a sus APIs.
2. Copia `examples/config.example.json` a `config.local.json` y elige modelos a
   los que tu cuenta tenga acceso. Introduce tarifas actuales explícitas por
   millón de tokens y un presupuesto pequeño para la primera ejecución. Las
   tarifas son entradas del usuario, no valores garantizados por este proyecto.
3. Establece `allowed_paths` con los nombres exactos de los archivos que se
   pueden modificar y `context_paths` para archivos adicionales de consulta
   (por ejemplo, tests que el agente no puede editar). Evita secretos, datos personales y archivos de
   configuración del sistema. El contenido autorizado se enviará a ambas APIs.
4. Define una orden de pruebas aprobada por ti en `tests.command`, un tiempo
   máximo y una imagen Docker previamente obtenida. Los agentes no eligen la
   orden de ejecución. Usa una imagen fijada por digest en entornos de trabajo.
5. Prepara un repositorio objetivo limpio con al menos un commit y escribe una
   tarea breve en un archivo de texto local. Usa una carpeta de estado persistente,
   fuera del repositorio objetivo. Reutilízala para conservar el presupuesto acumulado;
   cada ejecución recibe su propio identificador, eventos y worktrees.

```console
python -m duet run --repo ../mi-proyecto --config config.local.json --task-file ../tarea.txt --state-dir ../mi-proyecto-duet-state
```

Revisa la rama y los commits indicados al terminar antes de incorporarlos a tu
proyecto. Un fallo de pruebas, revisión, presupuesto o validación detiene la
ejecución. El estado y las ramas permiten investigar lo sucedido.

Se puede usar directamente `python -m duet` desde este directorio. Instalar el
paquete es opcional: `python -m pip install .` añade el comando `duet`; ese paso
puede descargar herramientas de empaquetado.

## Prueba real de conexión, sin ejecutar código generado

```console
python scripts/secure_live_smoke.py
```

Ejecuta esa orden en tu propia terminal. Solicita las claves con entrada oculta,
las mantiene sólo en la memoria del proceso y ejecuta una prueba limitada a
US$1 y 12 llamadas acumuladas. No guarda claves ni las envía a GitHub.
Si ya están configuradas en el entorno, también puedes ejecutar:

```console
python -m duet live-smoke --directory ../live-validation
```

Ambos proveedores colaboran sobre `results.json`; un validador fijo comprueba
sumas y productos sin importar ni ejecutar contenido generado. Este recorrido
usa ramas Git y revisión cruzada reales y no necesita Docker. Valida conectividad
y coordinación; no sustituye la prueba Docker para tareas de programación.
El ledger se comparte entre intentos en `.duet-live-smoke-state`, junto al
directorio de salida. La disponibilidad de las claves y el saldo se validan al
llamar a las APIs; no se simula una conexión exitosa.

`examples/live-smoke.json` fija modelos económicos para esta comprobación:
[GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini) y
[Claude Haiku 4.5](https://platform.claude.com/docs/en/models/haiku-4-5/overview).
Las tarifas límite incorporan un margen del 25% sobre los precios publicados
consultados el 13 de octubre de 2026; revisa precios y disponibilidad antes de
reutilizar la configuración. Son opciones para la prueba, configurables para
trabajo posterior.

## Qué controla

- **Coordinación:** intercambio de tareas y mensajes estructurados entre los
  dos proveedores; revisión de cada cambio por el proveedor contrario.
- **Trabajo acotado:** límites de tareas, rondas, llamadas y tokens de salida.
  El planificador es secuencial: dos agentes lógicos no implican procesos
  simultáneos ni autonomía ilimitada.
- **Git:** worktrees y ramas por tarea, más una rama de integración. La copia de
  trabajo original se conserva para revisión y no se hace `push` desde el motor.
- **Presupuesto:** reserva persistente en SQLite antes de cada petición pagada,
  con reconciliación al recibir el uso informado. Una respuesta ambigua conserva
  la reserva y no se reintenta automáticamente.
- **Permisos:** escritura limitada a rutas exactas aprobadas; los modelos no
  reciben una consola, credenciales Git ni herramientas arbitrarias.
- **Pruebas:** comando fijo, timeout y contenedor sin red para código generado;
  la prueba local de demostración es un modo explícitamente de confianza.
- **Auditoría:** eventos JSONL y contabilidad persistente, sin registrar claves,
  prompts ni contenido completo del código.
- **CI:** pruebas sin secretos en Linux y Windows con Python 3.12/3.13, más una
  demostración Docker en Linux; acciones fijadas por commit y permisos de lectura.

Los límites monetarios son una barrera de admisión **estimada** según las tarifas
configuradas y el conteo previsto. No sustituyen los controles de facturación del
proveedor ni aseguran un tope contractual. Una tarifa incorrecta, cambios del
servicio o un fallo de comunicación pueden producir diferencias. Revisa el uso
en ambos proveedores y configura también sus límites disponibles.

## Publicación opcional en GitHub

El motor de agentes y el publicador son componentes separados. El publicador
requiere una identidad esperada explícita y sólo admite un repositorio privado
bajo esa cuenta. Usa una credencial ya disponible en Git Credential Manager, o
`GH_TOKEN`/`GITHUB_TOKEN` entregado de forma segura al proceso para la API. El
`push` utiliza la autenticación Git configurada; no se escriben tokens en URLs,
archivos temporales ni argumentos. Crear el repositorio requiere permisos que
una credencial de lectura puede no tener.

```console
python scripts/publish_github.py --check --expected-owner TU_USUARIO
python scripts/publish_github.py --publish --expected-owner TU_USUARIO
```

`--check` sólo comprueba la cuenta autenticada y la existencia/privacidad del
destino. `--publish` exige un árbol limpio en `main`, crea el repositorio privado
si falta y hace un push normal. Rechaza cuentas distintas, repositorios públicos
y un `origin` que no coincida. No fuerza pushes ni reemplaza otro remoto.

## Alcance y límites

Este proyecto reduce fallos mediante controles verificables; no existe una
garantía de que un sistema autónomo sea infalible. La revisión cruzada puede
equivocarse y los tests sólo comprueban lo que cubren. Los worktrees aíslan el
estado Git; el contenedor aporta el aislamiento de ejecución. Docker comparte
dependencias con el anfitrión y no sustituye una máquina desechable para código
hostil.

No hay daemon, monitor permanente, despliegue automático ni fusión automática.
No se reanuda ciegamente una ejecución interrumpida: en especial, no se repiten
peticiones cuyo cobro pudo ocurrir sin que llegara la respuesta. Consulta
[operación y recuperación](docs/OPERATIONS.md) y [seguridad](SECURITY.md).
