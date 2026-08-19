BEGIN;

CREATE TABLE IF NOT EXISTS public.supply_embeddings (
  supply_code text PRIMARY KEY,
  embedding double precision[] NOT NULL,
  embedding_source_text text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT supply_embeddings_dimension_check
    CHECK (array_length(embedding, 1) = 384)
);

COMMIT;
