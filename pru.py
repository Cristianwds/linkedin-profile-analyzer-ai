import os
from dotenv import load_dotenv
load_dotenv()
from main import autenticar_google_cli
from generar_presentacion_stats import construir_servicio_drive

creds = autenticar_google_cli()
drive = construir_servicio_drive(creds)

cuenta = drive.about().get(fields="user").execute()
print("Cuenta autenticada:", cuenta["user"]["emailAddress"])

id_master = os.environ["ID_PRESENTACION_MASTER_STATS"]
info = drive.files().get(
    fileId=id_master,
    fields="id,name,mimeType,driveId",
    supportsAllDrives=True,
).execute()
print(info)