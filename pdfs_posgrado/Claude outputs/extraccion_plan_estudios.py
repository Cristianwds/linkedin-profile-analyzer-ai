"""
extraccion_plan_estudios.py

Utilidad standalone (no forma parte del pipeline principal) para convertir el PDF oficial de
un plan de estudios de UdeSA a texto plano (.txt), listo para guardarse en la carpeta
planes_de_estudio/ y que main.py lo inyecte como contexto al analizar perfiles de esa carrera.

Los planes de UdeSA vienen como una grilla de "materias por año" (columnas: Primer/Segundo/
Tercer/Cuarto/Quinto Año). Se probaron dos formas de reconstruir esa grilla por líneas/bordes
de pdfplumber (extract_tables) y las dos fallaron en la práctica según el plan:
  - Si el PDF no tiene líneas dibujadas alrededor de la grilla, no encuentra ninguna tabla.
  - Si tiene ALGUNAS líneas sueltas (no una grilla completa), extract_tables() puede devolver
    la grilla partida en varias tablas chiquitas (una por cada bloque de líneas), o con los
    encabezados de año en una fila que no es la primera del bloque detectado (por ejemplo,
    con el nombre de la carrera como fila 0 y los "AÑO" recién en la fila 1).
  - Cuando eso pasa, el script caía al reordenamiento con IA (Gemini) sobre el texto plano
    corrido de la página — pero ese texto no siempre respeta el orden visual real por
    columna/fila (pdfplumber puede alterar el orden de lectura en layouts con texto rotado),
    así que la IA terminaba mezclando materias de años distintos en un mismo bloque.

MÉTODO ACTUAL (por coordenadas, más confiable que ambos anteriores):
En vez de depender de líneas dibujadas o del orden de lectura de pdfplumber, se usa la
posición real (x, y) de cada palabra en la página:
  1) Se busca la fila de encabezados "<ORDINAL> AÑO" (ej. "PRIMER AÑO") por posición vertical,
     y se guarda el centro horizontal (x) de cada columna encontrada.
  2) Se recorta el cuerpo de la grilla entre esa fila y la primera aparición de "CICLO" debajo
     (las notas al pie de estos planes siempre arrancan con "CICLO DE FUNDAMENTOS..." /
     "CICLO DE ORIENTACIÓN..."), y se descartan las etiquetas de semestre rotadas 90°, que
     quedan invertidas al extraer texto (ej. "SEMESTRE" -> "ERTSEMES").
  3) Las palabras del cuerpo se agrupan en "filas de grilla" por altura: todas las columnas
     empiezan una materia nueva aproximadamente a la misma altura, así que un salto vertical
     chico entre palabras consecutivas indica que es el renglón siguiente de una materia con
     nombre largo (se cortó en 2-3 líneas), y un salto grande indica que empezó la fila
     siguiente de la grilla.
  4) Cada palabra de cada fila se asigna a la columna de año cuyo centro horizontal está más
     cerca, reconstruyendo así la lista de materias de cada año en orden.

Si esta reconstrucción por coordenadas no encuentra una fila de encabezados de año válida en
ninguna página (plan con un formato totalmente distinto, o PDF escaneado como imagen), el
script cae en cascada a: (a) reconstrucción por pilares/encabezados de posgrado, (b) otra
reconstrucción para una segunda familia de planes de posgrado con formato narrativo/viñetas
(ver ambas más abajo), (c) detección de tablas por líneas/bordes, y (d) reordenamiento con
Gemini sobre el texto plano. Si ninguno de los métodos da resultado, YA NO se guarda un .txt
vacío: se guarda el texto crudo sin ordenar con una advertencia al principio, para que quede
algo revisable a mano en vez de un archivo vacío silencioso.

MÉTODO 2 (posgrado): los planes de posgrado exportados desde Figma (MBA, EMBA, Maestría en
Finanzas) no vienen en grilla de años — vienen organizados por tamaño de fuente: un título de
pilar/etapa grande, debajo sub-encabezados tipo "Materias obligatorias" / "Materias electivas"
más chicos, y a veces un tercer nivel ("Electivas Data", "Electivas Finanzas") todavía más
chico, cada uno con su lista de materias debajo. Reconstruye esa jerarquía así:
  1) Agrupa las palabras en filas por altura, y separa filas de "encabezado" (con letra más
     grande que el cuerpo) de filas de "materias" (letra del tamaño más común de la página).
  2) A cada tamaño de letra de encabezado, en el orden en que aparece, le asigna un nivel de
     jerarquía relativo (1, 2, 3...) según su tamaño — nunca un tamaño de punto fijo, porque
     el mismo nivel lógico usa tamaños distintos entre plantillas (ej. los pilares de la EMBA
     son de 34pt, pero las etapas del MBA son de 24pt).
  3) El cuerpo de materias entre un encabezado y el siguiente puede venir en 1, 2 o 3 columnas
     visuales (por diagramación, no necesariamente una por encabezado): las columnas se
     detectan solas por salto horizontal, usando la posición donde EMPIEZA cada racimo de
     palabras contiguas de una fila (nunca la de cada palabra suelta — una línea larga
     envuelta en una columna puede terminar muy cerca del arranque de la columna siguiente, y
     asignar palabra por palabra fusiona ahí dos columnas reales en una sola).
  4) Cada palabra que el extractor de PDF marca con un salto de línea final (así vienen
     exportados los PDFs de Figma) cierra el ítem actual — es la señal más confiable de dónde
     termina una materia y empieza la siguiente, más confiable que la geometría sola. Si NINGUNA
     palabra de la página trae esa marca, este método se descarta de entrada (no es esta
     plantilla) y sigue la cascada al método narrativo.
Si esta reconstrucción no encuentra ningún encabezado con al menos una materia debajo en
ninguna página, se descarta (devuelve None) y sigue la cascada al método narrativo.

MÉTODO 3 (posgrado narrativo): otra familia de planes de posgrado (por ejemplo Maestría en
Ciencias del Comportamiento, Doctorado en Ciencias Aplicadas) que NO trae la marca de salto de
línea de Figma, y organiza el contenido en 2 columnas en vez de por tamaño de fuente puro: a la
izquierda los títulos de sección (letra grande) y a veces una descripción general en prosa; a
la derecha, opcionalmente agrupadas bajo una sub-etiqueta con ":" ("Primer cuatrimestre:",
"Employee Experience:"), las materias como viñetas "•". Reconstruye así:
  1) Separa cada fila en 2 bandas de columna (izquierda/derecha) por dónde EMPIEZA cada racimo
     de palabras contiguas — no por palabra suelta ni por la posición de la fila entera, porque
     un párrafo narrativo largo en la columna izquierda recorre casi todo el ancho de página y
     sus palabras del medio pueden caer más cerca del arranque de la columna derecha que de la
     propia (y un encabezado de la izquierda puede compartir la misma altura, o casi, que una
     viñeta de la derecha — agrupar en filas antes de separar columnas mezclaría ambas).
  2) En la columna izquierda: un renglón con letra más grande que el cuerpo es un título de
     sección (varios renglones seguidos sin nada entre medio son el mismo título partido en 2-3
     líneas); antes del primer título, un renglón de letra normal es la descripción general del
     programa; después de un título, se descarta (leyenda de bajo valor tipo "(materias
     obligatorias)").
  3) En la columna derecha: un renglón que arranca con "•" es una materia; uno que termina en
     ":" es una sub-etiqueta que agrupa las materias siguientes; cualquier otro es la
     continuación (envuelta en 2-3 líneas) del renglón anterior.
  4) Cada sub-etiqueta o materia de la columna derecha se asigna al título de sección más
     cercano verticalmente (no necesariamente el anterior: el primer ítem de una sección puede
     aparecer un poco más arriba que el título de esa sección).
Si esta reconstrucción no encuentra 2 columnas reales, o ningún título con al menos una materia
debajo, se descarta y sigue la cascada al método de tablas.

USO:
    python extraccion_plan_estudios.py "ruta/al/plan_oficial.pdf" "nombre_archivo_salida.txt"

Ejemplo:
    python extraccion_plan_estudios.py "Plan_Economia_2024.pdf" "economia.txt"

El segundo argumento tiene que ser EXACTAMENTE el mismo nombre que uses como "archivo_plan"
para esa carrera en el diccionario CARRERAS_UDESA de main.py.

Flags opcionales:
    --con-crudo   Agrega al final del .txt el texto completo extraído del PDF sin procesar,
                  como referencia para verificar manualmente que no falte ninguna materia.

Requiere: pdfplumber, python-dotenv, google-genai (ya están en requirements.txt del proyecto
principal) y una GEMINI_API_KEY válida en el .env para el reordenamiento con IA (opcional:
solo se usa como último recurso, si falla la reconstrucción por coordenadas y por líneas).
"""

