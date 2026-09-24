"""
generar_presentacion_stats.py

Genera automáticamente la presentación de estadísticas (Google Slides) de una corrida del
pipeline, a partir de un archivo "master" con tokens {{...}} en el texto y formas nativas
etiquetadas (alt text) en los gráficos, en vez de datos de ejemplo.

CÓMO FUNCIONA
-------------
1. Existe un archivo maestro en Google Slides (nunca se edita directamente) donde:
   a) el texto dinámico está escrito como "{{SEMAFORO_VERDE_PCT}}" en vez de "[XX%]",
   b) cada barra/celda que tiene que cambiar de tamaño o color tiene un "texto alternativo"
      (alt text) único, configurado a mano una sola vez (ver CHECKLIST DEL MASTER abajo),
   c) los anillos del semáforo y de Acerca de NO son formas editables por código: son
      gráficos circulares vinculados a la hoja fija "Estadísticas del Día" (la misma que ya
      usa el resto del pipeline, NOMBRE_HOJA_ESTADISTICAS_DIA en main.py) — nunca a la hoja
      con nombre de fecha, porque esa se recrea cada día y el vínculo de Slides queda pegado
      al objeto con el que se creó. "Estadísticas del Día" en cambio nunca se recrea, solo
      se sobreescriben sus valores en el mismo lugar — por eso un gráfico armado ahí, y
      vinculado una sola vez desde el master, sigue funcionando para siempre sin retocarlo.
2. Cada corrida del pipeline:
   a) calcula los porcentajes reales del día a partir de la hoja de resultados,
   b) duplica el master (Drive API) hacia la carpeta de presentaciones,
   c) reemplaza cada token de texto por su valor real (Slides API, replaceAllText),
   d) redimensiona las barras y recolorea la grilla de pictogramas según esos mismos
      porcentajes (Slides API, updatePageElementTransform / updateShapeProperties),
   e) refresca los gráficos vinculados (reusa refrescar_graficos_slides()) — para cuando
      esto corre, "Estadísticas del Día" ya fue reescrita por el resto del pipeline,
   f) devuelve el id / link de la presentación ya lista para revisar y compartir.

Los cuadros "Tu análisis" y los datos de contacto de la diapo de cierre siguen sin token ni
alt text a propósito: se completan a mano, como en el resto del flujo.

CONFIGURACIÓN NECESARIA (agregar a .env / .env.yaml)
-----------------------------------------------------
ID_PRESENTACION_MASTER_STATS="id_del_archivo_master"

(ID_CARPETA_PRESENTACIONES ya existe en el proyecto — ahí se guarda cada presentación
generada. ID_SPREADSHEET también ya existe — de ahí sale el rango del semáforo vinculado.)

CHECKLIST DEL MASTER (una sola vez, a mano, en Google Slides)
----------------------------------------------------------------
Texto (ya cubierto en la primera vuelta): cada campo dinámico dice su token {{...}}.

Formas para redimensionar/recolorear — click derecho en la forma > "Texto alternativo" >
"Opciones avanzadas" > campo "Título" > escribir exactamente uno de estos nombres (sin
espacios, tal cual):

  Diapo "Titular y URL personalizado":
    BARRA_TITULAR_APROBADO, BARRA_TITULAR_MEJORAR, BARRA_TITULAR_NODETECTADO
      (los 3 segmentos de la barra de Titular, en ese orden de izquierda a derecha)
    BARRA_URL_APROBADO, BARRA_URL_MEJORAR
      (los 2 segmentos de la barra de URL — esta sigue teniendo solo 2 estados)

  Diapo "Experiencia laboral":
    BARRA_EXPERIENCIA_APROBADO, BARRA_EXPERIENCIA_MEJORAR, BARRA_EXPERIENCIA_NODETECTADO
      (los 3 segmentos de la barra, de izquierda a derecha)

  Diapo "Educación y certificaciones":
    TRACK_EDUCACION                (el fondo fijo de la barra de Educación — sigue 2 estados)
    BARRA_EDUCACION_APROBADO       (el relleno verde que se agranda o achica)
    BARRA_CERTIFICACIONES_APROBADO, BARRA_CERTIFICACIONES_MEJORAR, BARRA_CERTIFICACIONES_NODETECTADO
      (los 3 segmentos de la barra de Certificaciones, de izquierda a derecha — ya NO es
      una barra de relleno sobre fondo, son 3 segmentos como en Titular/Experiencia)

  Diapo "Aptitudes":
    APTITUDES_CELL_01, APTITUDES_CELL_02, ... APTITUDES_CELL_20
    (los 20 cuadraditos de la grilla, en el orden que quieras — el código pinta los
    primeros N de verde (Aprobado), los siguientes M de amarillo (A Mejorar) y el resto
    de rojo (No detectado) según los porcentajes del día)

  Diapo "Análisis general · semáforo" y diapo "Acerca de":
    1. En la hoja "Estadísticas del Día" (la fija, ya existente — no la de fecha), sobre
       los rangos de Verde/Amarillo/Rojo y de Aprobado/A Mejorar/No detectado de "Acerca
       de", armar dos gráficos circulares (uno por tabla).
    2. En cada diapo del master, borrar el gráfico de muestra e insertar un gráfico
       VINCULADO (Insertar > Gráfico > Vincular gráfico) que apunte a cada uno de esos dos
       gráficos. Los números grandes superpuestos siguen siendo tokens de texto, no hace
       falta tocarlos.
    Como "Estadísticas del Día" nunca se recrea (el resto del pipeline la sobreescribe en
    el mismo lugar, no la reemplaza), este vínculo se arma UNA sola vez y no hay que
    retocarlo nunca más — el código solo necesita refrescarlo en cada corrida.

Correr este archivo solo (bloque main abajo) contra una hoja de prueba y revisar en los
logs: si un token da "0 ocurrencias" o una forma sale como "no encontrada", ese nombre no
quedó escrito igual en el master (typo, mayúscula de más, etc.).
"""

