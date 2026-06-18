# Imagen base oficial de Python
FROM python:3.11-slim

# Instalar dependencias del sistema para psycopg2
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Directorio de trabajo
WORKDIR /app

# Copiar requirements primero (aprovecha caché de Docker)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el código
COPY . .

# Crear carpeta para uploads temporales
RUN mkdir -p /app/data/imports

# Puerto que usa Streamlit (HF Spaces requiere 7860)
EXPOSE 7860

# Variables de entorno para Streamlit
ENV STREAMLIT_SERVER_PORT=7860
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Comando de inicio
CMD ["streamlit", "run", "app.py", "--server.port=7860", "--server.address=0.0.0.0"]