import sys
import os
import re
from collections import Counter
import pdfplumber
from dotenv import load_dotenv

load_dotenv()
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

CARPETA_DESTINO = "planes_de_estudio"

INSTRUCCIONES_REORDENAMIENTO = """
Sos un asistente que ordena texto extraído de un PDF de un plan de estudios universitario
(UdeSA, Argentina). El texto de entrada viene de una grilla "materias por año" (columnas:
Primer Año, Segundo Año, etc.) que se extrajo línea por línea y quedó desordenada: materias de
distintos años pueden aparecer mezcladas en la misma línea del texto, y una materia cuyo
nombre ocupa más de una línea puede aparecer partida y ubicada en un lugar inesperado.

Tu tarea es reconstruir la lista real de materias de cada año/nivel que encuentres en el texto,
usando tu conocimiento de nombres típicos de materias universitarias para resolver la
ambigüedad cuando el texto no tiene ningún separador claro entre columnas.

Reglas:
- No inventes materias que no estén en el texto de entrada.
- Si el texto menciona el nombre de la carrera, incluilo como primera línea.
- Ignorá texto que sea claramente ruido de la extracción (por ejemplo, fragmentos con letras
  en orden invertido tipo "ERTSEMES" — son etiquetas rotadas de la página, no materias).
- Podés incluir al final, en una sola sección aparte, las notas al pie relevantes (asteriscos,
  aclaraciones sobre optativas, orientaciones) si las hay, resumidas brevemente.
- Devolvé ÚNICAMENTE el resultado en este formato, sin explicaciones tuyas ni comentarios:

<NOMBRE DE LA CARRERA>

<NOMBRE DEL AÑO O NIVEL, EN MAYÚSCULAS>
- <materia 1>
- <materia 2>
...

(repetir el bloque de año/nivel por cada uno que encuentres)

NOTAS
- <nota 1, si hay>
"""

# ==============================================================================
# MÉTODO 1 (principal): reconstrucción de la grilla por coordenadas de palabra
# ==============================================================================

# Etiquetas de semestre/año que aparecen rotadas 90° en el margen de estos planes y quedan
# invertidas al extraer el texto normalmente (ej. "SEMESTRE" -> "ERTSEMES", "PRIMER" ->
# "REMIRP", "SEGUNDO" -> "ODNUGES"). No son materias, hay que descartarlas explícitamente:
# como están muy cerca de la columna 1 en X, un filtro por posición horizontal las confunde
# con materias reales, así que se filtran por el texto exacto en vez de por geometría.
TOKENS_RUIDO_ROTADO = {"ERTSEMES", "REMIRP", "ODNUGES"}

# Separación vertical máxima (en puntos PDF) entre dos líneas consecutivas para considerarlas
# parte de la MISMA fila de la grilla (o sea, el renglón siguiente de una materia larga que se
# cortó en 2-3 líneas, no una fila nueva). Calibrado contra los planes reales: los saltos
# dentro de una misma materia rondan 2-11pt, y los saltos entre una fila y la siguiente rondan
# 18-45pt.
TOLERANCIA_MISMA_FILA = 15

# Separación horizontal máxima entre un ordinal ("PRIMER") y la palabra "AÑO" para
# considerarlos parte del mismo encabezado de columna ("PRIMER AÑO").
MARGEN_PAREJA_ORDINAL_ANIO = 45

# Tolerancia vertical para agrupar palabras del encabezado en una misma fila.
TOLERANCIA_FILA_ENCABEZADO = 6


def _localizar_encabezado_anios(palabras):
    """
    Busca la fila de encabezados de columna ("PRIMER AÑO", "SEGUNDO AÑO", ...) entre TODAS las
    palabras de la página, agrupando por posición vertical real (no por el orden de lectura de
    pdfplumber, que en estos PDFs puede venir alterado por el texto rotado). Devuelve
    (top_encabezado, columnas), donde columnas es una lista de {"nombre", "centro_x"} ordenada
    de izquierda a derecha, o (None, None) si no se encuentra ninguna fila con al menos 2
    columnas de año válidas.

    Si "AÑO" aparece más de una vez en la página (por ejemplo, también dentro de una nota al
    pie tipo "...DEL PRIMER AÑO O EN EL PRIMER SEMESTRE DEL SEGUNDO AÑO"), se prioriza la
    ocurrencia más arriba de la página: los encabezados reales de la grilla siempre están
    arriba de las notas al pie.
    """
    ocurrencias_anio = [p for p in palabras if p['text'].strip().upper() == 'AÑO']
    if not ocurrencias_anio:
        return None, None

    ocurrencias_anio.sort(key=lambda p: p['top'])
    filas_candidatas = []
    for palabra in ocurrencias_anio:
        if filas_candidatas and palabra['top'] - filas_candidatas[-1][-1]['top'] <= TOLERANCIA_FILA_ENCABEZADO:
            filas_candidatas[-1].append(palabra)
        else:
            filas_candidatas.append([palabra])

    for fila_anios in filas_candidatas:  # ya ordenadas de arriba hacia abajo
        top_fila = fila_anios[0]['top']
        palabras_en_fila = [p for p in palabras if abs(p['top'] - top_fila) <= TOLERANCIA_FILA_ENCABEZADO]

        columnas = []
        for palabra_anio in sorted(fila_anios, key=lambda p: p['x0']):
            candidatos_ordinal = [
                p for p in palabras_en_fila
                if p['x1'] <= palabra_anio['x0'] + 1
                and 0 <= palabra_anio['x0'] - p['x1'] < MARGEN_PAREJA_ORDINAL_ANIO
            ]
            if not candidatos_ordinal:
                continue  # "AÑO" suelto sin ordinal al lado: no es un encabezado de columna real
            ordinal = max(candidatos_ordinal, key=lambda p: p['x1'])
            columnas.append({
                "nombre": f"{ordinal['text'].upper()} AÑO",
                "centro_x": (ordinal['x0'] + palabra_anio['x1']) / 2,
            })

        if len(columnas) >= 2:
            return top_fila, columnas

    return None, None


