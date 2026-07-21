import io
import json
import os
import pdfplumber
import time
import hashlib

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
    'https://www.googleapis.com/auth/documents'       # Permiso para inyectar texto en Docs
]

# Obtenemos los IDs y credenciales de forma segura
ID_SPREADSHEET = os.getenv('ID_SPREADSHEET')
ID_CARPETA = os.getenv('ID_CARPETA')
ID_CARPETA_INFORMES = os.getenv('ID_CARPETA_INFORMES')
ID_PLANTILLA_INFORME = os.getenv('ID_PLANTILLA_INFORME')  # <-- NUEVO: ID del Google Doc plantilla
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
NVIDIA_API_KEY = os.getenv('NVIDIA_API_KEY')

client = genai.Client(api_key=GEMINI_API_KEY)

INSTRUCCIONES_SISTEMA = """
Eres un Consultor Senior de Empleabilidad de la Universidad de San Andrés (UdeSA).
Tu tarea es evaluar el texto extraído del perfil de LinkedIn de un estudiante y devolver
ÚNICAMENTE un objeto JSON válido con la evaluación.

CONTEXTO TEMPORAL:
Hoy es {FECHA_ACTUAL}. Evalúa la coherencia de los meses y años transcurridos en base a esta fecha actual.

CRITERIOS DE EVALUACIÓN (CHECKLIST UDESA):
1. Fundamentales:
   - Titular: ¿Es descriptivo, incluye palabras clave y comunica valor?
   - Ubicación: ¿Está actualizada?
   - URL: ¿Tiene un formato limpio y personalizado?
   (Nota: Ignora Foto de Perfil y Banner, el formato PDF no las incluye).

2. Contenido y Experiencia:
   - Acerca de: ¿Es una narrativa convincente, optimizada y con llamado a la acción?
   - Experiencia Laboral: ¿Usa verbos de acción y métricas/logros en lugar de solo tareas?
   - Educación: ¿Está completa y relevante?
   - Certificaciones: ¿Añadió credenciales importantes?

3. Interacción y Red (si aplica en el texto):
   - Aptitudes: ¿Listó habilidades clave? ¿Tiene validaciones?
   - Recomendaciones: ¿Recibió recomendaciones escritas?

REGLAS DE PUNTUACIÓN Y SEMÁFORO:
1. Asigna un "puntaje_general" del 1 al 100 basado en el cumplimiento del checklist.
2. El "color_semaforo" se calcula estrictamente: Verde (75-100), Amarillo (50-74), Rojo (0-49).

ESTRUCTURA EXACTA DEL JSON:
{
  "apellido_estudiante": "Apellido",
  "nombre_estudiante": "Nombre",
  "puntaje_general": 0,
  "color_semaforo": "Verde, Amarillo o Rojo",
  "observacion_principal": "• Fuerte: [El mayor acierto]\\n• Crítico: [El error más grave]\\n• Acción: [Paso inmediato a seguir]",
  "evaluacion_detallada": {
    "titular": {"estado": "Aprobado | A Mejorar", "comentario": "..."},
    "ubicacion_y_url": {"estado": "Aprobado | A Mejorar", "comentario": "..."},
    "acerca_de": {"estado": "Aprobado | A Mejorar", "comentario": "..."},
    "experiencia_laboral": {"estado": "Aprobado | A Mejorar", "comentario": "..."},
    "educacion_y_certificaciones": {"estado": "Aprobado | A Mejorar", "comentario": "..."},
    "aptitudes_y_recomendaciones": {"estado": "Aprobado | No detectado", "comentario": "..."}
  }
}

IMPORTANTE: "observacion_principal" debe ser un string único utilizando saltos de línea (\\n) y viñetas (•) para mantener la concisión, máximo 3 puntos.
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
                q=query, spaces='drive', fields='nextPageToken, files(id, name)', pageToken=pagina_token
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
    try:
        request = servicio.files().get_media(fileId=file_id)
        archivo_memoria = io.BytesIO()
        downloader = MediaIoBaseDownload(archivo_memoria, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()

        archivo_memoria.seek(0)
        texto_completo = ""
        with pdfplumber.open(archivo_memoria) as pdf:
            for pagina in pdf.pages:
                texto_extraido = pagina.extract_text()
                if texto_extraido:
                    texto_completo += texto_extraido + "\n"
        return texto_completo
    except Exception as e:
        return None

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


def calcular_hash_perfil(texto_perfil):
    contenido = texto_perfil + INSTRUCCIONES_SISTEMA  # si cambian las instrucciones, cambia el hash
    return hashlib.sha256(contenido.encode('utf-8')).hexdigest()


CACHE_ANALISIS = cargar_cache()

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
        model="llama-3.3-70b-versatile", # model="llama-3.1-8b-instant" por si hay que analizar muchos
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
        model="meta/llama-3.3-70b-instruct",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        top_p=0.7,
        max_tokens=2048,
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


def analizar_perfil_con_ia(texto_perfil, fecha_hoy):
    """Orquestador: caché → Gemini → Groq → NVIDIA."""

    hash_perfil = calcular_hash_perfil(texto_perfil)
    if hash_perfil in CACHE_ANALISIS:
        print("   [💾 Resultado obtenido de caché, no se llamó a ninguna IA.]")
        return CACHE_ANALISIS[hash_perfil]

    instrucciones_con_fecha = INSTRUCCIONES_SISTEMA.replace("{FECHA_ACTUAL}", fecha_hoy)
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


def escribir_matriz_sheets(servicio_sheets, spreadsheet_id, lista_resultados):
    """Toma el JSON de la IA y agrega las filas respetando la estructura de 5 columnas."""
    print("\nEscribiendo datos en Google Sheets...")

    valores = []
    for resultado in lista_resultados:
        # Aseguramos el orden exacto: Apellido, Nombre, Puntaje, Semáforo, Observación
        fila = [
            resultado.get('apellido_estudiante', ''),
            resultado.get('nombre_estudiante', 'Desconocido'),
            resultado.get('puntaje_general', 0),
            resultado.get('color_semaforo', 'Error'),
            resultado.get('observacion_principal', 'Sin observaciones.')
        ]
        valores.append(fila)

    cuerpo = {'values': valores}

    try:
        # Cambiamos el rango a 'A2:E' para abarcar tus 5 columnas
        resultado = servicio_sheets.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range='A2:E',
            valueInputOption='USER_ENTERED',
            body=cuerpo
        ).execute()

        filas_actualizadas = resultado.get('updates').get('updatedCells')
        print(f"✅ ¡Éxito! Se actualizaron {filas_actualizadas} celdas en la matriz.")
    except Exception as e:
        print(f"Error al escribir en Sheets: {e}")


# ==============================================================================
# MÓDULO 4: GOOGLE DOCS (Generación de Informes a partir de Plantilla)
# ==============================================================================

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
        "FECHA_INFORME": fecha_hoy,
        "PUNTAJE_GENERAL": str(datos_alumno.get('puntaje_general', 0)),
        "COLOR_SEMAFORO": datos_alumno.get('color_semaforo', 'Desconocido'),
        "OBSERVACION_PRINCIPAL": datos_alumno.get('observacion_principal', ''),

        "ESTADO_TITULAR": estado('titular'),
        "COMENTARIO_TITULAR": comentario('titular'),

        "ESTADO_UBICACION_URL": estado('ubicacion_y_url'),
        "COMENTARIO_UBICACION_URL": comentario('ubicacion_y_url'),

        "ESTADO_ACERCA_DE": estado('acerca_de'),
        "COMENTARIO_ACERCA_DE": comentario('acerca_de'),

        "ESTADO_EXPERIENCIA_LABORAL": estado('experiencia_laboral'),
        "COMENTARIO_EXPERIENCIA_LABORAL": comentario('experiencia_laboral'),

        "ESTADO_EDUCACION_CERTIFICACIONES": estado('educacion_y_certificaciones'),
        "COMENTARIO_EDUCACION_CERTIFICACIONES": comentario('educacion_y_certificaciones'),

        "ESTADO_APTITUDES_RECOMENDACIONES": estado('aptitudes_y_recomendaciones'),
        "COMENTARIO_APTITUDES_RECOMENDACIONES": comentario('aptitudes_y_recomendaciones'),
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
            fields='id'
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

if __name__ == "__main__":
    print("Iniciando Pipeline de Desarrollo Profesional UdeSA...")

    fecha_hoy = date.today().strftime("%d/%m/%Y")

    # Validación temprana: si falta el ID de la plantilla, avisamos antes de procesar nada.
    if not ID_PLANTILLA_INFORME:
        print("⚠️  ADVERTENCIA: No se encontró ID_PLANTILLA_INFORME en el archivo .env.")
        print("   Los informes individuales no podrán generarse hasta configurar esa variable.")

    servicio_drive = autenticar_drive()

    if servicio_drive:
        lista_pdfs = listar_pdfs_en_carpeta(servicio_drive, ID_CARPETA)
        print(f"Se encontraron {len(lista_pdfs)} perfiles para analizar.\n")

        resultados_finales = []  # Aquí guardaremos todos los JSONs

        for archivo in lista_pdfs:
            print(f"Procesando: {archivo['name']}...")

            # Paso A: Extraer texto
            texto = extraer_texto_drive_en_memoria(servicio_drive, archivo['id'])

            # Paso B: Mandar a la IA
            if texto:
                analisis_json = analizar_perfil_con_ia(texto, fecha_hoy)
                if analisis_json:
                    resultados_finales.append(analisis_json)
                    print(f"✅ Análisis completado para: {analisis_json.get('nombre_estudiante', 'Desconocido')}")

            print("-" * 40)

        servicio_sheets = autenticar_sheets()
        if servicio_sheets and resultados_finales:
            escribir_matriz_sheets(servicio_sheets, ID_SPREADSHEET, resultados_finales)

        # PASO 4: Generar los Google Docs individuales a partir de la plantilla
        if ID_PLANTILLA_INFORME:
            print("\nIniciando fase de creación de reportes individuales...")
            for resultado in resultados_finales:
                generar_documento_informe(
                    servicio_drive,
                    ID_PLANTILLA_INFORME,
                    ID_CARPETA_INFORMES,
                    resultado,
                    fecha_hoy
                )

        print("\n🎉 PIPELINE FINALIZADO.")
        # Imprimimos la lista completa de resultados estructurados
        # print(json.dumps(resultados_finales, indent=2, ensure_ascii=False))