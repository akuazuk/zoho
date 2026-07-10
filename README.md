# Zoho / MIS data pipeline

Автоматизация синхронизации данных клиники: MariaDB → SQLite → Zoho Analytics, прогноз CF → Google Sheets, ручной экспорт в BigQuery.

## Два блока проекта

| Код | Конфиг | Что делает | Где / когда |
|-----|--------|------------|-------------|
| **`mis-daily`** | `cloud/mis-daily-config.env` + секреты в `.env` | MariaDB → SQLite → Zoho `combined_report_temp` | Mac, **каждый день 06:30** |
| **`prognosis-sheets`** | `cloud/prognosis-config.env` | Zoho `Прогноз_CF` → лист **Фин_Неделя** | GCP, cron из конфига (сейчас **пн–пт 08:00–17:00 MSK**) |

### Куда пишет `prognosis-sheets` (см. `cloud/prognosis-config.env`)

| Ячейка | Содержимое |
|--------|------------|
| **A140** | Сумма **текущей недели** |
| **A1** | Сумма **следующей недели** |

### Расписание (тоже в `cloud/prognosis-config.env`)

```env
PROGNOSIS_CRON_SCHEDULE=0 8-17 * * 1-5   # в файле: PROGNOSIS_CRON_SCHEDULE="0 8-17 * * 1-5"
PROGNOSIS_TIMEZONE=Europe/Moscow
```

Правка ячеек **или расписания** → commit → push в `main` → Cloud Build обновит job и scheduler.

## Компоненты

| Задача | Скрипт | Где запускать |
|--------|--------|---------------|
| **mis-daily** — MariaDB → SQLite → Zoho | `scripts/run_daily_sync.sh` | **Mac** (LaunchAgent) |
| **prognosis-sheets** — Прогноз_CF → Фин_Неделя | `zoho_prognosis_sheets.py` | **GCP** Cloud Run Job |
| BigQuery sync (вручную) | `bq_sync.py` | Mac / любая машина с ключом carbide |

MariaDB (`178.163.240.131`) и локальная SQLite-база — причина оставить **ежедневный sync на Mac**.  
Прогноз в Sheets нужен только Zoho API + Google Sheets — **удобно перенести в облако**, Mac не нужен.

## Быстрый старт (Mac)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить секреты
```

Ежедневный sync:

```bash
bash scripts/install_daily.sh   # если есть install-скрипт
# или вручную: bash scripts/run_daily_sync.sh
```

Прогноз → Sheets (локально):

```bash
python zoho_prognosis_sheets.py run
```

## Прогноз: Mac (временно) — только будни 8:00–18:00

```bash
bash scripts/install_prognosis_hourly.sh
```

LaunchAgent срабатывает каждый час, но скрипт **пропускает** запуск вне окна пн–пт 08:00–17:59 (`PROGNOSIS_SCHEDULE=weekdays_8_18`).

Проверка:

```bash
launchctl list | grep prognosis
tail -f logs/prognosis_sheets_*.log
```

## Прогноз: настройки через Git

**Один файл:** `cloud/prognosis-config.env` — ячейки, таблица, Zoho-таблица.  
**Секреты:** `.env` (Mac) / GCP Secret Manager (облако).

После правки → commit в `main` → Cloud Build обновит job.

## Прогноз: Google Cloud (рекомендуется)

Бесплатного лимита Cloud Run + Cloud Scheduler хватает на ~10 запусков/день по будням.

1. Установить [Google Cloud SDK](https://cloud.google.com/sdk/docs/install)
2. `gcloud auth login && gcloud config set project carbide-datum-383616`
3. Из корня проекта:

```bash
bash cloud/deploy_prognosis.sh
```

Расписание: **пн–пт, каждый час 08:00–17:00 по Москве**.

Тест:

```bash
gcloud run jobs execute prognosis-sheets --region=europe-west1 --wait
```

Когда облако работает — отключить Mac:

```bash
launchctl unload ~/Library/LaunchAgents/com.kravira.prognosis-hourly.plist
```

## Секреты

Не коммитить в git:

- `.env`
- `*-*.json` (ключи service account)
- `data/`, `logs/`

Два GCP-проекта:

- **carbide-datum-383616** — BigQuery (`GOOGLE_APPLICATION_CREDENTIALS`)
- **sonorous-saga-321204** — Google Sheets (`GOOGLE_SHEETS_CREDENTIALS`)

## GitHub

Репозиторий: [akuazuk/zoho](https://github.com/akuazuk/zoho.git)

```bash
git init
git add .
git commit -m "Initial commit: MIS/Zoho pipeline"
git remote add origin https://github.com/akuazuk/zoho.git
git branch -M main
git push -u origin main
```
