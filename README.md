# linkedin-profile-analyzer-ai

Pipeline de Desarrollo Profesional (UdeSA) que analiza perfiles de LinkedIn (exportados como
PDF) con IA, y vuelca los resultados en una matriz de Google Sheets, informes individuales en
Google Docs y estadísticas en Slides.

Incluye un **panel web** (`web_app.py` + `static/index.html`) para que cualquier persona del
equipo pueda correr el pipeline y gestionar carreras/planes de estudio sin usar la terminal.

## Uso desde la terminal (como antes)

```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env         # completar con los IDs y API keys reales
python main.py
```

La primera vez, `python main.py` va a abrir el navegador para el login de Google (hace falta
`client_secret.json` de la consola de Google Cloud, ver sección de autenticación más abajo) y va
a guardar `token.json` para las próximas corridas.

## Uso desde el panel web

```bash
pip install -r requirements.txt
cp .env.example .env         # completar con los IDs y API keys reales
uvicorn web_app:app --reload --port 8080
```

Abrir http://localhost:8080

**Importante:** el panel web NO hace el login de Google por vos. Antes de usarlo por primera vez
(en local o antes de desplegar), corré `python main.py` una vez a mano para completar el flujo de
OAuth y generar `token.json`. El backend web reutiliza ese mismo token.

Desde el panel se puede:
- Elegir modo "cohorte completa" (una carrera) o "conjunto mixto", e iniciar el análisis con un
  botón — con progreso en vivo y un resumen final (semáforo, links a Sheets/Drive/Slides, tabla
  de alumnos analizados).
- Agregar, editar o eliminar carreras (nombre + palabras clave de detección).
- Subir el PDF del plan de estudios de una carrera: se procesa con la misma lógica de
  `extraccion_plan_estudios.py` (grilla por coordenadas → tablas → reordenamiento con IA como
  último recurso) y avisa si el resultado conviene revisarlo a mano.

Las carreras y sus palabras clave viven en `carreras.json` (ya no están hardcodeadas en
`main.py`), así que los cambios desde la web quedan persistidos ahí.

## Autenticación con Google

1. En Google Cloud Console (proyecto `849635297315`), descargar las credenciales OAuth de
   escritorio como `client_secret.json` y ponerlo en la raíz del repo (nunca se commitea).
2. Habilitar las APIs de Drive, Sheets, Docs y Slides en el proyecto.
3. Correr `python main.py` una vez y completar el login. Esto genera `token.json`.
4. Si se agregan scopes nuevos en el futuro, hay que borrar `token.json` para forzar un login
   nuevo (ver notas del proyecto).

## Despliegue en Cloud Run

```bash
gcloud run deploy linkedin-profile-analyzer \
  --source . \
  --region southamerica-east1 \
  --service-account TU_SERVICE_ACCOUNT@849635297315.iam.gserviceaccount.com \
  --set-env-vars-file .env.yaml \
  --allow-unauthenticated=false
```

Notas:
- Usá una cuenta de servicio con permisos sobre la carpeta de Drive, el spreadsheet, la
  plantilla de Docs y la presentación de Slides (compartilos con el email de la cuenta de
  servicio), en vez del flujo OAuth de usuario — así no depende de que alguien haga login a
  mano dentro del contenedor.
- `--allow-unauthenticated=false` + IAM (o un proxy con SSO de UdeSA) para que el panel no quede
  público, ya que corre acciones sobre Drive/Sheets institucionales.
- El estado de las corridas del pipeline vive en memoria del proceso (`web_app.py`), pensado
  para un solo worker/instancia. Si se necesita escalar a más de una instancia concurrente, ese
  estado tiene que pasar a algo compartido (ej. Firestore).

## Estructura del proyecto

```
main.py                        # pipeline principal (Drive -> IA -> Sheets/Docs/Slides)
web_app.py                     # backend FastAPI que envuelve el pipeline para la web
static/index.html              # frontend del panel (sin build step)
extraccion_plan_estudios.py    # utilidad para convertir el PDF de un plan de estudios a .txt
carreras.json                  # config de carreras UdeSA (nombre, palabras clave, archivo_plan)
planes_de_estudio/*.txt        # planes de estudio en texto plano, usados como contexto extra
Dockerfile                     # build para Cloud Run
.env.example                   # variables de entorno necesarias (copiar a .env)
```