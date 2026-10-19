"""
generar_presentacion_stats.py

Genera automáticamente la presentación de estadísticas (Google Slides) de una corrida del
pipeline, a partir de un archivo "master" con tokens {{...}} en el texto y formas nativas
etiquetadas (alt text) en los gráficos, en vez de datos de ejemplo.

CÓMO FUNCIONA
-------------
1. Existe un archivo maestro en Google Slides (nunca se edita directamente) donde:
   a) el texto dinámico está escrito como "{{SEMAFORO_VERDE_PCT}}" en vez de "[XX%]",
   b) cada barra, celda o segmento que tiene que cambiar de tamaño o color tiene un "texto
      alternativo" (alt text) único (ver CHECKLIST DEL MASTER abajo).
   Todo lo que cambia con los datos son formas nativas de Slides que el código controla: no
   hay gráficos vinculados a Sheets. Así la presentación usa siempre la misma paleta y la misma
   tipografía, y los gráficos coinciden siempre con los números.
2. Las filas salen del Histórico (columnas Fecha, Carrera, Modo y los estados), filtradas según
   el caso (ver generar_presentacion_desde_historico):
     - al terminar una corrida de cohorte: esa cohorte en ese día,
     - al terminar una corrida mixta: todas las corridas mixtas de ese día,
     - desde la web: un rango de fechas, opcionalmente filtrado por carrera y/o modo.
   Con esas filas:
   a) calcula los porcentajes,
   b) duplica el master (Drive API) hacia la carpeta de presentaciones,
   c) reemplaza cada token de texto por su valor real (Slides API, replaceAllText) y achica
      la letra del título si es largo, para que no se pise con el subtítulo,
   d) redimensiona las barras y recolorea la grilla y los anillos según esos mismos
      porcentajes (Slides API, updatePageElementTransform / updateShapeProperties),
   e) devuelve el id / link de la presentación ya lista para revisar y compartir.

Los cuadros "Tu análisis" y los datos de contacto de la diapo de cierre siguen sin token ni
alt text a propósito: se completan a mano, como en el resto del flujo.

CONFIGURACIÓN NECESARIA (agregar a .env / .env.yaml)
-----------------------------------------------------
ID_PRESENTACION_MASTER_STATS="id_del_archivo_master"

(ID_CARPETA_PRESENTACIONES ya existe en el proyecto — ahí se guarda cada presentación
generada. ID_SPREADSHEET también ya existe — de su Histórico salen las filas.)

CHECKLIST DEL MASTER
--------------------
Texto: cada campo dinámico dice su token {{...}} (FECHA_CORRIDA, CANT_ALUMNOS, los *_PCT, etc.).

Formas (alt text: click derecho > "Texto alternativo" > "Opciones avanzadas" > "Título", con
el nombre exacto, sin espacios):

  Portada:
    TITULO_PRESENTACION            (el cuadro del título: se le ajusta el tamaño de letra)

  Diapo "Análisis general · semáforo":
    SEMAFORO_SEG_01 ... SEMAFORO_SEG_20
      (los 20 segmentos del anillo, en sentido horario desde arriba: el código pinta los
      primeros N de verde, los siguientes M de amarillo y el resto de rojo; cada uno = 5%)

  Diapo "Titular y URL personalizado":
    BARRA_TITULAR_APROBADO, BARRA_TITULAR_MEJORAR, BARRA_TITULAR_NODETECTADO
      (los 3 segmentos de la barra de Titular, en ese orden de izquierda a derecha)
    BARRA_URL_APROBADO, BARRA_URL_MEJORAR
      (los 2 segmentos de la barra de URL — esta tiene solo 2 estados)

  Diapo "Acerca de":
    ACERCADE_SEG_01 ... ACERCADE_SEG_20   (anillo, igual que el del semáforo)

  Diapo "Experiencia laboral":
    BARRA_EXPERIENCIA_APROBADO, BARRA_EXPERIENCIA_MEJORAR, BARRA_EXPERIENCIA_NODETECTADO

  Diapo "Educación y certificaciones":
    TRACK_EDUCACION                (el fondo fijo de la barra de Educación)
    BARRA_EDUCACION_APROBADO       (el relleno verde que se agranda o achica)
    BARRA_CERTIFICACIONES_APROBADO, BARRA_CERTIFICACIONES_MEJORAR, BARRA_CERTIFICACIONES_NODETECTADO

  Diapo "Aptitudes":
    APTITUDES_CELL_01 ... APTITUDES_CELL_20   (la grilla de 20 cuadraditos, cada uno = 5%)

El largo total de cada barra es el que tiene en el master (ancho que se ve, con su escala):
el código solo reparte ese largo entre los segmentos según los porcentajes.

Para probarlo solo: `python generar_presentacion_stats.py 2026-10-08` (un día) o
`python generar_presentacion_stats.py 2026-10-01 2026-10-17 --carrera "Abogacía" --modo Cohorte`
(ver el bloque main abajo). Si un token da "0 ocurrencias" o una forma sale como "no encontrada", ese nombre no
quedó escrito igual en el master (typo, mayúscula de más, etc.).
"""

import math
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build


