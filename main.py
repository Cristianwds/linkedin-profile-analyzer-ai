import io
import json
import os
import pdfplumber
import re
import time
import hashlib
import unicodedata
import almacenamiento_estado
import entorno
import generar_presentacion_stats as presentacion_stats

from datetime import datetime
from zoneinfo import ZoneInfo

from google import genai
from google.genai import types
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from openai import OpenAI
from groq import Groq

# Activacion del venv antes de ejecutar: source venv/bin/activate (en Windows: venv\Scripts\activate)

# ejecucion del codigo en bash: python main.py

# probar una presentacion de estadisticas sin correr el pipeline:
# python generar_presentacion_stats.py <desde> [<hasta>] [--carrera ...] [--modo Cohorte|Mixto]

# ejecucion para pagina web: uvicorn web_app:app --reload --port 8080
# Abrir http://localhost:8080

# Pagina web: https://linkedin-profile-analyzer-849635297315.southamerica-east1.run.app

# Redeploy a Cloud Run (correrlo desde la raiz del proyecto, en una sola linea):
# gcloud run deploy linkedin-profile-analyzer --source . --region southamerica-east1 --service-account linkedin-analyzer-sa@udesa-analizador-perfiles.iam.gserviceaccount.com --env-vars-file .env.yaml --max-instances 1 --no-cpu-throttling

# Cargar las variables de entorno: del .env, o de .env.prueba si existe (ver entorno.py, para
# probar sin tocar la planilla ni las carpetas de Drive de producción).
entorno.cargar_variables()

# ==============================================================================
# CONFIGURACIÓN GENERAL
# ==============================================================================

# 1. Credenciales de Drive
# Los dos scopes de identidad (openid + userinfo.email) no los usa el pipeline en sí, pero hacen
# falta para que el backend web sepa QUIÉN se logueó (ver web_app.py) — se incluyen acá para que
# tanto el login de consola como el login web pidan siempre el mismo combo de permisos.
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets',  # Permiso para editar el spread sheet
    'https://www.googleapis.com/auth/documents',       # Permiso para inyectar texto en Docs
    'https://www.googleapis.com/auth/presentations',   # Permiso para inyectar en las presentaciones
    'openid',
    'https://www.googleapis.com/auth/userinfo.email',
]

# Obtenemos los IDs y credenciales de forma segura
ID_SPREADSHEET = os.getenv('ID_SPREADSHEET')
ID_CARPETA = os.getenv('ID_CARPETA')
ID_CARPETA_INFORMES = os.getenv('ID_CARPETA_INFORMES')
ID_CARPETA_ANALIZADOS = os.getenv('ID_CARPETA_ANALIZADOS')  # Carpeta padre para PDFs ya analizados
ID_PLANTILLA_INFORME = os.getenv('ID_PLANTILLA_INFORME')  # <-- NUEVO: ID del Google Doc plantilla
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
NVIDIA_API_KEY = os.getenv('NVIDIA_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

# Presentaciones de estadísticas. ID_PRESENTACION_MASTER_STATS es la plantilla actual
# (Plantilla_DP_version3): generar_presentacion_stats.py la copia a ID_CARPETA_PRESENTACIONES y le
# carga los números, barras y anillos con formas nativas. Al terminar cada corrida se genera una
# por cohorte (o una con los mixtos del día), y desde la web se puede pedir una por rango de fechas
# (ver generar_presentacion_de_la_corrida y generar_presentacion_por_rango).
ID_PRESENTACION_MASTER_STATS = os.getenv('ID_PRESENTACION_MASTER_STATS')
ID_CARPETA_PRESENTACIONES = os.getenv('ID_CARPETA_PRESENTACIONES')

# Zona horaria fija para la fecha de cada corrida: Cloud Run corre en UTC, así que con un
# datetime.now() pelado, a partir de las 21:00 de Argentina la corrida quedaba fechada al día
# siguiente (hoja del día, columna Fecha de Histórico, subcarpetas y el "Hoy es" del prompt).
# Requiere el paquete tzdata (está en requirements.txt): la imagen python:slim puede no traer la
# base de zonas horarias del sistema.
ZONA_HORARIA = ZoneInfo("America/Argentina/Buenos_Aires")


# Timeout por llamada a cada proveedor de IA: sin esto, un proveedor que se cuelga (pasó con
# NVIDIA) deja el pipeline entero esperando sin límite en vez de pasar al siguiente de la cadena.
TIMEOUT_IA_SEGUNDOS = 90

client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options=types.HttpOptions(timeout=TIMEOUT_IA_SEGUNDOS * 1000),  # en milisegundos
)

INSTRUCCIONES_SISTEMA = """
Eres un Consultor Senior de Empleabilidad de la Universidad de San Andrés (UdeSA).
Tu tarea es evaluar el texto extraído del perfil de LinkedIn de un estudiante y devolver
ÚNICAMENTE un objeto JSON válido con la evaluación.

CONTEXTO TEMPORAL:
Hoy es {FECHA_ACTUAL}. Evalúa la coherencia de los meses y años transcurridos en base a esta fecha actual.

DATOS A EXTRAER (no son parte del checklist, son datos factuales del perfil):
   - Carrera: identificá la carrera/título universitario del estudiante. Este dato puede aparecer en más de un lugar
     del perfil — revisá SIEMPRE estas tres fuentes antes de decidir, no te quedes con la primera
     que encuentres:
       1. Titular (headline): a veces dice directamente algo como "Estudiante de [Carrera] en
          [Universidad]".
       2. Extracto: a veces se menciona la carrera o el año que cursa dentro del texto libre.
       3. Educación: el nombre del título/grado listado ahí.
     Si varias fuentes coinciden, preferí la redacción de Educación por ser la más precisa y
     formal. Si Educación es ambigua o no aparece, usá lo que diga el Titular o el Extracto.

CRITERIOS DE EVALUACIÓN (CHECKLIST UDESA):
Para cada sección de "evaluacion_detallada", el "comentario" no debe ser solo diagnóstico: cuando
corresponda, sugerí concretamente qué podría agregar, cambiar o reordenar el estudiante (esto es
especialmente importante en Titular y Aptitudes, donde se detalla abajo qué tipo de sugerencia se
espera).
Si más abajo, después de este checklist, se incluye un bloque "PLAN DE ESTUDIOS DE REFERENCIA",
tenelo en cuenta como contexto adicional en toda la evaluación (no armes una sección aparte para
comentarlo, ni lo evalúes en sí mismo): usalo para enriquecer tus sugerencias en Titular y
Aptitudes en particular, y para juzgar si la Experiencia Laboral y el Acerca de son coherentes
con la formación de esa carrera. Si no se incluye ese bloque, evaluá con el resto de la
información del perfil normalmente, sin mencionar su ausencia.

1. Fundamentales:
   - Titular (Headline): ¿Es descriptivo, incluye palabras clave y comunica una propuesta de
     valor real, no solo el puesto o rol actual? Un buen titular suele combinar varios de estos
     elementos (no hace falta que tenga todos): rol real o aspiracional, ámbito de interés o
     expertise, propuesta de valor, palabras clave del sector, hard/technical skills,
     certificaciones o cursos relevantes, propósito profesional. En el "comentario", además de
     evaluar, sugerí concretamente qué estructura, conceptos o palabras podría incorporar el
     estudiante, basándote en el resto del perfil (Acerca de, Experiencia, Educación). y, si está
     disponible, en el PLAN DE ESTUDIOS DE REFERENCIA de su carrera.
     (Si el perfil no tiene ningún titular más allá del nombre de la persona, usá el estado 
     'No detectado' en vez de 'A Mejorar'.)
   - Ubicación: ¿Está presente?
   - URL: ¿Tiene un formato limpio y personalizado? No hace falta que sea literalmente
     "nombre-apellido" (linkedin.com/in/nombreapellido es solo un ejemplo posible); lo que
     importa es que NO sea la combinación de letras y números que asigna LinkedIn por defecto, y
     que se note que fue editada/personalizada.
   (Nota: Ignora Foto de Perfil y Banner, el formato PDF no las incluye).

2. Contenido y Experiencia:
   - Acerca de: ¿Es una narrativa convincente, optimizada con palabras clave y con un llamado a
     la acción claro al cierre? Idealmente el contenido toca estas tres partes (no hace falta que
     estén separadas explícitamente en el texto): (1) Quién sos — rol y/o formación actual;
     (2) Qué podés ofrecer — tu propuesta de valor: habilidades, proyectos y experiencias;
     (3) Hacia dónde vas — tu foco profesional, qué te apasiona, tus intereses.
     (Si el perfil no tiene ningún texto en la sección Acerca de, usá el estado 'No detectado' 
     en vez de 'A Mejorar'.)
   - Experiencia Laboral: Evaluá esto por cada puesto listado, no solo en general:
     (a) ¿Describe logros con verbos de acción y métricas/datos concretos, en vez de ser solo un
         listado de tareas o responsabilidades?
     (b) ¿La forma en que está redactada la experiencia es relevante respecto al rol real o
         aspiracional que se desprende del Titular? Si el Titular es demasiado genérico o
         incompleto como para inferir un rol target, indicá dentro del "comentario" que este
         punto "no se puede evaluar" por esa razón — no penalices el puntaje solo por esto.

   - Educación: ¿Está completa (se detalla el título obtenido o el nombre completo de la
     carrera, no solo "Universidad de San Andrés" sin especificar)? ¿Incluye la formación en
     UdeSA? Este último punto es EXCLUYENTE: si no aparecen mencionados los estudios en UdeSA,
     marcá esta sección como "A Mejorar" sin importar qué tan completo esté el resto del detalle.
     Limitate a educación formal (grado, posgrado; el secundario/bachillerato es opcional y no
     resta si no aparece).
   - Certificaciones: ¿Se agregaron credenciales relevantes y actualizadas (o cursos, que en
     algunos perfiles aparecen en una sección separada llamada "Cursos")? A diferencia de
     Educación, esta sección es un PLUS, no excluyente: si no aparece ninguna certificación ni
     curso, usá el estado "No detectado" (no es en sí mismo un problema grave). (Nota: evalúa: 
     nombre, vigencia si se menciona, relevancia para el perfil profesional.)

3. Aptitudes:
   - ¿Tiene al menos 50 aptitudes cargadas y relevantes? 
   - ¿Las aptitudes están ordenadas de forma estratégica? LinkedIn permite destacar/fijar las
     principales; evaluá si las que aparecen primero son las más relevantes respecto al Titular,
     el Acerca de, la Experiencia detallada y los puestos a los que aspira el estudiante, o si
     convendría reordenarlas.
   - En el "comentario", además de evaluar, sugerí concretamente 2 a 4 aptitudes que NO estén
     listadas pero que se puedan inferir razonablemente del resto del perfil (Titular, Acerca de,
     Experiencia) y, si está disponible, del PLAN DE ESTUDIOS DE REFERENCIA de su carrera. Si
     convendría reordenarlas, proponé el orden apropiado.
    (Nota: No evalúes validaciones/endorsements de terceros en las aptitudes; el formato PDF no
    permite verificarlas de forma confiable.)
REGLAS DE PUNTUACIÓN Y SEMÁFORO:
1. Asigna un "puntaje_general" del 1 al 100 basado ÚNICAMENTE en el cumplimiento del checklist de
   arriba (secciones 1, 2 y 3). El bloque "analisis_bonus" (más abajo) es puramente informativo:
   ninguno de sus campos debe influir en el "puntaje_general" ni en el "color_semaforo".
2. El "color_semaforo" se calcula estrictamente: Verde (75-100), Amarillo (50-74), Rojo (0-49).

CRITERIO PARA "punto_fuerte", "punto_critico" Y "proxima_accion" (MUY IMPORTANTE, LEÉ ESTO ANTES
DE COMPLETAR EL JSON):
- "punto_fuerte" NO es simplemente "la sección está presente" o "cumple lo mínimo esperable" (por
  ejemplo, que el titular solo mencione la carrera y la universidad NO alcanza para ser un punto
  fuerte). Solo cuenta como fuerte algo que va MÁS ALLÁ de lo básico: una narrativa con impacto
  real, logros con métricas concretas, un uso de palabras clave claramente diferencial, etc. Si
  ninguna sección del perfil alcanza ese nivel, no inventes un punto fuerte artificial: usá
  literalmente el string "Sin puntos destacados en este perfil."
- "punto_critico" tiene que ser el problema de MAYOR impacto para la empleabilidad del estudiante,
  no cualquier detalle menor. Priorizá en este orden: (1) secciones fundamentales ausentes o vacías
  (Acerca de, Experiencia Laboral, no tener UdeSA en Educación), (2) contenido presente pero de baja
  calidad (sin métricas, sin palabras clave), (3) detalles cosméticos (URL sin personalizar,
  aptitudes insuficientes). Si el perfil no tiene ningún problema relevante, usá literalmente el
  string "Sin puntos críticos relevantes."
- "proxima_accion" tiene que derivarse DIRECTAMENTE del "punto_critico" que identificaste: es el
  paso concreto e inmediato para resolver ESE problema puntual, no una lista genérica de tareas. Si
  no hay puntos críticos, sugerí como mucho un ajuste menor de pulido, o usá literalmente el string
  "Sin acciones prioritarias en este momento."

CRITERIO PARA "analisis_bonus" (INFORMATIVO, NO AFECTA EL PUNTAJE NI EL SEMÁFORO):
- "recomendaciones": indicá si el perfil tiene recomendaciones escritas de colegas, managers o
  clientes, e idealmente si llega a 2-3 o más. Es un dato complementario, no un déficit grave si
  falta.
- "secciones_adicionales": mencioná cualquier sección extra que sume valor o personalización más
  allá del checklist básico (por ejemplo: Idiomas, Proyectos, Voluntariados, Publicaciones,
  Cursos, Logros/Honores). Si no encontrás ninguna, indicalo brevemente ("No se detectan
  secciones adicionales.").

ESTRUCTURA EXACTA DEL JSON:
{
  "apellido_estudiante": "Apellido",
  "nombre_estudiante": "Nombre",
  "carrera_estudiante": "<nombre de la carrera detectada>",
  "puntaje_general": 0,
  "color_semaforo": "Verde, Amarillo o Rojo",
  "punto_fuerte": "El mayor acierto del perfil, o 'Sin puntos destacados en este perfil.'",
  "punto_critico": "El problema de mayor impacto, o 'Sin puntos críticos relevantes.'",
  "proxima_accion": "El paso concreto derivado de punto_critico, o 'Sin acciones prioritarias en este momento.'",
  "evaluacion_detallada": {
    "titular": {"estado": "Aprobado | A Mejorar | No detectado", "comentario": "..."},
    "ubicacion": {"estado": "Aprobado | A Mejorar", "comentario": "..."},
    "url": {"estado": "Aprobado | A Mejorar", "comentario": "..."},
    "acerca_de": {"estado": "Aprobado | A Mejorar | No detectado", "comentario": "..."},
    "experiencia_laboral": {"estado": "Aprobado | A Mejorar | No detectado", "comentario": "..."},
    "educacion": {"estado": "Aprobado | A Mejorar", "comentario": "..."},
    "certificaciones": {"estado": "Aprobado | A Mejorar | No detectado", "comentario": "..."},
    "aptitudes": {"estado": "Aprobado | A Mejorar | No detectado", "comentario": "..."}
  },
  "analisis_bonus": {
    "recomendaciones": "...",
    "secciones_adicionales": "..."
  }
}

IMPORTANTE: "punto_fuerte", "punto_critico" y "proxima_accion" son tres campos de texto
INDEPENDIENTES (no uses viñetas ni los combines en un solo string). Cada uno debe ser una o dos
oraciones, concisas y directas. Los campos de "analisis_bonus" son igual de concisos, pero NUNCA
deben afectar "puntaje_general" ni "color_semaforo".
"""