def _localizar_fin_de_grilla(palabras, top_encabezado):
    """
    Las notas al pie de estos planes siempre arrancan con un título tipo "CICLO DE
    FUNDAMENTOS" / "CICLO DE ORIENTACIÓN". Usamos la primera aparición de "CICLO" debajo del
    encabezado como límite inferior de la grilla, para no arrastrar esas notas como si fueran
    materias. Si no aparece (algún plan con otro formato de notas), no se recorta nada.
    """
    candidatos = [p for p in palabras if p['top'] > top_encabezado and p['text'].strip().upper() == 'CICLO']
    return min((p['top'] for p in candidatos), default=None)


def _agrupar_en_filas_de_grilla(palabras_cuerpo):
    """
    Agrupa las palabras del cuerpo en "filas" reales de la grilla (una fila = un nivel de
    materias), sin importar que alguna se extienda en 2 o 3 líneas de texto.
    """
    palabras_ordenadas = sorted(palabras_cuerpo, key=lambda p: p['top'])
    filas = []
    for palabra in palabras_ordenadas:
        if filas and palabra['top'] - filas[-1][-1]['top'] <= TOLERANCIA_MISMA_FILA:
            filas[-1].append(palabra)
        else:
            filas.append([palabra])
    return filas


def _columna_mas_cercana(palabra, columnas):
    centro_palabra = (palabra['x0'] + palabra['x1']) / 2
    return min(columnas, key=lambda c: abs(c['centro_x'] - centro_palabra))


def extraer_grilla_por_coordenadas(pagina):
    """
    Reconstruye la grilla "materias por año" de una página usando la posición real de cada
    palabra, en vez de la detección de tablas por líneas de pdfplumber. Devuelve un diccionario
    {nombre_columna: [materias en orden]}, o None si no se encontró una fila de encabezados de
    año válida en esta página.
    """
    palabras = pagina.extract_words(use_text_flow=False, keep_blank_chars=False)

    top_encabezado, columnas = _localizar_encabezado_anios(palabras)
    if not columnas:
        return None

    top_fin = _localizar_fin_de_grilla(palabras, top_encabezado)

    palabras_cuerpo = [
        p for p in palabras
        if p['top'] > top_encabezado + TOLERANCIA_FILA_ENCABEZADO
        and (top_fin is None or p['top'] < top_fin)
        and p['text'].strip().upper() not in TOKENS_RUIDO_ROTADO
    ]

    materias_por_columna = {c['nombre']: [] for c in columnas}

    for fila in _agrupar_en_filas_de_grilla(palabras_cuerpo):
        celdas = {c['nombre']: [] for c in columnas}
        for palabra in sorted(fila, key=lambda p: (p['top'], p['x0'])):
            columna = _columna_mas_cercana(palabra, columnas)
            celdas[columna['nombre']].append(palabra['text'])
        for nombre_columna, palabras_celda in celdas.items():
            if palabras_celda:
                texto_materia = re.sub(r'\s+', ' ', ' '.join(palabras_celda)).strip()
                materias_por_columna[nombre_columna].append(texto_materia)

    return materias_por_columna


def grilla_a_texto(materias_por_columna):
    bloques = []
    for nombre_columna, materias in materias_por_columna.items():
        if not materias:
            continue
        lista = "\n".join(f"- {m}" for m in materias)
        bloques.append(f"{nombre_columna}\n{lista}")
    return "\n\n".join(bloques)


def extraer_grilla_de_pdf(pdf):
    """Recorre todas las páginas del PDF y combina las grillas encontradas en cada una (por si
    un plan largo se extiende a una segunda página), matcheando por nombre de columna."""
    grilla_combinada = {}
    alguna_pagina_con_grilla = False

    for pagina in pdf.pages:
        grilla_pagina = extraer_grilla_por_coordenadas(pagina)
        if not grilla_pagina:
            continue
        alguna_pagina_con_grilla = True
        for nombre_columna, materias in grilla_pagina.items():
            grilla_combinada.setdefault(nombre_columna, []).extend(materias)

    return grilla_combinada if alguna_pagina_con_grilla else None


# ==============================================================================
# MÉTODO 2: reconstrucción por pilares/encabezados (planes de posgrado)
# ==============================================================================

# Separación vertical máxima entre dos líneas consecutivas para considerarlas parte de la misma
# fila (o sea, el renglón siguiente de una materia larga cortada en 2-3 líneas).
TOLERANCIA_FILA_PILARES = 8

# Separación horizontal máxima entre dos palabras de encabezado para considerarlas parte del
# mismo título (ej. el número de pilar "02" y el texto "Visión de Negocios" al lado).
MARGEN_GRUPO_ENCABEZADO_PILARES = 80

# Separación horizontal máxima para agrupar los arranques de racimo en una misma columna visual
# del cuerpo.
MARGEN_COLUMNA_CUERPO_PILARES = 100

# Separación horizontal máxima DENTRO de una fila para considerar que dos palabras son del mismo
# racimo (mismo ítem/columna) en vez de pertenecer a columnas visuales distintas.
MARGEN_RACIMO_FILA_PILARES = 40


def _agrupar_filas_pilares(palabras):
    palabras = sorted(palabras, key=lambda p: p['top'])
    filas = []
    for p in palabras:
        if filas and p['top'] - filas[-1][-1]['top'] <= TOLERANCIA_FILA_PILARES:
            filas[-1].append(p)
        else:
            filas.append([p])
    return filas


def _agrupar_por_gap_x0_pilares(x0s, margen):
    """Agrupa una lista de x0 en bandas/columnas por salto horizontal, devolviendo el borde
    izquierdo (mínimo) de cada banda."""
    x0s = sorted(x0s)
    grupos = [[x0s[0]]]
    for x in x0s[1:]:
        if x - grupos[-1][-1] <= margen:
            grupos[-1].append(x)
        else:
            grupos.append([x])
    return [min(g) for g in grupos]


