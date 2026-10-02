SELECT * FROM (SELECT 'name t (uuid c202..)' AS tbl, id, src FROM a.t UNION ALL SELECT 'name t_new (uuid 74a5..)' AS tbl, id, src FROM a.t_new) ORDER BY tbl, id;
