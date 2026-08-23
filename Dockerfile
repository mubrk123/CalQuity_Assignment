FROM python:3.11-slim

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY . .
RUN chmod +x start.sh

# The corpus is built from the PDFs at boot, not baked in, so the index can
# never disagree with the source documents.
EXPOSE 8000
CMD ["./start.sh"]
