FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY backend /app/backend
COPY simulator /app/simulator
COPY database /app/database
COPY data/maps /app/data/maps
WORKDIR /app/backend
# train the (small) model at build time so the image is self-contained and reproducible (seed 42)
RUN python -m app.ml.train
EXPOSE 8000
CMD ["sh", "-c", "python -m app.migrate && python -m app.seed && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
