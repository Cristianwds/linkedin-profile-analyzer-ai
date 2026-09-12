"""
web_app.py

Backend web (FastAPI) para que cualquier persona del equipo de Desarrollo Profesional pueda
correr el pipeline de análisis de LinkedIn y gestionar carreras/planes de estudio sin tocar
código ni la terminal.

Este archivo NO reimplementa nada del pipeline: importa las funciones de main.py y
extraccion_plan_estudios.py tal cual están, y solo les agrega:

  1. LOGIN POR PERSONA: cada quien entra con su propia cuenta de Google (OAuth "Authorization
     Code" para apps web, no el flujo de escritorio que usa `python main.py`). El pipeline
     corre siempre con la identidad de quien está logueado en el navegador — no hay una cuenta
     "del sistema" compartida. El token de cada persona se guarda en tokens/<email>.json.
  2. Ejecución en background (el pipeline pega a varias APIs externas y tarda minutos; no puede
     bloquear el request HTTP).
  3. Un endpoint de estado que el frontend consulta cada pocos segundos (polling) para mostrar
     el progreso en vivo.
  4. CRUD simple sobre carreras.json (agregar/editar/borrar carrera, subir plan de estudios).

Setup de autenticación (ver README para el detalle paso a paso):
  - Hace falta un cliente OAuth de tipo "Aplicación web" en Google Cloud Console (distinto del
    cliente "Aplicación de escritorio" que usa `python main.py`), con estas URIs de redirección
    autorizadas: http://localhost:8080/auth/callback (para probar en local) y
    https://TU-DOMINIO/auth/callback (para producción).
  - Descargar ese cliente como client_secret_web.json y ponerlo en la raíz del repo (nunca se
    commitea — ya está en .gitignore).
  - Definir SESSION_SECRET en el .env (una clave random fija; si no se define, se genera una al
    arrancar el proceso y todas las sesiones activas se invalidan cada vez que reinicia el
    server — anotalo en el .env para que no pase).
  - Opcional: DOMINIO_PERMITIDO=udesa.edu.ar en el .env, para rechazar logins de cuentas de
    Google que no sean del dominio institucional.

Correr en local:
    pip install -r requirements.txt
    uvicorn web_app:app --reload --port 8080
Luego abrir http://localhost:8080
"""

import json
import os
import re
import secrets
import threading
import time
import uuid
from typing import List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

import main as pipeline
import extraccion_plan_estudios as extractor
import almacenamiento_estado

app = FastAPI(title="UdeSA · Análisis de Perfiles de LinkedIn")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Firma las cookies de sesión (guardan solo el email de la persona logueada, nada más sensible).
# Sin SESSION_SECRET fijo en producción, cada reinicio del proceso desloguea a todo el mundo.
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
if not os.getenv("SESSION_SECRET"):
    print("   [⚠️ SESSION_SECRET no está seteada: se genera una al azar y las sesiones activas "
          "se invalidan en cada reinicio del contenedor. Definila fija en Cloud Run para prod.]")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")

CLIENT_SECRETS_FILE = os.getenv("GOOGLE_OAUTH_CLIENT_SECRETS_FILE", "client_secret_web.json")
DOMINIO_PERMITIDO = os.getenv("DOMINIO_PERMITIDO")  # ej. "udesa.edu.ar"; None = cualquier cuenta de Google
CARPETA_TOKENS = "tokens"


# ==============================================================================
# LOGIN POR PERSONA (OAuth 2.0 - Authorization Code para app web)
# ==============================================================================

def _archivo_token_usuario(email):
    """Nombre de archivo seguro derivado del email, para tokens/<email>.json."""
    seguro = re.sub(r'[^a-zA-Z0-9_.-]', '_', email.lower())
    return os.path.join(CARPETA_TOKENS, f"{seguro}.json")


def guardar_credenciales_usuario(email, creds):
    almacenamiento_estado.escribir_texto(_archivo_token_usuario(email), creds.to_json())