import os
from datetime import date

from googleapiclient.discovery import build


# ==============================================================================
# CONFIGURACIÓN — AJUSTAR SI LOS HEADERS REALES DE LA HOJA SON DISTINTOS
# ==============================================================================

COLUMNA_SEMAFORO = "Semáforo general"
COLUMNA_URL = "URL personalizado"
COLUMNA_TITULAR = "Titular"
COLUMNA_ACERCA_DE = "Acerca de"
COLUMNA_EXPERIENCIA = "Experiencia laboral"
COLUMNA_EDUCACION = "Educación"
COLUMNA_CERTIFICACIONES = "Certificaciones"
COLUMNA_APTITUDES = "Aptitudes"

TOKENS_ESPERADOS = [
    "FECHA_CORRIDA", "CANT_ALUMNOS", "TITULO_PRESENTACION", "SUBTITULO_PRESENTACION",
    "SEMAFORO_VERDE_PCT", "SEMAFORO_AMARILLO_PCT", "SEMAFORO_ROJO_PCT",
    "TITULAR_APROBADO_PCT", "TITULAR_MEJORAR_PCT", "TITULAR_NODETECTADO_PCT",
    "URL_APROBADO_PCT", "URL_MEJORAR_PCT",
    "ACERCADE_APROBADO_PCT", "ACERCADE_MEJORAR_PCT", "ACERCADE_NODETECTADO_PCT",
    "EXPERIENCIA_APROBADO_PCT", "EXPERIENCIA_MEJORAR_PCT", "EXPERIENCIA_NODETECTADO_PCT",
    "EDUCACION_APROBADO_PCT",
    "CERTIFICACIONES_APROBADO_PCT", "CERTIFICACIONES_MEJORAR_PCT", "CERTIFICACIONES_NODETECTADO_PCT",
    "APTITUDES_APROBADO_PCT", "APTITUDES_MEJORAR_PCT", "APTITUDES_NODETECTADO_PCT",
]

# Colores del sistema (ver /topics del proyecto): verde=Aprobado, amarillo=A Mejorar, rojo=No detectado.
CANTIDAD_CELDAS_APTITUDES = 20  # cada celda de la grilla representa 100/20 = 5%
COLOR_APROBADO = {"red": 23 / 255, "green": 138 / 255, "blue": 76 / 255}     # #178A4C
COLOR_MEJORAR = {"red": 224 / 255, "green": 161 / 255, "blue": 0 / 255}      # #E0A100
COLOR_NODETECTADO = {"red": 192 / 255, "green": 57 / 255, "blue": 43 / 255}  # #C0392B

