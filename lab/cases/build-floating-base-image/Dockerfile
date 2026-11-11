# Built fine for years. python:3 has since moved to a newer Python, and the pinned numpy has no
# wheel for it and cannot be built from source there.
FROM python:3
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY report.py .
CMD ["python", "report.py"]
