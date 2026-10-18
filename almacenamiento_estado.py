"""
almacenamiento_estado.py

Capa de persistencia para los archivos de estado que hoy se leen/escriben directo del disco
(carreras_grado.json, carreras_posgrado.json, cache_analisis.json, tokens/<email>.json, planes de
estudio). Si hay un bucket activo (ver nombre_bucket()), lee/escribe en ese bucket de Cloud Storage
en vez del disco local; si no, todo funciona exactamente igual que antes, con archivos locales.

Cuándo se usa el bucket:
  - En Cloud Run (donde el disco no persiste): siempre que GCS_BUCKET_ESTADO esté definida.
  - En tu compu: NUNCA por defecto, aunque GCS_BUCKET_ESTADO esté en el .env — así correr la
    web o `python main.py` en local no puede leer ni pisar el estado de producción (tokens de
    login de cada persona, caché, carreras, planes) por un .env copiado de producción. Para
    usarlo a propósito desde tu compu (como hace sincronizar_estado_bucket.py), hay que definir
    además USAR_BUCKET_EN_LOCAL=1.

La variable se lee cada vez que se necesita (no una sola vez al importar este módulo): antes se
leía al importarlo, y como main.py lo importa ANTES de cargar el .env, el resultado dependía del
orden de los imports de cada script.
"""

import json
import os

_buckets = {}  # nombre del bucket -> objeto Bucket, para armar el cliente una sola vez
_aviso_bucket_ignorado_mostrado = False


def corre_en_cloud_run():
    """Cloud Run define K_SERVICE sola en cada contenedor."""
    return bool(os.getenv("K_SERVICE"))


def nombre_bucket():
    """Nombre del bucket a usar, o None para usar el disco local (ver docstring del módulo)."""
    global _aviso_bucket_ignorado_mostrado
    bucket = os.getenv("GCS_BUCKET_ESTADO")
    if not bucket:
        return None
    if corre_en_cloud_run() or os.getenv("USAR_BUCKET_EN_LOCAL") == "1":
        return bucket
    if not _aviso_bucket_ignorado_mostrado:
        print(f"   [ℹ️ GCS_BUCKET_ESTADO ('{bucket}') está definida, pero fuera de Cloud Run se usan los "
              "archivos locales. Para usar el bucket desde acá, definí también USAR_BUCKET_EN_LOCAL=1.]")
        _aviso_bucket_ignorado_mostrado = True
    return None


def _obtener_bucket():
    """Arma el cliente de Cloud Storage una sola vez por bucket (import diferido: si no usás
    bucket, no hace falta ni tener instalada la librería). Solo se llama si hay bucket activo."""
    bucket = nombre_bucket()
    if bucket not in _buckets:
        from google.cloud import storage
        _buckets[bucket] = storage.Client().bucket(bucket)
    return _buckets[bucket]


def existe(ruta_local):
    if nombre_bucket():
        return _obtener_bucket().blob(ruta_local).exists()
    return os.path.exists(ruta_local)


def leer_json(ruta_local, valor_por_defecto):
    """ruta_local es la misma ruta relativa que ya usás para disco (ej. 'carreras_grado.json',
    'tokens/juli_udesa_edu_ar.json'); en el bucket se guarda con ese mismo nombre de blob."""
    if nombre_bucket():
        blob = _obtener_bucket().blob(ruta_local)
        if not blob.exists():
            return valor_por_defecto
        try:
            return json.loads(blob.download_as_text())
        except Exception as e:
            print(f"   [⚠️ No se pudo leer '{ruta_local}' del bucket, se usa valor por defecto: {e}]")
            return valor_por_defecto

    if not os.path.exists(ruta_local):
        return valor_por_defecto
    try:
        with open(ruta_local, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"   [⚠️ No se pudo leer '{ruta_local}' del disco, se usa valor por defecto: {e}]")
        return valor_por_defecto


def escribir_json(ruta_local, datos):
    if nombre_bucket():
        _obtener_bucket().blob(ruta_local).upload_from_string(
            json.dumps(datos, ensure_ascii=False, indent=2),
            content_type="application/json"
        )
        return

    carpeta = os.path.dirname(ruta_local)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    with open(ruta_local, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)


def leer_texto(ruta_local):
    """Variante para texto plano (la usan los tokens de Google: creds.to_json() es un string,
    no lo queremos re-serializar con json.dumps)."""
    if nombre_bucket():
        blob = _obtener_bucket().blob(ruta_local)
        return blob.download_as_text() if blob.exists() else None

    if not os.path.exists(ruta_local):
        return None
    with open(ruta_local, 'r', encoding='utf-8') as f:
        return f.read()


def borrar(ruta_local):
    """No falla si el archivo/blob no existe — mismo comportamiento no-estricto que ya tenía
    el os.remove() envuelto en un chequeo de existencia previo."""
    if nombre_bucket():
        blob = _obtener_bucket().blob(ruta_local)
        if blob.exists():
            blob.delete()
        return

    if os.path.exists(ruta_local):
        os.remove(ruta_local)


def escribir_texto(ruta_local, contenido):
    if nombre_bucket():
        _obtener_bucket().blob(ruta_local).upload_from_string(contenido, content_type="application/json")
        return

    carpeta = os.path.dirname(ruta_local)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    with open(ruta_local, 'w', encoding='utf-8') as f:
        f.write(contenido)