PT_A_EMU = 12700  # por si algún elemento del master quedó en puntos en vez de EMU


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
    """Lee una hoja completa y la devuelve como lista de diccionarios {header: valor}."""
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
    ]


def _porcentaje_entero(cantidad, total):
    """Redondea a entero (0-100). No devuelve el símbolo % — eso lo agrega quien lo muestre
    como texto; para redimensionar una forma hace falta el número solo."""
    if not total:
        return 0
    return round(100 * cantidad / total)


def calcular_metricas_del_dia(filas):
    """
    Recibe las filas del día (ver leer_filas_hoja_como_diccionarios) y devuelve un
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


def formatear_tokens_texto(metricas, fecha_iso, titulo_presentacion, subtitulo_presentacion):
    """Arma el diccionario {TOKEN: 'texto a insertar'} para el reemplazo de texto,
    a partir de las métricas numéricas (agrega el símbolo % donde corresponde)."""
    mapa = {
        "FECHA_CORRIDA": fecha_iso,
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


def duplicar_o_reemplazar_presentacion_del_dia(servicio_drive, id_master, carpeta_destino_id, nombre_archivo):
    """Duplica el master hacia carpeta_destino_id. Si ya existe una presentación con ese
    nombre (el pipeline ya corrió hoy), la manda a la papelera y arranca de una copia limpia
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

def reemplazar_tokens_presentacion(servicio_slides, presentation_id, mapa_tokens_texto):
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
        print(f"AVISO: estos tokens de texto no aparecieron en el master: {', '.join(sin_uso)}")


# ==============================================================================
# MÓDULO 4: FORMAS NATIVAS — barras redimensionadas y pictograma recoloreado
# ==============================================================================

def mapear_formas_por_alt_text(servicio_slides, presentation_id):
    """Recorre todas las diapositivas (y grupos, si los hay) y arma un diccionario
    {texto_alternativo: {objectId, size, transform}} para poder ubicar cada forma
    etiquetada en el master, sin depender de su objectId (que cambia en cada copia)."""
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
    return _en_emu(forma["size"]["width"]["magnitude"], forma["size"]["width"]["unit"])


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


def requests_barra_dos_segmentos(mapa_formas, alt_text_seg1, alt_text_seg2, pct_seg1):
    """Para barras tipo Titular/URL/Experiencia: dos segmentos de color que siempre suman
    el 100% de la barra (no hay fondo neutro). pct_seg1 es 0-100; el segundo se calcula
    como el resto, para que nunca queden huecos ni superposición."""
    if alt_text_seg1 not in mapa_formas or alt_text_seg2 not in mapa_formas:
        faltante = alt_text_seg1 if alt_text_seg1 not in mapa_formas else alt_text_seg2
        print(f"AVISO: no encontré la forma '{faltante}' en el master (revisar el alt text).")
        return []

    seg1, seg2 = mapa_formas[alt_text_seg1], mapa_formas[alt_text_seg2]
    ancho_total = _ancho_actual_emu(seg1) + _ancho_actual_emu(seg2)
    x_izquierda = min(_x_actual_emu(seg1), _x_actual_emu(seg2))

    nuevo_ancho1 = ancho_total * (pct_seg1 / 100)
    nuevo_ancho2 = ancho_total - nuevo_ancho1
    nuevo_x2 = x_izquierda + nuevo_ancho1

    return [
        _request_resize_ancho(seg1, nuevo_ancho1, x_izquierda),
        _request_resize_ancho(seg2, nuevo_ancho2, nuevo_x2),
    ]


