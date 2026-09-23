"""
lote_extraccion_planes.py

Utilidad standalone para procesar una CARPETA ENTERA de PDFs de planes de estudio de una sola
corrida, reusando exactamente la misma lógica de extraccion_plan_estudios.py (grilla por año
para grado → pilares numerados para posgrado → tablas por líneas → reordenamiento con IA como
último recurso) para cada archivo.

Pensado para la carga inicial de muchas carreras de golpe (por ejemplo, todo el catálogo de
posgrados) sin tener que correr extraccion_plan_estudios.py a mano, uno por uno — y también para
correrlo de nuevo cada vez que se agregan PDFs nuevos a la carpeta: por default, el script SALTA
los PDFs cuyo .txt de salida ya existe en planes_de_estudio/ (es decir, ya fueron extraídos en
una corrida anterior) y solo procesa los que faltan. Si un PDF puntual necesita reprocesarse
(por ejemplo, porque salió con el método "crudo" y se quiere reintentar después de algún ajuste),
basta con borrar su .txt de planes_de_estudio/ a mano, o correr con --forzar para reprocesar
absolutamente todo.

No crea ni modifica carreras_posgrado.json / carreras_grado.json — eso se arma aparte, a mano
o con ayuda, una vez que revisaste los .txt generados. Este script SOLO genera los .txt en
planes_de_estudio/ y un manifiesto (manifiesto_planes.json) con el resultado de cada archivo,
para que sepas de un vistazo cuáles conviene revisar antes de confiar en ellos. El manifiesto se
actualiza incrementalmente entre corridas: los PDFs saltados conservan la entrada de la corrida
en la que efectivamente se procesaron, no se pierden ni se vuelven a listar como "sin procesar".

USO:
    python lote_extraccion_planes.py "ruta/a/la/carpeta/con/pdfs"

Ejemplo:
    python lote_extraccion_planes.py "pdfs_posgrado"

Flags opcionales:
    --con-crudo   Igual que en extraccion_plan_estudios.py: agrega al final de cada .txt el
                  texto completo extraído del PDF sin procesar, como referencia.
    --forzar      Reprocesa TODOS los PDFs de la carpeta, incluso los que ya tienen un .txt
                  guardado de una corrida anterior (por default esos se saltan).

El nombre de archivo .txt de salida se deriva del nombre del PDF (minúsculas, sin tildes,
espacios y símbolos -> guion bajo), NO del nombre real de la carrera, porque el script no lo
conoce (y es justamente ese nombre derivado el que se usa para decidir si un PDF ya fue
extraído). Por eso el manifiesto incluye una columna "nombre_sugerido" (el nombre del PDF
prolijado, con mayúsculas), para que la revises y corrijas a mano antes de armar
carreras_posgrado.json.
"""

import sys
import os
import re
import json
import unicodedata

import extraccion_plan_estudios as extractor

MANIFIESTO = "manifiesto_planes.json"


def _normalizar_para_slug(texto):
    texto = texto.lower()
    sin_tildes = unicodedata.normalize('NFKD', texto)
    return ''.join(c for c in sin_tildes if not unicodedata.combining(c))


def _slug_desde_nombre_pdf(nombre_pdf):
    """'Maestría en Finanzas (2024).pdf' -> 'maestria_en_finanzas_2024.txt'"""
    base = os.path.splitext(nombre_pdf)[0]
    base = _normalizar_para_slug(base)
    base = re.sub(r'[^a-z0-9]+', '_', base).strip('_')
    return f"{base}.txt"


def _nombre_sugerido_desde_pdf(nombre_pdf):
    """'maestria_en_finanzas_2024.pdf' -> 'Maestria En Finanzas 2024' (punto de partida para
    que vos le pongas el nombre real y bien escrito de la carrera, con tildes)."""
    base = os.path.splitext(nombre_pdf)[0]
    base = re.sub(r'[_\-]+', ' ', base).strip()
    return base.title()


