# HR Projections 26 Portal Starter

This starter package creates a Supabase + Streamlit portal for publishing HR Projections 26 output workbooks.

It supports:

- One published record per model run
- Interactive Streamlit dashboard
- Filters, sorting, search, and run archive
- Private Supabase Storage bucket for output workbooks
- Rolling 5-day data retention
- Manual/local purge script
- Optional Supabase Edge Function purge job
- Optional future paid-access policy template

## Folder layout

```text
sql/
  01_schema.sql
  02_public_read_policies.sql
  03_private_paid_access_policies_later.sql
scripts/
  import_output_workbook.py
  purge_expired.py
streamlit_app/
  app.py
.streamlit/
  secrets.toml.example
supabase/functions/purge-expired/
  index.ts
.github/workflows/
  purge-expired.yml
requirements.txt
.env.example
```

## 1) Supabase setup

In Supabase SQL Editor, run these in order:

1. `sql/01_schema.sql`
2. `sql/02_public_read_policies.sql`

Then create a private Storage bucket:

```text
hr-projections-outputs
```

For the MVP, the dashboard reads active non-expired prediction data. Admin imports use the service role key from local/server-side secrets.

## 2) Local Python setup

From this folder:

```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate   # Windows PowerShell
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```env
SUPABASE_URL=https://YOUR_PROJECT_REF.supabase.co
SUPABASE_ANON_KEY=YOUR_SUPABASE_ANON_KEY
SUPABASE_SERVICE_ROLE_KEY=YOUR_SERVICE_ROLE_KEY
SUPABASE_STORAGE_BUCKET=hr-projections-outputs
```

Never commit `.env` or the real `.streamlit/secrets.toml` file.

## 3) Import a model output workbook

After ChatGPT produces an HR Projections 26 output workbook, run:

```bash
python scripts/import_output_workbook.py \
  --workbook "/path/to/HR_Projections_26_Output.xlsx" \
  --slate-date 2026-06-24 \
  --source-workbook-name "Raw_MLB_Workbook.xlsx" \
  --notes "Initial portal import"
```

The script will:

1. Read every sheet in the output workbook.
2. Create a `prediction_runs` record.
3. Mark older runs as not latest.
4. Upload the workbook to Supabase Storage.
5. Insert sheet rows into `prediction_rows` with the original row data preserved as JSON.
6. Extract common dashboard fields for fast display.

## 4) Run the Streamlit app locally

Copy the secrets template:

```bash
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
```

Edit `.streamlit/secrets.toml`:

```toml
SUPABASE_URL = "https://YOUR_PROJECT_REF.supabase.co"
SUPABASE_ANON_KEY = "YOUR_SUPABASE_ANON_KEY"
```

Start the app:

```bash
streamlit run streamlit_app/app.py
```

## 5) Deploy Streamlit

Recommended first deploy:

1. Push this folder to a private GitHub repo.
2. Deploy `streamlit_app/app.py` to Streamlit Community Cloud or another Streamlit host.
3. Add these as app secrets:

```toml
SUPABASE_URL = "https://YOUR_PROJECT_REF.supabase.co"
SUPABASE_ANON_KEY = "YOUR_SUPABASE_ANON_KEY"
```

Do not paste the service role key into a public repo. The service role key should only be used by import/purge jobs running in a secure server-side context.

## 6) Purge expired data after 5 days

Manual/local purge:

```bash
python scripts/purge_expired.py
```

This deletes expired workbook files from Supabase Storage first, then deletes expired database runs. `prediction_rows` and `published_files` are deleted by cascade when their run is deleted.

Optional automation paths:

- GitHub Actions: use `.github/workflows/purge-expired.yml` and add repo secrets.
- Supabase Edge Function: deploy `supabase/functions/purge-expired/index.ts`, then schedule it using Supabase Cron / pg_cron + pg_net.

## 7) Future paid access

The starter is public-read for non-expired runs. Later, when Stripe + Supabase Auth are ready:

1. Add Supabase Auth login.
2. Add Stripe Checkout + webhook.
3. Store subscription state in `subscriptions`.
4. Run `sql/03_private_paid_access_policies_later.sql` after testing.
5. Update the Streamlit app to require login and check active subscription before showing predictions.

## Security notes

- `SUPABASE_ANON_KEY` can be used by the Streamlit app if RLS is enabled and only select policies are exposed.
- `SUPABASE_SERVICE_ROLE_KEY` bypasses RLS and must stay server-side only.
- Keep the Storage bucket private.
- Public users should not be able to insert, update, or delete prediction data.
- Published model data is not guaranteed to be erased from managed backups immediately after purge; the purge controls live portal/database/storage visibility and active storage usage.

## Suggested daily workflow

1. Upload latest raw MLB workbook to ChatGPT.
2. Run the locked HR Projections 26 model.
3. Download the values-only output workbook.
4. Import it:

```bash
python scripts/import_output_workbook.py --workbook "output.xlsx" --slate-date YYYY-MM-DD --source-workbook-name "raw.xlsx"
```

5. Refresh the Streamlit portal.
6. The previous published runs stay visible until they expire after 5 days.
