import psycopg2
import pandas as pd
from dotenv import load_dotenv
import os
from datetime import date

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

def insert_gvm_scores():
    df = pd.read_csv('gvm_analytics.csv')
    today = date.today()
    
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    inserted = 0
    for _, row in df.iterrows():
        cur.execute("""
            INSERT INTO gvm_scores 
                (nse_code, stock_name, segment, g_score, v_score, m_score, gvm_score, verdict, commentary, score_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (
            str(row['NSE Code']).strip(),
            str(row['Name']).strip(),
            str(row['Segment']).strip(),
            float(row['G Score']) if pd.notna(row['G Score']) else None,
            float(row['V Score']) if pd.notna(row['V Score']) else None,
            float(row['M Score']) if pd.notna(row['M Score']) else None,
            float(row['GVM Score']) if pd.notna(row['GVM Score']) else None,
            str(row['Verdict']).strip(),
            str(row['Punchline']).strip(),
            today
        ))
        inserted += 1
    
    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ {inserted} stocks inserted into gvm_scores for {today}")

if __name__ == "__main__":
    insert_gvm_scores()