def _agrupar_palabras_en_racimos_pilares(fila_ordenada, margen=MARGEN_RACIMO_FILA_PILARES):
    """Parte una fila (ya ordenada por x0) en racimos de palabras contiguas: dos palabras caen
    en el mismo racimo solo si el hueco horizontal entre ellas es chico (texto del mismo ítem).
    Esto evita que una palabra de una línea envuelta (ej. el final de un ítem largo en la
    columna izquierda) quede más cerca en x0 de la columna derecha que de su propia columna y
    salte de racimo — el racimo entero se asigna a UNA sola columna según su primera palabra,
    nunca palabra por palabra."""
    racimos = [[fila_ordenada[0]]]
    for p in fila_ordenada[1:]:
        anterior = racimos[-1][-1]
        if p['x0'] - anterior['x1'] <= margen:
            racimos[-1].append(p)
        else:
            racimos.append([p])
    return racimos


def _reparar_letra_suelta_pilares(texto):
    """Corrige un glitch de extracción de fuente donde una sola letra mayúscula queda separada
    de la palabra que sigue (ej. 'H abilidades' -> 'Habilidades'). Es un patrón geométrico
    seguro: una palabra real de una sola letra mayúscula seguida de otra en minúscula casi nunca
    ocurre en este tipo de texto. (El caso inverso — una letra minúscula suelta al final de una
    palabra, ej. 'Innovació n' — se deja sin tocar a propósito: una regla que una una letra
    minúscula suelta corregiría ese glitch pero fusionaría por error la palabra "y" cada vez que
    aparece sola junto a otra palabra, que es muchísimo más común.)"""
    return re.sub(r'\b([A-ZÁÉÍÓÚÑ])\s(?=[a-záéíóúñ])', r'\1', texto)


def _limpiar_texto_item_pilares(palabras_texto):
    texto = re.sub(r'\s+', ' ', ' '.join(palabras_texto)).strip()
    return _reparar_letra_suelta_pilares(texto)


def _extraer_pilares_de_pagina(pagina):
    """
    Reconstruye la jerarquía pilar > sub-encabezado > materias de una página de plan de
    posgrado a partir del tamaño de letra y la posición de cada palabra. Devuelve
    (nombre_programa, secciones), donde secciones es una lista de
    {"encabezado", "nivel", "items"} en el orden en que aparecen en la página. Si la página no
    tiene ningún encabezado más grande que el cuerpo, o no tiene la firma de esta plantilla
    (ver más abajo), devuelve (None, []).
    """
    palabras = pagina.extract_words(use_text_flow=False, keep_blank_chars=False, extra_attrs=["size"])
    if not palabras:
        return None, []

    # Esta reconstrucción depende por completo de que cada palabra que cierra una materia venga
    # marcada con un salto de línea incorporado (así exportan los PDFs de Figma que usan las
    # plantillas de MBA/EMBA/Maestrías). Hay otra familia de planes de posgrado (con viñetas "•"
    # y texto narrativo, ver MÉTODO 3 más abajo) que NO trae esa marca — si no aparece ni una
    # sola vez en la página, esta reconstrucción no es aplicable y mejor no intentarla: sin esa
    # señal, esta lógica arma ítems mezclando texto de encabezados y de cuerpo sin darse cuenta.
    if not any(p['text'].endswith('\n') for p in palabras):
        return None, []

    tamanos = Counter(round(p['size'], 1) for p in palabras)
    tamano_cuerpo = tamanos.most_common(1)[0][0]
    tamano_max = max(p['size'] for p in palabras)
    umbral_encabezado = tamano_cuerpo + 1

    filas = _agrupar_filas_pilares(palabras)

    idx = 0
    if filas and any(p['size'] == tamano_max for p in filas[0]):
        idx = 1  # fila 0 = título de tapa ("Plan de Estudios"), no es parte del contenido

    nombre_programa = None
    if idx < len(filas) and any(p['size'] > umbral_encabezado for p in filas[idx]):
        nombre_programa = ' '.join(
            p['text'].strip().rstrip('\n') for p in sorted(filas[idx], key=lambda p: p['x0'])
        )
        idx += 1

    # Partimos el resto de las filas en BLOQUES: cada bloque = una fila de encabezado(s) + todas
    # las filas de cuerpo hasta el próximo encabezado (o el final de la página).
    bloques = []
    bloque_actual = None
    tamanos_encabezado_vistos = []

    for fila in filas[idx:]:
        es_encabezado = any(p['size'] > umbral_encabezado for p in fila)
        if es_encabezado:
            grupos_crudos = sorted(
                [p for p in fila if p['size'] > umbral_encabezado - 0.01],
                key=lambda p: p['x0']
            )
            grupos = []
            for p in grupos_crudos:
                if grupos and p['x0'] - grupos[-1][-1]['x1'] <= MARGEN_GRUPO_ENCABEZADO_PILARES:
                    grupos[-1].append(p)
                else:
                    grupos.append([p])
            encabezados = []
            for g in grupos:
                tam = max(pp['size'] for pp in g)
                if tam not in tamanos_encabezado_vistos:
                    tamanos_encabezado_vistos.append(tam)
                nivel = sorted(tamanos_encabezado_vistos, reverse=True).index(tam) + 1
                texto = _limpiar_texto_item_pilares([pp['text'].rstrip('\n') for pp in g])
                encabezados.append({"texto": texto, "x0": g[0]['x0'], "nivel": nivel})
            bloque_actual = {"encabezados": encabezados, "filas_cuerpo": []}
            bloques.append(bloque_actual)
        else:
            if bloque_actual is None:
                continue  # texto suelto antes del primer encabezado (subtítulo, etc.): se ignora
            bloque_actual["filas_cuerpo"].append(fila)

    # Para cada bloque, reconstruimos sus materias detectando las columnas del CUERPO por su
    # cuenta (no asumimos que coinciden 1 a 1 con la cantidad de encabezados del bloque).
    secciones = []
    for bloque in bloques:
        palabras_cuerpo = [p for fila in bloque["filas_cuerpo"] for p in fila]
        if not palabras_cuerpo:
            for enc in bloque["encabezados"]:
                secciones.append({"encabezado": enc["texto"], "nivel": enc["nivel"], "items": []})
            continue

        # Partimos cada fila en racimos ANTES de detectar columnas, y las columnas se calculan a
        # partir de dónde EMPIEZA cada racimo (nunca de la x0 de cada palabra suelta): ver
        # docstring de _agrupar_palabras_en_racimos_pilares.
        filas_con_racimos = []
        for fila in bloque["filas_cuerpo"]:
            fila_ordenada = sorted(fila, key=lambda p: p['x0'])
            filas_con_racimos.append(_agrupar_palabras_en_racimos_pilares(fila_ordenada))

        columnas_x0 = _agrupar_por_gap_x0_pilares(
            [racimo[0]['x0'] for racimos in filas_con_racimos for racimo in racimos],
            MARGEN_COLUMNA_CUERPO_PILARES
        )

        # Buffer independiente por columna, para que un ítem que se corta en 2-3 líneas en una
        # columna no se mezcle con lo que aparece en otra columna mientras tanto.
        items_por_columna = {x0: [] for x0 in columnas_x0}
        buffer_por_columna = {x0: [] for x0 in columnas_x0}

        for racimos in filas_con_racimos:
            for racimo in racimos:
                # Todo el racimo va a UNA columna (la de su primera palabra) — nunca se decide
                # palabra por palabra, que es lo que rompía ítems largos envueltos en 2 líneas.
                columna = min(columnas_x0, key=lambda c: abs(c - racimo[0]['x0']))
                for p in racimo:
                    texto_p = p['text']
                    termina_item = texto_p.endswith('\n')
                    texto_p = texto_p.rstrip('\n').strip()
                    if texto_p:
                        buffer_por_columna[columna].append(texto_p)
                    if termina_item and buffer_por_columna[columna]:
                        items_por_columna[columna].append(_limpiar_texto_item_pilares(buffer_por_columna[columna]))
                        buffer_por_columna[columna] = []
        for x0, buf in buffer_por_columna.items():
            if buf:
                items_por_columna[x0].append(_limpiar_texto_item_pilares(buf))

        encabezados = sorted(bloque["encabezados"], key=lambda e: e["x0"])
        if len(encabezados) == len(columnas_x0):
            # Coinciden 1 a 1: cada columna tiene su propio encabezado (ej. "Electivas Data" /
            # "Electivas Finanzas" lado a lado).
            for enc, x0 in zip(encabezados, columnas_x0):
                secciones.append({"encabezado": enc["texto"], "nivel": enc["nivel"], "items": items_por_columna[x0]})
        else:
            # No coinciden (ej. un solo "Materias obligatorias" con el cuerpo en 2-3 columnas
            # visuales solo por espacio): todas las columnas caen bajo el/los mismo(s)
            # encabezado(s), en orden de izquierda a derecha.
            items_combinados = []
            for x0 in columnas_x0:
                items_combinados.extend(items_por_columna[x0])
            if encabezados:
                for enc in encabezados:
                    secciones.append({"encabezado": enc["texto"], "nivel": enc["nivel"], "items": items_combinados})
            else:
                secciones.append({"encabezado": None, "nivel": 1, "items": items_combinados})

    return nombre_programa, secciones