# ==============================================================================
# MÓDULO DE AUTENTICACIÓN (OAuth 2.0)
# ==============================================================================
# Separado en dos capas:
#
#   1. CONSEGUIR credenciales (Credentials): hay dos formas de llegar a un objeto Credentials
#      válido — autenticar_google_cli() abajo (login de escritorio, un solo token.json
#      compartido, pensado para correr `python main.py` a mano) o el login web por persona que
#      vive en web_app.py (cada usuario logueado en el navegador tiene su propio archivo de
#      token en tokens/, ver web_app.py). main.py no sabe ni le importa cuál de las dos se usó.
#
#   2. CONSTRUIR el servicio de Google a partir de esas credenciales (construir_servicio_*):
#      esto es lo mismo sin importar de dónde salieron las credenciales, así que el resto del
#      pipeline (ejecutar_pipeline, etc.) recibe siempre un objeto Credentials ya resuelto y
#      arma los servicios con las funciones de abajo.
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials


def autenticar_google_cli():
    """
    Login de escritorio para uso por CONSOLA (`python main.py`): un solo token.json compartido
    en la raíz del repo, pensado para correrlo a mano una persona a la vez. El panel web NO usa
    esta función — ahí cada persona tiene su propio login y su propio archivo en tokens/
    (ver web_app.py: auth_login / auth_callback / cargar_credenciales_usuario).
    """
    creds = None

    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                os.remove('token.json')
                creds = None

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
            creds = flow.run_local_server(port=0)

        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return creds


def construir_servicio_drive(creds):
    """Arma el cliente de Drive a partir de un objeto Credentials ya resuelto (sin importar si
    vino del login de consola o del login web de una persona puntual)."""
    try:
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"Error al construir el servicio de Drive: {e}")
        return None


# ==============================================================================
# MÓDULO 1: GOOGLE DRIVE (Extracción - Continuación)
# ==============================================================================

def listar_pdfs_en_carpeta(servicio, folder_id):
    """Lista los PDFs de la carpeta. Si la API falla (carpeta inexistente, o la persona logueada
    no tiene acceso), lanza RuntimeError en vez de devolver una lista vacía: antes el error se
    perdía en la consola y la web mostraba "Se encontraron 0 perfiles" como si estuviera todo bien."""
    archivos = []
    try:
        query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
        pagina_token = None
        while True:
            resultados = servicio.files().list(
                q=query, spaces='drive', fields='nextPageToken, files(id, name)', pageToken=pagina_token,
                supportsAllDrives=True, includeItemsFromAllDrives=True
            ).execute()
            archivos.extend(resultados.get('files', []))
            pagina_token = resultados.get('nextPageToken')
            if not pagina_token:
                break
        return archivos
    except Exception as e:
        raise RuntimeError(
            f"No se pudo listar la carpeta de perfiles de Drive (ID_CARPETA). Revisá que exista y "
            f"que tu cuenta tenga acceso: {_resumir_error(e)}"
        ) from e