# ==============================================================================
# CONFIGURACIÓN — AJUSTAR SI LOS HEADERS REALES DE LA HOJA SON DISTINTOS
# ==============================================================================

# Encabezados del Histórico (las hojas diarias usan los mismos, salvo Fecha y Modo).
COLUMNA_FECHA = "Fecha"
COLUMNA_MODO = "Modo"            # "Cohorte" o "Mixto": de qué tipo de corrida salió la fila
COLUMNA_CARRERA = "Carrera"
COLUMNA_SEMAFORO = "Semáforo"
COLUMNA_URL = "Estado Url"
COLUMNA_TITULAR = "Estado Titular"
COLUMNA_ACERCA_DE = "Estado Acerca de"
COLUMNA_EXPERIENCIA = "Estado Experiencia"
COLUMNA_EDUCACION = "Estado Educación"
COLUMNA_CERTIFICACIONES = "Estado Certificaciones"
COLUMNA_APTITUDES = "Estado Aptitudes"

# Colores del sistema: verde=Aprobado, amarillo=A Mejorar, rojo=No detectado (y Verde/Amarillo/Rojo
# en el semáforo).
CANTIDAD_CELDAS = 20  # grilla de aptitudes y anillos: cada celda o segmento representa 100/20 = 5%
COLOR_APROBADO = {"red": 23 / 255, "green": 138 / 255, "blue": 76 / 255}     # #178A4C
COLOR_MEJORAR = {"red": 224 / 255, "green": 161 / 255, "blue": 0 / 255}      # #E0A100
COLOR_NODETECTADO = {"red": 192 / 255, "green": 57 / 255, "blue": 43 / 255}  # #C0392B

# Título de la portada: entra en un renglón a 45 pt hasta ~36 caracteres (tipografía Antonio en
# un cuadro de 813 pt). Más largo, se achica la letra en proporción para que no pase a un segundo
# renglón y se pise con el subtítulo.
TAMANO_TITULO_PT = 45
CARACTERES_TITULO_A_TAMANO_COMPLETO = 36
TAMANO_TITULO_MINIMO_PT = 24

PT_A_EMU = 12700  # por si algún elemento del master quedó en puntos en vez de EMU

MODO_COHORTE = "Cohorte"
MODO_MIXTO = "Mixto"
PREFIJO_HOJA_HISTORICO = "Histórico"  # una hoja por año: "Histórico 2026", "Histórico 2027", ...


# ==============================================================================
# HELPERS DE SERVICIO (mismo patrón que construir_servicio_slides/sheets en main.py)
# ==============================================================================

def construir_servicio_drive(creds):
    return build("drive", "v3", credentials=creds)


# ==============================================================================
# MÓDULO 1: LECTURA Y CÁLCULO DE MÉTRICAS DEL DÍA
# ==============================================================================

def _normalizar(texto):
    import unicodedata
    if texto is None:
        return ""
    texto = unicodedata.normalize("NFKD", str(texto).strip().lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


def leer_filas_hoja_como_diccionarios(servicio_sheets, spreadsheet_id, nombre_hoja):
    """Lee una hoja completa y la devuelve como lista de diccionarios {header: valor}, sin las
    filas vacías (si quedó alguna en el medio, no tiene que contar como un alumno más)."""
    rango = f"'{nombre_hoja}'!A1:ZZ"
    resultado = servicio_sheets.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=rango
    ).execute()
    valores = resultado.get("values", [])
    if len(valores) < 2:
        return []

    headers = valores[0]
    return [
        {headers[i]: (fila[i] if i < len(fila) else "") for i in range(len(headers))}
        for fila in valores[1:]
        if any(str(celda).strip() for celda in fila)
    ]


def _parsear_fecha(texto):
    """Fecha de la columna Fecha del Histórico ("7/10/2026" o "07/10/2026"), o None si no se puede leer."""
    try:
        dia, mes, anio = (int(parte) for parte in str(texto).strip().split("/"))
        return date(anio, mes, dia)
    except (ValueError, TypeError):
        return None


def leer_filas_historico(servicio_sheets, spreadsheet_id, desde, hasta):
    """Filas del Histórico con Fecha entre desde y hasta (inclusive, objetos date), leyendo la hoja
    de cada año del rango ("Histórico 2026", ...). Las hojas que no existen se saltean. Cada fila
    lleva además "_fecha" (date), para no tener que volver a parsearla."""
    titulos = {
        h["properties"]["title"]
        for h in servicio_sheets.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title").execute()["sheets"]
    }
    filas = []
    for anio in range(desde.year, hasta.year + 1):
        hoja = f"{PREFIJO_HOJA_HISTORICO} {anio}"
        if hoja not in titulos:
            continue
        for fila in leer_filas_hoja_como_diccionarios(servicio_sheets, spreadsheet_id, hoja):
            fecha = _parsear_fecha(fila.get(COLUMNA_FECHA))
            if fecha and desde <= fecha <= hasta:
                fila["_fecha"] = fecha
                filas.append(fila)
    return filas