def _formatear_pilares_a_texto(nombre_programa, secciones):
    """Arma el texto final tipo breadcrumb ('Pilar > Sub-encabezado > materias') a partir de las
    secciones de una página."""
    lineas = []
    if nombre_programa:
        lineas.append(nombre_programa)
        lineas.append("")

    pila = []  # [(nivel, texto)]
    for s in secciones:
        # El apilado/desapilado de la pila pasa SIEMPRE, tenga o no ítems propios esta sección —
        # un encabezado de pilar sin ítems directos (su contenido cuelga de un sub-encabezado)
        # igual tiene que reemplazar en la pila al pilar anterior; si nos salteamos esto cuando
        # no hay ítems, el pilar viejo nunca se desapila y queda pegado como prefijo de todo lo
        # que sigue en la página.
        while pila and pila[-1][0] >= s["nivel"]:
            pila.pop()
        pila.append((s["nivel"], s["encabezado"]))
        if not s["items"]:
            continue
        breadcrumb = " > ".join(t for _, t in pila) if s["encabezado"] else None
        if breadcrumb:
            lineas.append(breadcrumb)
        for it in s["items"]:
            lineas.append(f"- {it}")
        lineas.append("")

    return "\n".join(lineas).strip()


def extraer_pilares_de_pdf(pdf):
    """Recorre todas las páginas de un plan de posgrado y arma el texto final por página. Si
    ninguna página tuvo al menos un encabezado con una materia debajo, devuelve None (esta
    reconstrucción no aplica a este PDF, y extraer_texto_de_pdf sigue la cascada al método de
    tablas)."""
    bloques_texto = []
    for pagina in pdf.pages:
        nombre_programa, secciones = _extraer_pilares_de_pagina(pagina)
        if not any(s["items"] for s in secciones):
            continue
        texto_pagina = _formatear_pilares_a_texto(nombre_programa, secciones)
        if texto_pagina:
            bloques_texto.append(texto_pagina)

    return "\n\n".join(bloques_texto) if bloques_texto else None


# ==============================================================================
# MÉTODO 3: reconstrucción narrativa/viñetas (otra familia de planes de posgrado)
# ==============================================================================

# Separación horizontal máxima para agrupar los arranques de racimo en una misma banda de
# columna (columna de encabezados a la izquierda vs. columna de sub-etiquetas/viñetas a la
# derecha).
MARGEN_BANDA_COLUMNA_NARRATIVO = 100

# Separación horizontal máxima DENTRO de una fila para considerar que dos palabras son del mismo
# racimo, igual que en el método de pilares (ver docstring de _agrupar_palabras_en_racimos_pilares).
MARGEN_RACIMO_NARRATIVO = 40

# Separación vertical máxima entre dos filas de encabezado consecutivas (sin ninguna otra fila
# entre medio) para considerarlas el mismo título partido en 2-3 líneas, en vez de dos secciones
# distintas: calibrado contra el interlineado real de estos títulos (~27-28pt), con margen de
# sobra por debajo del salto real entre una tarjeta/sección y la siguiente (~100pt o más).
MARGEN_MISMO_TITULO_NARRATIVO = 45


def _agrupar_filas_narrativo(palabras):
    palabras = sorted(palabras, key=lambda p: p['top'])
    filas = []
    for p in palabras:
        if filas and p['top'] - filas[-1][-1]['top'] <= TOLERANCIA_FILA_PILARES:
            filas[-1].append(p)
        else:
            filas.append([p])
    return filas


def _agrupar_por_gap_narrativo(x0s, margen):
    x0s = sorted(x0s)
    grupos = [[x0s[0]]]
    for x in x0s[1:]:
        if x - grupos[-1][-1] <= margen:
            grupos[-1].append(x)
        else:
            grupos.append([x])
    return grupos


def _racimos_de_fila_narrativo(fila, margen=MARGEN_RACIMO_NARRATIVO):
    """Igual que _agrupar_palabras_en_racimos_pilares: el arranque de cada racimo es la única
    x0 que se usa para decidir a qué banda de columna pertenece un tramo de fila, nunca la x0 de
    cada palabra suelta. Acá hace más falta todavía que en el método de pilares: un párrafo
    narrativo largo en la banda izquierda (la descripción general del programa, por ejemplo)
    recorre casi todo el ancho de la página, y palabras del medio de esa oración pueden caer a
    solo unos puntos de donde arranca la banda derecha — decidir palabra por palabra partiría
    esa oración por la mitad."""
    palabras = sorted(fila, key=lambda p: p['x0'])
    racimos = [[palabras[0]]]
    for p in palabras[1:]:
        anterior = racimos[-1][-1]
        if p['x0'] - anterior['x1'] <= margen:
            racimos[-1].append(p)
        else:
            racimos.append([p])
    return racimos