def cargar_credenciales_usuario(email):
    """Lee el token de esa persona (disco o bucket, según almacenamiento_estado), y lo refresca
    si hace falta (guardando el nuevo access token). Devuelve None si no hay token guardado o si
    no se pudo refrescar (en ese caso hay que volver a loguearse)."""
    contenido = almacenamiento_estado.leer_texto(_archivo_token_usuario(email))
    if contenido is None:
        return None

    creds = Credentials.from_authorized_user_info(json.loads(contenido), pipeline.SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(GoogleAuthRequest())
            guardar_credenciales_usuario(email, creds)
        except Exception:
            return None

    return creds if creds and creds.valid else None


def _construir_flow(request: Request):
    # OAUTH_REDIRECT_URI permite fijar la URL a mano (útil detrás de un proxy/Cloud Run donde
    # request.url_for a veces arma el esquema http en vez de https). Si no está seteada, se
    # deduce del propio request.
    redirect_uri = os.getenv("OAUTH_REDIRECT_URI") or str(request.url_for("auth_callback"))

    # oauthlib exige HTTPS por seguridad y corta con InsecureTransportError si detecta un
    # redirect_uri en http:// — correcto en producción, pero rompe las pruebas en localhost
    # (donde no hay HTTPS). Esta bandera SOLO habilita la excepción cuando el redirect_uri es
    # http (nunca en producción, donde Google exige https igual). Nunca actives esto a mano vía
    # variable de entorno para un dominio público real.
    if redirect_uri.startswith("http://"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

    return Flow.from_client_secrets_file(
        CLIENT_SECRETS_FILE, scopes=pipeline.SCOPES, redirect_uri=redirect_uri
    )


@app.get("/auth/login")
def auth_login(request: Request):
    if not os.path.exists(CLIENT_SECRETS_FILE):
        raise HTTPException(
            500,
            f"Falta '{CLIENT_SECRETS_FILE}' en el servidor. Hace falta un cliente OAuth de tipo "
            "'Aplicación web' descargado de Google Cloud Console (ver README)."
        )
    flow = _construir_flow(request)
    estado = secrets.token_urlsafe(16)
    url_autorizacion, _ = flow.authorization_url(
        access_type="offline",       # necesario para conseguir refresh_token
        prompt="consent",            # fuerza a que Google lo entregue siempre, no solo la primera vez
        state=estado,
        include_granted_scopes="true",
    )
    # El code_verifier de PKCE recién existe DESPUÉS de llamar a authorization_url() (ahí es
    # donde la librería lo genera) — guardarlo antes, como en un intento anterior, lo dejaba en
    # None. Se persiste en la sesión junto con el state, para reusarlo en auth_callback.
    request.session["oauth_state"] = estado
    request.session["oauth_code_verifier"] = flow.code_verifier
    return RedirectResponse(url_autorizacion)


@app.get("/auth/callback", name="auth_callback")
def auth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse(f"/?error={error}")
    if not code or not state or state != request.session.get("oauth_state"):
        return RedirectResponse("/?error=estado_invalido")

    flow = _construir_flow(request)
    flow.code_verifier = request.session.get("oauth_code_verifier")
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        # Se imprime el detalle real en la consola del servidor — el mensaje genérico en pantalla
        # no alcanza para diagnosticar (invalid_grant, redirect_uri_mismatch, access_denied, etc.)
        print(f"[❌ auth_callback] Falló el intercambio de código por token: {e}")
        return RedirectResponse("/?error=login_fallido")

    creds = flow.credentials

    try:
        servicio_oauth2 = build('oauth2', 'v2', credentials=creds)
        info = servicio_oauth2.userinfo().get().execute()
        email = info.get('email')
    except Exception as e:
        print(f"[❌ auth_callback] Falló la consulta de userinfo: {e}")
        return RedirectResponse("/?error=no_se_pudo_identificar")

    if not email:
        return RedirectResponse("/?error=no_se_pudo_identificar")

    if DOMINIO_PERMITIDO and not email.lower().endswith(f"@{DOMINIO_PERMITIDO.lower()}"):
        return RedirectResponse("/?error=dominio_no_autorizado")

    guardar_credenciales_usuario(email, creds)
    request.session["email"] = email
    request.session.pop("oauth_state", None)
    request.session.pop("oauth_code_verifier", None)
    return RedirectResponse("/")


@app.get("/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/api/auth/estado")
def api_auth_estado(request: Request):
    email = request.session.get("email")
    if not email:
        return {"autenticado": False, "email": None}
    # Si el token de esta persona ya no sirve (revocado, expirado sin refresh_token), la
    # deslogueamos acá para que el frontend le vuelva a pedir el login.
    if not cargar_credenciales_usuario(email):
        request.session.clear()
        return {"autenticado": False, "email": None}
    return {"autenticado": True, "email": email}


def usuario_actual(request: Request) -> Credentials:
    """Dependencia de FastAPI: exige sesión activa y devuelve las Credentials de esa persona."""
    email = request.session.get("email")
    if not email:
        raise HTTPException(401, "Iniciá sesión con Google primero.")
    creds = cargar_credenciales_usuario(email)
    if not creds:
        request.session.clear()
        raise HTTPException(401, "Tu sesión de Google expiró o fue revocada. Iniciá sesión de nuevo.")
    return creds


def email_actual(request: Request) -> str:
    return request.session.get("email")


# ==============================================================================
# ESTADO DE CORRIDAS DEL PIPELINE (en memoria, un solo proceso)
# ==============================================================================
# OJO: esto asume un único worker/proceso (típico en Cloud Run con esta escala de uso). Si el
# día de mañana se necesita más de un worker concurrente, este estado tiene que pasar a algo
# compartido (ej. Firestore) — no debería hacer falta para el volumen actual del pipeline.

RUNS = {}
RUNS_LOCK = threading.Lock()
MAX_LOG_POR_CORRIDA = 500  # tope de líneas de log guardadas por corrida, para no crecer sin límite


def _nueva_entrada_run(email):
    return {
        "estado": "corriendo",  # corriendo | finalizado | error
        "log": [],
        "procesados": 0,
        "total": 0,
        "resumen": None,
        "error": None,
        "iniciado_por": email,
        "iniciado_en": time.time(),
    }


def _correr_pipeline_en_thread(run_id, creds, carrera_cohorte):
    def callback(mensaje, procesados, total):
        with RUNS_LOCK:
            entrada = RUNS[run_id]
            entrada["log"].append(mensaje)
            if len(entrada["log"]) > MAX_LOG_POR_CORRIDA:
                entrada["log"] = entrada["log"][-MAX_LOG_POR_CORRIDA:]
            entrada["procesados"] = procesados
            entrada["total"] = total

    try:
        resumen = pipeline.ejecutar_pipeline(creds, callback_progreso=callback, carrera_cohorte=carrera_cohorte)
        with RUNS_LOCK:
            RUNS[run_id]["estado"] = "finalizado"
            RUNS[run_id]["resumen"] = resumen
    except Exception as e:
        with RUNS_LOCK:
            RUNS[run_id]["estado"] = "error"
            RUNS[run_id]["error"] = str(e)
            RUNS[run_id]["log"].append(f"❌ El pipeline se interrumpió: {e}")


class IniciarPipelineBody(BaseModel):
    carrera_cohorte: Optional[str] = None  # None = modo "conjunto mixto"; nombre exacto = modo "cohorte"


@app.post("/api/pipeline/iniciar")
def iniciar_pipeline(body: IniciarPipelineBody, creds: Credentials = Depends(usuario_actual), email: str = Depends(email_actual)):
    if body.carrera_cohorte and body.carrera_cohorte not in pipeline.CARRERAS_UDESA:
        raise HTTPException(400, f"'{body.carrera_cohorte}' no es una carrera configurada.")

    # Evita pisar una corrida que ya está en curso (el pipeline mueve archivos de Drive; correr
    # dos veces en simultáneo podría duplicar trabajo o pisarse) — esto aplica para todo el
    # servidor, no solo para esta persona, porque comparten la misma carpeta de Drive.
    with RUNS_LOCK:
        hay_corrida_activa = any(r["estado"] == "corriendo" for r in RUNS.values())
    if hay_corrida_activa:
        raise HTTPException(409, "Ya hay una corrida del pipeline en curso. Esperá a que termine.")

    run_id = str(uuid.uuid4())
    with RUNS_LOCK:
        RUNS[run_id] = _nueva_entrada_run(email)

    hilo = threading.Thread(
        target=_correr_pipeline_en_thread, args=(run_id, creds, body.carrera_cohorte), daemon=True
    )
    hilo.start()

    return {"run_id": run_id}


@app.get("/api/pipeline/estado/{run_id}")
def estado_pipeline(run_id: str, _creds: Credentials = Depends(usuario_actual)):
    with RUNS_LOCK:
        entrada = RUNS.get(run_id)
        if entrada is None:
            raise HTTPException(404, "No existe esa corrida.")
        return dict(entrada)


# ==============================================================================
# GESTIÓN DE CARRERAS Y PLANES DE ESTUDIO
# ==============================================================================

class CarreraBody(BaseModel):
    nombre: str
    palabras_clave: List[str]
    nivel: str  # "grado" | "posgrado"


class PlanTextoBody(BaseModel):
    texto: str


@app.get("/api/carreras")
def listar_carreras(_creds: Credentials = Depends(usuario_actual)):
    pipeline.recargar_carreras()
    resultado = []
    for nombre, datos in pipeline.CARRERAS_UDESA.items():
        ruta_plan = os.path.join(pipeline.CARPETA_PLANES_DE_ESTUDIO, datos.get("archivo_plan", ""))
        resultado.append({
            "nombre": nombre,
            "palabras_clave": datos.get("palabras_clave", []),
            "archivo_plan": datos.get("archivo_plan"),
            "nivel": datos.get("nivel", "grado"),
            "tiene_plan": almacenamiento_estado.existe(ruta_plan),
        })
    resultado.sort(key=lambda c: c["nombre"])
    return resultado


@app.post("/api/carreras")
def crear_o_actualizar_carrera(body: CarreraBody, _creds: Credentials = Depends(usuario_actual)):
    if body.nivel not in ("grado", "posgrado"):
        raise HTTPException(400, "El nivel tiene que ser 'grado' o 'posgrado'.")
    try:
        datos = pipeline.agregar_o_actualizar_carrera(body.nombre, body.palabras_clave, nivel=body.nivel)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"nombre": body.nombre.strip(), **datos}


@app.delete("/api/carreras/{nombre}")
def borrar_carrera(nombre: str, borrar_archivo_plan: bool = False, _creds: Credentials = Depends(usuario_actual)):
    if not pipeline.eliminar_carrera(nombre, borrar_archivo_plan=borrar_archivo_plan):
        raise HTTPException(404, f"No existe la carrera '{nombre}'.")
    return {"ok": True}


@app.post("/api/carreras/{nombre}/plan")
async def subir_plan_de_estudios(nombre: str, archivo: UploadFile = File(...), _creds: Credentials = Depends(usuario_actual)):
    """
    Sube el PDF oficial del plan de estudios de una carrera, lo procesa con la misma lógica de
    extraccion_plan_estudios.py (grilla por coordenadas → tablas por líneas → reordenamiento con
    IA como último recurso) y guarda el .txt resultante en planes_de_estudio/. La carrera debe
    existir de antemano (crearla primero con POST /api/carreras).
    """
    if nombre not in pipeline.CARRERAS_UDESA:
        raise HTTPException(404, f"No existe la carrera '{nombre}'. Creála primero.")
    if not archivo.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "El archivo tiene que ser un PDF.")

    os.makedirs("/tmp/planes_subidos", exist_ok=True)
    ruta_temporal = f"/tmp/planes_subidos/{uuid.uuid4()}.pdf"
    contenido = await archivo.read()
    with open(ruta_temporal, "wb") as f:
        f.write(contenido)

    avisos = []
    try:
        texto_final, metodo_usado = extractor.extraer_texto_de_pdf(
            ruta_temporal, callback_aviso=avisos.append
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        os.remove(ruta_temporal)

    nombre_archivo_txt = pipeline.CARRERAS_UDESA[nombre]["archivo_plan"]
    ruta_destino = os.path.join(pipeline.CARPETA_PLANES_DE_ESTUDIO, nombre_archivo_txt)
    almacenamiento_estado.escribir_texto(ruta_destino, texto_final)

    return {
        "nombre_carrera": nombre,
        "archivo_guardado": nombre_archivo_txt,
        "metodo_usado": metodo_usado,
        "caracteres": len(texto_final),
        "avisos": avisos,
        "requiere_revision_manual": metodo_usado in ("crudo", "ia"),
        "vista_previa": texto_final[:2000],
    }


@app.get("/api/carreras/{nombre}/plan")
def ver_plan_de_estudios(nombre: str, _creds: Credentials = Depends(usuario_actual)):
    if nombre not in pipeline.CARRERAS_UDESA:
        raise HTTPException(404, f"No existe la carrera '{nombre}'.")
    texto = pipeline.cargar_plan_de_estudios(nombre)
    if texto is None:
        raise HTTPException(404, "Esta carrera todavía no tiene un plan de estudios cargado.")
    return {"nombre_carrera": nombre, "texto": texto}


@app.put("/api/carreras/{nombre}/plan")
def editar_plan_de_estudios(nombre: str, body: PlanTextoBody, _creds: Credentials = Depends(usuario_actual)):
    """
    Sobrescribe directo el texto del plan de estudios de una carrera, sin pasar por el PDF —
    para cuando alguien quiere corregir a mano algo que la extracción automática no reconstruyó
    bien, o directamente escribir/actualizar el plan sin tener el PDF a mano.
    """
    try:
        caracteres = pipeline.guardar_texto_plan_de_estudios(nombre, body.texto)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"nombre_carrera": nombre, "caracteres": caracteres}


# ==============================================================================
# ESTADO GENERAL DEL SISTEMA (para el panel de diagnóstico en la web)
# ==============================================================================

@app.get("/api/sistema/estado")
def estado_sistema(_creds: Credentials = Depends(usuario_actual)):
    variables_requeridas = [
        "ID_SPREADSHEET", "ID_CARPETA", "ID_CARPETA_INFORMES",
        "ID_CARPETA_ANALIZADOS", "ID_PLANTILLA_INFORME", "GEMINI_API_KEY",
    ]
    faltantes = [v for v in variables_requeridas if not os.getenv(v)]
    return {
        "variables_env_faltantes": faltantes,
        "carreras_configuradas": len(pipeline.CARRERAS_UDESA),
        "hay_corrida_activa": any(r["estado"] == "corriendo" for r in RUNS.values()),
    }


# ==============================================================================
# FRONTEND ESTÁTICO
# ==============================================================================
# index.html se sirve libre (sin login) porque es el único lugar donde la propia página muestra
# el botón "Iniciar sesión con Google" — recién de ahí para adelante, todas las llamadas a /api/*
# exigen sesión activa (ver Depends(usuario_actual) en cada endpoint de arriba).

@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")