def extraer_texto_drive_en_memoria(servicio, file_id):
    """
    Descarga el PDF y devuelve (texto_completo, url_perfil).
    La URL se busca primero entre los hipervínculos reales embebidos en el PDF (mucho más
    confiable que el texto: no depende de que el texto visible se haya partido en dos líneas
    al extraerlo, ni de errores de tipografía/kerning). Si el PDF no tiene un hipervínculo de
    LinkedIn embebido, se cae a un regex sobre el texto extraído como respaldo.
    """
    try:
        request = servicio.files().get_media(fileId=file_id, supportsAllDrives=True)
        archivo_memoria = io.BytesIO()
        downloader = MediaIoBaseDownload(archivo_memoria, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()

        archivo_memoria.seek(0)
        texto_completo = ""
        url_perfil = None
        with pdfplumber.open(archivo_memoria) as pdf:
            for pagina in pdf.pages:
                texto_extraido = pagina.extract_text()
                if texto_extraido:
                    texto_completo += texto_extraido + "\n"

                if not url_perfil:
                    for hipervinculo in pagina.hyperlinks:
                        uri = (hipervinculo.get('uri') or '')
                        if 'linkedin.com/in/' in uri.lower():
                            url_perfil = uri
                            break

        if not url_perfil:
            url_perfil = extraer_url_perfil(texto_completo)  # respaldo por regex sobre el texto

        return texto_completo, url_perfil
    except Exception as e:
        print(f"   [⚠️ No se pudo descargar/leer el PDF {file_id}: {e}]")
        return None, None

PATRON_URL_LINKEDIN = re.compile(
    r'(https?://)?(www\.)?linkedin\.com/in/[A-Za-z0-9\-_%.]+/?',
    re.IGNORECASE
)


def extraer_url_perfil(texto_perfil):
    """
    Busca la URL de LinkedIn directo en el texto extraído del PDF (regex, NO depende de la IA,
    para evitar que la transcriba mal). Devuelve la URL completa con https:// o None si no la
    encuentra.
    """
    if not texto_perfil:
        return None

    coincidencia = PATRON_URL_LINKEDIN.search(texto_perfil)
    if not coincidencia:
        return None

    url = coincidencia.group(0).rstrip('/')
    if url.lower().startswith('http'):
        return url
    if url.lower().startswith('www.'):
        return f"https://{url}"
    return f"https://www.{url}"

# ==============================================================================
# MÓDULO 1B: GOOGLE DRIVE (Subcarpetas por fecha y movimiento de archivos)
# ==============================================================================

def obtener_o_crear_subcarpeta(servicio_drive, id_carpeta_padre, nombre_subcarpeta):
    """
    Busca una subcarpeta con nombre 'nombre_subcarpeta' dentro de id_carpeta_padre.
    Si no existe, la crea. Idempotente: si se corre dos veces el mismo día, no duplica.
    """
    query = (
        f"'{id_carpeta_padre}' in parents and name='{nombre_subcarpeta}' "
        "and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    resultados = servicio_drive.files().list(
        q=query, spaces='drive', fields='files(id, name)',
        supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    encontradas = resultados.get('files', [])

    if encontradas:
        return encontradas[0]['id']

    metadata = {
        'name': nombre_subcarpeta,
        'mimeType': 'application/vnd.google-apps.folder',
        'parents': [id_carpeta_padre]
    }
    carpeta = servicio_drive.files().create(body=metadata, fields='id', supportsAllDrives=True).execute()
    print(f"📁 Subcarpeta creada: '{nombre_subcarpeta}'")
    return carpeta.get('id')


def mover_archivo_a_carpeta(servicio_drive, file_id, id_carpeta_destino, id_carpeta_origen):
    """Mueve un archivo de Drive de una carpeta a otra (Drive maneja carpetas como 'padres')."""
    try:
        servicio_drive.files().update(
            fileId=file_id,
            addParents=id_carpeta_destino,
            removeParents=id_carpeta_origen,
            fields='id, parents',
            supportsAllDrives=True
        ).execute()
        return True
    except Exception as e:
        print(f"   [⚠️ No se pudo mover el archivo {file_id} a la carpeta destino: {e}]")
        return False


# ==============================================================================
# CONFIGURACIÓN MULTI-PROVEEDOR
# ==============================================================================


# Orden de la cadena de fallback. Gemini primero (mejor calidad de análisis), después OpenAI
# (GPT-6 Luna: el plan gratis de OpenAI da 50 pedidos por día, que alcanzan como respaldo) y al
# final los proveedores gratuitos alternativos. Un proveedor sin API key falla al instante y se
# pasa al siguiente.
ORDEN_PROVEEDORES = ["gemini", "openai", "groq", "nvidia"]

cliente_openai = OpenAI(api_key=OPENAI_API_KEY, timeout=TIMEOUT_IA_SEGUNDOS) if OPENAI_API_KEY else None

cliente_groq = Groq(api_key=GROQ_API_KEY, timeout=TIMEOUT_IA_SEGUNDOS) if GROQ_API_KEY else None

cliente_nvidia = OpenAI(
    api_key=NVIDIA_API_KEY,
    base_url="https://integrate.api.nvidia.com/v1",
    timeout=TIMEOUT_IA_SEGUNDOS,
) if NVIDIA_API_KEY else None

# ==============================================================================
# CACHÉ POR HASH DE CONTENIDO (evita re-analizar el mismo perfil dos veces)
# ==============================================================================

ARCHIVO_CACHE = "cache_analisis.json"


def cargar_cache():
    return almacenamiento_estado.leer_json(ARCHIVO_CACHE, {})


def guardar_cache(cache):
    try:
        almacenamiento_estado.escribir_json(ARCHIVO_CACHE, cache)
    except Exception as e:
        print(f"   [⚠️ No se pudo guardar la caché: {e}]")


def calcular_hash_perfil(texto_perfil, plan_de_estudios=None):
    # Si cambian las instrucciones o el plan de estudios inyectado, cambia el hash (se re-analiza).
    contenido = texto_perfil + INSTRUCCIONES_SISTEMA + (plan_de_estudios or "")
    return hashlib.sha256(contenido.encode('utf-8')).hexdigest()


CACHE_ANALISIS = cargar_cache()

# ==============================================================================
# MÓDULO 1C: PRECLASIFICACIÓN DE CARRERA Y PLANES DE ESTUDIO (UdeSA)
# ==============================================================================
# Carpeta local con un archivo .txt por carrera (plan de estudios en texto plano). Se busca
# relativa a donde corre el script. Creála en la raíz del repo, al mismo nivel que main.py:
#
#   planes_de_estudio/
#     ingenieria_en_inteligencia_artificial.txt
#     tecnologia_digital.txt
#     ...
#
CARPETA_PLANES_DE_ESTUDIO = "planes_de_estudio"

# Mapa de carreras de UdeSA: nombre canónico -> palabras clave para detectarla en el texto crudo
# del PDF (sin usar IA) + nombre del archivo con su plan de estudios + nivel (grado/posgrado).
#
# NOTA: este diccionario YA NO se hardcodea acá. Se guarda en DOS archivos separados por nivel
# (carreras_grado.json y carreras_posgrado.json, mismo nivel que este archivo) para que se
# puedan filtrar/agrupar por nivel en la web sin tener que inferirlo de ningún otro dato. En
# memoria, CARRERAS_UDESA sigue siendo UN SOLO diccionario combinado (con 'nivel' como campo más
# de cada entrada) para no tener que tocar el resto del pipeline (detección de carrera, modo
# cohorte, etc.) — la separación en dos archivos es solo un detalle de PERSISTENCIA. Usá
# agregar_o_actualizar_carrera() / eliminar_carrera() en vez de mutar CARRERAS_UDESA a mano.
ARCHIVO_CARRERAS_POR_NIVEL = {
    "grado": "carreras_grado.json",
    "posgrado": "carreras_posgrado.json",
}
ARCHIVO_CARRERAS_LEGACY = "carreras.json"  # el archivo único de antes de separar por nivel

def ruta_plan_de_estudios(datos_carrera):
    """Ruta del .txt del plan de estudios de una carrera, separado en subcarpeta grado/ o
    posgrado/ según su nivel (mismo default que guardar_carreras: si el nivel no es válido,
    asume 'grado'). Centraliza acá la construcción de esta ruta para que main.py y web_app.py
    no la arme cada uno por su cuenta."""
    nivel = datos_carrera.get('nivel') if datos_carrera.get('nivel') in ARCHIVO_CARRERAS_POR_NIVEL else 'grado'
    return os.path.join(CARPETA_PLANES_DE_ESTUDIO, nivel, datos_carrera.get("archivo_plan", ""))


def _migrar_planes_de_estudio_a_subcarpetas_si_hace_falta():
    """
    Los .txt de planes de estudio vivían todos juntos en planes_de_estudio/ (grado y posgrado
    mezclados) — a diferencia de los JSON de carreras, que ya estaban separados. Esta función
    los reparte en planes_de_estudio/grado/ y planes_de_estudio/posgrado/.

    Es idempotente POR ARCHIVO (no con un flag global como _migrar_carreras_legacy_si_hace_falta):
    para cada carrera, si el .txt viejo (ruta plana) existe y el nuevo (con subcarpeta) todavía
    no, lo mueve; si ya está migrado, no toca nada. Así no importa si se agregan carreras nuevas
    después de correr esto una vez, ni si el proceso se reinicia a mitad de la migración.
    """
    for nombre_carrera, datos in CARRERAS_UDESA.items():
        if not datos.get("archivo_plan"):
            continue
        ruta_vieja = os.path.join(CARPETA_PLANES_DE_ESTUDIO, datos["archivo_plan"])
        ruta_nueva = ruta_plan_de_estudios(datos)
        if not almacenamiento_estado.existe(ruta_vieja) or almacenamiento_estado.existe(ruta_nueva):
            continue
        try:
            texto = almacenamiento_estado.leer_texto(ruta_vieja)
            almacenamiento_estado.escribir_texto(ruta_nueva, texto)
            almacenamiento_estado.borrar(ruta_vieja)
            print(f"   [ℹ️ Migración automática: '{ruta_vieja}' -> '{ruta_nueva}' ({nombre_carrera}).]")
        except Exception as e:
            print(f"   [⚠️ No se pudo migrar el plan de '{nombre_carrera}' a subcarpetas: {e}]")

def _migrar_carreras_legacy_si_hace_falta():
    """
    Si todavía no existen los archivos separados por nivel pero sí existe el carreras.json de
    antes de esta funcionalidad, migra TODO su contenido a carreras_grado.json (a la fecha de
    este cambio, las carreras ya cargadas eran todas de grado) y crea un carreras_posgrado.json
    vacío. Corre una sola vez — una vez migrado, carreras.json deja de leerse (se puede borrar a
    mano, o dejarlo de recuerdo, no molesta).
    """
    ya_migrado = any(almacenamiento_estado.existe(a) for a in ARCHIVO_CARRERAS_POR_NIVEL.values())
    if ya_migrado or not almacenamiento_estado.existe(ARCHIVO_CARRERAS_LEGACY):
        return

    carreras_viejas = almacenamiento_estado.leer_json(ARCHIVO_CARRERAS_LEGACY, None)
    if carreras_viejas is None:
        print(f"   [⚠️ No se pudo migrar '{ARCHIVO_CARRERAS_LEGACY}'.]")
        return

    almacenamiento_estado.escribir_json(ARCHIVO_CARRERAS_POR_NIVEL["grado"], carreras_viejas)
    almacenamiento_estado.escribir_json(ARCHIVO_CARRERAS_POR_NIVEL["posgrado"], {})

    print(f"   [ℹ️ Migración automática: se movieron {len(carreras_viejas)} carreras de "
          f"'{ARCHIVO_CARRERAS_LEGACY}' a '{ARCHIVO_CARRERAS_POR_NIVEL['grado']}' (todas como "
          f"'grado' por default). Si alguna en realidad es de posgrado, editala desde la web.]")


def cargar_carreras():
    """Lee carreras_grado.json y carreras_posgrado.json y los combina en un solo diccionario en
    memoria, agregándole 'nivel' a cada entrada según de qué archivo salió."""
    _migrar_carreras_legacy_si_hace_falta()

    combinado = {}
    for nivel, archivo in ARCHIVO_CARRERAS_POR_NIVEL.items():
        datos = almacenamiento_estado.leer_json(archivo, None)
        if datos is None:
            continue
        for nombre, info in datos.items():
            info = dict(info)
            info['nivel'] = nivel
            combinado[nombre] = info
    return combinado


def guardar_carreras(carreras):
    """Separa el diccionario combinado por 'nivel' y escribe cada mitad en su archivo
    correspondiente (carreras_grado.json / carreras_posgrado.json). El campo 'nivel' no se
    persiste DENTRO de cada archivo (es redundante, ya lo dice el nombre del archivo) — se
    vuelve a agregar al leer, en cargar_carreras()."""
    por_nivel = {nivel: {} for nivel in ARCHIVO_CARRERAS_POR_NIVEL}
    for nombre, info in carreras.items():
        nivel = info.get('nivel') if info.get('nivel') in ARCHIVO_CARRERAS_POR_NIVEL else 'grado'
        info_sin_nivel = {k: v for k, v in info.items() if k != 'nivel'}
        por_nivel[nivel][nombre] = info_sin_nivel

    for nivel, archivo in ARCHIVO_CARRERAS_POR_NIVEL.items():
        almacenamiento_estado.escribir_json(archivo, por_nivel[nivel])


CARRERAS_UDESA = cargar_carreras()
_migrar_planes_de_estudio_a_subcarpetas_si_hace_falta()

def recargar_carreras():
    """Vuelve a leer los archivos de carreras del disco y actualiza CARRERAS_UDESA in-place
    (mismo dict, mismas referencias) para que el resto de las funciones que ya lo tienen
    importado vean el cambio sin reiniciar el proceso."""
    CARRERAS_UDESA.clear()
    CARRERAS_UDESA.update(cargar_carreras())
    return CARRERAS_UDESA


def _slug_archivo_plan(nombre_carrera):
    """Nombre de archivo .txt derivado del nombre de la carrera (minúsculas, sin tildes,
    espacios -> guion bajo), para cuando se crea una carrera nueva desde la web y no se
    especifica un archivo_plan a mano."""
    base = _normalizar_texto(nombre_carrera)
    base = re.sub(r'[^a-z0-9]+', '_', base).strip('_')
    return f"{base}.txt"


def agregar_o_actualizar_carrera(nombre_carrera, palabras_clave, nivel=None, archivo_plan=None):
    """Crea o actualiza una entrada de CARRERAS_UDESA y persiste en el archivo del nivel
    correspondiente. Si la carrera ya existía y no se pasa nivel/archivo_plan, conserva los que
    tenía (para no romper el link a un plan ya cargado, o cambiarle el nivel sin querer, al
    editar solo las palabras clave)."""
    nombre_carrera = nombre_carrera.strip()
    if not nombre_carrera:
        raise ValueError("El nombre de la carrera no puede estar vacío.")

    palabras_clave = [p.strip() for p in palabras_clave if p.strip()]
    if not palabras_clave:
        raise ValueError("Hace falta al menos una palabra clave para detectar la carrera.")

    existente = CARRERAS_UDESA.get(nombre_carrera, {})
    nivel_final = nivel or existente.get("nivel")
    if nivel_final not in ARCHIVO_CARRERAS_POR_NIVEL:
        raise ValueError("El nivel tiene que ser 'grado' o 'posgrado'.")

    CARRERAS_UDESA[nombre_carrera] = {
        "palabras_clave": palabras_clave,
        "archivo_plan": archivo_plan or existente.get("archivo_plan") or _slug_archivo_plan(nombre_carrera),
        "nivel": nivel_final,
    }
    guardar_carreras(CARRERAS_UDESA)
    return CARRERAS_UDESA[nombre_carrera]


def eliminar_carrera(nombre_carrera, borrar_archivo_plan=False):
    """Saca la carrera de CARRERAS_UDESA y persiste (en el archivo de su nivel). Por defecto NO
    borra el .txt del plan del disco (podría estar linkeado desde otro lado o quererse reusar);
    pasá borrar_archivo_plan=True para borrarlo también."""
    datos = CARRERAS_UDESA.pop(nombre_carrera, None)
    if datos is None:
        return False
    guardar_carreras(CARRERAS_UDESA)
    if borrar_archivo_plan and datos.get("archivo_plan"):
        ruta = ruta_plan_de_estudios(datos)
        almacenamiento_estado.borrar(ruta)
    return True


def guardar_texto_plan_de_estudios(nombre_carrera, texto):
    """Sobrescribe directo el .txt del plan de estudios de una carrera con el texto que se le
    pase — usado por el editor de planes de la web (ver/editar sin necesidad de resubir el PDF).
    Lanza ValueError si la carrera no existe."""
    if nombre_carrera not in CARRERAS_UDESA:
        raise ValueError(f"No existe la carrera '{nombre_carrera}'.")
    ruta = ruta_plan_de_estudios(CARRERAS_UDESA[nombre_carrera])
    almacenamiento_estado.escribir_texto(ruta, texto)
    return len(texto)


def _normalizar_texto(texto):
    """Minúsculas y sin tildes, para que el matching de keywords no dependa de cómo haya
    quedado el acento tras la extracción del PDF (evita duplicar cada keyword con y sin
    tilde a mano)."""
    texto = texto.lower()
    texto_sin_tildes = unicodedata.normalize('NFKD', texto)
    return ''.join(c for c in texto_sin_tildes if not unicodedata.combining(c))


def _nombre_carpeta_carrera(carrera_detectada):
    """Nombre de subcarpeta para agrupar informes por carrera. Usa el nombre canónico de
    CARRERAS_UDESA si coincide con alguna carrera conocida (evita carpetas duplicadas por
    variantes de redacción de la IA, p. ej. 'Ingeniería en IA' vs 'Ingeniería en Inteligencia
    Artificial'); si no coincide con ninguna, usa el texto crudo que devolvió la IA."""
    if not carrera_detectada:
        return "Sin carrera detectada"
    texto_normalizado = _normalizar_texto(carrera_detectada)
    for nombre_canonico in CARRERAS_UDESA:
        if _normalizar_texto(nombre_canonico) == texto_normalizado:
            return nombre_canonico
    return carrera_detectada


def detectar_carrera_por_keywords(texto_perfil):
    if not texto_perfil:
        return None

    texto_normalizado = _normalizar_texto(texto_perfil)

    # Se juntan TODAS las apariciones (carrera, inicio, fin) de cualquier keyword en el texto.
    coincidencias = []
    for nombre_carrera, datos in CARRERAS_UDESA.items():
        for palabra_clave in datos["palabras_clave"]:
            clave = _normalizar_texto(palabra_clave)
            if not clave:
                continue
            inicio = texto_normalizado.find(clave)
            while inicio != -1:
                coincidencias.append((nombre_carrera, inicio, inicio + len(clave)))
                inicio = texto_normalizado.find(clave, inicio + 1)

    # Una keyword puede ser prefijo de la frase de OTRA carrera: "licenciatura en economia" aparece
    # dentro de "Licenciatura en Economía Empresarial". Si dos coincidencias de carreras distintas
    # se pisan en el texto, gana la que llega más lejos (la frase más completa); la otra era solo
    # un pedazo de ésta. Entre coincidencias que NO se pisan se mantiene el orden del diccionario.
    def _es_pedazo_de_otra(c1):
        for c2 in coincidencias:
            if c2[0] == c1[0]:
                continue
            se_pisan = c2[1] < c1[2] and c1[1] < c2[2]
            if se_pisan and (c2[2] > c1[2] or (c2[2] == c1[2] and c2[1] < c1[1])):
                return True
        return False

    for coincidencia in coincidencias:
        if not _es_pedazo_de_otra(coincidencia):
            return coincidencia[0]
    return None


def cargar_plan_de_estudios(nombre_carrera):
    """Lee el archivo .txt del plan de estudios de la carrera detectada, si existe."""
    if not nombre_carrera or nombre_carrera not in CARRERAS_UDESA:
        return None

    ruta = ruta_plan_de_estudios(CARRERAS_UDESA[nombre_carrera])
    try:
        return almacenamiento_estado.leer_texto(ruta)
    except Exception as e:
        print(f"   [⚠️ No se pudo leer el plan de estudios de '{nombre_carrera}': {e}]")
        return None


# ==============================================================================
# MÓDULO 2: MULTI-PROVEEDOR IA (Gemini → OpenAI → Groq → NVIDIA con backoff y caché)
# ==============================================================================

CODIGOS_HTTP_LIMITE = (429, 503)  # 429 = límite de cuota/tasa, 503 = proveedor saturado
FRASES_ERROR_LIMITE = (
    "rate limit", "rate_limit", "ratelimit", "too many requests",
    "quota", "resource_exhausted", "overloaded", "high demand",
)
# Cupo agotado hasta el día siguiente (o sin saldo): esperar unos segundos no lo resuelve. OpenAI
# dice "requests per day (RPD)" o "insufficient_quota"; Gemini nombra la cuota "…PerDay…".
FRASES_CUPO_DIARIO = ("insufficient_quota", "per day", "perday")


def _es_cupo_diario_agotado(error):
    """True si el error es un límite que no se libera reintentando en segundos."""
    error_msg = str(error).lower()
    return any(frase in error_msg for frase in FRASES_CUPO_DIARIO)


def _es_error_de_limite(error):
    """True si el error es de cuota/saturación (vale la pena esperar y reintentar), False si es
    cualquier otra cosa (pasar directo al siguiente proveedor).

    Primero mira el código HTTP que traen las excepciones de los SDK (google-genai lo expone como
    .code; openai y groq como .status_code). Si no hay código, se cae a frases puntuales del
    mensaje — nunca a subcadenas sueltas: antes se buscaba "rate", y eso matcheaba
    "generateContent" (que aparece en los 404 de Gemini cuando dan de baja un modelo), con lo
    que se esperaban 2+4+8 s por perfil reintentando algo que nunca iba a andar."""
    for atributo in ("code", "status_code"):
        codigo = getattr(error, atributo, None)
        if isinstance(codigo, int):
            return codigo in CODIGOS_HTTP_LIMITE

    error_msg = str(error).lower()
    return any(frase in error_msg for frase in FRASES_ERROR_LIMITE)


# Cada adaptador devuelve (texto, uso): uso son los tokens de la llamada como
# {'entrada', 'salida', 'total'}, o None si el proveedor no los informó. Sirven para el resumen
# de uso de IA de cada corrida (promedio de tokens por perfil, ver _resumir_uso_ia).

def _armar_uso(entrada, salida, total):
    if total is None and entrada is not None and salida is not None:
        total = entrada + salida
    if total is None:
        return None
    return {"entrada": entrada, "salida": salida, "total": total}


def _uso_de_respuesta_chat(respuesta):
    """Tokens de una respuesta con formato OpenAI (Groq y NVIDIA usan el mismo). En los modelos
    que razonan (como GPT-6 Luna), completion_tokens ya incluye los tokens de razonamiento."""
    uso = getattr(respuesta, 'usage', None)
    if uso is None:
        return None
    return _armar_uso(uso.prompt_tokens, uso.completion_tokens, uso.total_tokens)


def _llamar_gemini(prompt):
    respuesta = client.models.generate_content(
        model='gemini-flash-latest',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2
        )
    )
    uso = respuesta.usage_metadata
    if uso is None:
        return respuesta.text, None
    # total_token_count incluye también los tokens de "razonamiento" del modelo, si los usó.
    return respuesta.text, _armar_uso(uso.prompt_token_count, uso.candidates_token_count, uso.total_token_count)


def _llamar_openai(prompt):
    if not cliente_openai:
        raise RuntimeError("OPENAI_API_KEY no configurada.")
    respuesta = cliente_openai.chat.completions.create(
        model="gpt-6-luna",
        messages=[{"role": "user", "content": prompt}],
        # Luna razona antes de responder y esos tokens se cuentan como salida: el tope deja lugar
        # al razonamiento más el JSON. OpenAI calcula el límite por minuto (TPM) con este tope y no
        # con lo que se usa, así que no conviene subirlo de más.
        max_completion_tokens=8000,
        response_format={"type": "json_object"},
    )
    return respuesta.choices[0].message.content, _uso_de_respuesta_chat(respuesta)


def _llamar_groq(prompt):
    if not cliente_groq:
        raise RuntimeError("GROQ_API_KEY no configurada.")
    respuesta = cliente_groq.chat.completions.create(
        model="qwen/qwen3.8-27b",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_completion_tokens=2048,
        top_p=1,
        stream=False,          # necesitamos la respuesta completa para parsear JSON, no streaming
        response_format={"type": "json_object"},
        stop=None
    )
    return respuesta.choices[0].message.content, _uso_de_respuesta_chat(respuesta)


def _llamar_nvidia(prompt):
    if not cliente_nvidia:
        raise RuntimeError("NVIDIA_API_KEY no configurada.")
    respuesta = cliente_nvidia.chat.completions.create(
        model="deepseek-ai/deepseek-v4.1-flash",  # el anterior (v4-flash-0731) fue dado de baja el 2026-09-21 (HTTP 410)
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        top_p=0.7,
        max_tokens=4096,
        stream=False
    )
    texto = respuesta.choices[0].message.content
    # NVIDIA no siempre soporta json mode estricto según el modelo; limpiamos
    # posibles fences de markdown por si el modelo los agrega igual.
    return texto.replace("```json", "").replace("```", "").strip(), _uso_de_respuesta_chat(respuesta)


ADAPTADORES = {
    "gemini": _llamar_gemini,
    "openai": _llamar_openai,
    "groq": _llamar_groq,
    "nvidia": _llamar_nvidia,
}


def _proveedor_configurado(nombre_proveedor):
    """True si el proveedor tiene su API key cargada."""
    return {
        "gemini": bool(GEMINI_API_KEY),
        "openai": cliente_openai is not None,
        "groq": cliente_groq is not None,
        "nvidia": cliente_nvidia is not None,
    }.get(nombre_proveedor, False)


def _resumir_error(error, largo=160):
    """Error de una API en una sola línea corta, para mostrarlo en el log de la web."""
    texto = " ".join(str(error).split())
    return texto if len(texto) <= largo else texto[:largo] + "…"


def _intentar_proveedor_con_backoff(nombre_proveedor, prompt, intentos_maximos=3, avisar=print):
    """Reintenta un proveedor con backoff exponencial.

    Devuelve (json_parseado, uso_de_tokens, None) si salió bien, o (None, None, motivo) si no:
    motivo es "cuota" si se agotaron los reintentos por límite de cuota o saturación (429/503),
    y "error" si fue cualquier otro error.

    avisar: función avisar(mensaje) para reportar reintentos y fallos. En consola es print; el
    pipeline le pasa una que además lo manda al log de la web."""
    funcion = ADAPTADORES[nombre_proveedor]

    for intento in range(intentos_maximos):
        try:
            texto_crudo, uso = funcion(prompt)
            return json.loads(texto_crudo), uso, None
        except Exception as e:
            if _es_error_de_limite(e) and _es_cupo_diario_agotado(e):
                avisar(f"   [❌ {nombre_proveedor}: cupo diario agotado o sin saldo ({_resumir_error(e, 100)}). "
                       "No se reintenta.]")
                return None, None, "cuota"
            if _es_error_de_limite(e):
                espera = 2 ** (intento + 1)  # 2s, 4s, 8s...
                avisar(f"   [⏳ {nombre_proveedor}: límite o saturación ({_resumir_error(e, 100)}). "
                       f"Reintento {intento+1}/{intentos_maximos} en {espera}s...]")
                time.sleep(espera)
            else:
                avisar(f"   [❌ {nombre_proveedor}: error irrecuperable → {_resumir_error(e)}]")
                return None, None, "error"

    avisar(f"   [❌ {nombre_proveedor}: se agotaron los reintentos (probablemente límite de cuota).]")
    return None, None, "cuota"


def analizar_perfil_con_ia(texto_perfil, fecha_hoy, carrera_cohorte=None, avisar=print, registro=None):
    """Orquestador: preclasificación de carrera → caché → Gemini → OpenAI → Groq → NVIDIA.

    carrera_cohorte: si se pasa (modo "cohorte completa"), pisa la detección automática por
    keywords para TODO el lote, avisando por consola si un perfil puntual no la menciona (no
    se descarta el perfil, solo se informa la inconsistencia).

    avisar: función avisar(mensaje) para los mensajes importantes (avisos de cohorte, cuota
    agotada, cambio de proveedor, fallo total). En consola es print; el pipeline le pasa una que
    además los manda al log de la web.

    registro: dict opcional que se completa con lo que pasó en este perfil, para el resumen de
    uso de IA de la corrida (ver _resumir_uso_ia): 'proveedor' (el que lo analizó, 'caché' si no
    hizo falta llamar a ninguno, o None si fallaron todos), 'proveedor_original' (para 'caché':
    quién lo había analizado), 'uso' (tokens de la llamada que salió bien) y 'fallos' (lista de
    {'proveedor', 'motivo'} de los proveedores que fallaron antes)."""
    if registro is None:
        registro = {}
    registro.update(proveedor=None, proveedor_original=None, uso=None, fallos=[])

    carrera_detectada = detectar_carrera_por_keywords(texto_perfil)

    if carrera_cohorte:
        if carrera_detectada and carrera_detectada != carrera_cohorte:
            avisar(f"   [⚠️ Este perfil menciona '{carrera_detectada}', pero se analiza como "
                  f"'{carrera_cohorte}' (modo cohorte).]")
        elif not carrera_detectada:
            avisar(f"   [⚠️ Este perfil no menciona ninguna carrera reconocida; se analiza "
                  f"igual como '{carrera_cohorte}' (modo cohorte).]")
        carrera_detectada = carrera_cohorte

    plan_de_estudios = cargar_plan_de_estudios(carrera_detectada) if carrera_detectada else None

    hash_perfil = calcular_hash_perfil(texto_perfil, plan_de_estudios)
    if hash_perfil in CACHE_ANALISIS:
        print("   [💾 Resultado obtenido de caché, no se llamó a ninguna IA.]")
        registro["proveedor"] = "caché"
        registro["proveedor_original"] = CACHE_ANALISIS[hash_perfil].get("proveedor_ia")
        return CACHE_ANALISIS[hash_perfil]

    instrucciones_con_fecha = INSTRUCCIONES_SISTEMA.replace("{FECHA_ACTUAL}", fecha_hoy)

    if plan_de_estudios:
        print(f"   [🎓 Plan de estudios detectado: {carrera_detectada}]")
        instrucciones_con_fecha += (
            "\n\nPLAN DE ESTUDIOS DE REFERENCIA (es material de contexto sobre la carrera del "
            "estudiante; NO lo evalúes ni lo menciones directamente, usalo solo para juzgar si el "
            f"perfil refleja competencias acordes a su carrera):\n{plan_de_estudios}"
        )
    elif carrera_detectada:
        print(f"   [ℹ️ Carrera detectada ('{carrera_detectada}') pero sin plan de estudios cargado todavía.]")
    else:
        print("   [ℹ️ No se pudo preclasificar la carrera por keywords; se analiza sin plan de estudios.]")

    prompt = f"{instrucciones_con_fecha}\n\nPERFIL DEL ESTUDIANTE:\n{texto_perfil}"

    # Los proveedores sin API key se saltean sin aviso: si no, cada perfil que pasa al respaldo
    # sumaría un ❌ "no configurada" al recuadro de avisos (por ejemplo, OpenAI antes de cargar
    # OPENAI_API_KEY en el .env.yaml de producción).
    cadena = [p for p in ORDEN_PROVEEDORES if _proveedor_configurado(p)]
    for indice_proveedor, proveedor in enumerate(cadena):
        print(f"   [🔎 Analizando con: {proveedor}]")
        resultado, uso, motivo_fallo = _intentar_proveedor_con_backoff(proveedor, prompt, avisar=avisar)
        if resultado:
            if indice_proveedor > 0:
                # Que quede visible en la web que este perfil NO lo analizó el proveedor principal.
                # ⚠️ (no ℹ️) para que salga en el recuadro de avisos del resumen: los proveedores
                # alternativos no puntúan igual que Gemini, conviene saber qué perfiles analizaron.
                avisar(f"   [⚠️ Este perfil se analizó con '{proveedor}' (el proveedor principal no respondió).]")
            # Se guarda junto con el análisis (y por lo tanto en la caché), para saber quién lo
            # analizó aunque en una corrida futura salga de la caché.
            resultado["proveedor_ia"] = proveedor
            registro.update(proveedor=proveedor, uso=uso)
            CACHE_ANALISIS[hash_perfil] = resultado
            guardar_cache(CACHE_ANALISIS)
            return resultado
        registro["fallos"].append({"proveedor": proveedor, "motivo": motivo_fallo})
        if indice_proveedor + 1 < len(cadena):
            avisar(f"   [↪️ Pasando a '{cadena[indice_proveedor + 1]}' tras fallo de {proveedor}...]")

    avisar("   ❌ Ningún proveedor de IA pudo analizar este perfil.")
    return None


# Mismos umbrales que "REGLAS DE PUNTUACIÓN Y SEMÁFORO" en INSTRUCCIONES_SISTEMA. No se arma el
# prompt con estas constantes a propósito: cambiar el texto del prompt invalida la caché.
PUNTAJE_MINIMO_VERDE = 75
PUNTAJE_MINIMO_AMARILLO = 50


def semaforo_para_puntaje(puntaje):
    if puntaje >= PUNTAJE_MINIMO_VERDE:
        return "Verde"
    if puntaje >= PUNTAJE_MINIMO_AMARILLO:
        return "Amarillo"
    return "Rojo"


def normalizar_puntaje_y_semaforo(resultado, avisar=print):
    """Deja el puntaje como entero de 0 a 100 y recalcula el semáforo a partir de él.

    El prompt le pide a la IA las dos cosas, pero nada garantiza que sean coherentes (un 72 con
    "Verde", o el puntaje como texto "72"), sobre todo con los proveedores de respaldo. El
    semáforo alimenta la planilla, los informes y las presentaciones, así que se deriva siempre del
    puntaje. Si el puntaje no es un número, se deja lo que dio la IA, con un aviso."""
    try:
        puntaje = round(float(str(resultado.get('puntaje_general')).replace(',', '.')))
    except (TypeError, ValueError):
        avisar(f"   [⚠️ La IA devolvió un puntaje que no es un número ({resultado.get('puntaje_general')!r}): "
               f"queda el semáforo que dio ('{resultado.get('color_semaforo')}').]")
        return resultado
    puntaje = max(0, min(100, puntaje))
    semaforo = semaforo_para_puntaje(puntaje)
    if resultado.get('color_semaforo') != semaforo:
        avisar(f"   [ℹ️ Semáforo corregido según el puntaje ({puntaje}): la IA dijo "
               f"'{resultado.get('color_semaforo')}', queda '{semaforo}'.]")
    resultado['puntaje_general'] = puntaje
    resultado['color_semaforo'] = semaforo
    return resultado

# ==============================================================================
# MÓDULO 3: GOOGLE SHEETS (Escritura de Matriz Actualizada)
# ==============================================================================

def construir_servicio_sheets(creds):
    """Arma el cliente de Sheets a partir de un objeto Credentials ya resuelto."""
    try:
        return build('sheets', 'v4', credentials=creds)
    except Exception as e:
        print(f"Error al construir el servicio de Sheets: {e}")
        return None

def construir_servicio_docs(creds):
    """Arma el cliente de Docs a partir de un objeto Credentials ya resuelto."""
    try:
        return build('docs', 'v1', credentials=creds)
    except Exception as e:
        print(f"Error al construir el servicio de Docs: {e}")
        return None

NOMBRE_HOJA_PLANTILLA = "Plantilla"
NOMBRE_HOJA_HISTORICO = "Histórico"

def _obtener_metadata_hojas(servicio_sheets, spreadsheet_id):
    """Devuelve la lista de propiedades (sheetId, title, index) de cada hoja del spreadsheet."""
    resultado = servicio_sheets.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields='sheets.properties'
    ).execute()
    return [hoja['properties'] for hoja in resultado.get('sheets', [])]


