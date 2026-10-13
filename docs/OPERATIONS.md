# Operación y recuperación

## Preparación

Ejecuta `python -m duet doctor` para inspeccionar dependencias y presencia de las
variables necesarias sin revelar sus valores. El diagnóstico local no demuestra
que una clave esté vigente, que tenga saldo ni que el modelo esté habilitado.
Una prueba real sólo queda validada cuando ambos proveedores responden y el
recorrido concluye con pruebas y revisión satisfactorias.

Mantén separados el código del orquestador, el repositorio objetivo y el estado
persistente. El objetivo debe tener un commit inicial y estar limpio.
Reutiliza el mismo directorio de estado para acumular reservas y gastos.
Los eventos y worktrees se separan automáticamente por identificador de ejecución.
Crear otro ledger reinicia la contabilidad: no lo uses para eludir un límite.
El motor trabaja a partir del commit base y genera ramas
propias; conserva la referencia base para comparar el resultado.

Antes de usar APIs reales:

1. Revisa la lista exacta de rutas autorizadas y el texto de la tarea.
2. Elige un objetivo pequeño, verificable por tests deterministas.
3. Configura modelos, tarifas vigentes, número de llamadas y presupuesto.
4. Comprueba Docker y descarga previamente una imagen de pruebas confiable.
5. Proporciona las claves mediante el entorno sin imprimirlas en el terminal.
6. Ejecuta la demo y los tests del proyecto antes de un cambio operativo.

La identidad de Git para los commits del orquestador no concede acceso a
GitHub. La autenticación Git, la conexión de una aplicación y las claves API de
OpenAI/Anthropic son autorizaciones distintas.

## Qué revisar al terminar

Comprueba el estado final informado por la CLI, la rama de integración, los
commits de cada tarea y los eventos de pruebas y revisión. Compara la rama con
el commit base. La aprobación de un modelo no reemplaza una revisión humana
cuando el cambio afecta datos, seguridad o un servicio en producción.

Los eventos JSONL sirven para seguir fases, resultados y errores sin copiar
fuentes ni secretos. SQLite registra reservas y uso. Conserva ambos mientras
necesites la trazabilidad. No publiques el directorio de estado con el código.

## Diagnóstico de fallos

**Falta una clave o el modelo no es accesible.** Configura la variable mediante
tu gestor de secretos, confirma la habilitación del modelo en el proveedor y
conserva el directorio de estado. No intentes solventarlo con cookies de una
sesión de navegador o copiando credenciales a archivos del repositorio.

**Se alcanzó el presupuesto.** Inspecciona la contabilidad y el uso de ambas APIs.
Las reservas de peticiones fallidas pueden seguir consumiendo el presupuesto
local cuando no se sabe si fueron cobradas. Cambiar o borrar el ledger no
reembolsa costes. Planifica cualquier nueva ejecución con lo ya gastado.

**Timeout o respuesta inválida.** El sistema detiene el flujo y evita reintentos
automáticos de llamadas pagadas ambiguas. Revisa conectividad, configuración y
estado del proveedor. Conserva la ejecución fallida para conciliación; repetir
manualmente una tarea puede tener un coste adicional.

**Tests fallidos o revisión rechazada.** El agente puede corregir dentro de los
límites configurados. Al agotarlos, revisa los cambios parciales y ajusta la
tarea o las pruebas. No elimines controles sólo para conseguir un estado verde.

**Docker ausente o inaccesible.** Instala/inicia un entorno Docker autorizado y
verifica acceso a la imagen. No cambies a ejecución local para salvar un error
de aislamiento en un proyecto que contiene código generado desconocido.

**Conflictos Git o cambios locales.** Conserva tus cambios existentes y prepara
un objetivo limpio. El orquestador no debe descartar trabajo ajeno. Después de
revisar resultados, elimina sólo los worktrees y ramas de la ejecución elegida
utilizando las herramientas normales de Git. No borres carpetas compartidas a
ciegas.

## Publicación y CI

El script `scripts/publish_github.py` permite comprobar la identidad con
`--check` y publicar con `--publish`. La creación utiliza la API de GitHub; el
push usa las credenciales configuradas de Git. Una clave de entorno que permita
crear repositorios no garantiza que Git pueda hacer el push. Si la creación
funciona y el push falla, el repositorio privado puede quedar creado y vacío;
corrige la autenticación Git y vuelve a ejecutar sin borrar el remoto.

El publicador exige `--expected-owner`, una rama `main` con commit y árbol limpio,
y que `origin` esté ausente o apunte exactamente al destino HTTPS esperado. Un
repositorio existente debe ser privado y pertenecer al usuario esperado. No
sobrescribe la configuración de otro remoto ni fuerza actualizaciones.

Los checks de CI comprueban el motor con proveedores simulados. La prueba
Docker de Linux comprueba el aislamiento del entorno de pruebas y no consume
APIs. El workflow configurado no equivale a una ejecución exitosa: verifica
por separado el resultado de GitHub Actions tras publicar. Si la cuenta no
permite ejecutar Actions, se necesita habilitar ese permiso o usar CI externa.

No incluyas secretos de los modelos en la CI ordinaria. Una comprobación con
APIs reales debe ser manual, con presupuesto acotado, claves limitadas y un
repositorio de prueba sin información sensible.

## Referencias de servicios

- [GitHub: crear un repositorio para la cuenta autenticada](https://docs.github.com/en/rest/repos/repos#create-a-repository-for-the-authenticated-user).
- [GitHub: comprobar la cuenta autenticada](https://docs.github.com/en/rest/users/users#get-the-authenticated-user).
- [OpenAI: documentación de la API](https://platform.openai.com/docs/api-reference).
- [Anthropic: documentación de Claude](https://platform.claude.com/docs/en/api/overview).

La disponibilidad de modelos, precios y permisos de cuenta debe verificarse en
los servicios antes de cada cambio de configuración. La prueba real incluye
tarifas límite con fecha de referencia, que deben revisarse antes de reutilizarlas.
