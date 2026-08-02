"""
extraccion_plan_estudios.py

Utilidad standalone (no forma parte del pipeline principal) para convertir el PDF oficial de
un plan de estudios de UdeSA a texto plano (.txt), listo para guardarse en la carpeta
planes_de_estudio/ y que main.py lo inyecte como contexto al analizar perfiles de esa carrera.

Los planes de UdeSA vienen como una grilla de "materias por año" (columnas: Primer/Segundo/
Tercer/Cuarto Año). Se probaron dos formas de reconstruir esa grilla automáticamente por
geometría del PDF, y las dos fallaron en la práctica:
  1) Detección de tabla por posición de texto ("text" strategy de pdfplumber): corta palabras
     a la mitad en layouts irregulares.
  2) Detección de tabla por líneas/bordes: en los planes reales de UdeSA no hay líneas
     dibujadas alrededor de la grilla, así que no la encuentra (o peor, engancha algún otro
     recuadro decorativo de la página como si fuera la tabla real).

Por eso el flujo real de este script es:
  1) Intenta la detección por líneas (funciona SI el PDF tiene líneas reales; se descarta
     cualquier "tabla" que no tenga pinta de ser la grilla de años, para evitar falsos
     positivos).
  2) Si no encuentra una tabla válida, le pide a Gemini que reordene el texto extraído en la
     lista de materias por año. Un modelo de lenguaje es mejor para este problema que la
     heurística geométrica: entiende que "Economía I Microeconomía I..." son nombres de
     materias distintos aunque no haya ningún separador en el texto, cosa que una regla
     posicional no puede inferir sin saber dónde truena cada columna.
  3) Siempre guarda también el texto plano completo, como respaldo por si el paso 2 no está
     disponible (sin GEMINI_API_KEY) o el resultado necesita corrección manual.

USO:
    python extraccion_plan_estudios.py "ruta/al/plan_oficial.pdf" "nombre_archivo_salida.txt"

Ejemplo:
    python extraccion_plan_estudios.py "Plan_Economia_2024.pdf" "economia.txt"

El segundo argumento tiene que ser EXACTAMENTE el mismo nombre que uses como "archivo_plan"
para esa carrera en el diccionario CARRERAS_UDESA de main.py.

Requiere: pdfplumber, python-dotenv, google-genai (ya están en requirements.txt del proyecto
principal) y una GEMINI_API_KEY válida en el .env para el paso 2 (opcional: sin la key, el
script sigue funcionando, solo se salta el reordenamiento con IA).
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
    Valida que una tabla detectada por pdfplumber realmente sea la grilla de materias por año,
    y no algún otro recuadro/borde decorativo de la página que se coló como falso positivo.
    Exige que al menos la mitad de los encabezados de la primera fila contengan la palabra
    'año'.
    """
    if not tabla or len(tabla[0]) < 3:
        return False
    encabezados = [(c or "") for c in tabla[0]]
    cantidad_con_anio = sum(1 for c in encabezados if 'año' in c.lower())
    return cantidad_con_anio >= max(2, len(encabezados) // 2)


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
    """Convierte una tabla ya limpia en texto tipo 'AÑO: lista de materias'."""
    filas = combinar_filas_partidas(tabla)
    if len(filas) < 2:
        return ""

    encabezados = filas[0]
    filas_materias = filas[1:]
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


def reordenar_con_ia(texto_crudo):
    """
    Le pide a Gemini que reordene la grilla de materias a partir del texto ya extraído del PDF.
    Es un modelo de lenguaje el que mejor puede resolver la ambigüedad de dónde termina una
    materia y empieza la siguiente cuando no hay ningún separador en el texto (algo que un
    regex o una heurística de posición no puede inferir sin datos geométricos confiables).
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
        tablas = extraer_tablas(pdf)

    if not texto_plano.strip() and not tablas:
        print("❌ No se pudo extraer texto ni tablas del PDF (¿está escaneado como imagen? En ese caso hace falta OCR, avisame).")
        sys.exit(1)

    partes_finales = []

    if tablas:
        print(f"   [📊 Se detectaron {len(tablas)} tabla(s) de materias por año, vía líneas/bordes del PDF]")
        for tabla in tablas:
            texto_tabla = tabla_a_texto(tabla)
            if texto_tabla:
                partes_finales.append(texto_tabla)
    else:
        print("   [ℹ️ No se detectó ninguna grilla por líneas/bordes. Probando reordenar el texto con IA...]")
        texto_reordenado = reordenar_con_ia(texto_plano)
        if texto_reordenado:
            partes_finales.append(texto_reordenado.strip())
            print("   [✨ Reordenamiento con IA completado]")

    if incluir_texto_crudo:
        partes_finales.append("--- TEXTO COMPLETO EXTRAÍDO (referencia / verificación) ---\n" + texto_plano.strip())

    texto_final = "\n\n".join(partes_finales)
    ruta_guardada = guardar_txt(texto_final, nombre_archivo)

    print(f"✅ Listo. Se guardó en: {ruta_guardada}")
    print(f"   ({len(texto_final)} caracteres)")
    print("\n⚠️  Revisalo antes de usarlo, comparando contra el 'TEXTO COMPLETO EXTRAÍDO' de más "
          "abajo: el reordenamiento con IA es una ayuda para no armarlo 100% a mano, no una "
          "garantía. Fijate que no falte ninguna materia y que estén en el año correcto.")


if __name__ == "__main__":
    main()