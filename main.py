import io
import json
import os
import pdfplumber
import re
import time
import hashlib
import unicodedata

from datetime import date

from google import genai
from google.genai import types
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from dotenv import load_dotenv
from openai import OpenAI
from groq import Groq

# Activacion del venv antes de ejecutar: venv\Scripts\activate
# ejecucion del codigo: python main.py

# Cargar las variables de entorno desde el archivo .env
load_dotenv()

# ==============================================================================
# CONFIGURACIÓN GENERAL
# ==============================================================================

# 1. Credenciales de Drive
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets',  # Permiso para editar el spread sheet
    'https://www.googleapis.com/auth/documents',       # Permiso para inyectar texto en Docs
    'https://www.googleapis.com/auth/presentations'   # Permiso para inyectar en las presentaciones
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
ID_PRESENTACION_STATS = os.getenv('ID_PRESENTACION_STATS')

client = genai.Client(api_key=GEMINI_API_KEY)

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
- "consistencia_idioma": revisá la sección "Idiomas" del perfil. SOLO si declara un nivel de
  Inglés de "Competencia profesional completa" o "Competencia bilingüe o nativa", aplicá este
  criterio (si no declara ese nivel, escribí "No aplica: no se declara un nivel avanzado de
  inglés."): si el perfil (tal como está en este PDF) está mayormente en español, recomendá
  armar también una versión del perfil en inglés (aclarando que esto es una recomendación basada
  en lo que muestra este PDF puntual, ya que no se puede verificar si el estudiante ya tiene una
  versión en inglés configurada aparte). Si el perfil ya está mayormente en inglés, en cambio,
  revisá que TODAS las secciones estén en ese idioma de forma consistente, y marcá si encontrás
  alguna sección mezclada en español.

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
    "acerca_de": "acerca_de": {"estado": "Aprobado | A Mejorar | No detectado", "comentario": "..."},
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
# MÓDULO DE AUTENTICACIÓN UNIFICADO (OAuth 2.0 - Usuario Real)
# ==============================================================================
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials


def autenticar_google():
    """Autentica al usuario mediante OAuth 2.0 y maneja el archivo token.json."""
    creds = None

    # El archivo token.json almacena las credenciales de acceso del usuario.
    # Se crea automáticamente la primera vez que se completa el flujo de inicio de sesión.
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    # Si no hay credenciales válidas (o expiraron), dejamos que el usuario inicie sesión.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # Si falla la renovación por seguridad, borramos el token viejo para forzar login
                os.remove('token.json')
                creds = None

        if not creds:
            # Buscamos el archivo que descargaste de la consola de Google Cloud
            flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
            creds = flow.run_local_server(port=0)

        # Guardamos las credenciales en tu PC para la próxima vez
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    return creds


def autenticar_drive():
    """Conecta con Google Drive usando tus credenciales de usuario."""
    try:
        creds = autenticar_google()
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"Error de autenticación en Drive: {e}")
        return None


# ==============================================================================
# MÓDULO 1: GOOGLE DRIVE (Extracción - Continuación)
# ==============================================================================

def listar_pdfs_en_carpeta(servicio, folder_id):
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
        print(f"Error al listar archivos: {e}")
        return archivos


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
        print(f"   [⚠️ No se pudo mover el archivo a 'Analizados': {e}]")
        return False


# ==============================================================================
# CONFIGURACIÓN MULTI-PROVEEDOR
# ==============================================================================


# Orden de la cadena de fallback. Gemini primero (mejor calidad de análisis),
# después los proveedores gratuitos alternativos.
ORDEN_PROVEEDORES = ["gemini", "groq", "nvidia"]

cliente_groq = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

cliente_nvidia = OpenAI(
    api_key=NVIDIA_API_KEY,
    base_url="https://integrate.api.nvidia.com/v1"
) if NVIDIA_API_KEY else None

# ==============================================================================
# CACHÉ POR HASH DE CONTENIDO (evita re-analizar el mismo perfil dos veces)
# ==============================================================================

