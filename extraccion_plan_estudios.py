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
script cae en cascada a: (a) detección de tablas por líneas/bordes, y (b) reordenamiento con
Gemini sobre el texto plano. Si ninguno de los tres métodos da resultado, YA NO se guarda un
.txt vacío: se guarda el texto crudo sin ordenar con una advertencia al principio, para que
quede algo revisable a mano en vez de un archivo vacío silencioso.

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
# MÉTODO 2 (fallback legado): detección de tablas por líneas/bordes del PDF
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
# MÉTODO 3 (último recurso): reordenamiento del texto plano con IA
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
    with pdfplumber.open(ruta_pdf) as pdf:
        texto_plano = extraer_texto_plano(pdf)
        grilla = extraer_grilla_de_pdf(pdf)

        if not texto_plano.strip() and not grilla:
            print("❌ No se pudo extraer texto ni reconstruir ninguna grilla del PDF (¿está escaneado como imagen? En ese caso hace falta OCR, avisame).")
            sys.exit(1)

        partes_finales = []

        if grilla:
            total_materias = sum(len(m) for m in grilla.values())
            print(f"   [📐 Grilla reconstruida por posición: {total_materias} materias en {len(grilla)} columna(s)]")
            partes_finales.append(grilla_a_texto(grilla))
        else:
            print("   [ℹ️ No se pudo reconstruir la grilla por posición. Probando por líneas/bordes del PDF...]")
            tablas = extraer_tablas(pdf)
            if tablas:
                print(f"   [📊 Se detectaron {len(tablas)} tabla(s) de materias por año, vía líneas/bordes del PDF]")
                for tabla in tablas:
                    texto_tabla = tabla_a_texto(tabla)
                    if texto_tabla:
                        partes_finales.append(texto_tabla)
            if not partes_finales:
                print("   [ℹ️ Tampoco se detectó ninguna grilla por líneas/bordes. Probando reordenar el texto con IA...]")
                texto_reordenado = reordenar_con_ia(texto_plano)
                if texto_reordenado:
                    partes_finales.append(texto_reordenado.strip())
                    print("   [✨ Reordenamiento con IA completado]")

        # Red de seguridad: si ningún método anterior funcionó, no dejamos un .txt vacío en
        # silencio (que fue justamente el bug original) — guardamos el texto crudo con una
        # advertencia bien visible, para que quede algo revisable a mano.
        if not partes_finales:
            print("   [⚠️ Ningún método de reconstrucción funcionó. Se guarda el texto crudo como respaldo.]")
            partes_finales.append(
                "⚠️ NO SE PUDO RECONSTRUIR LA GRILLA AUTOMÁTICAMENTE — TEXTO CRUDO SIN ORDENAR, REVISAR A MANO.\n"
                "--------------------------------------------------------------------------------\n"
                + texto_plano.strip()
            )
        elif incluir_texto_crudo:
            partes_finales.append("--- TEXTO COMPLETO EXTRAÍDO (referencia / verificación) ---\n" + texto_plano.strip())

    texto_final = "\n\n".join(partes_finales)
    ruta_guardada = guardar_txt(texto_final, nombre_archivo)

    print(f"✅ Listo. Se guardó en: {ruta_guardada}")
    print(f"   ({len(texto_final)} caracteres)")
    print("\n⚠️  Revisalo antes de usarlo: fijate que no falte ninguna materia y que estén en el "
          "año correcto. Corré con --con-crudo si querés el texto completo sin procesar al "
          "final del archivo, como referencia para comparar.")


if __name__ == "__main__":
    main()