def obtener_o_crear_hoja_diaria(servicio_sheets, spreadsheet_id, fecha_iso):
    """
    Devuelve el nombre de la hoja del día (formato AAAA-MM-DD). Si todavía no existe,
    la crea duplicando la hoja 'Plantilla' (así hereda headers/formato) y la renombra.
    Si el pipeline ya corrió hoy, simplemente reutiliza la hoja existente.
    """
    hojas = _obtener_metadata_hojas(servicio_sheets, spreadsheet_id)
    titulos = {hoja['title']: hoja['sheetId'] for hoja in hojas}

    if fecha_iso in titulos:
        return fecha_iso

    if NOMBRE_HOJA_PLANTILLA not in titulos:
        raise RuntimeError(
            f"No se encontró la hoja '{NOMBRE_HOJA_PLANTILLA}'. "
            "Creála a mano con los headers correspondientes antes de correr el pipeline."
        )

    peticion = {
        'requests': [{
            'duplicateSheet': {
                'sourceSheetId': titulos[NOMBRE_HOJA_PLANTILLA],
                'insertSheetIndex': len(hojas),
                'newSheetName': fecha_iso
            }
        }]
    }
    servicio_sheets.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body=peticion
    ).execute()

    print(f"📄 Hoja nueva creada para hoy: '{fecha_iso}'")
    return fecha_iso


