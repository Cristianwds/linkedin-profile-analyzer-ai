FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run inyecta $PORT; se lo pasamos a uvicorn en el arranque.
ENV PORT=8080
EXPOSE 8080

# --proxy-headers/--forwarded-allow-ips: Cloud Run termina el HTTPS en su proxy y le llega http al
# contenedor; sin esto, request.url_for() arma el redirect_uri del login con http:// y Google lo
# rechaza (redirect_uri_mismatch). Es seguro confiar en esos headers: al contenedor solo se llega
# a través del front-end de Cloud Run.
CMD ["sh", "-c", "uvicorn web_app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