def filtrar_filas(filas, carrera=None, modo=None):
    """Filtra por carrera y/o por modo ("Cohorte"/"Mixto"), sin distinguir mayúsculas ni tildes.
    None en cualquiera de los dos = sin filtro."""
    resultado = filas
    if carrera:
        resultado = [f for f in resultado if _normalizar(f.get(COLUMNA_CARRERA)) == _normalizar(carrera)]
    if modo:
        resultado = [f for f in resultado if _normalizar(f.get(COLUMNA_MODO)) == _normalizar(modo)]
    return resultado


def texto_periodo(desde, hasta=None):
    """"08/10/2026" para un día, "01/10/2026 – 17/10/2026" para un rango."""
    hasta = hasta or desde
    if desde == hasta:
        return desde.strftime("%d/%m/%Y")
    return f"{desde.strftime('%d/%m/%Y')} – {hasta.strftime('%d/%m/%Y')}"


def _porcentaje_entero(cantidad, total):
    """Redondea a entero (0-100). No devuelve el símbolo % — eso lo agrega quien lo muestre
    como texto; para redimensionar una forma hace falta el número solo."""
    if not total:
        return 0
    return round(100 * cantidad / total)


def calcular_metricas(filas):
    """
    Recibe las filas a resumir (de leer_filas_historico, ya filtradas) y devuelve un
    diccionario {NOMBRE: valor} con CANT_ALUMNOS (int, cantidad de filas) y un porcentaje
    entero (0-100) por cada categoría/estado real que produce el pipeline. No se inventan
    sub-métricas nuevas: son exactamente las columnas y estados que ya salen en la planilla.
    """
    total = len(filas)

    def contar(columna, *valores_esperados):
        objetivo = {_normalizar(v) for v in valores_esperados}
        return sum(1 for fila in filas if _normalizar(fila.get(columna, "")) in objetivo)

    return {
        "CANT_ALUMNOS": total,
        "SEMAFORO_VERDE_PCT": _porcentaje_entero(contar(COLUMNA_SEMAFORO, "Verde"), total),
        "SEMAFORO_AMARILLO_PCT": _porcentaje_entero(contar(COLUMNA_SEMAFORO, "Amarillo"), total),
        "SEMAFORO_ROJO_PCT": _porcentaje_entero(contar(COLUMNA_SEMAFORO, "Rojo"), total),
        "TITULAR_APROBADO_PCT": _porcentaje_entero(contar(COLUMNA_TITULAR, "Aprobado"), total),
        "TITULAR_MEJORAR_PCT": _porcentaje_entero(contar(COLUMNA_TITULAR, "A Mejorar"), total),
        "TITULAR_NODETECTADO_PCT": _porcentaje_entero(contar(COLUMNA_TITULAR, "No detectado"), total),
        "URL_APROBADO_PCT": _porcentaje_entero(contar(COLUMNA_URL, "Aprobado"), total),
        "URL_MEJORAR_PCT": _porcentaje_entero(contar(COLUMNA_URL, "A Mejorar"), total),
        "ACERCADE_APROBADO_PCT": _porcentaje_entero(contar(COLUMNA_ACERCA_DE, "Aprobado"), total),
        "ACERCADE_MEJORAR_PCT": _porcentaje_entero(contar(COLUMNA_ACERCA_DE, "A Mejorar"), total),
        "ACERCADE_NODETECTADO_PCT": _porcentaje_entero(contar(COLUMNA_ACERCA_DE, "No detectado"), total),
        "EXPERIENCIA_APROBADO_PCT": _porcentaje_entero(contar(COLUMNA_EXPERIENCIA, "Aprobado"), total),
        "EXPERIENCIA_MEJORAR_PCT": _porcentaje_entero(contar(COLUMNA_EXPERIENCIA, "A Mejorar"), total),
        "EXPERIENCIA_NODETECTADO_PCT": _porcentaje_entero(contar(COLUMNA_EXPERIENCIA, "No detectado"), total),
        "EDUCACION_APROBADO_PCT": _porcentaje_entero(contar(COLUMNA_EDUCACION, "Aprobado"), total),
        "CERTIFICACIONES_APROBADO_PCT": _porcentaje_entero(contar(COLUMNA_CERTIFICACIONES, "Aprobado"), total),
        "CERTIFICACIONES_MEJORAR_PCT": _porcentaje_entero(contar(COLUMNA_CERTIFICACIONES, "A Mejorar"), total),
        "CERTIFICACIONES_NODETECTADO_PCT": _porcentaje_entero(contar(COLUMNA_CERTIFICACIONES, "No detectado"), total),
        "APTITUDES_APROBADO_PCT": _porcentaje_entero(contar(COLUMNA_APTITUDES, "Aprobado"), total),
        "APTITUDES_MEJORAR_PCT": _porcentaje_entero(contar(COLUMNA_APTITUDES, "A Mejorar"), total),
        "APTITUDES_NODETECTADO_PCT": _porcentaje_entero(contar(COLUMNA_APTITUDES, "No detectado"), total),
    }


def subtitulo_desde_filas(filas):
    """Subtítulo de la portada según las carreras de las filas: el nombre si es una sola (una
    cohorte), "Perfiles de N carreras" si son varias, o vacío si la columna no tiene datos."""
    carreras = {str(fila.get(COLUMNA_CARRERA, "")).strip() for fila in filas} - {""}
    if len(carreras) == 1:
        return carreras.pop()
    if carreras:
        return f"Perfiles de {len(carreras)} carreras"
    return ""