# --- Columnas de la Observación (0-indexadas) en cada hoja, con la columna URL nueva entre
# Nombre y Carrera. OJO: esto asume que ya agregaste la columna URL a mano en 'Plantilla' e
# 'Histórico', entre Nombre y Carrera, y corriste el "Volver a los datos sin formato" si hacía
# falta. Si el orden de columnas termina siendo otro, avisame para ajustar estos índices.
COLUMNA_OBSERVACION_DIARIA = 6      # G: Apellido, Nombre, URL, Carrera, Puntaje, Semáforo, Observación
COLUMNA_OBSERVACION_HISTORICO = 7   # H: Fecha, Apellido, Nombre, URL, Carrera, Puntaje, Semáforo, Observación

ETIQUETA_FUERTE = "Punto Fuerte: "
ETIQUETA_CRITICO = "Punto Crítico: "
ETIQUETA_ACCION = "Próxima Acción: "

def obtener_o_crear_hoja_historico(servicio_sheets, spreadsheet_id, año):
    """
    Devuelve el nombre de la hoja Histórico del año dado.

    'Histórico' dejó de ser una única hoja fija para siempre: ahora hay una por año
    ('Histórico 2026', 'Histórico 2027', ...), para que no crezca sin límite. Esta función:

      1. Si ya existe 'Histórico {año}', la reutiliza tal cual.
      2. Si no existe pero todavía queda la hoja vieja sin año ('Histórico', de antes de este
         cambio), la migra: la renombra in-place a 'Histórico {año}' — conserva todos los datos
         que ya tenía.
      3. Si no existe ninguna de las dos, busca la hoja 'Histórico {año}' más reciente que haya
         (típicamente la del año anterior), la duplica, renombra la copia y le borra todas las
         filas de datos (deja encabezado, columnas y formato intactos).
      4. Si no hay absolutamente ninguna hoja Histórico (instalación nueva desde cero), corta
         con un error pidiendo crear 'Histórico {año}' a mano una vez — mismo criterio que ya
         usa obtener_o_crear_hoja_diaria con 'Plantilla'.

    Las hojas 'Estadísticas' y 'Estadísticas del día' de la planilla arman el nombre
    'Histórico {año}' con INDIRECT a partir del año de su celda B1, así que pasan solas al año
    nuevo: si cambia este formato de nombre, hay que cambiar también esas fórmulas.
    """
    nombre_objetivo = f"{NOMBRE_HOJA_HISTORICO} {año}"
    hojas = _obtener_metadata_hojas(servicio_sheets, spreadsheet_id)
    titulos = {hoja['title']: hoja['sheetId'] for hoja in hojas}

    if nombre_objetivo in titulos:
        return nombre_objetivo

    # Migración: la hoja vieja sin año todavía existe con ese nombre plano.
    if NOMBRE_HOJA_HISTORICO in titulos:
        peticion = {
            'requests': [{
                'updateSheetProperties': {
                    'properties': {'sheetId': titulos[NOMBRE_HOJA_HISTORICO], 'title': nombre_objetivo},
                    'fields': 'title'
                }
            }]
        }
        servicio_sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=peticion).execute()
        print(f"   [ℹ️ Migración automática: '{NOMBRE_HOJA_HISTORICO}' pasa a llamarse '{nombre_objetivo}' "
              f"(conserva todos los datos que ya tenía).]")
        return nombre_objetivo

    # Buscamos la hoja "Histórico {año}" más reciente que exista, para usarla como base.
    patron_anio = re.compile(rf'^{re.escape(NOMBRE_HOJA_HISTORICO)} (\d{{4}})$')
    candidatas = [(int(m.group(1)), titulo) for titulo in titulos if (m := patron_anio.match(titulo))]

    if not candidatas:
        raise RuntimeError(
            f"No se encontró ninguna hoja '{nombre_objetivo}' ni '{NOMBRE_HOJA_HISTORICO}'. "
            f"Creá '{nombre_objetivo}' a mano con las 16 columnas correspondientes (de Fecha a Modo) antes de correr el pipeline."
        )

    candidatas.sort(key=lambda t: t[0], reverse=True)
    _, nombre_fuente = candidatas[0]

    peticion = {
        'requests': [{
            'duplicateSheet': {
                'sourceSheetId': titulos[nombre_fuente],
                'insertSheetIndex': len(hojas),
                'newSheetName': nombre_objetivo
            }
        }]
    }
    servicio_sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=peticion).execute()

    # La copia trae todas las filas de datos del año anterior — las borramos, dejando solo el
    # encabezado (fila 1) y el formato/columnas intactos.
    servicio_sheets.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id, range=f"'{nombre_objetivo}'!A2:P", body={}
    ).execute()

    print(f"   [📄 Hoja nueva creada para el año {año}: '{nombre_objetivo}' (copiada de '{nombre_fuente}', sin las filas de datos).]")
    return nombre_objetivo

def _construir_texto_observacion(resultado):
    """Arma el texto de la columna Observación a partir de los 3 campos separados de la IA."""
    fuerte = resultado.get('punto_fuerte') or 'Sin puntos destacados en este perfil.'
    critico = resultado.get('punto_critico') or 'Sin puntos críticos relevantes.'
    accion = resultado.get('proxima_accion') or 'Sin acciones prioritarias en este momento.'
    return f"{ETIQUETA_FUERTE}{fuerte}\n{ETIQUETA_CRITICO}{critico}\n{ETIQUETA_ACCION}{accion}"


def _construir_runs_negrita(texto):
    """Arma los textFormatRuns para poner en negrita las 3 etiquetas fijas dentro del texto."""
    idx_critico = texto.index(ETIQUETA_CRITICO)
    idx_accion = texto.index(ETIQUETA_ACCION)
    return [
        {"startIndex": 0, "format": {"bold": True}},
        {"startIndex": len(ETIQUETA_FUERTE), "format": {"bold": False}},
        {"startIndex": idx_critico, "format": {"bold": True}},
        {"startIndex": idx_critico + len(ETIQUETA_CRITICO), "format": {"bold": False}},
        {"startIndex": idx_accion, "format": {"bold": True}},
        {"startIndex": idx_accion + len(ETIQUETA_ACCION), "format": {"bold": False}},
    ]


def _construir_celda_url(url_perfil):
    """
    Celda con hipervínculo al perfil. OJO: usa ';' como separador de argumentos, que es el
    habitual en hojas con configuración regional en español. Si tu Sheet está en otra
    configuración regional y la fórmula aparece como error, avisame y cambio a ','.
    """
    if url_perfil:
        return f'=HYPERLINK("{url_perfil}";"Ver perfil")'
    return "Sin URL detectada"


def _parsear_fila_inicio(rango_actualizado):
    """Extrae el número de fila inicial de un string tipo "'2026-07-29'!A2:G6"."""
    coincidencia = re.search(r'![A-Za-z]+(\d+):', rango_actualizado)
    return int(coincidencia.group(1)) if coincidencia else None


def _obtener_sheet_id_por_nombre(servicio_sheets, spreadsheet_id, nombre_hoja):
    """Sin distinguir mayúsculas, igual que los rangos de la API de Sheets ('Hoja'!A1)."""
    hojas = _obtener_metadata_hojas(servicio_sheets, spreadsheet_id)
    for hoja in hojas:
        if hoja['title'].casefold() == nombre_hoja.casefold():
            return hoja['sheetId']
    return None


def _aplicar_negrita_observaciones(servicio_sheets, spreadsheet_id, sheet_id, fila_inicio,
                                     lista_resultados, indice_columna, avisar=print):
    """
    Segunda pasada: sobrescribe la columna de Observación con texto + textFormatRuns para que
    las etiquetas queden en negrita. values().append() no soporta formato por caracter, por eso
    hace falta este batchUpdate aparte, apuntando a las filas recién escritas.
    """
    filas_grid = []
    for resultado in lista_resultados:
        texto = _construir_texto_observacion(resultado)
        filas_grid.append({
            "values": [{
                "userEnteredValue": {"stringValue": texto},
                "textFormatRuns": _construir_runs_negrita(texto)
            }]
        })

    peticion = {
        'requests': [{
            'updateCells': {
                'rows': filas_grid,
                'fields': 'userEnteredValue,textFormatRuns',
                'range': {
                    'sheetId': sheet_id,
                    'startRowIndex': fila_inicio - 1,
                    'endRowIndex': fila_inicio - 1 + len(lista_resultados),
                    'startColumnIndex': indice_columna,
                    'endColumnIndex': indice_columna + 1
                }
            }
        }]
    }

    try:
        servicio_sheets.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body=peticion).execute()
    except Exception as e:
        avisar(f"   [⚠️ No se pudo aplicar negrita a las observaciones: {_resumir_error(e)}]")


def escribir_matriz_sheets(servicio_sheets, spreadsheet_id, lista_resultados, nombre_hoja, avisar=print):
    """
    Agrega las filas del día a la hoja diaria (14 columnas: Apellido, Nombre, URL, Carrera,
    Puntaje, Semáforo, Observación, Estado Titular, Estado Url, Estado Acerca de,
    Estado Experiencia, Estado Educación, Estado Certificaciones, Estado Aptitudes — mismas
    7 columnas de estado por categoría que ya se escriben en Histórico). Requiere que
    'Plantilla' ya tenga la columna URL agregada entre Nombre y Carrera, y las 7 columnas de
    Estado agregadas después de Observación (mismos headers que Histórico).
    """
    print(f"\nEscribiendo datos en la hoja '{nombre_hoja}'...")

    valores = []
    for resultado in lista_resultados:
        fila = [
            resultado.get('apellido_estudiante', ''),
            resultado.get('nombre_estudiante', 'Desconocido'),
            _construir_celda_url(resultado.get('url_perfil')),
            resultado.get('carrera_estudiante', 'No especificado'),
            resultado.get('puntaje_general', 0),
            resultado.get('color_semaforo', 'Error'),
            _construir_texto_observacion(resultado),
            _estado_categoria(resultado, 'titular'),
            _estado_categoria(resultado, 'url'),
            _estado_categoria(resultado, 'acerca_de'),
            _estado_categoria(resultado, 'experiencia_laboral'),
            _estado_categoria(resultado, 'educacion'),
            _estado_categoria(resultado, 'certificaciones'),
            _estado_categoria(resultado, 'aptitudes'),
        ]
        valores.append(fila)

    cuerpo = {'values': valores}
    # Rango con el nombre de hoja entre comillas simples: soporta nombres con espacios/tildes.
    rango = f"'{nombre_hoja}'!A2:N"   # antes era A2:G — ahora son 14 columnas, no 7

    try:
        resultado_append = servicio_sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=rango,
            valueInputOption='USER_ENTERED',
            body=cuerpo
        ).execute()

        filas_actualizadas = resultado_append.get('updates').get('updatedCells')
        avisar(f"✅ Se escribieron {len(valores)} filas en la hoja del día '{nombre_hoja}' ({filas_actualizadas} celdas).")

        fila_inicio = _parsear_fila_inicio(resultado_append['updates']['updatedRange'])
        sheet_id = _obtener_sheet_id_por_nombre(servicio_sheets, spreadsheet_id, nombre_hoja)
        if fila_inicio and sheet_id is not None:
            _aplicar_negrita_observaciones(
                servicio_sheets, spreadsheet_id, sheet_id, fila_inicio,
                lista_resultados, COLUMNA_OBSERVACION_DIARIA, avisar=avisar
            )
    except Exception as e:
        avisar(f"❌ No se pudo escribir en la hoja del día '{nombre_hoja}': {_resumir_error(e)}")