def _cargar_manifiesto_anterior():
    """Lee manifiesto_planes.json si ya existe de una corrida previa, indexado por nombre de PDF
    original, para poder mezclarlo con los resultados de esta corrida sin perder lo ya hecho. Si
    no existe o está corrupto, arranca de un manifiesto vacío (no es un error fatal: la corrida
    igual puede seguir, simplemente no hay historial previo que conservar)."""
    if not os.path.exists(MANIFIESTO):
        return {}
    try:
        with open(MANIFIESTO, 'r', encoding='utf-8') as f:
            entradas = json.load(f)
        return {entrada["pdf_original"]: entrada for entrada in entradas if entrada.get("pdf_original")}
    except Exception as e:
        print(f"⚠️ No se pudo leer '{MANIFIESTO}' de una corrida anterior, se arranca de cero: {e}")
        return {}


def _guardar_manifiesto(manifiesto_por_pdf):
    entradas = sorted(manifiesto_por_pdf.values(), key=lambda e: e["pdf_original"])
    with open(MANIFIESTO, 'w', encoding='utf-8') as f:
        json.dump(entradas, f, ensure_ascii=False, indent=2)


def procesar_un_pdf(ruta_pdf, nombre_pdf, incluir_texto_crudo=False, callback_aviso=None):
    """
    Extrae el texto de UN PDF de plan de estudios y arma el diccionario de resultado que
    comparten tanto procesar_carpeta() de acá abajo (que además lo guarda como .txt suelto en
    planes_de_estudio/ y lo anota en manifiesto_planes.json) como el endpoint de carga por lote
    de la web en web_app.py (que en cambio deja el texto en memoria para que la persona confirme
    a mano a qué carrera corresponde, antes de guardar nada definitivo). Esta función NO persiste
    nada por su cuenta — solo extrae y arma el resultado, para que cada uno de los dos usos
    decida qué hacer con él.
    """
    archivo_txt = _slug_desde_nombre_pdf(nombre_pdf)
    nombre_sugerido = _nombre_sugerido_desde_pdf(nombre_pdf)
    try:
        texto_final, metodo_usado = extractor.extraer_texto_de_pdf(
            ruta_pdf, incluir_texto_crudo=incluir_texto_crudo, callback_aviso=callback_aviso
        )
    except ValueError as e:
        return {
            "pdf_original": nombre_pdf,
            "archivo_txt": None,
            "nombre_sugerido": nombre_sugerido,
            "texto": None,
            "metodo_usado": "error",
            "caracteres": 0,
            "requiere_revision_manual": True,
            "error": str(e),
        }

    return {
        "pdf_original": nombre_pdf,
        "archivo_txt": archivo_txt,
        "nombre_sugerido": nombre_sugerido,
        "texto": texto_final,
        "metodo_usado": metodo_usado,
        "caracteres": len(texto_final),
        "requiere_revision_manual": metodo_usado in ("crudo", "ia"),
        "error": None,
    }