ARCHIVO_CACHE = "cache_analisis.json"


def cargar_cache():
    if os.path.exists(ARCHIVO_CACHE):
        try:
            with open(ARCHIVO_CACHE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def guardar_cache(cache):
    try:
        with open(ARCHIVO_CACHE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
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
# del PDF (sin usar IA) + nombre del archivo con su plan de estudios. Sumá una entrada nueva acá
# por cada carrera para la que cargues un plan; si una carrera no está en este diccionario, el
# perfil se analiza igual, pero sin el contexto extra del plan de estudios.
CARRERAS_UDESA = {
    "Ingeniería en Inteligencia Artificial": {
        "palabras_clave": ["ingeniería en inteligencia artificial", "ingeniería en ia"],
        "archivo_plan": "ingenieria_en_inteligencia_artificial.txt",
    },
    "Licenciatura en Negocios Digitales": {
        "palabras_clave": ["negocios digitales"],
        "archivo_plan": "negocios_digitales.txt",
    },
    "Licenciatura en Ciencias del Comportamiento": {
        "palabras_clave": ["ciencias del comportamiento"],
        "archivo_plan": "ciencias_del_comportamiento.txt",
    },
    "Licenciatura en Economía": {
        "palabras_clave": ["licenciatura en economia", "licenciatura en economía"],
        "archivo_plan": "economia.txt",
    },
    "Ingeniería en Biotecnología": {
        "palabras_clave": ["ingeniería en biotecnología", "ingenieria en biotecnologia"],
        "archivo_plan": "ingenieria_en_biotecnologia.txt",
    },
    "Abogacía": {
        "palabras_clave": ["abogacía", "abogacia", "estudiante de abogacía"],
        "archivo_plan": "abogacia.txt",
    },
    "Licenciatura en Administración de Empresas": {
        "palabras_clave": ["licenciatura en administración de empresas", "licenciatura en administracion de empresas"],
        "archivo_plan": "administracion.txt",
    },
    "Licenciatura en Ciencias de la Educación": {
        "palabras_clave": ["ciencias de la educación", "ciencias de la educacion"],
        "archivo_plan": "ciencias_educacion.txt",
    },
    "Licenciatura en Ciencia Política y Gobierno": {
        "palabras_clave": ["ciencia política", "ciencias políticas", "ciencia politica"],
        "archivo_plan": "ciencias_politicas.txt",
    },
    "Licenciatura en Comunicación": {
        "palabras_clave": ["licenciatura en comunicación", "licenciatura en comunicacion"],
        "archivo_plan": "comunicacion.txt",
    },
    "Licenciatura en Diseño": {
        "palabras_clave": ["licenciatura en diseño", "licenciatura en diseno"],
        "archivo_plan": "diseno.txt",
    },
    "Licenciatura en Economía Empresarial": {
        "palabras_clave": ["economía empresarial", "economia empresarial"],
        "archivo_plan": "economia_empresarial.txt",
    },
    "Licenciatura en Finanzas": {
        "palabras_clave": ["licenciatura en finanzas"],
        "archivo_plan": "finanzas.txt",
    },
    "Licenciatura en Humanidades": {
        "palabras_clave": ["licenciatura en humanidades"],
        "archivo_plan": "humanidades.txt",
    },
    "Ingeniería Industrial": {
        "palabras_clave": ["ingeniería industrial", "ingenieria industrial"],
        "archivo_plan": "ingenieria_industrial.txt",
    },
    "Ingeniería en Sustentabilidad": {
        "palabras_clave": ["ingeniería en sustentabilidad", "ingenieria en sustentabilidad"],
        "archivo_plan": "ingenieria_sustentabilidad.txt",
    },
    "Profesorado en Educación Primaria": {
        "palabras_clave": ["profesorado en educación primaria", "profesorado de educación primaria"],
        "archivo_plan": "profesorado_educacion_primaria.txt",
    },
    "Licenciatura en Relaciones Internacionales": {
        "palabras_clave": ["relaciones internacionales"],
        "archivo_plan": "relaciones_internacionales.txt",
    },
    # TODO: agregar acá el resto de las carreras de UdeSA que se desee cubrir
}


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
    for nombre_carrera, datos in CARRERAS_UDESA.items():
        for palabra_clave in datos["palabras_clave"]:
            if _normalizar_texto(palabra_clave) in texto_normalizado:
                return nombre_carrera
    return None


def cargar_plan_de_estudios(nombre_carrera):
    """Lee el archivo .txt del plan de estudios de la carrera detectada, si existe."""
    if not nombre_carrera or nombre_carrera not in CARRERAS_UDESA:
        return None

    ruta = os.path.join(CARPETA_PLANES_DE_ESTUDIO, CARRERAS_UDESA[nombre_carrera]["archivo_plan"])
    if not os.path.exists(ruta):
        return None

    try:
        with open(ruta, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"   [⚠️ No se pudo leer el plan de estudios de '{nombre_carrera}': {e}]")
        return None


# ==============================================================================
# MÓDULO 2: MULTI-PROVEEDOR IA (Gemini → Groq → NVIDIA con backoff y caché)
# ==============================================================================

def _es_error_de_limite(error_msg):
    error_msg = error_msg.lower()
    return "429" in error_msg or "quota" in error_msg or "rate" in error_msg or "503" in error_msg or "demand" in error_msg


def _llamar_gemini(prompt):
    respuesta = client.models.generate_content(
        model='gemini-flash-latest',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.2
        )
    )
    return respuesta.text


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
    return respuesta.choices[0].message.content


def _llamar_nvidia(prompt):
    if not cliente_nvidia:
        raise RuntimeError("NVIDIA_API_KEY no configurada.")
    respuesta = cliente_nvidia.chat.completions.create(
        model="deepseek-ai/deepseek-v4-flash-0731",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        top_p=0.7,
        max_tokens=4096,
        stream=False
    )
    texto = respuesta.choices[0].message.content
    # NVIDIA no siempre soporta json mode estricto según el modelo; limpiamos
    # posibles fences de markdown por si el modelo los agrega igual.
    return texto.replace("```json", "").replace("```", "").strip()


ADAPTADORES = {
    "gemini": _llamar_gemini,
    "groq": _llamar_groq,
    "nvidia": _llamar_nvidia,
}


def _intentar_proveedor_con_backoff(nombre_proveedor, prompt, intentos_maximos=3):
    """Reintenta un proveedor con backoff exponencial. Devuelve JSON parseado o None."""
    funcion = ADAPTADORES[nombre_proveedor]

    for intento in range(intentos_maximos):
        try:
            texto_crudo = funcion(prompt)
            return json.loads(texto_crudo)
        except Exception as e:
            error_msg = str(e)

            if _es_error_de_limite(error_msg):
                espera = 2 ** (intento + 1)  # 2s, 4s, 8s...
                print(f"   [⏳ {nombre_proveedor}: límite alcanzado. Reintento {intento+1}/{intentos_maximos} en {espera}s...]")
                time.sleep(espera)
            else:
                print(f"   [❌ {nombre_proveedor}: error irrecuperable → {e}]")
                return None

    print(f"   [❌ {nombre_proveedor}: se agotaron los reintentos.]")
    return None


def analizar_perfil_con_ia(texto_perfil, fecha_hoy, carrera_cohorte=None):
    """Orquestador: preclasificación de carrera → caché → Gemini → Groq → NVIDIA.

    carrera_cohorte: si se pasa (modo "cohorte completa"), pisa la detección automática por
    keywords para TODO el lote, avisando por consola si un perfil puntual no la menciona (no
    se descarta el perfil, solo se informa la inconsistencia)."""

    carrera_detectada = detectar_carrera_por_keywords(texto_perfil)

    if carrera_cohorte:
        if carrera_detectada and carrera_detectada != carrera_cohorte:
            print(f"   [⚠️ Este perfil menciona '{carrera_detectada}', pero se analiza como "
                  f"'{carrera_cohorte}' (modo cohorte).]")
        elif not carrera_detectada:
            print(f"   [⚠️ Este perfil no menciona ninguna carrera reconocida; se analiza "
                  f"igual como '{carrera_cohorte}' (modo cohorte).]")
        carrera_detectada = carrera_cohorte

    plan_de_estudios = cargar_plan_de_estudios(carrera_detectada) if carrera_detectada else None

    hash_perfil = calcular_hash_perfil(texto_perfil, plan_de_estudios)
    if hash_perfil in CACHE_ANALISIS:
        print("   [💾 Resultado obtenido de caché, no se llamó a ninguna IA.]")
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

    for proveedor in ORDEN_PROVEEDORES:
        print(f"   [🔎 Analizando con: {proveedor}]")
        resultado = _intentar_proveedor_con_backoff(proveedor, prompt)
        if resultado:
            CACHE_ANALISIS[hash_perfil] = resultado
            guardar_cache(CACHE_ANALISIS)
            return resultado
        print(f"   [↪️ Pasando al siguiente proveedor tras fallo de {proveedor}...]")

    print("   ❌ Ningún proveedor pudo analizar este perfil.")
    return None

# ==============================================================================
# MÓDULO 3: GOOGLE SHEETS (Escritura de Matriz Actualizada)
# ==============================================================================

def autenticar_sheets():
    """Conecta con Google Sheets usando tus credenciales de usuario."""
    try:
        creds = autenticar_google()
        return build('sheets', 'v4', credentials=creds)
    except Exception as e:
        print(f"Error de autenticación en Sheets: {e}")
        return None

def autenticar_slides():
    """Conecta con Google Slides usando tus credenciales de usuario."""
    try:
        creds = autenticar_google()
        return build('slides', 'v1', credentials=creds)
    except Exception as e:
        print(f"Error de autenticación en Slides: {e}")
        return None

NOMBRE_HOJA_PLANTILLA = "Plantilla"
NOMBRE_HOJA_HISTORICO = "Histórico"
NOMBRE_HOJA_ESTADISTICAS_DIA = "Estadísticas del Día" 

OBJECT_IDS_GRAFICOS_STATS = [
    "g3f4088a6156_1_0",
    "g3f4088a6156_1_1",
    "g3f4088a6156_1_2",
    "g3f4088a6156_1_3",
    "g3f4088a6156_1_4",
    "g3f4088a6156_1_5",
    "g3f4088a6156_1_6",
    "g3f4088a6156_1_7",
]

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
    hojas = _obtener_metadata_hojas(servicio_sheets, spreadsheet_id)
    for hoja in hojas:
        if hoja['title'] == nombre_hoja:
            return hoja['sheetId']
    return None


def _aplicar_negrita_observaciones(servicio_sheets, spreadsheet_id, sheet_id, fila_inicio,
                                     lista_resultados, indice_columna):
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
        print(f"   [⚠️ No se pudo aplicar negrita a las observaciones: {e}]")


def escribir_matriz_sheets(servicio_sheets, spreadsheet_id, lista_resultados, nombre_hoja):
    """
    Agrega las filas del día a la hoja diaria (7 columnas: Apellido, Nombre, URL, Carrera,
    Puntaje, Semáforo, Observación). Requiere que 'Plantilla' ya tenga la columna URL agregada
    entre Nombre y Carrera.
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
            _construir_texto_observacion(resultado)
        ]
        valores.append(fila)

    cuerpo = {'values': valores}
    # Rango con el nombre de hoja entre comillas simples: soporta nombres con espacios/tildes.
    rango = f"'{nombre_hoja}'!A2:G"

    try:
        resultado_append = servicio_sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=rango,
            valueInputOption='USER_ENTERED',
            body=cuerpo
        ).execute()

        filas_actualizadas = resultado_append.get('updates').get('updatedCells')
        print(f"✅ ¡Éxito! Se actualizaron {filas_actualizadas} celdas en '{nombre_hoja}'.")

        fila_inicio = _parsear_fila_inicio(resultado_append['updates']['updatedRange'])
        sheet_id = _obtener_sheet_id_por_nombre(servicio_sheets, spreadsheet_id, nombre_hoja)
        if fila_inicio and sheet_id is not None:
            _aplicar_negrita_observaciones(
                servicio_sheets, spreadsheet_id, sheet_id, fila_inicio,
                lista_resultados, COLUMNA_OBSERVACION_DIARIA
            )
    except Exception as e:
        print(f"Error al escribir en Sheets: {e}")

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

def escribir_historico_sheets(servicio_sheets, spreadsheet_id, lista_resultados, fecha_hoy):
    """Escribe las filas del día en la hoja fija 'Histórico' (15 columnas, Fecha en A, URL en D),
    calculando la próxima fila libre en vez de usar append() (ver _obtener_proxima_fila_libre)."""
    print(f"Escribiendo datos en '{NOMBRE_HOJA_HISTORICO}'...")

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
        ]
        valores.append(fila)

    try:
        fila_inicio = _obtener_proxima_fila_libre(servicio_sheets, spreadsheet_id, NOMBRE_HOJA_HISTORICO)
        fila_fin = fila_inicio + len(valores) - 1
        rango = f"'{NOMBRE_HOJA_HISTORICO}'!A{fila_inicio}:O{fila_fin}"

        servicio_sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=rango,
            valueInputOption='USER_ENTERED',
            body={'values': valores}
        ).execute()

        print(f"✅ ¡Éxito! Se escribieron {len(valores)} filas en '{NOMBRE_HOJA_HISTORICO}' (desde la fila {fila_inicio}).")

        sheet_id = _obtener_sheet_id_por_nombre(servicio_sheets, spreadsheet_id, NOMBRE_HOJA_HISTORICO)
        if sheet_id is not None:
            _aplicar_negrita_observaciones(
                servicio_sheets, spreadsheet_id, sheet_id, fila_inicio,
                lista_resultados, COLUMNA_OBSERVACION_HISTORICO
            )
    except Exception as e:
        print(f"Error al escribir en '{NOMBRE_HOJA_HISTORICO}': {e}")


def actualizar_fecha_estadisticas_diarias(servicio_sheets, spreadsheet_id, fecha_hoy):
    """
    Escribe la fecha de hoy en B1 de 'Estadísticas del Día'. Esa hoja tiene fórmulas COUNTIFS
    que filtran 'Histórico' por esa fecha (columna A), así que no hace falta calcular ningún
    porcentaje acá: al cambiar B1, los conteos —y los gráficos vinculados en el Slides que
    apuntan a ellos— se recalculan solos.
    """
    try:
        servicio_sheets.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{NOMBRE_HOJA_ESTADISTICAS_DIA}'!B1",
            valueInputOption='USER_ENTERED',
            body={'values': [[fecha_hoy]]}
        ).execute()
        print(f"✅ Fecha de '{NOMBRE_HOJA_ESTADISTICAS_DIA}' actualizada a {fecha_hoy}.")
    except Exception as e:
        print(f"   [⚠️ No se pudo actualizar '{NOMBRE_HOJA_ESTADISTICAS_DIA}': {e}]")

def refrescar_graficos_slides(servicio_slides, id_presentacion, object_ids):
    """
    Fuerza el refresh de los gráficos vinculados (sheetsChart) insertados en el Slides de
    estadísticas, para que reflejen los valores recién escritos en 'Estadísticas del Día'.
    """
    if not id_presentacion or not object_ids:
        return
    peticiones = [{'refreshSheetsChart': {'objectId': oid}} for oid in object_ids]
    try:
        servicio_slides.presentations().batchUpdate(
            presentationId=id_presentacion,
            body={'requests': peticiones}
        ).execute()
        print(f"✅ Se refrescaron {len(object_ids)} gráficos en el Slides de estadísticas.")
    except Exception as e:
        print(f"   [⚠️ No se pudieron refrescar los gráficos del Slides: {e}]")

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


def generar_documento_informe(servicio_drive, id_plantilla, id_carpeta_destino, datos_alumno, fecha_hoy):
    """
    Genera el informe de un alumno copiando la plantilla de Google Docs y
    reemplazando los placeholders {{...}} por la información analizada por la IA.
    """
    apellido = datos_alumno.get('apellido_estudiante', '')
    nombre = datos_alumno.get('nombre_estudiante', 'Desconocido')
    nombre_documento = f"Informe_LinkedIn_{apellido}_{nombre}".strip()

    print(f"✍️  Generando documento de feedback para: {nombre} {apellido}...")

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

        # 2. Autenticamos el servicio de Google Docs
        creds = autenticar_google()
        servicio_docs = build('docs', 'v1', credentials=creds)

        # 3. Armamos un request de tipo replaceAllText por cada placeholder
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
            print(f"   ⚠️ El documento de {nombre} {apellido} se creó, pero no se pudo completar la información.")

        print(f"✅ Documento guardado en Drive (ID: {doc_id}).")
        return doc_id

    except Exception as e:
        print(f"Error al generar el informe de {nombre} {apellido}: {e}")
        return None


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

def ejecutar_pipeline(callback_progreso=None, carrera_cohorte=None):
    """
    Corre el pipeline completo: extrae los PDFs pendientes de la carpeta de Drive, los analiza
    con la IA, escribe los resultados en Sheets y genera los informes individuales en Docs.

    Se separó del bloque `if __name__ == "__main__":` para que, además del script de consola de
    siempre (`python main.py`), también la pueda invocar el backend web (FastAPI) sin duplicar
    lógica.

    callback_progreso, si se pasa, es una función con la forma
    callback_progreso(mensaje: str, procesados: int, total: int) — pensada para que el backend
    web reporte avance en vivo (ej. "Analizando perfil 4 de 12...") sin tener que parsear la
    salida de consola. Si no se pasa, no hace nada distinto: el script de consola sigue
    funcionando exactamente igual que antes, basado en los print().

    carrera_cohorte: nombre canónico de una carrera de CARRERAS_UDESA, o None. Si se pasa, todo
    el lote se analiza como si fuera de esa carrera (modo "cohorte completa"), avisando por
    perfil si el texto no la menciona. Si es None, cada perfil usa su propia detección
    automática (modo "conjunto mixto"). El script de consola lo pide con _preguntar_modo_analisis()
    antes de llamar a esta función; el backend web lo pasaría directo.

    Devuelve un diccionario resumen (útil para el dashboard web):
        {
            "fecha": "dd/mm/aaaa",
            "total_pdfs": int,
            "analizados": int,
            "fallidos": int,
            "resultados": [ ... lista de JSONs de análisis ... ]
        }

    Lanza RuntimeError si falla la autenticación con Drive, para que el backend web pueda
    distinguir ese caso de "no había perfiles para analizar".
    """
    def _avisar(mensaje, procesados=0, total=0):
        print(mensaje)
        if callback_progreso:
            callback_progreso(mensaje, procesados, total)

    _avisar("Iniciando Pipeline de Desarrollo Profesional UdeSA...")

    # Fecha calculada una única vez acá, y pasada como parámetro al resto del pipeline.
    hoy = date.today()
    fecha_hoy = hoy.strftime("%d/%m/%Y")   # Formato humano: IA, informes, columna 'Fecha' del Histórico
    fecha_iso = hoy.strftime("%Y-%m-%d")   # Formato ISO: nombres de hoja y de subcarpetas

    # Validaciones tempranas: si falta algún ID, avisamos antes de procesar nada.
    if not ID_PLANTILLA_INFORME:
        print("⚠️  ADVERTENCIA: No se encontró ID_PLANTILLA_INFORME en el archivo .env.")
        print("   Los informes individuales no podrán generarse hasta configurar esa variable.")
    if not ID_CARPETA_ANALIZADOS:
        print("⚠️  ADVERTENCIA: No se encontró ID_CARPETA_ANALIZADOS en el archivo .env.")
        print("   Los perfiles analizados no se moverán hasta configurar esa variable.")

    servicio_drive = autenticar_drive()
    if not servicio_drive:
        raise RuntimeError("No se pudo autenticar con Google Drive. Revisá las credenciales.")

    lista_pdfs = listar_pdfs_en_carpeta(servicio_drive, ID_CARPETA)
    total_pdfs = len(lista_pdfs)
    _avisar(f"Se encontraron {total_pdfs} perfiles para analizar.\n", 0, total_pdfs)

    # Subcarpeta de 'Analizados' del día (se crea una sola vez si no existe).
    carpeta_analizados_hoy = None
    # Lo comento solo para seguir con las pruebas. Luego descomentar para que se muevan los perfiles
    #if ID_CARPETA_ANALIZADOS:
    #    carpeta_analizados_hoy = obtener_o_crear_subcarpeta(
    #        servicio_drive, ID_CARPETA_ANALIZADOS, fecha_iso
    #    )

    resultados_finales = []  # Aquí guardaremos todos los JSONs

    for indice, archivo in enumerate(lista_pdfs, start=1):
        _avisar(f"Procesando: {archivo['name']}...", indice - 1, total_pdfs)

        # Paso A: Extraer texto y URL
        texto, url_perfil = extraer_texto_drive_en_memoria(servicio_drive, archivo['id'])

        # Paso B: Mandar a la IA
        if texto:
            analisis_json = analizar_perfil_con_ia(texto, fecha_hoy, carrera_cohorte)
            if analisis_json:
                analisis_json['url_perfil'] = url_perfil
                if carrera_cohorte:
                    # Aseguramos que el campo que arma la IA coincida textualmente con la
                    # carrera elegida (se usa para las carpetas por carrera y para Histórico).
                    analisis_json['carrera_estudiante'] = carrera_cohorte
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
                print("   ⚠️ No se pudo analizar (fallaron los 3 proveedores). Queda en la carpeta original para reintentar.")

        print("-" * 40)

    # PASO 3: Escribir en la hoja diaria y en el Histórico
    servicio_sheets = autenticar_sheets()
    if servicio_sheets and resultados_finales:
        nombre_hoja_hoy = obtener_o_crear_hoja_diaria(servicio_sheets, ID_SPREADSHEET, fecha_iso)
        escribir_matriz_sheets(servicio_sheets, ID_SPREADSHEET, resultados_finales, nombre_hoja_hoy)
        escribir_historico_sheets(servicio_sheets, ID_SPREADSHEET, resultados_finales, fecha_hoy)
        actualizar_fecha_estadisticas_diarias(servicio_sheets, ID_SPREADSHEET, fecha_hoy)
        servicio_slides = autenticar_slides()
        if servicio_slides and ID_PRESENTACION_STATS:
            refrescar_graficos_slides(servicio_slides, ID_PRESENTACION_STATS, OBJECT_IDS_GRAFICOS_STATS)

    # PASO 4: Generar los Google Docs individuales, en la subcarpeta de informes del día
    if ID_PLANTILLA_INFORME and resultados_finales:
        _avisar("\nIniciando fase de creación de reportes individuales...", len(resultados_finales), total_pdfs)
        carpeta_informes_hoy = obtener_o_crear_subcarpeta(
            servicio_drive, ID_CARPETA_INFORMES, fecha_iso
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
                ID_PLANTILLA_INFORME,
                carpetas_carrera_hoy[nombre_carpeta],
                resultado,
                fecha_hoy
            )

    _avisar("🎉 PIPELINE FINALIZADO.", total_pdfs, total_pdfs)

    return {
        "fecha": fecha_hoy,
        "total_pdfs": total_pdfs,
        "analizados": len(resultados_finales),
        "fallidos": total_pdfs - len(resultados_finales),
        "resultados": resultados_finales,
    }


if __name__ == "__main__":
    carrera_cohorte = _preguntar_modo_analisis()
    ejecutar_pipeline(carrera_cohorte=carrera_cohorte)