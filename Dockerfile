FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md constraints.txt ./
COPY src ./src
RUN pip install --no-cache-dir -c constraints.txt . && useradd --create-home --uid 10001 orderflow
USER orderflow
EXPOSE 8000
CMD ["orderflow", "serve", "--host", "0.0.0.0"]
