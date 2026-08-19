import os
import time
import psycopg
from dotenv import load_dotenv
from fastembed import TextEmbedding

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("Error: DATABASE_URL must be set in .env")
    exit(1)

def build_source_text(row):
    """Builds the text to embed from the supply row fields."""
    supply_code = row.get("supply_code") or ""
    customer_name = row.get("customer_name") or "Sin Nombre"
    service_address = row.get("service_address") or ""
    district = row.get("district") or ""
    route_code = row.get("route_code") or ""
    supply_status = row.get("supply_status") or ""
    
    parts = [
        f"NIS: {supply_code}",
        f"Cliente: {customer_name}",
        f"Direccion: {service_address}",
        f"Distrito: {district}",
        f"Ruta: {route_code}",
        f"Estado: {supply_status}"
    ]
    return " | ".join(parts)

def backfill():
    print("Connecting to local database...")
    local_conn = psycopg.connect(DATABASE_URL)
    local_cur = local_conn.cursor()
    
    # Query all supplies from the local database
    print("Querying supplies from local database...")
    local_cur.execute("""
        SELECT supply_code, customer_name, service_address, district, route_code, supply_status
        FROM public.customer_supplies cs
        WHERE NOT EXISTS (
          SELECT 1 FROM public.supply_embeddings embedding
          WHERE embedding.supply_code = cs.supply_code
        )
        ORDER BY supply_code
    """)
    
    columns = [desc[0] for desc in local_cur.description]
    rows = [dict(zip(columns, row)) for row in local_cur.fetchall()]
    total_rows = len(rows)
    print(f"Total supplies to process: {total_rows}")
    
    if total_rows == 0:
        print("No supplies found in local database.")
        return
        
    print("Initializing fastembed model (BAAI/bge-small-en-v1.5)...")
    model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
    
    batch_size = 500
    start_time = time.time()
    
    for i in range(0, total_rows, batch_size):
        batch = rows[i:i + batch_size]
        batch_texts = []
        batch_codes = []
        
        for row in batch:
            source_text = build_source_text(row)
            batch_texts.append(source_text)
            batch_codes.append(row["supply_code"])
            
        # Generate embeddings
        embeddings_gen = model.embed(batch_texts)
        embeddings = [list(float(x) for x in emb) for emb in embeddings_gen]
        
        # Construct single-query batch insert statement
        placeholders = ", ".join(["(%s, %s, %s)"] * len(batch))
        query = f"""
            INSERT INTO public.supply_embeddings (supply_code, embedding, embedding_source_text)
            VALUES {placeholders}
            ON CONFLICT (supply_code) DO UPDATE
            SET 
                embedding = EXCLUDED.embedding,
                embedding_source_text = EXCLUDED.embedding_source_text,
                updated_at = NOW();
        """
        
        flat_params = []
        for code, text, emb in zip(batch_codes, batch_texts, embeddings):
            flat_params.extend([code, emb, text])
            
        # Execute batch upsert
        local_cur.execute(query, flat_params)
        local_conn.commit()
                
        elapsed = time.time() - start_time
        processed = min(i + batch_size, total_rows)
        rate = processed / elapsed
        eta_seconds = (total_rows - processed) / rate if rate > 0 else 0
        print(f"Processed {processed}/{total_rows} ({processed/total_rows*100:.1f}%). Elapsed: {elapsed:.1f}s. Rate: {rate:.1f} items/s. ETA: {eta_seconds/60:.1f}m", flush=True)
        
    print(f"Completed! Total time: {time.time() - start_time:.1f}s")
    
    local_conn.close()

if __name__ == "__main__":
    backfill()
