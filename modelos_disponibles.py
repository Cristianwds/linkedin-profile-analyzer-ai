import os
from google import genai
from dotenv import load_dotenv

# Carga las variables del archivo .env
load_dotenv()

# Para usar la variable, debes llamarla a través de os.environ o os.getenv
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

print("Modelos disponibles:")
for modelo in client.models.list():
    print(modelo.name)