-- 0002 : horodatage de mise à jour des jobs d'ingestion (observabilité des
-- baux et des reprises). Ajouté après 0001, déjà appliquée : jamais de
-- modification rétroactive d'une migration livrée.
ALTER TABLE ingestion_jobs
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