def requests_barra_tres_segmentos(mapa_formas, alt_text_seg1, alt_text_seg2, alt_text_seg3, pct_seg1, pct_seg2):
    """Para barras de 3 estados (Titular, Experiencia, Certificaciones): tres segmentos de
    color que siempre suman el 100% de la barra. pct_seg1 y pct_seg2 son 0-100; el tercero
    se calcula como el resto, para que nunca queden huecos ni superposición."""
    tags = [alt_text_seg1, alt_text_seg2, alt_text_seg3]
    faltantes = [t for t in tags if t not in mapa_formas]
    if faltantes:
        print(f"AVISO: no encontré estas formas en el master: {', '.join(faltantes)}")
        return []

    seg1, seg2, seg3 = (mapa_formas[t] for t in tags)
    ancho_total = _ancho_actual_emu(seg1) + _ancho_actual_emu(seg2) + _ancho_actual_emu(seg3)
    x_izquierda = min(_x_actual_emu(seg1), _x_actual_emu(seg2), _x_actual_emu(seg3))

    nuevo_ancho1 = ancho_total * (pct_seg1 / 100)
    nuevo_ancho2 = ancho_total * (pct_seg2 / 100)
    nuevo_ancho3 = ancho_total - nuevo_ancho1 - nuevo_ancho2

    nuevo_x2 = x_izquierda + nuevo_ancho1
    nuevo_x3 = nuevo_x2 + nuevo_ancho2

    return [
        _request_resize_ancho(seg1, nuevo_ancho1, x_izquierda),
        _request_resize_ancho(seg2, nuevo_ancho2, nuevo_x2),
        _request_resize_ancho(seg3, nuevo_ancho3, nuevo_x3),
    ]


def requests_barra_simple(mapa_formas, alt_text_track, alt_text_fill, pct_fill):
    """Para barras tipo Educación/Certificaciones: un fondo fijo (track) y un relleno que
    se agranda o achica, ambos anclados al mismo borde izquierdo."""
    if alt_text_track not in mapa_formas or alt_text_fill not in mapa_formas:
        faltante = alt_text_track if alt_text_track not in mapa_formas else alt_text_fill
        print(f"AVISO: no encontré la forma '{faltante}' en el master (revisar el alt text).")
        return []

    track, fill = mapa_formas[alt_text_track], mapa_formas[alt_text_fill]
    ancho_total = _ancho_actual_emu(track)
    x_izquierda = _x_actual_emu(track)
    nuevo_ancho = ancho_total * (pct_fill / 100)

    return [_request_resize_ancho(fill, nuevo_ancho, x_izquierda)]


def requests_pictograma_aptitudes(mapa_formas, pct_aprobado, pct_mejorar):
    """Recolorea las 20 celdas de la grilla: las primeras N en verde (Aprobado), las
    siguientes M en amarillo (A Mejorar) y el resto en rojo (No detectado), donde N y M
    salen de redondear los porcentajes del día al 5% más cercano (cada celda = 5%)."""
    n = CANTIDAD_CELDAS_APTITUDES
    cantidad_aprobado = max(0, min(n, round(n * pct_aprobado / 100)))
    cantidad_mejorar = max(0, min(n - cantidad_aprobado, round(n * pct_mejorar / 100)))

    requests = []
    for i in range(1, n + 1):
        alt_text = f"APTITUDES_CELL_{i:02d}"
        forma = mapa_formas.get(alt_text)
        if not forma:
            print(f"AVISO: no encontré la celda '{alt_text}' en el master (revisar el alt text).")
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
    return requests


def actualizar_formas_nativas(servicio_slides, presentation_id, metricas):
    """Junta las barras y el pictograma en un solo batchUpdate. Los anillos del semáforo y
    de Acerca de NO se tocan acá — se refrescan aparte como gráficos vinculados (ver
    refrescar_graficos_slides en main.py, y el checklist del master)."""
    mapa_formas = mapear_formas_por_alt_text(servicio_slides, presentation_id)

    requests = []
    requests += requests_barra_tres_segmentos(
        mapa_formas, "BARRA_TITULAR_APROBADO", "BARRA_TITULAR_MEJORAR", "BARRA_TITULAR_NODETECTADO",
        metricas["TITULAR_APROBADO_PCT"], metricas["TITULAR_MEJORAR_PCT"],
    )
    requests += requests_barra_dos_segmentos(mapa_formas, "BARRA_URL_APROBADO", "BARRA_URL_MEJORAR", metricas["URL_APROBADO_PCT"])
    requests += requests_barra_tres_segmentos(
        mapa_formas, "BARRA_EXPERIENCIA_APROBADO", "BARRA_EXPERIENCIA_MEJORAR", "BARRA_EXPERIENCIA_NODETECTADO",
        metricas["EXPERIENCIA_APROBADO_PCT"], metricas["EXPERIENCIA_MEJORAR_PCT"],
    )
    requests += requests_barra_simple(mapa_formas, "TRACK_EDUCACION", "BARRA_EDUCACION_APROBADO", metricas["EDUCACION_APROBADO_PCT"])
    requests += requests_barra_tres_segmentos(
        mapa_formas, "BARRA_CERTIFICACIONES_APROBADO", "BARRA_CERTIFICACIONES_MEJORAR", "BARRA_CERTIFICACIONES_NODETECTADO",
        metricas["CERTIFICACIONES_APROBADO_PCT"], metricas["CERTIFICACIONES_MEJORAR_PCT"],
    )
    requests += requests_pictograma_aptitudes(mapa_formas, metricas["APTITUDES_APROBADO_PCT"], metricas["APTITUDES_MEJORAR_PCT"])

    if requests:
        servicio_slides.presentations().batchUpdate(presentationId=presentation_id, body={"requests": requests}).execute()


