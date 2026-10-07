"""
sincronizar_estado_bucket.py

Sube al bucket de Cloud Storage (GCS_BUCKET_ESTADO) las carreras y los planes de estudio que
tenés en tu carpeta local, para no tener que cargarlos uno por uno desde la web:

  - carreras_grado.json y carreras_posgrado.json
  - planes_de_estudio/grado/*.txt y planes_de_estudio/posgrado/*.txt

Es lo que la web lee en producción (en Cloud Run el disco no persiste, ver
almacenamiento_estado.py), así que lo que subas acá aparece en la web al refrescar la página.

POR DEFECTO NO CAMBIA NADA: solo compara tu carpeta local con el bucket y te muestra qué haría.

USO:
    python sincronizar_estado_bucket.py                  # solo muestra las diferencias (seguro)
    python sincronizar_estado_bucket.py --aplicar        # sube lo NUEVO y agrega las carreras que faltan
    python sincronizar_estado_bucket.py --aplicar --sobrescribir=finanzas.txt
                                                         # además pisa ESE plan (o varios, separados
                                                         # por coma) que ya existe en el bucket
    python sincronizar_estado_bucket.py --aplicar --sobrescribir
                                                         # pisa TODOS los planes que difieren (con
                                                         # cuidado: puede borrar ediciones hechas
                                                         # desde la web)

Reglas de seguridad:
  - Un plan que ya existe en el bucket y es distinto NO se pisa, salvo que pases --sobrescribir
    (por ejemplo, si alguien lo editó a mano desde la web, no lo perdés sin querer).
  - En los JSON de carreras solo se AGREGAN las carreras que faltan en el bucket; las que ya
    existen (con las palabras clave que alguien haya ajustado desde la web) no se tocan.

Requisitos: GCS_BUCKET_ESTADO en el .env, y credenciales de Google en tu compu con acceso al
bucket (las mismas con las que corrés `gcloud`; si falla, `gcloud auth application-default login`).
"""

import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import almacenamiento_estado  # noqa: E402  (tiene que importarse DESPUÉS de load_dotenv)

CARPETA_PLANES = "planes_de_estudio"
NIVELES = ("grado", "posgrado")
ARCHIVOS_CARRERAS = {"grado": "carreras_grado.json", "posgrado": "carreras_posgrado.json"}


def _normalizar(texto):
    """Compara sin que importen los saltos de línea de Windows (\\r\\n) ni espacios al final."""
    return texto.replace("\r\n", "\n").strip()


def _leer_local(ruta):
    with open(ruta, "r", encoding="utf-8") as f:
        return f.read()


def _subir_texto(nombre_blob, contenido):
    bucket = almacenamiento_estado._obtener_bucket()
    bucket.blob(nombre_blob).upload_from_string(contenido, content_type="text/plain; charset=utf-8")


def _planes_locales():
    """Devuelve [(nombre_blob, ruta_local)]. El nombre del blob usa SIEMPRE '/', igual que en
    Cloud Run (Linux). En Windows os.path.join usaría '\\' y crearía objetos distintos."""
    resultado = []
    for nivel in NIVELES:
        carpeta = os.path.join(CARPETA_PLANES, nivel)
        if not os.path.isdir(carpeta):
            continue
        for nombre in sorted(os.listdir(carpeta)):
            if nombre.lower().endswith(".txt"):
                resultado.append((f"{CARPETA_PLANES}/{nivel}/{nombre}", os.path.join(carpeta, nombre)))
    return resultado


