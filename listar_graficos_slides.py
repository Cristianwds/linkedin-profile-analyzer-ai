"""
listar_graficos_slides.py

Script de UNA SOLA CORRIDA para obtener el objectId de cada gráfico vinculado (sheetsChart)
insertado en la presentación de estadísticas. Ejecutalo desde la misma carpeta que main.py
(usa su misma autenticación: autenticar_google()).

Requisitos antes de correrlo:
  1. Haber agregado 'https://www.googleapis.com/auth/presentations' a SCOPES en main.py.
  2. Haber borrado token.json y vuelto a loguearte (así el token nuevo incluye el permiso de Slides).
  3. Haber habilitado la Google Slides API para el proyecto en Google Cloud Console.
  4. Haber insertado ya los gráficos en el Slides como VINCULADOS (Insertar > Gráfico >
     De Hojas de cálculo), no como imagen pegada.
  5. Tener ID_PRESENTACION_STATS cargado en tu .env (mismo archivo que ID_SPREADSHEET, etc.):
         ID_PRESENTACION_STATS=el_id_de_tu_presentacion

Uso:
    python listar_graficos_slides.py

Volvés a correr este script cada vez que reordenes, borres o agregues gráficos en el Slides
(cualquier cambio estructural les cambia el objectId), para actualizar la lista
OBJECT_IDS_GRAFICOS_STATS en main.py.
"""

import os
from dotenv import load_dotenv
from googleapiclient.discovery import build
from main import autenticar_google  # reusa la misma autenticación que ya tenés en main.py

load_dotenv()  # por las dudas: main.py ya lo hace al importarse, esto es solo defensivo

ID_PRESENTACION_STATS = os.getenv('ID_PRESENTACION_STATS')


def listar_graficos_vinculados(id_presentacion):
    if not id_presentacion:
        print("❌ No se encontró ID_PRESENTACION_STATS en tu .env. Agregalo y volvé a correr.")
        return []

    creds = autenticar_google()
    servicio_slides = build('slides', 'v1', credentials=creds)

    presentacion = servicio_slides.presentations().get(
        presentationId=id_presentacion
    ).execute()

    encontrados = []
    for indice_slide, slide in enumerate(presentacion.get('slides', []), start=1):
        for elemento in slide.get('pageElements', []):
            sheets_chart = elemento.get('sheetsChart')
            if sheets_chart:
                object_id = elemento['objectId']
                chart_id = sheets_chart.get('chartId')
                spreadsheet_id = sheets_chart.get('spreadsheetId')
                encontrados.append({
                    "diapositiva": indice_slide,
                    "objectId": object_id,
                    "chartId_en_sheets": chart_id,
                    "spreadsheetId": spreadsheet_id,
                })
                print(f"Diapositiva {indice_slide} | objectId: {object_id} | "
                      f"chartId (Sheets): {chart_id}")

    if not encontrados:
        print("\n⚠️ No se encontró ningún gráfico vinculado (sheetsChart) en esta presentación.")
        print("   Revisá que los hayas insertado con 'Insertar > Gráfico > De Hojas de cálculo'")
        print("   y no como imagen pegada, y que ID_PRESENTACION_STATS sea el correcto.")
    else:
        print(f"\n✅ Se encontraron {len(encontrados)} gráficos vinculados.")
        print("   Pasame esta lista completa para actualizar OBJECT_IDS_GRAFICOS_STATS en main.py.")

    return encontrados


if __name__ == "__main__":
    listar_graficos_vinculados(ID_PRESENTACION_STATS)