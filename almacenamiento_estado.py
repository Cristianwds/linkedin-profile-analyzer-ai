"""
almacenamiento_estado.py

Capa de persistencia para los archivos de estado que hoy se leen/escriben directo del disco
(carreras_grado.json, carreras_posgrado.json, cache_analisis.json, tokens/<email>.json). Se
activa sola si existe la variable de entorno GCS_BUCKET_ESTADO: en ese caso lee/escribe en ese
bucket de Cloud Storage en vez del disco local. Si esa variable NO está seteada (uso en tu
compu, o `python main.py` por consola), todo sigue funcionando exactamente igual que antes.
"""

import json
import os

BUCKET_ESTADO = os.getenv("GCS_BUCKET_ESTADO")

_cliente_storage = None
_bucket = None


def _obtener_bucket():
    """Arma el cliente de Cloud Storage una sola vez (import diferido: si no usás bucket, no
    hace falta ni tener instalada la librería)."""
    global _cliente_storage, _bucket
    if _bucket is None:
        from google.cloud import storage
        _cliente_storage = storage.Client()
        _bucket = _cliente_storage.bucket(BUCKET_ESTADO)
    return _bucket


def existe(ruta_local):
    if BUCKET_ESTADO:
        return _obtener_bucket().blob(ruta_local).exists()
    return os.path.exists(ruta_local)


def leer_json(ruta_local, valor_por_defecto):
    """ruta_local es la misma ruta relativa que ya usás para disco (ej. 'carreras_grado.json',
    'tokens/juli_udesa_edu_ar.json'); en el bucket se guarda con ese mismo nombre de blob."""
    if BUCKET_ESTADO:
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
    if BUCKET_ESTADO:
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
    if BUCKET_ESTADO:
        blob = _obtener_bucket().blob(ruta_local)
        return blob.download_as_text() if blob.exists() else None

    if not os.path.exists(ruta_local):
        return None
    with open(ruta_local, 'r', encoding='utf-8') as f:
        return f.read()


def borrar(ruta_local):
    """No falla si el archivo/blob no existe — mismo comportamiento no-estricto que ya tenía
    el os.remove() envuelto en un chequeo de existencia previo."""
    if BUCKET_ESTADO:
        blob = _obtener_bucket().blob(ruta_local)
        if blob.exists():
            blob.delete()
        return

    if os.path.exists(ruta_local):
        os.remove(ruta_local)


def escribir_texto(ruta_local, contenido):
    if BUCKET_ESTADO:
        _obtener_bucket().blob(ruta_local).upload_from_string(contenido, content_type="application/json")
        return

    carpeta = os.path.dirname(ruta_local)
    if carpeta:
        os.makedirs(carpeta, exist_ok=True)
    with open(ruta_local, 'w', encoding='utf-8') as f:
        f.write(contenido)