def sincronizar_carreras(aplicar):
    print("== Carreras ==")
    for nivel, archivo in ARCHIVOS_CARRERAS.items():
        if not os.path.exists(archivo):
            print(f"  (no existe {archivo} en tu carpeta, se omite)")
            continue
        local = json.loads(_leer_local(archivo))
        remoto = almacenamiento_estado.leer_json(archivo, {})

        nuevas = [n for n in local if n not in remoto]
        distintas = [n for n in local if n in remoto and remoto[n] != local[n]]

        print(f"  {archivo}: {len(local)} locales, {len(remoto)} en el bucket -> "
              f"{len(nuevas)} nuevas, {len(distintas)} ya existentes con datos distintos (no se tocan)")
        for n in nuevas:
            print(f"      + {n}")
        for n in distintas:
            print(f"      ~ {n}  (en el bucket tiene otras palabras clave o archivo; se respeta el del bucket)")

        if aplicar and nuevas:
            for n in nuevas:
                remoto[n] = local[n]
            almacenamiento_estado.escribir_json(archivo, remoto)
            print(f"      ✅ Se agregaron {len(nuevas)} carreras a {archivo} en el bucket.")


def _quiere_sobrescribir(blob, sobrescribir):
    """sobrescribir: False (nada), True (todo) o un set de nombres de archivo (ej. {'finanzas.txt'})."""
    if sobrescribir is True:
        return True
    return bool(sobrescribir) and os.path.basename(blob) in sobrescribir


def sincronizar_planes(aplicar, sobrescribir):
    print("\n== Planes de estudio ==")
    nuevos, distintos, iguales = [], [], []
    for blob, ruta in _planes_locales():
        local = _leer_local(ruta)
        remoto = almacenamiento_estado.leer_texto(blob)
        if remoto is None:
            nuevos.append((blob, local))
        elif _normalizar(remoto) != _normalizar(local):
            distintos.append((blob, local, remoto))
        else:
            iguales.append(blob)

    print(f"  {len(iguales)} iguales, {len(nuevos)} nuevos, {len(distintos)} distintos al del bucket")
    for blob, local in nuevos:
        print(f"      + {blob}  ({len(local)} caracteres)")
    for blob, local, remoto in distintos:
        print(f"      ~ {blob}  (local {len(local)} car. vs bucket {len(remoto)} car.)")

    if not aplicar:
        return

    for blob, local in nuevos:
        _subir_texto(blob, local)
    if nuevos:
        print(f"      ✅ Se subieron {len(nuevos)} planes nuevos.")

    for blob, local, _ in distintos:
        if _quiere_sobrescribir(blob, sobrescribir):
            _subir_texto(blob, local)
            print(f"      ✅ Sobrescrito con la versión local: {blob}")
    sin_tocar = [b for b, _, _ in distintos if not _quiere_sobrescribir(b, sobrescribir)]
    if sin_tocar:
        print(f"      ⏭️  NO se tocaron (son distintos): {', '.join(os.path.basename(b) for b in sin_tocar)}. "
              "Usá --sobrescribir=archivo.txt si querés pisarlos.")


def main():
    aplicar = "--aplicar" in sys.argv
    # --sobrescribir (todo) o --sobrescribir=a.txt,b.txt (solo esos planes).
    sobrescribir = False
    for arg in sys.argv[1:]:
        if arg == "--sobrescribir":
            sobrescribir = True
        elif arg.startswith("--sobrescribir="):
            sobrescribir = {n.strip() for n in arg.split("=", 1)[1].split(",") if n.strip()}

    if not almacenamiento_estado.BUCKET_ESTADO:
        print("❌ Falta GCS_BUCKET_ESTADO en el .env: no sé a qué bucket subir.")
        sys.exit(1)

    print(f"Bucket: {almacenamiento_estado.BUCKET_ESTADO}")
    if aplicar:
        extra = ""
        if sobrescribir is True:
            extra = " (sobrescribiendo TODOS los planes distintos)"
        elif sobrescribir:
            extra = f" (sobrescribiendo: {', '.join(sorted(sobrescribir))})"
        print("Modo: APLICAR cambios" + extra + "\n")
    else:
        print("Modo: solo mostrar diferencias (no se cambia nada)\n")

    sincronizar_carreras(aplicar)
    sincronizar_planes(aplicar, sobrescribir)

    if not aplicar:
        print("\nNo se cambió nada. Corré con --aplicar para subir lo nuevo.")


if __name__ == "__main__":
    main()
