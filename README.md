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

Las carreras y sus palabras clave viven en `carreras_grado.json` / `carreras_posgrado.json`
(ya no están hardcodeadas en `main.py`), así que los cambios desde la web quedan persistidos ahí.

### Carga masiva de planes de estudio (`lote_extraccion_planes.py`)

Para cargar de golpe muchos PDFs de planes de estudio (por ejemplo, todo el catálogo de
posgrados), sin pasar uno por uno por la web:

```bash
python lote_extraccion_planes.py "pdfs_posgrado"
```

Genera los `.txt` en `planes_de_estudio/<nivel>/` y un `manifiesto_planes.json` con el resultado
de cada archivo. El nombre del `.txt` de salida sale sin tildes ni espacios automáticamente
(`Maestría en Finanzas.pdf` → `maestria_en_finanzas.txt`).

Los PDFs de origen viven en `pdfs_grado/` y `pdfs_posgrado/` — son solo para uso local (correr
este script) y están excluidos del deploy vía `.gcloudignore`, ya que el pipeline en producción
lee los `.txt` ya extraídos, no los PDFs crudos.

## Autenticación con Google

1. En Google Cloud Console (proyecto `849635297315`), descargar las credenciales OAuth de
   escritorio como `client_secret.json` y ponerlo en la raíz del repo (nunca se commitea).
2. Habilitar las APIs de Drive, Sheets, Docs y Slides en el proyecto.
3. Correr `python main.py` una vez y completar el login. Esto genera `token.json`.
4. Si se agregan scopes nuevos en el futuro, hay que borrar `token.json` para forzar un login
   nuevo (ver notas del proyecto).

Para el panel web hace falta además un cliente OAuth de tipo "Aplicación web" (`client_secret_web.json`,
nunca se commitea — en producción se monta desde Secret Manager, ver más abajo).

## Despliegue en Cloud Run

### 1. Preparar `.env.yaml`

Cloud Run no acepta el `.env` tal cual — necesita un archivo YAML separado. Creá
`.env.yaml` en la raíz del repo con las mismas variables que tenés en tu `.env`, pero en
sintaxis YAML (`clave: "valor"`, **con espacio después de los dos puntos** — sin el espacio,
`gcloud` tira `expected map-like data`):

```yaml
ID_SPREADSHEET: "id_de_tu_hoja_de_calculo"
ID_CARPETA: "id_carpeta_origen_pdfs"
ID_CARPETA_INFORMES: "id_carpeta_destino_informes"
ID_CARPETA_ANALIZADOS: "id_carpeta_pdfs_analizados"
ID_PLANTILLA_INFORME: "id_Informe_modelo"
ID_PRESENTACION_STATS: "tu_id_de_presentacion"
ID_CARPETA_PRESENTACIONES: "id_carpeta_destino_presentaciones"
GEMINI_API_KEY: "tu_api_key_de_gemini"
GROQ_API_KEY: "tu_key_de_groq"
NVIDIA_API_KEY: "tu_key_de_nvidia"
SESSION_SECRET: "clave_generada_con_secrets.token_hex(32)"
DOMINIO_PERMITIDO: "udesa.edu.ar"
GCS_BUCKET_ESTADO: "udesa-analizador-perfiles-estado"
GOOGLE_OAUTH_CLIENT_SECRETS_FILE: "/secrets/client_secret_web.json"
```

`.env.yaml` tiene los mismos secretos que `.env` — **nunca se commitea** (ya está en
`.gitignore` y `.gcloudignore`).

**`GOOGLE_OAUTH_CLIENT_SECRETS_FILE` es obligatoria en Cloud Run**, aunque el `.env.example` la
marque como opcional: por default la app busca `client_secret_web.json` en la raíz del proyecto,
pero en producción ese archivo viene montado desde Secret Manager en `/secrets/client_secret_web.json`
(no en la raíz), así que hay que decirle explícitamente dónde está. Como `--env-vars-file`
reemplaza todas las variables de entorno del servicio (no las combina con las que ya tenía),
olvidarse esta línea rompe el login de Google en el próximo deploy.

### 2. Deployar

```bash
gcloud run deploy linkedin-profile-analyzer --source . --region southamerica-east1 --service-account linkedin-analyzer-sa@udesa-analizador-perfiles.iam.gserviceaccount.com --env-vars-file .env.yaml
```

(En `cmd.exe` de Windows no se puede usar `\` para cortar el comando en varias líneas como en
bash; en PowerShell la continuación es con backtick `` ` ``. Más simple: ponerlo todo en una
sola línea, como arriba.)

**No agregues `--allow-unauthenticated` ni `--no-allow-unauthenticated` a este comando.** Cada
vez que se especifica alguna de las dos, gcloud sincroniza la política de IAM del servicio con
esa flag — así que `--no-allow-unauthenticated` en un redeploy le sacaría el acceso público al
servicio aunque ya se lo hubieras dado antes. Sin ninguna de las dos flags, gcloud deja la
política de IAM tal cual está.

### 3. Habilitar el acceso público al servicio (una sola vez, la primera vez)

El control de acceso real de esta app es el login de Google con `DOMINIO_PERMITIDO=udesa.edu.ar`,
no IAM de Google Cloud — así que el servicio de Cloud Run tiene que quedar abierto a nivel de
IAM para que cualquiera pueda llegar a la pantalla de login, y de ahí para adelante la app filtra
por dominio:

```bash
gcloud run services add-iam-policy-binding linkedin-profile-analyzer --region southamerica-east1 --member="allUsers" --role="roles/run.invoker"
```

Esto solo hace falta correrlo una vez (el permiso queda asociado al servicio y los deploys
posteriores no lo tocan, siempre que no uses `--allow-unauthenticated`/`--no-allow-unauthenticated`
como se explica arriba).

Notas:
- Usá una cuenta de servicio con permisos sobre la carpeta de Drive, el spreadsheet, la
  plantilla de Docs y la presentación de Slides (compartilos con el email de la cuenta de
  servicio), en vez del flujo OAuth de usuario — así no depende de que alguien haga login a
  mano dentro del contenedor.
- `GCS_BUCKET_ESTADO` es obligatoria en Cloud Run (disco efímero): sin ella se pierden los
  tokens de login de cada persona, la caché de análisis y la config de carreras en cada reinicio
  de la instancia.
- El estado de las corridas del pipeline vive en memoria del proceso (`web_app.py`), pensado
  para un solo worker/instancia. Si se necesita escalar a más de una instancia concurrente, ese
  estado tiene que pasar a algo compartido (ej. Firestore).

## Estructura del proyecto

```
main.py                        # pipeline principal (Drive -> IA -> Sheets/Docs/Slides)
web_app.py                     # backend FastAPI que envuelve el pipeline para la web
static/index.html              # frontend del panel (sin build step)
extraccion_plan_estudios.py    # utilidad para convertir el PDF de un plan de estudios a .txt
lote_extraccion_planes.py      # procesa una carpeta entera de PDFs de golpe (ver más arriba)
carreras_grado.json            # config de carreras de grado (nombre, palabras clave, archivo_plan)
carreras_posgrado.json         # config de carreras de posgrado, mismo formato
planes_de_estudio/<nivel>/*.txt  # planes de estudio en texto plano, usados como contexto extra
pdfs_grado/, pdfs_posgrado/    # PDFs de origen para carga masiva — solo uso local, no se deployan
Dockerfile                     # build para Cloud Run
.env.example                   # variables de entorno necesarias (copiar a .env)
.env.yaml                      # mismas variables en YAML, para el deploy a Cloud Run (no se commitea)
```