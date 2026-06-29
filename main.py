import io
import json
import os
import pdfplumber
import time 
from google import genai
from google.genai import types
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from dotenv import load_dotenv

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
    'https://www.googleapis.com/auth/spreadsheets', # Permiso para editar el spread sheet
    'https://www.googleapis.com/auth/documents'     # Permiso para inyectar texto en Docs
]

# Obtenemos los IDs y credenciales de forma segura
ID_SPREADSHEET = os.getenv('ID_SPREADSHEET')
ID_CARPETA = os.getenv('ID_CARPETA')
ID_CARPETA_INFORMES = os.getenv('ID_CARPETA_INFORMES')

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
client = genai.Client(api_key=GEMINI_API_KEY)

INSTRUCCIONES_SISTEMA = """
Eres un Consultor Senior de Empleabilidad de la Universidad de San Andrés (UdeSA).
Tu tarea es evaluar el texto extraído del perfil de LinkedIn de un estudiante y devolver
ÚNICAMENTE un objeto JSON válido con la evaluación.

CONTEXTO TEMPORAL:
Hoy es junio de 2026. Evalúa la coherencia de los meses y años transcurridos en base a esta fecha actual.

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
            resultados = servicio.files().list(q=query, spaces='drive', fields='nextPageToken, files(id, name)', pageToken=pagina_token).execute()
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
# MÓDULO 2: GEMINI (Análisis de IA con Manejo de Cuotas)
# ==============================================================================
def analizar_perfil_con_ia(texto_perfil, intentos_maximos=4):
    prompt = f"{INSTRUCCIONES_SISTEMA}\n\nPERFIL DEL ESTUDIANTE:\n{texto_perfil}"
    
    for intento in range(intentos_maximos):
        try:
            respuesta = client.models.generate_content(
                model='gemini-flash-latest',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2 
                )
            )
            return json.loads(respuesta.text)
            
        except Exception as e:
            error_msg = str(e)
            
            # Bloque 1: Si es un micro-corte del servidor (503)
            if "503" in error_msg or "demand" in error_msg:
                print(f"  [⚠️ Servidor saturado. Reintentando ({intento+1}/{intentos_maximos}) en 5 segundos...]")
                time.sleep(5)
                
            # Bloque 2: Si agotamos la cuota gratuita por minuto (429)
            elif "429" in error_msg or "Quota" in error_msg:
                print(f"  [⏳ Límite de API alcanzado. El bot pausará por 60 segundos antes del reintento ({intento+1}/{intentos_maximos})...]")
                time.sleep(60) # Pausa larga para que se reinicie el contador de Google
                
            # Bloque 3: Errores irrecuperables
            else:
                print(f"Error crítico en Gemini: {e}")
                return None
                
    print("  ❌ Se agotaron los intentos para este perfil.")
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
# MÓDULO 4: GOOGLE DOCS (Generación de Informes Detallados)
# ==============================================================================
def generar_documento_informe(servicio_drive, id_carpeta_destino, datos_alumno):
    """Genera un Google Doc formateado con la evaluación detallada del alumno."""
    apellido = datos_alumno.get('apellido_estudiante', '')
    nombre = datos_alumno.get('nombre_estudiante', 'Desconocido')
    nombre_documento = f"Informe_LinkedIn_{apellido}_{nombre}".strip()
    
    print(f"✍️ Generando documento de feedback para: {nombre} {apellido}...")
    
    # 1. Creamos un Google Doc vacío en tu carpeta "Informes Generados"
    meta_doc = {
        'name': nombre_documento,
        'mimeType': 'application/vnd.google-apps.document',
        'parents': [id_carpeta_destino]
    }
    
    try:
        doc_creado = servicio_drive.files().create(body=meta_doc, fields='id').execute()
        doc_id = doc_creado.get('id')
        
        # 2. Autenticamos el servicio específico de Google Docs usando tu usuario
        creds = autenticar_google()
        servicio_docs = build('docs', 'v1', credentials=creds)

        # 3. Armamos el contenido del informe en texto estructurado
        lineas_texto = [
            "UNIVERSIDAD DE SAN ANDRÉS - DESARROLLO PROFESIONAL\n",
            "INFORME DE OPTIMIZACIÓN DE PERFIL DE LINKEDIN\n",
            "==================================================\n\n",
            f"Estudiante: {nombre} {apellido}\n",
            f"Puntaje General: {datos_alumno.get('puntaje_general', 0)}/100\n",
            f"Estado del Semáforo: {datos_alumno.get('color_semaforo', 'Desconocido')}\n\n",
            "DIAGNÓSTICO PRINCIPAL:\n",
            f"{datos_alumno.get('observacion_principal', '')}\n\n",
            "--------------------------------------------------\n\n",
            "EVALUACIÓN DETALLADA POR SECCIÓN:\n\n"
        ]
        
        evaluacion = datos_alumno.get('evaluacion_detallada', {})
        for seccion, detalles in evaluacion.items():
            lineas_texto.append(f"• SECCIÓN: {seccion.upper()}\n")
            lineas_texto.append(f"  Estado: {detalles.get('estado', 'No evaluado')}\n")
            lineas_texto.append(f"  Feedback: {detalles.get('comentario', '')}\n\n")
            
        texto_final = "".join(lineas_texto)
        
        # 4. Inyectamos el texto al Google Doc
        pedidos = [
            {
                'insertText': {
                    'location': {'index': 1},
                    'text': texto_final
                }
            }
        ]
        servicio_docs.documents().batchUpdate(documentId=doc_id, body={'requests': pedidos}).execute()
        print(f"✅ Documento guardado en Drive.")
        
    except Exception as e:
        print(f"Error al escribir en el Google Doc de {nombre}: {e}")

# ==============================================================================
# EJECUCIÓN DEL PIPELINE (Bucle Principal)
# ==============================================================================
if __name__ == "__main__":
    print("Iniciando Pipeline de Desarrollo Profesional UdeSA...")
    servicio_drive = autenticar_drive()
    
    if servicio_drive:
        lista_pdfs = listar_pdfs_en_carpeta(servicio_drive, ID_CARPETA)
        print(f"Se encontraron {len(lista_pdfs)} perfiles para analizar.\n")
        
        resultados_finales = [] # Aquí guardaremos todos los JSONs
        
        for archivo in lista_pdfs:
            print(f"Procesando: {archivo['name']}...")
            
            # Paso A: Extraer texto
            texto = extraer_texto_drive_en_memoria(servicio_drive, archivo['id'])
            
            # Paso B: Mandar a la IA
            if texto:
                analisis_json = analizar_perfil_con_ia(texto)
                if analisis_json:
                    resultados_finales.append(analisis_json)
                    print(f"✅ Análisis completado para: {analisis_json.get('nombre_estudiante', 'Desconocido')}")
            print("-" * 40)

        servicio_sheets = autenticar_sheets()
        if servicio_sheets and resultados_finales:
            escribir_matriz_sheets(servicio_sheets, ID_SPREADSHEET, resultados_finales)
        

        # PASO 4: Generar los Google Docs individuales
        print("\nIniciando fase de creación de reportes individuales...")
        
        for resultado in resultados_finales:
            generar_documento_informe(servicio_drive, ID_CARPETA_INFORMES, resultado)

        print("\n🎉 PIPELINE FINALIZADO. Resultados obtenidos:")
        # Imprimimos la lista completa de resultados estructurados
        print(json.dumps(resultados_finales, indent=2, ensure_ascii=False))