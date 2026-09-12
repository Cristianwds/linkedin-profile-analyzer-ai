"""
lote_extraccion_planes.py

Utilidad standalone para procesar una CARPETA ENTERA de PDFs de planes de estudio de una sola
corrida, reusando exactamente la misma lógica de extraccion_plan_estudios.py (grilla por
coordenadas → tablas por líneas → reordenamiento con IA como último recurso) para cada archivo.

Pensado para la carga inicial de muchas carreras de golpe (por ejemplo, todo el catálogo de
posgrados) sin tener que correr extraccion_plan_estudios.py a mano, uno por uno.

No crea ni modifica carreras_posgrado.json / carreras_grado.json — eso se arma aparte, a mano
o con ayuda, una vez que revisaste los .txt generados. Este script SOLO genera los .txt en
planes_de_estudio/ y un manifiesto (manifiesto_planes.json) con el resultado de cada archivo,
para que sepas de un vistazo cuáles conviene revisar antes de confiar en ellos.

USO:
    python lote_extraccion_planes.py "ruta/a/la/carpeta/con/pdfs"

Ejemplo:
    python lote_extraccion_planes.py "pdfs_posgrado"

Flags opcionales:
    --con-crudo   Igual que en extraccion_plan_estudios.py: agrega al final de cada .txt el
                  texto completo extraído del PDF sin procesar, como referencia.

El nombre de archivo .txt de salida se deriva del nombre del PDF (minúsculas, sin tildes,
espacios y símbolos -> guion bajo) — NO del nombre real de la carrera, porque el script no lo
conoce. Por eso el manifiesto incluye una columna "nombre_sugerido" (el nombre del PDF prolijado,
con mayúsculas), para que la revises y corrijas a mano antes de armar carreras_posgrado.json.
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


def procesar_carpeta(carpeta_pdfs, incluir_texto_crudo=False):
    if not os.path.isdir(carpeta_pdfs):
        print(f"❌ No se encontró la carpeta: {carpeta_pdfs}")
        sys.exit(1)

    archivos_pdf = sorted(f for f in os.listdir(carpeta_pdfs) if f.lower().endswith('.pdf'))
    if not archivos_pdf:
        print(f"⚠️ No se encontró ningún PDF en '{carpeta_pdfs}'.")
        sys.exit(0)

    print(f"📂 Se encontraron {len(archivos_pdf)} PDF(s) en '{carpeta_pdfs}'.\n")

    manifiesto = []
    for indice, nombre_pdf in enumerate(archivos_pdf, start=1):
        ruta_pdf = os.path.join(carpeta_pdfs, nombre_pdf)
        archivo_txt = _slug_desde_nombre_pdf(nombre_pdf)
        nombre_sugerido = _nombre_sugerido_desde_pdf(nombre_pdf)

        print(f"[{indice}/{len(archivos_pdf)}] Procesando: {nombre_pdf}")
        try:
            texto_final, metodo_usado = extractor.extraer_texto_de_pdf(
                ruta_pdf,
                incluir_texto_crudo=incluir_texto_crudo,
                callback_aviso=lambda m: print(f"    [{m}]")
            )
        except ValueError as e:
            print(f"    ❌ {e}")
            manifiesto.append({
                "pdf_original": nombre_pdf,
                "archivo_txt": None,
                "nombre_sugerido": nombre_sugerido,
                "metodo_usado": "error",
                "caracteres": 0,
                "requiere_revision_manual": True,
                "error": str(e),
            })
            print("-" * 60)
            continue

        ruta_guardada = extractor.guardar_txt(texto_final, archivo_txt)
        print(f"    ✅ Guardado en: {ruta_guardada} (método: {metodo_usado}, "
              f"{len(texto_final)} caracteres)")

        manifiesto.append({
            "pdf_original": nombre_pdf,
            "archivo_txt": archivo_txt,
            "nombre_sugerido": nombre_sugerido,
            "metodo_usado": metodo_usado,
            "caracteres": len(texto_final),
            "requiere_revision_manual": metodo_usado in ("crudo", "ia"),
        })
        print("-" * 60)

    with open(MANIFIESTO, 'w', encoding='utf-8') as f:
        json.dump(manifiesto, f, ensure_ascii=False, indent=2)

    revisar = [m for m in manifiesto if m["requiere_revision_manual"]]
    print(f"\n🎉 Terminado. {len(manifiesto)} archivo(s) procesados, "
          f"{len(manifiesto) - len(revisar)} con buena confianza, {len(revisar)} para revisar a mano.")
    if revisar:
        print("\n⚠️ Convendría revisar estos antes de confiar en ellos del todo:")
        for m in revisar:
            print(f"   - {m['pdf_original']} -> {m.get('archivo_txt') or '(no se generó)'} "
                  f"(método: {m['metodo_usado']})")
    print(f"\n📋 Detalle completo guardado en: {MANIFIESTO}")


if __name__ == "__main__":
    incluir_texto_crudo = '--con-crudo' in sys.argv
    argumentos = [a for a in sys.argv[1:] if a != '--con-crudo']

    if len(argumentos) != 1:
        print("Uso: python lote_extraccion_planes.py <carpeta_con_pdfs> [--con-crudo]")
        print('Ejemplo: python lote_extraccion_planes.py "pdfs_posgrado"')
        sys.exit(1)

    procesar_carpeta(argumentos[0], incluir_texto_crudo)