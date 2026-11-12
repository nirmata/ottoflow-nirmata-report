FROM python:3.12-slim

# Install kubectl
RUN apt-get update && apt-get install -y curl ca-certificates && \
    curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && \
    rm kubectl && apt-get clean

WORKDIR /app

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY app.py .

# Copy OttoFlow binary and workflow files (mounted or copied at build time)
# COPY ../bin/ottoflow /usr/local/bin/ottoflow
# COPY ../*.yaml /app/workflow/

ENV PORT=3000
EXPOSE 3000

CMD ["python", "app.py"]
