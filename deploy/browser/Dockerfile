FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
RUN pip install --no-cache-dir playwright==1.63.0
COPY assistant/browser_worker.py /opt/browser_worker.py
ENV PYTHONDONTWRITEBYTECODE=1
USER pwuser
CMD ["python", "-I", "-u", "/opt/browser_worker.py"]