def formatear_tokens_texto(metricas, periodo, titulo_presentacion, subtitulo_presentacion):
    """Arma el diccionario {TOKEN: 'texto a insertar'} para el reemplazo de texto,
    a partir de las métricas numéricas (agrega el símbolo % donde corresponde).
    periodo: el texto de texto_periodo() ("08/10/2026" o "01/10/2026 – 17/10/2026"); va en el
    token FECHA_CORRIDA, que está en la etiqueta de la portada y en el pie de cada diapositiva."""
    mapa = {
        "FECHA_CORRIDA": periodo,
        "CANT_ALUMNOS": str(metricas["CANT_ALUMNOS"]),
        "TITULO_PRESENTACION": titulo_presentacion,
        "SUBTITULO_PRESENTACION": subtitulo_presentacion,
    }
    for nombre, valor in metricas.items():
        if nombre == "CANT_ALUMNOS":
            continue
        mapa[nombre] = f"{valor}%"
    return mapa


# ==============================================================================
# MÓDULO 2: DUPLICAR EL MASTER (Drive API)
# ==============================================================================

def _buscar_archivo_por_nombre_en_carpeta(servicio_drive, carpeta_id, nombre):
    nombre_escapado = nombre.replace("'", "\\'")
    query = f"'{carpeta_id}' in parents and name = '{nombre_escapado}' and trashed = false"
    resultado = servicio_drive.files().list(
        q=query,
        fields="files(id, name)",
        spaces="drive",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute()
    archivos = resultado.get("files", [])
    return archivos[0]["id"] if archivos else None


def duplicar_o_reemplazar_presentacion(servicio_drive, id_master, carpeta_destino_id, nombre_archivo):
    """Duplica el master hacia carpeta_destino_id. Si ya existe una presentación con ese
    nombre (por ejemplo, la misma cohorte corrida otra vez ese día), la manda a la papelera y arranca de una copia limpia
    del master — nunca reutiliza una copia ya completada, porque ahí ya no quedan tokens.

    El master (y potencialmente la carpeta de destino) viven en un Drive compartido, así que
    todas las llamadas necesitan supportsAllDrives=True — sin eso, la API de Drive devuelve
    "File not found" para archivos que en realidad sí existen y son accesibles."""
    id_existente = _buscar_archivo_por_nombre_en_carpeta(servicio_drive, carpeta_destino_id, nombre_archivo)
    if id_existente:
        servicio_drive.files().update(
            fileId=id_existente, body={"trashed": True}, supportsAllDrives=True
        ).execute()

    copia = servicio_drive.files().copy(
        fileId=id_master,
        body={"name": nombre_archivo, "parents": [carpeta_destino_id]},
        supportsAllDrives=True,
    ).execute()
    return copia["id"]


# ==============================================================================
# MÓDULO 3: REEMPLAZO DE TEXTO (Slides API — tokens {{...}})
# ==============================================================================

def reemplazar_tokens_presentacion(servicio_slides, presentation_id, mapa_tokens_texto, avisar=print):
    requests = [
        {
            "replaceAllText": {
                "containsText": {"text": f"{{{{{token}}}}}", "matchCase": True},
                "replaceText": valor,
            }
        }
        for token, valor in mapa_tokens_texto.items()
    ]
    respuesta = servicio_slides.presentations().batchUpdate(
        presentationId=presentation_id, body={"requests": requests}
    ).execute()

    sin_uso = [
        token for token, reply in zip(mapa_tokens_texto.keys(), respuesta.get("replies", []))
        if reply.get("replaceAllText", {}).get("occurrencesChanged", 0) == 0
    ]
    if sin_uso:
        avisar(f"   [⚠️ Presentación: estos tokens no aparecieron en el master: {', '.join(sin_uso)}]")


def tamano_titulo_pt(titulo):
    """Tamaño de letra del título de la portada según su largo (ver TAMANO_TITULO_PT)."""
    largo = len(titulo or "")
    if largo <= CARACTERES_TITULO_A_TAMANO_COMPLETO:
        return TAMANO_TITULO_PT
    return max(TAMANO_TITULO_MINIMO_PT,
               math.floor(TAMANO_TITULO_PT * CARACTERES_TITULO_A_TAMANO_COMPLETO / largo))


def requests_tamano_titulo(mapa_formas, titulo, avisar=print):
    """Achica la letra del título si es largo. Va DESPUÉS del reemplazo de tokens: el cuadro ya
    tiene el texto real. Si el título entra a tamaño completo, no se toca nada."""
    tamano = tamano_titulo_pt(titulo)
    if tamano == TAMANO_TITULO_PT:
        return []
    forma = mapa_formas.get("TITULO_PRESENTACION")
    if not forma:
        avisar("   [⚠️ Presentación: no encontré la forma 'TITULO_PRESENTACION' en el master; "
               "el título largo puede pisarse con el subtítulo.]")
        return []
    return [{
        "updateTextStyle": {
            "objectId": forma["objectId"],
            "textRange": {"type": "ALL"},
            "style": {"fontSize": {"magnitude": tamano, "unit": "PT"}},
            "fields": "fontSize",
        }
    }]


# ==============================================================================
# MÓDULO 4: FORMAS NATIVAS — barras redimensionadas, grilla y anillos recoloreados
# ==============================================================================

def mapear_formas_por_alt_text(servicio_slides, presentation_id):
    """Recorre todas las diapositivas (y grupos, si los hay) y arma un diccionario
    {texto_alternativo: {objectId, size, transform}} para poder ubicar cada forma
    etiquetada en el master, sin depender de su objectId."""
    presentacion = servicio_slides.presentations().get(presentationId=presentation_id).execute()
    mapa = {}

    def recorrer(elementos):
        for el in elementos:
            titulo = el.get("title")
            if titulo:
                mapa[titulo] = {
                    "objectId": el["objectId"],
                    "size": el.get("size"),
                    "transform": el.get("transform"),
                }
            if "elementGroup" in el:
                recorrer(el["elementGroup"]["children"])

    for pagina in presentacion.get("slides", []):
        recorrer(pagina.get("pageElements", []))

    return mapa


def _en_emu(magnitud, unidad):
    return magnitud * PT_A_EMU if unidad == "PT" else magnitud


def _ancho_actual_emu(forma):
    """Ancho con el que se VE la forma en la diapositiva: el ancho intrínseco (size.width) por la
    escala horizontal (transform.scaleX). Antes se usaba solo el intrínseco, y como las formas
    del master tienen todas 236 pt intrínsecos estirados con distintas escalas, cada barra
    terminaba más corta o más larga que en el diseño (ej. Titular 709 pt en vez de 800, o
    Experiencia 709 pt en vez de 380, metiéndose debajo del recuadro de análisis)."""
    intrinseco = _en_emu(forma["size"]["width"]["magnitude"], forma["size"]["width"]["unit"])
    return intrinseco * abs(forma["transform"].get("scaleX", 1))


def _x_actual_emu(forma):
    return _en_emu(forma["transform"]["translateX"], forma["transform"].get("unit", "EMU"))


def _request_resize_ancho(forma, nuevo_ancho_emu, nuevo_x_emu):
    """Arma el request que redimensiona una forma a un ancho absoluto (en EMU) y la mueve a
    una posición X absoluta, sin tocar su alto, su Y ni su rotación.

    El ancho final que se ve en la diapositiva es size.width (intrínseco, fijo) × scaleX
    (variable) — por eso para llegar a un ancho absoluto hay que dividir por el ancho
    intrínseco, no por el ancho actualmente renderizado."""
    ancho_intrinseco_emu = _en_emu(forma["size"]["width"]["magnitude"], forma["size"]["width"]["unit"])
    nuevo_scale_x = nuevo_ancho_emu / ancho_intrinseco_emu if ancho_intrinseco_emu else 0
    # La API de Slides rechaza un scale exactamente en 0 ("affine transform not invertible" —
    # la matriz queda sin inversa). Pasa cuando un segmento da 0% en el día. En vez de eso,
    # lo dejamos en un ancho mínimo casi invisible, no matemáticamente cero.
    nuevo_scale_x = max(nuevo_scale_x, 0.0001)

    t = forma["transform"]
    return {
        "updatePageElementTransform": {
            "objectId": forma["objectId"],
            "transform": {
                "scaleX": nuevo_scale_x,
                "scaleY": t["scaleY"],
                "shearX": t.get("shearX", 0),
                "shearY": t.get("shearY", 0),
                "translateX": nuevo_x_emu,
                "translateY": t["translateY"],
                "unit": "EMU",
            },
            "applyMode": "ABSOLUTE",
        }
    }


def _request_segmento(forma, nuevo_ancho_emu, nuevo_x_emu, ancho_total_emu):
    """Redimensiona un segmento de barra, o lo borra de la copia si queda en 0% (menos de medio
    punto porcentual del total): achicado casi a cero igual se veía como una rayita al final de
    la barra. Se borra solo en la presentación generada, nunca en el master."""
    if nuevo_ancho_emu < ancho_total_emu * 0.005:
        return {"deleteObject": {"objectId": forma["objectId"]}}
    return _request_resize_ancho(forma, nuevo_ancho_emu, nuevo_x_emu)


def _formas_o_aviso(mapa_formas, alt_texts, avisar):
    faltantes = [t for t in alt_texts if t not in mapa_formas]
    if faltantes:
        avisar(f"   [⚠️ Presentación: no encontré estas formas en el master (revisar el alt text): {', '.join(faltantes)}]")
        return None
    return [mapa_formas[t] for t in alt_texts]


def requests_barra_dos_segmentos(mapa_formas, alt_text_seg1, alt_text_seg2, pct_seg1, avisar=print):
    """Para barras de 2 estados (URL): dos segmentos de color que siempre suman el 100% de la
    barra (no hay fondo neutro). pct_seg1 es 0-100; el segundo se calcula como el resto, para
    que nunca queden huecos ni superposición."""
    formas = _formas_o_aviso(mapa_formas, [alt_text_seg1, alt_text_seg2], avisar)
    if not formas:
        return []

    seg1, seg2 = formas
    ancho_total = _ancho_actual_emu(seg1) + _ancho_actual_emu(seg2)
    x_izquierda = min(_x_actual_emu(seg1), _x_actual_emu(seg2))

    nuevo_ancho1 = ancho_total * (pct_seg1 / 100)
    nuevo_ancho2 = ancho_total - nuevo_ancho1
    nuevo_x2 = x_izquierda + nuevo_ancho1

    return [
        _request_segmento(seg1, nuevo_ancho1, x_izquierda, ancho_total),
        _request_segmento(seg2, nuevo_ancho2, nuevo_x2, ancho_total),
    ]


def requests_barra_tres_segmentos(mapa_formas, alt_text_seg1, alt_text_seg2, alt_text_seg3, pct_seg1, pct_seg2,
                                  avisar=print):
    """Para barras de 3 estados (Titular, Experiencia, Certificaciones): tres segmentos de
    color que siempre suman el 100% de la barra. pct_seg1 y pct_seg2 son 0-100; el tercero
    se calcula como el resto, para que nunca queden huecos ni superposición."""
    formas = _formas_o_aviso(mapa_formas, [alt_text_seg1, alt_text_seg2, alt_text_seg3], avisar)
    if not formas:
        return []

    seg1, seg2, seg3 = formas
    ancho_total = _ancho_actual_emu(seg1) + _ancho_actual_emu(seg2) + _ancho_actual_emu(seg3)
    x_izquierda = min(_x_actual_emu(seg1), _x_actual_emu(seg2), _x_actual_emu(seg3))

    nuevo_ancho1 = ancho_total * (pct_seg1 / 100)
    nuevo_ancho2 = ancho_total * (pct_seg2 / 100)
    # max(0, ...): los porcentajes vienen redondeados y 1 y 2 pueden sumar 101.
    nuevo_ancho3 = max(0, ancho_total - nuevo_ancho1 - nuevo_ancho2)

    nuevo_x2 = x_izquierda + nuevo_ancho1
    nuevo_x3 = nuevo_x2 + nuevo_ancho2

    return [
        _request_segmento(seg1, nuevo_ancho1, x_izquierda, ancho_total),
        _request_segmento(seg2, nuevo_ancho2, nuevo_x2, ancho_total),
        _request_segmento(seg3, nuevo_ancho3, nuevo_x3, ancho_total),
    ]


def requests_barra_simple(mapa_formas, alt_text_track, alt_text_fill, pct_fill, avisar=print):
    """Para barras tipo Educación: un fondo fijo (track) y un relleno que se agranda o achica,
    ambos anclados al mismo borde izquierdo."""
    formas = _formas_o_aviso(mapa_formas, [alt_text_track, alt_text_fill], avisar)
    if not formas:
        return []

    track, fill = formas
    ancho_total = _ancho_actual_emu(track)
    x_izquierda = _x_actual_emu(track)
    nuevo_ancho = ancho_total * (pct_fill / 100)

    return [_request_segmento(fill, nuevo_ancho, x_izquierda, ancho_total)]


def cantidades_por_celda(porcentajes, cantidad_celdas=CANTIDAD_CELDAS):
    """Reparte cantidad_celdas entre las categorías según sus porcentajes, por el método del
    mayor resto: siempre suman exactamente cantidad_celdas, y cada categoría queda a lo sumo a
    una celda de su valor exacto. (Redondear cada una por separado podía dar 19 o 21 celdas.)

    Si los porcentajes no suman 100 (por ejemplo, perfiles con un estado que no es ninguno de
    los tres), se reparte en proporción entre los tres."""
    total = sum(porcentajes)
    if total <= 0:
        return [0] * (len(porcentajes) - 1) + [cantidad_celdas]
    exactos = [cantidad_celdas * p / total for p in porcentajes]
    cantidades = [math.floor(x) for x in exactos]
    faltan = cantidad_celdas - sum(cantidades)
    for i in sorted(range(len(exactos)), key=lambda i: exactos[i] - cantidades[i], reverse=True)[:faltan]:
        cantidades[i] += 1
    return cantidades


def requests_celdas(mapa_formas, prefijo_alt_text, pct_aprobado, pct_mejorar, pct_nodetectado, avisar=print):
    """Recolorea las 20 celdas de una grilla o los 20 segmentos de un anillo (alt text
    <prefijo>01 a <prefijo>20): las primeras N en verde, las siguientes M en amarillo y el resto
    en rojo, según cantidades_por_celda (cada celda = 5%)."""
    n = CANTIDAD_CELDAS
    cantidad_aprobado, cantidad_mejorar, _ = cantidades_por_celda([pct_aprobado, pct_mejorar, pct_nodetectado], n)

    requests = []
    faltantes = []
    for i in range(1, n + 1):
        alt_text = f"{prefijo_alt_text}{i:02d}"
        forma = mapa_formas.get(alt_text)
        if not forma:
            faltantes.append(alt_text)
            continue
        if i <= cantidad_aprobado:
            color = COLOR_APROBADO
        elif i <= cantidad_aprobado + cantidad_mejorar:
            color = COLOR_MEJORAR
        else:
            color = COLOR_NODETECTADO
        requests.append({
            "updateShapeProperties": {
                "objectId": forma["objectId"],
                "shapeProperties": {"shapeBackgroundFill": {"solidFill": {"color": {"rgbColor": color}}}},
                "fields": "shapeBackgroundFill.solidFill.color",
            }
        })
    if faltantes:
        avisar(f"   [⚠️ Presentación: faltan {len(faltantes)} formas '{prefijo_alt_text}..' en el master "
               f"(revisar el alt text): {', '.join(faltantes[:3])}{'…' if len(faltantes) > 3 else ''}]")
    return requests


def actualizar_formas_nativas(servicio_slides, presentation_id, metricas, titulo_presentacion="", avisar=print):
    """Junta las barras, la grilla, los anillos y el tamaño del título en un solo batchUpdate."""
    mapa_formas = mapear_formas_por_alt_text(servicio_slides, presentation_id)
    m = metricas

    requests = []
    requests += requests_tamano_titulo(mapa_formas, titulo_presentacion, avisar)
    requests += requests_celdas(mapa_formas, "SEMAFORO_SEG_", m["SEMAFORO_VERDE_PCT"], m["SEMAFORO_AMARILLO_PCT"],
                                m["SEMAFORO_ROJO_PCT"], avisar)
    requests += requests_barra_tres_segmentos(
        mapa_formas, "BARRA_TITULAR_APROBADO", "BARRA_TITULAR_MEJORAR", "BARRA_TITULAR_NODETECTADO",
        m["TITULAR_APROBADO_PCT"], m["TITULAR_MEJORAR_PCT"], avisar,
    )
    requests += requests_barra_dos_segmentos(mapa_formas, "BARRA_URL_APROBADO", "BARRA_URL_MEJORAR",
                                             m["URL_APROBADO_PCT"], avisar)
    requests += requests_celdas(mapa_formas, "ACERCADE_SEG_", m["ACERCADE_APROBADO_PCT"], m["ACERCADE_MEJORAR_PCT"],
                                m["ACERCADE_NODETECTADO_PCT"], avisar)
    requests += requests_barra_tres_segmentos(
        mapa_formas, "BARRA_EXPERIENCIA_APROBADO", "BARRA_EXPERIENCIA_MEJORAR", "BARRA_EXPERIENCIA_NODETECTADO",
        m["EXPERIENCIA_APROBADO_PCT"], m["EXPERIENCIA_MEJORAR_PCT"], avisar,
    )
    requests += requests_barra_simple(mapa_formas, "TRACK_EDUCACION", "BARRA_EDUCACION_APROBADO",
                                      m["EDUCACION_APROBADO_PCT"], avisar)
    requests += requests_barra_tres_segmentos(
        mapa_formas, "BARRA_CERTIFICACIONES_APROBADO", "BARRA_CERTIFICACIONES_MEJORAR", "BARRA_CERTIFICACIONES_NODETECTADO",
        m["CERTIFICACIONES_APROBADO_PCT"], m["CERTIFICACIONES_MEJORAR_PCT"], avisar,
    )
    requests += requests_celdas(mapa_formas, "APTITUDES_CELL_", m["APTITUDES_APROBADO_PCT"], m["APTITUDES_MEJORAR_PCT"],
                                m["APTITUDES_NODETECTADO_PCT"], avisar)

    if requests:
        servicio_slides.presentations().batchUpdate(presentationId=presentation_id, body={"requests": requests}).execute()


# ==============================================================================
# ORQUESTADOR
# ==============================================================================

def nombre_presentacion(desde, hasta=None, carrera=None, modo=None):
    """Nombre del archivo: "Estadísticas DP — [Cohorte|Mixto —] [<carrera> —] <fecha o rango ISO>".
    Con la fecha en formato ISO para que los archivos queden ordenados en el Drive."""
    hasta = hasta or desde
    partes = ["Estadísticas DP"]
    if modo:
        partes.append(MODO_COHORTE if _normalizar(modo) == _normalizar(MODO_COHORTE) else MODO_MIXTO)
    if carrera:
        partes.append(carrera)
    partes.append(desde.isoformat() if desde == hasta else f"{desde.isoformat()} a {hasta.isoformat()}")
    return " — ".join(partes)


def subtitulo_presentacion(filas, carrera=None, modo=None):
    """"Cohorte · Maestría en Marketing", "Conjunto mixto · Perfiles de 4 carreras", etc."""
    base = carrera or subtitulo_desde_filas(filas)
    if modo and _normalizar(modo) == _normalizar(MODO_COHORTE):
        etiqueta = "Cohorte"
    elif modo:
        etiqueta = "Conjunto mixto"
    else:
        return base
    return f"{etiqueta} · {base}" if base else etiqueta


def generar_presentacion_stats(
    creds,
    filas,
    desde=None,
    hasta=None,
    titulo_presentacion="Análisis de perfiles de LinkedIn",
    subtitulo_presentacion="",
    id_master=None,
    carpeta_destino_id=None,
    nombre_archivo=None,
    avisar=print,
):
    """
    Genera la presentación a partir de las filas que se le pasen (de una corrida, de un día o de
    un rango del Histórico). desde/hasta son el período que se muestra en la portada y en el pie
    (por defecto, hoy en Argentina).

    nombre_archivo: por defecto nombre_presentacion(desde, hasta). Si ya existe uno igual en la
    carpeta, se manda a la papelera y se regenera. avisar: igual que en el pipeline, para que los
    problemas lleguen a la web.

    Devuelve (presentation_id, url_presentacion).
    """
    # Misma zona horaria que ZONA_HORARIA en main.py (no se importa de ahí para no arrastrar todo
    # el pipeline al importar este módulo): en Cloud Run, date.today() da la fecha en UTC.
    desde = desde or datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")).date()
    hasta = hasta or desde
    id_master = id_master or os.environ["ID_PRESENTACION_MASTER_STATS"]
    carpeta_destino_id = carpeta_destino_id or os.environ["ID_CARPETA_PRESENTACIONES"]
    nombre_archivo = nombre_archivo or nombre_presentacion(desde, hasta)

    servicio_drive = construir_servicio_drive(creds)
    servicio_slides = build("slides", "v1", credentials=creds)

    metricas = calcular_metricas(filas)
    mapa_tokens_texto = formatear_tokens_texto(metricas, texto_periodo(desde, hasta), titulo_presentacion,
                                               subtitulo_presentacion)

    presentation_id = duplicar_o_reemplazar_presentacion(
        servicio_drive, id_master, carpeta_destino_id, nombre_archivo
    )

    reemplazar_tokens_presentacion(servicio_slides, presentation_id, mapa_tokens_texto, avisar)
    actualizar_formas_nativas(servicio_slides, presentation_id, metricas, titulo_presentacion, avisar)

    url_presentacion = f"https://docs.google.com/presentation/d/{presentation_id}/edit"
    print(f"Presentación generada: {url_presentacion}")
    return presentation_id, url_presentacion


def generar_presentacion_desde_historico(creds, servicio_sheets, spreadsheet_id, desde, hasta=None, carrera=None,
                                         modo=None, carpeta_destino_id=None, id_master=None, nombre_archivo=None,
                                         avisar=print):
    """
    Lee el Histórico entre desde y hasta, filtra por carrera y/o modo, y genera la presentación con
    esas filas. Es la misma función para la presentación automática de cada corrida (un día, una
    cohorte o los mixtos) y para la que se pide desde la web por rango de fechas.

    Devuelve (presentation_id, url, cantidad_de_perfiles), o None si no hay filas que cumplan el
    filtro (con un aviso).
    """
    hasta = hasta or desde
    filas = filtrar_filas(leer_filas_historico(servicio_sheets, spreadsheet_id, desde, hasta), carrera, modo)
    if not filas:
        avisar(f"⚠️ No hay perfiles en el Histórico para {texto_periodo(desde, hasta)}"
               f"{' · ' + carrera if carrera else ''}{' · ' + modo if modo else ''}: no se generó la presentación.")
        return None
    presentation_id, url = generar_presentacion_stats(
        creds, filas, desde=desde, hasta=hasta,
        subtitulo_presentacion=subtitulo_presentacion(filas, carrera, modo),
        id_master=id_master, carpeta_destino_id=carpeta_destino_id,
        nombre_archivo=nombre_archivo or nombre_presentacion(desde, hasta, carrera, modo),
        avisar=avisar,
    )
    return presentation_id, url, len(filas)


# ==============================================================================
# PRUEBA STANDALONE (correr a mano, ver el bloque de abajo)
# ==============================================================================

if __name__ == "__main__":
    import argparse
    import entorno
    # main también las carga al importarse; se llama acá para que quede claro de dónde salen
    # ID_SPREADSHEET y los demás IDs (.env, o .env.prueba si existe).
    entorno.cargar_variables()
    from main import autenticar_google_cli, construir_servicio_sheets

    parser = argparse.ArgumentParser(description="Genera una presentación de prueba a partir del Histórico.")
    parser.add_argument("desde", help="fecha de inicio, AAAA-MM-DD (ej. 2026-10-08)")
    parser.add_argument("hasta", nargs="?", help="fecha de fin, AAAA-MM-DD (por defecto, igual a desde)")
    parser.add_argument("--carrera", help="solo esa carrera (nombre exacto, sin importar tildes)")
    parser.add_argument("--modo", choices=[MODO_COHORTE, MODO_MIXTO], help="solo cohortes o solo conjuntos mixtos")
    args = parser.parse_args()
    desde = date.fromisoformat(args.desde)
    hasta = date.fromisoformat(args.hasta) if args.hasta else desde

    creds = autenticar_google_cli()
    resultado = generar_presentacion_desde_historico(
        creds, construir_servicio_sheets(creds), os.environ["ID_SPREADSHEET"], desde, hasta,
        carrera=args.carrera, modo=args.modo,
        nombre_archivo="PRUEBA — " + nombre_presentacion(desde, hasta, args.carrera, args.modo),
    )
    if resultado:
        print(f"{resultado[2]} perfiles en la presentación.")