def _obtener_proxima_fila_libre(servicio_sheets, spreadsheet_id, nombre_hoja, columna_referencia="A"):
    """
    Cuenta cuántas filas tienen contenido en la columna de referencia (por defecto A, 'Fecha')
    y devuelve el número de la próxima fila libre. Se usa junto con values().update() en vez de
    values().append(), para que escribir en 'Histórico' funcione bien aunque la hoja esté
    armada como Tabla nativa de Sheets con muchas filas vacías de más (necesario para que ande
    la Tabla Dinámica de Carrera).
    """
    rango = f"'{nombre_hoja}'!{columna_referencia}:{columna_referencia}"
    resultado = servicio_sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=rango
    ).execute()
    valores = resultado.get('values', [])
    return len(valores) + 1

def escribir_historico_sheets(servicio_sheets, spreadsheet_id, lista_resultados, fecha_hoy, nombre_hoja_historico,
                              modo="", avisar=print):
    """Escribe las filas del día en la hoja Histórico del año correspondiente (16 columnas,
    Fecha en A, URL en D, Modo en P), calculando la próxima fila libre en vez de usar append()
    (ver _obtener_proxima_fila_libre).

    modo: "Cohorte" o "Mixto", según el tipo de corrida. Con Fecha, Carrera y Modo se arma cada
    presentación de estadísticas (ver generar_presentacion_stats.generar_presentacion_desde_historico).
    Va al final de la tabla a propósito: las hojas 'Estadísticas' y 'Estadísticas del día' de la
    planilla cuentan con fórmulas que apuntan a las columnas por letra, y agregarla en el medio las corría."""
    print(f"Escribiendo datos en '{nombre_hoja_historico}'...")

    valores = []
    for resultado in lista_resultados:
        fila = [
            fecha_hoy,
            resultado.get('apellido_estudiante', ''),
            resultado.get('nombre_estudiante', 'Desconocido'),
            _construir_celda_url(resultado.get('url_perfil')),
            resultado.get('carrera_estudiante', 'No especificado'),
            resultado.get('puntaje_general', 0),
            resultado.get('color_semaforo', 'Error'),
            _construir_texto_observacion(resultado),
            _estado_categoria(resultado, 'titular'),
            _estado_categoria(resultado, 'url'),
            _estado_categoria(resultado, 'acerca_de'),
            _estado_categoria(resultado, 'experiencia_laboral'),
            _estado_categoria(resultado, 'educacion'),
            _estado_categoria(resultado, 'certificaciones'),
            _estado_categoria(resultado, 'aptitudes'),
            modo,
        ]
        valores.append(fila)

    try:
        fila_inicio = _obtener_proxima_fila_libre(servicio_sheets, spreadsheet_id, nombre_hoja_historico)
        fila_fin = fila_inicio + len(valores) - 1
        rango = f"'{nombre_hoja_historico}'!A{fila_inicio}:P{fila_fin}"

        servicio_sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=rango,
            valueInputOption='USER_ENTERED',
            body={'values': valores}
        ).execute()

        avisar(f"✅ Se escribieron {len(valores)} filas en '{nombre_hoja_historico}' (desde la fila {fila_inicio}).")

        sheet_id = _obtener_sheet_id_por_nombre(servicio_sheets, spreadsheet_id, nombre_hoja_historico)
        if sheet_id is not None:
            _aplicar_negrita_observaciones(
                servicio_sheets, spreadsheet_id, sheet_id, fila_inicio,
                lista_resultados, COLUMNA_OBSERVACION_HISTORICO, avisar=avisar
            )
    except Exception as e:
        avisar(f"❌ No se pudo escribir en '{nombre_hoja_historico}': {_resumir_error(e)}")


# Subcarpetas de ID_CARPETA_PRESENTACIONES, igual que los informes (Cohorte / Mezclado), más una
# para las que se piden desde la web por rango de fechas.
SUBCARPETA_PRESENTACIONES_COHORTE = "Cohorte"
SUBCARPETA_PRESENTACIONES_MIXTO = "Mezclado"
SUBCARPETA_PRESENTACIONES_RANGO = "Por rango"


def generar_presentacion_de_la_corrida(creds, servicio_sheets, servicio_drive, fecha, carrera_cohorte=None,
                                       avisar=print):
    """
    Presentación de estadísticas al terminar una corrida, a partir del Histórico (que ya tiene las
    filas de esta corrida):
      - corrida de cohorte: "Estadísticas DP — Cohorte — <carrera> — <fecha>", solo con esa cohorte
        en ese día (si la misma cohorte se corrió en dos tandas el mismo día, junta las dos);
      - corrida mixta: "Estadísticas DP — Mixto — <fecha>", con todas las corridas mixtas del día.
    Si ya existe una con ese nombre, se manda a la papelera y se regenera.

    Devuelve el id de la presentación, o None si no se pudo (con un aviso): la presentación no
    tiene que frenar los informes.
    """
    if not ID_CARPETA_PRESENTACIONES:
        avisar("⚠️ No está configurada ID_CARPETA_PRESENTACIONES: no se generó la presentación de estadísticas.")
        return None
    modo = presentacion_stats.MODO_COHORTE if carrera_cohorte else presentacion_stats.MODO_MIXTO
    try:
        carpeta = obtener_o_crear_subcarpeta(
            servicio_drive, ID_CARPETA_PRESENTACIONES,
            SUBCARPETA_PRESENTACIONES_COHORTE if carrera_cohorte else SUBCARPETA_PRESENTACIONES_MIXTO,
        )
        resultado = presentacion_stats.generar_presentacion_desde_historico(
            creds, servicio_sheets, ID_SPREADSHEET, fecha, fecha, carrera=carrera_cohorte, modo=modo,
            carpeta_destino_id=carpeta, id_master=ID_PRESENTACION_MASTER_STATS, avisar=avisar,
        )
        if not resultado:
            return None
        id_presentacion, _, cantidad = resultado
        avisar(f"✅ Presentación de estadísticas generada: "
               f"{presentacion_stats.nombre_presentacion(fecha, fecha, carrera_cohorte, modo)} ({cantidad} perfiles).")
        return id_presentacion
    except Exception as e:
        avisar(f"❌ No se pudo generar la presentación de estadísticas: {_resumir_error(e)}")
        return None


def generar_presentacion_por_rango(creds, desde, hasta, carrera=None, modo=None, avisar=print):
    """
    Presentación de estadísticas de un rango de fechas, opcionalmente filtrada por carrera y/o modo
    ("Cohorte"/"Mixto"), a partir del Histórico. La usa la web ("Generar presentación"). Se guarda
    en ID_CARPETA_PRESENTACIONES/Por rango.

    Devuelve {"url", "nombre", "perfiles"}, o None si no hay perfiles para ese filtro (con un aviso).
    Lanza RuntimeError si falta configuración o falla la conexión con Google.
    """
    faltantes = [n for n, v in (("ID_SPREADSHEET", ID_SPREADSHEET), ("ID_CARPETA_PRESENTACIONES", ID_CARPETA_PRESENTACIONES),
                                ("ID_PRESENTACION_MASTER_STATS", ID_PRESENTACION_MASTER_STATS)) if not v]
    if faltantes:
        raise RuntimeError(f"Faltan variables de entorno: {', '.join(faltantes)}.")
    servicio_sheets = construir_servicio_sheets(creds)
    servicio_drive = construir_servicio_drive(creds)
    if not servicio_sheets or not servicio_drive:
        raise RuntimeError("No se pudo conectar con Google Sheets o Drive.")

    carpeta = obtener_o_crear_subcarpeta(servicio_drive, ID_CARPETA_PRESENTACIONES, SUBCARPETA_PRESENTACIONES_RANGO)
    resultado = presentacion_stats.generar_presentacion_desde_historico(
        creds, servicio_sheets, ID_SPREADSHEET, desde, hasta, carrera=carrera, modo=modo,
        carpeta_destino_id=carpeta, id_master=ID_PRESENTACION_MASTER_STATS, avisar=avisar,
    )
    if not resultado:
        return None
    _, url, cantidad = resultado
    return {"url": url, "nombre": presentacion_stats.nombre_presentacion(desde, hasta, carrera, modo), "perfiles": cantidad}

# ==============================================================================
# MÓDULO 4: GOOGLE DOCS (Generación de Informes a partir de Plantilla)
# ==============================================================================

def _estado_categoria(resultado, categoria):
    """Estado ('Aprobado'/'A Mejorar'/'No detectado') de una categoría puntual, para las
    columnas de detalle de 'Histórico' que alimentan las estadísticas."""
    return resultado.get('evaluacion_detallada', {}).get(categoria, {}).get('estado', 'No evaluado')

def construir_mapa_reemplazos(datos_alumno, fecha_hoy):
    """
    Construye el diccionario {placeholder: valor} a partir del JSON de la IA.
    Las claves deben coincidir EXACTAMENTE con los placeholders escritos en la
    plantilla de Google Docs (sin las llaves dobles, esas se agregan al buscar).
    """
    evaluacion = datos_alumno.get('evaluacion_detallada', {})

    def estado(seccion):
        return evaluacion.get(seccion, {}).get('estado', 'No evaluado')

    def comentario(seccion):
        return evaluacion.get(seccion, {}).get('comentario', '')

    return {
        "NOMBRE_ESTUDIANTE": datos_alumno.get('nombre_estudiante', 'Desconocido'),
        "APELLIDO_ESTUDIANTE": datos_alumno.get('apellido_estudiante', ''),
        "CARRERA_ESTUDIANTE": datos_alumno.get('carrera_estudiante', 'No especificado'),
        "URL_PERFIL": datos_alumno.get('url_perfil') or "No detectada",
        "FECHA_INFORME": fecha_hoy,
        "PUNTAJE_GENERAL": str(datos_alumno.get('puntaje_general', 0)),
        "COLOR_SEMAFORO": datos_alumno.get('color_semaforo', 'Desconocido'),
        "PUNTO_FUERTE": datos_alumno.get('punto_fuerte', 'Sin puntos destacados en este perfil.'),
        "PUNTO_CRITICO": datos_alumno.get('punto_critico', 'Sin puntos críticos relevantes.'),
        "PROXIMA_ACCION": datos_alumno.get('proxima_accion', 'Sin acciones prioritarias en este momento.'),

        "ESTADO_TITULAR": estado('titular'),
        "COMENTARIO_TITULAR": comentario('titular'),

        "ESTADO_UBICACION": estado('ubicacion'),
        "COMENTARIO_UBICACION": comentario('ubicacion'),

        "ESTADO_URL": estado('url'),
        "COMENTARIO_URL": comentario('url'),

        "ESTADO_ACERCA_DE": estado('acerca_de'),
        "COMENTARIO_ACERCA_DE": comentario('acerca_de'),

        "ESTADO_EXPERIENCIA_LABORAL": estado('experiencia_laboral'),
        "COMENTARIO_EXPERIENCIA_LABORAL": comentario('experiencia_laboral'),

        "ESTADO_EDUCACION": estado('educacion'),
        "COMENTARIO_EDUCACION": comentario('educacion'),

        "ESTADO_CERTIFICACIONES": estado('certificaciones'),
        "COMENTARIO_CERTIFICACIONES": comentario('certificaciones'),

        "ESTADO_APTITUDES": estado('aptitudes'),
        "COMENTARIO_APTITUDES": comentario('aptitudes'),

        "BONUS_RECOMENDACIONES": datos_alumno.get('analisis_bonus', {}).get('recomendaciones', ''),
        "BONUS_SECCIONES_ADICIONALES": datos_alumno.get('analisis_bonus', {}).get('secciones_adicionales', ''),
    
    }


