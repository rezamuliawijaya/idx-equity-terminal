# Synthetic demonstration data

Everything in this folder is generated arithmetically by `src/data/sample_data.py`. The issuers do **not** exist and the prices are random walks. It exists so the application can be exercised end to end.

Load it from the Data Management page, or run:

```
python scripts/seed_demo_data.py
```

Real data must be imported from IDX filings (https://www.idx.co.id/) using `financial_template.csv`, with the source URL recorded on every row.
