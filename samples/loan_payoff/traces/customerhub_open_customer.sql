-- SYNTHETIC: statements the CustomerHub app ran when it opened one customer, one per line,
-- in the normalized form pg_stat_statements / an Extended Events export shows ($1 = bound value).
SELECT "CUST_ID", "FIRST_NM", "LAST_NM", "FULL_NAME", "SSN", "DOB", "EMAIL", "PHONE" FROM lpo."CUST_MSTR" WHERE "CUST_ID" = $1
SELECT "ADDR_SEQ", "LINE1", "CITY", "POSTCODE" FROM lpo."CUST_ADDR" WHERE "CUST_ID" = $1 ORDER BY "ADDR_SEQ"
SELECT l."LOAN_NO", l."PRODUCT_CD", l."PRINCIPAL_BAL", l."STATUS_CD" FROM lpo."LOAN_ACCT" l WHERE l."CUST_ID" = $1