def generar_documento_informe(servicio_drive, servicio_docs, id_plantilla, id_carpeta_destino, datos_alumno, fecha_hoy, avisar=print):
    """
    Genera el informe de un alumno copiando la plantilla de Google Docs, reemplazando los
    placeholders {{...}} por la información analizada por la IA, y exportando el resultado a
    PDF (conversión de Google, sin costo de IA). El PDF queda en la carpeta de la carrera, y el
    Doc editable se guarda en su subcarpeta 'Docs informes', por si hay que corregir algo a mano
    y volver a exportarlo.

    servicio_docs se recibe ya armado (construir_servicio_docs(creds), una sola vez por corrida
    del pipeline) en vez de autenticarse de nuevo por cada alumno.

    Devuelve el ID del archivo PDF final (o, si la exportación a PDF falla por algún motivo, el
    ID del Doc editable como respaldo — se prefiere dejar ALGO generado antes que perder el
    informe por completo).
    """
    apellido = datos_alumno.get('apellido_estudiante', '')
    nombre = datos_alumno.get('nombre_estudiante', 'Desconocido')
    nombre_documento = f"Informe_LinkedIn_{apellido}_{nombre}".strip()

    avisar(f"✍️  Generando documento de feedback para: {nombre} {apellido}...")

    try:
        # 1. Copiamos la plantilla directo a la carpeta de destino.
        metadata_copia = {
            'name': nombre_documento,
            'parents': [id_carpeta_destino]
        }
        copia = servicio_drive.files().copy(
            fileId=id_plantilla,
            body=metadata_copia,
            fields='id',
            supportsAllDrives=True
        ).execute()
        doc_id = copia.get('id')

        # 2. Armamos un request de tipo replaceAllText por cada placeholder
        mapa_reemplazos = construir_mapa_reemplazos(datos_alumno, fecha_hoy)

        pedidos = []
        for placeholder, valor in mapa_reemplazos.items():
            pedidos.append({
                'replaceAllText': {
                    'containsText': {
                        'text': f"{{{{{placeholder}}}}}",
                        'matchCase': True
                    },
                    'replaceText': str(valor) if valor is not None else ""
                }
            })

        # 4. Ejecutamos el batchUpdate, con reintentos por si la copia
        #    todavía no está lista para recibir ediciones justo después de crearse.
        intentos_batch = 3
        for intento in range(intentos_batch):
            resultado_batch = servicio_docs.documents().batchUpdate(
                documentId=doc_id,
                body={'requests': pedidos}
            ).execute()

            total_reemplazos = sum(
                r.get('replaceAllText', {}).get('occurrencesChanged', 0)
                for r in resultado_batch.get('replies', [])
            )

            if total_reemplazos > 0:
                break  # Reemplazó correctamente, no hace falta reintentar

            print(f"   [Aviso] No se detectaron reemplazos (intento {intento+1}/{intentos_batch}). Reintentando en 2s...")
            time.sleep(2)
        else:
            avisar(f"   ⚠️ El documento de {nombre} {apellido} se creó, pero no se pudo completar la información.")

        # 5. Exportamos el Doc ya completado a PDF, lo subimos a la misma carpeta, y guardamos
        #    el Doc editable aparte, en 'Docs informes'.
        id_pdf = _exportar_doc_a_pdf_y_archivar_doc(servicio_drive, doc_id, nombre_documento, id_carpeta_destino, avisar=avisar)
        if id_pdf:
            print(f"✅ Informe guardado en Drive como PDF (ID: {id_pdf}).")
            return id_pdf

        avisar(f"   [⚠️ No se pudo exportar a PDF; queda el Doc editable como respaldo (ID: {doc_id}).]")
        return doc_id

    except Exception as e:
        avisar(f"   ❌ No se pudo generar el informe de {nombre} {apellido}: {_resumir_error(e)}")
        return None


