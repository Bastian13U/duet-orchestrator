# Seguridad

## Modelo de confianza

El usuario aprueba el repositorio objetivo, la tarea, las rutas autorizadas, los
modelos, las tarifas y el comando de pruebas. Las respuestas de los modelos y
los archivos del repositorio son datos no confiables. Que un agente proponga
una instrucción no le concede permisos adicionales.

Los proveedores reciben los fragmentos de código autorizados y los mensajes
necesarios para colaborar. Utiliza sólo proyectos que puedas compartir con
ambos servicios y revisa sus condiciones de tratamiento de datos. La ejecución
simulada no envía datos a proveedores.

## Límites de privilegios

- Las claves API se leen del entorno, del archivo exacto `.env.local` junto al
  orquestador o de sus dos destinos específicos del Administrador de credenciales
  de Windows. No se enumera el almacén del usuario ni se copian claves entre
  fuentes. No forman parte del contexto deliberadamente enviado a los modelos.
- Los agentes proponen cambios estructurados. El motor valida rutas y contenido
  antes de aplicarlos. No ofrece una herramienta de shell a los modelos.
- Las rutas de escritura son una lista exacta, no una autorización recursiva a
  todo el repositorio. No deben incluir archivos sensibles ni infraestructura
  que el usuario no quiera permitir modificar.
- Los comandos de prueba los configura el operador. En modo Docker el trabajo
  generado corre sin red y sin las claves del proceso padre. No montes el
  socket Docker, carpetas personales ni gestores de credenciales en la imagen.
- Una rama/worktree Git no es una barrera de seguridad del sistema operativo.
  No habilites ejecución local de confianza para código generado desconocido.
- El motor no publica ramas, crea remotos, fusiona en `main` ni despliega. El
  publicador requiere una invocación separada, autenticación autorizada y un
  destino privado verificado.

## Secretos y trazas

Nunca pegues una clave en una conversación. Inyecta secretos mediante un gestor
del sistema o del entorno de ejecución. `.gitignore` reduce inclusiones
accidentales, pero no detecta todos los secretos ni elimina archivos ya
versionados. Revisa lo que se va a publicar y habilita las protecciones de
secretos que admita tu alojamiento.

El archivo `.env.local` es texto local: debe mantenerse privado y fuera de Git.
El lector limita tamaño y formato y rechaza enlaces y archivos con varios enlaces
físicos. El gestor de Windows persiste claves para el mismo usuario y equipo;
no impide que otros procesos autorizados como ese usuario puedan leerlas.
El configurador pide entrada oculta y nunca admite claves como argumentos.

La auditoría conserva metadatos y contabilidad, no prompts ni respuestas
completas. El repositorio objetivo, sus worktrees y sus commits contienen el
código generado; trátalos según la sensibilidad del proyecto. Protege el
directorio de estado con permisos de usuario y políticas de retención adecuadas.

Si se expone una credencial, revócala y rótala en el proveedor. Borrar el texto
del último commit no la vuelve segura ni la elimina del historial.

## Presupuesto y recuperación

La reserva monetaria se realiza antes de llamar a una API. Los fallos de red o
respuestas sin uso verificable pueden conservar una reserva sin gasto conocido;
es preferible detenerse que repetir una petición potencialmente facturada.
SQLite ayuda a mantener la contabilidad local, pero no conoce en tiempo real
la factura de los proveedores. No edites el ledger para recuperar presupuesto
sin conciliar antes el uso externo.

Ante un bloqueo o fallo, conserva el estado y el historial. Revisa los eventos,
las ramas y el uso externo. Inicia una nueva ejecución con un presupuesto
calculado considerando lo ya gastado; no borres la contabilidad para sortear un
límite. No existe recuperación automática que garantice exactamente una
ejecución de cada llamada externa.

## CI y cadena de suministro

La CI no necesita claves API ni secretos de proveedores. El token de GitHub
dispone de `contents: read`, las acciones están fijadas por commit y Checkout
no persiste sus credenciales. La CI ejecuta código del repositorio: revisa los
cambios a sus workflows antes de autorizarlos y no uses este workflow como
validador seguro para contribuciones deliberadamente hostiles.

La imagen `python:3.12-slim` facilita la demostración; es una etiqueta mutable.
En producción, fija una imagen revisada por digest y mantenla actualizada. Para
código potencialmente hostil, usa un runner o máquina virtual desechable sin
acceso a datos de la organización.

## Informar un problema

No incluyas secretos, prompts privados ni código confidencial en incidencias
públicas. En un repositorio privado, utiliza el canal privado acordado con el
propietario e incluye pasos de reproducción mínimos, impacto y versión. Si se
habilita la notificación privada de vulnerabilidades en GitHub, úsala.
