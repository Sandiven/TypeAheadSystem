CREATE TEMP TABLE queries_staging (query_text TEXT, historical_count INT);
COPY queries_staging(query_text, historical_count) FROM '/tmp/queries.csv' CSV HEADER;
INSERT INTO queries (query_text, historical_count, last_searched_at)
SELECT query_text, historical_count, NOW()
FROM queries_staging
ON CONFLICT (query_text)
DO UPDATE SET historical_count = EXCLUDED.historical_count, last_searched_at = NOW();
SELECT COUNT(*) AS total_queries FROM queries;