def _texto_fila_narrativo(fila):
    return re.sub(r'\s+', ' ', ' '.join(p['text'].strip() for p in sorted(fila, key=lambda p: p['x0']))).strip()


def _extraer_narrativo_de_pagina(pagina):
    """
    Reconstruye la otra familia de planes de posgrado: sin la marca de salto de línea de Figma,
    organizados en dos columnas — a la izquierda los títulos de sección (letra grande) y a veces
    una descripción general en prosa; a la derecha, opcionalmente agrupadas bajo una sub-etiqueta
    ("Primer cuatrimestre:", "Employee Experience:"), las materias como viñetas "•". Devuelve
    (nombre_programa, descripcion, secciones), donde secciones es una lista de
    {"encabezado", "grupos": [{"sublabel", "items"}]}. Si la página no tiene esta estructura
    (sin encabezados en la banda izquierda, o sin viñetas/sub-etiquetas en la derecha), devuelve
    (None, None, []).
    """
    palabras = pagina.extract_words(use_text_flow=False, keep_blank_chars=False, extra_attrs=["size"])
    if not palabras:
        return None, None, []

    tamanos = Counter(round(p['size'], 1) for p in palabras)
    tamano_cuerpo = tamanos.most_common(1)[0][0]
    umbral = tamano_cuerpo + 1

    # Título de tapa + nombre del programa: las primeras 1-2 filas con letra más grande que el
    # cuerpo, agrupadas SIN separar por banda todavía (el título suele estar centrado en la
    # página, no pertenece a ninguna de las 2 bandas reales de contenido).
    filas_todas = _agrupar_filas_narrativo(palabras)
    idx = 0
    nombre_programa = None
    if filas_todas and any(p['size'] > umbral for p in filas_todas[0]):
        idx = 1
        if idx < len(filas_todas) and any(p['size'] > umbral for p in filas_todas[idx]):
            nombre_programa = _texto_fila_narrativo(filas_todas[idx])
            idx += 1

    top_corte = filas_todas[idx][0]['top'] if idx < len(filas_todas) else None
    palabras_resto = [p for p in palabras if top_corte is not None and p['top'] >= top_corte - 1]
    if not palabras_resto:
        return nombre_programa, None, []

    # Bandas de columna por el arranque de cada racimo (nunca por palabra suelta: ver docstring
    # de _racimos_de_fila_narrativo).
    filas_resto_agrupadas = _agrupar_filas_narrativo(palabras_resto)
    arranques_racimo = [
        racimo[0]['x0']
        for fila in filas_resto_agrupadas
        for racimo in _racimos_de_fila_narrativo(fila)
    ]
    bordes_banda = sorted(min(g) for g in _agrupar_por_gap_narrativo(arranques_racimo, MARGEN_BANDA_COLUMNA_NARRATIVO))
    if len(bordes_banda) < 2:
        return nombre_programa, None, []  # no hay 2 columnas reales: esta reconstrucción no aplica
    banda_izq, banda_der = bordes_banda[0], bordes_banda[-1]

    # Partimos cada fila en racimos y asignamos CADA RACIMO (nunca cada palabra) a la banda más
    # cercana, agrupando racimos consecutivos de la misma banda en una sub-fila.
    filas_izq, filas_der = [], []
    for fila in filas_resto_agrupadas:
        sub_fila_actual, banda_actual = None, None
        for racimo in _racimos_de_fila_narrativo(fila):
            b = min(bordes_banda, key=lambda x: abs(x - racimo[0]['x0']))
            if b == banda_actual:
                sub_fila_actual.extend(racimo)
            else:
                if sub_fila_actual is not None:
                    (filas_izq if banda_actual == banda_izq else filas_der).append(sub_fila_actual)
                sub_fila_actual, banda_actual = list(racimo), b
        if sub_fila_actual is not None:
            (filas_izq if banda_actual == banda_izq else filas_der).append(sub_fila_actual)

    # Columna izquierda: encabezados (letra grande, fusionando líneas consecutivas del mismo
    # título) y, antes del primer encabezado, una descripción general en prosa. La prosa que
    # aparece DESPUÉS de un encabezado (ej. la leyenda "(materias obligatorias)") se descarta:
    # es de bajo valor informativo frente al riesgo de confundirla con una materia real.
    headers = []
    descripcion = []
    for f in filas_izq:
        if any(p['size'] > umbral for p in f):
            top_f = f[0]['top']
            if headers and top_f - headers[-1]["top_ultima_linea"] <= MARGEN_MISMO_TITULO_NARRATIVO:
                headers[-1]["texto"] += " " + _texto_fila_narrativo(f)
                headers[-1]["top_ultima_linea"] = top_f
            else:
                headers.append({"top": top_f, "top_ultima_linea": top_f, "texto": _texto_fila_narrativo(f)})
        elif not headers:
            descripcion.append(_texto_fila_narrativo(f))
    texto_descripcion = " ".join(descripcion) if descripcion else None

    # Columna derecha: sub-etiquetas ("Primer cuatrimestre:"), ítems con viñeta ("• Materia..."),
    # o continuaciones (el renglón siguiente de un ítem/etiqueta larga, sin viñeta ni ":").
    filas_der_clasificadas = []
    for f in filas_der:
        texto = _texto_fila_narrativo(f)
        primera_palabra = sorted(f, key=lambda p: p['x0'])[0]['text'].strip()
        if primera_palabra == '•':
            filas_der_clasificadas.append({
                "top": f[0]['top'], "tipo": "item", "texto": re.sub(r'^•\s*', '', texto).strip()
            })
        elif texto.endswith(':'):
            filas_der_clasificadas.append({"top": f[0]['top'], "tipo": "sublabel", "texto": texto})
        else:
            filas_der_clasificadas.append({"top": f[0]['top'], "tipo": "cont", "texto": texto})

    fusionadas = []
    for f in filas_der_clasificadas:
        if f["tipo"] == "cont" and fusionadas:
            fusionadas[-1]["texto"] += " " + f["texto"]
        else:
            fusionadas.append(dict(f))

    if not headers or not fusionadas:
        return nombre_programa, texto_descripcion, []

    # Cada sub-etiqueta o ítem de la derecha se asigna al encabezado de la izquierda más cercano
    # verticalmente (no necesariamente el anterior: a veces el primer ítem de una sección
    # aparece un poco más arriba que el título de esa sección).
    for f in fusionadas:
        f["header"] = min(headers, key=lambda h: abs(h["top"] - f["top"]))["texto"]

    resultado = {h["texto"]: [] for h in headers}
    grupo_actual = {h["texto"]: None for h in headers}
    for f in fusionadas:
        h = f["header"]
        if f["tipo"] == "sublabel":
            grupo_actual[h] = {"sublabel": f["texto"], "items": []}
            resultado[h].append(grupo_actual[h])
        else:
            if grupo_actual[h] is None:
                grupo_actual[h] = {"sublabel": None, "items": []}
                resultado[h].append(grupo_actual[h])
            grupo_actual[h]["items"].append(f["texto"])

    secciones = []
    for h in headers:
        grupos = [g for g in resultado[h["texto"]] if g["items"]]
        if grupos:
            secciones.append({"encabezado": h["texto"], "grupos": grupos})

    return nombre_programa, texto_descripcion, secciones