def procesar_carpeta(carpeta_pdfs, incluir_texto_crudo=False, forzar=False):
    if not os.path.isdir(carpeta_pdfs):
        print(f"❌ No se encontró la carpeta: {carpeta_pdfs}")
        sys.exit(1)

    archivos_pdf = sorted(f for f in os.listdir(carpeta_pdfs) if f.lower().endswith('.pdf'))
    if not archivos_pdf:
        print(f"⚠️ No se encontró ningún PDF en '{carpeta_pdfs}'.")
        sys.exit(0)

    manifiesto = _cargar_manifiesto_anterior()

    # Separamos qué PDFs ya fueron extraídos en una corrida anterior (su .txt ya existe en
    # planes_de_estudio/) de los que hay que procesar ahora. "Ya extraído" se decide por la
    # presencia del .txt en disco, no por el manifiesto — así, si alguien borra un .txt puntual
    # a mano para forzar su reprocesamiento, alcanza con eso sin tener que tocar el manifiesto.
    pendientes = []
    saltados = []
    for nombre_pdf in archivos_pdf:
        archivo_txt = _slug_desde_nombre_pdf(nombre_pdf)
        ruta_txt = os.path.join(extractor.CARPETA_DESTINO, archivo_txt)
        if os.path.exists(ruta_txt) and not forzar:
            saltados.append(nombre_pdf)
        else:
            pendientes.append(nombre_pdf)

    print(f"📂 Se encontraron {len(archivos_pdf)} PDF(s) en '{carpeta_pdfs}'.")
    if saltados and not forzar:
        print(f"⏭️  Se saltean {len(saltados)} que ya tenían un .txt extraído (corré con --forzar para reprocesarlos igual).")
    print(f"▶️  Quedan {len(pendientes)} por procesar.\n")

    if not pendientes:
        print("🎉 No hay nada nuevo para procesar.")
        if saltados:
            print(f"\n📋 Detalle completo (incluye corridas anteriores) guardado en: {MANIFIESTO}")
        return

    for indice, nombre_pdf in enumerate(pendientes, start=1):
        ruta_pdf = os.path.join(carpeta_pdfs, nombre_pdf)

        print(f"[{indice}/{len(pendientes)}] Procesando: {nombre_pdf}")
        resultado = procesar_un_pdf(
            ruta_pdf, nombre_pdf,
            incluir_texto_crudo=incluir_texto_crudo,
            callback_aviso=lambda m: print(f"    [{m}]")
        )

        # El manifiesto guarda solo metadatos, no el texto completo (eso ya queda en el .txt
        # aparte) — por eso se descarta la clave "texto" acá antes de anotarlo.
        entrada_manifiesto = {k: v for k, v in resultado.items() if k != "texto"}
        manifiesto[nombre_pdf] = entrada_manifiesto

        if resultado["error"]:
            print(f"    ❌ {resultado['error']}")
            print("-" * 60)
            continue

        ruta_guardada = extractor.guardar_txt(resultado["texto"], resultado["archivo_txt"])
        print(f"    ✅ Guardado en: {ruta_guardada} (método: {resultado['metodo_usado']}, "
              f"{resultado['caracteres']} caracteres)")
        print("-" * 60)

    _guardar_manifiesto(manifiesto)

    procesados_ahora = [manifiesto[nombre_pdf] for nombre_pdf in pendientes]
    revisar = [m for m in procesados_ahora if m["requiere_revision_manual"]]
    print(f"\n🎉 Terminado. {len(procesados_ahora)} archivo(s) procesados en esta corrida "
          f"({len(procesados_ahora) - len(revisar)} con buena confianza, {len(revisar)} para revisar a mano)"
          + (f", {len(saltados)} saltados por ya estar extraídos." if saltados else "."))
    if revisar:
        print("\n⚠️ Convendría revisar estos antes de confiar en ellos del todo:")
        for m in revisar:
            print(f"   - {m['pdf_original']} -> {m.get('archivo_txt') or '(no se generó)'} "
                  f"(método: {m['metodo_usado']})")
    print(f"\n📋 Detalle completo (incluye corridas anteriores) guardado en: {MANIFIESTO}")


if __name__ == "__main__":
    incluir_texto_crudo = '--con-crudo' in sys.argv
    forzar = '--forzar' in sys.argv
    argumentos = [a for a in sys.argv[1:] if a not in ('--con-crudo', '--forzar')]

    if len(argumentos) != 1:
        print("Uso: python lote_extraccion_planes.py <carpeta_con_pdfs> [--con-crudo] [--forzar]")
        print('Ejemplo: python lote_extraccion_planes.py "pdfs_posgrado"')
        sys.exit(1)

    procesar_carpeta(argumentos[0], incluir_texto_crudo, forzar)