FROM python:3.12-slim

WORKDIR /app

COPY cloud/requirements-cloud.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY zoho_analytics.py zoho_prognosis_sheets.py ./
COPY cloud/formulas/ cloud/formulas/

# Credentials mounted via Secret Manager -> /secrets/sheets-sa.json
ENV GOOGLE_SHEETS_CREDENTIALS=/secrets/sheets-sa.json
ENV PROGNOSIS_SCHEDULE=always

CMD ["python", "zoho_prognosis_sheets.py", "run"]