def _formatear_narrativo_a_texto(nombre_programa, descripcion, secciones):
    lineas = []
    if nombre_programa:
        lineas.append(nombre_programa)
    if descripcion:
        lineas.append(descripcion)
    if nombre_programa or descripcion:
        lineas.append("")
    for s in secciones:
        for g in s["grupos"]:
            lineas.append(f'{s["encabezado"]} > {g["sublabel"]}' if g["sublabel"] else s["encabezado"])
            for it in g["items"]:
                lineas.append(f"- {it}")
            lineas.append("")
    return "\n".join(lineas).strip()


def extraer_narrativo_de_pdf(pdf):
    """Recorre todas las páginas con la reconstrucción narrativa/viñetas y arma el texto final
    por página. Si ninguna página tuvo al menos un encabezado con una materia debajo, devuelve
    None (esta reconstrucción tampoco aplica a este PDF, y extraer_texto_de_pdf sigue la cascada
    al método de tablas)."""
    bloques_texto = []
    for pagina in pdf.pages:
        nombre_programa, descripcion, secciones = _extraer_narrativo_de_pagina(pagina)
        if not any(s["grupos"] for s in secciones):
            continue
        texto_pagina = _formatear_narrativo_a_texto(nombre_programa, descripcion, secciones)
        if texto_pagina:
            bloques_texto.append(texto_pagina)

    return "\n\n".join(bloques_texto) if bloques_texto else None


# ==============================================================================
# MÉTODO 4 (fallback legado): detección de tablas por líneas/bordes del PDF
# ==============================================================================

def extraer_texto_plano(pdf):
    """Texto corrido de todas las páginas, en el orden de lectura de pdfplumber."""
    texto_completo = ""
    for pagina in pdf.pages:
        texto_pagina = pagina.extract_text()
        if texto_pagina:
            texto_completo += texto_pagina + "\n"
    return texto_completo


