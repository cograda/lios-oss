Import CSV bank statements into the finance system.

Arguments: $ARGUMENTS

## Process

### Step 1 — Find CSVs

Scan for CSV files in this order:

1. If a path was provided as an argument, use that (file or directory)
2. Otherwise scan `~/Desktop/Finance/` for CSV files
3. If nothing found there, check `server/csv-inbox/` as a fallback

If no CSVs found anywhere, tell the user to drop bank statement CSVs into `~/Desktop/Finance/` and re-run.

If a filter was provided (e.g. "aib" or "revolut"), only process matching files.

### Step 2 — Import each file

For each CSV file:

1. Read the full file content
2. Call `finance_import_csv` with:
   - `content`: the full CSV text
   - `filename`: the original filename (important — Revolut account detection uses filename hashes)
   - Do NOT provide `account_name` or `account_type` — let the server auto-detect via fingerprints

3. Handle the response:
   - **`success`** — report the results (imported, duplicates, categorized, transfers)
   - **`duplicate`** — skip, mention it was already imported
   - **`unknown_account`** — this is a new fingerprint. Show the user:
     - The detected format and fingerprint (e.g. "Revolut hash: de3b34")
     - The first few sample transactions from the response
     - Ask which account this belongs to (e.g. "Revolut Alex", "AIB Joint")
     - Then call `finance_register_fingerprint` with the fingerprint details + user's answer
     - Then re-call `finance_import_csv` with explicit `account_name` and `account_type`
   - **`error`** — report the error

### Step 3 — Report

Show a summary for each file:
- Filename
- Account (auto-detected or user-specified)
- Transactions imported / duplicates skipped
- Categorised / uncategorised

Then show the overall categorisation coverage by calling `finance_summary`.

If there are uncategorised transactions, suggest running `finance_uncategorized` to see what needs rules, or offer to review them now.

## Account Names

Known accounts (for reference when asking the user):
- AIB: "AIB Current", "AIB Joint", "AIB Sam"
- Revolut: "Revolut Alex", "Revolut Sam", "Revolut Joint"

## Examples

- `/import-finance` — scans ~/Desktop/Finance/, auto-detects accounts, imports all CSVs
- `/import-finance ~/Downloads/statement.csv` — import a specific file
- `/import-finance revolut` — only import Revolut-format CSVs
