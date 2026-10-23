-- ============================================================
--  資料庫還原後核對（在伺服器上跑）
--
--  同一個檔案對兩個庫都能跑 —— 用 to_regclass 判斷表是否存在，
--  不存在的表自動跳過（所以不會因為「這個庫沒有那張表」而報錯）。
--
--  用法：
--      psql -h 127.0.0.1 -U joyli -d platform_forwarder -f verify_restore.sql
--      psql -h 127.0.0.1 -U joyli -d crm_db             -f verify_restore.sql
--
--  期望值來自本機 dump 當時的實測（2026-10-22 12:23）。
--  重點不是「必須等於這些數字」，而是「伺服器 ≈ 本機」。
--  若你的業務資料後來有變動，數字本來就會不同。
-- ============================================================

\echo ''
\echo '===== 這個庫 ====='
SELECT current_database() AS db,
       pg_size_pretty(pg_database_size(current_database())) AS size,
       current_setting('TimeZone') AS timezone,
       (SELECT count(*) FROM pg_stat_user_tables) AS tables;

-- ------------------------------------------------------------
-- 各表行數（兩庫通用的表名清單，不存在的自動跳過）
-- ------------------------------------------------------------
\echo ''
\echo '===== 各表行數 ====='
\echo '本機基準 pf : forward_log=108 igfb_dm=176 igfb_comment=72 igfb_post=13 acct_map=5'
\echo '本機基準 crm: crm_business=2 accounts=5 kb_document=22 kb_chunk=102 kb_asset=7 staff=2 log=204'
DO $$
DECLARE
    names text[] := ARRAY[
        'forward_log', 'igfb_dm', 'igfb_comment', 'igfb_post',
        'connector_account_map', 'connector_outbound_attempt', 'connector_callback_queue',
        'crm_business', 'crm_business_platform_account', 'business_ai_switch',
        'crm_staff', 'crm_session',
        'kb_collection', 'kb_document', 'kb_chunk', 'kb_asset', 'kb_param',
        'business_kb_binding', 'business_faq', 'business_table',
        'autoreply_log', 'crm_outbound_task', 'crm_ai_inbound_queue', 'crm_ops_config'
    ];
    t text;
    n bigint;
    shown int := 0;
BEGIN
    FOREACH t IN ARRAY names LOOP
        IF to_regclass(t) IS NOT NULL THEN
            EXECUTE format('SELECT count(*) FROM %I', t) INTO n;
            RAISE NOTICE '%  %', rpad(t, 34), n;
            shown := shown + 1;
        END IF;
    END LOOP;
    RAISE NOTICE '---- 本庫存在 % 張上述表 ----', shown;
END $$;

-- ------------------------------------------------------------
-- 業務資料（只在 crm_db 有）
-- ------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('crm_business') IS NULL THEN RETURN; END IF;
    RAISE NOTICE '';
    RAISE NOTICE '===== 商家與平台帳號 =====';
END $$;

DO $$
DECLARE r record;
BEGIN
    IF to_regclass('crm_business') IS NULL THEN RETURN; END IF;
    FOR r IN SELECT id, business_name, enabled,
                    (meta_app_id IS NOT NULL) AS own_app
               FROM crm_business ORDER BY id LOOP
        RAISE NOTICE 'business %  %  enabled=%  own_meta_app=%',
            r.id, rpad(coalesce(r.business_name,'(null)'), 20), r.enabled, r.own_app;
    END LOOP;
END $$;

DO $$
DECLARE r record;
BEGIN
    IF to_regclass('crm_business_platform_account') IS NULL THEN RETURN; END IF;
    RAISE NOTICE '';
    RAISE NOTICE '===== 平台帳號（對齊鍵必須有值，否則 webhook 收訊會靜默失敗）=====';
    FOR r IN SELECT id, business_id, platform, platform_business_account_id AS native,
                    (credential IS NOT NULL) AS has_cred, enabled
               FROM crm_business_platform_account ORDER BY platform, id LOOP
        RAISE NOTICE '% | biz=% | acc=% | native=% | cred=% | enabled=%',
            rpad(r.platform, 9), r.business_id, rpad(r.id::text, 6),
            rpad(coalesce(r.native, '(NULL!)'), 22), r.has_cred, r.enabled;
    END LOOP;
END $$;

-- ------------------------------------------------------------
-- RAG 向量完成度
-- ------------------------------------------------------------
DO $$
DECLARE c bigint; w bigint;
BEGIN
    IF to_regclass('kb_chunk') IS NULL THEN RETURN; END IF;
    SELECT count(*), count(*) FILTER (WHERE embedding IS NOT NULL) INTO c, w FROM kb_chunk;
    RAISE NOTICE '';
    RAISE NOTICE '===== RAG 向量化：% 塊，已完成 % ，缺 % =====', c, w, c - w;
    IF c > 0 AND w = 0 THEN
        RAISE NOTICE '⚠️  一筆向量都沒有 —— 知識庫檢索會完全失效';
    END IF;
END $$;

-- ------------------------------------------------------------
-- 搬遷遺留問題
-- ------------------------------------------------------------
DO $$
DECLARE bad bigint; total bigint;
BEGIN
    IF to_regclass('kb_document') IS NULL THEN RETURN; END IF;
    SELECT count(*) FILTER (WHERE source_path LIKE 'C:%'), count(*)
      INTO bad, total FROM kb_document;
    RAISE NOTICE '';
    RAISE NOTICE '===== source_path 檢查：% / % 筆仍是 Windows 絕對路徑 =====', bad, total;
    IF bad > 0 THEN
        RAISE NOTICE '⚠️  需要跑 docs/DEPLOY.md §3.4 的修復 SQL（否則「重新解析」會失敗）';
    END IF;
END $$;

DO $$
DECLARE bad bigint; total bigint;
BEGIN
    IF to_regclass('kb_asset') IS NULL THEN RETURN; END IF;
    SELECT count(*) FILTER (WHERE rel_path LIKE '%\%'), count(*) INTO bad, total FROM kb_asset;
    RAISE NOTICE 'kb_asset：% / % 筆 rel_path 含反斜線（Windows 路徑）', bad, total;
END $$;

-- ------------------------------------------------------------
-- AI 開關（每個商家每平台每場景都該有列；無列＝關）
-- ------------------------------------------------------------
DO $$
DECLARE biz int; sw int;
BEGIN
    IF to_regclass('business_ai_switch') IS NULL THEN RETURN; END IF;
    SELECT count(*) INTO biz FROM crm_business;
    SELECT count(*) INTO sw FROM business_ai_switch;
    RAISE NOTICE '';
    RAISE NOTICE '===== AI 開關：% 個商家，% 筆開關列（每商家應為 6 筆：4 dm + 2 comment）=====', biz, sw;
    IF biz > 0 AND sw < biz * 6 THEN
        RAISE NOTICE '⚠️  開關列不足 —— 啟動時 backfill_ai_switches() 會補，但先確認是預期行為';
    END IF;
END $$;

\echo ''
\echo '===== 完成 ====='
