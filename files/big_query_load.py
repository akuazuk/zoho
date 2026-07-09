import os
from google.cloud import storage
from google.cloud import bigquery

os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = '/Users/pavel/Kravira_Work/Jupyter/PL/carbide-datum-383616-8960c7f83f5b.json'

project_id = 'carbide-datum-383616'
bucket_name = 'kravira_report'
source_file_name = 'combined_report_all_zoho.csv'
dataset_id = 'kravira_last'
table_id = 'combined_report_temp'

storage_client = storage.Client(project=project_id)
bigquery_client = bigquery.Client(project=project_id)

def load_file_from_gcs():
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(source_file_name)
    local_file_path = '/tmp/' + source_file_name
    blob.download_to_filename(local_file_path)
    return local_file_path

def load_data_to_table(local_file_path):
    table_ref = bigquery_client.dataset(dataset_id).table(table_id)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        autodetect=True,
        skip_leading_rows=1,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE
    )

    with open(local_file_path, "rb") as source_file:
        job = bigquery_client.load_table_from_file(
            source_file,
            table_ref,
            job_config=job_config
        )

    job.result()
    print(f"Данные загружены в таблицу {dataset_id}.{table_id}")

def main():
    local_file_path = load_file_from_gcs()
    load_data_to_table(local_file_path)

if __name__ == "__main__":
    main()