FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ldq_python_client-1.0.0-py3-none-any.whl ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./

EXPOSE 8000

CMD ["python", "server.py", "--transport", "streamable-http", "--host", "0.0.0.0"]
