import os, pandas as pd, psycopg
from screener_loader import SCREENER_COLUMNS

DB = os.environ["DATABASE_URL"]

print("Loading screener.csv...")
df = pd.read_csv("screener.csv")

# Rename using existing mapping
df = df.rename(columns={"NSE Code": "nse_code", "Name": "company_name"})
df = df.rename(columns=SCREENER_COLUMNS)

# Clean NSE codes
df = df[df["nse_code"].notna()]
df["nse_code"] = df["nse_code"].astype(str).str.strip()
df = df[~df["nse_code"].isin(["", "nan"])]
df = df.drop_duplicates(subset="nse_code", keep="first").reset_index(drop=True)
print(f"Rows: {len(df)} | Cols: {len(df.columns)}")

# Fix mixed columns — force numeric where possible
for c in df.columns:
    if str(df[c].dtype) != "object":
        df[c] = pd.to_numeric(df[c], errors="coerce")

# Build column definitions for Postgres
def pg_type(dtype):
    return "TEXT" if dtype == "object" else "NUMERIC"

cols_def = ",\n  ".join(f'"{c}" {pg_type(str(df[c].dtype))}' for c in df.columns)

with psycopg.connect(DB) as conn:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS screener_raw")
        cur.execute(f"""
            CREATE TABLE screener_raw (
              id SERIAL PRIMARY KEY,
              {cols_def},
              loaded_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        print("Table created.")

        cols = list(df.columns)
        placeholders = ", ".join(["%s"] * len(cols))
        col_names = ", ".join([f'"{c}"' for c in cols])
        insert_sql = f"INSERT INTO screener_raw ({col_names}) VALUES ({placeholders})"

        batch, total = [], 0
        for _, row in df.iterrows():
            vals = []
            for c in cols:
                v = row[c]
                try:
                    if pd.isna(v):
                        vals.append(None)
                    elif str(df[c].dtype) == "object":
                        vals.append(str(v))
                    else:
                        vals.append(float(v))
                except (ValueError, TypeError):
                    vals.append(str(v) if v is not None else None)
            batch.append(vals)
            if len(batch) == 100:
                cur.executemany(insert_sql, batch)
                conn.commit()
                total += len(batch)
                batch = []
                print(f"  {total} inserted...")

        if batch:
            cur.executemany(insert_sql, batch)
            conn.commit()
            total += len(batch)

        print(f"Done. {total} rows in screener_raw.")