# ==============================================================================
# ORQUESTADOR
# ==============================================================================

def generar_presentacion_stats(
    creds,
    filas_del_dia,
    fecha_iso=None,
    titulo_presentacion="Análisis de perfiles de LinkedIn",
    subtitulo_presentacion="",
    id_master=None,
    carpeta_destino_id=None,
):
    """
    Punto de entrada pensado para llamarse desde ejecutar_pipeline() en main.py, al final
    de la corrida — DESPUÉS de que el resto del pipeline ya escribió "Estadísticas del Día"
    para hoy, ya con las filas del día leídas de la hoja de resultados.
    Devuelve (presentation_id, url_presentacion).
    """
    fecha_iso = fecha_iso or date.today().isoformat()
    id_master = id_master or os.environ["ID_PRESENTACION_MASTER_STATS"]
    carpeta_destino_id = carpeta_destino_id or os.environ["ID_CARPETA_PRESENTACIONES"]

    servicio_drive = construir_servicio_drive(creds)
    servicio_slides = build("slides", "v1", credentials=creds)

    metricas = calcular_metricas_del_dia(filas_del_dia)
    mapa_tokens_texto = formatear_tokens_texto(metricas, fecha_iso, titulo_presentacion, subtitulo_presentacion)

    nombre_archivo = f"Estadísticas DP — {fecha_iso}"
    presentation_id = duplicar_o_reemplazar_presentacion_del_dia(
        servicio_drive, id_master, carpeta_destino_id, nombre_archivo
    )

    reemplazar_tokens_presentacion(servicio_slides, presentation_id, mapa_tokens_texto)
    actualizar_formas_nativas(servicio_slides, presentation_id, metricas)

    # Semáforo y Acerca de: sus gráficos están vinculados a "Estadísticas del Día", que para
    # cuando esta función corre ya fue reescrita con los números de hoy por el resto del
    # pipeline (ver checklist del master) — así que acá solo hace falta refrescarlos.
    # Descomentar una vez que el master ya tenga los 2 gráficos vinculados insertados:
    # refrescar_graficos_slides() ya existe en main.py y encuentra sola todos los gráficos
    # vinculados de la presentación, no hace falta pasarle cuáles son.
    #
    # from main import refrescar_graficos_slides
    # refrescar_graficos_slides(servicio_slides, presentation_id)

    url_presentacion = f"https://docs.google.com/presentation/d/{presentation_id}/edit"
    print(f"Presentación generada: {url_presentacion}")
    return presentation_id, url_presentacion


# ==============================================================================
# PRUEBA STANDALONE (correr `python generar_presentacion_stats.py` a mano)
# ==============================================================================

if __name__ == "__main__":
    from main import autenticar_google_cli, construir_servicio_sheets

    creds = autenticar_google_cli()
    servicio_sheets = construir_servicio_sheets(creds)

    spreadsheet_id = os.environ["ID_SPREADSHEET"]
    hoja_de_prueba = "2026-09-23"  # hoja histórica con datos reales, para probar

    filas = leer_filas_hoja_como_diccionarios(servicio_sheets, spreadsheet_id, hoja_de_prueba)
    if not filas:
        raise SystemExit(f"No hay filas en la hoja '{hoja_de_prueba}' para calcular métricas.")
    print("Headers reales de la hoja:", list(filas[0].keys()))
    
    generar_presentacion_stats(
        creds,
        filas,
        titulo_presentacion="Análisis de perfiles — cohorte de prueba",
        subtitulo_presentacion="Corrida de prueba del generador de presentaciones.",
    )