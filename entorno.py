"""
entorno.py

Elige qué archivo de variables cargar, para poder probar el pipeline y la web en tu compu sin
tocar la planilla ni las carpetas de Drive de producción.

Regla (la primera que se cumpla):
  1. Si en la terminal está definida ARCHIVO_ENV, se carga ese archivo y ningún otro (ruta
     relativa a la raíz del proyecto). Sirve para forzar uno a propósito, por ejemplo:
         ARCHIVO_ENV=.env python main.py          # producción aunque exista .env.prueba
  2. Si no, y existe .env.prueba en la raíz del proyecto, se carga ese: ENTORNO DE PRUEBA.
  3. Si no, el .env de siempre (igual que antes de este archivo).

Se carga UN solo archivo, nunca los dos: si a .env.prueba le falta una clave, queda vacía, en vez
de completarse con el valor de producción del .env. Por eso todos los scripts cargan las
variables con cargar_variables() en vez de llamar a load_dotenv() por su cuenta.

En Cloud Run no hay ningún .env (están en .gcloudignore) y las variables vienen de .env.yaml, así
que ahí no cambia nada; igual, en Cloud Run .env.prueba se ignora aunque se haya subido por error.

Como seguro extra, en el entorno de prueba se corta con un error si alguno de los IDs donde el
pipeline escribe (planilla y carpetas) es igual al de producción (.env o .env.yaml): pasa si se
copia el .env a .env.prueba y queda un ID sin cambiar.
"""

import os
import re

from dotenv import dotenv_values, load_dotenv

RAIZ_PROYECTO = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_ENV_PRODUCCION = ".env"
ARCHIVO_ENV_PRUEBA = ".env.prueba"

# IDs donde el pipeline escribe: filas en la planilla, PDFs que se mueven, informes y
# presentaciones que se crean. En el entorno de prueba no pueden coincidir con los de producción.
IDS_DONDE_SE_ESCRIBE = (
    "ID_SPREADSHEET", "ID_CARPETA", "ID_CARPETA_ANALIZADOS",
    "ID_CARPETA_INFORMES", "ID_CARPETA_PRESENTACIONES",
)
# Las plantillas solo se copian (nunca se editan), así que compartirlas con producción no ensucia
# nada: si coinciden, solo se avisa.
IDS_PLANTILLAS = ("ID_PLANTILLA_INFORME", "ID_PRESENTACION_MASTER_STATS")

_archivo_cargado = None


def _ruta_en_proyecto(nombre):
    return nombre if os.path.isabs(nombre) else os.path.join(RAIZ_PROYECTO, nombre)


def _elegir_archivo():
    elegido = os.getenv("ARCHIVO_ENV")
    if elegido:
        if not os.path.exists(_ruta_en_proyecto(elegido)):
            raise SystemExit(f"ARCHIVO_ENV={elegido}: ese archivo no existe en {RAIZ_PROYECTO}.")
        return elegido
    if not os.getenv("K_SERVICE") and os.path.exists(_ruta_en_proyecto(ARCHIVO_ENV_PRUEBA)):
        return ARCHIVO_ENV_PRUEBA
    return ARCHIVO_ENV_PRODUCCION


def _ids_de_produccion():
    """Los IDs de .env y .env.yaml (los que existan), sin cargarlos en el entorno. .env.yaml se lee
    a mano, línea por línea (CLAVE: "valor"), para no sumar PyYAML como dependencia."""
    ids = {}
    ruta_env = _ruta_en_proyecto(ARCHIVO_ENV_PRODUCCION)
    if os.path.exists(ruta_env):
        ids.update({k: v for k, v in dotenv_values(ruta_env).items() if v})
    ruta_yaml = _ruta_en_proyecto(".env.yaml")
    if os.path.exists(ruta_yaml):
        with open(ruta_yaml, encoding="utf-8") as f:
            for linea in f:
                coincidencia = re.match(r'\s*(\w+)\s*:\s*["\']?([^"\'#\s]*)', linea)
                if coincidencia and coincidencia.group(2):
                    ids.setdefault(coincidencia.group(1), coincidencia.group(2))
    return ids


def _verificar_ids_distintos_de_produccion():
    produccion = _ids_de_produccion()
    repetidos = [c for c in IDS_DONDE_SE_ESCRIBE if os.getenv(c) and os.getenv(c) == produccion.get(c)]
    if repetidos:
        raise SystemExit(
            f"🧪 {_archivo_cargado} tiene los mismos IDs que producción en: {', '.join(repetidos)}.\n"
            f"   Cambialos por los de la planilla y las carpetas de prueba (ver .env.prueba.example), "
            f"o corré con ARCHIVO_ENV=.env si de verdad querés usar producción."
        )
    plantillas = [c for c in IDS_PLANTILLAS if os.getenv(c) and os.getenv(c) == produccion.get(c)]
    if plantillas:
        print(f"   [ℹ️ {', '.join(plantillas)}: es la misma plantilla que en producción. Solo se copia, "
              f"no se edita, pero si querés probar cambios en la plantilla usá una copia.]")


def cargar_variables():
    """Carga las variables del archivo elegido (ver la regla arriba). Se puede llamar desde varios
    módulos: solo carga la primera vez. Como load_dotenv(), no pisa variables que ya estén
    definidas en la terminal."""
    global _archivo_cargado
    if _archivo_cargado is not None:
        return _archivo_cargado
    _archivo_cargado = _elegir_archivo()
    load_dotenv(_ruta_en_proyecto(_archivo_cargado))
    if es_entorno_prueba():
        print(f"🧪 ENTORNO DE PRUEBA: variables de {_archivo_cargado} (planilla y carpetas de Drive de prueba).")
        _verificar_ids_distintos_de_produccion()
    return _archivo_cargado


def archivo_cargado():
    """Nombre del archivo de variables que se cargó (o None si todavía no se llamó a
    cargar_variables)."""
    return _archivo_cargado


def es_entorno_prueba():
    """True si se cargó cualquier archivo que no sea el .env de producción."""
    if _archivo_cargado is None:
        return False
    return os.path.abspath(_ruta_en_proyecto(_archivo_cargado)) != _ruta_en_proyecto(ARCHIVO_ENV_PRODUCCION)