def _exportar_doc_a_pdf_y_archivar_doc(servicio_drive, doc_id, nombre_documento, id_carpeta_destino, avisar=print):
    """
    Exporta un Google Doc ya completado a PDF (conversión de Google, sin costo de IA), sube ese
    PDF a la misma carpeta, y mueve el Doc editable a la subcarpeta 'Docs informes', para que
    quede a mano si hay que corregir algo y volver a exportarlo.

    Las 3 etapas (exportar, subir, archivar el Doc) están separadas en try/except independientes
    a propósito: si falla exportar o subir, no hay PDF real todavía, así que se devuelve None y el
    Doc queda en la carpeta principal como respaldo. Pero si falla mover el Doc (después de que el
    PDF ya se subió con éxito), eso NO es una falla real del resultado — el PDF ya existe en
    Drive — así que se devuelve igual el ID del PDF, solo con un aviso de que el Doc quedó junto
    al PDF.

    Antes el Doc se intentaba BORRAR (3 intentos con espera) y solo se movía a 'Docs informes' si
    el borrado fallaba por falta de permiso: el resultado dependía de los permisos de quien
    corriera el pipeline, y cada informe perdía unos segundos en reintentos.

    Reintenta la descarga del export con backoff: el Doc se acaba de crear/editar (files().copy +
    batchUpdate) y a veces la API de Drive tarda unos segundos en "verlo" desde export_media — más
    notorio en Unidades Compartidas —, lo que devuelve un 404 "File not found" pasajero aunque el
    archivo exista.
    """
    intentos_maximos = 4
    for intento in range(intentos_maximos):
        try:
            # a. Descargamos el PDF exportado a memoria (mismo patrón que la descarga de
            #    perfiles en extraer_texto_drive_en_memoria, pero con export_media en vez de
            #    get_media).
            request = servicio_drive.files().export_media(fileId=doc_id, mimeType='application/pdf')
            archivo_memoria = io.BytesIO()
            downloader = MediaIoBaseDownload(archivo_memoria, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            archivo_memoria.seek(0)
            break  # export salió bien, seguimos afuera del loop de reintentos
        except Exception as e:
            if intento < intentos_maximos - 1:
                espera = 2 * (intento + 1)  # 2s, 4s, 6s...
                avisar(f"   [⏳ Export a PDF: '{e}'. Reintento {intento + 1}/{intentos_maximos - 1} en {espera}s...]")
                time.sleep(espera)
            else:
                avisar(f"   [⚠️ Falló la exportación a PDF tras {intentos_maximos} intentos: {e}]")
                return None

    try:
        # b. Subimos ese PDF como un archivo nuevo en la misma carpeta.
        metadata_pdf = {
            'name': f"{nombre_documento}.pdf",
            'parents': [id_carpeta_destino]
        }
        media = MediaIoBaseUpload(archivo_memoria, mimetype='application/pdf', resumable=False)
        pdf_creado = servicio_drive.files().create(
            body=metadata_pdf,
            media_body=media,
            fields='id',
            supportsAllDrives=True
        ).execute()
        id_pdf = pdf_creado.get('id')
    except Exception as e:
        # Acá sí es una falla real: no llegamos a tener el PDF. El Doc queda en la carpeta
        # principal como respaldo.
        avisar(f"   [⚠️ El PDF se exportó pero falló al subirlo a Drive: {e}]")
        return None

    # c. Recién ahora que el PDF quedó CONFIRMADO en Drive, movemos el Doc editable a
    # 'Docs informes', en un bloque SEPARADO a propósito: si falla, el PDF ya existe y es un
    # ÉXITO — no hay que devolver None (el caller pensaría que hay que usar el Doc como respaldo).
    detalle = ""
    try:
        carpeta_docs = obtener_o_crear_subcarpeta(servicio_drive, id_carpeta_destino, "Docs informes")
        movido = mover_archivo_a_carpeta(servicio_drive, doc_id, carpeta_docs, id_carpeta_destino)
    except Exception as e:
        movido = False
        detalle = f" ({_resumir_error(e)})"

    if movido:
        print(f"   [📦 Doc editable (ID: {doc_id}) guardado en 'Docs informes'.]")
    else:
        avisar(f"   [⚠️ No se pudo mover el Doc editable (ID: {doc_id}) a 'Docs informes': quedó junto al PDF{detalle}.]")

    return id_pdf


# ==============================================================================
# EJECUCIÓN DEL PIPELINE (Bucle Principal)
# ==============================================================================

def _preguntar_modo_analisis():
    """
     Devuelve el nombre de la carrera en modo cohorte, o None en modo mixto.
    """
    print("\n¿Qué perfiles vas a analizar?")
    print("  1. Una cohorte completa de una misma carrera")
    print("  2. Un conjunto de perfiles de distintas carreras")
    opcion = input("Elegí 1 o 2: ").strip()

    if opcion == "1":
        carreras_disponibles = list(CARRERAS_UDESA.keys())

        print("\nCarreras disponibles:")
        for indice, nombre in enumerate(carreras_disponibles, start=1):
            print(f"  {indice}. {nombre}")

        seleccion = input("Elegí el número de la carrera: ").strip()

        if not seleccion.isdigit() or not (1 <= int(seleccion) <= len(carreras_disponibles)):
            print("⚠️ Opción inválida, se analiza en modo mixto igual.")
            return None

        return carreras_disponibles[int(seleccion) - 1]

    return None

def ejecutar_pipeline(creds, callback_progreso=None, carrera_cohorte=None):
    """
    Corre el pipeline completo: extrae los PDFs pendientes de la carpeta de Drive, los analiza
    con la IA, escribe los resultados en Sheets y genera los informes individuales en Docs.

    Se separó del bloque `if __name__ == "__main__":` para que, además del script de consola de
    siempre (`python main.py`), también la pueda invocar el backend web (FastAPI) sin duplicar
    lógica.

    creds: objeto Credentials YA resuelto (ver MÓDULO DE AUTENTICACIÓN más arriba). El script de
    consola lo consigue con autenticar_google_cli(); el backend web lo consigue del login de la
    persona que está usando el panel (cada quien con su propia cuenta, ver web_app.py). Todo lo
    que este pipeline haga en Drive/Sheets/Docs/Slides queda hecho con la identidad de esas
    credenciales — no hay un usuario "del sistema" compartido.

    callback_progreso, si se pasa, es una función con la forma
    callback_progreso(mensaje: str, procesados: int, total: int) — pensada para que el backend
    web reporte avance en vivo (ej. "Analizando perfil 4 de 12...") sin tener que parsear la
    salida de consola. Si no se pasa, no hace nada distinto: el script de consola sigue
    funcionando exactamente igual que antes, basado en los print().

    carrera_cohorte: nombre canónico de una carrera de CARRERAS_UDESA, o None. Si se pasa, todo
    el lote se analiza como si fuera de esa carrera (modo "cohorte completa"), avisando por
    perfil si el texto no la menciona. Si es None, cada perfil usa su propia detección
    automática (modo "conjunto mixto"). El script de consola lo pide con _preguntar_modo_analisis()
    antes de llamar a esta función; el backend web lo pasa directo desde el selector de la web.

    Devuelve un diccionario resumen (útil para el dashboard web, ver construir_resumen_pipeline).

    Lanza RuntimeError si falla la construcción del servicio de Drive, para que el backend web
    pueda distinguir ese caso de "no había perfiles para analizar".
    """
    # Último avance conocido: los mensajes que no traen procesados/total (por ejemplo los de las
    # funciones de Sheets/Docs/Slides, que reciben avisar=_avisar) conservan el avance en vez de
    # mandar la barra de progreso de la web de vuelta a 0.
    progreso = {"procesados": 0, "total": 0}

    # Avisos y errores de la corrida, para listarlos aparte en el resumen final (en el log
    # completo se pierden entre los mensajes normales). Se detectan por la convención de emojis
    # que usan todos los mensajes de problema del pipeline: ⚠️ = aviso, ❌ = error.
    problemas = []

    def _avisar(mensaje, procesados=None, total=None, perfil=None):
        """perfil: nombre del PDF al que se refiere el mensaje, para los que no lo dicen solos
        (los de la IA, como "Este perfil se analizó con 'groq'"). Solo se agrega en el recuadro
        de avisos: en el log ya se ve, porque el "Procesando: <archivo>" aparece justo antes."""
        print(mensaje)
        if procesados is not None:
            progreso["procesados"] = procesados
        if total is not None:
            progreso["total"] = total
        if "⚠️" in mensaje or "❌" in mensaje:
            texto = mensaje.strip().strip("[]").strip()
            problemas.append(f"{texto} — {perfil}" if perfil else texto)
        if callback_progreso:
            callback_progreso(mensaje, progreso["procesados"], progreso["total"])

    _avisar("Iniciando Pipeline de Desarrollo Profesional UdeSA...")

    # Fecha calculada una única vez acá, y pasada como parámetro al resto del pipeline.
    hoy = datetime.now(ZONA_HORARIA)
    fecha_hoy = hoy.strftime("%d/%m/%Y")   # Formato humano: IA, informes, columna 'Fecha' del Histórico
    fecha_iso = hoy.strftime("%Y-%m-%d")   # Formato ISO: nombres de hoja y de subcarpetas

    # Validaciones tempranas: sin la carpeta de origen o sin la planilla no tiene sentido arrancar
    # (sin ID_SPREADSHEET, por ejemplo, se analizaban todos los perfiles con la IA y recién al
    # final fallaba la escritura).
    faltantes = [nombre for nombre, valor in (("ID_CARPETA", ID_CARPETA), ("ID_SPREADSHEET", ID_SPREADSHEET)) if not valor]
    if faltantes:
        raise RuntimeError(f"Faltan variables de entorno obligatorias: {', '.join(faltantes)} "
                           "(en el .env, o en el .env.yaml del deploy a Cloud Run).")
    if not ID_PLANTILLA_INFORME:
        _avisar("⚠️ No está configurada ID_PLANTILLA_INFORME: los informes individuales no se van a generar.")
    if not ID_CARPETA_ANALIZADOS:
        _avisar("⚠️ No está configurada ID_CARPETA_ANALIZADOS: los perfiles analizados no se van a mover.")

    servicio_drive = construir_servicio_drive(creds)
    if not servicio_drive:
        raise RuntimeError("No se pudo construir el servicio de Google Drive. Revisá las credenciales.")

    lista_pdfs = listar_pdfs_en_carpeta(servicio_drive, ID_CARPETA)
    total_pdfs = len(lista_pdfs)
    _avisar(f"Se encontraron {total_pdfs} perfiles para analizar.\n", 0, total_pdfs)

    # Subcarpeta de 'Analizados' del día (se crea una sola vez si no existe). Si
    # ID_CARPETA_ANALIZADOS no está configurada, los PDFs quedan en la carpeta de origen.
    carpeta_analizados_hoy = None
    if ID_CARPETA_ANALIZADOS:
        carpeta_analizados_hoy = obtener_o_crear_subcarpeta(
            servicio_drive, ID_CARPETA_ANALIZADOS, fecha_iso
        )

    resultados_finales = []  # Aquí guardaremos todos los JSONs
    # Qué IA analizó cada perfil, con cuántos tokens y qué proveedores fallaron (ver
    # analizar_perfil_con_ia), para el resumen de uso de IA. 'indice_resultado' marca la
    # posición del perfil en resultados_finales cuando el análisis salió bien.
    registros_ia = []

    for indice, archivo in enumerate(lista_pdfs, start=1):
        _avisar(f"Procesando: {archivo['name']}...", indice - 1, total_pdfs)

        # Paso A: Extraer texto y URL
        texto, url_perfil = extraer_texto_drive_en_memoria(servicio_drive, archivo['id'])

        # Paso B: Mandar a la IA
        if texto:
            registro = {"archivo": archivo['name']}
            registros_ia.append(registro)
            analisis_json = analizar_perfil_con_ia(
                texto, fecha_hoy, carrera_cohorte,
                avisar=lambda mensaje: _avisar(mensaje, perfil=archivo['name']),
                registro=registro
            )
            if analisis_json:
                # También para los que salen de la caché, que pueden ser de antes de este chequeo.
                normalizar_puntaje_y_semaforo(analisis_json, avisar=lambda mensaje: _avisar(mensaje, perfil=archivo['name']))
                analisis_json['url_perfil'] = url_perfil
                if carrera_cohorte:
                    # Aseguramos que el campo que arma la IA coincida textualmente con la
                    # carrera elegida (se usa para las carpetas por carrera y para Histórico).
                    analisis_json['carrera_estudiante'] = carrera_cohorte
                registro["indice_resultado"] = len(resultados_finales)
                resultados_finales.append(analisis_json)
                _avisar(
                    f"✅ Análisis completado para: {analisis_json.get('nombre_estudiante', 'Desconocido')}",
                    indice, total_pdfs
                )

                # Paso C: Mover el PDF ya analizado, para no volver a listarlo mañana.
                # Si falla el análisis (los 3 proveedores caen), el PDF se queda en la
                # carpeta original a propósito, para reintentarlo en la próxima corrida.
                if carpeta_analizados_hoy:
                    movido = mover_archivo_a_carpeta(
                        servicio_drive, archivo['id'], carpeta_analizados_hoy, ID_CARPETA
                    )
                    if movido:
                        print(f"   📦 Movido a 'Analizados/{fecha_iso}'.")
            else:
                _avisar(f"⚠️ No se pudo analizar '{archivo['name']}' (fallaron todos los proveedores de IA, "
                        "revisá el detalle de arriba). Queda en la carpeta original para reintentar.",
                        indice, total_pdfs)
        else:
            _avisar(f"⚠️ No se pudo leer el texto de '{archivo['name']}' (¿PDF vacío, escaneado o sin permiso de lectura?). "
                    "Se omite.", indice, total_pdfs)

        print("-" * 40)

    # PASO 3: Escribir en la hoja diaria y en el Histórico
    servicio_sheets = construir_servicio_sheets(creds) if resultados_finales else None
    id_presentacion_generada = None
    if resultados_finales and not servicio_sheets:
        _avisar("❌ No se pudo conectar con Google Sheets: los resultados de esta corrida NO se escribieron "
                "en la planilla (los análisis quedaron en caché, se pueden volver a correr sin gastar IA).")
    if servicio_sheets:
        nombre_hoja_hoy = obtener_o_crear_hoja_diaria(servicio_sheets, ID_SPREADSHEET, fecha_iso)
        escribir_matriz_sheets(servicio_sheets, ID_SPREADSHEET, resultados_finales, nombre_hoja_hoy, avisar=_avisar)

        nombre_hoja_historico = obtener_o_crear_hoja_historico(servicio_sheets, ID_SPREADSHEET, hoy.year)
        modo_corrida = presentacion_stats.MODO_COHORTE if carrera_cohorte else presentacion_stats.MODO_MIXTO
        escribir_historico_sheets(servicio_sheets, ID_SPREADSHEET, resultados_finales, fecha_hoy,
                                  nombre_hoja_historico, modo=modo_corrida, avisar=_avisar)

        if ID_PRESENTACION_MASTER_STATS:
            id_presentacion_generada = generar_presentacion_de_la_corrida(
                creds, servicio_sheets, servicio_drive, hoy.date(), carrera_cohorte, avisar=_avisar
            )

    # PASO 4: Generar los Google Docs individuales, en la subcarpeta de informes del día
    servicio_docs = construir_servicio_docs(creds) if ID_PLANTILLA_INFORME and resultados_finales else None
    if ID_PLANTILLA_INFORME and resultados_finales and not servicio_docs:
        _avisar("❌ No se pudo conectar con Google Docs: no se generaron los informes individuales.")
    if servicio_docs:
        _avisar("\nIniciando fase de creación de reportes individuales...")

        # Informes generados / Mezclado|Cohorte / fecha / carrera / archivo — separado en dos
        # ramas según el modo de la corrida, para que una cohorte completa de una carrera no se
        # mezcle en la misma carpeta con corridas de conjunto mixto que puedan tocar esa misma
        # fecha y carrera por coincidencia.
        nombre_rama_modo = "Cohorte" if carrera_cohorte else "Mezclado"
        carpeta_modo = obtener_o_crear_subcarpeta(
            servicio_drive, ID_CARPETA_INFORMES, nombre_rama_modo
        )
        carpeta_informes_hoy = obtener_o_crear_subcarpeta(
            servicio_drive, carpeta_modo, fecha_iso
        )
        carpetas_carrera_hoy = {}  # cache: nombre de carpeta -> id, para no repetir la búsqueda
        for resultado in resultados_finales:
            nombre_carpeta = _nombre_carpeta_carrera(resultado.get('carrera_estudiante'))
            if nombre_carpeta not in carpetas_carrera_hoy:
                carpetas_carrera_hoy[nombre_carpeta] = obtener_o_crear_subcarpeta(
                    servicio_drive, carpeta_informes_hoy, nombre_carpeta
                )
            generar_documento_informe(
                servicio_drive,
                servicio_docs,
                ID_PLANTILLA_INFORME,
                carpetas_carrera_hoy[nombre_carpeta],
                resultado,
                fecha_hoy,
                avisar=_avisar
            )

    uso_ia = _resumir_uso_ia(registros_ia)
    print(f"\n🤖 Uso de IA: {_texto_uso_ia(uso_ia)}")

    if problemas:
        _avisar(f"🎉 PIPELINE FINALIZADO, con {len(problemas)} aviso(s) o error(es): revisalos en el resumen.",
                total_pdfs, total_pdfs)
    else:
        _avisar("🎉 PIPELINE FINALIZADO.", total_pdfs, total_pdfs)

    return construir_resumen_pipeline(fecha_hoy, total_pdfs, resultados_finales, fecha_iso,
                                      id_presentacion_generada, problemas, registros_ia)


def _resumir_uso_ia(registros_ia):
    """
    Resumen de uso de IA de una corrida, a partir de los registros por perfil que completa
    analizar_perfil_con_ia: cuántos perfiles analizó cada proveedor y con cuántos tokens, el
    promedio de tokens por perfil (solo de los que llamaron a una IA: los que salen de la caché no
    gastan tokens), y en cuántos perfiles falló cada proveedor, separando "cuota" (límite de
    cuota o saturación) de "error" (cualquier otro).
    """
    por_proveedor = {}
    tokens_por_perfil = []
    fallos_por_proveedor = {}

    for proveedor in ORDEN_PROVEEDORES + ["caché"]:  # en este orden, para mostrarlos igual siempre
        registros = [r for r in registros_ia if r.get("proveedor") == proveedor]
        if not registros:
            continue
        tokens = [r["uso"]["total"] for r in registros if r.get("uso")]
        por_proveedor[proveedor] = {
            "perfiles": len(registros),
            "tokens": sum(tokens),
            "promedio_tokens": round(sum(tokens) / len(tokens)) if tokens else None,
        }
        tokens_por_perfil.extend(tokens)

    for registro in registros_ia:
        for fallo in registro.get("fallos", []):
            conteo = fallos_por_proveedor.setdefault(fallo["proveedor"], {"cuota": 0, "error": 0})
            conteo[fallo["motivo"]] += 1

    return {
        "por_proveedor": por_proveedor,
        "promedio_tokens_por_perfil": round(sum(tokens_por_perfil) / len(tokens_por_perfil)) if tokens_por_perfil else None,
        "perfiles_con_tokens": len(tokens_por_perfil),
        "tokens_totales": sum(tokens_por_perfil),
        "fallos_por_proveedor": fallos_por_proveedor,
        "sin_analizar": sum(1 for r in registros_ia if not r.get("proveedor")),
    }


def _texto_uso_ia(uso_ia):
    """Una línea con el resumen de uso de IA, para la consola."""
    partes = [f"{proveedor} {datos['perfiles']}" for proveedor, datos in uso_ia["por_proveedor"].items()]
    texto = ", ".join(partes) or "ningún perfil analizado"
    if uso_ia["promedio_tokens_por_perfil"] is not None:
        texto += f" | promedio {uso_ia['promedio_tokens_por_perfil']} tokens por perfil"
    for proveedor, conteo in uso_ia["fallos_por_proveedor"].items():
        if conteo["cuota"]:
            texto += f" | {proveedor} sin cuota/saturado en {conteo['cuota']} perfil(es)"
        if conteo["error"]:
            texto += f" | {proveedor} con error en {conteo['error']} perfil(es)"
    return texto


def construir_resumen_pipeline(fecha_hoy, total_pdfs, resultados_finales, fecha_iso, id_presentacion_generada=None,
                               problemas=None, registros_ia=None):
    """
    Arma el diccionario resumen final del pipeline: conteos por semáforo, por carrera, alumnos
    fallidos/exitosos y links directos a Sheets/Slides, pensado para mostrarse tal cual en el
    dashboard web al terminar una corrida.

    id_presentacion_generada: ID de la presentación de estadísticas de esta corrida (la de su
    cohorte o la de los mixtos del día, ver generar_presentacion_de_la_corrida), si se pudo crear.
    Si es None, no hay link (si fue por un error, el aviso ya está en el resumen).

    problemas: lista de mensajes de aviso/error de la corrida (ver _avisar en ejecutar_pipeline),
    para que la web los muestre aparte en el resumen en vez de dejarlos perdidos en el log.

    registros_ia: qué IA analizó cada perfil y con cuántos tokens (ver analizar_perfil_con_ia).
    Se usa para la columna "IA" de cada alumno y para el resumen de uso de IA (_resumir_uso_ia).
    """
    registros_ia = registros_ia or []
    registro_por_indice = {r["indice_resultado"]: r for r in registros_ia if "indice_resultado" in r}

    conteo_semaforo = {"Verde": 0, "Amarillo": 0, "Rojo": 0}
    conteo_por_carrera = {}
    alumnos = []

    for indice, resultado in enumerate(resultados_finales):
        semaforo = resultado.get('color_semaforo', 'Error')
        if semaforo in conteo_semaforo:
            conteo_semaforo[semaforo] += 1

        carrera = resultado.get('carrera_estudiante') or 'Sin carrera detectada'
        conteo_por_carrera[carrera] = conteo_por_carrera.get(carrera, 0) + 1

        registro = registro_por_indice.get(indice, {})
        alumnos.append({
            "nombre": f"{resultado.get('nombre_estudiante', 'Desconocido')} {resultado.get('apellido_estudiante', '')}".strip(),
            "carrera": carrera,
            "puntaje": resultado.get('puntaje_general', 0),
            "semaforo": semaforo,
            "url_perfil": resultado.get('url_perfil'),
            "ia": registro.get("proveedor"),
            "ia_original": registro.get("proveedor_original"),
            "tokens": (registro.get("uso") or {}).get("total"),
        })

    links = {}
    if ID_SPREADSHEET:
        links["planilla"] = f"https://docs.google.com/spreadsheets/d/{ID_SPREADSHEET}/edit"
    if ID_CARPETA_INFORMES:
        links["carpeta_informes"] = f"https://drive.google.com/drive/folders/{ID_CARPETA_INFORMES}"
    if id_presentacion_generada:
        links["presentacion_estadisticas"] = f"https://docs.google.com/presentation/d/{id_presentacion_generada}/edit"

    return {
        "fecha": fecha_hoy,
        "fecha_iso": fecha_iso,
        "total_pdfs": total_pdfs,
        "analizados": len(resultados_finales),
        "fallidos": total_pdfs - len(resultados_finales),
        "conteo_semaforo": conteo_semaforo,
        "conteo_por_carrera": conteo_por_carrera,
        "alumnos": alumnos,
        "links": links,
        "avisos": list(problemas or []),
        "uso_ia": _resumir_uso_ia(registros_ia),
        "resultados": resultados_finales,
    }


if __name__ == "__main__":
    creds_cli = autenticar_google_cli()
    carrera_cohorte = _preguntar_modo_analisis()
    ejecutar_pipeline(creds_cli, carrera_cohorte=carrera_cohorte)