def _es_tabla_de_anios(tabla):
    """
    Valida que una tabla detectada por pdfplumber realmente sea (una porción de) la grilla de
    materias por año, buscando la fila de encabezados en CUALQUIER posición de la tabla (no
    asume que sea la fila 0: algunos planes traen el nombre de la carrera como primera fila,
    con los encabezados de año recién en la fila 1).
    """
    for fila in tabla:
        celdas = [(c or "") for c in fila]
        if len(celdas) < 3:
            continue
        cantidad_con_anio = sum(1 for c in celdas if 'año' in c.lower())
        if cantidad_con_anio >= max(2, len(celdas) // 2):
            return True
    return False


def extraer_tablas(pdf):
    """Intenta reconstruir la grilla de materias por año vía detección de líneas/bordes de
    pdfplumber, descartando cualquier tabla que no tenga pinta real de ser esa grilla."""
    tablas_encontradas = []
    for pagina in pdf.pages:
        for tabla in (pagina.extract_tables() or []):
            if _es_tabla_de_anios(tabla):
                tablas_encontradas.append(tabla)
    return tablas_encontradas


def _limpiar_celda(celda):
    """Colapsa saltos de línea internos de una celda en un solo espacio, y normaliza espacios."""
    texto = (celda or "").replace("\n", " ")
    return re.sub(r"\s+", " ", texto).strip()


def combinar_filas_partidas(tabla):
    """Red de seguridad: junta una fila 'huérfana' (con solo alguna columna completa) con la
    fila anterior, para el caso borde en que una materia partida aparezca como fila aparte."""
    filas_limpias = []
    for fila_cruda in tabla:
        fila = [_limpiar_celda(celda) for celda in fila_cruda]
        columnas_con_texto = sum(1 for celda in fila if celda)

        if filas_limpias and 0 < columnas_con_texto < len(fila):
            fila_anterior = filas_limpias[-1]
            for i, celda in enumerate(fila):
                if celda:
                    fila_anterior[i] = f"{fila_anterior[i]} {celda}".strip() if fila_anterior[i] else celda
        else:
            filas_limpias.append(fila)

    return filas_limpias


def tabla_a_texto(tabla):
    """Convierte una tabla ya limpia en texto tipo 'AÑO: lista de materias'. Encuentra la fila
    de encabezados en cualquier posición (ver _es_tabla_de_anios) en vez de asumir que es la
    fila 0."""
    filas = combinar_filas_partidas(tabla)
    if len(filas) < 2:
        return ""

    indice_encabezado = None
    for i, fila in enumerate(filas):
        cantidad_con_anio = sum(1 for c in fila if 'año' in (c or '').lower())
        if cantidad_con_anio >= max(2, len(fila) // 2):
            indice_encabezado = i
            break
    if indice_encabezado is None:
        return ""

    encabezados = filas[indice_encabezado]
    filas_materias = filas[indice_encabezado + 1:]
    num_columnas = len(encabezados)
    materias_por_columna = [[] for _ in range(num_columnas)]

    for fila in filas_materias:
        for i in range(min(num_columnas, len(fila))):
            if fila[i]:
                materias_por_columna[i].append(fila[i])

    bloques = []
    for i, encabezado in enumerate(encabezados):
        if not encabezado or not materias_por_columna[i]:
            continue
        materias = "\n".join(f"- {m}" for m in materias_por_columna[i])
        bloques.append(f"{encabezado.upper()}\n{materias}")

    return "\n\n".join(bloques)


# ==============================================================================
# MÉTODO 5 (último recurso): reordenamiento del texto plano con IA
# ==============================================================================

def reordenar_con_ia(texto_crudo):
    """
    Le pide a Gemini que reordene la grilla de materias a partir del texto ya extraído del PDF.
    Solo se usa si fallaron tanto la reconstrucción por coordenadas como la detección de tablas
    por líneas: el texto plano corrido no siempre respeta el orden visual real de la grilla, así
    que este método es el menos confiable de los tres.
    """
    if not GEMINI_API_KEY:
        print("   [ℹ️ No hay GEMINI_API_KEY en el .env; se omite el reordenamiento con IA.]")
        return None

    try:
        from google import genai
        from google.genai import types

        cliente = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"{INSTRUCCIONES_REORDENAMIENTO}\n\nTEXTO EXTRAÍDO DEL PDF:\n{texto_crudo}"
        respuesta = cliente.models.generate_content(
            model='gemini-flash-latest',
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1)
        )
        return respuesta.text
    except Exception as e:
        print(f"   [⚠️ No se pudo reordenar con IA: {e}]")
        return None


# ==============================================================================
# GUARDADO Y CLI
# ==============================================================================

def guardar_txt(texto, nombre_archivo):
    os.makedirs(CARPETA_DESTINO, exist_ok=True)
    ruta_salida = os.path.join(CARPETA_DESTINO, nombre_archivo)
    with open(ruta_salida, 'w', encoding='utf-8') as f:
        f.write(texto)
    return ruta_salida


def extraer_texto_de_pdf(ruta_pdf, incluir_texto_crudo=False, callback_aviso=None):
    """
    Corazón reutilizable del script: toma la ruta a un PDF de plan de estudios y devuelve
    (texto_final, metodo_usado, avisos), sin tocar la consola ni el disco. Se separó del main()
    de CLI para que tanto la terminal como el backend web (subida de un plan desde la web para
    crear/actualizar una carrera) usen exactamente la misma lógica de extracción.

    metodo_usado: uno de "grilla", "posgrado", "posgrado-narrativo", "tablas", "ia", "crudo" —
    útil para que quien llame (la web, por ejemplo) le muestre al usuario qué tan confiable
    salió la extracción (el método "crudo" significa que ningún método automático funcionó y
    hace falta revisión manual).

    callback_aviso, si se pasa, es una función callback_aviso(mensaje: str) que recibe los mismos
    avisos que el CLI imprime por consola, para que el caller (ej. el backend web) los pueda
    reenviar como parte del progreso en vivo. Si no se pasa, no se imprime ni reenvía nada (a
    diferencia del CLI, que sigue usando print() directo en su propio wrapper más abajo).

    Lanza ValueError si el PDF no tiene ni texto ni grilla extraíble (PDF escaneado como imagen).
    """
    def _avisar(mensaje):
        if callback_aviso:
            callback_aviso(mensaje)

    with pdfplumber.open(ruta_pdf) as pdf:
        texto_plano = extraer_texto_plano(pdf)
        grilla = extraer_grilla_de_pdf(pdf)

        if not texto_plano.strip() and not grilla:
            raise ValueError(
                "No se pudo extraer texto ni reconstruir ninguna grilla del PDF "
                "(¿está escaneado como imagen? En ese caso hace falta OCR)."
            )

        partes_finales = []
        metodo_usado = None

        if grilla:
            total_materias = sum(len(m) for m in grilla.values())
            _avisar(f"📐 Grilla reconstruida por posición: {total_materias} materias en {len(grilla)} columna(s)")
            partes_finales.append(grilla_a_texto(grilla))
            metodo_usado = "grilla"
        else:
            _avisar("ℹ️ No se pudo reconstruir la grilla por posición. Probando por pilares (planes de posgrado)...")
            texto_pilares = extraer_pilares_de_pdf(pdf)
            if texto_pilares:
                _avisar("🏛️ Plan reconstruido por pilares/encabezados de posgrado")
                partes_finales.append(texto_pilares)
                metodo_usado = "posgrado"

        if not partes_finales:
            _avisar("ℹ️ Tampoco se pudo reconstruir por pilares. Probando reconstrucción narrativa (otra plantilla de posgrado)...")
            texto_narrativo = extraer_narrativo_de_pdf(pdf)
            if texto_narrativo:
                _avisar("📝 Plan reconstruido por la reconstrucción narrativa/viñetas de posgrado")
                partes_finales.append(texto_narrativo)
                metodo_usado = "posgrado-narrativo"

        if not partes_finales:
            _avisar("ℹ️ Tampoco se pudo reconstruir de forma narrativa. Probando por líneas/bordes del PDF...")
            tablas = extraer_tablas(pdf)
            if tablas:
                _avisar(f"📊 Se detectaron {len(tablas)} tabla(s) de materias por año, vía líneas/bordes del PDF")
                for tabla in tablas:
                    texto_tabla = tabla_a_texto(tabla)
                    if texto_tabla:
                        partes_finales.append(texto_tabla)
                if partes_finales:
                    metodo_usado = "tablas"
            if not partes_finales:
                _avisar("ℹ️ Tampoco se detectó ninguna grilla por líneas/bordes. Probando reordenar el texto con IA...")
                texto_reordenado = reordenar_con_ia(texto_plano)
                if texto_reordenado:
                    partes_finales.append(texto_reordenado.strip())
                    metodo_usado = "ia"
                    _avisar("✨ Reordenamiento con IA completado")

        # Red de seguridad: si ningún método anterior funcionó, no dejamos un .txt vacío en
        # silencio (que fue justamente el bug original) — guardamos el texto crudo con una
        # advertencia bien visible, para que quede algo revisable a mano.
        if not partes_finales:
            _avisar("⚠️ Ningún método de reconstrucción funcionó. Se guarda el texto crudo como respaldo.")
            metodo_usado = "crudo"
            partes_finales.append(
                "⚠️ NO SE PUDO RECONSTRUIR LA GRILLA AUTOMÁTICAMENTE — TEXTO CRUDO SIN ORDENAR, REVISAR A MANO.\n"
                "--------------------------------------------------------------------------------\n"
                + texto_plano.strip()
            )
        elif incluir_texto_crudo:
            partes_finales.append("--- TEXTO COMPLETO EXTRAÍDO (referencia / verificación) ---\n" + texto_plano.strip())

    return "\n\n".join(partes_finales), metodo_usado


def main():
    incluir_texto_crudo = '--con-crudo' in sys.argv
    argumentos = [a for a in sys.argv[1:] if a != '--con-crudo']

    if len(argumentos) != 2:
        print("Uso: python extraccion_plan_estudios.py <ruta_al_pdf> <nombre_archivo_salida.txt> [--con-crudo]")
        print('Ejemplo: python extraccion_plan_estudios.py "Plan_Economia_2024.pdf" "economia.txt"')
        sys.exit(1)

    ruta_pdf, nombre_archivo = argumentos
    if not nombre_archivo.lower().endswith('.txt'):
        nombre_archivo += '.txt'

    if not os.path.exists(ruta_pdf):
        print(f"❌ No se encontró el archivo: {ruta_pdf}")
        sys.exit(1)

    print(f"📄 Procesando: {ruta_pdf}")
    try:
        texto_final, metodo_usado = extraer_texto_de_pdf(
            ruta_pdf, incluir_texto_crudo=incluir_texto_crudo, callback_aviso=lambda m: print(f"   [{m}]")
        )
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    ruta_guardada = guardar_txt(texto_final, nombre_archivo)

    print(f"✅ Listo. Se guardó en: {ruta_guardada} (método: {metodo_usado})")
    print(f"   ({len(texto_final)} caracteres)")
    print("\n⚠️  Revisalo antes de usarlo: fijate que no falte ninguna materia y que estén en el "
          "año correcto. Corré con --con-crudo si querés el texto completo sin procesar al "
          "final del archivo, como referencia para comparar.")


if __name__ == "__main__":
    main()